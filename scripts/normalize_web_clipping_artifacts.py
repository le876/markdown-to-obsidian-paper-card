#!/usr/bin/env python3
"""Normalize narrow, auditable arXiv/LaTeXML clipping artifacts."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Callable, Match

from safe_atomic_io import atomic_write_text


CAPTION_RE = re.compile(
    r"^\s*(?:>\s*)?(?:Figure|Fig\.?|图)\s*[A-Za-z]?\d+\s*[:：]",
    re.IGNORECASE,
)


def _tex(raw: str) -> str:
    """Restore one TeX command slash and remove escaped subscript markers."""
    value = raw
    while "\\\\" in value:
        value = value.replace("\\\\", "\\")
    return value.replace("\\_", "_").strip()


def _math(raw: str) -> str:
    return f"${_tex(raw)}$"


def _same_number(match: Match[str], tex_template: str) -> str:
    return _math(tex_template.format(**match.groupdict()))


def _normalize_caption_line(line: str) -> tuple[str, int]:
    if not CAPTION_RE.match(line):
        return line, 0

    count = 0
    value = line

    substitutions: list[tuple[re.Pattern[str], Callable[[Match[str]], str]]] = [
        (
            re.compile(
                r"(?<![$\\])\b\d+(?:,\s*\d{3})*\s*(?:mm|cm|m|ms|s|Hz|kHz|MHz|GHz)\s+"
                r"(?P<tex>\d+(?:\{,\}\d{3})*\\{1,2},\\{1,2}mathrm\{(?:mm|cm|m|ms|s|Hz|kHz|MHz|GHz)\})"
            ),
            lambda m: _math(m.group("tex")),
        ),
        (
            re.compile(
                r"(?<![$\\])\b\d{1,3}(?:,\s*\d{3})+\s+"
                r"(?P<tex>\d{1,3}(?:\{,\}\d{3})+)"
            ),
            lambda m: _math(m.group("tex")),
        ),
        (
            re.compile(r"(?<![$\\])\b\d+\s+(?P<tex>\d+\{,\}\d{3}(?:\{,\}\d{3})*)"),
            lambda m: _math(m.group("tex")),
        ),
        (
            re.compile(
                r"(?<![$\\])\b(?P<n>\d+(?:\.\d+)?)\s*%\s+"
                r"(?P<tex>(?P=n)\\{1,2}%)"
            ),
            lambda m: _math(m.group("tex")),
        ),
        (
            re.compile(
                r"≈\s*\d+(?:\.\d+)?\s+"
                r"(?P<tex>\{?\\{1,2}approx\}?\s*\d+(?:\.\d+)?\\{1,2}%)"
            ),
            lambda m: _math(m.group("tex")),
        ),
        (
            re.compile(
                r"α\s*=\s*(?P<n>\d+(?:\.\d+)?)\s+"
                r"\\{1,2}alpha\s*=\s*(?P=n)"
            ),
            lambda m: _math(r"\alpha=" + m.group("n")),
        ),
        (
            re.compile(
                r"R\s+(?P<n>\d+(?:\.\d+)?)\s+R\^\{2\}\s*=\s*(?P=n)"
            ),
            lambda m: _math("R^{2}=" + m.group("n")),
        ),
        (
            re.compile(
                r"π\s*(?P<n>\d+(?:\.\d+)?)\s+"
                r"(?P<tex>\\{1,2}pi\\?_\{(?P=n)\})"
            ),
            lambda m: _math(m.group("tex")),
        ),
        (
            re.compile(
                r"(?P<v>[A-Za-z])\s*=\s*(?P<n>\d+(?:\.\d+)?)\s+"
                r"(?P=v)\s*=\s*(?P=n)"
            ),
            lambda m: _math(f"{m.group('v')}={m.group('n')}"),
        ),
        (
            re.compile(
                r"t\s*(?P<a>\d+)\s+t\\?_\{(?P=a)\}\s*[–—-]\s*"
                r"(?P<b>\d+)\s+t\\?_\{(?P=b)\}"
            ),
            lambda m: _math(f"t_{{{m.group('a')}}}")
            + "–"
            + _math(f"t_{{{m.group('b')}}}"),
        ),
        (
            re.compile(
                r"t\s*(?P<a>\d+)\s*,\s*…\s+t\s*=\s*(?P=a)\s*,"
                r"\\{1,2}ldots\s*,\s*(?P<b>\d+)"
            ),
            lambda m: _math(f"t={m.group('a')},\\ldots,{m.group('b')}"),
        ),
    ]

    for pattern, replacement in substitutions:
        value, replacements = pattern.subn(replacement, value)
        count += replacements

    plain_math_substitutions: list[tuple[re.Pattern[str], Callable[[Match[str]], str]]] = [
        (
            re.compile(
                r"(?<![$\\\w])(?P<n>\d+(?:\.\d+)?)\s+"
                r"(?P<u>mm|cm|ms|Hz|kHz|MHz|GHz)\b"
            ),
            lambda m: _math(f"{m.group('n')}\\,\\mathrm{{{m.group('u')}}}"),
        ),
        (
            re.compile(r"(?<![$\\\w])(?P<n>\d+(?:\.\d+)?)%(?![$\w])"),
            lambda m: _math(m.group("n") + r"\%"),
        ),
    ]
    for pattern, replacement in plain_math_substitutions:
        value, replacements = pattern.subn(replacement, value)
        count += replacements
    return value, count


RAW_CAPTION_TEX_RE = re.compile(
    r"\\\\(?:alpha|pi|mathrm|ldots|approx|%)|\\_\{|\{\\\\approx\}"
)


def normalize_web_clipping_artifacts(text: str) -> tuple[str, dict[str, Any]]:
    output: list[str] = []
    caption_lines = 0
    fixed = 0
    residual: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(keepends=True), 1):
        if CAPTION_RE.match(line):
            caption_lines += 1
        normalized, replacements = _normalize_caption_line(line)
        fixed += replacements
        if CAPTION_RE.match(normalized) and RAW_CAPTION_TEX_RE.search(normalized):
            residual.append({"line": line_number, "text": normalized.strip()[:240]})
        output.append(normalized)
    return "".join(output), {
        "caption_lines_scanned": caption_lines,
        "caption_math_artifacts_fixed": fixed,
        "residual_caption_math_artifacts": len(residual),
        "residual": residual,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--markdown-path", required=True)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()

    path = Path(args.markdown_path)
    old = path.read_text(encoding="utf-8")
    new, report = normalize_web_clipping_artifacts(old)
    changed = new != old
    if args.write and changed:
        atomic_write_text(path, new, min_bytes=100)
    print(json.dumps({**report, "changed": changed, "wrote": bool(args.write and changed)}, ensure_ascii=False))
    return 0 if not report["residual_caption_math_artifacts"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
