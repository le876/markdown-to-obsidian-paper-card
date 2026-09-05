#!/usr/bin/env python3
"""Create a controlled assignment for a bilingual paper translation worker."""

from __future__ import annotations

import argparse
import hashlib
import re
from pathlib import Path

from paper_translation_packet import packet_jsonl, read_packet
from safe_atomic_io import atomic_write_json, atomic_write_text
from sync_paper_translation_agent_prompt import (
    DEFAULT_MODEL,
    DEFAULT_REASONING_EFFORT,
    read_fragment,
    render_agent,
)


INLINE_MATH_RE = re.compile(r"(?<!\$)\$(?!\$)(?:\\.|[^$\n])+\$(?!\$)")
CITATION_RE = re.compile(r"\[\[#\^ref-\d+\\?\|[^\]]+\]\]")
IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]+\)")


def read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def count_source_paragraphs(lines: list[str]) -> int:
    return sum(1 for line in lines if re.match(r"^>\s+\S", line))


def count_chinese_body(lines: list[str]) -> int:
    count = 0
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith((">", "#", "!", "|", "```", "---")) and re.search(r"[\u4e00-\u9fff]", stripped):
            count += 1
    return count


def metrics(lines: list[str]) -> dict[str, int | str]:
    text = "\n".join(lines)
    return {
        "source_blockquote_paragraphs": count_source_paragraphs(lines),
        "chinese_body_paragraphs": count_chinese_body(lines),
        "inline_formulas": len(INLINE_MATH_RE.findall(text)),
        "display_math_delimiters": len(re.findall(r"(?m)^\s*\$\$\s*$", text)),
        "images": len(IMAGE_RE.findall(text)),
        "citation_links": len(CITATION_RE.findall(text)),
        "reference_blocks": len(re.findall(r"\^ref-", text)),
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


def numbered_excerpt(lines: list[str], start: int, end: int) -> str:
    return "\n".join(f"{number:>6}: {lines[number - 1]}" for number in range(start, end + 1))


def build_packet_assignment(args: argparse.Namespace) -> str:
    packet_path = Path(args.translation_packet).resolve()
    output_path = Path(args.translation_output).resolve()
    packet = read_packet(packet_path)
    units = packet["units"]
    if args.scope == "range":
        if args.start_unit is None or args.end_unit is None:
            raise SystemExit("--start-unit and --end-unit are required for packet range fallback")
        if args.start_unit < 1 or args.end_unit < args.start_unit or args.end_unit > len(units):
            raise SystemExit("invalid packet range boundary")
        units = units[args.start_unit - 1 : args.end_unit]
        boundary = f"units {args.start_unit}-{args.end_unit}"
    else:
        boundary = "all ordered units"
    packet_slice = packet_path.with_name(packet_path.stem + ".assignment.jsonl")
    atomic_write_text(packet_slice, packet_jsonl({**packet, "units": units}), min_bytes=1)
    fragment_path = Path(__file__).resolve().parents[1] / "references" / "paper-translation-prompt-fragment.md"
    _, prompt_sha256, translator_fingerprint = render_agent(
        read_fragment(fragment_path),
        model=DEFAULT_MODEL,
        reasoning_effort=DEFAULT_REASONING_EFFORT,
    )
    assignment_path = (
        Path(args.assignment_manifest).resolve()
        if args.assignment_manifest
        else packet_path.with_name("translation-assignment.json")
    )
    assignment = {
        "schema_version": 1,
        "packet_path": str(packet_slice.resolve()),
        "output_path": str(output_path),
        "unit_count": len(units),
        "packet_sha256": hashlib.sha256(packet_slice.read_bytes()).hexdigest(),
        "prompt_sha256": prompt_sha256,
        "translator_fingerprint": translator_fingerprint,
        "scope": boundary,
    }
    atomic_write_json(assignment_path, assignment)
    return f"Use the paper-translation-worker agent with assignment manifest: {assignment_path}"


def build_assignment(args: argparse.Namespace) -> str:
    source = Path(args.markdown_path).resolve()
    working_copy = Path(args.working_copy).resolve()
    lines = read_lines(source)
    protocol = Path(args.protocol).resolve() if args.protocol else Path(__file__).resolve().parents[1] / "references" / "paper-bilingual-translation-subagent.md"
    if not source.is_file() or not working_copy.is_file():
        raise SystemExit("--markdown-path and --working-copy must both exist")
    if source == working_copy:
        raise SystemExit("working copy must be disjoint from source markdown")
    if not protocol.is_file():
        raise SystemExit(f"translation protocol does not exist: {protocol}")

    if args.scope == "whole-document":
        selected = lines
        boundary = "whole document"
        excerpt = "Read the complete working copy directly; no line excerpt is intentionally supplied."
    else:
        if args.start_line is None or args.end_line is None:
            raise SystemExit("--start-line and --end-line are required for --scope range")
        if args.start_line < 1 or args.end_line < args.start_line or args.end_line > len(lines):
            raise SystemExit("invalid range boundary")
        selected = lines[args.start_line - 1 : args.end_line]
        boundary = f"lines {args.start_line}-{args.end_line}"
        excerpt = numbered_excerpt(lines, args.start_line, args.end_line)

    selected_metrics = metrics(selected)
    full_metrics = metrics(lines)
    return f"""# Controlled bilingual translation assignment

Protocol to read first:
{protocol}

Authorization: the user requested the Markdown-to-Obsidian paper-card workflow; this authorizes one controlled translation worker for this declared scope only.

Source file (read-only):
{source}

Working copy (the only file you may edit):
{working_copy}

Scope:
{boundary}

Required operation:
- Read the complete working copy before translating so terminology, symbols, citations, and author voice remain consistent.
- Preserve English source paragraphs as blockquotes and write Chinese paragraphs directly below their matching English paragraphs.
- Do not edit frontmatter, headings, images, tables, display math, References, citation link syntax, or unrelated text.
- Do not summarize, omit claims, call an external translation engine, or edit the source file or final output path.
- Report the working-copy path and the before/after counts when done.

Protected full-document metrics before editing:
{full_metrics}

Metrics for this scope before editing:
{selected_metrics}

Line excerpt for a range fallback:
```markdown
{excerpt}
```

Handoff requirements:
- Confirm the source file was untouched.
- Report source and Chinese paragraph counts, inline formula count, citation-link count, image count, display-math delimiter count, References block count, and SHA-256 of the edited working copy.
- If the whole document cannot be completed faithfully, do not compress it. Stop before damage and report a recommended continuous range for a fallback assignment.
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--markdown-path", help="Legacy read-only stabilized paper Markdown.")
    parser.add_argument("--working-copy", help="Legacy disjoint copy that the worker may edit.")
    parser.add_argument("--translation-packet", help="Compact packet used by the default worker workflow.")
    parser.add_argument("--translation-output", help="JSONL file the packet worker must create.")
    parser.add_argument("--assignment-manifest", help="Compact JSON assignment manifest to write in packet mode.")
    parser.add_argument("--scope", choices=("whole-document", "range"), default="whole-document")
    parser.add_argument("--start-line", type=int)
    parser.add_argument("--end-line", type=int)
    parser.add_argument("--start-unit", type=int)
    parser.add_argument("--end-unit", type=int)
    parser.add_argument("--protocol")
    args = parser.parse_args()
    if args.translation_packet:
        if not args.translation_output:
            raise SystemExit("--translation-output is required with --translation-packet")
        print(build_packet_assignment(args))
    else:
        if not args.markdown_path or not args.working_copy:
            raise SystemExit("legacy mode requires --markdown-path and --working-copy")
        print(build_assignment(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
