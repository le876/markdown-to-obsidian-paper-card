from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import safe_atomic_io


class SafeAtomicIOTests(unittest.TestCase):
    def test_permission_error_falls_back_to_unlink_replace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "state.json"
            target.write_text('{"old": true}\n', encoding="utf-8")
            real_replace = safe_atomic_io.os.replace
            calls = 0

            def flaky_replace(source: Path, destination: Path) -> None:
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise PermissionError("simulated Windows sharing violation")
                real_replace(source, destination)

            with mock.patch.object(safe_atomic_io.os, "replace", side_effect=flaky_replace):
                method = safe_atomic_io.atomic_write_json(target, {"new": True})

            self.assertEqual(method, "windows_unlink_replace")
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"new": True})

    def test_failed_move_after_unlink_restores_old_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "state.json"
            old = '{"old": true}\n'
            target.write_text(old, encoding="utf-8")
            with mock.patch.object(
                safe_atomic_io.os,
                "replace",
                side_effect=(PermissionError("locked"), OSError("move failed")),
            ):
                with self.assertRaises(OSError):
                    safe_atomic_io.atomic_write_json(target, {"new": True})

            self.assertEqual(target.read_text(encoding="utf-8"), old)

    def test_invalid_json_never_overwrites_old_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "state.json"
            old = '{"old": true}\n'
            target.write_text(old, encoding="utf-8")
            with self.assertRaises(json.JSONDecodeError):
                safe_atomic_io.atomic_write_text(
                    target,
                    "{invalid",
                    validator=safe_atomic_io.validate_json_text,
                )
            self.assertEqual(target.read_text(encoding="utf-8"), old)

    def test_invalid_markdown_never_overwrites_old_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "paper.md"
            old = "---\ntitle: old\n---\n\n# Old\n"
            target.write_text(old, encoding="utf-8")
            with self.assertRaises(ValueError):
                safe_atomic_io.atomic_write_text(
                    target,
                    "too short",
                    validator=safe_atomic_io.validate_markdown_text,
                )
            self.assertEqual(target.read_text(encoding="utf-8"), old)


if __name__ == "__main__":
    unittest.main()
