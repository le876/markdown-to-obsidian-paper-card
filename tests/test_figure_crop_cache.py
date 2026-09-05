from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image, ImageChops


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import postprocess_mineru_figure_crops as crop  # noqa: E402


SOURCE_MARKDOWN = (
    "![part a](paper-a.png)\n\n"
    "![part b](paper-b.png)\n\n"
    "> Fig. 1: Combined figure.\n"
)


class FigureCropCacheTests(unittest.TestCase):
    class DummyDocument:
        def close(self) -> None:
            return None

    class FakeFitz:
        @staticmethod
        def open(_path: object = None) -> "FigureCropCacheTests.DummyDocument":
            return FigureCropCacheTests.DummyDocument()

    def setUp(self) -> None:
        self.original_fitz = crop.fitz
        self.original_image = crop.Image
        self.original_image_chops = crop.ImageChops
        crop.fitz = self.FakeFitz
        crop.Image = Image
        crop.ImageChops = ImageChops

    def tearDown(self) -> None:
        crop.fitz = self.original_fitz
        crop.Image = self.original_image
        crop.ImageChops = self.original_image_chops

    @staticmethod
    def fake_crop_from_bbox(*, out_path: Path, **_kwargs: object) -> None:
        Image.new("RGB", (32, 16), (40, 90, 180)).save(out_path, format="PNG")

    def make_fixture(self, root: Path) -> tuple[argparse.Namespace, Path, Path]:
        layout = root / "layout"
        resources = root / "vault" / "_resources"
        layout.mkdir()
        resources.mkdir(parents=True)
        markdown = root / "paper.md"
        markdown.write_text(SOURCE_MARKDOWN, encoding="utf-8", newline="\n")
        (layout / "paper_content_list.json").write_text(
            json.dumps(
                [
                    {"type": "image", "img_path": "images/a.png", "bbox": [100, 100, 450, 450], "page_idx": 0},
                    {"type": "image", "img_path": "images/b.png", "bbox": [500, 100, 900, 450], "page_idx": 0},
                ]
            ),
            encoding="utf-8",
            newline="\n",
        )
        asset_map = root / "asset-map.json"
        asset_map.write_text(
            json.dumps({"paper-a.png": "a.png", "paper-b.png": "b.png"}),
            encoding="utf-8",
            newline="\n",
        )
        pdf_path = root / "source.pdf"
        pdf_path.write_bytes(b"%PDF-1.4\nfigure crop cache fixture\n")
        cache_path = root / "workflow" / "figure-crop-cache.json"
        args = argparse.Namespace(
            extract_dir=None,
            source_pdf=str(pdf_path),
            layout_dir=str(layout),
            asset_map=str(asset_map),
            cache_path=str(cache_path),
            markdown_path=str(markdown),
            resource_root=str(resources),
            asset_prefix="paper",
            render_scale=1.0,
            padding_points=0.0,
        )
        return args, markdown, resources / "paper-fig1-pdf-crop.png"

    def test_crop_cache_reuses_verified_output_and_invalidates_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            args, markdown, output = self.make_fixture(Path(temporary))

            with mock.patch.object(crop, "crop_from_bbox", side_effect=self.fake_crop_from_bbox):
                first = crop.process(args)
            self.assertEqual(first["cache_hits"], 0)
            self.assertEqual(first["cache_misses"], 1)
            self.assertEqual(first["generated"][0]["status"], "generated")
            self.assertTrue(output.is_file())
            original_bytes = output.read_bytes()
            cache = json.loads(Path(args.cache_path).read_text(encoding="utf-8"))
            self.assertEqual(cache["schema_version"], 1)
            self.assertEqual(len(cache["entries"]), 1)

            markdown.write_text(SOURCE_MARKDOWN, encoding="utf-8", newline="\n")
            with mock.patch.object(crop, "render_crop_atomically", side_effect=AssertionError("must not render")):
                second = crop.process(args)
            self.assertEqual(second["cache_hits"], 1)
            self.assertEqual(second["cache_misses"], 0)
            self.assertEqual(second["generated"][0]["status"], "cache_hit")

            output.write_bytes(b"tampered")
            markdown.write_text(SOURCE_MARKDOWN, encoding="utf-8", newline="\n")
            with mock.patch.object(crop, "crop_from_bbox", side_effect=self.fake_crop_from_bbox):
                third = crop.process(args)
            self.assertEqual(third["cache_hits"], 0)
            self.assertEqual(third["cache_misses"], 1)
            self.assertEqual(output.read_bytes(), original_bytes)

            markdown.write_text(SOURCE_MARKDOWN, encoding="utf-8", newline="\n")
            changed_args = argparse.Namespace(**{**vars(args), "render_scale": 1.5})
            with mock.patch.object(crop, "crop_from_bbox", side_effect=self.fake_crop_from_bbox):
                changed = crop.process(changed_args)
            self.assertEqual(changed["cache_hits"], 0)
            self.assertEqual(changed["cache_misses"], 1)
            changed_cache = json.loads(Path(args.cache_path).read_text(encoding="utf-8"))
            self.assertEqual(len(changed_cache["entries"]), 2)

    def test_failed_promotion_preserves_existing_crop_and_removes_temporary_png(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            args, _, output = self.make_fixture(Path(temporary))
            output.write_bytes(b"existing-crop")
            document = self.DummyDocument()
            with mock.patch.object(crop, "crop_from_bbox", side_effect=self.fake_crop_from_bbox):
                with mock.patch.object(crop, "atomic_write_bytes", side_effect=RuntimeError("promotion failed")):
                    with self.assertRaisesRegex(RuntimeError, "promotion failed"):
                        crop.render_crop_atomically(
                            pdf=document,
                            page_idx=0,
                            bboxes=[[100, 100, 900, 450]],
                            out_path=output,
                            render_scale=1.0,
                            padding_points=0.0,
                            normalized_canvas=True,
                        )

            self.assertEqual(output.read_bytes(), b"existing-crop")
            self.assertEqual(list(output.parent.glob(f".{output.name}.crop-*.png")), [])


if __name__ == "__main__":
    unittest.main()
