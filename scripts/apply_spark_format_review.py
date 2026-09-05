from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re


ALLOWED_TYPES = {"abstract_heading", "citation_separator"}
CITATION_RE = re.compile(r"\[\^(\d+)\]")
IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
URL_RE = re.compile(r"https?://[^\s)>]+")
FOOTNOTE_DEF_RE = re.compile(r"^\[\^([^\]]+)\]:", re.MULTILINE)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--review", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    source_bytes = args.source.read_bytes()
    if source_bytes.startswith(b"\xef\xbb\xbf"):
        raise SystemExit("source must be UTF-8 without BOM")
    source_text = source_bytes.decode("utf-8")
    review = json.loads(args.review.read_text(encoding="utf-8"))
    if review.get("status") != "success":
        raise SystemExit("Spark review did not report success")
    if review.get("source_sha256") != digest(source_bytes):
        raise SystemExit("Spark review source hash mismatch")
    if review.get("unresolved"):
        raise SystemExit("Spark review contains unresolved items")

    lines = source_text.splitlines(keepends=True)
    seen: set[int] = set()
    for edit in review.get("changes", []):
        line_number = int(edit["line"])
        if line_number in seen:
            raise SystemExit(f"duplicate edit for line {line_number}")
        seen.add(line_number)
        if edit.get("type") not in ALLOWED_TYPES:
            raise SystemExit(f"disallowed edit type: {edit.get('type')}")
        if not 1 <= line_number <= len(lines):
            raise SystemExit(f"line is out of range: {line_number}")
        before = str(edit["before"])
        after = str(edit["after"])
        if "\n" in before or "\r" in before or "\n" in after or "\r" in after:
            raise SystemExit("edits must be single-line replacements")
        original = lines[line_number - 1]
        ending = "\r\n" if original.endswith("\r\n") else "\n" if original.endswith("\n") else ""
        body = original[: -len(ending)] if ending else original
        if body != before:
            raise SystemExit(f"before text mismatch at line {line_number}")
        lines[line_number - 1] = after + ending

    output_text = "".join(lines)
    if len(output_text.splitlines()) != len(source_text.splitlines()):
        raise SystemExit("line count changed")
    invariants = (
        (CITATION_RE.findall(source_text), CITATION_RE.findall(output_text), "citation sequence"),
        (IMAGE_RE.findall(source_text), IMAGE_RE.findall(output_text), "image targets"),
        (URL_RE.findall(source_text), URL_RE.findall(output_text), "URLs"),
        (FOOTNOTE_DEF_RE.findall(source_text), FOOTNOTE_DEF_RE.findall(output_text), "footnote definitions"),
    )
    for before_values, after_values, label in invariants:
        if before_values != after_values:
            raise SystemExit(f"{label} changed")
    if output_text.count("```") != source_text.count("```"):
        raise SystemExit("code-fence count changed")
    if output_text.count("|") != source_text.count("|"):
        raise SystemExit("table-pipe count changed")
    source_abstract = re.search(r"^#{1,6}\s+Abstract\s*$", source_text, flags=re.MULTILINE)
    if source_abstract is not None:
        abstract_match = re.search(r"^#{1,6}\s+Abstract\s*$", output_text, flags=re.MULTILINE)
        if abstract_match is None or abstract_match.group(0).strip() != "## Abstract":
            raise SystemExit("Abstract heading was not normalized to H2")
    if re.search(r"\[\^\d+\],\s+\[\^\d+\]", output_text):
        raise SystemExit("baseline-comma citation cluster remains")

    output_bytes = output_text.encode("utf-8")
    if not output_bytes:
        raise SystemExit("output is empty")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(args.output.name + ".tmp")
    temporary.write_bytes(output_bytes)
    os.replace(temporary, args.output)
    print(json.dumps({
        "ok": True,
        "changes": len(seen),
        "line_count": len(lines),
        "output_bytes": len(output_bytes),
        "output_sha256": digest(output_bytes),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
