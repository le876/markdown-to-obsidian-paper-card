from __future__ import annotations

import argparse
import base64
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from build_obsidian_paper_card import (
    NATIVE_SUBAGENT_BACKEND,
    SourceContext,
    build,
    filter_web_clipping_noise,
    prepare_assets,
)


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl2nWQAAAAASUVORK5CYII="
)


class WebNoiseAndRemoteAssetTests(unittest.TestCase):
    def empty_context(self) -> SourceContext:
        return SourceContext(None, None, {}, {}, None, None)

    def test_filters_captured_site_navigation_but_preserves_body_anchor(self) -> None:
        source = (
            "[Simple AI](#S8 \"8 Conclusion ‣ HiFi-UMI\")\n\n"
            "[Skip to content](#main)\n\n"
            "# HiFi-UMI\n\n"
            "See [Section 3](#section-3) for the policy architecture.\n\n"
            "[Section 3](#section-3)\n"
        )
        filtered, report = filter_web_clipping_noise(source)
        self.assertNotIn("Simple AI", filtered)
        self.assertNotIn("Skip to content", filtered)
        self.assertIn("See [Section 3](#section-3)", filtered)
        self.assertIn("[Section 3](#section-3)", filtered)
        self.assertEqual(report["removed_count"], 2)

    def test_remote_assets_are_deferred_then_downloaded_to_vault_resources_with_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "vault"
            resources = vault / "_resources"
            output = vault / "论文" / "Paper.md"
            source_path = root / "source.md"
            source_path.write_text("# Paper\n", encoding="utf-8", newline="\n")
            text = (
                "# Paper\n\n"
                "![A](https://example.test/a.png)\n\n"
                "![B](https://example.test/b.png)\n\n"
                "![A2](https://example.test/a.png)\n"
            )
            cache_path = root / "workflow" / "remote-asset-cache.json"

            with mock.patch(
                "build_obsidian_paper_card.load_remote_image"
            ) as loader:
                deferred, report, unresolved = prepare_assets(
                    text,
                    source_path,
                    output,
                    resources,
                    self.empty_context(),
                    True,
                    remote_mode="defer",
                )
            loader.assert_not_called()
            self.assertEqual(deferred, text)
            self.assertEqual(unresolved, [])
            self.assertEqual(
                sum(item["status"] == "remote_pending" for item in report),
                3,
            )

            calls: list[str] = []
            lock = threading.Lock()

            def fake_load(url: str) -> tuple[bytes, str, str]:
                with lock:
                    calls.append(url)
                return PNG_1X1, ".png", Path(url).stem

            with mock.patch(
                "build_obsidian_paper_card.load_remote_image",
                side_effect=fake_load,
            ):
                localized, report, unresolved = prepare_assets(
                    deferred,
                    source_path,
                    output,
                    resources,
                    self.empty_context(),
                    True,
                    remote_mode="download",
                    remote_cache_path=cache_path,
                )
            self.assertEqual(set(calls), {"https://example.test/a.png", "https://example.test/b.png"})
            self.assertEqual(len(calls), 2)
            self.assertNotIn("https://", localized)
            self.assertEqual(unresolved, [])
            self.assertTrue(cache_path.is_file())
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
            self.assertEqual(cache["schema_version"], 1)
            self.assertEqual(len(cache["entries"]), 2)
            targets = [Path(item["target"]) for item in report if item.get("target")]
            self.assertEqual(len(targets), 3)
            self.assertTrue(all(target.parent == resources for target in targets))
            self.assertTrue(all(target.is_file() for target in targets))

            with mock.patch(
                "build_obsidian_paper_card.load_remote_image",
                side_effect=AssertionError("network should not be used on cache hit"),
            ):
                localized_again, cached_report, cached_unresolved = prepare_assets(
                    deferred,
                    source_path,
                    output,
                    resources,
                    self.empty_context(),
                    True,
                    remote_mode="download",
                    remote_cache_path=cache_path,
                )
            self.assertEqual(localized_again, localized)
            self.assertEqual(cached_unresolved, [])
            self.assertEqual(
                sum(item["status"] == "remote_cache_hit" for item in cached_report),
                3,
            )

    def test_native_prepare_defers_network_and_layout_downloads_after_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "vault"
            vault.mkdir()
            source = root / "source.md"
            source.write_text(
                "# Paper\n\n## Abstract\n\nA paper claim.\n\n"
                "![Figure](https://example.test/figure.png)\n",
                encoding="utf-8",
                newline="\n",
            )
            output = vault / "论文" / "Paper.md"
            workflow = vault / ".workflow"
            args = argparse.Namespace(
                input_markdown=str(source),
                vault_root=str(vault),
                output_note=str(output),
                translation_mode="bilingual",
                concept_links="off",
                source_package=None,
                translation_stage="run",
                workflow_dir=str(workflow),
                translation_output=None,
                translator_fingerprint=None,
                worker_backend=NATIVE_SUBAGENT_BACKEND,
                translation_agent_role_file=None,
                translation_agent_task_name=None,
                worker_timeout_seconds=1800,
                worker_max_attempts=2,
                image_converter_layout="off",
                overwrite_image_converter_alignments=False,
                in_place=False,
                write=True,
            )
            fake_handoff = {
                "ok": True,
                "stage": "native_subagent_prepare",
                "worker_backend": "native-subagent",
                "assignment_path": str(workflow / "native-assignment.json"),
                "spawn_agent": {
                    "agent_type": "paper-translation-worker",
                    "task_name": "translate_paper_a1",
                    "fork_turns": "none",
                    "message": "read assignment",
                },
            }
            with mock.patch(
                "build_obsidian_paper_card.prepare_native_subagent",
                return_value=fake_handoff,
            ), mock.patch(
                "build_obsidian_paper_card.load_remote_image",
                side_effect=AssertionError("prepare must not download remote images"),
            ):
                result = build(args)
            self.assertEqual(result["status"], "awaiting_native_subagent")
            prepared = output.with_name(f".{output.name}.translation-prepared.md")
            self.assertIn("https://example.test/figure.png", prepared.read_text(encoding="utf-8"))

            layout_args = argparse.Namespace(**vars(args))
            layout_args.translation_stage = "layout"
            with mock.patch(
                "build_obsidian_paper_card.load_remote_image",
                return_value=(PNG_1X1, ".png", "figure"),
            ) as loader:
                layout = build(layout_args)
            loader.assert_called_once()
            self.assertEqual(
                layout["assets"]["download_window"],
                "parallel_with_translation_worker",
            )
            template = (workflow / "merge-template.md").read_text(encoding="utf-8")
            self.assertNotIn("https://example.test/figure.png", template)
            self.assertEqual(len(list((vault / "_resources").glob("*.png"))), 1)

    def test_noncritical_remote_chrome_is_removed_without_blocking_critical_figures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "vault"
            resources = vault / "_resources"
            output = vault / "论文" / "Paper.md"
            source_path = root / "source.md"
            source_path.write_text("# Paper\n", encoding="utf-8", newline="\n")
            text = (
                "# Paper\n\n"
                "![site logo](https://example.test/site-logo.png)\n\n"
                "![Figure 2](https://example.test/figure-2.png)\n"
                "Figure 2: Main result.\n"
            )

            with mock.patch(
                "build_obsidian_paper_card.load_remote_image",
                return_value=(PNG_1X1, ".png", "figure-2"),
            ) as loader:
                localized, report, unresolved = prepare_assets(
                    text,
                    source_path,
                    output,
                    resources,
                    self.empty_context(),
                    True,
                    remote_mode="download",
                )
            loader.assert_called_once_with("https://example.test/figure-2.png")
            self.assertNotIn("site-logo", localized)
            self.assertNotIn("https://", localized)
            self.assertEqual(unresolved, [])
            self.assertTrue(any(item["status"] == "remote_noncritical_removed" for item in report))

            with mock.patch(
                "build_obsidian_paper_card.load_remote_image",
                side_effect=OSError("network unavailable"),
            ):
                _, failed_report, failed_unresolved = prepare_assets(
                    "# Paper\n\n![Figure 3](https://example.test/figure-3.png)\nFigure 3: Result.\n",
                    source_path,
                    output,
                    resources,
                    self.empty_context(),
                    True,
                    remote_mode="download",
                )
            self.assertEqual(failed_unresolved, ["https://example.test/figure-3.png"])
            self.assertTrue(any(item.get("criticality") == "critical" for item in failed_report))


if __name__ == "__main__":
    unittest.main()
