#!/usr/bin/env python3
"""Build an Obsidian paper reading card from Markdown without calling MinerU."""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from normalize_obsidian_citations import (
    classify_citation_footnote_labels,
    count_arxiv_subject_labels,
    count_named_citation_groups,
    normalize as normalize_citations,
    normalize_arxiv_subject_labels,
)
from normalize_obsidian_figure_links import normalize_figure_links
from normalize_web_clipping_artifacts import normalize_web_clipping_artifacts
from paper_translation_packet import (
    CONSTRAINTS_VERSION,
    LAYOUT_NAME,
    append_worker_attempt,
    append_validated_cache,
    build_packet,
    build_merge_template,
    build_pending_packet,
    combine_cached_and_worker_output,
    extract_reference_section,
    extract_translation_units,
    fill_merge_template,
    merge_translations,
    read_packet,
    sha256_text,
    update_workflow_state,
    validate_output,
)
from run_native_paper_translation_worker import prepare as prepare_native_subagent
from safe_atomic_io import (
    atomic_write_bytes,
    atomic_write_json,
    atomic_write_text,
    validate_markdown_text,
)
from sync_paper_translation_agent_prompt import (
    DEFAULT_MODEL,
    DEFAULT_REASONING_EFFORT,
    read_fragment,
    render_agent,
)
from sync_image_converter_alignments import synchronize as synchronize_image_converter
from validate_full_paper_card import validate_full
from validate_obsidian_paper_note import validate as validate_paper_note


IMAGE_RE = re.compile(r"!\[(?P<alt>[^\]]*)\]\((?P<path>[^)\r\n]+)\)")
MIXED_IMAGE_RE = re.compile(r"!\[\[(?P<alt>[^\]\r\n]+)\]\]\((?P<path>[^)\r\n]+)\)")
TABLE_RE = re.compile(r"(?is)<table\b[^>]*>.*?</table>")
H1_RE = re.compile(r"(?m)^#\s+(.+?)\s*$")
MOJIBAKE_MARKERS = ("鍙", "浜", "琛", "璁", "鏈", "鈥", "銆", "�", "Ã", "Â", "â€")
LIGATURES = str.maketrans({"ﬁ": "fi", "ﬂ": "fl", "ﬀ": "ff", "ﬃ": "ffi", "ﬄ": "ffl"})
PAPER_CARD_IMAGE_CLASS = "paper-card-centered-images"
NATIVE_SUBAGENT_BACKEND = "native-subagent"
DIRECT_CLI_BACKEND = "direct-cli"
REMOTE_ASSET_WORKERS = 6
DEFAULT_STABLE_RESOURCE_ROOTS = ("_resources", "_附件")
NONCRITICAL_REMOTE_ASSET_RE = re.compile(
    r"(?:^|[^a-z0-9])(?:logo|icon|avatar|badge|banner|social|share|cookie|menu|navigation|tracking[-_ ]?pixel|favicon)(?:[^a-z0-9]|$)",
    re.IGNORECASE,
)
FIGURE_ASSET_CONTEXT_RE = re.compile(
    r"(?:\bfig(?:ure)?\.?\s*[s]?\d+|图\s*[s]?\d+|diagram|schematic|plot|chart|result|experiment)",
    re.IGNORECASE,
)

