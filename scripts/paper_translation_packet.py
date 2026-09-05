#!/usr/bin/env python3
"""Build, validate, cache, and merge compact bilingual-paper translation packets."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from safe_atomic_io import atomic_write_json, atomic_write_text


PACKET_SCHEMA_VERSION = 5
CONSTRAINTS_VERSION = "paper-bilingual-body-reference-title-suffix-v5"
BODY_CONSTRAINTS_VERSION = "paper-bilingual-body-v3"
REFERENCE_TITLE_CONSTRAINTS_VERSION = "paper-reference-title-suffix-v1"
LAYOUT_NAME = "english_blockquote_chinese_body"
PLACEHOLDER_RE = re.compile(r"\{\{ZH:(u\d{5})\}\}")
REF_TITLE_PLACEHOLDER_RE = re.compile(r"\{\{REFZH:(r\d{5})\}\}")
INLINE_MATH_RE = re.compile(r"(?<!\$)\$[^$\n]+\$(?!\$)")
CITATION_RE = re.compile(r"\[\[#\^ref-[^\]|]+\\?\|[^\]]+\]\]|<sup>,</sup>")
EMPHASIS_RE = re.compile(r"\*\*[^*\n]+\*\*")
FOOTNOTE_RE = re.compile(r"\[\^[A-Za-z0-9_-]+\]")
HEADING_RE = re.compile(r"^#{1,6}\s+")
IMAGE_RE = re.compile(r"^\s*!\[[^\]]*\]\([^)]+\)\s*$")
LIST_RE = re.compile(r"^(\s*)([-*+] |\d+[.)] )(.*)$")
REF_ENTRY_BLOCK_RE = re.compile(r"^\[(?P<n>\d+)\]\s+(?P<body>.+?)\s+\^ref-(?P=n)\s*$", re.DOTALL)
REF_TITLE_SUFFIX_RE = re.compile(r"(?P<base>.*?)《(?P<zh>[^《》\r\n]+)》\s*$", re.DOTALL)
EXPLICIT_TITLE_RE = re.compile(r"[\"“](?P<title>[^\"”\r\n]{3,})[\"”]")
ARXIV_STYLE_METADATA_TAIL_RE = re.compile(
    r",\s*(?:(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+)?"
    r"(?:19|20)\d{2}[a-z]?\.\s+(?:URL\b|arXiv\b)",
    re.IGNORECASE,
)
VENUE_MARKER_RE = re.compile(
    r"\.\s+(?:\(?arXiv\b|In\b|Proceedings\b|Proc\.\b|IEEE\b|ACM\b|"
    r"The\s+(?:International\s+)?Journal\b|Journal\b|Transactions\b|"
    r"Advances\b|Nature\b|Science\b|Springer\b|Morgan\s+Kaufmann\b|"
    r"Oxford\s+University\s+Press\b|Cambridge\s+University\s+Press\b)",
    re.IGNORECASE,
)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write_atomic(path: Path, content: str) -> None:
    atomic_write_text(path, content, min_bytes=2)


def write_json_atomic(path: Path, value: Any) -> None:
    atomic_write_json(path, value)


def split_frontmatter(text: str) -> tuple[str, str]:
    if not text.startswith("---\n"):
        return "", text
    marker = text.find("\n---\n", 4)
    if marker < 0:
        return "", text
    return text[: marker + 5], text[marker + 5 :]


def protected_tokens(text: str) -> dict[str, list[str]]:
    return {
        "inline_math": INLINE_MATH_RE.findall(text),
        "citation_tokens": CITATION_RE.findall(text),
        "markdown_footnotes": FOOTNOTE_RE.findall(text),
        "emphasis_tokens": EMPHASIS_RE.findall(text),
    }


def unit_kind(lines: list[str], abstract: bool) -> str:
    text = "\n".join(lines).strip()
    if abstract:
        return "abstract"
    if text.lower().startswith("keywords:"):
        return "keywords"
    if text.lower().startswith(("figure ", "fig. ", "fig ", "table ")):
        return "caption"
    if len(lines) == 1 and LIST_RE.match(lines[0]):
        return "list_item"
    return "paragraph"


CONTRIBUTOR_NON_NAME_RE = re.compile(
    r"\b(?:team|laborator(?:y|ies)|lab|university|institute|department|school|college|"
    r"company|corporation|authors?|contributors?|affiliations?|corresponding|equal|"
    r"contributed|contribution|contact|email|research|robotics)\b",
    re.IGNORECASE,
)
CONTRIBUTOR_NAME_TOKEN_RE = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ]+(?:[-'’][A-Za-zÀ-ÖØ-öø-ÿ]+)*")
HTML_TAG_RE = re.compile(r"<!--.*?-->|</?[A-Za-z][^<>]*>", re.DOTALL)


def is_contributor_passthrough(text: str) -> bool:
    """Recognize personal-name lists while ignoring contact-only identifiers."""
    value = re.sub(r"</?sup\b[^>]*>", "", text, flags=re.IGNORECASE)
    value = re.sub(r"<br\s*/?>", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"[⁰¹²³⁴⁵⁶⁷⁸⁹]+(?:\s*,\s*[⁰¹²³⁴⁵⁶⁷⁸⁹]+)*", "", value)
    value = re.sub(
        r"\[[^\]\n]*\]\((?:mailto:|https?://)[^)\n]+\)",
        " ",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"https?://\S+|\b\d{4}-\d{4}-\d{4}-\d{3}[\dX]\b|(?<!\w)@[A-Za-z0-9_.-]+", " ", value, flags=re.IGNORECASE)
    value = value.replace("·", ",")
    value = re.sub(r"^\s*(?:[-*+] |\d+[.)] )", "", value)
    if CONTRIBUTOR_NON_NAME_RE.search(value):
        return False
    if re.search(r"[^A-Za-zÀ-ÖØ-öø-ÿ\u4e00-\u9fff\s,;:&'’().\-–—/*†‡0-9]", value):
        return False
    latin_tokens = CONTRIBUTOR_NAME_TOKEN_RE.findall(value)
    if latin_tokens:
        allowed_lower = {"and", "de", "del", "der", "di", "la", "le", "van", "von"}
        if any(token.islower() and token.lower() not in allowed_lower for token in latin_tokens):
            return False
        return len([token for token in latin_tokens if token.lower() != "and"]) >= 2
    chinese = re.sub(r"[^\u4e00-\u9fff]", "", value)
    return len(chinese) >= 2


def is_protected_only_passthrough(text: str) -> bool:
    """Recognize units whose semantic content is entirely frozen transport tokens."""
    tokens = protected_tokens(text)
    frozen = [token for values in tokens.values() for token in values]
    if not frozen:
        return False
    residual = text
    for token in sorted(set(frozen), key=len, reverse=True):
        residual = residual.replace(token, "")
    residual = re.sub(r"[\s.,;:!?，。；：！？…()\[\]{}<>/\\|+\-=~`'\"“”‘’_*–—]+", "", residual)
    return not residual


def is_markup_only_passthrough(text: str) -> bool:
    """Recognize standalone HTML layout markers with no translatable text."""
    if not HTML_TAG_RE.search(text):
        return False
    residual = HTML_TAG_RE.sub("", text)
    residual = re.sub(
        r"[\s.,;:!?，。；：！？…()\[\]{}<>/\\|+\-=~`'\"“”‘’_*–—^†‡⁰¹²³⁴⁵⁶⁷⁸⁹]+",
        "",
        residual,
    )
    return not residual


def is_identity_or_numeric_passthrough(text: str) -> bool:
    """Recognize standalone labels whose exact spelling or value is the content."""
    value = text.strip()
    if re.fullmatch(r"\d[\d,]*(?:\.\d+)?(?:[KMBT])?", value, flags=re.IGNORECASE):
        return True
    if re.fullmatch(r"\[\^[^\]\n]+\]:\s*\d+", value):
        return True
    if re.fullmatch(r"(?:https?://|mailto:|doi:|orcid:)?[^\s]+", value, flags=re.IGNORECASE) and (
        "://" in value or "@" in value or value.lower().startswith(("doi:", "orcid:"))
    ):
        return True
    if re.fullmatch(r"[A-Z][A-Z0-9._/+\-]{1,20}", value):
        return True
    return bool(
        re.fullmatch(
            r"(?:[A-Z][A-Za-z0-9&.'’\-]*\s+){1,5}"
            r"(?:Robotics|Research|Labs?|Inc\.?|Corp\.?|LLC|Ltd\.?)",
            value,
        )
    )


def is_code_or_algorithm_passthrough(text: str) -> bool:
    """Recognize standalone code or pseudocode lines with no translatable prose."""
    if "\n" in text:
        return False
    value = text.strip().replace("\\_", "_")
    plain = value.replace("*", "").replace("`", "").strip()
    if not plain:
        return False
    if re.fullmatch(r"[⬆⬇←→↔⇐⇒⇔()\[\]{};,]+", plain):
        return True
    if re.match(
        r"^(?:class\s+[A-Za-z_]\w*(?:\([^)]*\))?\s*:|"
        r"(?:async\s+)?def\s+[A-Za-z_]\w*\s*\([^)]*\)\s*:?|"
        r"@[A-Za-z_]\w*(?:\([^)]*\))?)\s*$",
        plain,
    ):
        return True
    if re.match(
        r"^(?:\d+[A-Za-z]*function\b.*|function\b.*|"
        r"if\b.+\bthen\s*;?|else(?:\s+if)?\b.*|end(?:\s+(?:if|for|while|function))?\s*;?|"
        r"for\b.+\bdo\s*;?|for\s+[A-Za-z_]\w*\s+in\s+[^.!?。！？]+:|"
        r"while\b.+:|with\b.+:|"
        r"return\b.*|yield\b.*|break\s*;?|continue\s*;?|pass\s*;?)$",
        plain,
        re.IGNORECASE,
    ):
        return True
    if (
        ("←" in plain or "\\leftarrow" in plain)
        and not re.search(r"[.!?。！？]\s*$", plain)
        and not re.search(r"//\s*[A-Za-z]", plain)
    ):
        return True
    if re.fullmatch(
        r"(?:self\.)?[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+\s*\([^\r\n]*\)\s*;?",
        plain,
    ):
        return True
    if re.fullmatch(
        r"(?:self\.)?[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*\s*"
        r"(?:=|\+=|-=|\*=|/=)\s*[^\r\n]+[,;]?",
        plain,
    ) and not re.search(r"[!?。！？]\s*$", plain) and re.search(
        r"[_().0-9]|\b(?:None|True|False)\b", plain
    ):
        return True
    if re.fullmatch(
        r"(?:self\.)?[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*\s*\([^\r\n]*",
        plain,
    ):
        return True
    return False


def normalize_title_identity(text: str) -> str:
    """Compare frontmatter and rendered titles across filename-safe punctuation changes."""
    return re.sub(r"[\W_]+", "", text, flags=re.UNICODE).casefold()


def extract_reference_section(text: str) -> str:
    """Return the frozen References section, including its heading."""
    _, body = split_frontmatter(text)
    output: list[str] = []
    collecting = False
    for raw in body.splitlines(keepends=True):
        stripped = raw.strip()
        if HEADING_RE.match(stripped):
            title = HEADING_RE.sub("", stripped).strip().lower()
            if collecting and title != "references":
                break
            if title == "references":
                collecting = True
        if collecting:
            output.append(raw)
    return "".join(output).strip() + ("\n" if output else "")


def split_reference_title_suffix(body: str) -> tuple[str, str | None]:
    """Split the controlled Chinese-title suffix from the frozen English bibliography body."""
    match = REF_TITLE_SUFFIX_RE.fullmatch(body.strip())
    if not match:
        return body.strip(), None
    return match.group("base").rstrip(), match.group("zh").strip()


def extract_reference_entries(text: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for raw in extract_reference_section(text).splitlines():
        match = REF_ENTRY_BLOCK_RE.fullmatch(raw.strip())
        if not match:
            continue
        number = int(match.group("n"))
        english_entry, existing_zh = split_reference_title_suffix(match.group("body"))
        entries.append(
            {
                "reference_number": number,
                "english_entry": english_entry,
                "reference_block_id": f"^ref-{number}",
                "reference_entry_sha256": sha256_text(english_entry),
                "existing_zh": existing_zh,
            }
        )
    return entries


def _author_separator_candidates(entry: str, title_end: int) -> list[int]:
    candidates: list[int] = []
    for match in re.finditer(r"\.\s+", entry[:title_end]):
        prefix = entry[: match.start()].rstrip()
        last_token_match = re.search(r"([A-Za-zÀ-ÖØ-öø-ÿ'’\-]+)$", prefix)
        last_token = last_token_match.group(1) if last_token_match else ""
        if len(last_token) <= 2:
            continue
        if not ("," in prefix or re.search(r"\b(?:and|et\s+al)\b", prefix, re.IGNORECASE)):
            continue
        candidates.append(match.end())
    return candidates


def infer_reference_title(english_entry: str) -> tuple[str | None, str]:
    """Extract high-confidence titles, skip non-semantic records, and send ambiguity to the worker."""
    entry = english_entry.strip()
    author_year = re.search(r"\(\d{4}[a-z]?\)\s+(?P<title>.+?)(?=\.\s+)", entry)
    if author_year:
        title = author_year.group("title").strip()
        tail = entry[author_year.end("title") :]
        if len(title.split()) >= 2 and VENUE_MARKER_RE.match(tail):
            return title, "deterministic"
    metadata_tail = ARXIV_STYLE_METADATA_TAIL_RE.search(entry)
    if metadata_tail:
        starts = _author_separator_candidates(entry, metadata_tail.start())
        if starts:
            title = entry[starts[-1] : metadata_tail.start()].strip()
            if (
                len(title.split()) >= 2
                and not re.fullmatch(r"https?://\S+", title)
                and not is_reference_metadata_only(title)
            ):
                return title, "deterministic"
    first_sentence_end = re.search(r"\.\s+", entry)
    for explicit in EXPLICIT_TITLE_RE.finditer(entry):
        # Quoted author aliases such as Linxi "Jim" Fan occur before the
        # author-list sentence boundary and are metadata, not work titles.
        if first_sentence_end and explicit.start() < first_sentence_end.end():
            continue
        return explicit.group("title").strip(), "deterministic"
    venue = VENUE_MARKER_RE.search(entry)
    if venue:
        starts = _author_separator_candidates(entry, venue.start())
        if starts:
            title = entry[starts[-1] : venue.start()].strip()
            if (
                len(title.split()) >= 2
                and not re.fullmatch(r"https?://\S+", title)
                and not is_reference_metadata_only(title)
            ):
                return title, "deterministic"
    if is_reference_without_translatable_title(entry):
        return None, "none"
    return None, "worker"


def is_reference_metadata_only(text: str) -> bool:
    """Return true when a candidate contains identifiers and dates but no natural-language title."""
    value = re.sub(r"https?://\S+", " ", text, flags=re.IGNORECASE)
    value = re.sub(r"\bdoi\s*:\s*\S+", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"\barXiv(?:\s+preprint)?\s*:?\s*[\w./-]+", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\b", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"\b(?:19|20)\d{2}[a-z]?\b", " ", value)
    value = re.sub(r"\bURL\b|\^ref-[\w.-]+", " ", value, flags=re.IGNORECASE)
    words = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ]{2,}", value)
    return not words


def is_reference_without_translatable_title(entry: str) -> bool:
    """Recognize bibliography records that contain no natural-language work title."""
    if re.search(
        r"\.\s+[A-Za-z][A-Za-z0-9_.+-]*\s*,\s*(?:19|20)\d{2}\b",
        entry,
    ) and re.search(r"\bURL\b|https?://", entry, re.IGNORECASE):
        return True
    value = re.sub(r"https?://\S+", " ", entry, flags=re.IGNORECASE)
    value = re.sub(r"\bdoi\s*:\s*\S+", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"\barXiv(?:\s+preprint)?\s*:?\s*[\w./-]+(?:\s*\[[^\]]+\])?", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"\bURL\b|\^ref-[\w.-]+", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\b", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"\b(?:19|20)\d{2}[a-z]?\b", " ", value)
    value = re.sub(r"[\s,;:().\-–—/\\]+", " ", value).strip()
    if not re.search(r"[A-Za-zÀ-ÖØ-öø-ÿ\u4e00-\u9fff]", value):
        return True
    return is_contributor_passthrough(value)


def extract_reference_title_units(text: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    units: list[dict[str, Any]] = []
    descriptors: list[dict[str, Any]] = []
    for entry in extract_reference_entries(text):
        number = int(entry["reference_number"])
        title_hint, extraction_mode = infer_reference_title(str(entry["english_entry"]))
        descriptor = {**entry, "title_hint": title_hint, "extraction_mode": extraction_mode}
        if entry.get("existing_zh"):
            descriptors.append({**descriptor, "unit_id": None, "status": "reused"})
            continue
        if extraction_mode == "none":
            descriptors.append({**descriptor, "unit_id": None, "status": "skipped"})
            continue
        unit_id = f"r{number:05d}"
        english = title_hint if title_hint is not None else str(entry["english_entry"])
        protected = protected_tokens(english) if title_hint is not None else {
            "inline_math": [],
            "citation_tokens": [],
            "markdown_footnotes": [],
            "emphasis_tokens": [],
        }
        units.append(
            {
                "unit_id": unit_id,
                "order": len(units) + 1,
                "kind": "reference_title",
                "requires_chinese": True,
                "english": english,
                "source_sha256": sha256_text(english),
                "protected": protected,
                "reference_number": number,
                "reference_entry": str(entry["english_entry"]),
                "reference_entry_sha256": str(entry["reference_entry_sha256"]),
                "reference_block_id": str(entry["reference_block_id"]),
                "title_hint": title_hint,
                "extraction_mode": extraction_mode,
            }
        )
        descriptors.append({**descriptor, "unit_id": unit_id, "status": "pending"})
    return units, descriptors


def canonical_reference_source_section(text: str) -> str:
    """Remove only controlled title suffixes so the frozen English bibliography can be hashed."""
    section = extract_reference_section(text)
    output: list[str] = []
    for raw in section.splitlines(keepends=True):
        newline = "\n" if raw.endswith("\n") else ""
        line = raw.rstrip("\r\n")
        match = REF_ENTRY_BLOCK_RE.fullmatch(line.strip())
        if not match:
            output.append(raw)
            continue
        english_entry, _ = split_reference_title_suffix(match.group("body"))
        output.append(f"[{match.group('n')}] {english_entry} ^ref-{match.group('n')}" + newline)
    return "".join(output).strip() + ("\n" if output else "")


def extract_translation_units(text: str, *, include_references: bool = False) -> list[dict[str, Any]]:
    """Extract only prose units; headings and protected Markdown never enter a packet."""
    frontmatter, body = split_frontmatter(text)
    title_match = re.search(r"(?m)^title:\s*(.*?)\s*$", frontmatter)
    frontmatter_title = title_match.group(1).strip().strip("\"'") if title_match else ""
    normalized_frontmatter_title = normalize_title_identity(frontmatter_title)
    lines = body.splitlines()
    units: list[dict[str, Any]] = []
    paragraph: list[str] = []
    abstract_section = False
    contributors_section = False
    code_fence = False
    display_math = False
    references = False
    preamble_section = True

    def flush() -> None:
        nonlocal paragraph
        if not paragraph:
            return
        raw = "\n".join(paragraph).strip()
        paragraph = []
        if not raw or raw.startswith(">") or IMAGE_RE.fullmatch(raw):
            return
        reference_match = REF_ENTRY_BLOCK_RE.match(raw) if references else None
        if references and reference_match is None:
            return
        index = len(units) + 1
        kind = "reference" if reference_match else unit_kind(raw.splitlines(), abstract_section)
        text_value = (
            f"[{reference_match.group('n')}] {reference_match.group('body').strip()}"
            if reference_match
            else raw
        )
        bullet_prefix = None
        if kind == "list_item":
            match = LIST_RE.match(raw)
            assert match is not None
            bullet_prefix = match.group(1) + match.group(2)
            text_value = match.group(3).strip()
        normalized_raw = normalize_title_identity(raw)
        contributor_passthrough = (
            reference_match is None
            and is_contributor_passthrough(raw)
            and (
                contributors_section
                or (preamble_section and normalized_raw != normalized_frontmatter_title)
            )
        )
        protected = protected_tokens(text_value)
        deterministic_passthrough = reference_match is None and (
            is_protected_only_passthrough(text_value)
            or is_markup_only_passthrough(text_value)
            or is_identity_or_numeric_passthrough(text_value)
            or is_code_or_algorithm_passthrough(text_value)
        )
        if contributor_passthrough or deterministic_passthrough:
            kind = "passthrough"
        unit = {
            "unit_id": f"u{index:05d}",
            "order": index,
            "kind": kind,
            "requires_chinese": kind != "passthrough",
            "english": text_value,
            "source_sha256": sha256_text(text_value),
            "protected": protected,
        }
        if bullet_prefix is not None:
            unit["bullet_prefix"] = bullet_prefix
        if reference_match:
            unit["reference_block_id"] = f"^ref-{reference_match.group('n')}"
        units.append(unit)

    for raw in lines:
        stripped = raw.strip()
        if stripped.startswith("```"):
            flush()
            code_fence = not code_fence
            continue
        if code_fence:
            continue
        if stripped == "$$":
            flush()
            display_math = not display_math
            continue
        if display_math:
            continue
        if HEADING_RE.match(stripped):
            flush()
            title = HEADING_RE.sub("", stripped).strip().lower()
            references = title == "references"
            abstract_section = title == "abstract"
            contributors_section = title in {"contributors", "authors"}
            normalized_heading = normalize_title_identity(title)
            if not contributors_section and normalized_heading != normalized_frontmatter_title:
                preamble_section = False
            continue
        if references:
            if not include_references:
                continue
            if REF_ENTRY_BLOCK_RE.match(stripped):
                flush()
                paragraph = [raw]
                flush()
            elif paragraph or re.match(r"^\[\d+\]\s+", stripped):
                paragraph.append(raw)
            continue
        if not stripped:
            flush()
            continue
        if IMAGE_RE.fullmatch(stripped) or stripped.startswith("<table") or stripped.startswith("|") or stripped.startswith("<tr"):
            flush()
            continue
        if LIST_RE.match(raw):
            flush()
            paragraph = [raw]
            flush()
            continue
        paragraph.append(raw)
    flush()
    return units


def build_packet(markdown_path: Path, packet_path: Path, constraints_fingerprint: str = CONSTRAINTS_VERSION) -> dict[str, Any]:
    markdown = read_text(markdown_path)
    body_units = extract_translation_units(markdown)
    reference_title_units, reference_titles = extract_reference_title_units(markdown)
    units = body_units + reference_title_units
    for order, unit in enumerate(units, 1):
        unit["order"] = order
    reference_section = extract_reference_section(markdown)
    reference_source_section = canonical_reference_source_section(markdown)
    packet = {
        "schema_version": PACKET_SCHEMA_VERSION,
        "constraints_fingerprint": constraints_fingerprint,
        "source_markdown_sha256": sha256_text(markdown),
        "bilingual_layout": LAYOUT_NAME,
        "body_translation_units": len(body_units),
        "passthrough_units": sum(1 for unit in body_units if unit.get("kind") == "passthrough"),
        "layout_units_total": len(units),
        "model_units_total": sum(1 for unit in units if unit.get("requires_chinese", True)),
        "reference_title_units": len(reference_title_units),
        "reference_title_fallback_units": sum(1 for item in reference_titles if item.get("extraction_mode") == "worker"),
        "reference_title_reused_units": sum(1 for item in reference_titles if item.get("status") == "reused"),
        "reference_title_skipped_units": sum(1 for item in reference_titles if item.get("status") == "skipped"),
        "reference_blocks": len(re.findall(r"(?m)^\[\d+\].*\^ref-\d+\s*$", reference_section)),
        "reference_titles": reference_titles,
        "reference_source_section_sha256": sha256_text(reference_source_section),
        "reference_section_sha256": sha256_text(reference_source_section),
        "units": units,
    }
    write_json_atomic(packet_path, packet)
    return packet


def read_packet(packet_path: Path) -> dict[str, Any]:
    packet = json.loads(read_text(packet_path))
    if packet.get("schema_version") not in {1, 2, 3, 4, PACKET_SCHEMA_VERSION}:
        raise ValueError("unsupported translation packet schema")
    if not isinstance(packet.get("units"), list):
        raise ValueError("translation packet has no units")
    return packet


def packet_jsonl(packet: dict[str, Any]) -> str:
    rows = [json.dumps(unit, ensure_ascii=False, separators=(",", ":")) for unit in packet["units"]]
    return "\n".join(rows) + ("\n" if rows else "")


def parse_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, raw in enumerate(read_text(path).splitlines(), 1):
        if not raw.strip():
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSONL at line {line_number}: {exc.msg}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"translation output line {line_number} is not an object")
        rows.append(value)
    return rows


def validate_output(packet: dict[str, Any], output_path: Path) -> tuple[dict[str, dict[str, str]], list[str]]:
    expected = {str(unit["unit_id"]): unit for unit in packet["units"]}
    output: dict[str, dict[str, str]] = {}
    errors: list[str] = []
    raw_bytes = output_path.read_bytes()
    if raw_bytes.startswith(b"\xef\xbb\xbf"):
        errors.append("translation output must be UTF-8 without BOM")
    if "\ufffd" in raw_bytes.decode("utf-8", errors="replace"):
        errors.append("translation output contains a Unicode replacement character")
    for row in parse_jsonl(output_path):
        unit_id = row.get("unit_id")
        chinese = row.get("zh")
        if not isinstance(unit_id, str) or unit_id not in expected:
            errors.append(f"unknown unit_id: {unit_id!r}")
            continue
        if unit_id in output:
            errors.append(f"duplicate unit_id: {unit_id}")
            continue
        if not isinstance(chinese, str) or not chinese.strip():
            errors.append(f"empty Chinese translation: {unit_id}")
            continue
        english = str(expected[unit_id].get("english", ""))
        deterministic_identity = is_identity_or_numeric_passthrough(english)
        if (
            expected[unit_id].get("requires_chinese", True)
            and expected[unit_id].get("kind") != "reference"
            and re.fullmatch(r"\([A-Za-z0-9]+\)", english.strip()) is None
            and not re.search(r"[\u4e00-\u9fff]", chinese)
            and not (deterministic_identity and chinese.strip() == english.strip())
        ):
            errors.append(f"translation contains no Chinese text: {unit_id}")
        if expected[unit_id].get("kind") == "passthrough" and chinese.strip() != english.strip():
            errors.append(f"passthrough unit must preserve source text exactly: {unit_id}")
        if expected[unit_id].get("kind") == "reference":
            number = re.match(r"^\[(\d+)\]", str(expected[unit_id].get("english", "")))
            if number and not chinese.lstrip().startswith(f"[{number.group(1)}]"):
                errors.append(f"{unit_id} reference translation must preserve leading [{number.group(1)}]")
        validated = {"zh": chinese.strip()}
        protected_source = english
        if expected[unit_id].get("kind") == "reference_title":
            source_title = row.get("source_title")
            entry = str(expected[unit_id].get("reference_entry", ""))
            hint = expected[unit_id].get("title_hint")
            if not isinstance(source_title, str) or not source_title.strip():
                errors.append(f"missing source_title for reference title: {unit_id}")
            else:
                source_title = source_title.strip()
                if source_title not in entry:
                    errors.append(f"{unit_id} source_title is not an exact substring of the English reference entry")
                if hint is not None and source_title != hint:
                    errors.append(f"{unit_id} source_title does not match deterministic title_hint")
                validated["source_title"] = source_title
                protected_source = source_title
            if any(token in chinese for token in ("《", "》", "^ref-", "{{", "}}")) or "\n" in chinese or "\r" in chinese:
                errors.append(f"{unit_id} Chinese title contains forbidden markup or line breaks")
            if re.match(r"^\s*\[\d+\]", chinese):
                errors.append(f"{unit_id} Chinese title must not contain a reference number")
        protected = protected_tokens(protected_source) if expected[unit_id].get("kind") == "reference_title" else expected[unit_id]["protected"]
        for category, tokens in protected.items():
            for token in tokens:
                expected_count = protected_source.count(token)
                actual_count = chinese.count(token)
                if actual_count != expected_count:
                    errors.append(
                        f"{unit_id} changed {category} token count: expected={expected_count} actual={actual_count} token={token}"
                    )
        output[unit_id] = validated
    missing = sorted(set(expected) - set(output))
    if missing:
        errors.append("missing unit_ids: " + ", ".join(missing))
    return output, errors


def blockquote(text: str) -> str:
    return "\n".join("> " + line if line else ">" for line in text.splitlines())


def build_merge_template(markdown_path: Path, packet: dict[str, Any]) -> str:
    original = read_text(markdown_path)
    legacy_references = packet.get("schema_version") == 2
    actual = extract_translation_units(original, include_references=legacy_references)
    expected = [unit for unit in packet["units"] if unit.get("kind") != "reference_title"]
    if len(actual) != len(expected):
        raise ValueError("prepared Markdown translation-unit count changed")
    for expected_unit, actual_unit in zip(expected, actual, strict=True):
        if expected_unit["unit_id"] != actual_unit["unit_id"] or expected_unit["source_sha256"] != actual_unit["source_sha256"]:
            raise ValueError("prepared Markdown translation units no longer match packet")

    _, body = split_frontmatter(original)
    prefix = original[: len(original) - len(body)]
    lines = body.splitlines(keepends=True)
    output: list[str] = []
    unit_index = 0
    paragraph: list[str] = []
    code_fence = False
    display_math = False
    references = False

    def flush() -> None:
        nonlocal paragraph, unit_index
        if not paragraph:
            return
        raw = "".join(paragraph)
        stripped = raw.strip()
        paragraph = []
        if not stripped or stripped.startswith(">") or IMAGE_RE.fullmatch(stripped):
            output.append(raw)
            return
        unit = actual[unit_index]
        expected_unit = expected[unit_index]
        if sha256_text(unit["english"]) != expected_unit["source_sha256"]:
            raise ValueError("merge source mismatch")
        english = unit["english"]
        placeholder = "{{ZH:" + str(unit["unit_id"]) + "}}"
        if unit["kind"] == "passthrough":
            output.append(blockquote(english) + "\n\n" + placeholder + "\n")
        elif unit["kind"] == "list_item":
            prefix_value = str(unit.get("bullet_prefix", "- "))
            output.append(blockquote(prefix_value + english) + "\n\n" + placeholder + "\n")
        else:
            output.append(blockquote(english) + "\n\n" + placeholder + "\n")
        unit_index += 1

    for raw in lines:
        line = raw.rstrip("\r\n")
        stripped = line.strip()
        if stripped.startswith("```"):
            flush(); code_fence = not code_fence; output.append(raw); continue
        if code_fence:
            output.append(raw); continue
        if stripped == "$$":
            flush(); display_math = not display_math; output.append(raw); continue
        if display_math:
            output.append(raw); continue
        if HEADING_RE.match(stripped):
            flush(); title = HEADING_RE.sub("", stripped).strip().lower(); references = title == "references"; output.append(raw); continue
        if references:
            if int(packet.get("schema_version", 1)) >= 4:
                flush()
                match = REF_ENTRY_BLOCK_RE.fullmatch(stripped)
                if not match:
                    output.append(raw)
                    continue
                number = int(match.group("n"))
                descriptor = next((item for item in packet.get("reference_titles", []) if int(item["reference_number"]) == number), None)
                if descriptor is None or descriptor.get("status") in {"reused", "skipped"}:
                    output.append(raw)
                    continue
                english_entry, _ = split_reference_title_suffix(match.group("body"))
                output.append(f"[{number}] {english_entry}{{{{REFZH:{descriptor['unit_id']}}}}} ^ref-{number}\n")
                continue
            if not legacy_references:
                flush(); output.append(raw); continue
            if REF_ENTRY_BLOCK_RE.match(stripped):
                flush(); paragraph = [raw]; flush()
            elif paragraph or re.match(r"^\[\d+\]\s+", stripped):
                paragraph.append(raw)
            else:
                flush(); output.append(raw)
            continue
        if not stripped:
            flush(); output.append(raw); continue
        if IMAGE_RE.fullmatch(stripped) or stripped.startswith("<table") or stripped.startswith("|") or stripped.startswith("<tr"):
            flush(); output.append(raw); continue
        if LIST_RE.match(line):
            flush(); paragraph = [raw]; flush(); continue
        paragraph.append(raw)
    flush()
    if unit_index != len(expected):
        raise ValueError("not all translation units were placed in the layout template")
    template = prefix + "".join(output)
    found = PLACEHOLDER_RE.findall(template)
    expected_ids = [str(unit["unit_id"]) for unit in expected]
    if found != expected_ids:
        raise ValueError("layout template placeholders do not match packet order")
    if int(packet.get("schema_version", 1)) >= 4:
        found_reference_ids = REF_TITLE_PLACEHOLDER_RE.findall(template)
        expected_reference_ids = [str(unit["unit_id"]) for unit in packet["units"] if unit.get("kind") == "reference_title"]
        if found_reference_ids != expected_reference_ids:
            raise ValueError("layout template reference-title placeholders do not match packet order")
    return template


def fill_merge_template(template: str, packet: dict[str, Any], translations: dict[str, Any]) -> str:
    result = template
    for unit in packet["units"]:
        unit_id = str(unit["unit_id"])
        is_reference_title = unit.get("kind") == "reference_title"
        placeholder = ("{{REFZH:" if is_reference_title else "{{ZH:") + unit_id + "}}"
        if result.count(placeholder) != 1:
            raise ValueError(f"layout template must contain exactly one placeholder for {unit_id}")
        value = translations[unit_id]
        chinese = str(value.get("zh", "")) if isinstance(value, dict) else str(value)
        if unit["kind"] == "list_item":
            chinese = str(unit.get("bullet_prefix", "- ")) + chinese
        elif unit["kind"] == "reference":
            chinese = chinese.rstrip() + " " + str(unit["reference_block_id"])
        elif is_reference_title:
            chinese = "《" + chinese.strip() + "》"
        result = result.replace(placeholder, chinese)
    residual = PLACEHOLDER_RE.findall(result)
    if residual:
        raise ValueError("layout template still contains translation placeholders: " + ", ".join(residual))
    reference_residual = REF_TITLE_PLACEHOLDER_RE.findall(result)
    if reference_residual:
        raise ValueError("layout template still contains reference-title placeholders: " + ", ".join(reference_residual))
    return result


def merge_translations(markdown_path: Path, packet: dict[str, Any], translations: dict[str, Any]) -> str:
    return fill_merge_template(build_merge_template(markdown_path, packet), packet, translations)


def cache_key(unit: dict[str, Any], constraints_fingerprint: str, translator_fingerprint: str) -> str:
    contract = REFERENCE_TITLE_CONSTRAINTS_VERSION if unit.get("kind") == "reference_title" else BODY_CONSTRAINTS_VERSION
    return sha256_text("\0".join((str(unit["source_sha256"]), str(unit["kind"]), contract)))


def legacy_v3_cache_key(unit: dict[str, Any], translator_fingerprint: str) -> str:
    return sha256_text(
        "\0".join(
            (
                str(unit["source_sha256"]),
                str(unit["kind"]),
                "paper-bilingual-body-no-references-v3",
                translator_fingerprint,
            )
        )
    )


QUALITY_TIER_ORDER = {"standard": 1, "publication": 2}


def verified_quality_tier(runtime: dict[str, Any]) -> str | None:
    if runtime.get("runtime_verified") is not True:
        return None
    effort = str(runtime.get("reasoning_effort") or runtime.get("actual_reasoning_effort") or "").lower()
    return "publication" if effort in {"high", "xhigh", "max", "ultra"} else "standard"


def load_cache(
    path: Path,
    *,
    expected_quality_tier: str = "publication",
    strict_runtime: bool = False,
    expected_model: str | None = None,
    expected_reasoning_effort: str | None = None,
) -> dict[str, dict[str, str]]:
    if not path.is_file():
        return {}
    result: dict[str, dict[str, str]] = {}
    for row in parse_jsonl(path):
        quality_tier = verified_quality_tier(row)
        quality_ok = bool(
            quality_tier
            and QUALITY_TIER_ORDER[quality_tier]
            >= QUALITY_TIER_ORDER.get(expected_quality_tier, QUALITY_TIER_ORDER["publication"])
        )
        model_ok = not strict_runtime or expected_model is None or row.get("actual_model") == expected_model
        effort_ok = not strict_runtime or expected_reasoning_effort is None or str(row.get("actual_reasoning_effort", "")).lower() == expected_reasoning_effort.lower()
        if isinstance(row.get("cache_key"), str) and isinstance(row.get("zh"), str) and quality_ok and model_ok and effort_ok:
            result[row["cache_key"]] = {
                key: str(row[key])
                for key in ("zh", "source_title", "translator_fingerprint", "actual_model", "actual_reasoning_effort", "quality_tier")
                if isinstance(row.get(key), str)
            }
            result[row["cache_key"]]["quality_tier"] = str(quality_tier)
    return result


def append_validated_cache(
    path: Path,
    packet: dict[str, Any],
    translations: dict[str, Any],
    translator_fingerprint: str,
    runtime: dict[str, Any] | None = None,
) -> None:
    existing_rows: dict[str, dict[str, Any]] = {}
    if path.is_file():
        for row in parse_jsonl(path):
            if isinstance(row.get("cache_key"), str) and isinstance(row.get("zh"), str):
                existing_rows[str(row["cache_key"])] = row
    runtime = runtime or {}
    for unit in packet["units"]:
        if unit.get("requires_chinese", True) is False or unit.get("kind") == "passthrough":
            continue
        key = cache_key(unit, str(packet["constraints_fingerprint"]), translator_fingerprint)
        if str(unit["unit_id"]) not in translations:
            continue
        value = translations[str(unit["unit_id"])]
        validated = value if isinstance(value, dict) else {"zh": str(value)}
        existing_rows[key] = {
            "cache_key": key,
            "zh": validated["zh"],
            **({"source_title": validated["source_title"]} if validated.get("source_title") else {}),
            "unit_kind": unit.get("kind"),
            "translator_fingerprint": translator_fingerprint,
            "actual_model": runtime.get("model"),
            "actual_reasoning_effort": runtime.get("reasoning_effort"),
            "runtime_verified": bool(runtime.get("runtime_verified", False)),
            "quality_tier": verified_quality_tier(runtime),
        }
    rows = [existing_rows[key] for key in sorted(existing_rows)]
    write_atomic(path, "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))


def build_pending_packet(
    packet: dict[str, Any],
    cache_path: Path,
    translator_fingerprint: str,
    pending_packet_path: Path,
    cached_output_path: Path,
    *,
    expected_quality_tier: str = "publication",
    strict_runtime: bool = False,
    expected_model: str | None = None,
    expected_reasoning_effort: str | None = None,
) -> dict[str, Any]:
    cached = load_cache(
        cache_path,
        expected_quality_tier=expected_quality_tier,
        strict_runtime=strict_runtime,
        expected_model=expected_model,
        expected_reasoning_effort=expected_reasoning_effort,
    )
    pending: list[dict[str, Any]] = []
    cached_rows: list[dict[str, str]] = []
    cache_hit_count = 0
    passthrough_count = 0
    for unit in packet["units"]:
        if unit.get("requires_chinese", True) is False or unit.get("kind") == "passthrough":
            cached_rows.append({"unit_id": str(unit["unit_id"]), "zh": str(unit.get("english", ""))})
            passthrough_count += 1
            continue
        key = cache_key(unit, str(packet["constraints_fingerprint"]), translator_fingerprint)
        if key not in cached and unit.get("kind") != "reference_title":
            for candidate in cached.values():
                legacy_fingerprint = candidate.get("translator_fingerprint")
                if legacy_fingerprint and legacy_v3_cache_key(unit, legacy_fingerprint) in cached:
                    cached[key] = candidate
                    break
        if key in cached:
            cache_hit_count += 1
            cached_rows.append(
                {
                    "unit_id": str(unit["unit_id"]),
                    **{field: cached[key][field] for field in ("zh", "source_title") if field in cached[key]},
                }
            )
        else:
            pending.append(unit)
    pending_packet = {**packet, "units": pending}
    pending_content = packet_jsonl(pending_packet)
    if pending_content:
        write_atomic(pending_packet_path, pending_content)
    else:
        atomic_write_text(pending_packet_path, "", min_bytes=0)
    if cached_rows:
        write_atomic(cached_output_path, "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in cached_rows))
    else:
        cached_output_path.unlink(missing_ok=True)
    reference_ids = {str(unit["unit_id"]) for unit in packet["units"] if unit.get("kind") == "reference_title"}
    cached_ids = {str(row["unit_id"]) for row in cached_rows}
    return {
        "pending_count": len(pending),
        "cached_count": cache_hit_count,
        "passthrough_count": passthrough_count,
        "pending_reference_title_units": sum(1 for unit in pending if unit.get("kind") == "reference_title"),
        "cached_reference_title_units": len(reference_ids & cached_ids),
        "pending_packet": str(pending_packet_path),
        "cached_output": str(cached_output_path),
    }


def combine_cached_and_worker_output(cached_output_path: Path, worker_output_path: Path, combined_output_path: Path) -> Path:
    rows = []
    if cached_output_path.is_file():
        rows.extend(parse_jsonl(cached_output_path))
    if worker_output_path.is_file():
        rows.extend(parse_jsonl(worker_output_path))
    write_atomic(combined_output_path, "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
    return combined_output_path


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def update_workflow_state(path: Path, *, stage: str, status: str, duration_ms: int | None = None, artifacts: dict[str, str] | None = None, error: str | None = None, reused: bool | None = None, metadata: dict[str, Any] | None = None, state_updates: dict[str, Any] | None = None) -> dict[str, Any]:
    state: dict[str, Any] = json.loads(read_text(path)) if path.is_file() else {"schema_version": 5, "stages": {}, "artifacts": {}}
    state["schema_version"] = 5
    if state_updates:
        state.update(state_updates)
    stages = state.setdefault("stages", {})
    previous = stages.get(stage) or {}
    preserved_attempts = list(previous.get("attempts") or []) if stage == "translation_worker" else []
    if status == "pending":
        stages[stage] = {
            "status": status,
            "started_at": now(),
            **({"attempts": preserved_attempts} if preserved_attempts else {}),
            **({"metadata": metadata} if metadata else {}),
        }
    else:
        finished_at = now()
        started_at = previous.get("started_at")
        if duration_ms is None and isinstance(started_at, str):
            try:
                duration_ms = round((datetime.fromisoformat(finished_at.replace("Z", "+00:00")) - datetime.fromisoformat(started_at.replace("Z", "+00:00"))).total_seconds() * 1000)
            except ValueError:
                duration_ms = None
        stages[stage] = {"status": status, "finished_at": finished_at, **({"started_at": started_at} if started_at else {}), **({"duration_ms": duration_ms} if duration_ms is not None else {}), **({"attempts": preserved_attempts} if preserved_attempts else {}), **({"error": error} if error else {}), **({"reused": reused} if reused is not None else {}), **({"metadata": metadata} if metadata else {})}
    if artifacts:
        state.setdefault("artifacts", {}).update(artifacts)
    write_json_atomic(path, state)
    return state


def append_worker_attempt(path: Path, attempt: dict[str, Any], *, status: str) -> dict[str, Any]:
    state: dict[str, Any] = json.loads(read_text(path)) if path.is_file() else {"schema_version": 5, "stages": {}, "artifacts": {}}
    state["schema_version"] = 5
    stages = state.setdefault("stages", {})
    previous = stages.get("translation_worker") or {}
    attempts = list(previous.get("attempts") or [])
    attempt = {**attempt, "attempt": len(attempts) + 1}
    attempts.append(attempt)
    stages["translation_worker"] = {
        "status": status,
        "attempts": attempts,
        "finished_at": attempt.get("finished_at", now()),
        "duration_ms": attempt.get("duration_ms"),
        "metadata": {
            "pending_count": attempt.get("pending_units", 0),
            "completion_waits": attempt.get("completion_waits", 1),
            "runtime_verified": attempt.get("runtime_verified", False),
        },
    }
    write_json_atomic(path, state)
    return state


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--markdown-path", required=True)
    prepare.add_argument("--packet-path", required=True)
    prepare.add_argument("--constraints-fingerprint", default=CONSTRAINTS_VERSION)
    validate = subparsers.add_parser("validate")
    validate.add_argument("--packet-path", required=True)
    validate.add_argument("--translation-output", required=True)
    merge = subparsers.add_parser("merge")
    merge.add_argument("--markdown-path", required=True)
    merge.add_argument("--packet-path", required=True)
    merge.add_argument("--translation-output", required=True)
    merge.add_argument("--output-markdown", required=True)
    template = subparsers.add_parser("template")
    template.add_argument("--markdown-path", required=True)
    template.add_argument("--packet-path", required=True)
    template.add_argument("--output-markdown", required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        packet = build_packet(Path(args.markdown_path), Path(args.packet_path), args.constraints_fingerprint)
        print(json.dumps({"ok": True, "unit_count": len(packet["units"]), "packet_path": args.packet_path}, ensure_ascii=False))
        return 0
    packet = read_packet(Path(args.packet_path))
    if args.command == "template":
        write_atomic(Path(args.output_markdown), build_merge_template(Path(args.markdown_path), packet))
        print(json.dumps({"ok": True, "output_markdown": args.output_markdown}, ensure_ascii=False))
        return 0
    translations, errors = validate_output(packet, Path(args.translation_output))
    if args.command == "validate":
        print(json.dumps({"ok": not errors, "errors": errors, "unit_count": len(translations)}, ensure_ascii=False))
        return 0 if not errors else 2
    if errors:
        print(json.dumps({"ok": False, "errors": errors}, ensure_ascii=False))
        return 2
    write_atomic(Path(args.output_markdown), merge_translations(Path(args.markdown_path), packet, translations))
    print(json.dumps({"ok": True, "output_markdown": args.output_markdown}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
