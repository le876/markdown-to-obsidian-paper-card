#!/usr/bin/env python3
"""Run one isolated Codex paper-translation worker with runtime attestation."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from paper_translation_packet import is_identity_or_numeric_passthrough, protected_tokens
from safe_atomic_io import atomic_write_bytes, atomic_write_json, atomic_write_text
from sync_paper_translation_agent_prompt import (
    DEFAULT_MODEL,
    DEFAULT_REASONING_EFFORT,
    read_fragment,
    render_agent,
)


TRANSPORT_SCHEMA_VERSION = 3
UNIT_FAILURE_THRESHOLD = 5


FINAL_STATUS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["status", "translated_units", "translations", "reference_titles"],
    "properties": {
        "status": {"type": "string", "const": "completed"},
        "translated_units": {"type": "integer", "minimum": 0},
        "translations": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["unit_id", "zh"],
                "properties": {
                    "unit_id": {"type": "string", "pattern": "^u[0-9]{5}$"},
                    "zh": {"type": "string", "minLength": 1},
                },
            },
        },
        "reference_titles": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["unit_id", "source_title", "zh"],
                "properties": {
                    "unit_id": {"type": "string", "pattern": "^r[0-9]{5}$"},
                    "source_title": {"type": "string", "minLength": 1},
                    "zh": {"type": "string", "minLength": 1},
                },
            },
        },
    },
}


def build_final_status_schema(translated_units: int) -> dict[str, Any]:
    """Bind the worker's declared count to this exact pending assignment."""
    schema = json.loads(json.dumps(FINAL_STATUS_SCHEMA))
    schema["properties"]["translated_units"] = {"type": "integer", "const": translated_units}
    return schema


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def parse_runtime_header(log_text: str) -> dict[str, str | bool | None]:
    def match(pattern: str) -> str | None:
        found = re.search(pattern, log_text, flags=re.MULTILINE | re.IGNORECASE)
        return found.group(1).strip() if found else None

    return {
        "model": match(r"^model:\s*([^\r\n]+)$"),
        "sandbox": match(r"^sandbox:\s*([^\r\n]+)$"),
        "reasoning_effort": match(r"^reasoning effort:\s*([^\r\n]+)$"),
        "codex_version": match(r"^OpenAI Codex v([^\r\n]+)$"),
        "session_id": match(r"^session id:\s*([^\r\n]+)$"),
    }


def build_command(
    *,
    codex_executable: str,
    model: str,
    reasoning_effort: str,
    sterile_dir: Path,
    workflow_dir: Path,
    schema_path: Path,
    last_message_path: Path,
) -> list[str]:
    return [
        codex_executable,
        "exec",
        "--ephemeral",
        "--skip-git-repo-check",
        "--ignore-user-config",
        "--ignore-rules",
        "--model",
        model,
        "-c",
        f'model_reasoning_effort="{reasoning_effort}"',
        "-c",
        'sandbox_mode="workspace-write"',
        "--sandbox",
        "workspace-write",
        "-C",
        str(sterile_dir),
        "--add-dir",
        str(workflow_dir),
        "--color",
        "never",
        "--output-schema",
        str(schema_path),
        "--output-last-message",
        str(last_message_path),
        "-",
    ]


def resolve_codex_executable(value: str) -> str:
    candidate = Path(value)
    if candidate.is_absolute():
        if not candidate.is_file():
            raise ValueError(f"Codex executable does not exist: {candidate}")
        return str(candidate)
    if os.name == "nt" and candidate.suffix == "":
        resolved = shutil.which(value + ".cmd") or shutil.which(value + ".exe")
    else:
        resolved = shutil.which(value)
    if not resolved:
        raise ValueError(f"Codex executable was not found on PATH: {value}")
    return resolved


def build_worker_environment(source: dict[str, str] | None = None) -> dict[str, str]:
    environment = dict(source if source is not None else os.environ)
    for key in (
        "CODEX_CI",
        "CODEX_INTERNAL_ORIGINATOR_OVERRIDE",
        "CODEX_PERMISSION_PROFILE",
        "CODEX_SHELL",
        "CODEX_THREAD_ID",
    ):
        environment.pop(key, None)
    return environment


