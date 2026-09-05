#!/usr/bin/env python3
"""Create and validate hover-preview links from paper figure mentions to image attachments."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import urllib.parse
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from safe_atomic_io import atomic_write_text, validate_markdown_text


IMAGE_EXTENSIONS = {
    ".avif", ".bmp", ".gif", ".jpeg", ".jpg", ".png", ".svg", ".webp",
}
FIGURE_ID_PATTERN = r"[A-Za-z]?\d+"
# LaTeXML/web clippers may expose TeX's non-breaking ``~`` as a literal
# ASCII tilde or U+02DC SMALL TILDE, e.g. ``Fig.˜4``.  These are separators,
# but they are not matched by ``\s``.
FIGURE_GAP_PATTERN = r"[\s\u00a0\u2009\u202f~\u02dc]*"
MARKDOWN_IMAGE_LINE_RE = re.compile(
    r"^\s*!\[(?P<alt>[^\]]*)\]\((?P<target>[^)\r\n]+)\)\s*$"
)
WIKI_IMAGE_LINE_RE = re.compile(
    r"^\s*!\[\[(?P<target>[^\]|\r\n]+)(?:\|[^\]\r\n]+)?\]\]\s*$"
)
CAPTION_RE = re.compile(
    rf"^\s*(?:>\s*)?(?:\*\*|__)?(?:Figure|Fig\.?|\u56fe){FIGURE_GAP_PATTERN}"
    rf"(?P<figure>{FIGURE_ID_PATTERN})\s*(?:[:\uff1a.]|\([A-Za-z0-9]+\)|\uff08[A-Za-z0-9]+\uff09)",
    re.IGNORECASE,
)
EN_SINGULAR_RE = re.compile(
    rf"\b(?P<display>(?:Figure|Fig\.?){FIGURE_GAP_PATTERN}(?P<figure>{FIGURE_ID_PATTERN})"
    rf"(?:\s*(?:\([A-Za-z0-9]+\)|\uff08[A-Za-z0-9]+\uff09))?)",
    re.IGNORECASE,
)
CN_SINGULAR_RE = re.compile(
    rf"(?P<display>\u56fe{FIGURE_GAP_PATTERN}(?P<figure>{FIGURE_ID_PATTERN})"
    rf"(?:\s*(?:\([A-Za-z0-9]+\)|\uff08[A-Za-z0-9]+\uff09))?)"
)
EN_RANGE_RE = re.compile(
    rf"\b(?P<display>(?:Figures?|Figs?\.?){FIGURE_GAP_PATTERN}{FIGURE_ID_PATTERN}"
    rf"\s*[\u2013\u2014-]\s*{FIGURE_ID_PATTERN})",
    re.IGNORECASE,
)
CN_RANGE_RE = re.compile(
    rf"(?P<display>\u56fe{FIGURE_GAP_PATTERN}{FIGURE_ID_PATTERN}\s*[\u2013\u2014-]\s*{FIGURE_ID_PATTERN})"
)
EN_PLURAL_RE = re.compile(
    rf"\b(?P<prefix>(?:Figures|Figs\.?)){FIGURE_GAP_PATTERN}"
    rf"(?P<body>{FIGURE_ID_PATTERN}(?:\s*(?:,\s*(?:and\s+)?|\s+and\s+|&\s*)"
    rf"{FIGURE_ID_PATTERN})+)",
    re.IGNORECASE,
)
FIGURE_ID_RE = re.compile(FIGURE_ID_PATTERN, re.IGNORECASE)
PROTECTED_INLINE_RE = re.compile(
    r"!?\[\[[^\]\r\n]+\]\]"
    r"|!?\[[^\]\r\n]*\]\([^)]+\)"
    r"|\x60+[^\x60\r\n]*\x60+"
    r"|(?<!\$)\$[^$\r\n]+\$(?!\$)"
    r"|https?://[^\s<>()]+"
    r"|%%.*?%%"
)
MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[(?P<display>[^\]\r\n]+)\]\((?P<target>[^)\r\n]+)\)")
WIKI_LINK_RE = re.compile(r"(?<!!)\[\[(?P<target>[^\]|\r\n]+)\|(?P<display>[^\]\r\n]+)\]\]")
HEADING_RE = re.compile(r"^\s*#{1,6}\s+(?P<title>.+?)\s*$")
CODE_FENCE_RE = re.compile(r"^\s*(?:\x60{3}|~{3})")
EXCLUDED_VAULT_PARTS = {".git", ".obsidian", ".tmp"}


@dataclass(frozen=True)
class FigureImage:
    style: str
    target: str
    line: int


@dataclass(frozen=True)
class FigureTarget:
    figure_id: str
    style: str
    target: str
    line: int

    def render_link(self, display: str) -> str:
        if self.style == "wiki":
            return f"[[{self.target}|{display}]]"
        return f"[{display}]({encode_markdown_target(self.target)})"


def canonical_figure_id(raw: str) -> str:
    match = re.fullmatch(r"(?P<prefix>[A-Za-z]?)(?P<number>\d+)", raw.strip())
    if not match:
        return raw.strip().upper()
    return f"{match.group('prefix').upper()}{int(match.group('number'))}"


def normalize_figure_display(display: str) -> str:
    """Remove clipping-only TeX spacing artifacts without rewording a label."""
    return re.sub(
        rf"^((?:Figure|Fig\.?|\u56fe)){FIGURE_GAP_PATTERN}(?={FIGURE_ID_PATTERN})",
        r"\1 ",
        display,
        flags=re.IGNORECASE,
    )


def encode_markdown_target(raw: str) -> str:
    value = raw.strip()
    if value.startswith("<") and value.endswith(">"):
        value = value[1:-1].strip()
    decoded = urllib.parse.unquote(value)
    return urllib.parse.quote(decoded, safe="/:@-._~%")


def parse_image_line(line: str, line_number: int) -> FigureImage | None:
    markdown = MARKDOWN_IMAGE_LINE_RE.fullmatch(line.rstrip("\r\n"))
    if markdown:
        return FigureImage("markdown", markdown.group("target").strip(), line_number)
    wiki = WIKI_IMAGE_LINE_RE.fullmatch(line.rstrip("\r\n"))
    if wiki:
        return FigureImage("wiki", wiki.group("target").strip(), line_number)
    return None


def caption_figure_id(line: str) -> str | None:
    match = CAPTION_RE.match(line.rstrip("\r\n"))
    return canonical_figure_id(match.group("figure")) if match else None


def discover_figure_targets(text: str) -> dict[str, Any]:
    candidates: dict[str, list[FigureTarget]] = {}
    ambiguous: dict[str, list[dict[str, Any]]] = {}
    pending: list[FigureImage] = []

    for line_number, line in enumerate(text.splitlines(keepends=True), 1):
        image = parse_image_line(line, line_number)
        if image:
            pending.append(image)
            continue
        if not line.strip() and pending:
            continue
        figure_id = caption_figure_id(line)
        if figure_id and pending:
            if len(pending) == 1:
                image = pending[0]
                candidates.setdefault(figure_id, []).append(
                    FigureTarget(figure_id, image.style, image.target, image.line)
                )
            else:
                ambiguous.setdefault(figure_id, []).append(
                    {
                        "reason": "multi_asset",
                        "image_count": len(pending),
                        "lines": [item.line for item in pending],
                        "targets": [item.target for item in pending],
                    }
                )
            pending = []
            continue
        pending = []

    targets: dict[str, FigureTarget] = {}
    for figure_id, values in candidates.items():
        unique = {(item.style, item.target): item for item in values}
        if figure_id in ambiguous:
            ambiguous[figure_id].append(
                {"reason": "single_target_also_has_ambiguous_group", "targets": [item.target for item in values]}
            )
        elif len(unique) == 1:
            targets[figure_id] = next(iter(unique.values()))
        else:
            ambiguous[figure_id] = [
                {
                    "reason": "multiple_distinct_targets",
                    "targets": [item.target for item in unique.values()],
                    "lines": [item.line for item in unique.values()],
                }
            ]

    return {
        "targets": targets,
        "ambiguous": ambiguous,
    }


def _overlaps(span: tuple[int, int], protected: Iterable[tuple[int, int]]) -> bool:
    start, end = span
    return any(start < other_end and end > other_start for other_start, other_end in protected)


def _unresolved_entry(
    report: dict[str, Any],
    *,
    figure_id: str,
    display: str,
    line_number: int,
    ambiguous: dict[str, Any],
) -> None:
    reason = "ambiguous_target" if figure_id in ambiguous else "missing_target"
    report["unmatched"].append(
        {"figure": figure_id, "display": display, "line": line_number, "reason": reason}
    )


def _link_segment(
    segment: str,
    *,
    line_number: int,
    targets: dict[str, FigureTarget],
    ambiguous: dict[str, Any],
    report: dict[str, Any],
) -> str:
    replacements: list[tuple[int, int, str]] = []
    occupied: list[tuple[int, int]] = []

    for pattern in (EN_RANGE_RE, CN_RANGE_RE):
        for match in pattern.finditer(segment):
            occupied.append(match.span())
            report["range_mentions"].append(
                {"display": match.group("display"), "line": line_number}
            )

    for match in EN_PLURAL_RE.finditer(segment):
        if _overlaps(match.span(), occupied):
            continue
        body = match.group("body")
        linked_body = body
        body_replacements: list[tuple[int, int, str]] = []
        for id_match in FIGURE_ID_RE.finditer(body):
            figure_id = canonical_figure_id(id_match.group(0))
            report["figure_mentions"] += 1
            target = targets.get(figure_id)
            if target is None:
                _unresolved_entry(
                    report,
                    figure_id=figure_id,
                    display=id_match.group(0),
                    line_number=line_number,
                    ambiguous=ambiguous,
                )
                continue
            body_replacements.append(
                (id_match.start(), id_match.end(), target.render_link(id_match.group(0)))
            )
            report["figure_links_written"] += 1
        for start, end, replacement in reversed(body_replacements):
            linked_body = linked_body[:start] + replacement + linked_body[end:]
        if body_replacements:
            replacements.append(
                (match.start(), match.end(), match.group("prefix") + " " + linked_body)
            )
        occupied.append((match.start(), match.end()))

    singular_matches: list[re.Match[str]] = []
    for pattern in (EN_SINGULAR_RE, CN_SINGULAR_RE):
        singular_matches.extend(pattern.finditer(segment))
    singular_matches.sort(key=lambda item: item.start())

    for match in singular_matches:
        if _overlaps(match.span(), occupied):
            continue
        figure_id = canonical_figure_id(match.group("figure"))
        display = match.group("display")
        normalized_display = normalize_figure_display(display)
        if normalized_display != display:
            report["normalized_figure_artifacts"] += 1
        report["figure_mentions"] += 1
        target = targets.get(figure_id)
        if target is None:
            _unresolved_entry(
                report,
                figure_id=figure_id,
                display=normalized_display,
                line_number=line_number,
                ambiguous=ambiguous,
            )
            if normalized_display != display:
                replacements.append((match.start(), match.end(), normalized_display))
        else:
            replacements.append((match.start(), match.end(), target.render_link(normalized_display)))
            report["figure_links_written"] += 1
        occupied.append(match.span())

    result = segment
    for start, end, replacement in sorted(replacements, reverse=True):
        result = result[:start] + replacement + result[end:]
    return result


def _link_eligible_line(
    line: str,
    *,
    line_number: int,
    targets: dict[str, FigureTarget],
    ambiguous: dict[str, Any],
    report: dict[str, Any],
) -> str:
    newline = ""
    content = line
    if line.endswith("\r\n"):
        content, newline = line[:-2], "\r\n"
    elif line.endswith("\n"):
        content, newline = line[:-1], "\n"

    spans = [match.span() for match in PROTECTED_INLINE_RE.finditer(content)]
    output: list[str] = []
    cursor = 0
    for start, end in spans:
        if start > cursor:
            output.append(
                _link_segment(
                    content[cursor:start],
                    line_number=line_number,
                    targets=targets,
                    ambiguous=ambiguous,
                    report=report,
                )
            )
        output.append(content[start:end])
        cursor = end
    if cursor < len(content):
        output.append(
            _link_segment(
                content[cursor:],
                line_number=line_number,
                targets=targets,
                ambiguous=ambiguous,
                report=report,
            )
        )
    return "".join(output) + newline


def normalize_figure_links(
    text: str,
    markdown_path: Path,
    vault_root: Path,
) -> tuple[str, dict[str, Any]]:
    text, orphan_fragment_links_removed = strip_orphan_figure_fragment_links(text)
    discovery = discover_figure_targets(text)
    targets: dict[str, FigureTarget] = discovery["targets"]
    ambiguous: dict[str, Any] = discovery["ambiguous"]
    report: dict[str, Any] = {
        "figure_targets": len(targets),
        "figure_mentions": 0,
        "figure_links_written": 0,
        "orphan_fragment_links_removed": orphan_fragment_links_removed,
        "normalized_figure_artifacts": 0,
        "unmatched": [],
        "ambiguous": ambiguous,
        "range_mentions": [],
        "targets": {
            figure_id: {
                "style": target.style,
                "target": target.target,
                "line": target.line,
            }
            for figure_id, target in sorted(targets.items())
        },
    }

    lines = text.splitlines(keepends=True)
    output: list[str] = []
    frontmatter = bool(lines and lines[0].strip() == "---")
    code_fence = False
    display_math = False
    html_table = False
    hidden_comment = False
    references = False

    for line_number, line in enumerate(lines, 1):
        stripped = line.strip()

        if frontmatter:
            output.append(line)
            if line_number > 1 and stripped == "---":
                frontmatter = False
            continue
        if CODE_FENCE_RE.match(stripped):
            code_fence = not code_fence
            output.append(line)
            continue
        if code_fence:
            output.append(line)
            continue
        if stripped == "%%":
            hidden_comment = not hidden_comment
            output.append(line)
            continue
        if stripped.startswith("%%") and not stripped.endswith("%%"):
            hidden_comment = True
            output.append(line)
            continue
        if hidden_comment:
            if stripped.endswith("%%"):
                hidden_comment = False
            output.append(line)
            continue
        if stripped == "$$":
            display_math = not display_math
            output.append(line)
            continue
        if display_math or (stripped.startswith("$$") and stripped.endswith("$$")):
            output.append(line)
            continue
        if re.search(r"<table\b", stripped, re.IGNORECASE):
            html_table = True
        if html_table:
            output.append(line)
            if re.search(r"</table>", stripped, re.IGNORECASE):
                html_table = False
            continue

        heading = HEADING_RE.match(stripped)
        if heading:
            title = heading.group("title").strip().lower()
            references = title in {"references", "bibliography", "\u53c2\u8003\u6587\u732e"}
            output.append(line)
            continue
        if references:
            output.append(line)
            continue
        if (
            not stripped
            or stripped.startswith("|")
            or parse_image_line(line, line_number)
            or caption_figure_id(line)
        ):
            output.append(line)
            continue

        output.append(
            _link_eligible_line(
                line,
                line_number=line_number,
                targets=targets,
                ambiguous=ambiguous,
                report=report,
            )
        )

    updated = "".join(output)
    report["changed"] = updated != text
    report["unmatched_figure_mentions"] = len(report["unmatched"])
    report["ambiguous_figure_targets"] = len(ambiguous)
    report["numeric_ranges"] = len(report["range_mentions"])
    return updated, report


def strip_orphan_figure_fragment_links(text: str) -> tuple[str, int]:
    """Remove webpage-only Figure links when their fragment anchor was not clipped."""
    anchors = figure_fragment_anchors(text)
    removed = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal removed
        display = match.group("display")
        target = match.group("target").strip()
        if not figure_id_from_display(display) or not target.lower().startswith("#fig-"):
            return match.group(0)
        if target[1:].lower() in anchors:
            return match.group(0)
        removed += 1
        return display

    return MARKDOWN_LINK_RE.sub(replace, text), removed


def figure_fragment_anchors(text: str) -> set[str]:
    """Return clipped fragment anchors that can legitimately satisfy #fig-* links."""
    anchors = {
        match.group("anchor").lower()
        for match in re.finditer(
            r'''(?ix)(?:id|name)\s*=\s*["'](?P<anchor>fig-[^"']+)["']''',
            text,
        )
    }
    anchors.update(
        match.group("anchor").lower()
        for match in re.finditer(r"(?im)\^(?P<anchor>fig-[A-Za-z0-9._-]+)\s*$", text)
    )
    return anchors


