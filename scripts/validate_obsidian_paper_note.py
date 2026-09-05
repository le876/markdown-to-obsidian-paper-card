#!/usr/bin/env python3
"""Validate a MinerU-derived Obsidian paper note after final post-processing."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from normalize_obsidian_citations import (
    PROTECTED_SPAN_RE,
    classify_citation_footnote_labels,
    count_named_citation_groups,
)
from normalize_obsidian_figure_links import validate_figure_links
from normalize_web_clipping_artifacts import normalize_web_clipping_artifacts


MOJIBAKE_MARKERS = ("鍙", "浜", "琛", "璁", "鏈", "鈥", "銆", "�", "Ã", "Â", "â€")
TEMP_PATH_RE = re.compile(r"\.tmp|unzipped|!\[\[|(?:^|[\\/])images[\\/]|-assets[\\/]", re.IGNORECASE)
DEFAULT_STABLE_RESOURCE_ROOTS = ("_resources", "_附件")
UNSAFE_YAML_WINDOWS_PATH_RE = re.compile(r'(?m)^\w[\w-]*\s*:\s*"[A-Za-z]:\\')
IMAGE_RE = re.compile(r"!\[[^\]]*\]\((?P<path>[^)\r\n]+)\)")
REF_HEADING_RE = re.compile(r"(?im)^#{1,6}\s*(References|Bibliography|参考文献)\s*$")
REF_ENTRY_RE = re.compile(r"(?m)^\[(?P<n>\d+)\]\s+")
REF_BLOCK_RE = re.compile(r"(?m)^\[(?P<n>\d+)\].*?\s\^ref-(?P=n)\s*$")
REF_LINK_BODY = r"\[\[#\^ref-(?P<n>[A-Za-z0-9_-]+)\\?\|[^\]]+\]\]"
REF_LINK_RE = re.compile(REF_LINK_BODY)
CJK_RE = re.compile(r"[\u4e00-\u9fff]")
FRONTMATTER_RE = re.compile(r"(?s)^---\r?\n(.*?)\r?\n---\r?\n")
HTML_TABLE_RE = re.compile(r"(?is)</?\s*(?:table|tr|td|th)\b")
TRAILING_CITATION_CLUSTER_RE = re.compile(
    r"(?P<run>\[\[#\^ref-\d+\\?\|[^\]]+\]\](?:<sup>,</sup>\[\[#\^ref-\d+\\?\|[^\]]+\]\]){2,})[。．.!！？；;，,\s]*$"
)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def split_refs(text: str) -> tuple[str, str]:
    match = REF_HEADING_RE.search(text)
    if not match:
        return text, ""
    return text[: match.start()], text[match.start() :]


def validate_images(
    markdown_path: Path,
    vault_root: Path,
    text: str,
    *,
    allow_remote: bool = False,
    stable_resource_roots: tuple[str, ...] | list[str] | None = None,
) -> list[str]:
    errors: list[str] = []
    try:
        from PIL import Image
    except Exception as exc:  # pragma: no cover - environment dependent
        return [f"Pillow unavailable for image decode validation: {type(exc).__name__}: {exc}"]

    note_dir = markdown_path.parent
    vault_root = vault_root.resolve()
    configured_roots = (*DEFAULT_STABLE_RESOURCE_ROOTS, *(stable_resource_roots or ()))
    resource_roots: list[Path] = []
    for raw_root in configured_roots:
        candidate = Path(raw_root)
        if candidate.is_absolute():
            resolved_root = candidate.resolve()
        else:
            resolved_root = (vault_root / candidate).resolve()
        try:
            resolved_root.relative_to(vault_root)
        except ValueError:
            errors.append(f"configured stable resource root escapes the vault: {raw_root}")
            continue
        if resolved_root == vault_root:
            errors.append(f"configured stable resource root cannot be the vault root itself: {raw_root}")
            continue
        if resolved_root not in resource_roots:
            resource_roots.append(resolved_root)
    for match in IMAGE_RE.finditer(text):
        raw = match.group("path").strip("<>")
        if allow_remote and re.match(r"^(?:https?:|data:|#)", raw, flags=re.IGNORECASE):
            continue
        if re.match(r"^(?:[A-Za-z]:[\\/]|/)", raw):
            errors.append(f"image path is absolute instead of vault-relative Markdown path: {raw}")
            continue
        resolved = (note_dir / raw).resolve()
        if not any(resolved == root or root in resolved.parents for root in resource_roots):
            allowed = ", ".join(str(root.relative_to(vault_root)).replace("\\", "/") for root in resource_roots)
            errors.append(
                f"image path does not resolve inside an allowed stable vault resource root ({allowed}): "
                f"{raw} -> {resolved}"
            )
            continue
        if not resolved.exists():
            errors.append(f"image path does not exist: {raw} -> {resolved}")
            continue
        try:
            with Image.open(resolved) as image:
                image.verify()
        except Exception as exc:
            errors.append(f"image cannot be decoded: {raw}: {type(exc).__name__}: {exc}")
    return errors


def validate_references(text: str) -> list[str]:
    errors: list[str] = []
    named_groups = count_named_citation_groups(text)
    if named_groups:
        errors.append(
            f"body contains {named_groups} unresolved named citation groups; provide the matching BibTeX source"
        )
    citation_footnote_labels = classify_citation_footnote_labels(text)
    if citation_footnote_labels:
        rendered = ", ".join(f"[^{label}]" for label in sorted(citation_footnote_labels))
        errors.append(
            "bibliographic footnotes remain instead of References block links: " + rendered
        )
    before_refs, refs = split_refs(text)
    if not refs:
        return errors

    ref_entries = REF_ENTRY_RE.findall(refs)
    ref_blocks = REF_BLOCK_RE.findall(refs)
    ref_ids = set(re.findall(r"\^ref-([A-Za-z0-9_-]+)", refs))
    link_ids = [match.group("n") for match in REF_LINK_RE.finditer(before_refs)]

    if ref_entries and len(ref_blocks) != len(ref_entries):
        errors.append(f"reference entries and ^ref-n block ids differ: entries={len(ref_entries)} blocks={len(ref_blocks)}")

    for link_id in sorted(set(link_ids) - ref_ids):
        errors.append(f"citation link points to missing reference block: ^ref-{link_id}")

    non_ref_text = before_refs
    appendix_match = re.search(r"(?im)^#{1,6}\s*(Appendix|Appendices)\b.*$", refs)
    if appendix_match:
        non_ref_text += "\n" + refs[appendix_match.start() :]
    non_ref_scan = PROTECTED_SPAN_RE.sub(
        lambda match: re.sub(r"[^\r\n]", " ", match.group(0)),
        non_ref_text,
    )

    footnote_style_reference_links = sorted(
        {
            label
            for label in re.findall(r"\[\^(\d+)\]", non_ref_scan)
            if label in ref_ids
        },
        key=int,
    )
    if footnote_style_reference_links:
        errors.append(
            "body uses footnote syntax for existing References blocks: "
            + ", ".join(f"[^{label}]" for label in footnote_style_reference_links)
        )

    for match in re.finditer(r"(?<!#\^ref-)(?<!!)\[(?P<inner>\d{1,3}(?:\s*,\s*\d{1,3})*)\](?!\w|\()", non_ref_scan):
        numbers = [part.strip() for part in match.group("inner").split(",")]
        if all(number in ref_ids for number in numbers):
            errors.append(f"body contains raw citation marker instead of block link: [{match.group('inner')}]")

    if re.search(r"\]\]\s*,\s*\[\[#\^ref-", before_refs):
        errors.append("body citation links use baseline comma instead of <sup>,</sup>")

    residual_author_year = re.findall(
        r"\([^()\n]*(?:et\s+al\.|&)[^()\n]*(?:19|20)\d{2}[a-z]?[^()\n]*\)",
        non_ref_scan,
        flags=re.IGNORECASE,
    )
    if residual_author_year:
        sample = residual_author_year[0]
        errors.append(f"body contains author-year citation that was not converted to numeric superscript block link: {sample}")

    if re.search(r"(?m)^\[\d+\]\s+.*\^ref-\d+[^\S\r\n]*\r?\n(?=^\[\d+\]\s+)", refs):
        errors.append("References entries with ^ref-n block ids are not separated by blank lines")

    return errors


def validate_citation_placement(text: str) -> list[str]:
    warnings: list[str] = []
    lines = text.splitlines()
    previous_content_line = ""

    for line_number, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped:
            continue

        if CJK_RE.search(stripped):
            trailing = TRAILING_CITATION_CLUSTER_RE.search(stripped)
            current_links = list(REF_LINK_RE.finditer(stripped))
            previous_links = list(REF_LINK_RE.finditer(previous_content_line))
            if trailing and len(current_links) >= 3:
                has_non_trailing_citation = bool(REF_LINK_RE.search(stripped[: trailing.start()]))
                trailing_starts_late = trailing.start() / max(len(stripped), 1) > 0.55
                many_citations_stacked_at_end = len(current_links) >= 5 and not has_non_trailing_citation
                paired_english_has_distributed_citations = False
                if previous_links:
                    first_previous_ratio = previous_links[0].start() / max(len(previous_content_line), 1)
                    paired_english_has_distributed_citations = first_previous_ratio < 0.85
                if many_citations_stacked_at_end or (
                    paired_english_has_distributed_citations and not has_non_trailing_citation and trailing_starts_late
                ):
                    warnings.append(
                        "possible translated citation placement drift at line "
                        f"{line_number}: Chinese paragraph has a trailing citation cluster; "
                        "move citations near the corresponding claims when appropriate"
                    )

        if not stripped.startswith("#"):
            previous_content_line = stripped

    return warnings


def validate_heading_levels(text: str) -> list[str]:
    errors: list[str] = []
    h1s = re.findall(r"(?m)^#\s+(.+?)\s*$", text)
    if len(h1s) != 1:
        errors.append(f"paper note must have exactly one H1 title, found {len(h1s)}")

    for level, title in re.findall(r"(?m)^(#{1,6})\s+(.+?)\s*$", text):
        normalized = title.strip().lower()
        if normalized == "abstract" and len(level) != 2:
            errors.append(f"Abstract heading must be H2, found H{len(level)}")
        elif normalized in {"references", "bibliography", "appendix", "appendices", "acknowledgements", "acknowledgments"} and len(level) == 1:
            errors.append(f"section heading should not be H1: {title}")
    return errors


def validate_frontmatter(markdown_path: Path, text: str) -> list[str]:
    errors: list[str] = []
    raw = markdown_path.read_bytes()
    if not raw.startswith(b"---"):
        errors.append("frontmatter must begin at byte 0 with ---")
    match = FRONTMATTER_RE.match(text)
    if not match:
        errors.append("frontmatter block is missing or not at file start")
        return errors
    body = match.group(1)
    if not re.search(r"(?m)^title\s*:", body):
        errors.append("frontmatter missing title")
    if not re.search(r"(?m)^aliases\s*:", body):
        errors.append("frontmatter missing aliases")
    else:
        aliases_match = re.search(r"(?m)^aliases\s*:\s*(?P<inline>[^\r\n]*)", body)
        inline_aliases = aliases_match.group("inline").strip() if aliases_match else ""
        has_list_alias = bool(re.search(r"(?m)^aliases\s*:\s*\r?\n(?:[ \t].*\r?\n)*?[ \t]+-\s+\S", body))
        if inline_aliases in {"", "[]", "null", "~"} and not has_list_alias:
            errors.append("frontmatter aliases is empty")
    if UNSAFE_YAML_WINDOWS_PATH_RE.search(body):
        errors.append('frontmatter contains a double-quoted Windows path; use C:/... or single quotes to avoid YAML backslash escapes')
    return errors


def validate_translation_workflow_record(text: str) -> list[str]:
    warnings: list[str] = []
    match = FRONTMATTER_RE.match(text)
    frontmatter = match.group(1) if match else ""
    translation_workflow_match = re.search(r"(?im)^translation_workflow\s*:\s*(?P<value>.+?)\s*$", frontmatter)

    source_quote_lines = len(re.findall(r"(?m)^>\s+\S", text))
    chinese_body_lines = 0
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(">") or stripped.startswith("#"):
            continue
        if CJK_RE.search(stripped):
            chinese_body_lines += 1

    likely_bilingual_note = source_quote_lines >= 10 and chinese_body_lines >= 10
    if not likely_bilingual_note:
        return warnings

    if not translation_workflow_match:
        warnings.append(
            "bilingual paper note lacks frontmatter translation_workflow; record whether translation used controlled worker/subagent or why it did not"
        )
        return warnings

    value = translation_workflow_match.group("value").strip().strip("'\"").lower()
    records_worker = "worker" in value or "subagent" in value
    records_main_thread = "main-thread" in value or "main thread" in value or "main-agent" in value or "main agent" in value
    records_allowed_fallback = "user requested main" in value or "no subagent" in value or "unavailable" in value or "harness" in value
    if records_main_thread and not records_worker and not records_allowed_fallback:
        warnings.append(
            "translation_workflow records main-agent translation; current skill defaults to controlled worker/subagent unless the user explicitly requested main-agent-only translation or no subagent tool was available"
        )

    return warnings


def validate_text(
    markdown_path: Path,
    vault_root: Path,
    text: str,
    *,
    require_figure_links: bool = True,
    allow_remote_images: bool = False,
    stable_resource_roots: tuple[str, ...] | list[str] | None = None,
) -> dict[str, object]:
    errors: list[str] = []
    warnings: list[str] = []

    frontmatter_findings = validate_frontmatter(markdown_path, text)
    frontmatter_warnings = [
        finding for finding in frontmatter_findings if finding == "frontmatter aliases is empty"
    ]
    frontmatter_errors = [
        finding for finding in frontmatter_findings if finding not in frontmatter_warnings
    ]
    heading_findings = validate_heading_levels(text)
    heading_warnings = [
        finding for finding in heading_findings if finding.startswith("Abstract heading must be H2")
    ]
    heading_errors = [finding for finding in heading_findings if finding not in heading_warnings]
    image_errors = validate_images(
        markdown_path,
        vault_root,
        text,
        allow_remote=allow_remote_images,
        stable_resource_roots=stable_resource_roots,
    )
    errors.extend(frontmatter_errors)
    errors.extend(heading_errors)
    errors.extend(image_errors)
    errors.extend(validate_references(text))
    repaired_caption_text, caption_artifact_report = normalize_web_clipping_artifacts(text)
    if repaired_caption_text != text:
        warnings.append(
            "figure captions contain fixable rendered-text/TeX fallback artifacts"
        )
    if caption_artifact_report["residual_caption_math_artifacts"]:
        warnings.append(
            "figure captions contain residual non-body TeX display artifacts after normalization: "
            + str(caption_artifact_report["residual_caption_math_artifacts"])
        )
    warnings.extend(frontmatter_warnings)
    warnings.extend(heading_warnings)
    warnings.extend(validate_citation_placement(text))
    warnings.extend(validate_translation_workflow_record(text))
    figure_validation = validate_figure_links(text, markdown_path, vault_root)
    if require_figure_links:
        errors.extend(figure_validation["errors"])
    warnings.extend(figure_validation["warnings"])

    if TEMP_PATH_RE.search(text):
        errors.append("note contains temporary, wikilink image, or unstable resource path")
    if HTML_TABLE_RE.search(text):
        errors.append("note contains HTML table tags; convert final tables to Obsidian-stable Markdown tables so formulas render")
    for marker in MOJIBAKE_MARKERS:
        if marker in text:
            errors.append(f"mojibake marker remains: {marker}")
    if re.search(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", text):
        errors.append("control character remains in Markdown")
    if len(re.findall(r"(?m)^\s*\$\$\s*$", text)) % 2:
        errors.append("display math $$ delimiters are unbalanced")
    if len(re.findall(r"(?<!\$)\$(?!\$)", text)) % 2:
        errors.append("inline math $ delimiters are unbalanced")
    if len(re.findall(r"(?m)^```", text)) % 2:
        errors.append("code fences are unbalanced")
    text_without_code = re.sub(r"(?ms)^```.*?^```", "", text)
    if re.search(r"\\\(|\\\)", text_without_code):
        warnings.append("note contains \\( or \\) delimiters; Obsidian notes should usually use $...$")

    return {
        "ok": not errors,
        "markdown_path": str(markdown_path),
        "line_count": text.count("\n") + 1,
        "image_count": len(IMAGE_RE.findall(text)),
        "invalid_image_paths": len(image_errors),
        "frontmatter_errors": len(frontmatter_errors),
        "h1_count": len(re.findall(r"(?m)^#\s+", text)),
        "ref_link_count": len(REF_LINK_RE.findall(text)),
        "named_citation_groups": count_named_citation_groups(text),
        "caption_math_artifacts_fixable": caption_artifact_report["caption_math_artifacts_fixed"],
        "residual_caption_math_artifacts": caption_artifact_report["residual_caption_math_artifacts"],
        "caption_math_artifact_residual": caption_artifact_report["residual_caption_math_artifacts"],
        "figure_targets": figure_validation["figure_targets"],
        "figure_mentions": figure_validation["figure_mentions"],
        "figure_links_written": figure_validation["figure_links_written"],
        "unmatched_figure_mentions": figure_validation["unmatched_figure_mentions"],
        "ambiguous_figure_targets": figure_validation["ambiguous_figure_targets"],
        "broken_figure_links": figure_validation["broken_figure_links"],
        "figure_link_details": figure_validation["details"],
        "errors": errors,
        "warnings": warnings,
    }


def validate(
    markdown_path: Path,
    vault_root: Path,
    *,
    stable_resource_roots: tuple[str, ...] | list[str] | None = None,
) -> dict[str, object]:
    return validate_text(
        markdown_path,
        vault_root,
        read_text(markdown_path),
        stable_resource_roots=stable_resource_roots,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault-root", required=True)
    parser.add_argument("--markdown-path", required=True)
    parser.add_argument(
        "--stable-resource-root",
        action="append",
        dest="stable_resource_roots",
        help="Additional vault-relative stable asset root. Repeat as needed; defaults allow _resources and _附件.",
    )
    args = parser.parse_args()

    report = validate(
        Path(args.markdown_path).resolve(),
        Path(args.vault_root).resolve(),
        stable_resource_roots=args.stable_resource_roots,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
