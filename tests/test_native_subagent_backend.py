from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from build_obsidian_paper_card import NATIVE_SUBAGENT_BACKEND, build
from paper_translation_packet import (
    CONSTRAINTS_VERSION,
    build_packet,
    build_pending_packet,
    extract_translation_units,
    read_packet,
    validate_output,
)
from run_native_paper_translation_worker import (
    import_result,
    prepare,
    strict_final_message,
    task_name_for,
)
from run_paper_translation_worker import restore_fallback_leading_math_token
from run_paper_translation_worker import build_transport_rows, canonicalize_worker_rows
from sync_paper_translation_agent_prompt import read_fragment, render_agent


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_args(**overrides: object) -> argparse.Namespace:
    defaults: dict[str, object] = {
        "input_markdown": "",
        "vault_root": "",
        "output_note": "",
        "translation_mode": "bilingual",
        "concept_links": "off",
        "source_package": None,
        "translation_stage": "prepare",
        "workflow_dir": None,
        "translation_output": None,
        "translator_fingerprint": None,
        "worker_backend": NATIVE_SUBAGENT_BACKEND,
        "translation_agent_role_file": None,
        "translation_agent_task_name": None,
        "worker_timeout_seconds": 1800,
        "worker_max_attempts": 2,
        "image_converter_layout": "off",
        "overwrite_image_converter_alignments": False,
        "in_place": False,
        "write": True,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class NativeSubagentBackendTests(unittest.TestCase):
    def test_old_packet_identity_copy_is_accepted_without_nonbody_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            markdown = root / "prepared.md"
            markdown.write_text(
                "---\ntitle: T\naliases:\n  - T\n---\n# T\n\nDyna Robotics\n",
                encoding="utf-8",
            )
            packet = build_packet(markdown, root / "packet.json")
            old_unit = {**packet["units"][0], "kind": "paragraph", "requires_chinese": True}
            old_packet = {**packet, "units": [old_unit]}
            _, restore_maps = build_transport_rows([old_unit])
            canonical, unit_errors, missing = canonicalize_worker_rows(
                [old_unit],
                [{"unit_id": old_unit["unit_id"], "zh": "Dyna Robotics"}],
                [],
                restore_maps,
            )
            self.assertEqual(unit_errors, {})
            self.assertEqual(missing, [])
            self.assertEqual(canonical, [{"unit_id": old_unit["unit_id"], "zh": "Dyna Robotics"}])
            output = root / "translation.jsonl"
            output.write_text(
                json.dumps(canonical[0], ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            validated, errors = validate_output(old_packet, output)
            self.assertEqual(errors, [])
            self.assertEqual(validated[old_unit["unit_id"]]["zh"], "Dyna Robotics")

    def test_restores_rendered_leading_math_in_fallback_reference_title(self) -> None:
        source_title = (
            "$\\pi _ { 0 . 5 } \\colon \\mathrm { A }$ "
            "vision-language-action model with open-world generalization."
        )
        translated = "π<sub>0.5</sub>：具有开放世界泛化能力的视觉—语言—动作模型"
        restored = restore_fallback_leading_math_token(source_title, translated)
        self.assertEqual(
            restored,
            "$\\pi _ { 0 . 5 } \\colon \\mathrm { A }$具有开放世界泛化能力的视觉—语言—动作模型",
        )
        self.assertEqual(restored.count("$\\pi _ { 0 . 5 } \\colon \\mathrm { A }$"), 1)

    def test_does_not_repair_unrecognized_fallback_title_prefix(self) -> None:
        source_title = "$x_t$ contact-aware policy."
        translated = "未知前缀：接触感知策略"
        self.assertEqual(
            restore_fallback_leading_math_token(source_title, translated),
            translated,
        )

    def make_native_fixture(
        self,
        root: Path,
        *,
        body_paragraphs: int = 1,
    ) -> dict[str, Path | dict[str, object]]:
        workflow = root / "workflow"
        workflow.mkdir()
        prepared = root / "prepared.md"
        body = ["A protected claim with $x_t$ and citation [[#^ref-1|¹]]."]
        body.extend(
            f"Independent paper claim number {index}."
            for index in range(2, body_paragraphs + 1)
        )
        prepared.write_text(
            "---\ntitle: T\naliases:\n  - T\n---\n# T\n\n## Abstract\n\n"
            + "\n\n".join(body)
            + "\n\n## References\n\n[1] A. Author. Paper. 2024. ^ref-1\n",
            encoding="utf-8",
            newline="\n",
        )
        packet_path = workflow / "translation-packet.json"
        packet = build_packet(prepared, packet_path)
        fragment = read_fragment(
            SKILL_ROOT / "references" / "paper-translation-prompt-fragment.md"
        )
        role_text, prompt_sha256, fingerprint = render_agent(
            fragment, model="gpt-5.6-terra", reasoning_effort="high"
        )
        role_path = root / "paper-translation-worker.toml"
        role_path.write_text(role_text, encoding="utf-8", newline="\n")
        pending_path = workflow / "translation-pending-packet.jsonl"
        cached_path = workflow / "translation-cached-output.jsonl"
        cache_path = workflow / "translation-cache.jsonl"
        cache_report = build_pending_packet(
            packet,
            cache_path,
            fingerprint,
            pending_path,
            cached_path,
            expected_model="gpt-5.6-terra",
            expected_reasoning_effort="high",
        )
        output_path = workflow / "translation-output.jsonl"
        assignment_path = workflow / "translation-assignment.json"
        assignment = {
            "schema_version": 3,
            "packet_path": str(pending_path.resolve()),
            "output_path": str(output_path.resolve()),
            "unit_count": cache_report["pending_count"],
            "packet_sha256": sha256(pending_path),
            "prompt_sha256": prompt_sha256,
            "translator_fingerprint": fingerprint,
            "constraints_version": CONSTRAINTS_VERSION,
            "requested_runtime": {
                "model": "gpt-5.6-terra",
                "reasoning_effort": "high",
            },
        }
        assignment_path.write_text(
            json.dumps(assignment, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        state_path = workflow / "workflow-state.json"
        state_path.write_text(
            json.dumps(
                {
                    "translator": {"requested": assignment["requested_runtime"], "actual": None},
                    "stages": {"translation_worker": {"status": "pending", "attempts": []}},
                    "artifacts": {},
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        return {
            "workflow": workflow,
            "packet_path": packet_path,
            "assignment_path": assignment_path,
            "output_path": output_path,
            "role_path": role_path,
            "packet": packet,
        }

    def write_rollout(
        self,
        path: Path,
        native_assignment: dict[str, object],
        final_message: str,
        *,
        model: str = "gpt-5.6-terra",
        effort: str = "high",
        role: str = "paper-translation-worker",
        parent_id: str = "parent-thread-id",
    ) -> None:
        agent_path = str(native_assignment["agent_path"])
        records = [
            {
                "timestamp": "2026-07-29T10:24:07.503Z",
                "type": "session_meta",
                "payload": {
                    "session_id": parent_id,
                    "id": "child-thread-id",
                    "parent_thread_id": parent_id,
                    "timestamp": "2026-07-29T10:24:07.226Z",
                    "cli_version": "0.146.0",
                    "source": {
                        "subagent": {
                            "thread_spawn": {
                                "parent_thread_id": parent_id,
                                "agent_path": agent_path,
                                "agent_role": role,
                            }
                        }
                    },
                    "thread_source": "subagent",
                    "agent_role": role,
                    "agent_path": agent_path,
                },
            },
            {
                "timestamp": "2026-07-29T10:24:07.600Z",
                "type": "turn_context",
                "payload": {
                    "model": model,
                    "effort": effort,
                    "sandbox_policy": {"type": "workspace-write"},
                },
            },
            {
                "timestamp": "2026-07-29T10:25:00.000Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": final_message}],
                },
            },
            {
                "timestamp": "2026-07-29T10:25:00.100Z",
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "started_at": 1785320647,
                    "completed_at": 1785320700,
                    "duration_ms": 53000,
                    "last_agent_message": final_message,
                },
            },
        ]
        path.write_text(
            "".join(
                json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
                for record in records
            ),
            encoding="utf-8",
            newline="\n",
        )

    def test_standalone_code_and_pseudocode_are_deterministic_passthrough(self) -> None:
        source = (
            "---\ntitle: T\naliases:\n  - T\n---\n# T\n\n## Method\n\n"
            "⬇\n\nclass Worker:\n\ndef send(self, obj, dst, async\\_op)\n\n"
            "while batch\\_size < self.total\\_batch\\_size:\n\n"
            "self.rollout\\_group.generate(\n\nin\\_channel=self.data\\_ch,\n\n)\n\n"
            "if *$G$ is a node* then\n\nreturn $E_{node}$, $S_{node}$;\n\nend if\n\n"
            "$T_{best}\\leftarrow$ PipeliningTime ($T_s$, $T_t$);\n\n"
            "1ex $D_{table}\\leftarrow$ $\\{\\}$; // graph map to (time, schedule)\n\n"
            "(a) A typical RLinf worker.\n\n"
            "If the model is stable, then training converges.\n\n"
            "For the 7B model, spatial execution outperforms temporal execution.\n"
        )
        units = extract_translation_units(source)
        by_text = {str(unit["english"]): unit for unit in units}
        passthrough = (
            "⬇",
            "class Worker:",
            "def send(self, obj, dst, async\\_op)",
            "while batch\\_size < self.total\\_batch\\_size:",
            "self.rollout\\_group.generate(",
            "in\\_channel=self.data\\_ch,",
            ")",
            "if *$G$ is a node* then",
            "return $E_{node}$, $S_{node}$;",
            "end if",
            "$T_{best}\\leftarrow$ PipeliningTime ($T_s$, $T_t$);",
        )
        for value in passthrough:
            self.assertEqual(by_text[value]["kind"], "passthrough", value)
            self.assertFalse(by_text[value]["requires_chinese"], value)
        for value in (
            "1ex $D_{table}\\leftarrow$ $\\{\\}$; // graph map to (time, schedule)",
            "(a) A typical RLinf worker.",
            "If the model is stable, then training converges.",
            "For the 7B model, spatial execution outperforms temporal execution.",
        ):
            self.assertNotEqual(by_text[value]["kind"], "passthrough", value)
            self.assertTrue(by_text[value]["requires_chinese"], value)

    def test_builder_default_run_emits_native_handoff_without_local_codex(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "vault"
            vault.mkdir()
            source = root / "source.md"
            source.write_text(
                "# Fixture\n\n## Abstract\n\nA paper claim.\n",
                encoding="utf-8",
                newline="\n",
            )
            output = vault / "Papers" / "Fixture.md"
            workflow = vault / ".workflow"
            fake_handoff = {
                "ok": True,
                "stage": "native_subagent_prepare",
                "worker_backend": "native-subagent",
                "assignment_path": str(workflow / "translation-native-attempt-1-assignment.json"),
                "spawn_agent": {
                    "agent_type": "paper-translation-worker",
                    "task_name": "translate_fixture_a1",
                    "fork_turns": "none",
                    "message": "read assignment",
                },
            }
            with mock.patch(
                "build_obsidian_paper_card.prepare_native_subagent",
                return_value=fake_handoff,
            ) as native_prepare, mock.patch(
                "build_obsidian_paper_card.subprocess.Popen"
            ) as popen:
                result = build(
                    build_args(
                        input_markdown=str(source),
                        vault_root=str(vault),
                        output_note=str(output),
                        workflow_dir=str(workflow),
                        translation_stage="run",
                    )
                )
            self.assertEqual(result["status"], "awaiting_native_subagent")
            self.assertEqual(result["worker_backend"], "native-subagent")
            self.assertEqual(
                result["spawn_agent"]["agent_type"], "paper-translation-worker"
            )
            native_prepare.assert_called_once()
            popen.assert_not_called()

    def test_reprepare_uses_monotonic_attempt_and_unique_task_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.make_native_fixture(Path(temporary))
            args = argparse.Namespace(
                assignment=str(fixture["assignment_path"]),
                workflow_dir=str(fixture["workflow"]),
                agent_role_file=str(fixture["role_path"]),
                agent_role="paper-translation-worker",
                task_name=None,
                parent_thread_id="parent-thread-id",
            )
            first = prepare(args)
            second = prepare(args)
            self.assertIn("translation-native-attempt-1-assignment.json", first["assignment_path"])
            self.assertIn("translation-native-attempt-2-assignment.json", second["assignment_path"])
            self.assertNotEqual(first["task_name"], second["task_name"])
            self.assertTrue(first["task_name"].endswith("_a1"))
            self.assertTrue(second["task_name"].endswith("_a2"))

    def test_default_task_name_is_stable_and_unique_per_paper_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            common_prefix = "paper-card-" + "same-prefix-" * 4
            first_workflow = root / f"{common_prefix}alpha" / "workflow"
            second_workflow = root / f"{common_prefix}beta" / "workflow"
            first_workflow.mkdir(parents=True)
            second_workflow.mkdir(parents=True)

            first = task_name_for(first_workflow, 1)
            first_again = task_name_for(first_workflow, 1)
            second = task_name_for(second_workflow, 1)

            self.assertEqual(first, first_again)
            self.assertNotEqual(first, second)
            self.assertRegex(first, r"^translate_[a-z0-9_]+_[0-9a-f]{16}_a1$")
            self.assertLessEqual(len(first), 64)

    def test_prepare_and_import_validate_native_role_runtime_and_placeholders(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.make_native_fixture(Path(temporary))
            workflow = fixture["workflow"]
            handoff = prepare(
                argparse.Namespace(
                    assignment=str(fixture["assignment_path"]),
                    workflow_dir=str(workflow),
                    agent_role_file=str(fixture["role_path"]),
                    agent_role="paper-translation-worker",
                    task_name="translate_fixture_a1",
                    parent_thread_id="parent-thread-id",
                )
            )
            self.assertEqual(handoff["spawn_agent"]["agent_type"], "paper-translation-worker")
            self.assertEqual(handoff["spawn_agent"]["fork_turns"], "none")
            compact_packet = Path(str(handoff["packet_path"]))
            self.assertTrue(compact_packet.read_bytes().startswith(b"\xef\xbb\xbf"))
            native_assignment = json.loads(
                Path(str(handoff["assignment_path"])).read_text(encoding="utf-8")
            )
            transport_rows = [
                json.loads(line)
                for line in compact_packet.read_text(encoding="utf-8-sig").splitlines()
                if line.strip()
            ]
            translations: list[dict[str, str]] = []
            reference_titles: list[dict[str, str]] = []
            for row in transport_rows:
                if row["type"] == "body":
                    tokens = re.findall(r"\{\{[^{}]+\}\}", row["text"])
                    translations.append(
                        {
                            "unit_id": row["id"],
                            "zh": "中文译文" + (" " + " ".join(tokens) if tokens else "") + "。",
                        }
                    )
                elif "title" in row:
                    reference_titles.append(
                        {
                            "unit_id": row["id"],
                            "source_title": row["title"],
                            "zh": "中文题名",
                        }
                    )
                else:
                    self.assertIn("Paper", row["entry"])
                    reference_titles.append(
                        {
                            "unit_id": row["id"],
                            "source_title": "Paper",
                            "zh": "论文",
                        }
                    )
            final_message = json.dumps(
                {
                    "status": "completed",
                    "translated_units": len(transport_rows),
                    "translations": translations,
                    "reference_titles": reference_titles,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            rollout = Path(temporary) / "rollout-fixture.jsonl"
            self.write_rollout(rollout, native_assignment, final_message)
            result = import_result(
                argparse.Namespace(
                    assignment=str(handoff["assignment_path"]),
                    workflow_dir=str(workflow),
                    rollout_path=str(rollout),
                    sessions_root=None,
                )
            )
            output_path = Path(str(fixture["output_path"]))
            self.assertTrue(result["ok"])
            self.assertFalse(output_path.read_bytes().startswith(b"\xef\xbb\xbf"))
            _, errors = validate_output(read_packet(fixture["packet_path"]), output_path)
            self.assertEqual(errors, [])
            attestation = json.loads(
                Path(result["attestation_path"]).read_text(encoding="utf-8")
            )
            self.assertEqual(attestation["source"], "native_collaboration_spawn_agent")
            self.assertEqual(attestation["actual"]["agent_role"], "paper-translation-worker")
            self.assertEqual(attestation["actual"]["model"], "gpt-5.6-terra")
            self.assertEqual(attestation["actual"]["reasoning_effort"], "high")
            self.assertEqual(attestation["output_sha256"], sha256(output_path))

    def test_import_contract_error_does_not_write_valid_units_to_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.make_native_fixture(Path(temporary))
            workflow = fixture["workflow"]
            handoff = prepare(
                argparse.Namespace(
                    assignment=str(fixture["assignment_path"]),
                    workflow_dir=str(workflow),
                    agent_role_file=str(fixture["role_path"]),
                    agent_role="paper-translation-worker",
                    task_name="translate_contract_error_a1",
                    parent_thread_id="parent-thread-id",
                )
            )
            native_assignment = json.loads(
                Path(str(handoff["assignment_path"])).read_text(encoding="utf-8")
            )
            rows = [
                json.loads(line)
                for line in Path(str(handoff["packet_path"])).read_text(encoding="utf-8-sig").splitlines()
                if line.strip()
            ]
            translations: list[dict[str, str]] = []
            reference_titles: list[dict[str, str]] = []
            for row in rows:
                if row["type"] == "body":
                    tokens = re.findall(r"\{\{[^{}]+\}\}", row["text"])
                    translations.append(
                        {
                            "unit_id": row["id"],
                            "zh": "有效中文译文" + (" " + " ".join(tokens) if tokens else ""),
                        }
                    )
                elif "title" in row:
                    reference_titles.append(
                        {
                            "unit_id": row["id"],
                            "source_title": row["title"],
                            "zh": "中文题名",
                        }
                    )
                else:
                    reference_titles.append(
                        {
                            "unit_id": row["id"],
                            "source_title": "Paper",
                            "zh": "论文",
                        }
                    )
            translations.append(
                {"unit_id": "unexpected-contract-unit", "zh": "不应接受的额外译文"}
            )
            cache_path = workflow / "translation-cache.jsonl"
            cache_before = cache_path.read_bytes() if cache_path.is_file() else None
            final_message = json.dumps(
                {
                    "status": "completed",
                    "translated_units": len(rows),
                    "translations": translations,
                    "reference_titles": reference_titles,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            rollout = Path(temporary) / "rollout-contract-error.jsonl"
            self.write_rollout(rollout, native_assignment, final_message)

            result = import_result(
                argparse.Namespace(
                    assignment=str(handoff["assignment_path"]),
                    workflow_dir=str(workflow),
                    rollout_path=str(rollout),
                    sessions_root=None,
                )
            )

            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], "failed")
            self.assertTrue(result["overall_failed"])
            self.assertEqual(result["invalid_unit_count"], 0)
            attestation = json.loads(
                Path(result["attestation_path"]).read_text(encoding="utf-8")
            )
            self.assertIn(
                "unexpected body unit ids: unexpected-contract-unit",
                attestation["contract_errors"],
            )
            if cache_before is None:
                self.assertFalse(cache_path.exists())
            else:
                self.assertEqual(cache_path.read_bytes(), cache_before)

    def test_import_recovers_valid_units_when_four_units_fail(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.make_native_fixture(Path(temporary), body_paragraphs=6)
            workflow = fixture["workflow"]
            handoff = prepare(
                argparse.Namespace(
                    assignment=str(fixture["assignment_path"]),
                    workflow_dir=str(workflow),
                    agent_role_file=str(fixture["role_path"]),
                    agent_role="paper-translation-worker",
                    task_name="translate_partial_four_a1",
                    parent_thread_id="parent-thread-id",
                )
            )
            native_assignment = json.loads(
                Path(str(handoff["assignment_path"])).read_text(encoding="utf-8")
            )
            rows = [
                json.loads(line)
                for line in Path(str(handoff["packet_path"])).read_text(encoding="utf-8-sig").splitlines()
                if line.strip()
            ]
            body_index = 0
            translations = []
            reference_titles = []
            for row in rows:
                if row["type"] == "body":
                    tokens = re.findall(r"\{\{[^{}]+\}\}", row["text"])
                    zh = (
                        "invalid English-only translation " + " ".join(tokens)
                        if body_index < 4
                        else "有效中文译文" + (" " + " ".join(tokens) if tokens else "")
                    )
                    translations.append({"unit_id": row["id"], "zh": zh})
                    body_index += 1
                elif "title" in row:
                    reference_titles.append(
                        {"unit_id": row["id"], "source_title": row["title"], "zh": "中文题名"}
                    )
                else:
                    reference_titles.append(
                        {"unit_id": row["id"], "source_title": "Paper", "zh": "论文"}
                    )
            final_message = json.dumps(
                {
                    "status": "completed",
                    "translated_units": len(rows),
                    "translations": translations,
                    "reference_titles": reference_titles,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            rollout = Path(temporary) / "rollout-partial-four.jsonl"
            self.write_rollout(rollout, native_assignment, final_message)
            result = import_result(
                argparse.Namespace(
                    assignment=str(handoff["assignment_path"]),
                    workflow_dir=str(workflow),
                    rollout_path=str(rollout),
                    sessions_root=None,
                )
            )
            self.assertTrue(result["ok"])
            self.assertEqual(result["status"], "partial_success")
            self.assertEqual(result["invalid_unit_count"], 4)
            self.assertTrue(result["recovery_required"])
            self.assertFalse(Path(str(fixture["output_path"])).exists())
            self.assertTrue(Path(str(result["partial_output_path"])).is_file())
            cache_rows = [
                json.loads(line)
                for line in (workflow / "translation-cache.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(len(cache_rows), len(rows) - 4)
            recovery = build_pending_packet(
                read_packet(fixture["packet_path"]),
                workflow / "translation-cache.jsonl",
                str(native_assignment["translator_fingerprint"]),
                workflow / "recovery-pending.jsonl",
                workflow / "recovery-cached.jsonl",
                expected_model="gpt-5.6-terra",
                expected_reasoning_effort="high",
            )
            self.assertEqual(recovery["pending_count"], 4)
            state = json.loads((workflow / "workflow-state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["stages"]["translation_worker"]["status"], "recovery_pending")

    def test_import_marks_attempt_failed_at_five_invalid_units_but_keeps_valid_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.make_native_fixture(Path(temporary), body_paragraphs=6)
            workflow = fixture["workflow"]
            handoff = prepare(
                argparse.Namespace(
                    assignment=str(fixture["assignment_path"]),
                    workflow_dir=str(workflow),
                    agent_role_file=str(fixture["role_path"]),
                    agent_role="paper-translation-worker",
                    task_name="translate_threshold_five_a1",
                    parent_thread_id="parent-thread-id",
                )
            )
            native_assignment = json.loads(
                Path(str(handoff["assignment_path"])).read_text(encoding="utf-8")
            )
            rows = [
                json.loads(line)
                for line in Path(str(handoff["packet_path"])).read_text(encoding="utf-8-sig").splitlines()
                if line.strip()
            ]
            translations = []
            reference_titles = []
            body_index = 0
            for row in rows:
                if row["type"] == "body":
                    tokens = re.findall(r"\{\{[^{}]+\}\}", row["text"])
                    zh = (
                        "invalid English-only translation " + " ".join(tokens)
                        if body_index < 5
                        else "有效中文译文" + (" " + " ".join(tokens) if tokens else "")
                    )
                    translations.append({"unit_id": row["id"], "zh": zh})
                    body_index += 1
                elif "title" in row:
                    reference_titles.append(
                        {"unit_id": row["id"], "source_title": row["title"], "zh": "中文题名"}
                    )
                else:
                    reference_titles.append(
                        {"unit_id": row["id"], "source_title": "Paper", "zh": "论文"}
                    )
            final_message = json.dumps(
                {
                    "status": "completed",
                    "translated_units": len(rows),
                    "translations": translations,
                    "reference_titles": reference_titles,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            rollout = Path(temporary) / "rollout-threshold-five.jsonl"
            self.write_rollout(rollout, native_assignment, final_message)
            result = import_result(
                argparse.Namespace(
                    assignment=str(handoff["assignment_path"]),
                    workflow_dir=str(workflow),
                    rollout_path=str(rollout),
                    sessions_root=None,
                )
            )
            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["invalid_unit_count"], 5)
            self.assertTrue(result["overall_failed"])
            cache_rows = [
                json.loads(line)
                for line in (workflow / "translation-cache.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(len(cache_rows), len(rows) - 5)

    def test_global_role_change_after_handoff_uses_frozen_workflow_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.make_native_fixture(Path(temporary))
            workflow = fixture["workflow"]
            handoff = prepare(
                argparse.Namespace(
                    assignment=str(fixture["assignment_path"]),
                    workflow_dir=str(workflow),
                    agent_role_file=str(fixture["role_path"]),
                    agent_role="paper-translation-worker",
                    task_name="translate_changed_role_a1",
                    parent_thread_id="parent-thread-id",
                )
            )
            snapshot = Path(str(handoff["agent_role_path"]))
            self.assertEqual(snapshot.parent, workflow.resolve())
            snapshot_before = snapshot.read_bytes()
            Path(str(fixture["role_path"])).write_bytes(
                Path(str(fixture["role_path"])).read_bytes() + b"\n"
            )
            self.assertEqual(snapshot.read_bytes(), snapshot_before)
            with self.assertRaisesRegex(ValueError, "native subagent rollout does not exist"):
                import_result(
                    argparse.Namespace(
                        assignment=str(handoff["assignment_path"]),
                        workflow_dir=str(workflow),
                        rollout_path=str(Path(temporary) / "missing-rollout.jsonl"),
                        sessions_root=None,
                    )
                )

    def test_import_rejects_compact_packet_changed_after_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.make_native_fixture(Path(temporary))
            workflow = fixture["workflow"]
            handoff = prepare(
                argparse.Namespace(
                    assignment=str(fixture["assignment_path"]),
                    workflow_dir=str(workflow),
                    agent_role_file=str(fixture["role_path"]),
                    agent_role="paper-translation-worker",
                    task_name="translate_changed_packet_a1",
                    parent_thread_id="parent-thread-id",
                )
            )
            packet_path = Path(str(handoff["packet_path"]))
            packet_path.write_bytes(packet_path.read_bytes() + b"\n")
            with self.assertRaisesRegex(ValueError, "compact packet changed after preparation"):
                import_result(
                    argparse.Namespace(
                        assignment=str(handoff["assignment_path"]),
                        workflow_dir=str(workflow),
                        rollout_path=str(Path(temporary) / "missing-rollout.jsonl"),
                        sessions_root=None,
                    )
                )

    def test_native_final_json_allows_surrounding_whitespace(self) -> None:
        payload = {
            "status": "completed",
            "translated_units": 0,
            "translations": [],
            "reference_titles": [],
        }
        final = strict_final_message("\n  " + json.dumps(payload) + " \t\n", 0)
        self.assertEqual(final, payload)

    def test_import_rejects_rollout_from_another_parent_thread(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.make_native_fixture(Path(temporary))
            workflow = fixture["workflow"]
            handoff = prepare(
                argparse.Namespace(
                    assignment=str(fixture["assignment_path"]),
                    workflow_dir=str(workflow),
                    agent_role_file=str(fixture["role_path"]),
                    agent_role="paper-translation-worker",
                    task_name="translate_wrong_parent_a1",
                    parent_thread_id="expected-parent-thread",
                )
            )
            native_assignment = json.loads(
                Path(str(handoff["assignment_path"])).read_text(encoding="utf-8")
            )
            final_message = json.dumps(
                {
                    "status": "completed",
                    "translated_units": native_assignment["unit_count"],
                    "translations": [],
                    "reference_titles": [],
                },
                separators=(",", ":"),
            )
            rollout = Path(temporary) / "rollout-wrong-parent.jsonl"
            self.write_rollout(
                rollout,
                native_assignment,
                final_message,
                parent_id="different-parent-thread",
            )
            with self.assertRaisesRegex(ValueError, "frozen parent thread"):
                import_result(
                    argparse.Namespace(
                        assignment=str(handoff["assignment_path"]),
                        workflow_dir=str(workflow),
                        rollout_path=str(rollout),
                        sessions_root=None,
                    )
                )

    def test_import_rejects_wrong_actual_model(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.make_native_fixture(Path(temporary))
            workflow = fixture["workflow"]
            handoff = prepare(
                argparse.Namespace(
                    assignment=str(fixture["assignment_path"]),
                    workflow_dir=str(workflow),
                    agent_role_file=str(fixture["role_path"]),
                    agent_role="paper-translation-worker",
                    task_name="translate_wrong_model_a1",
                    parent_thread_id="parent-thread-id",
                )
            )
            native_assignment = json.loads(
                Path(str(handoff["assignment_path"])).read_text(encoding="utf-8")
            )
            compact_packet = Path(str(handoff["packet_path"]))
            rows = [
                json.loads(line)
                for line in compact_packet.read_text(encoding="utf-8-sig").splitlines()
                if line.strip()
            ]
            final_message = json.dumps(
                {
                    "status": "completed",
                    "translated_units": len(rows),
                    "translations": [
                        {"unit_id": row["id"], "zh": "中文译文。"}
                        for row in rows
                        if row["type"] == "body"
                    ],
                    "reference_titles": [],
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            rollout = Path(temporary) / "rollout-wrong-model.jsonl"
            self.write_rollout(
                rollout,
                native_assignment,
                final_message,
                model="gpt-5.6-sol",
            )
            with self.assertRaisesRegex(ValueError, "requested model"):
                import_result(
                    argparse.Namespace(
                        assignment=str(handoff["assignment_path"]),
                        workflow_dir=str(workflow),
                        rollout_path=str(rollout),
                        sessions_root=None,
                    )
                )


if __name__ == "__main__":
    unittest.main()