def _clean_link_target(raw: str) -> str:
    value = raw.strip()
    if value.startswith("<") and value.endswith(">"):
        value = value[1:-1].strip()
    return urllib.parse.unquote(value)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def resolve_attachment(
    markdown_path: Path,
    vault_root: Path,
    *,
    style: str,
    target: str,
) -> tuple[Path | None, str | None]:
    if style == "markdown":
        raw = _clean_link_target(target)
        if re.match(r"^(?:[A-Za-z]:[\\/]|/|https?:|data:)", raw, re.IGNORECASE):
            return None, "absolute_or_external"
        resolved = (markdown_path.parent / raw).resolve()
        if not _is_within(resolved, vault_root):
            return None, "outside_vault"
        return resolved, None if resolved.is_file() else "missing"

    raw = target.split("#", 1)[0].strip()
    if not raw:
        return None, "missing"
    if "/" in raw or "\\" in raw:
        resolved = (vault_root / raw.replace("\\", "/")).resolve()
        if not _is_within(resolved, vault_root):
            return None, "outside_vault"
        return resolved, None if resolved.is_file() else "missing"

    matches = [
        path.resolve()
        for path in vault_root.rglob(raw)
        if path.is_file() and not any(part in EXCLUDED_VAULT_PARTS for part in path.relative_to(vault_root).parts)
    ]
    unique = list(dict.fromkeys(matches))
    if len(unique) == 1:
        return unique[0], None
    return None, "ambiguous_wikilink" if len(unique) > 1 else "missing"


