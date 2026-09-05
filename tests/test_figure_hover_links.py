from __future__ import annotations

import argparse
import base64
import sys
import re
import tempfile
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from build_obsidian_paper_card import build  # noqa: E402
from normalize_obsidian_figure_links import (  # noqa: E402
    migrate,
    normalize_figure_links,
    strip_figure_link_markup,
    validate_figure_links,
)
from paper_translation_packet import build_packet, merge_translations  # noqa: E402
from validate_full_paper_card import validate_bilingual_layout  # noqa: E402
from validate_obsidian_paper_note import validate_text  # noqa: E402


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class FigureHoverLinkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.vault = Path(self.temporary.name) / "vault"
        self.paper_dir = self.vault / "papers"
        self.resources = self.vault / "_resources"
        self.paper_dir.mkdir(parents=True)
        self.resources.mkdir()
        self.note = self.paper_dir / "paper.md"

    def image(self, name: str) -> Path:
        path = self.resources / name
        path.write_bytes(PNG_1X1)
        return path

    def test_single_target_bilingual_subpanels_and_idempotence(self) -> None:
        self.image("figure 4.png")
        source = (
            "![setup](../_resources/figure%204.png)\n\n"
            "Figure 4: Task setup.\n\n"
            "As shown in Fig. 4(a), the robot moves.\n\n"
            "\u5982\u56fe 4\uff08a\uff09\u6240\u793a\uff0c\u673a\u5668\u4eba\u8fd0\u52a8\u3002\n"
        )

        updated, report = normalize_figure_links(source, self.note, self.vault)

        target = "../_resources/figure%204.png"
        self.assertIn(f"[Fig. 4(a)]({target})", updated)
        self.assertIn(f"[\u56fe 4\uff08a\uff09]({target})", updated)
        self.assertIn("Figure 4: Task setup.", updated)
        self.assertNotIn("[Figure 4](", updated)
        self.assertEqual(report["figure_targets"], 1)
        self.assertEqual(report["figure_links_written"], 2)

        second, second_report = normalize_figure_links(updated, self.note, self.vault)
        self.assertEqual(second.encode("utf-8"), updated.encode("utf-8"))
        self.assertEqual(second_report["figure_links_written"], 0)
        validation = validate_figure_links(second, self.note, self.vault)
        self.assertTrue(validation["ok"], validation["errors"])
        self.assertEqual(validation["figure_links_written"], 2)

    def test_latexml_tilde_spacing_is_normalized_linked_and_validated(self) -> None:
        self.image("f1.png")
        source = (
            "![one](../_resources/f1.png)\n\n"
            "Figure 1: Overview.\n\n"
            "See Fig.˜1 and Fig.~1.\n\n"
            "参见图 ˜1 和图~1。\n"
        )

        before = validate_figure_links(source, self.note, self.vault)
        self.assertTrue(before["ok"])
        self.assertEqual(before["malformed_figure_reference_artifacts"], 4)
        self.assertTrue(any("spacing artifacts" in warning for warning in before["warnings"]))

        updated, report = normalize_figure_links(source, self.note, self.vault)
        self.assertEqual(report["normalized_figure_artifacts"], 4)
        self.assertEqual(report["figure_links_written"], 4)
        self.assertNotIn("˜", updated)
        self.assertNotIn("Fig.~", updated)
        self.assertEqual(updated.count("[Fig. 1](../_resources/f1.png)"), 2)
        self.assertEqual(updated.count("[图 1](../_resources/f1.png)"), 2)

        after = validate_figure_links(updated, self.note, self.vault)
        self.assertTrue(after["ok"], after["errors"])
        second, second_report = normalize_figure_links(updated, self.note, self.vault)
        self.assertEqual(second, updated)
        self.assertEqual(second_report["normalized_figure_artifacts"], 0)

    def test_unresolved_tilde_reference_is_still_normalized_and_reported(self) -> None:
        source = "See Fig.˜8.\n图 ˜8 尚无目标。\n"
        updated, report = normalize_figure_links(source, self.note, self.vault)
        self.assertEqual(updated, "See Fig. 8.\n图 8 尚无目标。\n")
        self.assertEqual(report["normalized_figure_artifacts"], 2)
        self.assertEqual(report["unmatched_figure_mentions"], 2)

    def test_orphan_web_figure_fragments_are_removed_before_finalize(self) -> None:
        source = (
            "---\ntitle: T\naliases:\n  - T\n---\n# T\n\n"
            "As shown in [Figure 5](#fig-5), the error decreases.\n"
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            markdown = root / "prepared.md"
            markdown.write_text(source, encoding="utf-8")
            packet = build_packet(markdown, root / "packet.json")
            unit = packet["units"][0]
            merged = merge_translations(
                markdown,
                packet,
                {unit["unit_id"]: {"zh": "如[图 5](#fig-5)所示，误差下降。"}},
            )
        updated, report = normalize_figure_links(merged, self.note, self.vault)
        self.assertEqual(report["orphan_fragment_links_removed"], 2)
        self.assertNotIn("#fig-5", updated)
        figure_validation = validate_figure_links(updated, self.note, self.vault)
        self.assertTrue(figure_validation["ok"], figure_validation["errors"])
        layout_errors = validate_bilingual_layout(strip_figure_link_markup(updated), packet)
        self.assertEqual(layout_errors, [])

    def test_existing_web_figure_fragment_anchor_is_preserved(self) -> None:
        source = '<a id="fig-5"></a>\n\nSee [Figure 5](#fig-5).\n'
        updated, report = normalize_figure_links(source, self.note, self.vault)
        self.assertEqual(updated, source)
        self.assertEqual(report["orphan_fragment_links_removed"], 0)
        validation = validate_figure_links(updated, self.note, self.vault)
        self.assertTrue(validation["ok"], validation["errors"])

    def test_plural_supplement_and_ranges(self) -> None:
        for name in ("f2.png", "f3.png", "s1.png"):
            self.image(name)
        source = (
            "![f2](../_resources/f2.png)\nFigure 2: Two.\n\n"
            "![f3](../_resources/f3.png)\nFig. 3: Three.\n\n"
            "![s1](../_resources/s1.png)\nFig. S1: Supplement.\n\n"
            "Figs. 2 and 3 compare with Fig. S1.\n"
            "\u56fe 2\u3001\u56fe 3 \u4e0e\u56fe S1\u76f8\u6bd4\u3002\n"
            "Figures 2\u20134 and \u56fe 2\u20134 remain ranges.\n"
        )

        updated, report = normalize_figure_links(source, self.note, self.vault)

        self.assertIn("Figs. [2](../_resources/f2.png) and [3](../_resources/f3.png)", updated)
        self.assertIn("[Fig. S1](../_resources/s1.png)", updated)
        self.assertIn("[\u56fe 2](../_resources/f2.png)", updated)
        self.assertIn("[\u56fe 3](../_resources/f3.png)", updated)
        self.assertIn("[\u56fe S1](../_resources/s1.png)", updated)
        self.assertIn("Figures 2\u20134", updated)
        self.assertIn("\u56fe 2\u20134", updated)
        self.assertEqual(report["numeric_ranges"], 2)
        self.assertEqual(report["figure_links_written"], 6)

    def test_protected_regions_and_reference_links_are_ignored(self) -> None:
        self.image("f4.png")
        self.image("alt.png")
        source = (
            "---\nlabel: Fig. 4\n---\n"
            "# Fig. 4 heading\n\n"
            "![f4](../_resources/f4.png)\nFigure 4: Caption Fig. 4.\n\n"
            "`Fig. 4` and $Fig. 4$ and ![Fig. 4](../_resources/alt.png).\n"
            "```text\nFig. 4\n```\n"
            "$$\nFig. 4\n$$\n"
            "| Item | Value |\n|---|---|\n| Fig. 4 | x |\n"
            "%%\nFig. 4\n%%\n"
            "Body Fig. 4.\n\n"
            "## References\n\n"
            "[Figure 4](https://example.com) is a paper title.\n"
        )

        updated, report = normalize_figure_links(source, self.note, self.vault)

        self.assertEqual(updated.count("[Fig. 4](../_resources/f4.png)"), 1)
        self.assertIn("label: Fig. 4", updated)
        self.assertIn("# Fig. 4 heading", updated)
        self.assertIn("Figure 4: Caption Fig. 4.", updated)
        self.assertIn("[Figure 4](https://example.com)", updated)
        self.assertEqual(report["figure_links_written"], 1)
        validation = validate_figure_links(updated, self.note, self.vault)
        self.assertTrue(validation["ok"], validation["errors"])
        self.assertEqual(validation["figure_links_written"], 1)

    def test_multi_asset_target_is_ambiguous_and_not_guessed(self) -> None:
        self.image("part-a.png")
        self.image("part-b.png")
        source = (
            "![a](../_resources/part-a.png)\n"
            "![b](../_resources/part-b.png)\n"
            "Figure 6: Multi-panel figure.\n\n"
            "See Fig. 6 for details.\n"
        )

        updated, report = normalize_figure_links(source, self.note, self.vault)

        self.assertEqual(updated, source)
        self.assertNotIn("[Fig. 6]", updated)
        self.assertEqual(report["ambiguous_figure_targets"], 1)
        self.assertEqual(report["unmatched_figure_mentions"], 1)
        validation = validate_figure_links(updated, self.note, self.vault)
        self.assertTrue(validation["ok"], validation["errors"])
        self.assertTrue(any("ambiguous" in warning for warning in validation["warnings"]))

    def test_pdf_crop_is_the_only_link_target(self) -> None:
        self.image("paper-fig7-pdf-crop.png")
        source = (
            "![complete crop](../_resources/paper-fig7-pdf-crop.png)\n"
            "Figure 7: Recovered from the PDF.\n\n"
            "See Fig. 7.\n"
        )
        updated, report = normalize_figure_links(source, self.note, self.vault)
        self.assertIn("[Fig. 7](../_resources/paper-fig7-pdf-crop.png)", updated)
        self.assertEqual(report["figure_targets"], 1)

    def test_validator_blocks_missing_wrong_outside_and_non_image_targets(self) -> None:
        self.image("expected.png")
        self.image("wrong.png")
        (self.resources / "not-image.txt").write_text("x", encoding="utf-8")
        prefix = "![expected](../_resources/expected.png)\nFigure 8: Expected.\n\n"
        cases = {
            "missing": "[Fig. 8](../_resources/missing.png)",
            "wrong_figure_target": "[Fig. 8](../_resources/wrong.png)",
            "outside_vault": "[Fig. 8](../../../outside.png)",
            "not_image": "[Fig. 8](../_resources/not-image.txt)",
        }
        for reason, link in cases.items():
            with self.subTest(reason=reason):
                validation = validate_figure_links(prefix + link + "\n", self.note, self.vault)
                self.assertFalse(validation["ok"])
                self.assertTrue(any(reason in error for error in validation["errors"]))

    def test_validator_requires_simple_links_only_for_final_stage(self) -> None:
        self.image("f9.png")
        source = (
            "---\ntitle: Nine\naliases: []\n---\n# Nine\n\n"
            "![nine](../_resources/f9.png)\nFigure 9: Nine.\n\n"
            "See Fig. 9.\n"
        )
        self.note.write_text(source, encoding="utf-8")
        prepared = validate_text(self.note, self.vault, source, require_figure_links=False)
        final = validate_text(self.note, self.vault, source, require_figure_links=True)
        marker = "eligible figure mentions remain unlinked"
        self.assertFalse(any(marker in error for error in prepared["errors"]))
        self.assertTrue(final["ok"])
        self.assertTrue(any(marker in warning for warning in final["warnings"]))

    def test_translation_none_builder_links_after_resource_archiving(self) -> None:
        source_dir = Path(self.temporary.name) / "source"
        source_dir.mkdir()
        (source_dir / "source-figure.png").write_bytes(PNG_1X1)
        source = source_dir / "source.md"
        source.write_text(
            "---\ntitle: None mode\naliases: []\n---\n"
            "# None mode\n\n"
            "![eleven](source-figure.png)\n"
            "Figure 11: Archived image.\n\n"
            "See Fig. 11.\n\n"
            "\u5982\u56fe 11\u6240\u793a\u3002\n",
            encoding="utf-8",
        )
        report = build(
            argparse.Namespace(
                input_markdown=str(source),
                vault_root=str(self.vault),
                output_note=str(self.note),
                translation_mode="none",
                concept_links="off",
                source_package=None,
                translation_stage="run",
                workflow_dir=None,
                translation_output=None,
                translator_fingerprint=None,
                in_place=False,
                write=True,
            )
        )

        self.assertTrue(report["ok"], report)
        final_text = self.note.read_text(encoding="utf-8")
        english = re.search(r"\[Fig\. 11\]\(([^)]+)\)", final_text)
        chinese = re.search(r"\[\u56fe 11\]\(([^)]+)\)", final_text)
        self.assertIsNotNone(english)
        self.assertIsNotNone(chinese)
        self.assertEqual(english.group(1), chinese.group(1))
        self.assertIn("_resources/", english.group(1))
        self.assertEqual(report["figure_targets"], 1)
        self.assertEqual(report["figure_links_written"], 2)

    def test_legacy_wikilink_migration_dry_run_write_backup_and_idempotence(self) -> None:
        legacy = self.resources / "legacy.png"
        legacy.write_bytes(PNG_1X1)
        source = (
            "---\ntitle: Legacy\naliases: []\n---\n# Legacy\n\n"
            "![[legacy.png]]\n"
            "Figure 10: Legacy.\n\n"
            "\u5982\u56fe 10\u6240\u793a\u3002\n"
        )
        self.note.write_text(source, encoding="utf-8")
        base_args = {
            "vault_root": str(self.vault),
            "markdown_path": [str(self.note)],
            "directory": None,
            "recursive": False,
        }

        dry = migrate(argparse.Namespace(**base_args, write=False))
        self.assertEqual(dry["notes_changed"], 1)
        self.assertEqual(dry["notes_written"], 0)
        self.assertEqual(self.note.read_text(encoding="utf-8"), source)

        written = migrate(argparse.Namespace(**base_args, write=True))
        self.assertEqual(written["notes_written"], 1)
        migrated = self.note.read_text(encoding="utf-8")
        self.assertIn("[[legacy.png|\u56fe 10]]", migrated)
        backups = list(self.paper_dir.glob("paper.md.bak-*-before-figure-links"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), source)

        second = migrate(argparse.Namespace(**base_args, write=True))
        self.assertEqual(second["notes_changed"], 0)
        self.assertEqual(second["notes_written"], 0)
        self.assertEqual(len(list(self.paper_dir.glob("paper.md.bak-*-before-figure-links"))), 1)


if __name__ == "__main__":
    unittest.main()
