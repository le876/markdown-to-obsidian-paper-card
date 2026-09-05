from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from paper_translation_packet import (
    build_merge_template,
    build_packet,
    canonical_reference_source_section,
    fill_merge_template,
    infer_reference_title,
    sha256_text,
    validate_output,
)


SYNTHETIC_REFERENCE = (
    "Alice Smith, Béla Example, Carol Green, and David Brown. "
    "Test-dex: Studying dexterity with synthetic mixed reality. "
    "arXiv preprint arXiv:2210.06463, 2022."
)


class ReferenceTitleV4Tests(unittest.TestCase):
    def prepared(self, root: Path, reference: str = SYNTHETIC_REFERENCE) -> Path:
        path = root / "prepared.md"
        path.write_text(
            "---\ntitle: T\naliases:\n  - T\nbilingual_layout: english_blockquote_chinese_body\n---\n"
            "# T\n\nBody claim [[#^ref-6|⁶]].\n\n## References\n\n"
            f"[6] {reference} ^ref-6\n",
            encoding="utf-8",
        )
        return path

    def test_synthetic_example_title_is_extracted_deterministically(self) -> None:
        title, mode = infer_reference_title(SYNTHETIC_REFERENCE)
        self.assertEqual(title, "Test-dex: Studying dexterity with synthetic mixed reality")
        self.assertEqual(mode, "deterministic")

    def test_ambiguous_url_reference_falls_back_to_worker(self) -> None:
        title, mode = infer_reference_title("Interbotix ROS manipulators. https://example.com")
        self.assertIsNone(title)
        self.assertEqual(mode, "worker")

    def test_arxiv_metadata_is_not_misidentified_as_the_title(self) -> None:
        entry = (
            "Alice De Example, Bob Jones, Carol Green, and David Brown. "
            "Synthetic diffusion bridges for testing reference extraction. "
            "(arXiv:2106.01357), Dec 2021. doi: 10.48550/arXiv.2106.01357. "
            "URL https://example.org/abs/2106.01357."
        )
        title, mode = infer_reference_title(entry)
        self.assertEqual(title, "Synthetic diffusion bridges for testing reference extraction")
        self.assertEqual(mode, "deterministic")

    def test_quoted_author_alias_is_not_misidentified_as_title(self) -> None:
        groot = (
            'Example Robotics, Alice Smith, Bob "Jim" Jones, and Carol Green. '
            "Model T1: A Synthetic Foundation Model for Testing, "
            "March 2025a. URL https://example.org/abs/2503.14734."
        )
        title, mode = infer_reference_title(groot)
        self.assertEqual(title, "Model T1: A Synthetic Foundation Model for Testing")
        self.assertEqual(mode, "deterministic")

        isaac = (
            'Example Robotics, David Brown, Bob "Jim" Jones, and Carol Green. '
            "Example Lab: A GPU-Accelerated Simulation Framework for Testing, "
            "November 2025b. URL https://example.org/abs/2511.04831."
        )
        title, mode = infer_reference_title(isaac)
        self.assertEqual(title, "Example Lab: A GPU-Accelerated Simulation Framework for Testing")
        self.assertEqual(mode, "deterministic")

    def test_software_only_reference_is_skipped_without_chinese_suffix(self) -> None:
        software = (
            "Alice B. C. Smith. torchdiffeq, 2018. "
            "URL https://example.org/torchdiffeq."
        )
        title, mode = infer_reference_title(software)
        self.assertIsNone(title)
        self.assertEqual(mode, "none")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prepared = self.prepared(root, software)
            packet = build_packet(prepared, root / "packet.json")
            self.assertEqual(packet["reference_title_units"], 0)
            self.assertEqual(packet["reference_title_skipped_units"], 1)
            self.assertFalse(any(unit.get("kind") == "reference_title" for unit in packet["units"]))
            template = build_merge_template(prepared, packet)
            self.assertNotIn("{{REFZH:", template)
            self.assertIn(software, template)

    def test_only_fallback_transport_contains_full_reference_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            deterministic = build_packet(self.prepared(root), root / "deterministic.json")
            deterministic_unit = next(unit for unit in deterministic["units"] if unit.get("kind") == "reference_title")
            from run_paper_translation_worker import build_transport_rows

            transported, _ = build_transport_rows([deterministic_unit])
            self.assertNotIn("reference_entry", transported[0])

            fallback_prepared = self.prepared(root, "Interbotix ROS manipulators. https://example.com/interbotix, 2023.")
            fallback = build_packet(fallback_prepared, root / "fallback.json")
            fallback_unit = next(unit for unit in fallback["units"] if unit.get("kind") == "reference_title")
            transported_fallback, _ = build_transport_rows([fallback_unit])
            self.assertEqual(transported_fallback[0]["type"], "reference_title")
            self.assertEqual(transported_fallback[0]["entry"], fallback_unit["reference_entry"])
            self.assertEqual(
                json.dumps(transported_fallback, ensure_ascii=False).count(fallback_unit["reference_entry"]),
                1,
            )

    def test_layout_renders_exact_inline_title_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prepared = self.prepared(root)
            packet = build_packet(prepared, root / "packet.json")
            template = build_merge_template(prepared, packet)
            self.assertIn("{{REFZH:r00006}} ^ref-6", template)
            translations = {
                "u00001": {"zh": "正文论断 [[#^ref-6|⁶]]。"},
                "r00006": {
                    "source_title": "Test-dex: Studying dexterity with synthetic mixed reality",
                    "zh": "利用合成混合现实研究灵巧操作",
                },
            }
            final = fill_merge_template(template, packet, translations)
            expected = (
                "[6] " + SYNTHETIC_REFERENCE + "《利用合成混合现实研究灵巧操作》 ^ref-6"
            )
            self.assertIn(expected, final)
            self.assertNotIn("\n《利用合成混合现实研究灵巧操作》", final)
            self.assertEqual(
                sha256_text(canonical_reference_source_section(final)),
                packet["reference_source_section_sha256"],
            )

    def test_translation_validation_rejects_non_substring_source_title(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prepared = self.prepared(root)
            packet = build_packet(prepared, root / "packet.json")
            output = root / "output.jsonl"
            rows = []
            for unit in packet["units"]:
                if unit.get("kind") == "reference_title":
                    rows.append({"unit_id": unit["unit_id"], "source_title": "Invented title", "zh": "虚构题名"})
                else:
                    rows.append({"unit_id": unit["unit_id"], "zh": "正文论断 [[#^ref-6|⁶]]。"})
            output.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
            _, errors = validate_output(packet, output)
            self.assertTrue(any("exact substring" in error for error in errors))

    def test_existing_title_suffix_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prepared = self.prepared(root, SYNTHETIC_REFERENCE + "《利用合成混合现实研究灵巧操作》")
            packet = build_packet(prepared, root / "packet.json")
            self.assertEqual(packet["reference_title_reused_units"], 1)
            self.assertFalse(any(unit.get("kind") == "reference_title" for unit in packet["units"]))
            template = build_merge_template(prepared, packet)
            self.assertEqual(template.count("《利用合成混合现实研究灵巧操作》"), 1)


if __name__ == "__main__":
    unittest.main()
