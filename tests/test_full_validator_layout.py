from __future__ import annotations

import base64
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from validate_full_paper_card import unit_requires_cjk, validate_full  # noqa: E402
from validate_obsidian_paper_note import validate_heading_levels, validate_text  # noqa: E402


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class BilingualLayoutValidationTests(unittest.TestCase):
    def test_abstract_heading_must_be_h2(self) -> None:
        self.assertEqual(validate_heading_levels("# Paper\n\n## Abstract\n"), [])
        errors = validate_heading_levels("# Paper\n\n###### Abstract\n")
        self.assertIn("Abstract heading must be H2, found H6", errors)

    def test_body_prose_requires_chinese(self) -> None:
        self.assertTrue(unit_requires_cjk({"kind": "paragraph", "english": "A claim."}))

    def test_figure_panel_label_does_not_require_chinese(self) -> None:
        self.assertFalse(unit_requires_cjk({"kind": "caption", "english": "(a)"}))

    def test_reference_does_not_require_chinese_characters(self) -> None:
        self.assertFalse(
            unit_requires_cjk(
                {"kind": "reference", "english": "[66] interbotix_ros_manipulators. URL https://example.com"}
            )
        )

    def test_contributor_passthrough_uses_explicit_packet_contract(self) -> None:
        self.assertFalse(
            unit_requires_cjk(
                {
                    "kind": "passthrough",
                    "english": "Alice Smith, Bob Jones",
                    "requires_chinese": False,
                }
            )
        )

    def test_posthoc_audit_warns_without_packet_while_pipeline_finalize_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            note = vault / "论文" / "Audit.md"
            note.parent.mkdir()
            note.write_text(
                "---\ntitle: Audit\naliases: []\nbilingual_layout: english_blockquote_chinese_body\n---\n"
                "# Audit\n\n###### Abstract\n\n> English.\n\n中文。\n",
                encoding="utf-8",
                newline="\n",
            )
            posthoc = validate_full(
                note,
                vault,
                stage="final",
                validation_mode="posthoc_audit",
            )
            self.assertTrue(posthoc["ok"], posthoc["errors"])
            self.assertFalse(posthoc["provenance_verified"])
            self.assertTrue(any("aliases is empty" in item for item in posthoc["warnings"]))
            self.assertTrue(any("Abstract heading" in item for item in posthoc["warnings"]))
            self.assertTrue(any("provenance" in item for item in posthoc["warnings"]))

            pipeline = validate_full(
                note,
                vault,
                stage="final",
                validation_mode="pipeline_finalize",
            )
            self.assertFalse(pipeline["ok"])
            self.assertTrue(any("frozen translation packet" in item for item in pipeline["errors"]))

    def test_nested_resources_and_attachment_roots_are_stable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            note = vault / "论文" / "Assets.md"
            attachment = vault / "_附件" / "Assets" / "figure.png"
            note.parent.mkdir()
            attachment.parent.mkdir(parents=True)
            attachment.write_bytes(PNG_1X1)
            text = (
                "---\ntitle: Assets\naliases:\n  - Assets\n---\n# Assets\n\n"
                "![figure](../_附件/Assets/figure.png)\n"
            )
            note.write_text(text, encoding="utf-8", newline="\n")
            report = validate_text(note, vault, text, require_figure_links=False)
            self.assertTrue(report["ok"], report["errors"])


if __name__ == "__main__":
    unittest.main()