def figure_id_from_display(display: str) -> str | None:
    value = display.strip()
    for pattern in (EN_SINGULAR_RE, CN_SINGULAR_RE):
        match = pattern.fullmatch(value)
        if match:
            return canonical_figure_id(match.group("figure"))
    if re.fullmatch(FIGURE_ID_PATTERN, value, re.IGNORECASE):
        return canonical_figure_id(value)
    return None


def iter_figure_links(text: str) -> Iterable[tuple[str, str, str]]:
    """Yield body figure links while honoring the same protected regions as normalization."""
    lines = text.splitlines()
    frontmatter = bool(lines and lines[0].strip() == "---")
    code_fence = False
    display_math = False
    html_table = False
    hidden_comment = False
    references = False

    for line_number, line in enumerate(lines, 1):
        stripped = line.strip()
        if frontmatter:
            if line_number > 1 and stripped == "---":
                frontmatter = False
            continue
        if CODE_FENCE_RE.match(stripped):
            code_fence = not code_fence
            continue
        if code_fence:
            continue
        if stripped == "%%":
            hidden_comment = not hidden_comment
            continue
        if stripped.startswith("%%") and not stripped.endswith("%%"):
            hidden_comment = True
            continue
        if hidden_comment:
            if stripped.endswith("%%"):
                hidden_comment = False
            continue
        if stripped == "$$":
            display_math = not display_math
            continue
        if display_math or (stripped.startswith("$$") and stripped.endswith("$$")):
            continue
        if re.search(r"<table\b", stripped, re.IGNORECASE):
            html_table = True
        if html_table:
            if re.search(r"</table>", stripped, re.IGNORECASE):
                html_table = False
            continue
        heading = HEADING_RE.match(stripped)
        if heading:
            title = heading.group("title").strip().lower()
            references = title in {"references", "bibliography", "\u53c2\u8003\u6587\u732e"}
            continue
        if (
            references
            or not stripped
            or stripped.startswith("|")
            or parse_image_line(line, line_number)
            or caption_figure_id(line)
        ):
            continue
        for match in MARKDOWN_LINK_RE.finditer(line):
            figure_id = figure_id_from_display(match.group("display"))
            if figure_id:
                yield figure_id, "markdown", match.group("target")
        for match in WIKI_LINK_RE.finditer(line):
            figure_id = figure_id_from_display(match.group("display"))
            if figure_id:
                yield figure_id, "wiki", match.group("target")


