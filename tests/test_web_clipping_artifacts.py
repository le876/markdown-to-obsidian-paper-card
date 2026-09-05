from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from normalize_web_clipping_artifacts import normalize_web_clipping_artifacts  # noqa: E402
from validate_full_paper_card import validate_full  # noqa: E402


class WebClippingArtifactTests(unittest.TestCase):
    def test_caption_math_fallback_duplicates_are_restored(self) -> None:
        source = (
            "> Figure 1: Accuracy 3 mm 3\\\\,\\\\mathrm{mm}; "
            "over 20, 000 20{,}000 hours; 98 % 98\\\\%; "
            "about ≈ 96 {\\\\approx}96\\\\%; "
            "fit α = 0.268 \\\\alpha=0.268 and R 0.993 R^{2}=0.993.\n"
            "图 2：宽度 4 mm 4\\\\,\\\\mathrm{mm}；时刻 "
            "t 1 t\\_{1} – 6 t\\_{6}。\n"
            "> Figure 10: OpenPI- π 0.5 \\\\pi\\_{0.5}; horizon H = 12 H=12; "
            "increments t 2, … t=2,\\\\ldots,12; plain 7 mm and 95%.\n"
        )

        normalized, report = normalize_web_clipping_artifacts(source)
        self.assertIn("$7\\,\\mathrm{mm}$", normalized)
        self.assertIn("$20{,}000$", normalized)
        self.assertIn("$98\\%$", normalized)
        self.assertIn("${\\approx}96\\%$", normalized)
        self.assertIn("$\\alpha=0.268$", normalized)
        self.assertIn("$R^{2}=0.993$", normalized)
        self.assertIn("$4\\,\\mathrm{mm}$", normalized)
        self.assertIn("$t_{1}$–$t_{6}$", normalized)
        self.assertIn("$\\pi_{0.5}$", normalized)
        self.assertIn("$H=12$", normalized)
        self.assertIn("$t=2,\\ldots,12$", normalized)
        self.assertIn("$3\\,\\mathrm{mm}$", normalized)
        self.assertIn("$95\\%$", normalized)
        self.assertEqual(report["residual_caption_math_artifacts"], 0)
        self.assertGreaterEqual(report["caption_math_artifacts_fixed"], 11)

        second, second_report = normalize_web_clipping_artifacts(normalized)
        self.assertEqual(second, normalized)
        self.assertEqual(second_report["caption_math_artifacts_fixed"], 0)

    def test_body_prose_is_not_rewritten(self) -> None:
        source = "Body text contains H = 12 H=12 and Fig.˜1.\n"
        normalized, report = normalize_web_clipping_artifacts(source)
        self.assertEqual(normalized, source)
        self.assertEqual(report["caption_math_artifacts_fixed"], 0)

    def test_residual_caption_tex_is_a_nonblocking_warning(self) -> None:
        source = (
            "---\ntitle: T\naliases:\n  - T\n"
            "bilingual_layout: english_blockquote_chinese_body\n---\n"
            "# T\n\n## Abstract\n\nBody text for translation.\n\n"
            "Figure 1: Condition C motion C\\_{\\\\mathrm{motion}}.\n"
        )
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            note = vault / "prepared.md"
            note.write_text(source, encoding="utf-8")
            report = validate_full(note, vault, stage="prepared")
        self.assertTrue(report["ok"], report["errors"])
        self.assertTrue(report["ready_for_translation"], report)
        self.assertGreater(report["caption_math_artifact_residual"], 0)
        self.assertTrue(any("non-body" in warning for warning in report["warnings"]))


if __name__ == "__main__":
    unittest.main()