STANDALONE_LINK_RE = re.compile(
    r'^\s*\[(?P<label>[^\]]+)\]\((?P<target>[^\s)]+)(?:\s+["\'](?P<title>.*?)["\'])?\)\s*$'
)
WEB_NAV_LABEL_RE = re.compile(
    r"^(?:skip to (?:main )?content|home|menu|contents?|table of contents|previous|next|"
    r"back to top|view (?:pdf|html)|download pdf|pdf|html|tex source|share|simple ai|ar5iv)$",
    re.IGNORECASE,
)
WEB_NOISE_BLOCK_RE = re.compile(r"^\s*<(?:nav|script|style|noscript)\b", re.IGNORECASE)
WEB_NOISE_BLOCK_END_RE = re.compile(r"</(?:nav|script|style|noscript)>\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class SourceContext:
    root: Path | None
    manifest: dict[str, Any] | None
    asset_map: dict[str, str]
    asset_sha256: dict[Path, str]
    source_pdf: Path | None
    layout_root: Path | None


def filter_web_clipping_noise(text: str) -> tuple[str, dict[str, Any]]:
    """Remove narrow, auditable website chrome without rewriting paper prose."""
    lines = text.splitlines(keepends=True)
    output: list[str] = []
    removed: list[dict[str, Any]] = []
    in_fence = False
    in_noise_block = False
    frontmatter_end = 0
    if lines and lines[0].lstrip("\ufeff").strip() == "---":
        for index in range(1, len(lines)):
            if lines[index].strip() == "---":
                frontmatter_end = index + 1
                break
    first_h1 = next(
        (index for index, line in enumerate(lines) if index >= frontmatter_end and re.match(r"^#\s+\S", line)),
        len(lines),
    )

    for index, line in enumerate(lines):
        stripped = line.strip()
        if re.match(r"^\s*```", line):
            in_fence = not in_fence
            output.append(line)
            continue
        if in_fence or index < frontmatter_end:
            output.append(line)
            continue
        if in_noise_block:
            removed.append({"line": index + 1, "reason": "html_site_chrome", "text": stripped[:160]})
            if WEB_NOISE_BLOCK_END_RE.search(line):
                in_noise_block = False
            continue
        if WEB_NOISE_BLOCK_RE.match(line):
            removed.append({"line": index + 1, "reason": "html_site_chrome", "text": stripped[:160]})
            if not WEB_NOISE_BLOCK_END_RE.search(line):
                in_noise_block = True
            continue

        link = STANDALONE_LINK_RE.fullmatch(stripped)
        if link:
            label = re.sub(r"\s+", " ", link.group("label")).strip()
            target = link.group("target").strip("<>")
            title = (link.group("title") or "").strip()
            reason: str | None = None
            if WEB_NAV_LABEL_RE.fullmatch(label):
                reason = "known_navigation_label"
            elif target.startswith("#") and ("‣" in title or "›" in title):
                reason = "captured_section_navigation"
            elif index < first_h1 and target.startswith(("#", "/")):
                reason = "pre_title_navigation"
            if reason:
                removed.append({"line": index + 1, "reason": reason, "text": stripped[:160]})
                continue
        output.append(line)

    filtered = "".join(output)
    return filtered, {
        "status": "completed",
        "removed_count": len(removed),
        "removed": removed[:20],
        "truncated": len(removed) > 20,
    }


def synchronize_paper_card_image_layout(
    *,
    output_note: Path,
    vault_root: Path,
    layout_root: Path | None,
    mode: str,
    overwrite_existing: bool,
) -> dict[str, Any]:
    if mode == "off":
        return {"status": "disabled", "blocking": False}
    try:
        report = synchronize_image_converter(
            vault_root=vault_root,
            markdown_path=output_note,
            layout_dir=layout_root,
            write=True,
            overwrite_existing=overwrite_existing,
        )
    except Exception as exc:
        return {
            "status": "failed",
            "blocking": False,
            "warning": f"Image Converter alignment sync failed: {exc}",
        }
    if not report.get("plugin_enabled"):
        return {
            "status": "skipped",
            "blocking": False,
            "reason": "Image Converter is not enabled in this vault",
        }
    report["status"] = "completed" if report.get("ok") else "failed"
    report["blocking"] = False
    return report


class SimpleTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self.unsupported = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "tr":
            if self._row is not None:
                self.unsupported = True
            self._row = []
        elif tag in {"td", "th"}:
            if self._row is None or self._cell is not None:
                self.unsupported = True
            if values.get("rowspan") not in {None, "1"} or values.get("colspan") not in {None, "1"}:
                self.unsupported = True
            self._cell = []
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._cell is not None and self._row is not None:
            self._row.append("".join(self._cell))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._cell is not None:
                self.unsupported = True
            if self._row:
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


@dataclass(frozen=True)
class SpanningCell:
    text: str
    rowspan: int
    colspan: int


class SpanningTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[SpanningCell]] = []
        self._row: list[SpanningCell] | None = None
        self._cell: list[str] | None = None
        self._rowspan = 1
        self._colspan = 1
        self._table_depth = 0
        self.unsupported = False
        self.expanded_spans = 0

    @staticmethod
    def span_value(attrs: dict[str, str | None], name: str) -> int:
        raw = attrs.get(name)
        if raw in {None, ""}:
            return 1
        try:
            value = int(str(raw))
        except ValueError:
            return 0
        return value if value > 0 else 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "table":
            self._table_depth += 1
            if self._table_depth > 1:
                self.unsupported = True
        elif tag == "tr":
            if self._row is not None:
                self.unsupported = True
            self._row = []
        elif tag in {"td", "th"}:
            if self._row is None or self._cell is not None:
                self.unsupported = True
            self._rowspan = self.span_value(values, "rowspan")
            self._colspan = self.span_value(values, "colspan")
            if not self._rowspan or not self._colspan:
                self.unsupported = True
            if self._rowspan > 1 or self._colspan > 1:
                self.expanded_spans += 1
            self._cell = []
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag == "table":
            self._table_depth = max(0, self._table_depth - 1)
        elif tag in {"td", "th"} and self._cell is not None and self._row is not None:
            self._row.append(
                SpanningCell("".join(self._cell), self._rowspan, self._colspan)
            )
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._cell is not None:
                self.unsupported = True
            if self._row:
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def json_report(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def slugify(value: str) -> str:
    ascii_words = re.findall(r"[A-Za-z0-9]+", value.lower())
    if ascii_words:
        slug = "-".join(ascii_words[:6])
    else:
        slug = "paper"
    return slug[:48].strip("-") or "paper"


def get_title(text: str, fallback: str) -> str:
    match = H1_RE.search(text)
    return match.group(1).strip() if match else fallback


def split_frontmatter(text: str) -> tuple[str, str]:
    match = re.match(r"(?s)^---\r?\n(.*?)\r?\n---\r?\n?", text)
    return (match.group(0), text[match.end() :]) if match else ("", text)


def ensure_card_frontmatter(text: str, fallback_title: str, translation_mode: str, stage: str) -> str:
    text = text.lstrip("\ufeff \t\r\n")
    frontmatter, body = split_frontmatter(text)
    title = get_title(body if frontmatter else text, fallback_title).replace('"', "'")
    if not H1_RE.search(body if frontmatter else text):
        body = f"# {title}\n\n" + (body if frontmatter else text).lstrip()
    if translation_mode == "bilingual":
        workflow = "controlled-worker-pending" if stage == "prepare" else "controlled-worker-completed"
        translation_status = "pending" if stage == "prepare" else "completed"
        translation_skill = "markdown-to-obsidian-paper-card"
    else:
        workflow = "not-requested"
        translation_status = "not-requested"
        translation_skill = "none"
    if frontmatter:
        raw = frontmatter[4 : frontmatter.rfind("\n---")]
        lines = raw.splitlines()

        def set_scalar(key: str, value: str) -> None:
            pattern = re.compile(rf"^{re.escape(key)}\s*:")
            for index, line in enumerate(lines):
                if pattern.match(line):
                    lines[index] = f'{key}: "{value}"'
                    return
            lines.append(f'{key}: "{value}"')

        def ensure_list_value(key: str, value: str) -> None:
            index = next((i for i, line in enumerate(lines) if re.match(rf"^{re.escape(key)}\s*:", line)), None)
            if index is None:
                lines.extend([f"{key}:", f"  - {value}"])
                return
            tail = lines[index].split(":", 1)[1].strip()
            if tail.startswith("[") and tail.endswith("]"):
                existing = [item.strip().strip("\"'") for item in tail[1:-1].split(",") if item.strip()]
                if value not in existing:
                    existing.append(value)
                lines[index] = f"{key}: [" + ", ".join(existing) + "]"
                return
            if tail and tail not in {"null", "~"}:
                existing_scalar = tail.strip("\"'")
                if existing_scalar == value:
                    return
                lines[index] = f"{key}:"
                lines[index + 1:index + 1] = [f"  - {existing_scalar}", f"  - {value}"]
                return
            if tail in {"null", "~"}:
                lines[index] = f"{key}:"
            child = index + 1
            while child < len(lines) and (not lines[child].strip() or lines[child].startswith((" ", "\t"))):
                match = re.match(r"^\s*-\s+(.+?)\s*$", lines[child])
                if match and match.group(1).strip("\"'") == value:
                    return
                child += 1
            lines.insert(index + 1, f"  - {value}")

        if not any(re.match(r"^title\s*:", line) for line in lines):
            lines.insert(0, f'title: "{title}"')
        aliases_index = next((i for i, line in enumerate(lines) if re.match(r"^aliases\s*:", line)), None)
        if aliases_index is None:
            lines.extend(["aliases:", f'  - "{title}"'])
        else:
            aliases_tail = lines[aliases_index].split(":", 1)[1].strip()
            if aliases_tail and aliases_tail not in {"[]", "null", "~"}:
                has_alias = True
            else:
                has_alias = False
            if aliases_tail in {"[]", "null", "~"}:
                lines[aliases_index] = "aliases:"
            next_index = aliases_index + 1
            while not has_alias and next_index < len(lines) and (not lines[next_index].strip() or lines[next_index].startswith((" ", "\t"))):
                if re.match(r"^\s*-\s+\S", lines[next_index]):
                    has_alias = True
                    break
                next_index += 1
            if not has_alias:
                lines.insert(aliases_index + 1, f'  - "{title}"')
        set_scalar("translation_workflow", workflow)
        set_scalar("translation_status", translation_status)
        set_scalar("translation_skill", translation_skill)
        ensure_list_value("cssclasses", PAPER_CARD_IMAGE_CLASS)
        if translation_mode == "bilingual":
            set_scalar("bilingual_layout", LAYOUT_NAME)
        return "---\n" + "\n".join(lines) + "\n---\n" + body
    metadata = (
        "---\n"
        f'title: "{title}"\n'
        "aliases:\n"
        f'  - "{title}"\n'
        "tags:\n"
        "  - paper\n"
        "cssclasses:\n"
        f"  - {PAPER_CARD_IMAGE_CLASS}\n"
        f'translation_workflow: "{workflow}"\n'
        f'translation_status: "{translation_status}"\n'
        f'translation_skill: "{translation_skill}"\n'
    )
    if translation_mode == "bilingual":
        metadata += f'bilingual_layout: "{LAYOUT_NAME}"\n'
    return metadata + "---\n\n" + body


def mojibake_score(text: str) -> int:
    return sum(text.count(marker) for marker in MOJIBAKE_MARKERS)


def repair_mojibake_safely(text: str) -> tuple[str, dict[str, int]]:
    normalized = unicodedata.normalize("NFC", text.translate(LIGATURES))
    repaired_lines: list[str] = []
    repaired_count = 0
    for line in normalized.splitlines(keepends=True):
        best = line
        best_score = mojibake_score(line)
        candidates: list[str] = []
        for encoding in ("cp1252", "latin1"):
            try:
                candidates.append(line.encode(encoding).decode("utf-8"))
            except (UnicodeEncodeError, UnicodeDecodeError):
                pass
        try:
            candidates.append(line.encode("gbk").decode("utf-8"))
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
        for candidate in candidates:
            score = mojibake_score(candidate)
            if "�" not in candidate and score < best_score:
                best = candidate
                best_score = score
        if best != line:
            repaired_count += 1
        repaired_lines.append(best)
    result = "".join(repaired_lines)
    return result, {"repaired_lines": repaired_count, "residual_markers": mojibake_score(result)}


def collapse_existing_bilingual_pairs(text: str) -> tuple[str, dict[str, int | bool]]:
    """Recover the English source layer when prepare receives an existing bilingual card."""
    frontmatter, body = split_frontmatter(text)
    parts = re.split(r"(\r?\n[ \t]*\r?\n)", body)
    candidates: list[tuple[int, int, str]] = []

    def unquote(block: str) -> str | None:
        lines = block.splitlines()
        if not lines or any(line.strip() and not line.lstrip().startswith(">") for line in lines):
            return None
        restored = [re.sub(r"^\s*> ?", "", line) for line in lines]
        value = "\n".join(restored).strip()
        return value or None

    content_indexes = [index for index in range(0, len(parts), 2) if parts[index].strip()]
    for position, index in enumerate(content_indexes[:-1]):
        english = unquote(parts[index])
        if english is None:
            continue
        next_index = content_indexes[position + 1]
        chinese = parts[next_index].strip()
        if re.search(r"[\u4e00-\u9fff]", chinese) and not chinese.startswith(("#", "!", "|", "```", "$$")):
            candidates.append((index, next_index, english))

    declared_bilingual = bool(re.search(rf"(?m)^bilingual_layout\s*:\s*[\"']?{re.escape(LAYOUT_NAME)}", frontmatter))
    if not declared_bilingual and len(candidates) < 3:
        return text, {"detected": False, "collapsed_pairs": 0}
    used_chinese: set[int] = set()
    collapsed = 0
    for english_index, chinese_index, english in candidates:
        if chinese_index in used_chinese:
            continue
        parts[english_index] = english
        parts[chinese_index] = ""
        used_chinese.add(chinese_index)
        collapsed += 1
    return frontmatter + "".join(parts), {"detected": collapsed > 0, "collapsed_pairs": collapsed}


def normalize_heading_levels(text: str) -> str:
    frontmatter, body = split_frontmatter(text)
    seen_title = False
    normalized: list[str] = []
    for line in body.splitlines(keepends=True):
        match = re.match(r"^(#{1,6})\s+(.+?)(\r?\n)?$", line)
        if match:
            level, title, ending = match.groups()
            is_first_h1 = level == "#" and not seen_title
            if is_first_h1:
                seen_title = True
            elif title.strip().lower() == "abstract" or level == "#":
                line = "## " + title + (ending or "")
        normalized.append(line)
    return frontmatter + "".join(normalized)


def normalize_mixed_image_syntax(text: str) -> tuple[str, dict[str, int]]:
    """Repair malformed Obsidian-alt plus Markdown-target image syntax."""
    normalized, count = MIXED_IMAGE_RE.subn(
        lambda match: f"![{match.group('alt')}]({match.group('path')})",
        text,
    )
    return normalized, {"normalized": count}


def normalize_table_cell(value: str) -> str:
    value = unescape(value)
    value = re.sub(r"\s+", " ", value).strip()
    value = value.replace("\\(", "$").replace("\\)", "$")
    value = value.replace("|", "\\|")
    return value


def html_table_to_markdown(fragment: str) -> str | None:
    parser = SpanningTableParser()
    parser.feed(fragment)
    parser.close()
    if parser.unsupported or len(parser.rows) < 2:
        return None
    active: dict[int, tuple[int, str]] = {}
    expanded_rows: list[list[str]] = []
    for source_row in parser.rows:
        occupied = {column: value for column, (_, value) in active.items()}
        next_active = {
            column: (remaining - 1, value)
            for column, (remaining, value) in active.items()
            if remaining > 1
        }
        column = 0
        for cell in source_row:
            while column in occupied:
                column += 1
            value = normalize_table_cell(cell.text)
            for offset in range(cell.colspan):
                target = column + offset
                if target in occupied:
                    return None
                occupied[target] = value
                if cell.rowspan > 1:
                    next_active[target] = (cell.rowspan - 1, value)
            column += cell.colspan
        if not occupied:
            return None
        width = max(occupied) + 1
        expanded_rows.append([occupied.get(index, "") for index in range(width)])
        active = next_active
    if active:
        return None
    width = max(len(row) for row in expanded_rows)
    rows = [row + [""] * (width - len(row)) for row in expanded_rows]
    lines = ["| " + " | ".join(rows[0]) + " |", "| " + " | ".join("---" for _ in rows[0]) + " |"]
    lines.extend("| " + " | ".join(row) + " |" for row in rows[1:])
    return "\n".join(lines)


def convert_html_tables(text: str) -> tuple[str, dict[str, int]]:
    stats = {"converted": 0, "span_tables_converted": 0, "needs_review": 0}

    def replace(match: re.Match[str]) -> str:
        converted = html_table_to_markdown(match.group(0))
        if converted is None:
            stats["needs_review"] += 1
            return match.group(0)
        stats["converted"] += 1
        if re.search(r"(?i)\b(?:rowspan|colspan)\s*=", match.group(0)):
            stats["span_tables_converted"] += 1
        return converted

    return TABLE_RE.sub(replace, text), stats


def load_source_context(source_package: str | None) -> SourceContext:
    if not source_package:
        return SourceContext(None, None, {}, {}, None, None)
    root = Path(source_package).resolve()
    manifest_path = root / "source-manifest.json"
    if not manifest_path.is_file():
        raise ValueError(f"source package lacks source-manifest.json: {root}")
    manifest = json.loads(read_text(manifest_path))
    if manifest.get("schema_version") != 1:
        raise ValueError(f"unsupported source package schema: {manifest.get('schema_version')}")
    artifacts = manifest.get("artifacts") or {}
    asset_map = {str(key).replace("\\", "/"): str(value).replace("\\", "/") for key, value in (artifacts.get("asset_map") or {}).items()}
    asset_sha256 = {
        (root / str(item["path"])).resolve(): str(item["sha256"])
        for item in ((manifest.get("integrity") or {}).get("assets") or [])
        if isinstance(item, dict) and item.get("path") and item.get("sha256")
    }
    source_pdf = root / artifacts["source_pdf"] if artifacts.get("source_pdf") else None
    if source_pdf and not source_pdf.is_file():
        raise ValueError(f"manifest source_pdf is missing: {source_pdf}")
    layout_root = root / "provenance" / "layout"
    return SourceContext(root, manifest, asset_map, asset_sha256, source_pdf, layout_root if layout_root.is_dir() else None)


def resolve_bibtex_path(explicit: str | None, context: SourceContext) -> Path | None:
    """Resolve an explicit bibliography or one unambiguous BibTeX file in the source package."""
    if explicit:
        candidate = Path(explicit).resolve()
        if not candidate.is_file():
            raise ValueError(f"BibTeX source does not exist: {candidate}")
        return candidate
    if not context.root:
        return None
    preferred = context.root / "references.bib"
    if preferred.is_file():
        return preferred.resolve()
    candidates = sorted(context.root.rglob("*.bib"))
    return candidates[0].resolve() if len(candidates) == 1 else None


def is_remote(path: str) -> bool:
    return bool(re.match(r"^(?:https?:|data:|#)", path, flags=re.IGNORECASE))


def resolve_resource_configuration(
    vault_root: Path,
    *,
    resource_directory: str,
    configured_roots: list[str] | tuple[str, ...] | None,
) -> tuple[Path, tuple[str, ...]]:
    raw_primary = Path(resource_directory)
    if raw_primary.is_absolute():
        raise ValueError("resource-directory must be vault-relative")
    primary = (vault_root / raw_primary).resolve()
    try:
        primary_relative = primary.relative_to(vault_root.resolve())
    except ValueError as exc:
        raise ValueError("resource-directory escapes the vault") from exc
    if primary == vault_root.resolve():
        raise ValueError("resource-directory cannot be the vault root")
    roots = [str(primary_relative).replace("\\", "/"), *DEFAULT_STABLE_RESOURCE_ROOTS]
    roots.extend(configured_roots or ())
    normalized: list[str] = []
    for raw in roots:
        candidate = Path(raw)
        if candidate.is_absolute():
            resolved = candidate.resolve()
        else:
            resolved = (vault_root / candidate).resolve()
        try:
            relative = resolved.relative_to(vault_root.resolve())
        except ValueError as exc:
            raise ValueError(f"stable resource root escapes the vault: {raw}") from exc
        if resolved == vault_root.resolve():
            raise ValueError(f"stable resource root cannot be the vault root: {raw}")
        value = str(relative).replace("\\", "/")
        if value not in normalized:
            normalized.append(value)
    return primary, tuple(normalized)


def remote_asset_is_critical(text: str, match: re.Match[str]) -> bool:
    alt = match.group("alt").strip()
    raw = match.group("path").strip()
    context = text[max(0, match.start() - 180) : min(len(text), match.end() + 260)]
    if FIGURE_ASSET_CONTEXT_RE.search(alt):
        return True
    if NONCRITICAL_REMOTE_ASSET_RE.search(alt) or NONCRITICAL_REMOTE_ASSET_RE.search(raw):
        return False
    if FIGURE_ASSET_CONTEXT_RE.search(context):
        return True
    return True


def load_remote_image(raw: str) -> tuple[bytes, str, str]:
    if raw.startswith("#"):
        raise ValueError("fragment-only image targets are unsupported")
    if raw.lower().startswith("data:"):
        header, separator, payload = raw.partition(",")
        if not separator:
            raise ValueError("invalid data URI")
        if ";base64" in header.lower():
            content = base64.b64decode(payload, validate=True)
        else:
            content = urllib.parse.unquote_to_bytes(payload)
        source_name = "embedded-image"
    else:
        request = urllib.request.Request(raw, headers={"User-Agent": "Codex-Obsidian-Paper-Card/1.0"})
        with urllib.request.urlopen(request, timeout=30) as response:
            announced = response.headers.get("Content-Length")
            if announced and int(announced) > 50 * 1024 * 1024:
                raise ValueError("remote image exceeds 50 MiB")
            content = response.read(50 * 1024 * 1024 + 1)
        if len(content) > 50 * 1024 * 1024:
            raise ValueError("remote image exceeds 50 MiB")
        source_name = Path(urllib.parse.urlparse(raw).path).stem or "remote-image"
    from PIL import Image

    with Image.open(io.BytesIO(content)) as image:
        image_format = (image.format or "").upper()
        image.verify()
    suffixes = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp", "GIF": ".gif", "TIFF": ".tiff", "BMP": ".bmp"}
    suffix = suffixes.get(image_format)
    if not suffix:
        raise ValueError(f"unsupported decoded image format: {image_format or 'unknown'}")
    return content, suffix, source_name


def resolve_local_asset(raw_path: str, input_markdown: Path, context: SourceContext) -> Path | None:
    normalized = raw_path.strip().strip("<>").replace("\\", "/")
    if is_remote(normalized):
        return None
    normalized = normalized.removeprefix("./")
    candidates: list[Path] = []
    if context.root and normalized in context.asset_map:
        candidates.append(context.root / context.asset_map[normalized])
    candidates.append(input_markdown.parent / normalized)
    if context.root:
        candidates.append(context.root / normalized)
        assets_dir = (context.manifest or {}).get("artifacts", {}).get("assets_dir", "assets")
        candidates.append(context.root / assets_dir / Path(normalized).name)
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return None


def relative_markdown_path(output_note: Path, target: Path) -> str:
    return os.path.relpath(target, output_note.parent).replace("\\", "/")


def prepare_assets(
    text: str,
    input_markdown: Path,
    output_note: Path,
    resource_root: Path,
    context: SourceContext,
    write: bool,
    *,
    remote_mode: str = "download",
    remote_cache_path: Path | None = None,
) -> tuple[str, list[dict[str, Any]], list[str]]:
    if remote_mode not in {"download", "defer"}:
        raise ValueError(f"unsupported remote asset mode: {remote_mode}")
    title_slug = slugify(get_title(text, output_note.stem))
    copied: dict[Path, Path] = {}
    asset_report: list[dict[str, Any]] = []
    unresolved: list[str] = []
    remote_results: dict[str, dict[str, Any]] = {}
    cache_entries: dict[str, dict[str, Any]] = {}
    if remote_cache_path and remote_cache_path.is_file():
        try:
            cached_payload = json.loads(remote_cache_path.read_text(encoding="utf-8"))
            if cached_payload.get("schema_version") == 1 and isinstance(cached_payload.get("entries"), dict):
                cache_entries = dict(cached_payload["entries"])
        except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
            cache_entries = {}

    remote_criticality: dict[str, bool] = {}
    for image_match in IMAGE_RE.finditer(text):
        raw_remote = image_match.group("path").strip()
        if is_remote(raw_remote):
            remote_criticality[raw_remote] = remote_criticality.get(raw_remote, False) or remote_asset_is_critical(
                text, image_match
            )
    unique_remote = [raw for raw, critical in remote_criticality.items() if critical]
    if write and remote_mode == "download" and unique_remote:
        pending: list[str] = []
        for raw in unique_remote:
            cache_key = hashlib.sha256(raw.encode("utf-8")).hexdigest()
            cached = cache_entries.get(cache_key) or {}
            target_name = cached.get("target_name")
            digest = cached.get("sha256")
            target = resource_root / str(target_name) if target_name else None
            if (
                target is not None
                and target.parent.resolve() == resource_root.resolve()
                and target.is_file()
                and isinstance(digest, str)
                and sha256(target) == digest
            ):
                remote_results[raw] = {
                    "ok": True,
                    "target": target,
                    "sha256": digest,
                    "status": "remote_cache_hit",
                    "download_ms": 0,
                    "source_url_sha256": cache_key,
                }
            else:
                pending.append(raw)

        def fetch(raw: str) -> tuple[str, dict[str, Any]]:
            started = time.perf_counter()
            cache_key = hashlib.sha256(raw.encode("utf-8")).hexdigest()
            try:
                content, suffix, source_name = load_remote_image(raw)
                return raw, {
                    "ok": True,
                    "content": content,
                    "suffix": suffix,
                    "source_name": source_name,
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "download_ms": round((time.perf_counter() - started) * 1000),
                    "source_url_sha256": cache_key,
                }
            except Exception as exc:
                return raw, {
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "download_ms": round((time.perf_counter() - started) * 1000),
                    "source_url_sha256": cache_key,
                }

        if pending:
            with ThreadPoolExecutor(max_workers=min(REMOTE_ASSET_WORKERS, len(pending))) as executor:
                futures = {executor.submit(fetch, raw): raw for raw in pending}
                for future in as_completed(futures):
                    raw, result = future.result()
                    remote_results[raw] = result

    def replace(match: re.Match[str]) -> str:
        raw = match.group("path").strip()
        if is_remote(raw):
            display_source = "<data-uri>" if raw.lower().startswith("data:") else raw
            critical = remote_criticality.get(raw, True)
            if not critical:
                asset_report.append(
                    {
                        "source": display_source,
                        "status": "remote_noncritical_removed" if write else "remote_noncritical_would_remove",
                        "criticality": "noncritical",
                        "source_url_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
                    }
                )
                return "" if write else match.group(0)
            if not write:
                asset_report.append({"source": display_source, "status": "external_requires_write", "criticality": "critical"})
                return match.group(0)
            if remote_mode == "defer":
                asset_report.append(
                    {
                        "source": display_source,
                        "status": "remote_pending",
                        "criticality": "critical",
                        "source_url_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
                    }
                )
                return match.group(0)
            result = remote_results.get(raw) or {
                "ok": False,
                "error": "remote asset result is missing",
                "download_ms": 0,
                "source_url_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
            }
            if not result.get("ok"):
                unresolved.append(display_source)
                asset_report.append(
                    {
                        "source": display_source,
                        "status": "remote_error",
                        "criticality": "critical",
                        "error": result.get("error"),
                        "download_ms": result.get("download_ms", 0),
                        "source_url_sha256": result.get("source_url_sha256"),
                    }
                )
                return match.group(0)
            target = result.get("target")
            method = "reused"
            if target is None:
                content = bytes(result["content"])
                digest = str(result["sha256"])
                target = resource_root / (
                    f"{title_slug}-{slugify(str(result['source_name']))}-{digest[:10]}{result['suffix']}"
                )
                resource_root.mkdir(parents=True, exist_ok=True)
                method = "reused" if target.is_file() else atomic_write_bytes(target, content, min_bytes=8)
                result["target"] = target
                result["status"] = "downloaded"
                cache_entries[str(result["source_url_sha256"])] = {
                    "target_name": target.name,
                    "sha256": digest,
                }
            asset_report.append(
                {
                    "source": display_source,
                    "target": str(target),
                    "status": result.get("status", "downloaded"),
                    "criticality": "critical",
                    "sha256": result.get("sha256"),
                    "write_method": method,
                    "download_ms": result.get("download_ms", 0),
                    "source_url_sha256": result.get("source_url_sha256"),
                }
            )
            return f"![{match.group('alt')}]({relative_markdown_path(output_note, Path(target))})"
        source = resolve_local_asset(raw, input_markdown, context)
        if source is None:
            unresolved.append(raw)
            asset_report.append({"source": raw, "status": "unresolved"})
            return match.group(0)
        try:
            source.relative_to(resource_root.resolve())
        except ValueError:
            pass
        else:
            asset_report.append({"source": str(source), "target": str(source), "status": "already_archived"})
            return f"![{match.group('alt')}]({relative_markdown_path(output_note, source)})"
        target = copied.get(source)
        if target is None:
            suffix = source.suffix.lower() or ".bin"
            source_digest = context.asset_sha256.get(source.resolve()) or sha256(source)
            target = resource_root / f"{title_slug}-{source.stem}-{source_digest[:10]}{suffix}"
            copied[source] = target
            asset_report.append({"source": str(source), "target": str(target), "status": "copied" if write else "would_copy"})
            if write:
                resource_root.mkdir(parents=True, exist_ok=True)
                if not target.exists():
                    atomic_write_bytes(target, source.read_bytes(), min_bytes=8)
        return f"![{match.group('alt')}]({relative_markdown_path(output_note, target)})"

    prepared = IMAGE_RE.sub(replace, text)
    if remote_cache_path and write and remote_mode == "download" and cache_entries:
        atomic_write_json(
            remote_cache_path,
            {"schema_version": 1, "entries": cache_entries},
        )
    return prepared, asset_report, unresolved


def run_figure_crop(
    staging_note: Path,
    resource_root: Path,
    context: SourceContext,
    asset_prefix: str,
    asset_report: list[dict[str, Any]] | None = None,
    *,
    cache_path: Path | None = None,
) -> dict[str, Any]:
    if not context.source_pdf or not context.layout_root:
        return {"status": "skipped", "reason": "source package has no source_pdf and layout provenance"}
    helper = Path(__file__).with_name("postprocess_mineru_figure_crops.py")
    asset_map_path = staging_note.with_name(staging_note.name + ".asset-map.json")
    asset_map = {
        Path(str(item["target"])).name: Path(str(item["source"])).name
        for item in (asset_report or [])
        if item.get("source") and item.get("target") and item.get("status") in {"copied", "would_copy", "already_archived"}
    }
    atomic_write_json(asset_map_path, asset_map)
    command = [
        sys.executable,
        str(helper),
        "--markdown-path", str(staging_note),
        "--resource-root", str(resource_root),
        "--asset-prefix", asset_prefix,
        "--source-pdf", str(context.source_pdf),
        "--layout-dir", str(context.layout_root),
        "--asset-map", str(asset_map_path),
    ]
    if cache_path is not None:
        command.extend(["--cache-path", str(cache_path)])
    completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
    asset_map_path.unlink(missing_ok=True)
    if completed.returncode != 0:
        return {"status": "skipped", "reason": "figure crop helper failed", "stderr": completed.stderr.strip()}
    try:
        return {"status": "completed", "result": json.loads(completed.stdout)}
    except json.JSONDecodeError:
        return {"status": "completed", "stdout": completed.stdout.strip()}


def run_concept_links(markdown_path: Path, vault_root: Path, mode: str) -> dict[str, Any]:
    if mode == "off":
        return {"status": "disabled"}
    helper = Path(__file__).with_name("auto_link_existing_concepts.py")
    index_cache = vault_root / ".tmp" / "paper-card-cache" / "concept-index-v1.json"
    command = [sys.executable, str(helper), "--vault-root", str(vault_root), "--markdown-path", str(markdown_path), "--index-cache", str(index_cache)]
    if mode == "write":
        command.append("--write")
    completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
    if completed.returncode != 0:
        raise RuntimeError(f"concept auto-link failed: {completed.stderr.strip() or completed.stdout.strip()}")
    return json.loads(completed.stdout)


def reanchor_archived_images(text: str, source_markdown: Path, output_note: Path, resource_root: Path) -> str:
    def replace(match: re.Match[str]) -> str:
        raw = match.group("path").strip()
        if is_remote(raw):
            return match.group(0)
        candidate = (source_markdown.parent / raw.strip("<>")).resolve()
        try:
            candidate.relative_to(resource_root.resolve())
        except ValueError:
            return match.group(0)
        if not candidate.is_file():
            return match.group(0)
        return f"![{match.group('alt')}]({relative_markdown_path(output_note, candidate)})"
    return IMAGE_RE.sub(replace, text)


def write_atomic(path: Path, content: str) -> None:
    atomic_write_text(path, content, min_bytes=20)


def translation_identity() -> dict[str, str]:
    fragment_path = Path(__file__).resolve().parents[1] / "references" / "paper-translation-prompt-fragment.md"
    fragment = read_fragment(fragment_path)
    _, prompt_sha256, translator_fingerprint = render_agent(
        fragment,
        model=DEFAULT_MODEL,
        reasoning_effort=DEFAULT_REASONING_EFFORT,
    )
    return {
        "agent": "paper-translation-worker",
        "model": DEFAULT_MODEL,
        "reasoning_effort": DEFAULT_REASONING_EFFORT,
        "quality_tier": "publication",
        "prompt_sha256": prompt_sha256,
        "translator_fingerprint": translator_fingerprint,
        "constraints_version": CONSTRAINTS_VERSION,
    }


def write_assignment_manifest(
    path: Path,
    *,
    packet_path: Path,
    output_path: Path,
    unit_count: int,
    packet_sha256: str,
    identity: dict[str, str],
) -> dict[str, Any]:
    assignment = {
        "schema_version": 3,
        "packet_path": str(packet_path.resolve()),
        "output_path": str(output_path.resolve()),
        "unit_count": unit_count,
        "packet_sha256": packet_sha256,
        "prompt_sha256": identity["prompt_sha256"],
        "translator_fingerprint": identity["translator_fingerprint"],
        "constraints_version": identity["constraints_version"],
        "requested_runtime": {
            "model": identity["model"],
            "reasoning_effort": identity["reasoning_effort"],
            "quality_tier": identity["quality_tier"],
        },
    }
    atomic_write_json(path, assignment)
    return assignment


def compact_cli_report(report: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "ok",
        "stage",
        "status",
        "worker_backend",
        "ready_for_translation",
        "citation_residual",
        "remote_images",
        "html_tables",
        "mojibake",
        "invalid_image_paths",
        "frontmatter_errors",
        "translation_units",
        "body_translation_units",
        "passthrough_units",
        "layout_units_total",
        "reference_title_units",
        "reference_title_fallback_units",
        "model_units_total",
        "cached_units",
        "worker_units",
        "cached_reference_title_units",
        "worker_reference_title_units",
        "reference_blocks",
        "images",
        "citation_links",
        "figure_targets",
        "figure_mentions",
        "figure_links_written",
        "unmatched_figure_mentions",
        "ambiguous_figure_targets",
        "broken_figure_links",
        "protected_token_errors",
        "placeholders",
        "runtime_model_verified",
        "runtime_source",
        "errors",
        "output_bytes",
        "output_sha256",
        "input_sha256",
        "prepared_sha256",
        "packet_sha256",
        "layout_template_sha256",
        "assignment_path",
        "assignment_sha256",
        "packet_path",
        "agent_role",
        "agent_role_path",
        "agent_role_sha256",
        "task_name",
        "agent_path",
        "spawn_agent",
        "output_note",
        "report_path",
        "error",
    )
    compact = {key: report[key] for key in keys if key in report}
    if "warnings_count" in report:
        compact["warnings"] = report["warnings_count"]
    if isinstance(report.get("errors"), list) and report["errors"]:
        compact["errors"] = list(report["errors"][:3])
    if "warnings_count" not in report and isinstance(report.get("warnings"), list) and report["warnings"]:
        compact["warnings"] = list(report["warnings"][:3])
    return compact


def backup_existing(
    path: Path,
    label: str,
    *,
    replacement_text: str | None = None,
    max_backups: int = 2,
) -> Path | None:
    if not path.is_file():
        return None
    if replacement_text is not None and path.read_bytes() == replacement_text.encode("utf-8"):
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = path.with_name(path.name + f".bak-{stamp}-{label}")
    shutil.copy2(path, backup)
    matching = sorted(
        path.parent.glob(path.name + f".bak-*-{label}"),
        key=lambda candidate: (candidate.stat().st_mtime_ns, candidate.name),
        reverse=True,
    )
    for stale in matching[max(1, max_backups) :]:
        stale.unlink(missing_ok=True)
    return backup


def compact_completed_workflow(workflow_dir: Path) -> dict[str, Any]:
    """Retain recovery evidence while deleting successful-run transport artifacts."""
    state_path = workflow_dir / "workflow-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
    attestation_name = str((state.get("artifacts") or {}).get("translation_runtime_attestation") or "")
    keep_names = {
        "workflow-state.json",
        "translation-cache.jsonl",
        "translation-validation-report.json",
        "layout-asset-report.json",
        "remote-asset-cache.json",
        "figure-crop-cache.json",
        "translation-agent-role-snapshot.toml",
        attestation_name,
    }
    disposable_names = {
        "translation-packet.json",
        "translation-pending-packet.jsonl",
        "translation-assignment.json",
        "translation-cached-output.jsonl",
        "translation-output.jsonl",
        "translation-combined-output.jsonl",
        "merge-template.md",
        "translation-native-final.schema.json",
        "preflight-report.json",
        "layout-report.json",
    }
    candidates = [workflow_dir / name for name in disposable_names]
    for pattern in (
        "translation-native-attempt-*-assignment.json",
        "translation-native-attempt-*-input.jsonl",
        "translation-native-attempt-*-partial.jsonl",
        "translation-native-attempt-*-attestation.json",
        "translation-worker-attempt-*-assignment.json",
        "translation-worker-attempt-*-input.jsonl",
        "translation-worker-attempt-*-output.jsonl",
        "translation-worker-attempt-*-attestation.json",
        "translation-worker-attempt-*.log",
    ):
        candidates.extend(workflow_dir.glob(pattern))
    removed: list[str] = []
    for candidate in dict.fromkeys(candidates):
        if candidate.name in keep_names or not candidate.is_file():
            continue
        candidate.unlink()
        removed.append(candidate.name)
    retained = sorted(path.name for path in workflow_dir.iterdir() if path.is_file())
    state["retention"] = {
        "policy": "completed_compact_v1",
        "removed_artifacts": sorted(removed),
        "retained_artifacts": retained,
    }
    atomic_write_json(state_path, state)
    return state["retention"]


def run_optional_concept_links(
    output_note: Path,
    vault_root: Path,
    mode: str,
    *,
    workflow_state: Path | None = None,
    translation_packet: Path | None = None,
    stable_resource_roots: tuple[str, ...] | list[str] | None = None,
) -> dict[str, Any]:
    if mode == "off":
        return {"status": "disabled", "blocking": False}
    if mode == "report":
        try:
            result = run_concept_links(output_note, vault_root, "report")
            return {"status": "completed", "blocking": False, "result": result}
        except Exception as exc:
            return {"status": "failed", "blocking": False, "warning": f"{type(exc).__name__}: {exc}"}
    candidate = output_note.with_name(f".{output_note.name}.concept-staging.md")
    try:
        shutil.copy2(output_note, candidate)
        result = run_concept_links(candidate, vault_root, "write")
        if translation_packet and translation_packet.is_file():
            validation = validate_full(
                candidate,
                vault_root,
                stage="final",
                workflow_state_path=workflow_state,
                translation_packet_path=translation_packet,
                validation_mode="pipeline_finalize",
                stable_resource_roots=stable_resource_roots,
            )
        else:
            validation = validate_paper_note(
                candidate,
                vault_root,
                stable_resource_roots=stable_resource_roots,
            )
        if not validation["ok"]:
            return {
                "status": "failed",
                "blocking": False,
                "warning": "concept-link candidate failed validation",
                "errors": validation["errors"][:3],
            }
        atomic_write_text(
            output_note,
            candidate.read_text(encoding="utf-8"),
            validator=validate_markdown_text,
            min_bytes=20,
        )
        return {"status": "completed", "blocking": False, "result": result}
    except Exception as exc:
        return {"status": "failed", "blocking": False, "warning": f"{type(exc).__name__}: {exc}"}
    finally:
        candidate.unlink(missing_ok=True)


def should_retry_translation_attempt(attempt: dict[str, Any], attempts_remaining: int) -> bool:
    """Allow one bounded sequential retry only for failures declared retryable by the runner."""
    return bool(attempt.get("retryable")) and attempts_remaining > 1


def build(args: argparse.Namespace) -> dict[str, Any]:
    input_markdown = Path(args.input_markdown).resolve()
    vault_root = Path(args.vault_root).resolve()
    output_note = Path(args.output_note).resolve()
    if not input_markdown.is_file():
        raise ValueError(f"input Markdown does not exist: {input_markdown}")
    if not vault_root.is_dir():
        raise ValueError(f"vault root does not exist: {vault_root}")
    resource_root, stable_resource_roots = resolve_resource_configuration(
        vault_root,
        resource_directory=str(getattr(args, "resource_directory", "_resources")),
        configured_roots=getattr(args, "stable_resource_roots", None),
    )
    try:
        output_note.relative_to(vault_root)
    except ValueError as exc:
        raise ValueError("output note must be located inside vault root") from exc
    if args.in_place and output_note != input_markdown:
        raise ValueError("--in-place requires --output-note to equal --input-markdown")
    if output_note == input_markdown and not args.in_place:
        raise ValueError("refusing to overwrite input without --in-place")

    workflow_dir = Path(args.workflow_dir).resolve() if args.workflow_dir else None
    if args.translation_mode == "bilingual" and not workflow_dir:
        raise ValueError("bilingual mode requires --workflow-dir")

    if workflow_dir and args.translation_mode == "bilingual" and args.translation_stage == "run":
        prepare_args = argparse.Namespace(**vars(args))
        prepare_args.translation_stage = "prepare"
        prepare_report = build(prepare_args)
        assignment_path = Path(str(prepare_report["assignment_path"])).resolve()
        assignment = json.loads(assignment_path.read_text(encoding="utf-8"))
        state_path = workflow_dir / "workflow-state.json"
        worker_backend = str(
            getattr(args, "worker_backend", NATIVE_SUBAGENT_BACKEND)
        )
        if worker_backend not in {NATIVE_SUBAGENT_BACKEND, DIRECT_CLI_BACKEND}:
            raise ValueError(f"unsupported worker backend: {worker_backend}")
        if (
            worker_backend == NATIVE_SUBAGENT_BACKEND
            and int(assignment["unit_count"]) > 0
        ):
            configured_role = getattr(args, "translation_agent_role_file", None)
            role_path = (
                Path(str(configured_role)).resolve()
                if configured_role
                else vault_root / ".codex" / "agents" / "paper-translation-worker.toml"
            )
            handoff = prepare_native_subagent(
                argparse.Namespace(
                    assignment=str(assignment_path),
                    workflow_dir=str(workflow_dir),
                    agent_role_file=str(role_path),
                    agent_role="paper-translation-worker",
                    task_name=getattr(args, "translation_agent_task_name", None),
                )
            )
            return {
                **handoff,
                "status": "awaiting_native_subagent",
                "ready_for_translation": True,
                "translation_units": prepare_report["translation_units"],
                "body_translation_units": prepare_report[
                    "body_translation_units"
                ],
                "passthrough_units": prepare_report["passthrough_units"],
                "layout_units_total": prepare_report["layout_units_total"],
                "reference_title_units": prepare_report["reference_title_units"],
                "reference_title_fallback_units": prepare_report[
                    "reference_title_fallback_units"
                ],
                "model_units_total": prepare_report["model_units_total"],
                "translation_cache": prepare_report["translation_cache"],
            }
        state_before_worker = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
        prior_attempts = (((state_before_worker.get("stages") or {}).get("translation_worker") or {}).get("attempts") or [])
        expected_attestation_path = workflow_dir / f"translation-worker-attempt-{len(prior_attempts) + 1}-attestation.json"
        worker_process: subprocess.Popen[str] | None = None
        runner_command: list[str] | None = None
        if int(assignment["unit_count"]) > 0:
            runner = Path(__file__).resolve().parent / "run_paper_translation_worker.py"
            runner_command = [
                sys.executable,
                str(runner),
                "--assignment",
                str(assignment_path),
                "--workflow-dir",
                str(workflow_dir),
                "--model",
                DEFAULT_MODEL,
                "--reasoning-effort",
                DEFAULT_REASONING_EFFORT,
                "--timeout-seconds",
                str(int(getattr(args, "worker_timeout_seconds", 1800))),
            ]
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            worker_process = subprocess.Popen(
                runner_command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=workflow_dir,
                creationflags=creationflags,
            )

        layout_args = argparse.Namespace(**vars(args))
        layout_args.translation_stage = "layout"
        build(layout_args)

        if worker_process is not None:
            runner_stdout, _ = worker_process.communicate(
                timeout=int(getattr(args, "worker_timeout_seconds", 1800)) + 90
            )
            runner_rows = [line for line in runner_stdout.splitlines() if line.strip().startswith("{")]
            runner_report: dict[str, Any] = {}
            if runner_rows:
                try:
                    parsed = json.loads(runner_rows[-1])
                    if isinstance(parsed, dict):
                        runner_report = parsed
                except json.JSONDecodeError:
                    runner_report = {}
            reported_attestation = runner_report.get("attestation_path")
            attestation_path = Path(str(reported_attestation)).resolve() if reported_attestation else expected_attestation_path
            attestation = json.loads(attestation_path.read_text(encoding="utf-8")) if attestation_path.is_file() else {}
            actual = attestation.get("actual") or {}
            attempt = {
                "pending_units": int(assignment["unit_count"]),
                "started_at": attestation.get("started_at"),
                "finished_at": attestation.get("finished_at", datetime.now().astimezone().isoformat()),
                "duration_ms": attestation.get("duration_ms"),
                "requested_model": DEFAULT_MODEL,
                "actual_model": actual.get("model"),
                "requested_reasoning_effort": DEFAULT_REASONING_EFFORT,
                "actual_reasoning_effort": actual.get("reasoning_effort"),
                "runtime_verified": actual.get("runtime_verified", False),
                "output_sha256": attestation.get("output_sha256"),
                "exit_code": attestation.get("exit_code", worker_process.returncode),
                "completion_waits": 1,
                "transport": attestation.get("transport"),
                "failure_stage": attestation.get("failure_stage"),
                "failure_class": attestation.get("failure_class"),
                "retryable": attestation.get("retryable", False),
                "failure_reasons": attestation.get("failure_reasons", []),
                "contract_errors": attestation.get("contract_errors", []),
                "unit_errors": attestation.get("unit_errors", {}),
                "partial_validated_units": attestation.get("partial_validated_units", 0),
                "invalid_unit_ids": attestation.get("invalid_unit_ids", []),
                "invalid_unit_count": attestation.get("invalid_unit_count", 0),
                "unit_failure_threshold": attestation.get("unit_failure_threshold", 5),
                "overall_failed": attestation.get("overall_failed", False),
                "partial_output_sha256": attestation.get("partial_output_sha256"),
            }
            success = worker_process.returncode == 0 and runner_report.get("ok") is True and attempt["runtime_verified"] is True
            append_worker_attempt(state_path, attempt, status="completed" if success else "failed")
            state = json.loads(state_path.read_text(encoding="utf-8"))
            translator = dict(state.get("translator") or {})
            translator["actual"] = actual
            state["translator"] = translator
            state.setdefault("artifacts", {})["translation_runtime_attestation"] = attestation_path.name
            if attestation_path.is_file():
                state["translation_runtime_attestation_sha256"] = sha256(attestation_path)
            atomic_write_json(state_path, state)
            if not success:
                partial_path_value = attestation.get("partial_output_path")
                partial_path = Path(str(partial_path_value)).resolve() if partial_path_value else None
                if (
                    partial_path is not None
                    and partial_path.is_file()
                    and partial_path.parent == workflow_dir.resolve()
                    and attestation.get("partial_output_sha256") == sha256(partial_path)
                    and actual.get("runtime_verified") is True
                    and actual.get("model") == DEFAULT_MODEL
                    and str(actual.get("reasoning_effort", "")).lower() == DEFAULT_REASONING_EFFORT.lower()
                ):
                    full_packet = read_packet(workflow_dir / "translation-packet.json")
                    partial_ids = {
                        str(row.get("unit_id"))
                        for row in (
                            json.loads(line)
                            for line in partial_path.read_text(encoding="utf-8").splitlines()
                            if line.strip()
                        )
                        if isinstance(row, dict)
                    }
                    partial_packet = {
                        **full_packet,
                        "units": [unit for unit in full_packet["units"] if str(unit["unit_id"]) in partial_ids],
                    }
                    partial_translations, partial_errors = validate_output(partial_packet, partial_path)
                    if not partial_errors and len(partial_translations) == len(partial_ids):
                        append_validated_cache(
                            workflow_dir / "translation-cache.jsonl",
                            full_packet,
                            partial_translations,
                            translation_identity()["translator_fingerprint"],
                            actual,
                        )
                attempts_remaining = int(getattr(args, "_worker_attempts_remaining", getattr(args, "worker_max_attempts", 2)))
                if should_retry_translation_attempt(attempt, attempts_remaining):
                    retry_args = argparse.Namespace(**vars(args))
                    retry_args._worker_attempts_remaining = attempts_remaining - 1
                    return build(retry_args)
                raise RuntimeError(
                    "direct Terra translation worker failed: "
                    + str(runner_report.get("failure_class") or runner_report.get("error") or runner_stdout[-1000:])
                    + "; automatic retry budget exhausted; rerun the same translation-stage=run command to resume from validated cache"
                )
        else:
            update_workflow_state(
                state_path,
                stage="translation_worker",
                status="completed",
                metadata={"pending_count": 0, "cached_count": prepare_report["translation_cache"]["cached_count"], "completion_waits": 0, "skipped": "cache_hit"},
            )

        finalize_args = argparse.Namespace(**vars(args))
        finalize_args.translation_stage = "finalize"
        return build(finalize_args)

    if workflow_dir and args.translation_mode == "bilingual" and args.translation_stage == "layout":
        prepared = output_note.with_name(f".{output_note.name}.translation-prepared.md")
        packet_path = workflow_dir / "translation-packet.json"
        template_path = workflow_dir / "merge-template.md"
        state_path = workflow_dir / "workflow-state.json"
        if not prepared.is_file() or not packet_path.is_file():
            raise ValueError("layout requires prepared Markdown and translation packet")
        packet = read_packet(packet_path)
        if packet["source_markdown_sha256"] != sha256_text(read_text(prepared)):
            raise ValueError("prepared Markdown hash no longer matches translation packet")
        started = time.perf_counter()
        template = build_merge_template(prepared, packet)
        remote_cache_path = workflow_dir / "remote-asset-cache.json"
        template, layout_assets, layout_unresolved = prepare_assets(
            template,
            input_markdown,
            output_note,
            resource_root,
            load_source_context(args.source_package),
            True,
            remote_mode="download",
            remote_cache_path=remote_cache_path,
        )
        asset_report_path = workflow_dir / "layout-asset-report.json"
        remote_asset_rows = [
            item
            for item in layout_assets
            if item.get("status") in {"downloaded", "remote_cache_hit"}
        ]
        remote_asset_keys = {
            str(item.get("source_url_sha256")) for item in remote_asset_rows
        }
        network_download_keys = {
            str(item.get("source_url_sha256"))
            for item in remote_asset_rows
            if item.get("status") == "downloaded"
        }
        cache_hit_keys = remote_asset_keys - network_download_keys
        download_ms_by_key = {
            str(item.get("source_url_sha256")): int(item.get("download_ms") or 0)
            for item in remote_asset_rows
        }
        asset_report = {
            "ok": not layout_unresolved,
            "download_window": "parallel_with_translation_worker",
            "remote_assets": len(remote_asset_keys),
            "network_downloads": len(network_download_keys),
            "cache_hits": len(cache_hit_keys),
            "download_ms_sum": sum(download_ms_by_key.values()),
            "unresolved": layout_unresolved,
            "assets": layout_assets,
        }
        atomic_write_json(asset_report_path, asset_report)
        if layout_unresolved:
            update_workflow_state(
                state_path,
                stage="layout",
                status="failed",
                error="remote asset download failed: " + ", ".join(layout_unresolved[:3]),
                artifacts={"layout_asset_report": asset_report_path.name},
            )
            raise ValueError(
                "remote asset download failed while translation continued: "
                + ", ".join(layout_unresolved[:3])
            )
        atomic_write_text(template_path, template, min_bytes=20)
        template_sha256 = sha256_text(template)
        update_workflow_state(
            state_path,
            stage="layout",
            status="completed",
            duration_ms=round((time.perf_counter() - started) * 1000),
            artifacts={
                "merge_template": template_path.name,
                "layout_asset_report": asset_report_path.name,
                **({"remote_asset_cache": remote_cache_path.name} if remote_cache_path.is_file() else {}),
            },
            metadata={
                "placeholder_count": len(packet["units"]),
                "remote_assets": asset_report["remote_assets"],
                "network_downloads": asset_report["network_downloads"],
                "remote_asset_cache_hits": asset_report["cache_hits"],
                "asset_download_ms_sum": asset_report["download_ms_sum"],
                "asset_download_window": asset_report["download_window"],
            },
            state_updates={"layout_template_sha256": template_sha256},
        )
        report_path = workflow_dir / "layout-report.json"
        report = {
            "ok": True,
            "stage": "layout",
            "translation_units": len(packet["units"]),
            "layout_template_sha256": template_sha256,
            "assets": asset_report,
            "report_path": str(report_path),
        }
        atomic_write_json(report_path, report)
        return report

    if workflow_dir and args.translation_mode == "bilingual" and args.translation_stage == "finalize":
        prepared = output_note.with_name(f".{output_note.name}.translation-prepared.md")
        packet_path = workflow_dir / "translation-packet.json"
        template_path = workflow_dir / "merge-template.md"
        output_path = Path(args.translation_output).resolve() if args.translation_output else workflow_dir / "translation-output.jsonl"
        cached_output_path = workflow_dir / "translation-cached-output.jsonl"
        combined_output_path = workflow_dir / "translation-combined-output.jsonl"
        cache_path = workflow_dir / "translation-cache.jsonl"
        state_path = workflow_dir / "workflow-state.json"
        if not prepared.is_file() or not packet_path.is_file() or (not output_path.is_file() and not cached_output_path.is_file()):
            raise ValueError("finalize requires prepared Markdown, translation packet, and translation output or cache")
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
        identity = translation_identity()
        packet = read_packet(packet_path)
        prepared_sha256 = sha256_text(read_text(prepared))
        packet_sha256 = sha256(packet_path)
        if packet["source_markdown_sha256"] != prepared_sha256:
            raise ValueError("prepared Markdown hash changed after translation packet creation")
        for key, actual in (
            ("prepared_sha256", prepared_sha256),
            ("packet_sha256", packet_sha256),
            ("prompt_sha256", identity["prompt_sha256"]),
        ):
            if state.get(key) and state[key] != actual:
                raise ValueError(f"workflow-state {key} no longer matches frozen artifact")
        if not template_path.is_file():
            template = build_merge_template(prepared, packet)
            atomic_write_text(template_path, template, min_bytes=20)
        else:
            template = read_text(template_path)
        template_sha256 = sha256_text(template)
        if state.get("layout_template_sha256") and state["layout_template_sha256"] != template_sha256:
            raise ValueError("merge template hash changed after layout stage")

        started = time.perf_counter()
        combined_path = combine_cached_and_worker_output(cached_output_path, output_path, combined_output_path)
        translations, errors = validate_output(packet, combined_path)
        if errors:
            update_workflow_state(state_path, stage="translation_validation", status="failed", error="; ".join(errors))
            raise ValueError("translation output validation failed: " + "; ".join(errors))
        fingerprint = identity["translator_fingerprint"]
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
        requested_runtime = ((state.get("translator") or {}).get("requested") or {})
        actual_runtime = ((state.get("translator") or {}).get("actual") or {})
        worker_output_present = output_path.is_file()
        runtime_verified = not worker_output_present or (
            actual_runtime.get("runtime_verified") is True
            and actual_runtime.get("model") == requested_runtime.get("model") == identity["model"]
            and str(actual_runtime.get("reasoning_effort", "")).lower()
            == str(requested_runtime.get("reasoning_effort", "")).lower()
            == identity["reasoning_effort"].lower()
        )
        if packet.get("schema_version") in {3, 4, 5} and worker_output_present:
            attestation_name = ((state.get("artifacts") or {}).get("translation_runtime_attestation"))
            attestation_path = workflow_dir / str(attestation_name or "missing-attestation.json")
            if not runtime_verified or not attestation_path.is_file():
                raise ValueError("schema v3 finalize requires verified actual Terra High runtime attestation")
            if state.get("translation_runtime_attestation_sha256") != sha256(attestation_path):
                raise ValueError("translation runtime attestation hash no longer matches workflow-state")
            try:
                attestation = json.loads(attestation_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ValueError("translation runtime attestation is not valid JSON") from exc
            attested_actual = attestation.get("actual") or {}
            if (
                attestation.get("success") is not True
                or attested_actual.get("runtime_verified") is not True
                or attested_actual.get("model") != actual_runtime.get("model")
                or str(attested_actual.get("reasoning_effort", "")).lower()
                != str(actual_runtime.get("reasoning_effort", "")).lower()
                or attestation.get("output_sha256") != sha256(output_path)
            ):
                raise ValueError("translation runtime attestation contents do not match verified worker output")
        runtime_source = "worker_attestation" if worker_output_present else "verified_translation_cache"
        translation_validation_path = workflow_dir / "translation-validation-report.json"
        translation_validation = {
            "ok": True,
            "unit_count": len(translations),
            "cached_units": 0,
            "combined_output_sha256": sha256(combined_path),
            "worker_output_sha256": sha256(output_path) if worker_output_present else None,
            "runtime_model_verified": runtime_verified,
            "runtime_source": runtime_source,
            "errors": [],
        }
        cached_rows = []
        if cached_output_path.is_file():
            cached_rows = [line for line in cached_output_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        passthrough_ids = {
            str(unit["unit_id"])
            for unit in packet["units"]
            if unit.get("requires_chinese", True) is False or unit.get("kind") == "passthrough"
        }
        cached_ids = {
            str(json.loads(line).get("unit_id"))
            for line in cached_rows
            if isinstance(json.loads(line), dict)
        }
        translation_validation["passthrough_units"] = len(cached_ids & passthrough_ids)
        translation_validation["cached_units"] = len(cached_ids - passthrough_ids)
        translation_validation["worker_units"] = len(translations) - len(cached_ids)
        reference_ids = {
            str(unit["unit_id"])
            for unit in packet["units"]
            if unit.get("kind") == "reference_title"
        }
        translation_validation["cached_reference_title_units"] = len(cached_ids & reference_ids)
        translation_validation["worker_reference_title_units"] = len(reference_ids - cached_ids)
        atomic_write_json(translation_validation_path, translation_validation)
        append_validated_cache(
            cache_path,
            packet,
            translations,
            fingerprint,
            actual_runtime if worker_output_present else {
                "model": requested_runtime.get("model"),
                "reasoning_effort": requested_runtime.get("reasoning_effort"),
                "runtime_verified": True,
            },
        )
        update_workflow_state(
            state_path,
            stage="translation_validation",
            status="completed",
            artifacts={"translation_validation_report": translation_validation_path.name},
            metadata={
                "validated_unit_count": len(translations),
                "cached_units": translation_validation["cached_units"],
                "worker_units": translation_validation["worker_units"],
                "runtime_model_verified": runtime_verified,
                "runtime_source": runtime_source,
            },
        )
        merged = fill_merge_template(template, packet, translations)
        merged = reanchor_archived_images(merged, prepared, output_note, resource_root)
        merged = ensure_card_frontmatter(merged, output_note.stem, "bilingual", "finalize")
        merged, figure_link_report = normalize_figure_links(merged, output_note, vault_root)
        staging = output_note.with_name(f".{output_note.name}.card-staging.md")
        backup_path: Path | None = None
        try:
            atomic_write_text(staging, merged, validator=validate_markdown_text, min_bytes=20)
            validation = validate_full(
                staging,
                vault_root,
                stage="final",
                workflow_state_path=state_path,
                translation_packet_path=packet_path,
                validation_mode="pipeline_finalize",
                stable_resource_roots=stable_resource_roots,
            )
            if not validation["ok"]:
                update_workflow_state(state_path, stage="validation", status="failed", error="; ".join(validation["errors"]))
                raise RuntimeError("card validation failed: " + "; ".join(validation["errors"]))
            backup_path = backup_existing(
                output_note,
                "before-paper-card-promotion",
                replacement_text=merged,
            )
            promotion_method = atomic_write_text(
                output_note,
                merged,
                validator=validate_markdown_text,
                min_bytes=20,
            )
            image_layout_context = load_source_context(args.source_package)
            image_layout = synchronize_paper_card_image_layout(
                output_note=output_note,
                vault_root=vault_root,
                layout_root=image_layout_context.layout_root,
                mode=getattr(args, "image_converter_layout", "center"),
                overwrite_existing=getattr(args, "overwrite_image_converter_alignments", False),
            )
            update_workflow_state(
                state_path,
                stage="translation_merge",
                status="completed",
                duration_ms=round((time.perf_counter() - started) * 1000),
                artifacts={"output_note": output_note.name},
            )
            rendered_reference_sha256 = sha256_text(extract_reference_section(merged))
            current_state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
            references_state = dict(current_state.get("references") or {})
            references_state.update(
                {
                    "rendered_section_sha256": rendered_reference_sha256,
                    "cached_title_units": translation_validation.get("cached_reference_title_units", 0),
                    "worker_title_units": translation_validation.get("worker_reference_title_units", 0),
                }
            )
            update_workflow_state(
                state_path,
                stage="validation",
                status="completed",
                metadata=validation,
                state_updates={"references": references_state},
            )
            update_workflow_state(state_path, stage="promotion", status="completed", metadata={"write_method": promotion_method})
            concept = run_optional_concept_links(
                output_note,
                vault_root,
                args.concept_links,
                workflow_state=state_path,
                translation_packet=packet_path,
                stable_resource_roots=stable_resource_roots,
            )
            update_workflow_state(state_path, stage="concept_links", status="completed" if concept["status"] != "failed" else "failed", metadata=concept)
            layout_asset_report_path = workflow_dir / "layout-asset-report.json"
            layout_asset_report = (
                json.loads(layout_asset_report_path.read_text(encoding="utf-8"))
                if layout_asset_report_path.is_file()
                else {"ok": True, "remote_assets": 0, "network_downloads": 0, "cache_hits": 0}
            )
            report_path = output_note.with_suffix(".card-report.json")
            report = {
                "ok": True,
                "stage": "finalize",
                "output_note": str(output_note),
                "translation_units": packet.get("body_translation_units", len(packet["units"])),
                "body_translation_units": packet.get("body_translation_units", len(packet["units"])),
                "reference_title_units": packet.get("reference_title_units", 0),
                "reference_title_fallback_units": packet.get("reference_title_fallback_units", 0),
                "model_units_total": len(packet["units"]),
                "cached_units": translation_validation["cached_units"],
                "worker_units": translation_validation["worker_units"],
                "cached_reference_title_units": translation_validation.get("cached_reference_title_units", 0),
                "worker_reference_title_units": translation_validation.get("worker_reference_title_units", 0),
                "reference_blocks": validation.get("reference_blocks", packet.get("reference_blocks", 0)),
                "reference_source_section_sha256": packet.get("reference_source_section_sha256"),
                "reference_rendered_section_sha256": rendered_reference_sha256,
                "images": validation.get("image_count", 0),
                "assets": layout_asset_report,
                "citation_links": validation.get("ref_link_count", 0),
                "figure_targets": validation.get("figure_targets", 0),
                "figure_mentions": validation.get("figure_mentions", 0),
                "figure_links_written": validation.get("figure_links_written", 0),
                "unmatched_figure_mentions": validation.get("unmatched_figure_mentions", 0),
                "ambiguous_figure_targets": validation.get("ambiguous_figure_targets", 0),
                "broken_figure_links": validation.get("broken_figure_links", 0),
                "figure_links": figure_link_report,
                "html_tables": validation.get("html_tables", 0),
                "mojibake": validation.get("mojibake", 0),
                "invalid_image_paths": validation.get("invalid_image_paths", 0),
                "protected_token_errors": validation.get("protected_token_errors", 0),
                "placeholders": validation.get("placeholders", 0),
                "runtime_model_verified": runtime_verified,
                "runtime_source": runtime_source,
                "errors": len(validation.get("errors", [])),
                "warnings_count": len(validation.get("warnings", [])),
                "output_bytes": output_note.stat().st_size,
                "output_sha256": sha256(output_note),
                "prepared_sha256": prepared_sha256,
                "packet_sha256": packet_sha256,
                "layout_template_sha256": template_sha256,
                "validation": validation,
                "concept_links": concept,
                "image_converter_layout": image_layout,
                "backup_path": str(backup_path) if backup_path else None,
                "report_path": str(report_path),
                "workflow_state_path": str(state_path),
                "warnings": [concept["warning"]] if concept.get("warning") else [],
            }
            atomic_write_json(report_path, report)
            report["workflow_retention"] = compact_completed_workflow(workflow_dir)
            prepared.unlink(missing_ok=True)
            atomic_write_json(report_path, report)
            return report
        finally:
            staging.unlink(missing_ok=True)

    context = load_source_context(args.source_package)
    bibtex_path = resolve_bibtex_path(getattr(args, "bibtex_path", None), context)
    original = read_text(input_markdown)
    normalized_text, encoding_report = repair_mojibake_safely(original)
    normalized_text, web_noise_report = filter_web_clipping_noise(normalized_text)
    normalized_text, clipping_artifact_report = normalize_web_clipping_artifacts(normalized_text)
    normalized_text, bilingual_source_report = collapse_existing_bilingual_pairs(normalized_text)
    stabilized = ensure_card_frontmatter(normalized_text, input_markdown.stem, args.translation_mode, args.translation_stage)
    stabilized = normalize_heading_levels(stabilized)
    stabilized, table_report = convert_html_tables(stabilized)
    stabilized, mixed_image_report = normalize_mixed_image_syntax(stabilized)
    arxiv_subject_labels_before = count_arxiv_subject_labels(stabilized)
    stabilized, _ = normalize_arxiv_subject_labels(stabilized)
    citation_labels_before = classify_citation_footnote_labels(stabilized)
    named_citation_groups_before = count_named_citation_groups(stabilized)
    stabilized = normalize_citations(stabilized, bibtex_path)
    arxiv_subject_labels_normalized = (
        arxiv_subject_labels_before - count_arxiv_subject_labels(stabilized)
    )
    citation_labels_after = classify_citation_footnote_labels(stabilized)
    named_citation_groups_after = count_named_citation_groups(stabilized)
    citation_report = {
        "status": "completed",
        "bibliographic_footnote_labels_detected": len(citation_labels_before),
        "bibliographic_footnote_labels_normalized": len(citation_labels_before) - len(citation_labels_after),
        "residual_bibliographic_footnote_labels": len(citation_labels_after),
        "named_citation_groups_detected": named_citation_groups_before,
        "named_citation_groups_normalized": named_citation_groups_before - named_citation_groups_after,
        "residual_named_citation_groups": named_citation_groups_after,
        "bibtex_path": str(bibtex_path) if bibtex_path else None,
        "arxiv_subject_labels_normalized": arxiv_subject_labels_normalized,
    }
    defer_remote_assets = bool(
        workflow_dir
        and args.translation_mode == "bilingual"
        and args.translation_stage == "prepare"
    )
    preflight_text, asset_report, unresolved = prepare_assets(
        stabilized,
        input_markdown,
        output_note,
        resource_root,
        context,
        args.write,
        remote_mode="defer" if defer_remote_assets else "download",
    )
    dry_counts = {
        "citation_residual": len(citation_labels_after) + named_citation_groups_after,
        "caption_math_artifact_residual": clipping_artifact_report["residual_caption_math_artifacts"],
        "remote_images": len(re.findall(r"!\[[^\]]*\]\((?:https?:|data:|#)", preflight_text, re.IGNORECASE)),
        "html_tables": table_report["needs_review"],
        "mojibake": encoding_report["residual_markers"],
        "invalid_image_paths": len(unresolved),
        "frontmatter_errors": 0,
        "translation_units": len(extract_translation_units(preflight_text)) if args.translation_mode == "bilingual" else 0,
        "protected_token_errors": 0,
    }
    report: dict[str, Any] = {
        "ok": False,
        "stage": args.translation_stage,
        "input_markdown": str(input_markdown),
        "output_note": str(output_note),
        "vault_root": str(vault_root),
        "source_package": str(context.root) if context.root else None,
        "write": bool(args.write),
        "input_sha256": sha256(input_markdown),
        **dry_counts,
        "citations": citation_report,
        "tables": table_report,
        "mixed_images": mixed_image_report,
        "encoding": encoding_report,
        "bilingual_source": bilingual_source_report,
        "web_clipping_noise": web_noise_report,
        "web_clipping_artifacts": clipping_artifact_report,
        "assets": asset_report,
        "unresolved_assets": unresolved[:3],
        "figure_crop": {"status": "skipped", "reason": "dry-run"},
    }
    report["ready_for_translation"] = args.translation_mode == "bilingual" and dry_counts["translation_units"] > 0 and all(
        dry_counts[key] == 0
        for key in (
            "citation_residual",
            "remote_images",
            "html_tables",
            "mojibake",
            "invalid_image_paths",
            "frontmatter_errors",
            "protected_token_errors",
        )
    )
    if dry_counts["caption_math_artifact_residual"]:
        report.setdefault("warnings", []).append(
            "non-body figure-caption TeX display artifacts remain; translation may proceed"
        )
    if not args.write:
        report["ok"] = True
        return report

    if workflow_dir and args.translation_mode == "bilingual" and args.translation_stage == "prepare":
        workflow_dir.mkdir(parents=True, exist_ok=True)
        state_path = workflow_dir / "workflow-state.json"
        if state_path.is_file():
            prior_state = json.loads(state_path.read_text(encoding="utf-8"))
            prior_state.pop("translation_runtime_attestation_sha256", None)
            (prior_state.get("artifacts") or {}).pop("translation_runtime_attestation", None)
            atomic_write_json(state_path, prior_state)
        prepared = output_note.with_name(f".{output_note.name}.translation-prepared.md")
        packet_path = workflow_dir / "translation-packet.json"
        pending_packet_path = workflow_dir / "translation-pending-packet.jsonl"
        assignment_path = workflow_dir / "translation-assignment.json"
        worker_output_path = workflow_dir / "translation-output.jsonl"
        preflight_report_path = workflow_dir / "preflight-report.json"
        for stale in (worker_output_path, workflow_dir / "translation-combined-output.jsonl", packet_path, pending_packet_path, assignment_path):
            stale.unlink(missing_ok=True)
        started = time.perf_counter()
        atomic_write_text(prepared, preflight_text, validator=validate_markdown_text, min_bytes=20)
        report["figure_crop"] = run_figure_crop(
            prepared,
            resource_root,
            context,
            slugify(get_title(preflight_text, output_note.stem)),
            asset_report,
            cache_path=workflow_dir / "figure-crop-cache.json",
        )
        cropped_text = read_text(prepared)
        final_prepared, final_assets, unresolved_after_crop = prepare_assets(
            cropped_text,
            input_markdown,
            output_note,
            resource_root,
            context,
            True,
            remote_mode="defer",
        )
        atomic_write_text(prepared, final_prepared, validator=validate_markdown_text, min_bytes=20)
        planned_remote_images = sum(
            item.get("status") == "remote_pending" for item in final_assets
        )
        preflight = validate_full(
            prepared,
            vault_root,
            stage="prepared",
            allow_remote_images=planned_remote_images > 0,
            stable_resource_roots=stable_resource_roots,
        )
        report.update(preflight)
        report["assets"] = final_assets
        report["planned_remote_images"] = planned_remote_images
        report["unresolved_assets"] = unresolved_after_crop[:3]
        report["input_sha256"] = sha256(input_markdown)
        report["prepared_sha256"] = sha256_text(final_prepared)
        report["report_path"] = str(preflight_report_path)
        atomic_write_json(preflight_report_path, report)
        if not preflight["ready_for_translation"]:
            raise ValueError(
                "strict preflight failed before translation: "
                + "; ".join(preflight["errors"][:3])
                + f"; report={preflight_report_path}"
            )
        packet = build_packet(prepared, packet_path)
        identity = translation_identity()
        fingerprint = identity["translator_fingerprint"]
        cache_report = build_pending_packet(
            packet,
            workflow_dir / "translation-cache.jsonl",
            fingerprint,
            pending_packet_path,
            workflow_dir / "translation-cached-output.jsonl",
            expected_quality_tier=identity["quality_tier"],
        )
        pending_sha256 = sha256(pending_packet_path)
        assignment = write_assignment_manifest(
            assignment_path,
            packet_path=pending_packet_path,
            output_path=worker_output_path,
            unit_count=cache_report["pending_count"],
            packet_sha256=pending_sha256,
            identity=identity,
        )
        packet_sha256 = sha256(packet_path)
        state_updates = {
            "bilingual_layout": LAYOUT_NAME,
            "input_sha256": report["input_sha256"],
            "prepared_sha256": report["prepared_sha256"],
            "packet_sha256": packet_sha256,
            "prompt_sha256": identity["prompt_sha256"],
            "translator": {
                "requested": {
                    "agent": identity["agent"],
                    "model": identity["model"],
                    "reasoning_effort": identity["reasoning_effort"],
                    "quality_tier": identity["quality_tier"],
                    "prompt_sha256": identity["prompt_sha256"],
                    "fingerprint": identity["translator_fingerprint"],
                    "constraints_version": identity["constraints_version"],
                },
                "actual": None,
            },
            "reference_blocks": packet["reference_blocks"],
            "passthrough_units": packet.get("passthrough_units", 0),
            "layout_units_total": packet.get("layout_units_total", len(packet["units"])),
            "model_units_total": packet.get("model_units_total", len(packet["units"])),
            "reference_source_section_sha256": packet.get("reference_source_section_sha256", packet["reference_section_sha256"]),
            "reference_section_sha256": packet["reference_section_sha256"],
            "references": {
                "reference_blocks": packet["reference_blocks"],
                "source_section_sha256": packet.get("reference_source_section_sha256", packet["reference_section_sha256"]),
                "rendered_section_sha256": None,
                "title_units": packet.get("reference_title_units", 0),
                "deterministic_title_units": packet.get("reference_title_units", 0) - packet.get("reference_title_fallback_units", 0),
                "fallback_title_units": packet.get("reference_title_fallback_units", 0),
                "cached_title_units": cache_report.get("cached_reference_title_units", 0),
                "worker_title_units": cache_report.get("pending_reference_title_units", 0),
            },
            "web_clipping_noise": web_noise_report,
            "planned_remote_images": planned_remote_images,
            "preflight": {key: preflight[key] for key in dry_counts},
        }
        update_workflow_state(
            state_path,
            stage="card_prepare",
            status="completed",
            duration_ms=round((time.perf_counter() - started) * 1000),
            artifacts={"prepared_markdown": prepared.name},
            metadata={
                "citation_normalization": citation_report,
                "web_clipping_noise_removed": web_noise_report["removed_count"],
                "planned_remote_images": planned_remote_images,
            },
            state_updates=state_updates,
        )
        update_workflow_state(
            state_path,
            stage="translation_packet",
            status="completed",
            artifacts={
                "translation_packet": packet_path.name,
                "translation_pending_packet": pending_packet_path.name,
                "translation_assignment": assignment_path.name,
            },
            metadata={
                "unit_count": len(packet["units"]),
                "body_translation_units": packet.get("body_translation_units", len(packet["units"])),
                "passthrough_units": packet.get("passthrough_units", 0),
                "layout_units_total": packet.get("layout_units_total", len(packet["units"])),
                "model_units_total": packet.get("model_units_total", len(packet["units"])),
                "reference_title_units": packet.get("reference_title_units", 0),
                "reference_title_fallback_units": packet.get("reference_title_fallback_units", 0),
                "pending_count": cache_report["pending_count"],
                "cached_count": cache_report["cached_count"],
                "constraints_fingerprint": packet["constraints_fingerprint"],
            },
        )
        worker_status = "completed" if cache_report["pending_count"] == 0 else "pending"
        update_workflow_state(
            state_path,
            stage="translation_worker",
            status=worker_status,
            metadata={"pending_count": cache_report["pending_count"], "cached_count": cache_report["cached_count"]},
        )
        report.update(
            {
                "ok": True,
                "stage": "prepare",
                "ready_for_translation": True,
                "translation_units": packet.get("body_translation_units", len(packet["units"])),
                "body_translation_units": packet.get("body_translation_units", len(packet["units"])),
                "passthrough_units": packet.get("passthrough_units", 0),
                "layout_units_total": packet.get("layout_units_total", len(packet["units"])),
                "reference_title_units": packet.get("reference_title_units", 0),
                "reference_title_fallback_units": packet.get("reference_title_fallback_units", 0),
                "model_units_total": packet.get("model_units_total", len(packet["units"])),
                "packet_sha256": packet_sha256,
                "assignment_path": str(assignment_path),
                "assignment": assignment,
                "translation_cache": cache_report,
            }
        )
        atomic_write_json(preflight_report_path, report)
        return report

    staging = output_note.with_name(f".{output_note.name}.card-staging.md")
    try:
        atomic_write_text(staging, preflight_text, validator=validate_markdown_text, min_bytes=20)
        report["figure_crop"] = run_figure_crop(
            staging,
            resource_root,
            context,
            slugify(get_title(stabilized, output_note.stem)),
            asset_report,
            cache_path=resource_root.parent / ".tmp" / "paper-card-cache" / "figure-crop-cache-v1.json",
        )
        final_text, final_asset_report, unresolved_after_crop = prepare_assets(
            read_text(staging), input_markdown, output_note, resource_root, context, True
        )
        final_text, figure_link_report = normalize_figure_links(final_text, output_note, vault_root)
        report["assets"] = final_asset_report
        report["unresolved_assets"] = unresolved_after_crop[:3]
        report["figure_links"] = figure_link_report
        atomic_write_text(staging, final_text, validator=validate_markdown_text, min_bytes=20)
        validation = validate_paper_note(
            staging,
            vault_root,
            stable_resource_roots=stable_resource_roots,
        )
        report["validation"] = validation
        report.update(
            {
                key: validation.get(key, 0)
                for key in (
                    "figure_targets", "figure_mentions", "figure_links_written",
                    "unmatched_figure_mentions", "ambiguous_figure_targets",
                    "broken_figure_links",
                )
            }
        )
        if not validation["ok"]:
            raise RuntimeError("card validation failed: " + "; ".join(validation["errors"]))
        backup_path = backup_existing(
            output_note,
            "before-paper-card-promotion",
            replacement_text=final_text,
        )
        atomic_write_text(output_note, final_text, validator=validate_markdown_text, min_bytes=20)
        image_layout = synchronize_paper_card_image_layout(
            output_note=output_note,
            vault_root=vault_root,
            layout_root=context.layout_root,
            mode=getattr(args, "image_converter_layout", "center"),
            overwrite_existing=getattr(args, "overwrite_image_converter_alignments", False),
        )
        concept = run_optional_concept_links(
            output_note,
            vault_root,
            args.concept_links,
            stable_resource_roots=stable_resource_roots,
        )
        report_path = output_note.with_suffix(".card-report.json")
        report.update(
            {
                "ok": True,
                "stage": "finalize",
                "backup_path": str(backup_path) if backup_path else None,
                "concept_links": concept,
                "image_converter_layout": image_layout,
                "report_path": str(report_path),
            }
        )
        atomic_write_json(report_path, report)
        return report
    finally:
        staging.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-markdown", required=True)
    parser.add_argument("--vault-root", required=True)
    parser.add_argument("--output-note", required=True)
    parser.add_argument("--translation-mode", required=True, choices=("bilingual", "none"))
    parser.add_argument("--concept-links", choices=("off", "report", "write"), default="off")
    parser.add_argument("--source-package")
    parser.add_argument(
        "--resource-directory",
        default="_resources",
        help="Vault-relative primary archive directory for assets (default: _resources).",
    )
    parser.add_argument(
        "--stable-resource-root",
        action="append",
        dest="stable_resource_roots",
        help="Additional vault-relative stable asset root. Repeat as needed; defaults also allow _resources and _附件.",
    )
    parser.add_argument("--bibtex-path", help="BibTeX source used to resolve escaped named citation keys; auto-detected from a source package when unambiguous.")
    parser.add_argument("--translation-stage", choices=("prepare", "layout", "finalize", "run"), default="finalize")
    parser.add_argument("--workflow-dir", help="Persistent per-input workflow directory for bilingual prepare/layout/finalize.")
    parser.add_argument("--translation-output", help="Validated JSONL written by the compact translation worker.")
    parser.add_argument("--translator-fingerprint", help="Deprecated compatibility override; automatic prompt-derived fingerprints are used by default.")
    parser.add_argument(
        "--worker-backend",
        choices=(NATIVE_SUBAGENT_BACKEND, DIRECT_CLI_BACKEND),
        default=NATIVE_SUBAGENT_BACKEND,
        help="Translation worker backend. Native subagent emits a spawn handoff; direct CLI is explicit compatibility mode.",
    )
    parser.add_argument(
        "--translation-agent-role-file",
        help="Project paper-translation-worker.toml. Defaults to <vault-root>/.codex/agents/paper-translation-worker.toml.",
    )
    parser.add_argument(
        "--translation-agent-task-name",
        help="Optional stable lowercase task name for the native paper translation subagent.",
    )
    parser.add_argument("--worker-timeout-seconds", type=int, default=1800, help="Hard timeout for each direct translation worker attempt used by --translation-stage run.")
    parser.add_argument("--worker-max-attempts", type=int, choices=(1, 2), default=2, help="Maximum sequential Terra attempts in one run; the second attempt receives only uncached units.")
    parser.add_argument(
        "--image-converter-layout",
        choices=("off", "center"),
        default="center",
        help="Synchronize final images to Image Converter as explicit center + no-wrap entries when the plugin is enabled.",
    )
    parser.add_argument(
        "--overwrite-image-converter-alignments",
        action="store_true",
        help="Replace existing per-image Image Converter alignment entries; manual settings are preserved by default.",
    )
    parser.add_argument("--in-place", action="store_true")
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    try:
        report = build(args)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False, separators=(",", ":")), file=sys.stderr)
        return 2
    print(json.dumps(compact_cli_report(report), ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