def strip_figure_link_markup(text: str) -> str:
    """Return the same visible text with figure-reference link markup removed."""
    def strip_markdown(match: re.Match[str]) -> str:
        display = match.group("display")
        return display if figure_id_from_display(display) else match.group(0)

    def strip_wiki(match: re.Match[str]) -> str:
        display = match.group("display")
        return display if figure_id_from_display(display) else match.group(0)

    text = MARKDOWN_LINK_RE.sub(strip_markdown, text)
    return WIKI_LINK_RE.sub(strip_wiki, text)

def validate_figure_links(text: str, markdown_path: Path, vault_root: Path) -> dict[str, Any]:
    _, normalization = normalize_figure_links(text, markdown_path, vault_root)
    discovery = discover_figure_targets(text)
    targets: dict[str, FigureTarget] = discovery["targets"]
    errors: list[str] = []
    warnings: list[str] = []
    broken: list[dict[str, Any]] = []
    present = 0
    fragment_anchors = figure_fragment_anchors(text)

    resolved_expected: dict[str, Path] = {}
    for figure_id, target in targets.items():
        resolved, reason = resolve_attachment(
            markdown_path, vault_root, style=target.style, target=target.target
        )
        if resolved is not None and reason is None:
            resolved_expected[figure_id] = resolved

    for figure_id, style, target_text in iter_figure_links(text):
        present += 1
        if (
            style == "markdown"
            and target_text.lower().startswith("#fig-")
            and target_text[1:].lower() in fragment_anchors
        ):
            continue
        resolved, reason = resolve_attachment(
            markdown_path, vault_root, style=style, target=target_text
        )
        if reason or resolved is None:
            broken.append(
                {"figure": figure_id, "target": target_text, "reason": reason or "missing"}
            )
            continue
        if resolved.suffix.lower() not in IMAGE_EXTENSIONS:
            broken.append(
                {"figure": figure_id, "target": target_text, "reason": "not_image"}
            )
            continue
        expected = resolved_expected.get(figure_id)
        if expected is not None and resolved != expected:
            broken.append(
                {
                    "figure": figure_id,
                    "target": target_text,
                    "reason": "wrong_figure_target",
                    "expected": str(expected),
                }
            )

    if normalization["figure_links_written"]:
        warnings.append(
            "eligible figure mentions remain unlinked: "
            f"{normalization['figure_links_written']}"
        )
    if normalization["normalized_figure_artifacts"]:
        warnings.append(
            "malformed figure-reference spacing artifacts remain: "
            f"{normalization['normalized_figure_artifacts']}"
        )
    if broken:
        errors.extend(
            f"broken figure link for {item['figure']}: {item['reason']} ({item['target']})"
            for item in broken[:5]
        )
    if normalization["unmatched_figure_mentions"]:
        warnings.append(
            "figure mentions have no unique image target: "
            f"{normalization['unmatched_figure_mentions']}"
        )
    if normalization["ambiguous_figure_targets"]:
        warnings.append(
            "figures have ambiguous or multi-asset targets: "
            f"{normalization['ambiguous_figure_targets']}"
        )
    if normalization["numeric_ranges"]:
        warnings.append(
            "figure ranges were preserved without automatic expansion: "
            f"{normalization['numeric_ranges']}"
        )

    return {
        "ok": not errors,
        "figure_targets": normalization["figure_targets"],
        "figure_mentions": normalization["figure_mentions"] + present,
        "figure_links_written": present,
        "eligible_unlinked_figure_mentions": normalization["figure_links_written"],
        "malformed_figure_reference_artifacts": normalization["normalized_figure_artifacts"],
        "unmatched_figure_mentions": normalization["unmatched_figure_mentions"],
        "ambiguous_figure_targets": normalization["ambiguous_figure_targets"],
        "broken_figure_links": len(broken),
        "numeric_figure_ranges": normalization["numeric_ranges"],
        "errors": errors,
        "warnings": warnings,
        "details": {
            "unmatched": normalization["unmatched"],
            "ambiguous": normalization["ambiguous"],
            "broken": broken,
            "ranges": normalization["range_mentions"],
            "targets": normalization["targets"],
        },
    }


