from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from paper_translation_packet import (
    CONSTRAINTS_VERSION,
    append_validated_cache,
    append_worker_attempt,
    build_packet,
    build_pending_packet,
    extract_translation_units,
    merge_translations,
    read_packet,
    update_workflow_state,
)
from resolve_zotero_paper import resolve
from run_paper_translation_worker import (
    TRANSPORT_SCHEMA_VERSION,
    build_final_status_schema,
    build_command,
    build_transport_rows,
    build_worker_environment,
    parse_runtime_header,
    resolve_codex_executable,
    restore_transport_translation,
    run,
)
from sync_paper_translation_agent_prompt import read_fragment, render_agent


class RuntimeV3Tests(unittest.TestCase):
    def make_assignment(self, root: Path, *, units: int = 1) -> tuple[Path, Path]:
        packet = root / "translation-pending-packet.jsonl"
        packet.write_text(
            "".join(json.dumps({"unit_id": f"u{index:05d}", "kind": "paragraph", "english": "Text."}) + "\n" for index in range(1, units + 1)),
            encoding="utf-8",
        )
        output = root / "translation-output.jsonl"
        fragment = read_fragment(SKILL_ROOT / "references" / "paper-translation-prompt-fragment.md")
        _, prompt_sha256, fingerprint = render_agent(fragment, model="gpt-5.6-terra", reasoning_effort="high")
        assignment = root / "translation-assignment.json"
        assignment.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "packet_path": str(packet),
                    "output_path": str(output),
                    "unit_count": units,
                    "packet_sha256": hashlib.sha256(packet.read_bytes()).hexdigest(),
                    "prompt_sha256": prompt_sha256,
                    "translator_fingerprint": fingerprint,
                    "constraints_version": CONSTRAINTS_VERSION,
                    "requested_runtime": {"model": "gpt-5.6-terra", "reasoning_effort": "high"},
                }
            ),
            encoding="utf-8",
        )
        return assignment, output

    def runner_args(self, assignment: Path, workflow: Path, **overrides: object) -> argparse.Namespace:
        values: dict[str, object] = {
            "assignment": str(assignment),
            "workflow_dir": str(workflow),
            "fragment_path": str(SKILL_ROOT / "references" / "paper-translation-prompt-fragment.md"),
            "model": "gpt-5.6-terra",
            "reasoning_effort": "high",
            "codex_executable": "codex",
            "timeout_seconds": 1800,
            "dry_run": False,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_runner_command_is_explicit_terra_high_and_isolated(self) -> None:
        command = build_command(
            codex_executable="codex",
            model="gpt-5.6-terra",
            reasoning_effort="high",
            sterile_dir=Path("C:/sterile"),
            workflow_dir=Path("C:/workflow"),
            schema_path=Path("C:/workflow/schema.json"),
            last_message_path=Path("C:/workflow/final.json"),
        )
        self.assertEqual(command[:3], ["codex", "exec", "--ephemeral"])
        self.assertIn("--ignore-user-config", command)
        self.assertIn("--ignore-rules", command)
        self.assertEqual(command[command.index("--model") + 1], "gpt-5.6-terra")
        self.assertIn('model_reasoning_effort="high"', command)
        self.assertIn('sandbox_mode="workspace-write"', command)
        self.assertEqual(command[command.index("--sandbox") + 1], "workspace-write")
        source = (SCRIPTS / "build_obsidian_paper_card.py").read_text(encoding="utf-8")
        for forbidden in ("wait_agent", "list_agents", "Get-Process"):
            self.assertNotIn(forbidden, source)

    @unittest.skipUnless(sys.platform == "win32", "Windows launcher resolution")
    def test_windows_runner_uses_executable_cmd_or_exe_launcher(self) -> None:
        with mock.patch("run_paper_translation_worker.shutil.which", side_effect=["C:/npm/codex.cmd", None]):
            self.assertEqual(resolve_codex_executable("codex"), "C:/npm/codex.cmd")

    def test_runtime_header_parser(self) -> None:
        parsed = parse_runtime_header(
            "OpenAI Codex v9.9.9\nmodel: gpt-5.6-terra\nreasoning effort: high\nsession id: abc-123\n"
        )
        self.assertEqual(parsed["model"], "gpt-5.6-terra")
        self.assertEqual(parsed["reasoning_effort"], "high")
        self.assertEqual(parsed["codex_version"], "9.9.9")
        self.assertEqual(parsed["session_id"], "abc-123")

    def test_final_schema_binds_exact_pending_count_and_transport_is_windows_utf8(self) -> None:
        self.assertEqual(build_final_status_schema(7)["properties"]["translated_units"]["const"], 7)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            assignment, _ = self.make_assignment(root)
            result = run(self.runner_args(assignment, root, dry_run=True))
            self.assertTrue(result["ok"])
            schema = json.loads((root / "translation-worker-final.schema.json").read_text(encoding="utf-8"))
            self.assertEqual(schema["properties"]["translated_units"]["const"], 1)
            transport = next(root.glob("translation-worker-attempt-*-input.jsonl"))
            self.assertTrue(transport.read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_contributor_names_are_deterministic_passthrough_but_organizations_are_translated(self) -> None:
        source = (
            "---\ntitle: T\naliases:\n  - T\n---\n# T\n\n## Contributors\n\n"
            "Alice Smith<sup>1</sup>, Bob Jones<sup>2</sup>\n\n"
            "Open Robotics Laboratory\n\n## Introduction\n\nA body claim.\n"
        )
        units = extract_translation_units(source)
        self.assertEqual([unit["kind"] for unit in units], ["passthrough", "paragraph", "paragraph"])
        self.assertFalse(units[0]["requires_chinese"])
        self.assertTrue(units[1]["requires_chinese"])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            markdown = root / "prepared.md"
            markdown.write_text(source, encoding="utf-8")
            packet = build_packet(markdown, root / "packet.json")
            report = build_pending_packet(
                packet,
                root / "cache.jsonl",
                "fingerprint",
                root / "pending.jsonl",
                root / "cached.jsonl",
            )
            self.assertEqual(packet["schema_version"], 5)
            self.assertEqual(packet["passthrough_units"], 1)
            self.assertEqual(report["passthrough_count"], 1)
            self.assertEqual(report["pending_count"], 2)
            self.assertNotIn("Alice Smith", (root / "pending.jsonl").read_text(encoding="utf-8"))
            self.assertIn("Alice Smith", (root / "cached.jsonl").read_text(encoding="utf-8"))

    def test_contributor_names_with_email_links_are_deterministic_passthrough(self) -> None:
        source = (
            "---\ntitle: T\naliases:\n  - T\n---\n# T\n\n## Authors\n\n"
            "Alice Smith  Bob Jones  Carol Green  David Brown "
            "[alice.research@example.org](https://example.com/mailto:alice.research@example.org) "
            "[bob@lab.example.org](mailto:bob@lab.example.org)\n\n"
            "Open Robotics Laboratory\n\n## Abstract\n\nA body claim.\n"
        )
        units = extract_translation_units(source)
        self.assertEqual([unit["kind"] for unit in units], ["passthrough", "paragraph", "abstract"])
        self.assertFalse(units[0]["requires_chinese"])
        self.assertTrue(units[1]["requires_chinese"])

    def test_identity_numeric_and_footnote_labels_never_enter_worker_packet(self) -> None:
        source = (
            "---\ntitle: T\naliases:\n  - T\n---\n# T\n\n"
            "Author:\n\nDyna Robotics\n\n01\n\n43.8M\n\nA2A\n\n"
            "[^1]: 01\n\n## Introduction\n\nA semantic body claim.\n"
        )
        units = extract_translation_units(source)
        by_text = {unit["english"]: unit for unit in units}
        for value in ("Dyna Robotics", "01", "43.8M", "A2A", "[^1]: 01"):
            self.assertEqual(by_text[value]["kind"], "passthrough")
            self.assertFalse(by_text[value]["requires_chinese"])
        self.assertTrue(by_text["Author:"]["requires_chinese"])
        self.assertTrue(by_text["A semantic body claim."]["requires_chinese"])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            markdown = root / "prepared.md"
            markdown.write_text(source, encoding="utf-8")
            packet = build_packet(markdown, root / "packet.json")
            report = build_pending_packet(
                packet,
                root / "cache.jsonl",
                "fingerprint",
                root / "pending.jsonl",
                root / "cached.jsonl",
            )
            pending = (root / "pending.jsonl").read_text(encoding="utf-8")
            self.assertEqual(report["passthrough_count"], 5)
            self.assertEqual(report["pending_count"], 2)
            for value in ("Dyna Robotics", "43.8M", "A2A", "[^1]: 01"):
                self.assertNotIn(value, pending)

    def test_markup_only_superscript_is_passthrough_but_semantic_markup_is_translated(self) -> None:
        source = (
            "---\ntitle: T\naliases:\n  - T\n---\n# T\n\n## Abstract\n\n"
            "A body claim.\n\n<sup>*</sup>\n\n<sup>Shared contribution</sup>\n\n"
            "## Introduction\n\nAnother body claim.\n"
        )
        units = extract_translation_units(source)
        self.assertEqual(
            [unit["kind"] for unit in units],
            ["abstract", "passthrough", "abstract", "paragraph"],
        )
        self.assertEqual(
            [unit["requires_chinese"] for unit in units],
            [True, False, True, True],
        )

    def test_preamble_authors_and_protected_only_units_are_deterministic_passthrough(self) -> None:
        source = (
            "---\ntitle: \"Reflex: Streaming Control\"\naliases:\n  - Reflex\n---\n"
            "# Reflex Streaming Control\n\nYuanchun Guo \u2003\u2003 Bingyan Liu\n\n###### Abstract\n\n"
            "A streaming controller.\n\n## Method\n\n"
            "$\\text{queue}\\leftarrow\\text{initial\\_inference}()$\n\n"
            "Equation $x_t$ remains stable.\n"
        )
        units = extract_translation_units(source)
        self.assertEqual(
            [unit["kind"] for unit in units],
            ["passthrough", "abstract", "passthrough", "paragraph"],
        )
        self.assertEqual([unit["requires_chinese"] for unit in units], [False, True, False, True])
        translations = {
            units[0]["unit_id"]: {"zh": units[0]["english"]},
            units[1]["unit_id"]: {"zh": "一个流式控制器。"},
            units[2]["unit_id"]: {"zh": units[2]["english"]},
            units[3]["unit_id"]: {"zh": "公式 $x_t$ 保持稳定。"},
        }
        with tempfile.TemporaryDirectory() as temporary:
            markdown = Path(temporary) / "prepared.md"
            markdown.write_text(source, encoding="utf-8")
            packet = build_packet(markdown, Path(temporary) / "packet.json")
            merged = merge_translations(markdown, packet, translations)
        self.assertEqual(merged.count("Yuanchun Guo"), 2)
        self.assertEqual(merged.count("initial\\_inference"), 2)

    def test_unicode_superscript_author_list_is_deterministic_passthrough(self) -> None:
        source = (
            "---\ntitle: T\naliases:\n  - T\n---\n# T\n\n"
            "Yaron Lipman¹,² · Ricky T. Q. Chen¹ · Heli Ben-Hamu² · "
            "Maximilian Nickel¹ · Matt Le¹\n\n## Abstract\n\nA body claim.\n"
        )
        units = extract_translation_units(source)
        self.assertEqual([unit["kind"] for unit in units], ["passthrough", "abstract"])
        self.assertFalse(units[0]["requires_chinese"])

    def test_preamble_title_equal_to_frontmatter_is_not_misclassified_as_contributor(self) -> None:
        source = (
            "---\ntitle: Deep Learning\naliases:\n  - Deep Learning\n---\n"
            "Deep Learning\n\n###### Abstract\n\nA short abstract.\n"
        )
        units = extract_translation_units(source)
        self.assertEqual([unit["kind"] for unit in units], ["paragraph", "abstract"])
        self.assertTrue(units[0]["requires_chinese"])

    def test_partial_worker_output_is_attested_and_rerun_packet_contains_only_missing_units(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            assignment, _ = self.make_assignment(root, units=2)

            class PartialProcess:
                returncode = 0
                pid = 701

                def __init__(self, command: list[str], **_: object) -> None:
                    self.command = command

                def communicate(self, input: str | None = None, timeout: int | None = None) -> tuple[str, None]:
                    final_path = Path(self.command[self.command.index("--output-last-message") + 1])
                    final_path.write_text(
                        json.dumps(
                            {
                                "status": "completed",
                                "translated_units": 2,
                                "translations": [{"unit_id": "u00001", "zh": "第一段译文。"}],
                                "reference_titles": [],
                            },
                            ensure_ascii=False,
                        ),
                        encoding="utf-8",
                    )
                    return "OpenAI Codex v9.9.9\nmodel: gpt-5.6-terra\nreasoning effort: high\nsession id: partial\n", None

                def poll(self) -> int:
                    return self.returncode

            with mock.patch("run_paper_translation_worker.subprocess.Popen", PartialProcess):
                result = run(self.runner_args(assignment, root))
            self.assertFalse(result["ok"])
            self.assertEqual(result["failure_class"], "unit_validation")
            self.assertEqual(result["partial_validated_units"], 1)
            attestation = json.loads(Path(result["attestation_path"]).read_text(encoding="utf-8"))
            partial_path = Path(attestation["partial_output_path"])
            self.assertEqual(hashlib.sha256(partial_path.read_bytes()).hexdigest(), attestation["partial_output_sha256"])

            markdown = root / "prepared.md"
            markdown.write_text("# T\n\nFirst paragraph.\n\nSecond paragraph.\n", encoding="utf-8")
            packet = build_packet(markdown, root / "packet.json")
            cache = root / "cache.jsonl"
            append_validated_cache(
                cache,
                packet,
                {"u00001": {"zh": "第一段译文。"}},
                "fingerprint",
                {"model": "gpt-5.6-terra", "reasoning_effort": "high", "runtime_verified": True},
            )
            pending = build_pending_packet(
                packet,
                cache,
                "fingerprint",
                root / "rerun-pending.jsonl",
                root / "rerun-cached.jsonl",
                expected_model="gpt-5.6-terra",
                expected_reasoning_effort="high",
            )
            self.assertEqual(pending["pending_count"], 1)
            self.assertIn('"unit_id":"u00002"', (root / "rerun-pending.jsonl").read_text(encoding="utf-8"))
            self.assertNotIn('"unit_id":"u00001"', (root / "rerun-pending.jsonl").read_text(encoding="utf-8"))

    def test_network_disconnect_is_retryable_without_automatic_second_process(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            assignment, _ = self.make_assignment(root)
            starts = 0

            class NetworkProcess:
                returncode = 1
                pid = 702

                def __init__(self, command: list[str], **_: object) -> None:
                    nonlocal starts
                    starts += 1

                def communicate(self, input: str | None = None, timeout: int | None = None) -> tuple[str, None]:
                    return "WebSocket WS401: HTTPS connection closed unexpectedly\n", None

                def poll(self) -> int:
                    return self.returncode

            with mock.patch("run_paper_translation_worker.subprocess.Popen", NetworkProcess):
                result = run(self.runner_args(assignment, root))
            self.assertEqual(starts, 1)
            self.assertFalse(result["ok"])
            self.assertEqual(result["failure_class"], "network_transport")
            self.assertTrue(result["retryable"])

    def test_worker_environment_does_not_inherit_parent_codex_session_policy(self) -> None:
        environment = build_worker_environment(
            {
                "PATH": "C:/bin",
                "CODEX_HOME": "C:/codex-home",
                "CODEX_CI": "1",
                "CODEX_THREAD_ID": "parent-thread",
                "CODEX_PERMISSION_PROFILE": ":read-only",
            }
        )
        self.assertEqual(environment["PATH"], "C:/bin")
        self.assertEqual(environment["CODEX_HOME"], "C:/codex-home")
        self.assertNotIn("CODEX_CI", environment)
        self.assertNotIn("CODEX_THREAD_ID", environment)
        self.assertNotIn("CODEX_PERMISSION_PROFILE", environment)

    def test_protected_tokens_use_transport_placeholders_and_restore_exactly(self) -> None:
        rows = [
            {
                "unit_id": "u00001",
                "order": 1,
                "kind": "paragraph",
                "english": "Minimize $E(x)$ using [[#^ref-1|¹]] and note[^n].",
                "protected": {
                    "inline_math": ["$E(x)$"],
                    "citation_tokens": ["[[#^ref-1|¹]]"],
                    "markdown_footnotes": ["[^n]"],
                    "emphasis_tokens": [],
                },
            }
        ]
        transported, mappings = build_transport_rows(rows)
        self.assertEqual(transported[0]["type"], "body")
        self.assertNotIn("$E(x)$", transported[0]["text"])
        self.assertNotIn("[[#^ref-1|¹]]", transported[0]["text"])
        translated = "最小化 {{PT:u00001:001}}，并使用 {{PT:u00001:002}} 和注释{{PT:u00001:003}}。"
        restored = restore_transport_translation("u00001", translated, mappings["u00001"])
        self.assertIn("$E(x)$", restored)
        self.assertIn("[[#^ref-1|¹]]", restored)
        self.assertIn("[^n]", restored)
        with self.assertRaises(ValueError):
            restore_transport_translation("u00001", translated.replace("{{PT:u00001:002}}", ""), mappings["u00001"])

    def test_model_mismatch_deletes_untrusted_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            assignment, output = self.make_assignment(root)

            class FakeProcess:
                returncode = 0
                pid = 123

                def __init__(self, command: list[str], **_: object) -> None:
                    self.command = command

                def communicate(self, input: str | None = None, timeout: int | None = None) -> tuple[str, None]:
                    output.write_text('{"unit_id":"u00001","zh":"中文"}\n', encoding="utf-8")
                    final_path = Path(self.command[self.command.index("--output-last-message") + 1])
                    final_path.write_text(
                        json.dumps(
                            {
                                "status": "completed",
                                "translated_units": 1,
                                "output_path": str(output),
                                "output_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
                            }
                        ),
                        encoding="utf-8",
                    )
                    return (
                        "OpenAI Codex v9.9.9\nmodel: gpt-5.6-sol\nreasoning effort: high\nsession id: wrong-model\n",
                        None,
                    )

                def poll(self) -> int:
                    return self.returncode

            with mock.patch("run_paper_translation_worker.subprocess.Popen", FakeProcess):
                result = run(self.runner_args(assignment, root))
            self.assertFalse(result["ok"])
            self.assertEqual(result["failure_class"], "runtime_mismatch")
            self.assertFalse(output.exists())
            attestations = list(root.glob("translation-worker-attempt-*-attestation.json"))
            self.assertEqual(len(attestations), 1)
            attestation = json.loads(attestations[0].read_text(encoding="utf-8"))
            self.assertFalse(attestation["actual"]["runtime_verified"])

    def test_runner_accepts_body_and_reference_title_arrays_from_one_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            assignment, output = self.make_assignment(root)
            packet = root / "translation-pending-packet.jsonl"
            rows = [
                {
                    "unit_id": "u00001",
                    "kind": "paragraph",
                    "english": "Body text.",
                    "protected": {"inline_math": [], "citation_tokens": [], "markdown_footnotes": [], "emphasis_tokens": []},
                },
                {
                    "unit_id": "r00001",
                    "kind": "reference_title",
                    "english": "A precise title",
                    "reference_entry": "A. Author. A precise title. Journal, 2024.",
                    "title_hint": "A precise title",
                    "extraction_mode": "deterministic",
                    "protected": {"inline_math": [], "citation_tokens": [], "markdown_footnotes": [], "emphasis_tokens": []},
                },
            ]
            packet.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            manifest = json.loads(assignment.read_text(encoding="utf-8"))
            manifest.update(
                {
                    "schema_version": 3,
                    "unit_count": 2,
                    "packet_sha256": hashlib.sha256(packet.read_bytes()).hexdigest(),
                }
            )
            assignment.write_text(json.dumps(manifest), encoding="utf-8")

            class FakeProcess:
                returncode = 0
                pid = 456

                def __init__(self, command: list[str], **_: object) -> None:
                    self.command = command

                def communicate(self, input: str | None = None, timeout: int | None = None) -> tuple[str, None]:
                    final_path = Path(self.command[self.command.index("--output-last-message") + 1])
                    final_path.write_text(
                        json.dumps(
                            {
                                "status": "completed",
                                "translated_units": 2,
                                "translations": [{"unit_id": "u00001", "zh": "正文译文。"}],
                                "reference_titles": [
                                    {"unit_id": "r00001", "source_title": "A precise title", "zh": "一个精确的题名"}
                                ],
                            }
                        ),
                        encoding="utf-8",
                    )
                    return (
                        "OpenAI Codex v9.9.9\nmodel: gpt-5.6-terra\nsandbox: read-only\nreasoning effort: high\nsession id: verified\n",
                        None,
                    )

                def poll(self) -> int:
                    return self.returncode

            with mock.patch("run_paper_translation_worker.subprocess.Popen", FakeProcess):
                result = run(self.runner_args(assignment, root))
            self.assertTrue(result["runtime_verified"])
            output_rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([row["unit_id"] for row in output_rows], ["u00001", "r00001"])
            self.assertEqual(output_rows[1]["source_title"], "A precise title")
            self.assertEqual(output_rows[1]["zh"], "一个精确的题名")
            attestations = list(root.glob("translation-worker-attempt-*-attestation.json"))
            self.assertEqual(len(attestations), 1)
            attestation = json.loads(attestations[0].read_text(encoding="utf-8"))
            self.assertTrue(attestation["actual"]["runtime_verified"])
            self.assertEqual(attestation["transport"]["schema_version"], TRANSPORT_SCHEMA_VERSION)
            self.assertEqual(attestation["transport"]["body_units"], 1)
            self.assertEqual(attestation["transport"]["reference_title_units"], 1)
            self.assertEqual(attestation["transport"]["fallback_title_units"], 0)

    def test_worker_attempts_are_append_only_across_prepare(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "workflow-state.json"
            update_workflow_state(state, stage="translation_worker", status="pending", metadata={"pending_count": 2})
            append_worker_attempt(state, {"pending_units": 2, "duration_ms": 10, "exit_code": 2}, status="failed")
            update_workflow_state(state, stage="translation_worker", status="pending", metadata={"pending_count": 2})
            append_worker_attempt(state, {"pending_units": 2, "duration_ms": 20, "exit_code": 0}, status="completed")
            attempts = json.loads(state.read_text(encoding="utf-8"))["stages"]["translation_worker"]["attempts"]
            self.assertEqual([attempt["attempt"] for attempt in attempts], [1, 2])
            self.assertEqual([attempt["duration_ms"] for attempt in attempts], [10, 20])

    def test_references_enter_only_as_title_units_and_cache_only_title_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            markdown = root / "prepared.md"
            packet_path = root / "packet.json"
            markdown.write_text(
                "---\ntitle: T\naliases:\n  - T\nbilingual_layout: english_blockquote_chinese_body\n---\n"
                "# T\n\nBody claim [[#^ref-1|¹]].\n\n## References\n\n"
                "[1] A. Author and B. Writer. A precise paper title. Journal of Tests, 2024. ^ref-1\n",
                encoding="utf-8",
            )
            packet = build_packet(markdown, packet_path)
            self.assertEqual(packet["reference_blocks"], 1)
            self.assertEqual(packet["schema_version"], 5)
            self.assertEqual(len(packet["units"]), 2)
            title_unit = next(unit for unit in packet["units"] if unit.get("kind") == "reference_title")
            self.assertEqual(title_unit["english"], "A precise paper title")
            self.assertEqual(title_unit["unit_id"], "r00001")
            transported, _ = build_transport_rows([title_unit])
            self.assertNotIn("A. Author", json.dumps(transported, ensure_ascii=False))
            self.assertEqual(
                transported,
                [{"id": "r00001", "type": "reference_title", "title": "A precise paper title"}],
            )
            cache = root / "cache.jsonl"
            append_validated_cache(
                cache,
                packet,
                {
                    "u00001": {"zh": "中文正文 [[#^ref-1|¹]]。"},
                    "r00001": {"source_title": "A precise paper title", "zh": "一个精确的论文题名"},
                },
                "fingerprint",
                {"model": "gpt-5.6-terra", "reasoning_effort": "high", "runtime_verified": True},
            )
            cached_text = cache.read_text(encoding="utf-8")
            self.assertNotIn("A. Author", cached_text)
            self.assertIn("一个精确的论文题名", cached_text)

    def test_compact_transport_removes_duplicate_reference_payloads(self) -> None:
        deterministic = {
            "unit_id": "r00001",
            "order": 1,
            "kind": "reference_title",
            "english": "A precise paper title",
            "reference_entry": "A. Author. A precise paper title. Journal, 2024.",
            "title_hint": "A precise paper title",
            "extraction_mode": "deterministic",
            "protected": {"inline_math": [], "citation_tokens": [], "markdown_footnotes": [], "emphasis_tokens": []},
        }
        fallback_entry = "Interbotix ROS manipulators. https://example.com/interbotix, 2023."
        fallback = {
            "unit_id": "r00002",
            "order": 2,
            "kind": "reference_title",
            "english": fallback_entry,
            "reference_entry": fallback_entry,
            "title_hint": None,
            "extraction_mode": "worker",
            "protected": {"inline_math": [], "citation_tokens": [], "markdown_footnotes": [], "emphasis_tokens": []},
        }
        compact, _ = build_transport_rows([deterministic, fallback])
        compact_json = "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) for row in compact)
        legacy_json = "".join(
            json.dumps(
                {
                    "unit_id": row["unit_id"],
                    "order": row["order"],
                    "kind": row["kind"],
                    "english": row["english"],
                    "reference_entry": row["reference_entry"] if row["extraction_mode"] == "worker" else None,
                    "title_hint": row["title_hint"],
                    "extraction_mode": row["extraction_mode"],
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            for row in (deterministic, fallback)
        )
        self.assertLess(len(compact_json), len(legacy_json) * 0.6)
        self.assertEqual(compact_json.count("A precise paper title"), 1)
        self.assertEqual(compact_json.count(fallback_entry), 1)
        self.assertEqual(compact[0], {"id": "r00001", "type": "reference_title", "title": "A precise paper title"})
        self.assertEqual(compact[1], {"id": "r00002", "type": "reference_title", "entry": fallback_entry})

    def test_cache_requires_verified_quality_tier_not_exact_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            markdown = root / "prepared.md"
            packet_path = root / "packet.json"
            markdown.write_text("# T\n\nBody claim.\n", encoding="utf-8")
            packet = build_packet(markdown, packet_path)
            cache = root / "cache.jsonl"
            append_validated_cache(
                cache,
                packet,
                {"u00001": "中文正文。"},
                "fingerprint",
                {"model": "gpt-5.6-terra", "reasoning_effort": "high", "runtime_verified": False},
            )
            first = build_pending_packet(
                packet,
                cache,
                "fingerprint",
                root / "pending.jsonl",
                root / "cached.jsonl",
                expected_model="gpt-5.6-terra",
                expected_reasoning_effort="high",
            )
            self.assertEqual(first["pending_count"], 1)
            append_validated_cache(
                cache,
                packet,
                {"u00001": "中文正文。"},
                "fingerprint",
                {"model": "gpt-6-terra", "reasoning_effort": "xhigh", "runtime_verified": True},
            )
            second = build_pending_packet(
                packet,
                cache,
                "fingerprint",
                root / "pending.jsonl",
                root / "cached.jsonl",
                expected_model="gpt-5.6-terra",
                expected_reasoning_effort="high",
            )
            self.assertEqual(second["cached_count"], 1)

            strict = build_pending_packet(
                packet,
                cache,
                "fingerprint",
                root / "strict-pending.jsonl",
                root / "strict-cached.jsonl",
                strict_runtime=True,
                expected_model="gpt-5.6-terra",
                expected_reasoning_effort="high",
            )
            self.assertEqual(strict["pending_count"], 1)


class ZoteroResolverTests(unittest.TestCase):
    def make_database(self, root: Path, *, titles: list[str], pdf_counts: list[int]) -> tuple[Path, list[str]]:
        data = root / "Zotero"
        data.mkdir()
        connection = sqlite3.connect(data / "zotero.sqlite")
        connection.executescript(
            """
            CREATE TABLE items(itemID INTEGER PRIMARY KEY, itemTypeID INT, dateAdded TEXT, dateModified TEXT, clientDateModified TEXT, libraryID INT, key TEXT, version INT, synced INT);
            CREATE TABLE fields(fieldID INTEGER PRIMARY KEY, fieldName TEXT);
            CREATE TABLE itemDataValues(valueID INTEGER PRIMARY KEY, value TEXT);
            CREATE TABLE itemData(itemID INT, fieldID INT, valueID INT);
            CREATE TABLE deletedItems(itemID INT);
            CREATE TABLE itemAttachments(itemID INTEGER PRIMARY KEY, parentItemID INT, linkMode INT, contentType TEXT, path TEXT);
            INSERT INTO fields(fieldID, fieldName) VALUES(1, 'title');
            """
        )
        keys: list[str] = []
        next_item = 1
        next_value = 1
        for title, pdf_count in zip(titles, pdf_counts):
            item_id = next_item
            item_key = f"ITEM{item_id:04d}"
            keys.append(item_key)
            connection.execute("INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?)", (item_id, 1, "", "", "", 1, item_key, 0, 0))
            connection.execute("INSERT INTO itemDataValues VALUES(?,?)", (next_value, title))
            connection.execute("INSERT INTO itemData VALUES(?,?,?)", (item_id, 1, next_value))
            next_item += 1
            next_value += 1
            for number in range(pdf_count):
                attachment_id = next_item
                attachment_key = f"ATT{attachment_id:05d}"
                filename = f"paper-{number}.pdf"
                connection.execute("INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?)", (attachment_id, 14, "", "", "", 1, attachment_key, 0, 0))
                connection.execute(
                    "INSERT INTO itemAttachments VALUES(?,?,?,?,?)",
                    (attachment_id, item_id, 0, "application/pdf", f"storage:{filename}"),
                )
                folder = data / "storage" / attachment_key
                folder.mkdir(parents=True)
                (folder / filename).write_bytes(b"%PDF-1.4\nfixture\n")
                next_item += 1
        connection.commit()
        connection.close()
        return data, keys

    def resolver_args(self, root: Path, data: Path, title: str) -> argparse.Namespace:
        vault = root / "vault"
        vault.mkdir(exist_ok=True)
        return argparse.Namespace(
            title=title,
            zotero_data_dir=str(data),
            vault_root=str(vault),
            target_directory="论文",
            snapshot_directory=str(root / "snapshots"),
            parse_options_fingerprint=None,
        )

    def test_vault_source_order_is_clippings_then_papers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data, _ = self.make_database(root, titles=["Exact Paper"], pdf_counts=[1])
            args = self.resolver_args(root, data, "Exact Paper")
            vault = Path(args.vault_root)
            clippings = vault / "Clippings"
            papers = vault / "论文"
            clippings.mkdir()
            papers.mkdir()
            clip = clippings / "captured-source.md"
            paper = papers / "paper-source.md"
            clip.write_text("---\ntitle: Exact Paper\n---\nclip\n", encoding="utf-8")
            paper.write_text("---\ntitle: Exact Paper\n---\npaper\n", encoding="utf-8")

            code, result = resolve(args)
            self.assertEqual(code, 0)
            self.assertEqual(result["source_kind"], "vault_markdown")
            self.assertEqual(result["source_scope"], "Clippings")
            self.assertEqual(result["source_search_order"], ["Clippings", "论文", "Zotero"])
            self.assertEqual(Path(result["input_markdown"]), clip.resolve())
            self.assertFalse((root / "snapshots").exists())

            clip.unlink()
            code, result = resolve(args)
            self.assertEqual(code, 0)
            self.assertEqual(result["source_scope"], "论文")
            self.assertEqual(Path(result["input_markdown"]), paper.resolve())
            self.assertFalse((root / "snapshots").exists())

    def test_unique_title_resolves_pdf_and_cleans_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data, _ = self.make_database(root, titles=["Exact Paper"], pdf_counts=[1])
            code, result = resolve(self.resolver_args(root, data, "Exact Paper"))
            self.assertEqual(code, 0)
            self.assertTrue(result["ok"])
            self.assertEqual(result["source_kind"], "zotero_pdf")
            self.assertEqual(result["source_search_order"], ["Clippings", "论文", "Zotero"])
            self.assertEqual(result["pdf_bytes"], 17)
            self.assertEqual(len(result["pdf_sha256"]), 64)
            self.assertFalse(any((root / "snapshots").iterdir()))

    def test_ambiguous_missing_and_multiple_pdf_fail_without_guessing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data, _ = self.make_database(root, titles=["Same Paper", "Same Paper", "No PDF", "Two PDFs"], pdf_counts=[1, 1, 0, 2])
            for title, error in (
                ("Same Paper", "multiple_exact_title_matches"),
                ("No PDF", "no_pdf_attachment"),
                ("Two PDFs", "multiple_pdf_attachments"),
            ):
                with self.subTest(title=title):
                    code, result = resolve(self.resolver_args(root, data, title))
                    self.assertEqual(code, 2)
                    self.assertEqual(result["error"], error)


if __name__ == "__main__":
    unittest.main()
