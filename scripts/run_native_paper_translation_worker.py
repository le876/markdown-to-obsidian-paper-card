#!/usr/bin/env python3
"""Prepare and import one native paper-translation subagent handoff."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from paper_translation_packet import append_validated_cache, read_packet, validate_output
from run_paper_translation_worker import (
    TRANSPORT_SCHEMA_VERSION,
    build_final_status_schema,
    build_transport_rows,
    canonicalize_worker_rows,
    sha256,
)
from safe_atomic_io import atomic_write_bytes, atomic_write_json
from sync_paper_translation_agent_prompt import (
    DEFAULT_MODEL,
    DEFAULT_REASONING_EFFORT,
    read_fragment,
    render_agent,
)


NATIVE_BRIDGE_SCHEMA_VERSION = 1
DEFAULT_AGENT_ROLE = "paper-translation-worker"
UNIT_FAILURE_THRESHOLD = 5


def is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def read_jsonl(path: Path, *, encoding: str = "utf-8") -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, raw in enumerate(path.read_text(encoding=encoding).splitlines(), start=1):
        if not raw.strip():
            continue
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError(f"expected a JSON object at {path}:{line_number}")
        rows.append(value)
    return rows


def normalized_text(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n").rstrip() + "\n"


def validate_role(
    role_path: Path,
    *,
    expected_name: str,
    expected_model: str,
    expected_effort: str,
    prompt_sha256: str,
    translator_fingerprint: str,
) -> str:
    if not role_path.is_file():
        raise ValueError(f"paper translation role does not exist: {role_path}")
    if role_path.read_bytes().startswith(b"\xef\xbb\xbf"):
        raise ValueError("paper translation role must be UTF-8 without BOM")
    role_text = role_path.read_text(encoding="utf-8")
    role = tomllib.loads(role_text)
    if role.get("name") != expected_name:
        raise ValueError("paper translation role name does not match the requested agent role")
    if role.get("model") != expected_model:
        raise ValueError("paper translation role model does not match the frozen assignment")
    if str(role.get("model_reasoning_effort", "")).lower() != expected_effort.lower():
        raise ValueError("paper translation role effort does not match the frozen assignment")

    fragment_path = Path(__file__).resolve().parents[1] / "references" / "paper-translation-prompt-fragment.md"
    rendered, actual_prompt_sha256, actual_fingerprint = render_agent(
        read_fragment(fragment_path),
        model=expected_model,
        reasoning_effort=expected_effort,
    )
    if actual_prompt_sha256 != prompt_sha256:
        raise ValueError("paper translation role prompt SHA-256 is stale")
    if actual_fingerprint != translator_fingerprint:
        raise ValueError("paper translation role fingerprint is stale")
    if normalized_text(rendered) != normalized_text(role_text):
        raise ValueError(
            "paper translation role is not synchronized with the authoritative prompt fragment"
        )
    return sha256(role_path)


def attempt_number(state: dict[str, Any], workflow_dir: Path) -> int:
    attempts = (
        (((state.get("stages") or {}).get("translation_worker") or {}).get("attempts"))
        or []
    )
    frozen_attempts = [len(attempts)]
    for path in workflow_dir.glob("translation-native-attempt-*-assignment.json"):
        match = re.fullmatch(r"translation-native-attempt-(\d+)-assignment\.json", path.name)
        if match:
            frozen_attempts.append(int(match.group(1)))
    return max(frozen_attempts) + 1


def task_name_for(workflow_dir: Path, attempt: int) -> str:
    resolved = workflow_dir.resolve()
    readable_name = resolved.name
    if readable_name.casefold() in {"workflow", ".workflow", "translation_workflow"}:
        readable_name = resolved.parent.name or readable_name
    slug = re.sub(r"[^a-z0-9]+", "_", readable_name.lower()).strip("_")
    slug = (slug or "paper")[:24].rstrip("_")
    normalized_identity = os.path.normcase(str(resolved)).replace("\\", "/")
    workflow_hash = hashlib.sha256(normalized_identity.encode("utf-8")).hexdigest()[:16]
    return f"translate_{slug}_{workflow_hash}_a{attempt}"


def prepare(args: argparse.Namespace) -> dict[str, Any]:
    workflow_dir = Path(args.workflow_dir).resolve()
    assignment_path = Path(args.assignment).resolve()
    role_path = Path(args.agent_role_file).resolve()
    if not workflow_dir.is_dir():
        raise ValueError("workflow-dir must be an existing directory")
    if not assignment_path.is_file() or not is_within(assignment_path, workflow_dir):
        raise ValueError("assignment must be an existing file inside workflow-dir")

    source_assignment = read_json(assignment_path)
    if source_assignment.get("schema_version") not in {1, 2, 3}:
        raise ValueError("unsupported translation assignment schema")
    pending_path = Path(str(source_assignment["packet_path"])).resolve()
    output_path = Path(str(source_assignment["output_path"])).resolve()
    if not is_within(pending_path, workflow_dir) or not is_within(output_path, workflow_dir):
        raise ValueError("assignment packet and output must stay inside workflow-dir")
    if not pending_path.is_file() or sha256(pending_path) != source_assignment.get("packet_sha256"):
        raise ValueError("pending packet is missing or its SHA-256 does not match assignment")
    if output_path.exists():
        raise ValueError(
            "translation output already exists; validate/finalize it or use a fresh workflow"
        )

    parent_thread_id = str(
        getattr(args, "parent_thread_id", None) or os.environ.get("CODEX_THREAD_ID") or ""
    ).strip()
    if not parent_thread_id:
        raise ValueError(
            "native subagent handoff requires the parent CODEX_THREAD_ID"
        )

    requested = source_assignment.get("requested_runtime") or {}
    model = str(requested.get("model") or DEFAULT_MODEL)
    effort = str(requested.get("reasoning_effort") or DEFAULT_REASONING_EFFORT)
    role_sha256 = validate_role(
        role_path,
        expected_name=args.agent_role,
        expected_model=model,
        expected_effort=effort,
        prompt_sha256=str(source_assignment.get("prompt_sha256", "")),
        translator_fingerprint=str(source_assignment.get("translator_fingerprint", "")),
    )
    role_snapshot_path = workflow_dir / "translation-agent-role-snapshot.toml"
    atomic_write_bytes(role_snapshot_path, role_path.read_bytes(), min_bytes=20)
    if sha256(role_snapshot_path) != role_sha256:
        raise ValueError("workflow role snapshot differs from the validated source role")

    pending_rows = read_jsonl(pending_path)
    transport_rows, _ = build_transport_rows(pending_rows)
    if len(transport_rows) != int(source_assignment.get("unit_count", -1)):
        raise ValueError("compact transport count differs from the frozen assignment")

    state_path = workflow_dir / "workflow-state.json"
    state = read_json(state_path) if state_path.is_file() else {}
    attempt = attempt_number(state, workflow_dir)
    stem = f"translation-native-attempt-{attempt}"
    transport_path = workflow_dir / f"{stem}-input.jsonl"
    native_assignment_path = workflow_dir / f"{stem}-assignment.json"
    schema_path = workflow_dir / "translation-native-final.schema.json"
    transport_content = "".join(
        json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
        for row in transport_rows
    )
    transport_bytes = b"\xef\xbb\xbf" + transport_content.encode("utf-8")
    atomic_write_bytes(transport_path, transport_bytes, min_bytes=5)
    atomic_write_json(schema_path, build_final_status_schema(len(transport_rows)))

    task_name = args.task_name or task_name_for(workflow_dir, attempt)
    if not re.fullmatch(r"[a-z0-9_]+", task_name):
        raise ValueError("task-name must contain only lowercase letters, digits, and underscores")
    agent_path = f"/root/{task_name}"
    native_assignment = {
        "schema_version": NATIVE_BRIDGE_SCHEMA_VERSION,
        "transport_schema_version": TRANSPORT_SCHEMA_VERSION,
        "source_assignment_path": str(assignment_path),
        "source_assignment_sha256": sha256(assignment_path),
        "source_pending_packet_path": str(pending_path),
        "source_pending_packet_sha256": sha256(pending_path),
        "packet_path": str(transport_path),
        "packet_sha256": sha256(transport_path),
        "packet_contract": {
            "encoding": "UTF-8",
            "bom": "required",
            "newline": "LF",
            "row_schema": "compact id/type/text-or-title-or-entry",
            "field_count_by_type": {
                "body": 3,
                "reference_title_deterministic": 3,
                "reference_title_fallback": 3,
            },
        },
        "unit_count": len(transport_rows),
        "body_units": sum(row.get("type") == "body" for row in transport_rows),
        "reference_title_units": sum(
            row.get("type") == "reference_title" for row in transport_rows
        ),
        "fallback_title_units": sum("entry" in row for row in transport_rows),
        "output_path": str(output_path),
        "output_contract": {
            "encoding": "UTF-8",
            "bom": "forbidden",
            "newline": "LF",
            "schema": "unit_id-to-translated-text JSONL",
        },
        "final_schema_path": str(schema_path),
        "final_schema_sha256": sha256(schema_path),
        "agent_role": args.agent_role,
        "agent_role_path": str(role_snapshot_path),
        "agent_role_source_path": str(role_path),
        "agent_role_sha256": role_sha256,
        "task_name": task_name,
        "agent_path": agent_path,
        "parent_thread_id": parent_thread_id,
        "fork_turns": "none",
        "requested_runtime": {"model": model, "reasoning_effort": effort},
        "prompt_sha256": source_assignment.get("prompt_sha256"),
        "translator_fingerprint": source_assignment.get("translator_fingerprint"),
        "prepared_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "prepared_at_unix": time.time(),
    }
    atomic_write_json(native_assignment_path, native_assignment)

    artifacts = state.setdefault("artifacts", {})
    artifacts.update(
        {
            "translation_native_assignment": native_assignment_path.name,
            "translation_native_input": transport_path.name,
            "translation_native_final_schema": schema_path.name,
            "translation_agent_role_snapshot": role_snapshot_path.name,
        }
    )
    worker_stage = state.setdefault("stages", {}).setdefault("translation_worker", {})
    worker_stage.update(
        {
            "status": "awaiting_native_subagent",
            "metadata": {
                **dict(worker_stage.get("metadata") or {}),
                "backend": "native_subagent",
                "agent_role": args.agent_role,
                "agent_path": agent_path,
                "pending_count": len(transport_rows),
                "completion_waits": 0,
            },
        }
    )
    state["worker_backend"] = "native-subagent"
    atomic_write_json(state_path, state)

    task_message = (
        f"Read the UTF-8 assignment manifest at {native_assignment_path}. "
        "Read only the compact packet named by that manifest. "
        "Follow the paper-translation-worker role and return exactly one JSON object "
        "matching the manifest final schema. Do not read the complete Markdown note, "
        "other skills, AGENTS.md, plugins, connectors, or parent history. Do not create "
        "another agent and do not write files."
    )
    return {
        "ok": True,
        "stage": "native_subagent_prepare",
        "worker_backend": "native-subagent",
        "unit_count": len(transport_rows),
        "assignment_path": str(native_assignment_path),
        "assignment_sha256": sha256(native_assignment_path),
        "packet_path": str(transport_path),
        "packet_sha256": sha256(transport_path),
        "agent_role": args.agent_role,
        "agent_role_path": str(role_snapshot_path),
        "agent_role_source_path": str(role_path),
        "agent_role_sha256": role_sha256,
        "task_name": task_name,
        "agent_path": agent_path,
        "spawn_agent": {
            "agent_type": args.agent_role,
            "task_name": task_name,
            "fork_turns": "none",
            "message": task_message,
        },
    }


def session_meta_from(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            for raw in handle:
                if not raw.strip():
                    continue
                value = json.loads(raw)
                if value.get("type") == "session_meta":
                    payload = value.get("payload")
                    return payload if isinstance(payload, dict) else None
                return None
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return None


def discover_rollout(
    sessions_root: Path,
    *,
    agent_path: str,
    agent_role: str,
    parent_thread_id: str,
    prepared_at_unix: float,
) -> Path:
    matches: list[Path] = []
    for candidate in sessions_root.rglob("rollout-*.jsonl"):
        try:
            if candidate.stat().st_mtime + 600 < prepared_at_unix:
                continue
        except OSError:
            continue
        meta = session_meta_from(candidate)
        if not meta:
            continue
        if (
            meta.get("agent_path") == agent_path
            and meta.get("agent_role") == agent_role
            and meta.get("parent_thread_id") == parent_thread_id
        ):
            matches.append(candidate.resolve())
    if len(matches) != 1:
        raise ValueError(
            f"expected exactly one native subagent rollout for {agent_path}, found {len(matches)}"
        )
    return matches[0]


def load_rollout(path: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], str]:
    session_meta: dict[str, Any] | None = None
    turn_context: dict[str, Any] | None = None
    task_complete: dict[str, Any] | None = None
    assistant_messages: list[str] = []
    for line_number, raw in enumerate(
        path.read_text(encoding="utf-8-sig").splitlines(), start=1
    ):
        if not raw.strip():
            continue
        item = json.loads(raw)
        if not isinstance(item, dict):
            raise ValueError(f"invalid rollout record at line {line_number}")
        payload = item.get("payload") or {}
        if item.get("type") == "session_meta":
            session_meta = payload
        elif item.get("type") == "turn_context":
            turn_context = payload
        elif (
            item.get("type") == "response_item"
            and payload.get("type") == "message"
            and payload.get("role") == "assistant"
        ):
            text = "".join(
                str(part.get("text"))
                for part in payload.get("content") or []
                if part.get("type") == "output_text"
            )
            if text:
                assistant_messages.append(text)
        elif item.get("type") == "event_msg" and payload.get("type") == "task_complete":
            task_complete = payload
    if not session_meta or not turn_context or not task_complete:
        raise ValueError("native subagent rollout is missing runtime or completion records")
    if len(assistant_messages) != 1:
        raise ValueError(
            "native paper translation subagent must emit exactly one assistant final message"
        )
    assistant_message = assistant_messages[0]
    completed_message = task_complete.get("last_agent_message")
    if isinstance(completed_message, str) and completed_message != assistant_message:
        raise ValueError("task-complete final message differs from the assistant final response")
    return session_meta, turn_context, task_complete, assistant_message


def unix_to_iso(value: Any, fallback: str | None = None) -> str | None:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")
    return fallback


def validate_runtime(
    *,
    session_meta: dict[str, Any],
    turn_context: dict[str, Any],
    native_assignment: dict[str, Any],
) -> dict[str, Any]:
    source = session_meta.get("source") or {}
    spawn = ((source.get("subagent") or {}).get("thread_spawn") or {})
    expected_role = native_assignment["agent_role"]
    expected_path = native_assignment["agent_path"]
    requested = native_assignment["requested_runtime"]
    if session_meta.get("thread_source") != "subagent":
        raise ValueError("rollout is not a native subagent session")
    if session_meta.get("agent_role") != expected_role or spawn.get("agent_role") != expected_role:
        raise ValueError("native subagent did not use the requested paper translation role")
    if session_meta.get("agent_path") != expected_path or spawn.get("agent_path") != expected_path:
        raise ValueError("native subagent path does not match the frozen assignment")
    parent = session_meta.get("parent_thread_id")
    expected_parent = native_assignment.get("parent_thread_id")
    if not parent or spawn.get("parent_thread_id") != parent:
        raise ValueError("native subagent parent-thread provenance is inconsistent")
    if not expected_parent or parent != expected_parent:
        raise ValueError("native subagent does not belong to the frozen parent thread")
    if session_meta.get("session_id") and session_meta.get("session_id") != parent:
        raise ValueError("native subagent session parent identifier is inconsistent")
    if turn_context.get("model") != requested["model"]:
        raise ValueError("native subagent did not run the requested model")
    if str(turn_context.get("effort", "")).lower() != str(
        requested["reasoning_effort"]
    ).lower():
        raise ValueError("native subagent did not run at the requested reasoning effort")
    sandbox_policy = turn_context.get("sandbox_policy") or {}
    return {
        "model": turn_context.get("model"),
        "reasoning_effort": str(turn_context.get("effort", "")).lower(),
        "sandbox": sandbox_policy.get("type"),
        "codex_version": session_meta.get("cli_version"),
        "session_id": session_meta.get("id"),
        "parent_thread_id": parent,
        "agent_path": session_meta.get("agent_path"),
        "agent_role": session_meta.get("agent_role"),
        "runtime_verified": True,
        "runtime_kind": "native_subagent",
    }


def strict_final_message(text: str, unit_count: int) -> dict[str, Any]:
    final = json.loads(text.strip())
    if not isinstance(final, dict):
        raise ValueError("native subagent final response must be a JSON object")
    required = {"status", "translated_units", "translations", "reference_titles"}
    if set(final) != required:
        raise ValueError("native subagent final response has missing or extra top-level fields")
    if final.get("status") != "completed":
        raise ValueError("native subagent did not report completed status")
    if final.get("translated_units") != unit_count:
        raise ValueError("native subagent translated_units does not match assignment")
    if not isinstance(final.get("translations"), list) or not isinstance(
        final.get("reference_titles"), list
    ):
        raise ValueError("native subagent translation arrays are invalid")
    return final


def update_native_state(
    *,
    state_path: Path,
    native_assignment: dict[str, Any],
    actual: dict[str, Any],
    attestation_path: Path,
    attestation: dict[str, Any],
) -> None:
    state = read_json(state_path)
    worker_stage = state.setdefault("stages", {}).setdefault("translation_worker", {})
    attempts = list(worker_stage.get("attempts") or [])
    attempts.append(
        {
            "attempt": len(attempts) + 1,
            "source": "native_collaboration_spawn_agent",
            "pending_units": native_assignment["unit_count"],
            "started_at": attestation.get("started_at"),
            "finished_at": attestation.get("finished_at"),
            "duration_ms": attestation.get("duration_ms"),
            "requested_model": native_assignment["requested_runtime"]["model"],
            "actual_model": actual["model"],
            "requested_reasoning_effort": native_assignment["requested_runtime"][
                "reasoning_effort"
            ],
            "actual_reasoning_effort": actual["reasoning_effort"],
            "runtime_verified": True,
            "runtime_kind": "native_subagent",
            "agent_role": actual["agent_role"],
            "session_id": actual["session_id"],
            "output_sha256": attestation.get("output_sha256"),
            "exit_code": 0,
            "completion_waits": 1,
            "failure_stage": attestation.get("failure_stage"),
            "failure_class": attestation.get("failure_class"),
            "retryable": attestation.get("retryable", False),
            "failure_reasons": attestation.get("failure_reasons", []),
            "contract_errors": attestation.get("contract_errors", []),
            "unit_errors": attestation.get("unit_errors", {}),
            "partial_validated_units": attestation.get("partial_validated_units", 0),
            "invalid_unit_ids": attestation.get("invalid_unit_ids", []),
            "overall_failed": attestation.get("overall_failed", False),
        }
    )
    attempt_status = str(attestation.get("status") or "completed")
    stage_status = {
        "completed": "completed",
        "partial_success": "recovery_pending",
        "failed": "failed",
    }.get(attempt_status, "failed")
    worker_stage.update(
        {
            "status": stage_status,
            "attempts": attempts,
            "finished_at": attestation.get("finished_at"),
            "duration_ms": attestation.get("duration_ms"),
            "metadata": {
                **dict(worker_stage.get("metadata") or {}),
                "backend": "native_subagent",
                "pending_count": native_assignment["unit_count"],
                "completion_waits": 1,
                "runtime_verified": True,
                "runtime_source": "native_subagent",
                "validated_units_recovered": attestation.get("validated_units_recovered", 0),
                "invalid_units": len(attestation.get("invalid_unit_ids") or []),
                "unit_failure_threshold": UNIT_FAILURE_THRESHOLD,
            },
        }
    )
    state.setdefault("translator", {})["actual"] = actual
    state.setdefault("artifacts", {})[
        "translation_runtime_attestation"
    ] = attestation_path.name
    state["translation_runtime_attestation_sha256"] = sha256(attestation_path)
    state["worker_backend"] = "native-subagent"
    atomic_write_json(state_path, state)


def import_result(args: argparse.Namespace) -> dict[str, Any]:
    workflow_dir = Path(args.workflow_dir).resolve()
    native_assignment_path = Path(args.assignment).resolve()
    if not native_assignment_path.is_file() or not is_within(
        native_assignment_path, workflow_dir
    ):
        raise ValueError("native assignment must be an existing file inside workflow-dir")
    native_assignment = read_json(native_assignment_path)
    if native_assignment.get("schema_version") != NATIVE_BRIDGE_SCHEMA_VERSION:
        raise ValueError("unsupported native bridge assignment schema")

    role_path = Path(str(native_assignment["agent_role_path"])).resolve()
    if (
        not role_path.is_file()
        or not is_within(role_path, workflow_dir)
        or sha256(role_path) != native_assignment["agent_role_sha256"]
    ):
        raise ValueError("frozen workflow role snapshot is missing or changed")
    source_assignment_path = Path(str(native_assignment["source_assignment_path"])).resolve()
    if (
        not source_assignment_path.is_file()
        or sha256(source_assignment_path)
        != native_assignment["source_assignment_sha256"]
    ):
        raise ValueError("source assignment changed after native handoff preparation")
    pending_path = Path(str(native_assignment["source_pending_packet_path"])).resolve()
    if (
        not pending_path.is_file()
        or sha256(pending_path) != native_assignment["source_pending_packet_sha256"]
    ):
        raise ValueError("source pending packet changed after native handoff preparation")
    transport_path = Path(str(native_assignment["packet_path"])).resolve()
    if (
        not transport_path.is_file()
        or sha256(transport_path) != native_assignment["packet_sha256"]
    ):
        raise ValueError("native compact packet changed after preparation")

    if args.rollout_path:
        rollout_path = Path(args.rollout_path).resolve()
    else:
        sessions_root = Path(args.sessions_root).resolve()
        rollout_path = discover_rollout(
            sessions_root,
            agent_path=str(native_assignment["agent_path"]),
            agent_role=str(native_assignment["agent_role"]),
            parent_thread_id=str(native_assignment["parent_thread_id"]),
            prepared_at_unix=float(native_assignment["prepared_at_unix"]),
        )
    if not rollout_path.is_file():
        raise ValueError("native subagent rollout does not exist")

    session_meta, turn_context, task_complete, assistant_message = load_rollout(
        rollout_path
    )
    actual = validate_runtime(
        session_meta=session_meta,
        turn_context=turn_context,
        native_assignment=native_assignment,
    )
    final = strict_final_message(
        assistant_message, int(native_assignment["unit_count"])
    )

    pending_rows = read_jsonl(pending_path)
    generated_transport, restore_maps = build_transport_rows(pending_rows)
    actual_transport = read_jsonl(transport_path, encoding="utf-8-sig")
    if generated_transport != actual_transport:
        raise ValueError("reconstructed compact packet differs from the frozen native input")
    expected_body_ids = [
        str(row["unit_id"])
        for row in pending_rows
        if row.get("kind") != "reference_title"
    ]
    expected_reference_ids = [
        str(row["unit_id"])
        for row in pending_rows
        if row.get("kind") == "reference_title"
    ]
    canonical_rows, unit_errors, missing_ids = canonicalize_worker_rows(
        pending_rows,
        final["translations"],
        final["reference_titles"],
        restore_maps,
    )
    returned_body_ids = [
        str(row.get("unit_id"))
        for row in final["translations"]
        if isinstance(row, dict)
    ]
    returned_reference_ids = [
        str(row.get("unit_id"))
        for row in final["reference_titles"]
        if isinstance(row, dict)
    ]
    unexpected_body_ids = sorted(set(returned_body_ids) - set(expected_body_ids))
    unexpected_reference_ids = sorted(
        set(returned_reference_ids) - set(expected_reference_ids)
    )
    contract_errors: list[str] = []
    if unexpected_body_ids:
        contract_errors.append(
            "unexpected body unit ids: " + ", ".join(unexpected_body_ids[:20])
        )
    if unexpected_reference_ids:
        contract_errors.append(
            "unexpected reference-title unit ids: "
            + ", ".join(unexpected_reference_ids[:20])
        )

    partial_content = "".join(
        json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
        for row in canonical_rows
    )
    candidate_path = workflow_dir / ".translation-native-candidate.jsonl"
    full_packet = read_packet(workflow_dir / "translation-packet.json")
    translations: dict[str, Any] = {}
    if canonical_rows:
        atomic_write_bytes(candidate_path, partial_content.encode("utf-8"), min_bytes=2)
        valid_ids = {str(row["unit_id"]) for row in canonical_rows}
        valid_packet = {
            **full_packet,
            "units": [
                unit
                for unit in full_packet["units"]
                if str(unit["unit_id"]) in valid_ids
            ],
        }
        try:
            translations, errors = validate_output(valid_packet, candidate_path)
            if errors or len(translations) != len(valid_ids):
                raise ValueError(
                    "native candidate failed per-unit packet validation: "
                    + "; ".join(errors)
                )
        finally:
            candidate_path.unlink(missing_ok=True)

    invalid_count = len(missing_ids)
    complete = not contract_errors and invalid_count == 0 and len(canonical_rows) == len(pending_rows)
    overall_failed = bool(contract_errors) or invalid_count >= UNIT_FAILURE_THRESHOLD
    recovery_required = not complete and not overall_failed
    status = "completed" if complete else ("failed" if overall_failed else "partial_success")
    output_path = Path(str(native_assignment["output_path"])).resolve()
    if not is_within(output_path, workflow_dir):
        raise ValueError("native output path escapes workflow-dir")
    partial_path = workflow_dir / (
        native_assignment_path.stem + "-partial.jsonl"
    )
    output_path.unlink(missing_ok=True)
    partial_path.unlink(missing_ok=True)
    if complete:
        atomic_write_bytes(output_path, partial_content.encode("utf-8"), min_bytes=2)
        if output_path.read_bytes().startswith(b"\xef\xbb\xbf"):
            raise ValueError("native translation output unexpectedly contains a UTF-8 BOM")
    elif canonical_rows:
        atomic_write_bytes(partial_path, partial_content.encode("utf-8"), min_bytes=2)

    if translations and not contract_errors:
        append_validated_cache(
            workflow_dir / "translation-cache.jsonl",
            full_packet,
            translations,
            str(native_assignment["translator_fingerprint"]),
            actual,
        )

    task_started = task_complete.get("started_at")
    task_completed = task_complete.get("completed_at")
    duration_ms = task_complete.get("duration_ms")
    if not isinstance(duration_ms, int) and isinstance(task_started, (int, float)) and isinstance(
        task_completed, (int, float)
    ):
        duration_ms = round((task_completed - task_started) * 1000)
    attestation_path = workflow_dir / (
        native_assignment_path.stem + "-attestation.json"
    )
    failure_reasons = list(contract_errors)
    failure_reasons.extend(
        f"{unit_id}: {reasons[0]}"
        for unit_id, reasons in unit_errors.items()
        if reasons
    )
    if missing_ids:
        failure_reasons.append("missing or invalid units: " + ", ".join(missing_ids[:20]))
    attestation = {
        "schema_version": 3,
        "success": complete,
        "status": status,
        "overall_failed": overall_failed,
        "recovery_required": recovery_required,
        "unit_failure_threshold": UNIT_FAILURE_THRESHOLD,
        "source": "native_collaboration_spawn_agent",
        "source_rollout": str(rollout_path),
        "source_rollout_sha256": sha256(rollout_path),
        "native_assignment_path": str(native_assignment_path),
        "native_assignment_sha256": sha256(native_assignment_path),
        "requested": {
            **native_assignment["requested_runtime"],
            "agent_role": native_assignment["agent_role"],
            "prompt_sha256": native_assignment["prompt_sha256"],
            "translator_fingerprint": native_assignment["translator_fingerprint"],
        },
        "actual": actual,
        "started_at": unix_to_iso(task_started, session_meta.get("timestamp")),
        "finished_at": unix_to_iso(task_completed),
        "duration_ms": duration_ms,
        "completion_waits": 1,
        "transport": {
            "schema_version": native_assignment["transport_schema_version"],
            "packet_bytes": transport_path.stat().st_size,
            "body_units": native_assignment["body_units"],
            "reference_title_units": native_assignment["reference_title_units"],
            "fallback_title_units": native_assignment["fallback_title_units"],
        },
        "translated_units": native_assignment["unit_count"],
        "validated_units_recovered": len(translations),
        "partial_validated_units": len(translations) if not complete else 0,
        "invalid_unit_ids": missing_ids,
        "partial_output_path": str(partial_path) if partial_path.is_file() else None,
        "partial_output_sha256": sha256(partial_path) if partial_path.is_file() else None,
        "failure_stage": "output_validation" if not complete else None,
        "failure_class": (
            "output_contract"
            if contract_errors
            else ("unit_failure_threshold" if overall_failed else ("unit_recovery_required" if recovery_required else None))
        ),
        "retryable": recovery_required,
        "failure_reasons": failure_reasons[:20],
        "contract_errors": contract_errors[:20],
        "unit_errors": {
            unit_id: reasons[:3]
            for unit_id, reasons in list(unit_errors.items())[:50]
        },
        "output_path": str(output_path),
        "output_sha256": sha256(output_path) if output_path.is_file() else None,
    }
    atomic_write_json(attestation_path, attestation)
    update_native_state(
        state_path=workflow_dir / "workflow-state.json",
        native_assignment=native_assignment,
        actual=actual,
        attestation_path=attestation_path,
        attestation=attestation,
    )
    return {
        "ok": not overall_failed,
        "status": status,
        "stage": "native_subagent_import",
        "worker_backend": "native-subagent",
        "translated_units": native_assignment["unit_count"],
        "validated_units_recovered": len(translations),
        "invalid_unit_ids": missing_ids,
        "invalid_unit_count": invalid_count,
        "unit_failure_threshold": UNIT_FAILURE_THRESHOLD,
        "recovery_required": recovery_required,
        "overall_failed": overall_failed,
        "output_path": str(output_path),
        "output_sha256": sha256(output_path) if output_path.is_file() else None,
        "partial_output_path": str(partial_path) if partial_path.is_file() else None,
        "partial_output_sha256": sha256(partial_path) if partial_path.is_file() else None,
        "attestation_path": str(attestation_path),
        "attestation_sha256": sha256(attestation_path),
        "agent_role": actual["agent_role"],
        "model": actual["model"],
        "reasoning_effort": actual["reasoning_effort"],
        "session_id": actual["session_id"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("--assignment", required=True)
    prepare_parser.add_argument("--workflow-dir", required=True)
    prepare_parser.add_argument("--agent-role-file", required=True)
    prepare_parser.add_argument("--agent-role", default=DEFAULT_AGENT_ROLE)
    prepare_parser.add_argument("--task-name")
    prepare_parser.add_argument(
        "--parent-thread-id",
        help="Parent Codex thread ID; defaults to the inherited CODEX_THREAD_ID.",
    )

    import_parser = subparsers.add_parser("import")
    import_parser.add_argument("--assignment", required=True)
    import_parser.add_argument("--workflow-dir", required=True)
    rollout = import_parser.add_mutually_exclusive_group(required=True)
    rollout.add_argument("--rollout-path")
    rollout.add_argument(
        "--discover-rollout",
        action="store_true",
        help="Discover the unique completed native subagent rollout from the frozen agent role and path.",
    )
    import_parser.add_argument(
        "--sessions-root",
        default=str(Path.home() / ".codex" / "sessions"),
    )

    args = parser.parse_args()
    try:
        result = prepare(args) if args.command == "prepare" else import_result(args)
    except Exception as exc:
        print(
            json.dumps(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