def build_transport_rows(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, list[tuple[str, str]]]]:
    transported: list[dict[str, Any]] = []
    restore_maps: dict[str, list[tuple[str, str]]] = {}
    for row in rows:
        unit_id = str(row["unit_id"])
        english = str(row.get("english", ""))
        replacements: list[tuple[str, str]] = []
        protected = row.get("protected") or {}
        sequence = 0
        for category in ("inline_math", "citation_tokens", "markdown_footnotes", "emphasis_tokens"):
            for token in protected.get(category) or []:
                token = str(token)
                sequence += 1
                placeholder = f"{{{{PT:{unit_id}:{sequence:03d}}}}}"
                if token not in english:
                    raise ValueError(f"protected token is missing from pending unit {unit_id}: {token}")
                english = english.replace(token, placeholder, 1)
                replacements.append((placeholder, token))
        def protected_value(value: Any) -> Any:
            if not isinstance(value, str):
                return value
            result = value
            for placeholder, token in replacements:
                result = result.replace(token, placeholder, 1)
            return result

        if row.get("kind") == "reference_title":
            transported_row = {
                "id": unit_id,
                "type": "reference_title",
                **(
                    {"entry": protected_value(row.get("reference_entry"))}
                    if row.get("extraction_mode") == "worker"
                    else {"title": english}
                ),
            }
        else:
            transported_row = {"id": unit_id, "type": "body", "text": english}
        transported.append(transported_row)
        restore_maps[unit_id] = replacements
    return transported, restore_maps


def restore_transport_translation(unit_id: str, translated: str, restore_map: list[tuple[str, str]]) -> str:
    restored = translated
    for placeholder, token in restore_map:
        if restored.count(placeholder) != 1:
            raise ValueError(f"worker changed protected placeholder count for {unit_id}: {placeholder}")
        restored = restored.replace(placeholder, token)
    return restored


def restore_fallback_leading_math_token(source_title: str, translated: str) -> str:
    """Restore one unambiguously rendered leading math token in a fallback title.

    Fallback reference rows expose a complete bibliography entry because the title
    boundary is not known before translation.  Their protected-token set is
    therefore derived only after the worker returns an exact ``source_title``.
    Keep this compatibility repair deliberately narrow: replace only the text
    before the first Chinese character, only when the source title starts with one
    inline-math token, the exact token is absent, and that prefix contains a
    recognizable rendering of the same math expression.  All other drift remains
    a hard validation error.
    """
    inline_math = protected_tokens(source_title).get("inline_math") or []
    if not inline_math:
        return translated
    token = str(inline_math[0])
    if not source_title.lstrip().startswith(token):
        return translated
    if source_title.count(token) != 1 or translated.count(token) != 0:
        return translated
    first_chinese = re.search(r"[\u4e00-\u9fff]", translated)
    if first_chinese is None or first_chinese.start() == 0:
        return translated
    rendered_prefix = translated[: first_chinese.start()].strip()
    if not rendered_prefix:
        return translated
    greek_renderings = {
        r"\alpha": "α",
        r"\beta": "β",
        r"\gamma": "γ",
        r"\delta": "δ",
        r"\eta": "η",
        r"\theta": "θ",
        r"\kappa": "κ",
        r"\lambda": "λ",
        r"\mu": "μ",
        r"\pi": "π",
        r"\rho": "ρ",
        r"\sigma": "σ",
        r"\tau": "τ",
        r"\phi": "φ",
        r"\psi": "ψ",
        r"\omega": "ω",
    }
    same_greek = any(
        command in token and symbol in rendered_prefix
        for command, symbol in greek_renderings.items()
    )
    same_scripted_number = bool(
        re.search(r"</?(?:sub|sup)>", rendered_prefix, flags=re.IGNORECASE)
        and re.search(r"\d", token)
        and re.search(r"\d", rendered_prefix)
    )
    if not (same_greek or same_scripted_number):
        return translated
    return token + translated[first_chinese.start() :]


