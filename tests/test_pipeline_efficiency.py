from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from build_obsidian_paper_card import (
    backup_existing,
    build,
    compact_cli_report,
    convert_html_tables,
    ensure_card_frontmatter,
    normalize_heading_levels,
    normalize_mixed_image_syntax,
    repair_mojibake_safely,
    run_optional_concept_links,
    should_retry_translation_attempt,
)
from paper_translation_packet import (
    build_merge_template,
    build_packet,
    fill_merge_template,
    read_packet,
    validate_output,
)
from sync_paper_translation_agent_prompt import read_fragment, render_agent
from validate_full_paper_card import validate_full


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


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
        "in_place": False,
        "write": True,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class PipelineEfficiencyTests(unittest.TestCase):
    def test_backup_skips_identical_content_and_keeps_two_recent_versions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            note = Path(temporary) / "Paper.md"
            note.write_text("v1\n", encoding="utf-8", newline="\n")
            self.assertIsNone(
                backup_existing(
                    note,
                    "before-paper-card-promotion",
                    replacement_text="v1\n",
                )
            )
            for index in range(3):
                backup_existing(
                    note,
                    "before-paper-card-promotion",
                    replacement_text=f"v{index + 2}\n",
                )
                note.write_text(f"v{index + 2}\n", encoding="utf-8", newline="\n")
            backups = list(note.parent.glob("Paper.md.bak-*-before-paper-card-promotion"))
            self.assertEqual(len(backups), 2)

    def test_full_validator_decodes_each_image_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            note_dir = vault / "论文"
            resources = vault / "_resources"
            note_dir.mkdir()
            resources.mkdir()
            image_path = resources / "once.png"
            image_path.write_bytes(PNG_1X1)
            note = note_dir / "Once.md"
            note.write_text(
                "---\n"
                "title: Once\n"
                "aliases:\n"
                "  - Once\n"
                "cssclasses:\n"
                "  - paper-card-centered-images\n"
                "---\n"
                "# Once\n\n"
                "![once](../_resources/once.png)\n\n"
                "A body sentence.\n",
                encoding="utf-8",
                newline="\n",
            )
            from PIL import Image

            real_open = Image.open
            with mock.patch.object(Image, "open", wraps=real_open) as image_open:
                validate_full(note, vault, stage="prepared")

            self.assertEqual(image_open.call_count, 1)

    def test_worker_retry_policy_is_bounded_and_retryable_only(self) -> None:
        self.assertTrue(should_retry_translation_attempt({"retryable": True}, 2))
        self.assertFalse(should_retry_translation_attempt({"retryable": True}, 1))
        self.assertFalse(should_retry_translation_attempt({"retryable": False}, 2))

    def test_abstract_is_canonical_h2_without_rewriting_local_h6_headings(self) -> None:
        source = (
            "---\ntitle: Reflex\n---\n# Reflex\n\n###### Abstract\n\nSummary.\n\n"
            "###### Proposition A.1.\n\nClaim.\n\n###### Proof.\n"
        )
        normalized = normalize_heading_levels(source)
        self.assertIn("\n## Abstract\n", normalized)
        self.assertNotIn("###### Abstract", normalized)
        self.assertIn("###### Proposition A.1.", normalized)
        self.assertIn("###### Proof.", normalized)

    def test_mixed_image_syntax_is_repaired_without_touching_legal_wikilink_embed(self) -> None:
        source = "![[descriptive alt]](images/figure.png)\n\n![[legal-embed.png]]\n"
        normalized, report = normalize_mixed_image_syntax(source)
        self.assertEqual(report["normalized"], 1)
        self.assertIn("![descriptive alt](images/figure.png)", normalized)
        self.assertIn("![[legal-embed.png]]", normalized)

    def test_frontmatter_preserves_fields_and_fills_empty_aliases(self) -> None:
        source = "---\ntitle: Example\naliases: []\ncustom: keep\n---\n# Example\n\nText.\n"
        result = ensure_card_frontmatter(source, "Fallback", "bilingual", "prepare")
        self.assertIn("custom: keep", result)
        self.assertIn('aliases:\n  - "Example"', result)
        self.assertIn('bilingual_layout: "english_blockquote_chinese_body"', result)
        self.assertIn("cssclasses:\n  - paper-card-centered-images", result)
        self.assertIn('translation_status: "pending"', result)
        self.assertIn('translation_skill: "markdown-to-obsidian-paper-card"', result)

    def test_final_frontmatter_replaces_stale_translation_metadata(self) -> None:
        source = (
            "---\ntitle: Example\naliases:\n  - Example\n"
            'translation_status: "pending-batched-agent-translation"\n'
            'translation_skill: "ai-research-writing"\n'
            "---\n# Example\n\nText.\n"
        )
        result = ensure_card_frontmatter(source, "Fallback", "bilingual", "finalize")
        self.assertIn('translation_status: "completed"', result)
        self.assertIn('translation_skill: "markdown-to-obsidian-paper-card"', result)
        self.assertNotIn("pending-batched-agent-translation", result)
        self.assertNotIn("ai-research-writing", result)

    def test_rowspan_and_colspan_are_expanded_deterministically(self) -> None:
        source = (
            '<table><tr><th rowspan="2">Method</th><th colspan="2">Score</th></tr>'
            '<tr><th>A</th><th>B</th></tr><tr><td>Ours</td><td>1</td><td>2</td></tr></table>'
        )
        result, report = convert_html_tables(source)
        self.assertEqual(report["span_tables_converted"], 1)
        self.assertEqual(report["needs_review"], 0)
        self.assertIn("| Method | Score | Score |", result)
        self.assertIn("| Method | A | B |", result)

    def test_nested_table_remains_and_fails_preflight_count(self) -> None:
        source = "<table><tr><td><table><tr><td>x</td></tr></table></td></tr></table>"
        result, report = convert_html_tables(source)
        self.assertEqual(result, source)
        self.assertEqual(report["needs_review"], 1)

    def test_safe_mojibake_repair(self) -> None:
        repaired, report = repair_mojibake_safely("A cafÃ© method.\n")
        self.assertEqual(repaired, "A café method.\n")
        self.assertEqual(report["residual_markers"], 0)

    def test_prompt_fingerprint_changes_with_each_identity_input(self) -> None:
        fragment = read_fragment(SKILL_ROOT / "references" / "paper-translation-prompt-fragment.md")
        rendered, prompt_hash, fingerprint = render_agent(fragment, model="gpt-5.6-terra", reasoning_effort="high")
        _, _, changed_model = render_agent(fragment, model="different-model", reasoning_effort="high")
        _, _, changed_effort = render_agent(fragment, model="gpt-5.6-terra", reasoning_effort="medium")
        _, _, changed_prompt = render_agent(fragment + "\nAdditional fidelity constraint.", model="gpt-5.6-terra", reasoning_effort="high")
        with mock.patch("sync_paper_translation_agent_prompt.PACKET_CONSTRAINTS_VERSION", "changed-contract"):
            _, _, changed_contract = render_agent(fragment, model="gpt-5.6-terra", reasoning_effort="high")
        self.assertNotEqual(fingerprint, changed_model)
        self.assertNotEqual(fingerprint, changed_effort)
        self.assertNotEqual(fingerprint, changed_prompt)
        self.assertNotEqual(fingerprint, changed_contract)
        self.assertEqual(prompt_hash, hashlib.sha256(fragment.encode("utf-8")).hexdigest())
        for banned in ("camera-ready", "BibTeX", "humanizer", "Related Work strategy"):
            self.assertNotIn(banned, rendered)
        self.assertNotIn("For `reference` units", rendered)
        self.assertNotIn("preserve authors, year, venue", rendered)
        self.assertIn("Never add, remove, fabricate, renumber, or replace citations", rendered)

    def test_prompt_sync_check_rejects_stale_agent(self) -> None:
        script = SCRIPTS / "sync_paper_translation_agent_prompt.py"
        with tempfile.TemporaryDirectory() as temporary:
            agent = Path(temporary) / "paper-worker.toml"
            agent.write_text("stale\n", encoding="utf-8")
            stale = subprocess.run(
                [sys.executable, str(script), "--check", "--agent-path", str(agent)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(stale.returncode, 2)
            self.assertEqual(json.loads(stale.stdout)["status"], "stale")
            written = subprocess.run(
                [sys.executable, str(script), "--write", "--agent-path", str(agent)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(written.returncode, 0, written.stderr)
            synchronized = subprocess.run(
                [sys.executable, str(script), "--check", "--agent-path", str(agent)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(synchronized.returncode, 0, synchronized.stderr)

    def test_fixed_layout_and_protected_native_footnote(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            markdown = root / "prepared.md"
            packet_path = root / "packet.json"
            markdown.write_text(
                "---\ntitle: T\naliases:\n  - T\nbilingual_layout: english_blockquote_chinese_body\n---\n# T\n\nA claim with $x$ and note[^note].\n",
                encoding="utf-8",
            )
            packet = build_packet(markdown, packet_path)
            self.assertIn("[^note]", packet["units"][0]["protected"]["markdown_footnotes"])
            template = build_merge_template(markdown, packet)
            merged = fill_merge_template(template, packet, {"u00001": "中文论断，保留 $x$ 和注释[^note]。"})
            self.assertIn("> A claim with $x$ and note[^note].\n\n中文论断", merged)

            invalid_cases = {
                "missing": "",
                "duplicate": (
                    '{"unit_id":"u00001","zh":"中文 $x$ [^note]"}\n'
                    '{"unit_id":"u00001","zh":"重复 $x$ [^note]"}\n'
                ),
                "unknown": '{"unit_id":"u99999","zh":"未知"}\n',
                "lost_token": '{"unit_id":"u00001","zh":"中文但丢失保护符号"}\n',
            }
            for label, content in invalid_cases.items():
                output = root / f"{label}.jsonl"
                output.write_text(content, encoding="utf-8")
                _, errors = validate_output(packet, output)
                self.assertTrue(errors, label)

    def test_strict_preflight_failures_create_no_assignment(self) -> None:
        fixtures = {
            "bad-data-uri": "![broken](data:image/png;base64,not-base64)",
            "absolute-image": "![absolute](C:/missing/image.png)",
            "nested-table": "<table><tr><td><table><tr><td>x</td></tr></table></td></tr></table>",
            "ambiguous-mojibake": "An uncertain replacement character � remains.",
        }
        for label, body in fixtures.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                vault = root / "vault"
                vault.mkdir()
                source = root / "source.md"
                source.write_text(f"# Failure fixture\n\n{body}\n", encoding="utf-8")
                output = vault / "论文" / "Failure.md"
                workflow = vault / ".workflow"
                with self.assertRaises(ValueError):
                    build(
                        build_args(
                            input_markdown=str(source),
                            vault_root=str(vault),
                            output_note=str(output),
                            workflow_dir=str(workflow),
                            translation_stage="prepare",
                        )
                    )
                self.assertFalse((workflow / "translation-assignment.json").exists())
                self.assertFalse((workflow / "translation-packet.json").exists())

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "vault"
            vault.mkdir()
            source = root / "source.md"
            broken = root / "broken.png"
            broken.write_bytes(b"not a decodable image")
            source.write_text("# Broken image\n\n![broken](broken.png)\n", encoding="utf-8")
            workflow = vault / ".workflow"
            with self.assertRaises(ValueError):
                build(
                    build_args(
                        input_markdown=str(source),
                        vault_root=str(vault),
                        output_note=str(vault / "论文" / "Broken.md"),
                        workflow_dir=str(workflow),
                        translation_stage="prepare",
                    )
                )
            self.assertFalse((workflow / "translation-assignment.json").exists())

    def test_concept_link_failures_are_nonblocking_and_off_does_not_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            note = root / "paper.md"
            note.write_text("---\ntitle: T\naliases:\n  - T\n---\n# T\n\n中文。\n", encoding="utf-8")
            with mock.patch("build_obsidian_paper_card.run_concept_links", side_effect=RuntimeError("boom")) as runner:
                off = run_optional_concept_links(note, root, "off")
                self.assertEqual(off["status"], "disabled")
                runner.assert_not_called()
                report = run_optional_concept_links(note, root, "report")
                self.assertEqual(report["status"], "failed")
                self.assertFalse(report["blocking"])
                before = note.read_bytes()
                write = run_optional_concept_links(note, root, "write")
                self.assertEqual(write["status"], "failed")
                self.assertFalse(write["blocking"])
                self.assertEqual(note.read_bytes(), before)

    def test_prepare_layout_finalize_integration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "vault"
            vault.mkdir()
            source = root / "source.md"
            output = vault / "论文" / "Fixture.md"
            workflow = vault / ".workflow"
            data_uri = "data:image/png;base64," + base64.b64encode(PNG_1X1).decode("ascii")
            source.write_text(
                "---\n"
                'title: "Fixture"\n'
                "aliases: []\n"
                "custom: keep-me\n"
                "---\n"
                "# Fixture\n\n"
                "## Abstract\n\n"
                "As shown in Fig. 1.\n\n"
                "This cafÃ© method uses $x_t$ and citation[^1]. It keeps note[^note].\n\n"
                '<table><tr><th rowspan="2">Method</th><th colspan="2">Score</th></tr>'
                '<tr><th>A</th><th>B</th></tr><tr><td>Ours</td><td>1</td><td>2</td></tr></table>\n\n'
                f"![pixel]({data_uri})\n\n"
                "Figure 1: Pixel.\n\n"
                "## References\n\n"
                '[^1]: A. Author. "Paper". Journal, 2024. https://doi.org/10.1/example\n\n'
                "[^note]: An explanatory Markdown footnote.\n",
                encoding="utf-8",
            )

            prepare = build(
                build_args(
                    input_markdown=str(source),
                    vault_root=str(vault),
                    output_note=str(output),
                    workflow_dir=str(workflow),
                    translation_stage="prepare",
                )
            )
            self.assertTrue(prepare["ready_for_translation"])
            for key in (
                "citation_residual",
                "remote_images",
                "html_tables",
                "mojibake",
                "invalid_image_paths",
                "frontmatter_errors",
                "protected_token_errors",
            ):
                self.assertEqual(prepare[key], 0, key)
            self.assertLess(len(json.dumps(compact_cli_report(prepare), ensure_ascii=False).encode("utf-8")), 4096)

            assignment = json.loads((workflow / "translation-assignment.json").read_text(encoding="utf-8"))
            self.assertEqual(
                set(assignment),
                {
                    "schema_version",
                    "packet_path",
                    "output_path",
                    "unit_count",
                    "packet_sha256",
                    "prompt_sha256",
                    "translator_fingerprint",
                    "constraints_version",
                    "requested_runtime",
                },
            )
            self.assertTrue(str(assignment["packet_path"]).endswith(".jsonl"))

            prepared_path = output.with_name(f".{output.name}.translation-prepared.md")
            frozen_before = prepared_path.read_bytes()
            layout = build(
                build_args(
                    input_markdown=str(source),
                    vault_root=str(vault),
                    output_note=str(output),
                    workflow_dir=str(workflow),
                    translation_stage="layout",
                )
            )
            self.assertTrue(layout["ok"])
            self.assertEqual(prepared_path.read_bytes(), frozen_before)

            prepared_path.write_text(prepared_path.read_text(encoding="utf-8") + "\nModified after freeze.\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                build(
                    build_args(
                        input_markdown=str(source),
                        vault_root=str(vault),
                        output_note=str(output),
                        workflow_dir=str(workflow),
                        translation_stage="finalize",
                    )
                )
            prepared_path.write_bytes(frozen_before)

            packet = read_packet(workflow / "translation-packet.json")
            self.assertEqual(packet["schema_version"], 5)
            self.assertEqual(packet["reference_blocks"], 1)
            self.assertFalse(any(unit.get("kind") == "reference" for unit in packet["units"]))
            translation_output = workflow / "translation-output.jsonl"
            def fake_translation(unit: dict[str, object]) -> dict[str, str]:
                english = str(unit["english"])
                if unit.get("kind") == "caption" and english.startswith("Figure 1"):
                    return {
                        "unit_id": str(unit["unit_id"]),
                        "zh": "\u56fe 1\uff1a\u50cf\u7d20\u3002",
                    }
                if "Fig. 1" in english:
                    return {
                        "unit_id": str(unit["unit_id"]),
                        "zh": "\u5982\u56fe 1\u6240\u793a\u3002" + english,
                    }
                if unit.get("kind") == "reference_title":
                    return {
                        "unit_id": str(unit["unit_id"]),
                        "source_title": str(unit.get("title_hint") or english),
                        "zh": "测试参考文献题名",
                    }
                return {"unit_id": str(unit["unit_id"]), "zh": "中文译文：" + english}

            translation_output.write_text(
                "".join(
                    json.dumps(
                        fake_translation(unit),
                        ensure_ascii=False,
                    )
                    + "\n"
                    for unit in packet["units"]
                ),
                encoding="utf-8",
            )
            translations, errors = validate_output(packet, translation_output)
            self.assertFalse(errors)
            self.assertEqual(len(translations), len(packet["units"]))

            attestation_path = workflow / "translation-worker-attempt-1-attestation.json"
            attestation_path.write_text(
                json.dumps(
                    {
                        "actual": {
                            "model": "gpt-5.6-terra",
                            "reasoning_effort": "high",
                            "runtime_verified": True,
                        },
                        "output_sha256": hashlib.sha256(translation_output.read_bytes()).hexdigest(),
                        "success": True,
                    }
                ),
                encoding="utf-8",
            )
            state_path = workflow / "workflow-state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["translator"]["actual"] = {
                "model": "gpt-5.6-terra",
                "reasoning_effort": "high",
                "runtime_verified": True,
            }
            state.setdefault("artifacts", {})["translation_runtime_attestation"] = attestation_path.name
            state["translation_runtime_attestation_sha256"] = hashlib.sha256(attestation_path.read_bytes()).hexdigest()
            state_path.write_text(json.dumps(state), encoding="utf-8")

            final = build(
                build_args(
                    input_markdown=str(source),
                    vault_root=str(vault),
                    output_note=str(output),
                    workflow_dir=str(workflow),
                    translation_stage="finalize",
                    translation_output=str(translation_output),
                )
            )
            self.assertTrue(final["ok"])
            final_text = output.read_text(encoding="utf-8")
            self.assertIn("> This café method", final_text)
            self.assertIn("\n\n中文译文：This café method", final_text)
            self.assertNotIn("{{ZH:", final_text)
            self.assertNotIn("<table", final_text)
            self.assertNotIn("data:image", final_text)
            self.assertRegex(final_text, r'(?m)^\[1\] A\. Author\. "Paper"\. Journal, 2024\..*\^ref-1$')
            english_figure_link = re.search(r"\[Fig\. 1\]\(([^)]+)\)", final_text)
            chinese_figure_link = re.search(r"\[\u56fe 1\]\(([^)]+)\)", final_text)
            self.assertIsNotNone(english_figure_link)
            self.assertIsNotNone(chinese_figure_link)
            self.assertEqual(english_figure_link.group(1), chinese_figure_link.group(1))
            self.assertIn("_resources/", english_figure_link.group(1))
            self.assertEqual(final["figure_targets"], 1)
            self.assertGreaterEqual(final["figure_links_written"], 2)
            self.assertNotRegex(final_text, r"(?m)^> \[1\]")
            self.assertIn("custom: keep-me", final_text)
            self.assertIn("aliases:\n  - \"Fixture\"", final_text)
            self.assertIn("cssclasses:\n  - paper-card-centered-images", final_text)
            validation = validate_full(
                output,
                vault,
                stage="final",
                workflow_state_path=workflow / "workflow-state.json",
                validation_mode="posthoc_audit",
            )
            self.assertTrue(validation["ok"], validation["errors"])
            self.assertFalse(validation["provenance_verified"])
            self.assertTrue(any("provenance" in warning for warning in validation["warnings"]))
            self.assertFalse((workflow / "translation-packet.json").exists())
            self.assertFalse((workflow / "translation-assignment.json").exists())
            self.assertFalse(prepared_path.exists())
            self.assertTrue((workflow / "translation-cache.jsonl").is_file())
            self.assertTrue(attestation_path.is_file())
            retained_state = json.loads((workflow / "workflow-state.json").read_text(encoding="utf-8"))
            self.assertEqual(retained_state["retention"]["policy"], "completed_compact_v1")


if __name__ == "__main__":
    unittest.main()
