from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from sync_image_converter_alignments import (  # noqa: E402
    image_converter_hash,
    image_converter_runtime_path,
    synchronize,
)


class ImageConverterAlignmentTests(unittest.TestCase):
    def test_hash_matches_live_image_converter_1_4_4_cache_entry(self) -> None:
        note = "论文/Flow Matching for Generative Modeling.md"
        image = (
            "/_resources/"
            "flow-fc02f342f561902536cbf5b90176f0ccd872b394d373be2d841652e06f17afad.jpg"
        )
        self.assertEqual(
            image_converter_hash(f"{note}:{image}", 0),
            "f9912fbf5d5dfc2f6c5cc88d4229efce",
        )

    def test_sync_preserves_manual_entry_and_reports_float_without_applying_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            vault = Path(directory)
            obsidian = vault / ".obsidian"
            resources = vault / "_resources"
            papers = vault / "论文"
            layout = vault / "layout"
            for path in (obsidian, resources, papers, layout):
                path.mkdir(parents=True, exist_ok=True)
            (obsidian / "community-plugins.json").write_text(
                json.dumps(["image-converter"]), encoding="utf-8"
            )
            (resources / "a.png").write_bytes(b"a")
            (resources / "b.png").write_bytes(b"b")
            note = papers / "paper.md"
            note.write_text(
                "# Paper\n\n![](../_resources/a.png)\n\n![](../_resources/b.png)\n",
                encoding="utf-8",
            )
            (layout / "sample_content_list.json").write_text(
                json.dumps(
                    [
                        {
                            "type": "text",
                            "page_idx": 0,
                            "bbox": [0, 0, 48, 82],
                            "text": "overlapping body text",
                        },
                        {
                            "type": "image",
                            "page_idx": 0,
                            "bbox": [52, 0, 96, 82],
                            "img_path": "images/a.png",
                        },
                        {
                            "type": "image",
                            "page_idx": 1,
                            "bbox": [0, 0, 96, 82],
                            "img_path": "images/b.png",
                        },
                    ]
                ),
                encoding="utf-8",
            )

            note_key = "论文/paper.md"
            first_runtime_path = image_converter_runtime_path("_resources/a.png")
            first_hash = image_converter_hash(f"{note_key}:{first_runtime_path}", 0)
            cache_path = obsidian / "image-converter-image-alignments.json"
            manual = {
                "position": "right",
                "width": "320px",
                "height": "",
                "wrap": True,
            }
            cache_path.write_text(
                json.dumps({note_key: {first_hash: manual}}, ensure_ascii=False),
                encoding="utf-8",
            )

            dry_run = synchronize(
                vault_root=vault,
                markdown_path=note,
                layout_dir=layout,
                write=False,
            )
            self.assertTrue(dry_run["ok"])
            self.assertEqual(dry_run["images"], 2)
            self.assertEqual(dry_run["float_candidates"], 1)
            self.assertEqual(dry_run["entries_preserved"], 1)
            self.assertEqual(dry_run["entries_added"], 1)
            first = next(item for item in dry_run["items"] if item["image"].endswith("a.png"))
            self.assertEqual(first["layout_recommendation"]["position"], "right")
            self.assertEqual(first["existing"], manual)
            second = next(item for item in dry_run["items"] if item["image"].endswith("b.png"))
            self.assertEqual(second["applied"], {"position": "center", "wrap": False})

            written = synchronize(
                vault_root=vault,
                markdown_path=note,
                layout_dir=layout,
                write=True,
            )
            self.assertTrue(written["ok"])
            self.assertTrue(written["changed"])
            self.assertTrue(written["reload_required"])
            self.assertTrue(Path(written["backup_path"]).is_file())
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
            self.assertEqual(cache[note_key][first_hash], manual)
            second_runtime_path = image_converter_runtime_path("_resources/b.png")
            second_hash = image_converter_hash(f"{note_key}:{second_runtime_path}", 0)
            self.assertEqual(
                cache[note_key][second_hash],
                {"position": "center", "width": "", "height": "", "wrap": False},
            )

            repeated = synchronize(
                vault_root=vault,
                markdown_path=note,
                layout_dir=layout,
                write=True,
            )
            self.assertFalse(repeated["changed"])
            self.assertEqual(repeated["entries_preserved"], 2)

    def test_disabled_plugin_is_reported_without_creating_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            vault = Path(directory)
            (vault / ".obsidian").mkdir()
            (vault / ".obsidian" / "community-plugins.json").write_text(
                "[]", encoding="utf-8"
            )
            note = vault / "paper.md"
            note.write_text("# Paper\n", encoding="utf-8")
            report = synchronize(vault_root=vault, markdown_path=note, write=True)
            self.assertFalse(report["ok"])
            self.assertFalse(
                (vault / ".obsidian" / "image-converter-image-alignments.json").exists()
            )


if __name__ == "__main__":
    unittest.main()