def classify_failure(
    *,
    timed_out: bool,
    returncode: int,
    runtime_verified: bool,
    log_text: str,
    contract_errors: list[str],
    unit_errors: dict[str, list[str]],
) -> tuple[str, str, bool]:
    """Return failure_stage, failure_class, and whether an explicit rerun is useful."""
    lowered = log_text.lower()
    if timed_out:
        return "worker_process", "timeout", True
    network_markers = (
        "ws401",
        "websocket",
        "connection reset",
        "connection closed",
        "connection aborted",
        "https connection",
        "error sending request",
        "failed to send request",
        "unexpected eof",
        "transport error",
    )
    if returncode != 0 and any(marker in lowered for marker in network_markers):
        return "worker_process", "network_transport", True
    if returncode != 0:
        return "worker_process", "worker_process", True
    if not runtime_verified:
        return "runtime_attestation", "runtime_mismatch", False
    if contract_errors:
        return "output_validation", "output_contract", True
    if unit_errors:
        return "output_validation", "unit_validation", True
    return "output_validation", "unknown", True


def canonicalize_worker_rows(
    pending_rows: list[dict[str, Any]],
    returned_rows: list[Any],
    returned_reference_rows: list[Any],
    restore_maps: dict[str, list[tuple[str, str]]],
) -> tuple[list[dict[str, str]], dict[str, list[str]], list[str]]:
    """Validate independent rows so trusted successes can survive a partial response."""
    expected = {str(row["unit_id"]): row for row in pending_rows}
    expected_order = [str(row["unit_id"]) for row in pending_rows]
    expected_body = {unit_id for unit_id, row in expected.items() if row.get("kind") != "reference_title"}
    expected_references = set(expected) - expected_body
    valid: dict[str, dict[str, str]] = {}
    errors: dict[str, list[str]] = {}
    seen: set[str] = set()

    def fail(unit_id: str, reason: str) -> None:
        errors.setdefault(unit_id, []).append(reason)

    for raw in returned_rows:
        if not isinstance(raw, dict):
            fail("<body-row>", "row is not an object")
            continue
        unit_id = str(raw.get("unit_id", ""))
        if unit_id not in expected_body:
            fail(unit_id or "<missing-id>", "unknown or wrong-kind body unit_id")
            continue
        if unit_id in seen:
            fail(unit_id, "duplicate unit_id")
            valid.pop(unit_id, None)
            continue
        seen.add(unit_id)
        chinese = raw.get("zh")
        if not isinstance(chinese, str) or not chinese.strip():
            fail(unit_id, "empty translation")
            continue
        try:
            restored = restore_transport_translation(unit_id, chinese, restore_maps[unit_id]).strip()
        except (KeyError, ValueError) as exc:
            fail(unit_id, str(exc))
            continue
        source = str(expected[unit_id].get("english", ""))
        requires_chinese = expected[unit_id].get("requires_chinese", True)
        if (
            requires_chinese
            and re.fullmatch(r"\([A-Za-z0-9]+\)", source.strip()) is None
            and not re.search(r"[\u4e00-\u9fff]", restored)
            and not (
                is_identity_or_numeric_passthrough(source)
                and restored == source.strip()
            )
        ):
            fail(unit_id, "translation contains no Chinese text")
            continue
        valid[unit_id] = {"unit_id": unit_id, "zh": restored}

    for raw in returned_reference_rows:
        if not isinstance(raw, dict):
            fail("<reference-row>", "row is not an object")
            continue
        unit_id = str(raw.get("unit_id", ""))
        if unit_id not in expected_references:
            fail(unit_id or "<missing-id>", "unknown or wrong-kind reference-title unit_id")
            continue
        if unit_id in seen:
            fail(unit_id, "duplicate unit_id")
            valid.pop(unit_id, None)
            continue
        seen.add(unit_id)
        try:
            source_title = restore_transport_translation(unit_id, str(raw.get("source_title", "")), restore_maps[unit_id]).strip()
            chinese = restore_transport_translation(unit_id, str(raw.get("zh", "")), restore_maps[unit_id]).strip()
        except (KeyError, ValueError) as exc:
            fail(unit_id, str(exc))
            continue
        pending = expected[unit_id]
        entry = str(pending.get("reference_entry", ""))
        hint = pending.get("title_hint")
        if not source_title or source_title not in entry:
            fail(unit_id, "source_title is not an exact substring of the reference entry")
        if hint is not None and source_title != hint:
            fail(unit_id, "source_title does not match deterministic title_hint")
        if pending.get("extraction_mode") == "worker":
            chinese = restore_fallback_leading_math_token(source_title, chinese)
        if not re.search(r"[\u4e00-\u9fff]", chinese):
            fail(unit_id, "Chinese title contains no Chinese text")
        if any(token in chinese for token in ("《", "》", "^ref-", "{{", "}}")) or "\n" in chinese or "\r" in chinese:
            fail(unit_id, "Chinese title contains forbidden markup")
        if re.match(r"^\s*\[\d+\]", chinese):
            fail(unit_id, "Chinese title contains a reference number")
        if unit_id not in errors:
            valid[unit_id] = {"unit_id": unit_id, "source_title": source_title, "zh": chinese}

    missing = [unit_id for unit_id in expected_order if unit_id not in valid]
    canonical = [valid[unit_id] for unit_id in expected_order if unit_id in valid]
    return canonical, errors, missing


def terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            text=True,
            check=False,
        )
    else:
        process.kill()


def run(args: argparse.Namespace) -> dict[str, Any]:
    assignment_path = Path(args.assignment).resolve()
    workflow_dir = Path(args.workflow_dir).resolve()
    if not assignment_path.is_file() or not is_within(assignment_path, workflow_dir):
        raise ValueError("assignment must be an existing file inside workflow-dir")
    assignment = json.loads(assignment_path.read_text(encoding="utf-8"))
    if assignment.get("schema_version") not in {1, 2, 3}:
        raise ValueError("unsupported translation assignment schema")

    packet_path = Path(str(assignment["packet_path"])).resolve()
    output_path = Path(str(assignment["output_path"])).resolve()
    if not is_within(packet_path, workflow_dir) or not is_within(output_path, workflow_dir):
        raise ValueError("assignment packet and output must stay inside workflow-dir")
    if not packet_path.is_file() or sha256(packet_path) != assignment.get("packet_sha256"):
        raise ValueError("pending packet is missing or its SHA-256 does not match assignment")
    pending_rows: list[dict[str, Any]] = []
    for raw in packet_path.read_text(encoding="utf-8").splitlines():
        if raw.strip():
            value = json.loads(raw)
            if not isinstance(value, dict) or not isinstance(value.get("unit_id"), str):
                raise ValueError("pending packet contains an invalid unit row")
            pending_rows.append(value)
    transport_rows, restore_maps = build_transport_rows(pending_rows)

    fragment_path = Path(args.fragment_path).resolve()
    fragment = read_fragment(fragment_path)
    rendered, prompt_sha256, translator_fingerprint = render_agent(
        fragment,
        model=args.model,
        reasoning_effort=args.reasoning_effort,
    )
    generated_agent = tomllib.loads(rendered)
    instructions = str(generated_agent["developer_instructions"])
    if assignment.get("prompt_sha256") != prompt_sha256:
        raise ValueError("assignment prompt SHA-256 is stale")
    if assignment.get("translator_fingerprint") != translator_fingerprint:
        raise ValueError("assignment translator fingerprint is stale")

    requested = assignment.get("requested_runtime") or {}
    if requested and (
        requested.get("model") != args.model
        or requested.get("reasoning_effort") != args.reasoning_effort
    ):
        raise ValueError("assignment requested runtime does not match runner runtime")

    state_path = workflow_dir / "workflow-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
    prior_attempts = (((state.get("stages") or {}).get("translation_worker") or {}).get("attempts") or [])
    attempt_number = len(prior_attempts) + 1
    stem = f"translation-worker-attempt-{attempt_number}"
    worker_log_path = workflow_dir / f"{stem}.log"
    attestation_path = workflow_dir / f"{stem}-attestation.json"
    last_message_path = workflow_dir / f"{stem}-final.json"
    schema_path = workflow_dir / "translation-worker-final.schema.json"
    transport_packet_path = workflow_dir / f"{stem}-input.jsonl"
    transport_assignment_path = workflow_dir / f"{stem}-assignment.json"
    atomic_write_json(schema_path, build_final_status_schema(len(transport_rows)))
    transport_content = "".join(
        json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
        for row in transport_rows
    )
    atomic_write_bytes(transport_packet_path, b"\xef\xbb\xbf" + transport_content.encode("utf-8"), min_bytes=5)
    transport_metrics = {
        "schema_version": TRANSPORT_SCHEMA_VERSION,
        "packet_bytes": transport_packet_path.stat().st_size,
        "source_chars": sum(
            len(str(row.get(field, "")))
            for row in transport_rows
            for field in ("text", "title", "entry")
        ),
        "body_units": sum(1 for row in transport_rows if row.get("type") == "body"),
        "reference_title_units": sum(1 for row in transport_rows if row.get("type") == "reference_title"),
        "fallback_title_units": sum(1 for row in transport_rows if "entry" in row),
    }
    transport_assignment = {
        "schema_version": TRANSPORT_SCHEMA_VERSION,
        "packet_path": str(transport_packet_path),
        "unit_count": len(transport_rows),
        "packet_sha256": sha256(transport_packet_path),
        "body_units": transport_metrics["body_units"],
        "reference_title_units": transport_metrics["reference_title_units"],
        "fallback_title_units": transport_metrics["fallback_title_units"],
    }
    atomic_write_json(transport_assignment_path, transport_assignment)
    output_path.unlink(missing_ok=True)
    last_message_path.unlink(missing_ok=True)
    attestation_path.unlink(missing_ok=True)

    prompt = (
        instructions.rstrip()
        + "\n\nAssignment manifest: "
        + str(transport_assignment_path)
        + "\nOn Windows PowerShell, read the assignment and packet with Get-Content -LiteralPath <path> -Encoding UTF8."
        + "\nDo not read any AGENTS.md, SKILL.md, plugin, connector, or unrelated file."
        + "\nDo not attempt any file write. Return every translation only through the required final JSON schema; the runner will write the JSONL."
    )
    started_at = time.time()
    started_at_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    started_perf = time.perf_counter()
    sterile_dir = Path(tempfile.mkdtemp(prefix="codex-paper-translation-"))
    command = build_command(
        codex_executable=resolve_codex_executable(args.codex_executable),
        model=args.model,
        reasoning_effort=args.reasoning_effort,
        sterile_dir=sterile_dir,
        workflow_dir=workflow_dir,
        schema_path=schema_path,
        last_message_path=last_message_path,
    )
    if args.dry_run:
        shutil.rmtree(sterile_dir, ignore_errors=True)
        return {
            "ok": True,
            "dry_run": True,
            "command": command,
            "prompt_sha256": prompt_sha256,
            "translator_fingerprint": translator_fingerprint,
            "transport": transport_metrics,
        }

    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=sterile_dir,
        creationflags=creationflags,
        env=build_worker_environment(),
    )
    timed_out = False
    try:
        log_text, _ = process.communicate(input=prompt, timeout=args.timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        terminate_process_tree(process)
        log_text, _ = process.communicate(timeout=30)
    finally:
        shutil.rmtree(sterile_dir, ignore_errors=True)
    duration_ms = round((time.perf_counter() - started_perf) * 1000)
    finished_at_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    atomic_write_text(worker_log_path, log_text or "worker emitted no console output\n", min_bytes=1)

    actual = parse_runtime_header(log_text)
    runtime_verified = (
        actual.get("model") == args.model
        and str(actual.get("reasoning_effort", "")).lower() == args.reasoning_effort.lower()
    )
    if timed_out or process.returncode != 0 or not runtime_verified:
        output_path.unlink(missing_ok=True)

    final_status: dict[str, Any] = {}
    if last_message_path.is_file():
        try:
            value = json.loads(last_message_path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                final_status = value
        except json.JSONDecodeError:
            final_status = {}

    expected_ids = [str(row["unit_id"]) for row in pending_rows]
    expected_body_ids = [str(row["unit_id"]) for row in pending_rows if row.get("kind") != "reference_title"]
    expected_reference_ids = [str(row["unit_id"]) for row in pending_rows if row.get("kind") == "reference_title"]
    returned = final_status.get("translations")
    returned_reference = final_status.get("reference_titles")
    returned_rows = returned if isinstance(returned, list) else []
    returned_reference_rows = returned_reference if isinstance(returned_reference, list) else []
    global_trusted = (
        not timed_out
        and process.returncode == 0
        and runtime_verified
        and final_status.get("status") == "completed"
        and isinstance(returned, list)
        and isinstance(returned_reference, list)
    )
    canonical_rows: list[dict[str, str]] = []
    unit_errors: dict[str, list[str]] = {}
    missing_ids = list(expected_ids)
    if global_trusted:
        canonical_rows, unit_errors, missing_ids = canonicalize_worker_rows(
            pending_rows,
            returned_rows,
            returned_reference_rows,
            restore_maps,
        )
    returned_body_ids = [str(row.get("unit_id")) for row in returned_rows if isinstance(row, dict)]
    returned_reference_ids = [str(row.get("unit_id")) for row in returned_reference_rows if isinstance(row, dict)]
    contract_errors: list[str] = []
    if final_status.get("status") != "completed":
        contract_errors.append("final status is not completed")
    if not isinstance(returned, list) or not isinstance(returned_reference, list):
        contract_errors.append("translation arrays are missing or invalid")
    if final_status.get("translated_units") != assignment.get("unit_count"):
        contract_errors.append("translated_units does not equal assignment unit_count")
    unexpected_body_ids = sorted(set(returned_body_ids) - set(expected_body_ids))
    unexpected_reference_ids = sorted(
        set(returned_reference_ids) - set(expected_reference_ids)
    )
    if unexpected_body_ids:
        contract_errors.append(
            "unexpected body unit ids: " + ", ".join(unexpected_body_ids[:20])
        )
    if unexpected_reference_ids:
        contract_errors.append(
            "unexpected reference-title unit ids: "
            + ", ".join(unexpected_reference_ids[:20])
        )
    payload_valid = global_trusted and not contract_errors and not unit_errors and len(canonical_rows) == len(expected_ids)
    partial_output_path = workflow_dir / f"{stem}-partial.jsonl"
    partial_output_path.unlink(missing_ok=True)
    if global_trusted and canonical_rows and not payload_valid:
        partial_content = "".join(
            json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
            for row in canonical_rows
        )
        atomic_write_text(partial_output_path, partial_content, min_bytes=2)
    if payload_valid:
        output_content = "".join(
            json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
            for row in canonical_rows
        )
        atomic_write_text(output_path, output_content, min_bytes=2)
    output_sha256 = sha256(output_path) if output_path.is_file() else None
    success = payload_valid and output_sha256 is not None
    failure_stage = None
    failure_class = None
    retryable = False
    if not success:
        failure_stage, failure_class, retryable = classify_failure(
            timed_out=timed_out,
            returncode=process.returncode,
            runtime_verified=runtime_verified,
            log_text=log_text,
            contract_errors=contract_errors,
            unit_errors=unit_errors,
        )
        if missing_ids and not contract_errors and len(missing_ids) < UNIT_FAILURE_THRESHOLD:
            failure_stage = "output_validation"
            failure_class = "unit_validation"
            retryable = True
        elif len(missing_ids) >= UNIT_FAILURE_THRESHOLD:
            failure_stage = "output_validation"
            failure_class = "unit_failure_threshold"
            retryable = False
    compact_reasons = (
        contract_errors
        + [f"{unit_id}: {reasons[0]}" for unit_id, reasons in unit_errors.items()]
        + (["missing or invalid units: " + ", ".join(missing_ids)] if missing_ids else [])
    )[:8]
    attestation = {
        "schema_version": 2,
        "requested": {
            "model": args.model,
            "reasoning_effort": args.reasoning_effort,
            "prompt_sha256": prompt_sha256,
            "translator_fingerprint": translator_fingerprint,
        },
        "actual": {**actual, "runtime_verified": runtime_verified},
        "started_at_unix": started_at,
        "started_at": started_at_iso,
        "finished_at": finished_at_iso,
        "duration_ms": duration_ms,
        "completion_waits": 1,
        "transport": transport_metrics,
        "exit_code": process.returncode,
        "timed_out": timed_out,
        "translated_units": final_status.get("translated_units"),
        "validated_units_recovered": len(canonical_rows),
        "partial_validated_units": len(canonical_rows) if not success else 0,
        "invalid_unit_ids": missing_ids if not success else [],
        "invalid_unit_count": len(missing_ids) if not success else 0,
        "unit_failure_threshold": UNIT_FAILURE_THRESHOLD,
        "overall_failed": (
            not global_trusted
            or bool(contract_errors)
            or len(missing_ids) >= UNIT_FAILURE_THRESHOLD
        ),
        "partial_output_path": str(partial_output_path) if partial_output_path.is_file() else None,
        "partial_output_sha256": sha256(partial_output_path) if partial_output_path.is_file() else None,
        "failure_stage": failure_stage,
        "failure_class": failure_class,
        "retryable": retryable,
        "failure_reasons": compact_reasons,
        "contract_errors": contract_errors[:8],
        "unit_errors": {unit_id: reasons[:3] for unit_id, reasons in list(unit_errors.items())[:20]},
        "output_path": str(output_path),
        "output_sha256": output_sha256,
        "worker_log_path": str(worker_log_path),
        "worker_log_sha256": sha256(worker_log_path),
        "success": success,
    }
    atomic_write_json(attestation_path, attestation)
    if not success:
        output_path.unlink(missing_ok=True)
        return {
            "ok": False,
            "status": "failed",
            "error": compact_reasons[0] if compact_reasons else str(failure_class),
            "failure_stage": failure_stage,
            "failure_class": failure_class,
            "retryable": retryable,
            "partial_validated_units": attestation["partial_validated_units"],
            "invalid_unit_ids": attestation["invalid_unit_ids"],
            "contract_errors": attestation["contract_errors"],
            "unit_errors": attestation["unit_errors"],
            "attestation_path": str(attestation_path),
            "attestation_sha256": sha256(attestation_path),
        }
    return {
        "ok": True,
        "status": "completed",
        "translated_units": assignment["unit_count"],
        "output_path": str(output_path),
        "output_sha256": output_sha256,
        "runtime_verified": True,
        "actual_model": actual["model"],
        "actual_reasoning_effort": actual["reasoning_effort"],
        "duration_ms": duration_ms,
        "completion_waits": 1,
        "transport": transport_metrics,
        "attestation_path": str(attestation_path),
        "attestation_sha256": sha256(attestation_path),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assignment", required=True)
    parser.add_argument("--workflow-dir", required=True)
    parser.add_argument(
        "--fragment-path",
        default=str(Path(__file__).resolve().parents[1] / "references" / "paper-translation-prompt-fragment.md"),
    )
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--reasoning-effort", default=DEFAULT_REASONING_EFFORT)
    parser.add_argument("--codex-executable", default="codex")
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        result = run(args)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False, separators=(",", ":")), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if result.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
