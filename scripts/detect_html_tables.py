#!/usr/bin/env python3
"""Detect and classify HTML tables in MinerU-derived Markdown notes.

This is a read-only triage tool. It does not convert tables. Its job is to
identify HTML table blocks, report line ranges and structural risks, and split
tables into conservative Markdown-conversion candidates versus tables that need
agent review.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any


TABLE_BLOCK_RE = re.compile(r"(?is)<\s*table\b.*?</\s*table\s*>")
TABLE_TAG_RE = re.compile(r"(?is)</?\s*table\b")
MATH_DOLLAR_RE = re.compile(r"(?<!\$)\$[^$\n]+\$(?!\$)")
LATEX_PAREN_RE = re.compile(r"\\\(|\\\)")
DISPLAY_MATH_RE = re.compile(r"\$\$")
CASES_RE = re.compile(r"\\begin\s*\{\s*cases\s*\}")
HTML_TABLE_TAG_RE = re.compile(r"(?is)</?\s*(?:table|tr|td|th)\b")
BLOCK_TAGS = {
    "article",
    "blockquote",
    "details",
    "div",
    "figure",
    "li",
    "ol",
    "p",
    "pre",
    "section",
    "ul",
}


@dataclass
class Cell:
    tag: str
    text: str
    rowspan: int
    colspan: int


class TableStructureParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.table_depth = 0
        self.rows: list[list[Cell]] = []
        self.current_row: list[Cell] | None = None
        self.current_cell: dict[str, Any] | None = None
        self.flags: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attr_map = {name.lower(): value for name, value in attrs}

        if tag == "table":
            self.table_depth += 1
            if self.table_depth > 1:
                self.flags.add("nested_table")
            return

        if self.table_depth != 1:
            return

        if tag == "tr":
            self.current_row = []
            return

        if tag in {"td", "th"} and self.current_row is not None:
            rowspan = parse_positive_int(attr_map.get("rowspan"), default=1)
            colspan = parse_positive_int(attr_map.get("colspan"), default=1)
            if rowspan != 1:
                self.flags.add("rowspan")
            if colspan != 1:
                self.flags.add("colspan")
            self.current_cell = {
                "tag": tag,
                "parts": [],
                "rowspan": rowspan,
                "colspan": colspan,
            }
            return

        if self.current_cell is not None:
            if tag == "br":
                self.current_cell["parts"].append(" / ")
            elif tag in BLOCK_TAGS:
                self.flags.add(f"block_tag:{tag}")
            elif tag == "img":
                self.flags.add("image_in_cell")
            elif tag in {"code", "script", "style"}:
                self.flags.add(f"code_like_tag:{tag}")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()

        if tag == "table":
            self.table_depth = max(0, self.table_depth - 1)
            return

        if self.table_depth != 1:
            return

        if tag in {"td", "th"} and self.current_cell is not None and self.current_row is not None:
            text = html.unescape("".join(self.current_cell["parts"]))
            text = normalize_cell_text(text)
            self.current_row.append(
                Cell(
                    tag=str(self.current_cell["tag"]),
                    text=text,
                    rowspan=int(self.current_cell["rowspan"]),
                    colspan=int(self.current_cell["colspan"]),
                )
            )
            self.current_cell = None
            return

        if tag == "tr" and self.current_row is not None:
            if self.current_row:
                self.rows.append(self.current_row)
            self.current_row = None

    def handle_data(self, data: str) -> None:
        if self.current_cell is not None:
            self.current_cell["parts"].append(data)

    def handle_entityref(self, name: str) -> None:
        if self.current_cell is not None:
            self.current_cell["parts"].append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if self.current_cell is not None:
            self.current_cell["parts"].append(f"&#{name};")


def parse_positive_int(value: str | None, default: int) -> int:
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def normalize_cell_text(value: str) -> str:
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def line_number_for_offset(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def find_unmatched_table_tags(text: str, matched_spans: list[tuple[int, int]]) -> list[dict[str, int | str]]:
    unmatched: list[dict[str, int | str]] = []
    for match in TABLE_TAG_RE.finditer(text):
        if any(start <= match.start() < end for start, end in matched_spans):
            continue
        unmatched.append(
            {
                "line": line_number_for_offset(text, match.start()),
                "tag": match.group(0),
                "reason": "table tag is outside a complete <table>...</table> block",
            }
        )
    return unmatched


def classify_table(raw_html: str, index: int, start: int, end: int, text: str) -> dict[str, Any]:
    parser = TableStructureParser()
    parser.feed(raw_html)
    parser.close()

    row_lengths = [sum(cell.colspan for cell in row) for row in parser.rows]
    nonzero_lengths = [length for length in row_lengths if length > 0]
    unique_lengths = sorted(set(nonzero_lengths))
    max_columns = max(nonzero_lengths, default=0)

    flags = sorted(parser.flags)
    risk_flags: list[str] = list(flags)
    if len(unique_lengths) > 1:
        risk_flags.append("inconsistent_column_count")
    if not parser.rows:
        risk_flags.append("no_rows_detected")
    if max_columns == 0:
        risk_flags.append("no_cells_detected")
    if MATH_DOLLAR_RE.search(raw_html):
        risk_flags.append("contains_inline_math")
    if LATEX_PAREN_RE.search(raw_html):
        risk_flags.append("contains_latex_paren_delimiters")
    if DISPLAY_MATH_RE.search(raw_html):
        risk_flags.append("contains_display_math_delimiter")
    if CASES_RE.search(raw_html):
        risk_flags.append("contains_cases_formula")
    if any("|" in cell.text for row in parser.rows for cell in row):
        risk_flags.append("contains_pipe_character")

    structural_blockers = {
        flag
        for flag in risk_flags
        if flag in {"rowspan", "colspan", "nested_table", "inconsistent_column_count", "no_rows_detected", "no_cells_detected", "image_in_cell"}
        or flag.startswith("block_tag:")
        or flag.startswith("code_like_tag:")
    }
    if structural_blockers:
        status = "needs_agent_review"
        reasons = sorted(structural_blockers)
    else:
        status = "markdown_conversion_candidate"
        reasons = []

    cell_text_lengths = [len(cell.text) for row in parser.rows for cell in row]

    return {
        "index": index,
        "status": status,
        "start_line": line_number_for_offset(text, start),
        "end_line": line_number_for_offset(text, end),
        "rows": len(parser.rows),
        "columns": max_columns,
        "row_lengths": row_lengths,
        "header_cells": sum(1 for row in parser.rows for cell in row if cell.tag == "th"),
        "cell_count": sum(len(row) for row in parser.rows),
        "max_cell_text_length": max(cell_text_lengths, default=0),
        "risk_flags": sorted(set(risk_flags)),
        "reasons": reasons,
    }


def analyze_text(text: str, source: str) -> dict[str, Any]:
    tables: list[dict[str, Any]] = []
    spans: list[tuple[int, int]] = []
    for index, match in enumerate(TABLE_BLOCK_RE.finditer(text), 1):
        spans.append(match.span())
        tables.append(classify_table(match.group(0), index, match.start(), match.end(), text))

    unmatched = find_unmatched_table_tags(text, spans)
    counts = {
        "tables": len(tables),
        "markdown_conversion_candidates": sum(1 for table in tables if table["status"] == "markdown_conversion_candidate"),
        "needs_agent_review": sum(1 for table in tables if table["status"] == "needs_agent_review"),
        "unmatched_table_tags": len(unmatched),
    }
    return {
        "source": source,
        "counts": counts,
        "tables": tables,
        "unmatched_table_tags": unmatched,
    }


def format_markdown(report: dict[str, Any]) -> str:
    counts = report["counts"]
    lines = [
        "# HTML Table Triage Report",
        "",
        f"- source: `{report['source']}`",
        f"- tables: {counts['tables']}",
        f"- markdown conversion candidates: {counts['markdown_conversion_candidates']}",
        f"- needs agent review: {counts['needs_agent_review']}",
        f"- unmatched table tags: {counts['unmatched_table_tags']}",
        "",
    ]

    if report["tables"]:
        lines.extend(["## Tables", ""])
        for table in report["tables"]:
            flags = ", ".join(table["risk_flags"]) if table["risk_flags"] else "none"
            reasons = ", ".join(table["reasons"]) if table["reasons"] else "none"
            lines.extend(
                [
                    f"### Table {table['index']}",
                    "",
                    f"- status: `{table['status']}`",
                    f"- lines: {table['start_line']}-{table['end_line']}",
                    f"- shape: {table['rows']} rows x {table['columns']} columns",
                    f"- row lengths: {table['row_lengths']}",
                    f"- header cells: {table['header_cells']}",
                    f"- max cell text length: {table['max_cell_text_length']}",
                    f"- risk flags: {flags}",
                    f"- reasons: {reasons}",
                    "",
                ]
            )

    if report["unmatched_table_tags"]:
        lines.extend(["## Unmatched Table Tags", ""])
        for item in report["unmatched_table_tags"]:
            lines.append(f"- line {item['line']}: `{item['tag']}` - {item['reason']}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def read_input(args: argparse.Namespace) -> tuple[str, str]:
    if args.stdin:
        return sys.stdin.read(), "<stdin>"
    if not args.markdown_path:
        raise SystemExit("Provide --markdown-path or --stdin.")
    path = Path(args.markdown_path).resolve()
    return path.read_text(encoding="utf-8"), str(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--markdown-path", help="Path to a Markdown note to inspect.")
    parser.add_argument("--stdin", action="store_true", help="Read Markdown from standard input.")
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    parser.add_argument("--fail-on-tables", action="store_true", help="Exit with code 2 if any complete HTML table exists.")
    parser.add_argument("--fail-on-needs-agent", action="store_true", help="Exit with code 3 if any table needs agent review.")
    args = parser.parse_args()

    text, source = read_input(args)
    report = analyze_text(text, source)

    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_markdown(report), end="")

    counts = report["counts"]
    if args.fail_on_needs_agent and counts["needs_agent_review"]:
        return 3
    if args.fail_on_tables and counts["tables"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
