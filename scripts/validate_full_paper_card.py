#!/usr/bin/env python3
"""Run one stage-aware quality pass over an Obsidian paper card."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from normalize_obsidian_figure_links import strip_figure_link_markup
from normalize_obsidian_citations import classify_citation_footnote_labels
from paper_translation_packet import (
    PLACEHOLDER_RE,
    REF_ENTRY_BLOCK_RE,
    REF_TITLE_PLACEHOLDER_RE,
    blockquote,
    canonical_reference_source_section,
    extract_reference_section,
    extract_translation_units,
    is_identity_or_numeric_passthrough,
    read_packet,
    sha256_text,
    split_reference_title_suffix,
)
from validate_obsidian_paper_note import (
    FRONTMATTER_RE,
    HTML_TABLE_RE,
    IMAGE_RE,
    MOJIBAKE_MARKERS,
    validate_text,
)


REMOTE_RE = re.compile(r"^(?:https?:|data:|#)", re.IGNORECASE)
CJK_RE = re.compile(r"[\u4e00-\u9fff]")
LAYOUT_NAME = "english_blockquote_chinese_body"
PAPER_CARD_IMAGE_CLASS = "paper-card-centered-images"


def read_json(path: Path | None) -> dict[str, Any]:
    if path is None or not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def count_mojibake(text: str) -> int:
    return sum(text.count(marker) for marker in MOJIBAKE_MARKERS)


def protected_token_errors(text: str) -> list[str]:
    errors: list[str] = []
    for unit in extract_translation_units(text):
        english = str(unit["english"])
        for category, tokens in unit["protected"].items():
            for token in tokens:
                if token not in english:
                    errors.append(f"{unit['unit_id']} packet extraction lost {category}: {token}")
    return errors


def frontmatter_has_cssclass(text: str, cssclass: str) -> bool:
    match = FRONTMATTER_RE.match(text)
    if not match:
        return False
    body = match.group(1)
    lines = body.splitlines()
    for index, line in enumerate(lines):
        field = re.match(r"^cssclasses\s*:\s*(?P<inline>.*)$", line)
        if not field:
            continue
        inline = field.group("inline").strip()
        if inline:
            values = [item.strip().strip("[]\"'") for item in inline.split(",")]
            return cssclass in values
        for child in lines[index + 1 :]:
            if child and not child.startswith((" ", "\t")):
                break
            item = re.match(r"^\s*-\s+(.+?)\s*$", child)
            if item and item.group(1).strip("\"'") == cssclass:
                return True
        return False
    return False


def expected_quote(unit: dict[str, Any]) -> str:
    english = strip_figure_link_markup(str(unit["english"]))
    if unit.get("kind") == "list_item":
        english = str(unit.get("bullet_prefix", "- ")) + english
    return blockquote(english)


def unit_requires_cjk(unit: dict[str, Any]) -> bool:
    """Return whether a paired translation must contain Chinese characters."""
    english = str(unit.get("english", "")).strip()
    if unit.get("requires_chinese", True) is False or unit.get("kind") == "passthrough":
        return False
    if unit.get("kind") == "reference":
        return False
    if is_identity_or_numeric_passthrough(english):
        return False
    return re.fullmatch(r"\([A-Za-z0-9]+\)", english) is None


def validate_bilingual_layout(text: str, packet: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    units = [unit for unit in (packet.get("units") or []) if unit.get("kind") != "reference_title"]
    cursor = 0
    for index, unit in enumerate(units):
        quote = expected_quote(unit)
        start = text.find(quote, cursor)
        if start < 0:
            errors.append(f"missing or reordered English blockquote for {unit.get('unit_id')}")
            continue
        quote_end = start + len(quote)
        next_quote = expected_quote(units[index + 1]) if index + 1 < len(units) else None
        segment_end = text.find(next_quote, quote_end) if next_quote else len(text)
        if segment_end < 0:
            segment_end = len(text)
        between = text[quote_end:segment_end]
        plain_lines = [
            line.strip()
            for line in between.splitlines()
            if line.strip()
            and not line.lstrip().startswith((">", "#", "!", "|", "```", "$$", "[["))
        ]
        if not plain_lines:
            errors.append(f"missing Chinese body unit after English blockquote: {unit.get('unit_id')}")
        elif unit_requires_cjk(unit) and not any(CJK_RE.search(line) for line in plain_lines):
            errors.append(f"paired body unit contains no Chinese text: {unit.get('unit_id')}")
        cursor = quote_end
    return errors


def validate_full(
    markdown_path: Path,
    vault_root: Path,
    *,
    stage: str,
    workflow_state_path: Path | None = None,
    translation_packet_path: Path | None = None,
    allow_remote_images: bool = False,
    validation_mode: str = "pipeline_finalize",
    stable_resource_roots: tuple[str, ...] | list[str] | None = None,
) -> dict[str, Any]:
    if validation_mode not in {"pipeline_finalize", "posthoc_audit"}:
        raise ValueError(f"unsupported validation mode: {validation_mode}")
    text = markdown_path.read_text(encoding="utf-8")
    base = validate_text(
        markdown_path,
        vault_root,
        text,
        require_figure_links=stage == "final",
        allow_remote_images=allow_remote_images,
        stable_resource_roots=stable_resource_roots,
    )
    errors = list(base["errors"])
    warnings = list(base["warnings"])
    citation_labels = classify_citation_footnote_labels(text)
    remote_images = sum(
        1 for match in IMAGE_RE.finditer(text) if REMOTE_RE.match(match.group("path").strip("<>"))
    )
    if IMAGE_RE.search(text) and not frontmatter_has_cssclass(text, PAPER_CARD_IMAGE_CLASS):
        warnings.append(
            f"paper card images are missing the optional centering cssclass: {PAPER_CARD_IMAGE_CLASS}"
        )
    token_errors = protected_token_errors(text) if stage == "prepared" else []
    units = extract_translation_units(text) if stage == "prepared" else []
    reference_section = extract_reference_section(text)
    reference_blocks = len(re.findall(r"(?m)^\[\d+\].*\^ref-\d+\s*$", reference_section))

    workflow_state = read_json(workflow_state_path)
    packet: dict[str, Any] = {}
    if translation_packet_path and translation_packet_path.is_file():
        packet = read_packet(translation_packet_path)

    if stage == "prepared":
        if not units:
            errors.append("prepared paper contains no translation units")
        errors.extend(error for error in token_errors if error not in errors)
    else:
        frontmatter_match = FRONTMATTER_RE.match(text)
        frontmatter = frontmatter_match.group(1) if frontmatter_match else ""
        if not re.search(
            rf'(?m)^bilingual_layout\s*:\s*["\']?{re.escape(LAYOUT_NAME)}["\']?\s*$',
            frontmatter,
        ):
            errors.append(f"frontmatter bilingual_layout must be {LAYOUT_NAME}")
        if workflow_state and workflow_state.get("bilingual_layout") != LAYOUT_NAME:
            errors.append(f"workflow-state bilingual_layout must be {LAYOUT_NAME}")
        if not packet:
            message = "provenance_unverified: translation packet unavailable; bilingual provenance was not reverified"
            if validation_mode == "pipeline_finalize":
                errors.append("pipeline finalize requires the frozen translation packet")
            else:
                warnings.append(message)
        else:
            errors.extend(validate_bilingual_layout(strip_figure_link_markup(text), packet))
            if packet.get("schema_version") == 3:
                reference_sha256 = sha256_text(reference_section)
                if reference_sha256 != packet.get("reference_section_sha256"):
                    errors.append("final References section no longer matches frozen prepared References")
                if reference_blocks != packet.get("reference_blocks"):
                    errors.append("final References block count no longer matches translation packet")
                if re.search(r"(?m)^>\s+\[\d+\]", reference_section):
                    errors.append("References must remain plain source-language entries, not bilingual blockquotes")
                if PLACEHOLDER_RE.search(reference_section):
                    errors.append("translation placeholders must not appear in References")
                state_reference_sha = workflow_state.get("reference_section_sha256") if workflow_state else None
                if state_reference_sha and state_reference_sha != reference_sha256:
                    errors.append("workflow-state reference_section_sha256 no longer matches final References")
            if int(packet.get("schema_version", 1)) >= 4:
                source_section = canonical_reference_source_section(text)
                source_sha256 = sha256_text(source_section)
                expected_source_sha256 = packet.get("reference_source_section_sha256")
                if source_sha256 != expected_source_sha256:
                    errors.append("final References English source no longer matches frozen prepared References")
                if reference_blocks != packet.get("reference_blocks"):
                    errors.append("final References block count no longer matches translation packet")
                descriptor_numbers = {
                    int(item["reference_number"])
                    for item in packet.get("reference_titles", [])
                    if item.get("status") != "skipped"
                }
                skipped_numbers = {
                    int(item["reference_number"])
                    for item in packet.get("reference_titles", [])
                    if item.get("status") == "skipped"
                }
                rendered_numbers: set[int] = set()
                for raw in reference_section.splitlines():
                    stripped = raw.strip()
                    if stripped.startswith(">") and re.search(r"\[\d+\]", stripped):
                        errors.append("References must remain plain English entries, not blockquotes")
                        continue
                    match = REF_ENTRY_BLOCK_RE.fullmatch(stripped)
                    if not match:
                        continue
                    number = int(match.group("n"))
                    _, chinese_title = split_reference_title_suffix(match.group("body"))
                    if number in skipped_numbers:
                        if chinese_title:
                            errors.append(f"reference {number} was classified as titleless but has a Chinese title suffix")
                        continue
                    if not chinese_title:
                        errors.append(f"reference {number} is missing its inline Chinese title suffix")
                        continue
                    if not CJK_RE.search(chinese_title):
                        errors.append(f"reference {number} Chinese title suffix contains no Chinese text")
                    rendered_numbers.add(number)
                if rendered_numbers != descriptor_numbers:
                    errors.append("reference title suffixes and translation packet descriptors differ")
                if PLACEHOLDER_RE.search(reference_section) or REF_TITLE_PLACEHOLDER_RE.search(reference_section):
                    errors.append("translation placeholders must not appear in References")
                state_source_sha = workflow_state.get("reference_source_section_sha256") if workflow_state else None
                if state_source_sha and state_source_sha != source_sha256:
                    errors.append("workflow-state reference source hash no longer matches final References")
        placeholders = PLACEHOLDER_RE.findall(text)
        if placeholders:
            errors.append("translation placeholders remain: " + ", ".join(placeholders[:3]))

    reference_placeholders = REF_TITLE_PLACEHOLDER_RE.findall(text)
    if stage == "final" and reference_placeholders:
        errors.append("reference-title placeholders remain: " + ", ".join(reference_placeholders[:3]))
    counts = {
        "citation_residual": len(citation_labels) + int(base.get("named_citation_groups", 0)),
        "caption_math_artifact_residual": int(base.get("caption_math_artifact_residual", 0)),
        "remote_images": 0 if allow_remote_images else remote_images,
        "planned_remote_images": remote_images if allow_remote_images else 0,
        "html_tables": len(HTML_TABLE_RE.findall(text)),
        "mojibake": count_mojibake(text),
        "invalid_image_paths": int(base.get("invalid_image_paths", 0)),
        "frontmatter_errors": int(base.get("frontmatter_errors", 0)),
        "translation_units": len(units) if stage == "prepared" else packet.get("body_translation_units", len(packet.get("units") or [])),
        "body_translation_units": len(units) if stage == "prepared" else packet.get("body_translation_units", len(packet.get("units") or [])),
        "reference_title_units": len(packet.get("reference_titles") or []) if packet else reference_blocks,
        "reference_title_fallback_units": packet.get("reference_title_fallback_units", 0) if packet else 0,
        "passthrough_units": packet.get("passthrough_units", 0) if packet else sum(1 for unit in units if unit.get("kind") == "passthrough"),
        "layout_units_total": packet.get("layout_units_total", len(packet.get("units") or [])) if packet else len(units),
        "model_units_total": packet.get("model_units_total", len(packet.get("units") or [])) if packet else sum(1 for unit in units if unit.get("requires_chinese", True)),
        "reference_blocks": reference_blocks,
        "placeholders": len(PLACEHOLDER_RE.findall(text)) + len(reference_placeholders),
        "protected_token_errors": len(token_errors),
        "figure_targets": base.get("figure_targets", 0),
        "figure_mentions": base.get("figure_mentions", 0),
        "figure_links_written": base.get("figure_links_written", 0),
        "unmatched_figure_mentions": base.get("unmatched_figure_mentions", 0),
        "ambiguous_figure_targets": base.get("ambiguous_figure_targets", 0),
        "broken_figure_links": base.get("broken_figure_links", 0),
    }
    ready = stage == "prepared" and not errors and all(
        counts[key] == 0
        for key in (
            "citation_residual",
            *( () if allow_remote_images else ("remote_images",) ),
            "html_tables",
            "mojibake",
            "invalid_image_paths",
            "frontmatter_errors",
            "protected_token_errors",
        )
    ) and counts["translation_units"] > 0
    return {
        "ok": not errors,
        "stage": stage,
        "validation_mode": validation_mode,
        "provenance_verified": bool(packet) if stage == "final" else None,
        "ready_for_translation": ready,
        **counts,
        "line_count": base["line_count"],
        "image_count": base["image_count"],
        "ref_link_count": base["ref_link_count"],
        "errors": errors[:20],
        "warnings": warnings[:20],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", required=True, choices=("prepared", "final"))
    parser.add_argument("--vault-root", required=True)
    parser.add_argument("--markdown-path", required=True)
    parser.add_argument("--workflow-state")
    parser.add_argument("--translation-packet")
    parser.add_argument(
        "--mode",
        choices=("pipeline_finalize", "posthoc_audit"),
        default="posthoc_audit",
        help="Use pipeline_finalize for promotion gates and posthoc_audit for existing notes.",
    )
    parser.add_argument(
        "--stable-resource-root",
        action="append",
        dest="stable_resource_roots",
        help="Additional vault-relative stable asset root. Repeat as needed; defaults allow _resources and _附件.",
    )
    args = parser.parse_args()
    report = validate_full(
        Path(args.markdown_path).resolve(),
        Path(args.vault_root).resolve(),
        stage=args.stage,
        workflow_state_path=Path(args.workflow_state).resolve() if args.workflow_state else None,
        translation_packet_path=Path(args.translation_packet).resolve() if args.translation_packet else None,
        validation_mode=args.mode,
        stable_resource_roots=args.stable_resource_roots,
    )
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
