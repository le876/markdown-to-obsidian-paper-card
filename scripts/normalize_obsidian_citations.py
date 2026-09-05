#!/usr/bin/env python3
"""Normalize paper citations to numeric Obsidian reference block links."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from safe_atomic_io import atomic_write_text


SUPERSCRIPT = str.maketrans("0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹")
REF_HEADING_RE = re.compile(r"(?im)^#{1,6}\s*(References|Bibliography|参考文献)\s*$")
HEADING_RE = re.compile(r"(?m)^(?P<level>#{1,6})\s+")
NUMERIC_FOOTNOTE_RE = re.compile(
    r"^(?P<quote>>[ \t]*)?\[\^(?P<label>\d+)\]:[ \t]*(?P<content>.*)$"
)
BIBLIOGRAPHIC_SIGNAL_RE = re.compile(
    r"(?i)\b(?:arxiv|doi|isbn|proceedings|journal|conference|transactions|vol\.?|pp\.?|"
    r"cited\s+by|external\s+links?|springer|elsevier|ieee|acm)\b|https?://|引用位置|"
    r"外部链接|期刊|会议|出版社"
)
AUTHOR_PREFIX_RE = re.compile(
    r"^(?:[A-ZÀ-ÖØ-Þ]\.[ \t]*){1,3}[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’.-]+(?:\s*,|\s+and\s+)",
    flags=re.IGNORECASE,
)
NAMED_CITATION_RE = re.compile(
    r"\\\[(?P<inner>[A-Za-z][A-Za-z0-9_.:+-]*(?:\s*,\s*[A-Za-z][A-Za-z0-9_.:+-]*)*)\\\](?!https?://)"
)
ARXIV_SUBJECT_LABEL_RE = re.compile(
    r"\\\[(?P<subject>(?:cs|math|stat|eess|physics|q-bio|q-fin|econ)(?:\.[A-Za-z]{2})?)\\\]",
    re.IGNORECASE,
)
TABLE_ROW_RE = re.compile(r"^[ \t]{0,3}(?:>[ \t]*)*\|.*\|[ \t]*(?:\r?\n)?$")
FENCE_RE = re.compile(r"^[ \t]{0,3}(?:>[ \t]*)*(?P<fence>`{3,}|~{3,})")
INLINE_CODE_RE = re.compile(r"(?P<ticks>`+)[^\r\n]*?(?P=ticks)")
UNESCAPED_TABLE_CITATION_RE = re.compile(
    r"\[\[#\^ref-(?P<n>[A-Za-z0-9_-]+)(?<!\\)\|(?P<label>[^\]]+)\]\]"
)


@dataclass(frozen=True)
class NumericFootnoteDefinition:
    label: int
    start: int
    end: int
    content: str
    quoted: bool


def superscript(number: int) -> str:
    return str(number).translate(SUPERSCRIPT)


def citation_link(number: int) -> str:
    return f"[[#^ref-{number}|{superscript(number)}]]"


def _escape_citation_links_outside_inline_code(line: str) -> str:
    pieces: list[str] = []
    cursor = 0
    for match in INLINE_CODE_RE.finditer(line):
        pieces.append(
            UNESCAPED_TABLE_CITATION_RE.sub(
                lambda citation: (
                    f"[[#^ref-{citation.group('n')}\\|{citation.group('label')}]]"
                ),
                line[cursor : match.start()],
            )
        )
        pieces.append(match.group(0))
        cursor = match.end()
    pieces.append(
        UNESCAPED_TABLE_CITATION_RE.sub(
            lambda citation: f"[[#^ref-{citation.group('n')}\\|{citation.group('label')}]]",
            line[cursor:],
        )
    )
    return "".join(pieces)


def escape_markdown_table_citation_pipes(text: str) -> str:
    """Escape wikilink alias pipes only on Markdown table rows outside code fences."""
    lines = text.splitlines(keepends=True)
    escaped: list[str] = []
    frontmatter = bool(lines and lines[0].rstrip("\r\n") == "---")
    fence_character = ""
    fence_length = 0

    for index, line in enumerate(lines):
        stripped = line.rstrip("\r\n")
        if frontmatter:
            escaped.append(line)
            if index > 0 and stripped == "---":
                frontmatter = False
            continue

        fence_match = FENCE_RE.match(line)
        if fence_character:
            escaped.append(line)
            if (
                fence_match
                and fence_match.group("fence")[0] == fence_character
                and len(fence_match.group("fence")) >= fence_length
            ):
                fence_character = ""
                fence_length = 0
            continue
        if fence_match:
            fence = fence_match.group("fence")
            fence_character = fence[0]
            fence_length = len(fence)
            escaped.append(line)
            continue

        if TABLE_ROW_RE.match(line):
            line = _escape_citation_links_outside_inline_code(line)
        escaped.append(line)

    return "".join(escaped)


def find_pandoc(explicit: str | Path | None = None) -> Path:
    """Locate Pandoc without depending on the Windows console encoding or PATH alone."""
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    discovered = shutil.which("pandoc")
    if discovered:
        candidates.append(Path(discovered))
    home = Path.home()
    candidates.extend(sorted(home.glob("pandoc-*/**/pandoc.exe"), reverse=True))
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise ValueError("Pandoc is required to resolve named BibTeX citations but was not found")


def load_bibtex_csl(
    bibtex_path: str | Path,
    pandoc_path: str | Path | None = None,
) -> dict[str, dict[str, object]]:
    """Parse BibTeX through Pandoc and return entries keyed by their exact citation key."""
    source = Path(bibtex_path).resolve()
    if not source.is_file():
        raise ValueError(f"BibTeX source does not exist: {source}")
    command = [str(find_pandoc(pandoc_path)), str(source), "--from=biblatex", "--to=csljson"]
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
    )
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip() or "unknown Pandoc failure"
        raise ValueError(f"Pandoc could not parse BibTeX: {message}")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Pandoc returned invalid CSL JSON: {exc}") from exc
    if not isinstance(payload, list):
        raise ValueError("Pandoc CSL JSON root must be a list")
    entries: dict[str, dict[str, object]] = {}
    for raw in payload:
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
            continue
        key = raw["id"].strip()
        if not key:
            continue
        if key in entries:
            raise ValueError(f"duplicate BibTeX citation key: {key}")
        entries[key] = raw
    return entries


def _plain_csl(value: object) -> str:
    if isinstance(value, list):
        value = "; ".join(str(item) for item in value if item)
    if not isinstance(value, str):
        return ""
    value = re.sub(r"<[^>]+>", "", value)
    return re.sub(r"\s+", " ", value).strip().rstrip(".")


def _format_csl_authors(entry: dict[str, object]) -> str:
    raw_authors = entry.get("author")
    if not isinstance(raw_authors, list):
        return ""
    authors: list[str] = []
    for raw in raw_authors:
        if not isinstance(raw, dict):
            continue
        literal = _plain_csl(raw.get("literal"))
        if literal:
            authors.append(literal)
            continue
        family = _plain_csl(raw.get("family"))
        given = _plain_csl(raw.get("given"))
        rendered = ", ".join(part for part in (family, given) if part)
        if rendered:
            authors.append(rendered)
    return "; ".join(authors)


def _format_csl_year(entry: dict[str, object]) -> str:
    issued = entry.get("issued")
    if not isinstance(issued, dict):
        return ""
    parts = issued.get("date-parts")
    if not isinstance(parts, list) or not parts or not isinstance(parts[0], list) or not parts[0]:
        return ""
    return str(parts[0][0])


def format_csl_reference(entry: dict[str, object]) -> str:
    """Render a stable, readable reference without fabricating unavailable fields."""
    authors = _format_csl_authors(entry)
    title = _plain_csl(entry.get("title"))
    container = _plain_csl(entry.get("container-title") or entry.get("publisher"))
    year = _format_csl_year(entry)
    details = ". ".join(part for part in (authors, title, container, year) if part)
    doi = _plain_csl(entry.get("DOI"))
    url = _plain_csl(entry.get("URL"))
    locator = f"https://doi.org/{doi}" if doi else url
    if locator and locator not in details:
        details = f"{details}. {locator}" if details else locator
    return details.rstrip(". ") + "."


def parse_numeric_footnote_definitions(text: str) -> list[NumericFootnoteDefinition]:
    """Parse numeric footnote definitions without treating every numeric footnote as a citation."""
    lines = text.splitlines(keepends=True)
    offsets: list[int] = []
    offset = 0
    for line in lines:
        offsets.append(offset)
        offset += len(line)

    definitions: list[NumericFootnoteDefinition] = []
    index = 0
    while index < len(lines):
        first_line = lines[index].rstrip("\r\n")
        match = NUMERIC_FOOTNOTE_RE.match(first_line)
        if not match:
            index += 1
            continue

        quoted = bool(match.group("quote"))
        content_parts = [match.group("content").strip()]
        next_index = index + 1
        while next_index < len(lines):
            candidate = lines[next_index].rstrip("\r\n")
            continuation: str | None = None
            if quoted and candidate.startswith(">"):
                tail = candidate[1:]
                if tail.startswith("    ") or tail.startswith("\t"):
                    continuation = tail.lstrip()
            elif not quoted and (candidate.startswith("    ") or candidate.startswith("\t")):
                continuation = candidate.lstrip()
            if continuation is None:
                break
            content_parts.append(continuation)
            next_index += 1

        end = offsets[next_index] if next_index < len(lines) else len(text)
        definitions.append(
            NumericFootnoteDefinition(
                label=int(match.group("label")),
                start=offsets[index],
                end=end,
                content=re.sub(r"\s+", " ", " ".join(content_parts)).strip(),
                quoted=quoted,
            )
        )
        index = next_index
    return definitions


def looks_bibliographic_entry(content: str) -> bool:
    """Return True only for content with bibliographic structure, not ordinary explanatory notes."""
    normalized = re.sub(r"\s+", " ", content).strip()
    has_year = bool(re.search(r"\b(?:18|19|20)\d{2}[a-z]?\b", normalized, flags=re.IGNORECASE))
    has_signal = bool(BIBLIOGRAPHIC_SIGNAL_RE.search(normalized))
    has_author_prefix = bool(AUTHOR_PREFIX_RE.search(normalized))
    has_parenthesized_year = bool(re.search(r"\((?:18|19|20)\d{2}[a-z]?\)", normalized, flags=re.IGNORECASE))
    return (has_year and (has_signal or has_author_prefix or has_parenthesized_year)) or (
        has_author_prefix and has_signal
    )


def classify_citation_footnote_labels(text: str) -> set[int]:
    """Classify reference footnotes while preserving native Markdown footnotes.

    Numeric footnotes are citations only when their definitions are inside a References section,
    have bibliographic structure, or belong to a contiguous bibliography whose majority is
    independently bibliographic. Named footnotes and ordinary explanatory numeric notes remain native.
    """
    definitions = parse_numeric_footnote_definitions(text)
    if not definitions:
        return set()

    references_span = find_references_span(text)
    labels: set[int] = set()
    grouped: dict[int, list[NumericFootnoteDefinition]] = {}
    for definition in definitions:
        grouped.setdefault(definition.label, []).append(definition)
        in_references = bool(
            references_span
            and references_span[0] <= definition.start < references_span[1]
        )
        if in_references or looks_bibliographic_entry(definition.content):
            labels.add(definition.label)

    all_labels = sorted(grouped)
    contiguous = all_labels == list(range(all_labels[0], all_labels[-1] + 1))
    signal_labels = {
        label
        for label, items in grouped.items()
        if any(BIBLIOGRAPHIC_SIGNAL_RE.search(item.content) for item in items)
    }
    if len(all_labels) >= 5 and contiguous and len(signal_labels) / len(all_labels) >= 0.8:
        labels.update(all_labels)
    elif len(all_labels) >= 3 and contiguous and len(labels) / len(all_labels) >= 0.6:
        labels.update(signal_labels)
    return labels


def _remove_definition_spans(text: str, definitions: list[NumericFootnoteDefinition]) -> str:
    chunks: list[str] = []
    cursor = 0
    for definition in sorted(definitions, key=lambda item: item.start):
        chunks.append(text[cursor : definition.start])
        cursor = definition.end
    chunks.append(text[cursor:])
    return re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", "".join(chunks))


def _reference_block(label: int, definitions: list[NumericFootnoteDefinition]) -> str:
    quoted = [definition for definition in definitions if definition.quoted]
    unquoted = [definition for definition in definitions if not definition.quoted]
    if len(definitions) > 2 or len(quoted) > 1 or len(unquoted) > 1:
        raise ValueError(f"ambiguous duplicate citation footnote definitions for [^{label}]")

    context: list[str] = []
    if quoted and unquoted:
        context.append(f"> [{label}] {quoted[0].content}")
        target = unquoted[0].content
    else:
        target = definitions[0].content
    context.append(f"[{label}] {target} ^ref-{label}")
    return "\n\n".join(context)


def normalize_citation_footnotes(text: str) -> str:
    """Convert only bibliographic numeric footnotes into reference block links."""
    citation_labels = classify_citation_footnote_labels(text)
    if not citation_labels:
        return text

    definitions = parse_numeric_footnote_definitions(text)
    citation_definitions = [item for item in definitions if item.label in citation_labels]
    grouped: dict[int, list[NumericFootnoteDefinition]] = {}
    for definition in citation_definitions:
        grouped.setdefault(definition.label, []).append(definition)

    span = find_references_span(text)
    existing_numbers = reference_numbers(text[span[0] : span[1]]) if span else set()
    conflicts = sorted(existing_numbers & citation_labels)
    if conflicts:
        raise ValueError(f"citation footnotes conflict with existing reference numbers: {conflicts}")

    normalized = _remove_definition_spans(text, citation_definitions)
    label_pattern = "|".join(str(label) for label in sorted(citation_labels, reverse=True))
    normalized = re.sub(
        rf"\[\^(?P<label>{label_pattern})\]",
        lambda match: citation_link(int(match.group("label"))),
        normalized,
    )
    normalized = re.sub(
        r"(\[\[#\^ref-\d+\|[⁰¹²³⁴⁵⁶⁷⁸⁹]+\]\])(?:\s*,\s*|\s+)(?=\[\[#\^ref-\d+\|)",
        r"\1<sup>,</sup>",
        normalized,
    )

    blocks = "\n\n".join(
        _reference_block(label, grouped[label]) for label in sorted(citation_labels)
    )
    span = find_references_span(normalized)
    if span:
        start, end = span
        references = normalized[start:end].rstrip()
        after = normalized[end:].lstrip("\r\n")
        replacement = references + "\n\n" + blocks
        if after:
            return normalized[:start] + replacement.rstrip() + "\n\n" + after
        return normalized[:start] + replacement.rstrip() + "\n"
    return normalized.rstrip() + "\n\n## References\n\n" + blocks + "\n"


def find_references_span(text: str) -> tuple[int, int] | None:
    match = REF_HEADING_RE.search(text)
    if not match:
        return None
    ref_level = len(re.match(r"#+", match.group(0)).group(0)) if re.match(r"#+", match.group(0)) else 6
    next_start = len(text)
    for heading in HEADING_RE.finditer(text, match.end()):
        if len(heading.group("level")) <= ref_level:
            next_start = heading.start()
            break
    return match.start(), next_start


def reference_numbers(refs: str) -> set[int]:
    return {int(n) for n in re.findall(r"(?m)^\[(\d+)\]\s+", refs)}


def normalize_author_key(author: str) -> str:
    normalized = unicodedata.normalize("NFKD", author)
    normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    normalized = normalized.lower().replace("’", "'")
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def number_author_year_references(refs: str) -> str:
    if re.search(r"(?m)^\[\d+\]\s+", refs):
        return refs

    heading_match = REF_HEADING_RE.search(refs)
    if not heading_match:
        return refs

    heading = refs[: heading_match.end()].rstrip()
    body = refs[heading_match.end() :].strip()
    if not body:
        return refs

    paragraphs = [re.sub(r"\s+", " ", para.strip()) for para in re.split(r"\n\s*\n", body) if para.strip()]
    entries: list[str] = []
    for para in paragraphs:
        if entries and entries[-1].rstrip().endswith(","):
            entries[-1] = entries[-1].rstrip() + " " + para
        else:
            entries.append(para)

    if not entries:
        return refs

    numbered = [f"[{index}] {entry} ^ref-{index}" for index, entry in enumerate(entries, 1)]
    return heading + "\n\n" + "\n\n".join(numbered) + "\n"


def coalesce_numbered_reference_entries(refs: str) -> str:
    """Join wrapped numbered bibliography entries before block IDs are attached.

    PDF-to-Markdown tools sometimes insert blank lines in the middle of one
    bibliography entry.  Once a block ID is appended to the first physical
    line, the continuation becomes an orphan translation unit.  Treat the
    next numbered entry (or a native footnote definition) as the only hard
    boundary and collapse internal whitespace deterministically.
    """
    heading_match = REF_HEADING_RE.search(refs)
    if not heading_match:
        return refs

    prefix = refs[: heading_match.end()].rstrip()
    body = refs[heading_match.end() :].strip()
    if not body or len(re.findall(r"(?m)^\[\d+\]\s+", body)) < 2:
        return refs

    output: list[str] = []
    current: list[str] = []
    separated_by_blank = False
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if re.match(r"^\[\d+\]\s+", line):
            if current:
                output.append(re.sub(r"\s+", " ", " ".join(current)).strip())
            current = [line]
            separated_by_blank = False
        elif re.match(r"^\[\^", line):
            if current:
                output.append(re.sub(r"\s+", " ", " ".join(current)).strip())
                current = []
            output.append(line)
            separated_by_blank = False
        elif not line:
            separated_by_blank = bool(current)
        elif line and current:
            joined = re.sub(r"\s+", " ", " ".join(current)).strip()
            if separated_by_blank and re.search(r"[.!?][\"')\]]?$", joined):
                output.append(joined)
                current = []
                output.append(line)
            else:
                current.append(line)
            separated_by_blank = False
        elif line:
            output.append(line)
    if current:
        output.append(re.sub(r"\s+", " ", " ".join(current)).strip())

    return prefix + "\n\n" + "\n\n".join(output) + "\n"


def ensure_ref_blocks(refs: str) -> str:
    refs = coalesce_numbered_reference_entries(number_author_year_references(refs))
    return re.sub(
        r"(?m)^(?P<entry>\[(?P<n>\d+)\].*?)(?:[^\S\r\n]+\^ref-\d+)?[^\S\r\n]*$",
        lambda m: f"{m.group('entry').rstrip()} ^ref-{int(m.group('n'))}",
        refs,
    )


def normalize_ref_spacing(refs: str) -> str:
    return re.sub(
        r"(?m)(^\[\d+\]\s+.*\^ref-\d+[^\S\r\n]*\r?\n)(?![^\S\r\n]*\r?\n)(?=^\[\d+\]\s+)",
        r"\1\n",
        refs,
    )


def convert_inner(inner: str, refs: set[int]) -> str | None:
    normalized = inner.replace("–", "-").replace("—", "-").strip()
    range_match = re.fullmatch(r"(\d+)\s*-\s*(\d+)", normalized)
    if range_match:
        start, end = int(range_match.group(1)), int(range_match.group(2))
        if end < start or end - start > 20:
            return None
        numbers = list(range(start, end + 1))
    elif re.fullmatch(r"\d+(?:\s*,\s*\d+)*", normalized):
        numbers = [int(part.strip()) for part in normalized.split(",")]
    else:
        return None
    if any(number not in refs for number in numbers):
        return None
    return "<sup>,</sup>".join(citation_link(number) for number in numbers)


def build_author_year_map(refs: str) -> dict[tuple[str, str], int]:
    mapping: dict[tuple[str, str], int] = {}
    for match in re.finditer(r"(?m)^\[(?P<n>\d+)\]\s+(?P<entry>.+?)\s+\^ref-(?P=n)\s*$", refs):
        entry = match.group("entry")
        first_author = entry.split(",", 1)[0].strip()
        years = re.findall(r"\b((?:19|20)\d{2}[a-z]?)\b", entry)
        if not first_author or not years:
            continue
        number = int(match.group("n"))
        year = years[-1]
        mapping[(normalize_author_key(first_author), year)] = number
        if first_author.lower() == "team":
            mapping[(normalize_author_key("Octo Model Team"), year)] = number
        if first_author.lower() == "intelligence":
            mapping[(normalize_author_key("Physical Intelligence"), year)] = number
    return mapping


def parse_author_year_piece(piece: str, author_year_map: dict[tuple[str, str], int]) -> int | None:
    cleaned = piece.strip()
    cleaned = re.sub(r"^(e\.g\.|see|cf\.)\s*,?\s*", "", cleaned, flags=re.IGNORECASE)

    match = re.match(
        r"^(?P<author>.+?)\s+et\s+al\.,?\s+(?P<year>(?:19|20)\d{2}[a-z]?)$",
        cleaned,
        flags=re.IGNORECASE,
    )
    if not match:
        match = re.match(
            r"^(?P<author>.+?)\s*&\s*.+?,?\s+(?P<year>(?:19|20)\d{2}[a-z]?)$",
            cleaned,
            flags=re.IGNORECASE,
        )
    if not match:
        match = re.match(
            r"^(?P<author>[A-ZÀ-ÿ][A-Za-zÀ-ÿ'’.-]+(?:\s+[A-ZÀ-ÿ][A-Za-zÀ-ÿ'’.-]+){0,3}),?\s+(?P<year>(?:19|20)\d{2}[a-z]?)$",
            cleaned,
        )
    if not match:
        return None

    return author_year_map.get((normalize_author_key(match.group("author")), match.group("year")))


def convert_author_year_citations(segment: str, author_year_map: dict[tuple[str, str], int]) -> str:
    citation_group_re = re.compile(r"\(([^()\n]*(?:et\s+al\.|&)[^()\n]*(?:19|20)\d{2}[a-z]?[^()\n]*)\)")

    def repl(match: re.Match[str]) -> str:
        inner = match.group(1).strip()
        normalized = re.sub(r"\s*:\s*(?=[A-ZÀ-ÿ][A-Za-zÀ-ÿ'’.-]+\s+(?:et\s+al\.|&))", "; ", inner)
        parts = [part.strip() for part in re.split(r"\s*;\s*", normalized) if part.strip()]
        numbers: list[int] = []
        for part in parts:
            number = parse_author_year_piece(part, author_year_map)
            if number is None:
                return match.group(0)
            numbers.append(number)
        return "<sup>,</sup>".join(citation_link(number) for number in numbers)

    return citation_group_re.sub(repl, segment)


PROTECTED_SPAN_RE = re.compile(
    r"""
    (?P<frontmatter>\A---\r?\n.*?\r?\n---(?=\r?\n|\Z))
    |(?P<fenced>^(?P<fence>`{3,}|~{3,})[^\r\n]*\r?\n.*?^(?P=fence)[^\r\n]*(?:\r?\n|\Z))
    |(?P<display_math>(?<!\\)\$\$.*?(?<!\\)\$\$)
    |(?P<inline_code>(?P<ticks>`+)[^\r\n]*?(?P=ticks))
    |(?P<wikilink>!?\[\[[^\]\r\n]+\]\])
    |(?P<markdown_link>!?\[[^\]\r\n]*\]\([^\)\r\n]*\))
    |(?P<inline_math>(?<![\\$])\$(?!\$)(?:\\.|[^$\r\n])+?(?<!\\)\$(?!\$))
    """,
    re.MULTILINE | re.DOTALL | re.VERBOSE,
)


def transform_unprotected_spans(text: str, transform: Callable[[str], str]) -> str:
    """Apply a text transform outside Markdown/code/math/link protection spans."""
    output: list[str] = []
    cursor = 0
    for match in PROTECTED_SPAN_RE.finditer(text):
        output.append(transform(text[cursor : match.start()]))
        output.append(match.group(0))
        cursor = match.end()
    output.append(transform(text[cursor:]))
    return "".join(output)


def transform_outside_references(text: str, transform: Callable[[str], str]) -> str:
    """Transform paper prose and appendices while leaving the bibliography body untouched."""
    span = find_references_span(text)
    if not span:
        return transform_unprotected_spans(text, transform)
    start, end = span
    return (
        transform_unprotected_spans(text[:start], transform)
        + text[start:end]
        + transform_unprotected_spans(text[end:], transform)
    )


def normalize_arxiv_subject_labels(text: str) -> tuple[str, int]:
    """Render escaped arXiv subject metadata normally inside References only."""
    span = find_references_span(text)
    if not span:
        return text, 0
    start, end = span
    references, replacements = ARXIV_SUBJECT_LABEL_RE.subn(
        lambda match: f"[{match.group('subject')}]",
        text[start:end],
    )
    return text[:start] + references + text[end:], replacements


def count_arxiv_subject_labels(text: str) -> int:
    """Count recognizable escaped arXiv subject labels before they are rehomed."""
    return len(ARXIV_SUBJECT_LABEL_RE.findall(text))


def named_citation_keys(text: str) -> list[str]:
    """Return unique named citation keys in first-occurrence order, outside protected spans."""
    keys: list[str] = []
    seen: set[str] = set()

    def collect(segment: str) -> str:
        for match in NAMED_CITATION_RE.finditer(segment):
            for key in re.split(r"\s*,\s*", match.group("inner")):
                if key not in seen:
                    seen.add(key)
                    keys.append(key)
        return segment

    transform_outside_references(text, collect)
    return keys


def count_named_citation_groups(text: str) -> int:
    count = 0

    def collect(segment: str) -> str:
        nonlocal count
        count += len(NAMED_CITATION_RE.findall(segment))
        return segment

    transform_outside_references(text, collect)
    return count


def normalize_named_citations(
    text: str,
    bibtex_path: str | Path | None,
    pandoc_path: str | Path | None = None,
) -> str:
    """Resolve escaped LaTeXML citation-key groups against a verifiable BibTeX source."""
    keys = named_citation_keys(text)
    if not keys:
        return text
    if not bibtex_path:
        rendered = ", ".join(keys[:8])
        suffix = "..." if len(keys) > 8 else ""
        raise ValueError(
            "named citation keys require --bibtex-path; unresolved keys: " + rendered + suffix
        )

    entries = load_bibtex_csl(bibtex_path, pandoc_path)
    missing = [key for key in keys if key not in entries]
    if missing:
        raise ValueError("BibTeX is missing cited keys: " + ", ".join(missing))

    span = find_references_span(text)
    existing_numbers = reference_numbers(text[span[0] : span[1]]) if span else set()
    first_number = max(existing_numbers, default=0) + 1
    key_numbers = {key: first_number + index for index, key in enumerate(keys)}

    def replace(segment: str) -> str:
        def repl(match: re.Match[str]) -> str:
            group_keys = re.split(r"\s*,\s*", match.group("inner"))
            return "<sup>,</sup>".join(citation_link(key_numbers[key]) for key in group_keys)

        return NAMED_CITATION_RE.sub(repl, segment)

    normalized = transform_outside_references(text, replace)
    blocks = "\n\n".join(
        f"[{key_numbers[key]}] {format_csl_reference(entries[key])} ^ref-{key_numbers[key]}"
        for key in keys
    )
    span = find_references_span(normalized)
    if not span:
        return normalized.rstrip() + "\n\n## References\n\n" + blocks + "\n"
    start, end = span
    references = normalized[start:end].rstrip()
    after = normalized[end:].lstrip("\r\n")
    replacement = references + "\n\n" + blocks
    if after:
        return normalized[:start] + replacement.rstrip() + "\n\n" + after
    return normalized[:start] + replacement.rstrip() + "\n"


def _convert_citations_unprotected(segment: str, refs: set[int], author_year_map: dict[tuple[str, str], int]) -> str:
    segment = convert_author_year_citations(segment, author_year_map)

    def repl_bracket_range(match: re.Match[str]) -> str:
        replacement = convert_inner(f"{match.group('start')}-{match.group('end')}", refs)
        return replacement if replacement else match.group(0)

    segment = re.sub(
        r"(?<!!)(?<!\\)\[(?P<start>\d+)\]\s*[-–—]\s*\[(?P<end>\d+)\](?!\()",
        repl_bracket_range,
        segment,
    )

    def repl_list(match: re.Match[str]) -> str:
        replacement = convert_inner(match.group("inner"), refs)
        return replacement if replacement else match.group(0)

    segment = re.sub(r"(?<!!)(?<!\\)\[(?P<inner>\d+(?:\s*,\s*\d+)*)\](?!\()", repl_list, segment)
    segment = re.sub(
        r"(\[\[#\^ref-\d+\|[⁰¹²³⁴⁵⁶⁷⁸⁹]+\]\])\s*,\s*(?=\[\[#\^ref-\d+\|[⁰¹²³⁴⁵⁶⁷⁸⁹]+\]\])",
        r"\1<sup>,</sup>",
        segment,
    )
    return segment


def convert_citations(segment: str, refs: set[int], author_year_map: dict[tuple[str, str], int]) -> str:
    return transform_unprotected_spans(
        segment,
        lambda value: _convert_citations_unprotected(value, refs, author_year_map),
    )


def normalize(
    text: str,
    bibtex_path: str | Path | None = None,
    pandoc_path: str | Path | None = None,
) -> str:
    text = normalize_citation_footnotes(text)
    text, _ = normalize_arxiv_subject_labels(text)
    text = normalize_named_citations(text, bibtex_path, pandoc_path)
    span = find_references_span(text)
    if not span:
        return escape_markdown_table_citation_pipes(text)
    start, end = span
    before, refs, after = text[:start], text[start:end], text[end:]
    refs = normalize_ref_spacing(ensure_ref_blocks(refs))
    numbers = reference_numbers(refs)
    author_year_map = build_author_year_map(refs)
    converted_before = convert_citations(before, numbers, author_year_map)
    converted_after = convert_citations(after.lstrip("\r\n"), numbers, author_year_map)
    if converted_after:
        normalized = converted_before + refs.rstrip() + "\n\n" + converted_after
    else:
        normalized = converted_before + refs.rstrip() + "\n"
    return escape_markdown_table_citation_pipes(normalized)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--markdown-path", required=True)
    parser.add_argument("--bibtex-path")
    parser.add_argument("--pandoc-path")
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()

    path = Path(args.markdown_path)
    old = path.read_text(encoding="utf-8")
    new = normalize(old, args.bibtex_path, args.pandoc_path)
    changed = old != new
    if args.write and changed:
        atomic_write_text(path, new, min_bytes=100)
    print({"changed": changed, "wrote": bool(args.write and changed), "markdown_path": str(path)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