def collect_markdown_paths(args: argparse.Namespace, vault_root: Path) -> list[Path]:
    if args.markdown_path:
        paths = [Path(value).resolve() for value in args.markdown_path]
    else:
        directory = Path(args.directory).resolve()
        pattern = "**/*.md" if args.recursive else "*.md"
        if not directory.is_dir():
            raise ValueError(f"directory does not exist: {directory}")
        paths = sorted(path.resolve() for path in directory.glob(pattern) if path.is_file())
    for path in paths:
        if not path.is_file() or path.suffix.lower() != ".md":
            raise ValueError(f"not a Markdown file: {path}")
        if not _is_within(path, vault_root):
            raise ValueError(f"Markdown path is outside vault root: {path}")
    return list(dict.fromkeys(paths))


def migrate(args: argparse.Namespace) -> dict[str, Any]:
    vault_root = Path(args.vault_root).resolve()
    if not vault_root.is_dir():
        raise ValueError(f"vault root does not exist: {vault_root}")
    paths = collect_markdown_paths(args, vault_root)
    prepared: list[dict[str, Any]] = []

    for path in paths:
        source = path.read_text(encoding="utf-8")
        updated, report = normalize_figure_links(source, path, vault_root)
        validation = validate_figure_links(updated, path, vault_root)
        prepared.append(
            {
                "path": path,
                "source": source,
                "updated": updated,
                "report": report,
                "validation": validation,
            }
        )

    results: list[dict[str, Any]] = []
    for item in prepared:
        path: Path = item["path"]
        changed = item["updated"] != item["source"]
        written = False
        backup_path: Path | None = None
        errors = list(item["validation"]["errors"])
        if args.write and changed and not errors:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            backup_path = path.with_name(
                path.name + f".bak-{stamp}-before-figure-links"
            )
            shutil.copy2(path, backup_path)
            atomic_write_text(
                path,
                item["updated"],
                validator=validate_markdown_text,
                min_bytes=20,
            )
            written = True
        results.append(
            {
                "markdown_path": str(path),
                "changed": changed,
                "written": written,
                "backup_path": str(backup_path) if backup_path else None,
                **{
                    key: item["report"][key]
                    for key in (
                        "figure_targets",
                        "figure_mentions",
                        "figure_links_written",
                        "unmatched_figure_mentions",
                        "ambiguous_figure_targets",
                        "numeric_ranges",
                    )
                },
                "errors": errors,
                "warnings": item["validation"]["warnings"],
            }
        )

    return {
        "ok": all(not item["errors"] for item in results),
        "write": bool(args.write),
        "notes_scanned": len(results),
        "notes_changed": sum(bool(item["changed"]) for item in results),
        "notes_written": sum(bool(item["written"]) for item in results),
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault-root", required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--markdown-path", action="append")
    source.add_argument("--directory")
    parser.add_argument("--recursive", action="store_true")
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    if args.recursive and not args.directory:
        parser.error("--recursive requires --directory")
    result = migrate(args)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
