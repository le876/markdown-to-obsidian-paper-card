from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from run_spark_format_review import ENV_KEYS_TO_REMOVE, worker_environment  # noqa: E402


class SparkFormatReviewTests(unittest.TestCase):
    def test_parent_applier_preserves_crlf_and_local_h6_headings(self) -> None:
        source = (
            "# T\r\n\r\n"
            "###### Abstract\r\n\r\n"
            "Evidence[^15], [^6], [^16].\r\n\r\n"
            "###### Proposition 1\r\n"
        )
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            source_path = directory / "source.md"
            review_path = directory / "review.json"
            output_path = directory / "normalized.md"
            source_path.write_bytes(source.encode("utf-8"))
            lines = source.splitlines()
            review = {
                "status": "success",
                "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
                "changes": [
                    {
                        "line": 3,
                        "type": "abstract_heading",
                        "before": lines[2],
                        "after": "## Abstract",
                        "reason": "normalize",
                    },
                    {
                        "line": 5,
                        "type": "citation_separator",
                        "before": lines[4],
                        "after": "Evidence[^15]<sup>,</sup> [^6]<sup>,</sup> [^16].",
                        "reason": "normalize",
                    },
                ],
                "unresolved": [],
                "self_checks": {},
            }
            review_path.write_text(json.dumps(review), encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "apply_spark_format_review.py"),
                    "--source",
                    str(source_path),
                    "--review",
                    str(review_path),
                    "--output",
                    str(output_path),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            output = output_path.read_bytes()
            self.assertIn(b"## Abstract\r\n", output)
            self.assertIn("<sup>,</sup>".encode("utf-8"), output)
            self.assertIn("###### Proposition 1".encode("utf-8"), output)
            self.assertFalse(output.startswith(b"\xef\xbb\xbf"))

    def test_parent_applier_rejects_unapproved_edit_type(self) -> None:
        source = "# T\n\nText.\n"
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            source_path = directory / "source.md"
            review_path = directory / "review.json"
            output_path = directory / "normalized.md"
            source_path.write_text(source, encoding="utf-8", newline="\n")
            review_path.write_text(
                json.dumps(
                    {
                        "status": "success",
                        "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
                        "changes": [
                            {
                                "line": 3,
                                "type": "rewrite_prose",
                                "before": "Text.",
                                "after": "Changed.",
                                "reason": "not allowed",
                            }
                        ],
                        "unresolved": [],
                        "self_checks": {},
                    }
                ),
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "apply_spark_format_review.py"),
                    "--source",
                    str(source_path),
                    "--review",
                    str(review_path),
                    "--output",
                    str(output_path),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(output_path.exists())

    def test_worker_environment_removes_parent_codex_context(self) -> None:
        injected = {key: "must-not-leak" for key in ENV_KEYS_TO_REMOVE}
        with patch.dict(os.environ, injected, clear=False):
            environment = worker_environment()
        for key in ENV_KEYS_TO_REMOVE:
            self.assertNotIn(key, environment)

    def test_packaged_schema_is_valid_json(self) -> None:
        schema = json.loads((ROOT / "references" / "paper-formatting-final.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(schema["type"], "object")
        self.assertIn("changes", schema["properties"])


if __name__ == "__main__":
    unittest.main()
