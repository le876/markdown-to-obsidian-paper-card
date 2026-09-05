"""Clean-install CLI smoke tests. All inputs and parent identifiers are synthetic."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
try:
    import pymupdf
except ImportError:
    pymupdf = None


class ReleaseSmokeTests(unittest.TestCase):
    def cli(self, script: str, *args: object) -> dict:
        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        # Handoff preparation only: never used as evidence of a real model run.
        env["CODEX_THREAD_ID"] = "synthetic-release-smoke-parent"
        result = subprocess.run(
            [sys.executable, "-X", "utf8", str(ROOT / "scripts" / script), *map(str, args)],
            cwd=ROOT, env=env, capture_output=True, text=True,
            encoding="utf-8", errors="strict", timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_format_write_and_native_handoff_from_clean_paths(self):
        with tempfile.TemporaryDirectory(prefix="paper-card-smoke-") as temp:
            root = Path(temp)
            vault = root / "vault"
            vault.mkdir()
            source = root / "source.md"
            Image.new("RGB", (24, 12), "blue").save(root / "figure.png")
            source.write_text(
                "# Synthetic Controller\n\n## Abstract\n\n"
                "The controller minimizes $E(x)$ [1]. See Figure 1.\n\n"
                "![Figure 1](figure.png)\n\nFigure 1: Synthetic result.\n\n"
                "## References\n\n[1] A. Author. Synthetic control study. 2026.\n",
                encoding="utf-8", newline="\n",
            )
            output = vault / "papers" / "Formatted.md"
            common = ["--input-markdown", source, "--vault-root", vault,
                      "--output-note", output, "--translation-mode", "none"]
            self.cli("build_obsidian_paper_card.py", *common)
            self.assertFalse(output.exists(), "dry run must not create the note")
            self.cli("build_obsidian_paper_card.py", *common, "--write")
            text = output.read_text(encoding="utf-8")
            self.assertIn("$E(x)$", text)
            self.assertIn("^ref-1", text)
            self.assertTrue(list((vault / "_resources").rglob("*.png")))
            self.assertFalse(output.read_bytes().startswith(b"\xef\xbb\xbf"))

            role = vault / ".codex" / "agents" / "paper-translation-worker.toml"
            self.cli("sync_paper_translation_agent_prompt.py", "--agent-path", role, "--write")
            self.cli("sync_paper_translation_agent_prompt.py", "--agent-path", role, "--check")
            bilingual = vault / "papers" / "Bilingual.md"
            report = self.cli(
                "build_obsidian_paper_card.py", "--input-markdown", source,
                "--vault-root", vault, "--output-note", bilingual,
                "--translation-mode", "bilingual", "--translation-stage", "run",
                "--workflow-dir", root / "workflow", "--write",
            )
            self.assertEqual(report["status"], "awaiting_native_subagent")
            self.assertFalse(bilingual.exists(), "handoff is not completed translation")

    @unittest.skipIf(pymupdf is None, "optional PyMuPDF is not installed")
    def test_real_pdf_crop_and_verified_cache(self):
        with tempfile.TemporaryDirectory(prefix="paper-card-pdf-") as temp:
            root = Path(temp)
            layout = root / "layout"
            layout.mkdir()
            pdf = root / "source.pdf"
            with pymupdf.open() as document:
                page = document.new_page(width=1000, height=1000)
                page.draw_rect(pymupdf.Rect(100, 100, 450, 450), fill=(0, 0, 1))
                page.draw_rect(pymupdf.Rect(500, 100, 900, 450), fill=(1, 0, 0))
                document.save(pdf)
            (layout / "paper_content_list.json").write_text(json.dumps([
                {"type": "image", "img_path": "images/a.png", "bbox": [100, 100, 450, 450], "page_idx": 0},
                {"type": "image", "img_path": "images/b.png", "bbox": [500, 100, 900, 450], "page_idx": 0},
            ]), encoding="utf-8", newline="\n")
            asset_map = root / "asset-map.json"
            asset_map.write_text(json.dumps({"paper-a.png": "a.png", "paper-b.png": "b.png"}), encoding="utf-8", newline="\n")
            note = root / "paper.md"
            source = "![part a](paper-a.png)\n\n![part b](paper-b.png)\n\n> Fig. 1: Combined figure.\n"
            note.write_text(source, encoding="utf-8", newline="\n")
            args = ["--source-pdf", pdf, "--layout-dir", layout, "--asset-map", asset_map,
                    "--markdown-path", note, "--resource-root", root / "resources",
                    "--asset-prefix", "synthetic", "--cache-path", root / "cache.json"]
            first = self.cli("postprocess_mineru_figure_crops.py", *args)
            self.assertEqual(first["cache_misses"], 1)
            image = Path(first["generated"][0]["output"])
            with Image.open(image) as crop:
                crop.load()
                self.assertGreater(crop.width, 100)
            note.write_text(source, encoding="utf-8", newline="\n")
            second = self.cli("postprocess_mineru_figure_crops.py", *args)
            self.assertEqual(second["cache_hits"], 1)


if __name__ == "__main__":
    unittest.main()
