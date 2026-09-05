#!/usr/bin/env python3
"""Safely synchronize paper-card image alignment with Obsidian Image Converter.

Image Converter interoperability: Copyright (c) 2023 xRyul (MIT).
See THIRD_PARTY_NOTICES.md for the retained notice and MurmurHash3 attribution.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote

from safe_atomic_io import atomic_write_json


MASK32 = 0xFFFFFFFF
IMAGE_RE = re.compile(r"!\[(?P<alt>[^\]]*)\]\((?P<path>[^)\r\n]+)\)")
IMAGE_CONVERTER_ID = "image-converter"
DEFAULT_CACHE_NAME = "image-converter-image-alignments.json"
LAYOUT_TYPES = {"text", "image", "table", "equation", "interline_equation"}


@dataclass(frozen=True)
class LayoutItem:
    page: int
    kind: str
    bbox: tuple[float, float, float, float]
    image_path: str = ""
    text: str = ""


def _u32(value: int) -> int:
    return value & MASK32


def _imul(left: int, right: int) -> int:
    return _u32((left & MASK32) * (right & MASK32))


def _rotl32(value: int, bits: int) -> int:
    value &= MASK32
    return _u32((value << bits) | (value >> (32 - bits)))


def _fmix32(value: int) -> int:
    value ^= value >> 16
    value = _imul(value, 2246822507)
    value ^= value >> 13
    value = _imul(value, 3266489909)
    value ^= value >> 16
    return _u32(value)


def image_converter_hash(value: str, seed: int = 0) -> str:
    """Match Image Converter 1.4.4's MurmurHash3 x64 128 JavaScript function."""
    data = bytes(ord(char) & 0xFF for char in value)
    h1 = h2 = h3 = h4 = seed & MASK32
    c1 = 2277735313
    c2 = 1291169091
    block_count = len(data) >> 4

    for block in range(block_count):
        offset = block * 16
        k1 = int.from_bytes(data[offset : offset + 4], "little")
        k2 = int.from_bytes(data[offset + 4 : offset + 8], "little")
        k3 = int.from_bytes(data[offset + 8 : offset + 12], "little")
        k4 = int.from_bytes(data[offset + 12 : offset + 16], "little")

        k1 = _imul(_rotl32(_imul(k1, c1), 15), c2)
        h1 ^= k1
        h1 = _u32(_imul(_rotl32(h1, 19), 5) + 3864292196)

        k2 = _imul(_rotl32(_imul(k2, c1), 15), c2)
        h2 ^= k2
        h2 = _u32(_imul(_rotl32(h2, 17), 5) + 3864292196)

        k3 = _imul(_rotl32(_imul(k3, c1), 15), c2)
        h3 ^= k3
        h3 = _u32(_imul(_rotl32(h3, 15), 5) + 3864292196)

        k4 = _imul(_rotl32(_imul(k4, c1), 15), c2)
        h4 ^= k4
        h4 = _u32(_imul(_rotl32(h4, 13), 5) + 3864292196)

    tail = data[block_count * 16 :]
    k1 = k2 = k3 = k4 = 0
    if len(tail) >= 15:
        k4 ^= tail[14] << 16
    if len(tail) >= 14:
        k4 ^= tail[13] << 8
    if len(tail) >= 13:
        k4 ^= tail[12]
        k4 = _imul(_rotl32(_imul(k4, c1), 15), c2)
        h4 ^= k4
    if len(tail) >= 12:
        k3 ^= tail[11] << 24
    if len(tail) >= 11:
        k3 ^= tail[10] << 16
    if len(tail) >= 10:
        k3 ^= tail[9] << 8
    if len(tail) >= 9:
        k3 ^= tail[8]
        k3 = _imul(_rotl32(_imul(k3, c1), 15), c2)
        h3 ^= k3
    if len(tail) >= 8:
        k2 ^= tail[7] << 24
    if len(tail) >= 7:
        k2 ^= tail[6] << 16
    if len(tail) >= 6:
        k2 ^= tail[5] << 8
    if len(tail) >= 5:
        k2 ^= tail[4]
        k2 = _imul(_rotl32(_imul(k2, c1), 15), c2)
        h2 ^= k2
    if len(tail) >= 4:
        k1 ^= tail[3] << 24
    if len(tail) >= 3:
        k1 ^= tail[2] << 16
    if len(tail) >= 2:
        k1 ^= tail[1] << 8
    if len(tail) >= 1:
        k1 ^= tail[0]
        k1 = _imul(_rotl32(_imul(k1, c1), 15), c2)
        h1 ^= k1

    length = len(data)
    h1 ^= length
    h2 ^= length
    h3 ^= length
    h4 ^= length
    h1 = _u32(h1 + h2)
    h1 = _u32(h1 + h3)
    h1 = _u32(h1 + h4)
    h2 = _u32(h2 + h1)
    h2 = _u32(h2 + h3)
    h2 = _u32(h2 + h4)
    h3 = _u32(h3 + h1)
    h3 = _u32(h3 + h2)
    h3 = _u32(h3 + h4)
    h4 = _u32(h4 + h1)
    h4 = _u32(h4 + h2)
    h4 = _u32(h4 + h3)
    h1, h2, h3, h4 = map(_fmix32, (h1, h2, h3, h4))
    return f"{h4:08x}{h3:08x}{h2:08x}{h1:08x}"


def image_converter_runtime_path(vault_path: str) -> str:
    """Mirror Image Converter 1.4.4's Windows app:// path normalization."""
    normalized = vault_path.replace("\\", "/").lstrip("/")
    return f"/{normalized}" if os.name == "nt" else normalized


def read_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    value = json.loads(path.read_text(encoding="utf-8"))
    return value


def vault_relative(path: Path, vault_root: Path) -> str:
    root = vault_root.resolve()
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"path is outside vault: {path}") from exc
    return relative.as_posix()


def clean_markdown_target(raw: str) -> str:
    target = raw.strip()
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1]
    target = target.split("?", 1)[0].split("#", 1)[0]
    return unquote(target).replace("\\", "/")


def resolve_image_target(raw: str, markdown_path: Path, vault_root: Path) -> tuple[Path, str]:
    target = clean_markdown_target(raw)
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", target) or target.startswith("//"):
        raise ValueError(f"non-local image target: {raw}")
    candidate = Path(target)
    if candidate.is_absolute():
        resolved = candidate.resolve()
    else:
        resolved = (markdown_path.parent / candidate).resolve()
    return resolved, vault_relative(resolved, vault_root)


def iter_markdown_images(text: str, markdown_path: Path, vault_root: Path) -> Iterable[dict[str, str]]:
    seen: set[str] = set()
    for match in IMAGE_RE.finditer(text):
        raw = match.group("path")
        try:
            absolute, relative = resolve_image_target(raw, markdown_path, vault_root)
        except ValueError:
            continue
        if relative in seen:
            continue
        seen.add(relative)
        yield {
            "markdown_target": raw,
            "vault_path": relative,
            "absolute_path": str(absolute),
            "filename": absolute.name,
        }


def _coerce_bbox(value: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(value, list) or len(value) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(part) for part in value)
    except (TypeError, ValueError):
        return None
    if x1 <= x0 or y1 <= y0:
        return None
    return x0, y0, x1, y1


def load_layout_items(layout_dir: Path | None) -> list[LayoutItem]:
    if layout_dir is None or not layout_dir.is_dir():
        return []
    files = sorted(
        path
        for path in layout_dir.rglob("*_content_list.json")
        if not path.name.endswith("_content_list_v2.json")
    )
    items: list[LayoutItem] = []
    for path in files:
        try:
            payload = read_json(path, [])
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            payload = payload.get("items") or payload.get("content_list") or []
        if not isinstance(payload, list):
            continue
        for raw in payload:
            if not isinstance(raw, dict):
                continue
            kind = str(raw.get("type") or "")
            bbox = _coerce_bbox(raw.get("bbox"))
            if kind not in LAYOUT_TYPES or bbox is None:
                continue
            try:
                page = int(raw.get("page_idx", raw.get("page", 0)))
            except (TypeError, ValueError):
                page = 0
            items.append(
                LayoutItem(
                    page=page,
                    kind=kind,
                    bbox=bbox,
                    image_path=str(raw.get("img_path") or raw.get("image_path") or ""),
                    text=str(raw.get("text") or ""),
                )
            )
    return items


def _filename_matches(final_name: str, layout_name: str) -> bool:
    final = Path(final_name).name.lower()
    source = Path(layout_name).name.lower()
    if not source:
        return False
    if final == source:
        return True
    final_stem = Path(final).stem
    source_stem = Path(source).stem
    return len(source_stem) >= 12 and (
        final_stem.endswith("-" + source_stem) or source_stem in final_stem
    )


def _vertical_overlap(first: LayoutItem, second: LayoutItem) -> float:
    return max(0.0, min(first.bbox[3], second.bbox[3]) - max(first.bbox[1], second.bbox[1]))


def infer_float_candidate(image: LayoutItem, page_items: list[LayoutItem]) -> dict[str, Any]:
    text_items = [item for item in page_items if item.kind == "text" and item.text.strip()]
    bounds_items = page_items
    if not bounds_items:
        return {"position": "center", "wrap": False, "reason": "no_page_bounds", "confidence": 0.0}
    content_x0 = min(item.bbox[0] for item in bounds_items)
    content_x1 = max(item.bbox[2] for item in bounds_items)
    content_width = max(1.0, content_x1 - content_x0)
    image_width = image.bbox[2] - image.bbox[0]
    image_height = image.bbox[3] - image.bbox[1]
    width_fraction = image_width / content_width
    if width_fraction > 0.56:
        return {
            "position": "center",
            "wrap": False,
            "reason": "image_is_wide",
            "confidence": 1.0,
            "width_fraction": round(width_fraction, 3),
        }

    tolerance = content_width * 0.035
    left_score = right_score = 0.0
    left_blocks = right_blocks = 0
    for text in text_items:
        overlap = _vertical_overlap(image, text)
        if overlap < max(8.0, min(image_height, text.bbox[3] - text.bbox[1]) * 0.18):
            continue
        text_width = text.bbox[2] - text.bbox[0]
        if text_width < content_width * 0.22:
            continue
        if text.bbox[2] <= image.bbox[0] + tolerance:
            left_score += overlap * min(1.0, text_width / content_width)
            left_blocks += 1
        if text.bbox[0] >= image.bbox[2] - tolerance:
            right_score += overlap * min(1.0, text_width / content_width)
            right_blocks += 1

    image_center = (image.bbox[0] + image.bbox[2]) / 2
    content_center = (content_x0 + content_x1) / 2
    minimum_score = max(8.0, image_height * 0.12)
    if image_center > content_center and left_score >= minimum_score:
        return {
            "position": "right",
            "wrap": True,
            "reason": "overlapping_text_on_left",
            "confidence": round(min(1.0, left_score / max(image_height * 0.45, 1.0)), 3),
            "width_fraction": round(width_fraction, 3),
            "overlap_score": round(left_score, 2),
            "text_blocks": left_blocks,
        }
    if image_center < content_center and right_score >= minimum_score:
        return {
            "position": "left",
            "wrap": True,
            "reason": "overlapping_text_on_right",
            "confidence": round(min(1.0, right_score / max(image_height * 0.45, 1.0)), 3),
            "width_fraction": round(width_fraction, 3),
            "overlap_score": round(right_score, 2),
            "text_blocks": right_blocks,
        }
    return {
        "position": "center",
        "wrap": False,
        "reason": "no_opposite_side_overlap",
        "confidence": 0.8,
        "width_fraction": round(width_fraction, 3),
    }


def layout_recommendations(
    images: list[dict[str, str]],
    layout_items: list[LayoutItem],
) -> dict[str, dict[str, Any]]:
    by_page: dict[int, list[LayoutItem]] = defaultdict(list)
    layout_images: list[LayoutItem] = []
    for item in layout_items:
        by_page[item.page].append(item)
        if item.kind == "image" and item.image_path:
            layout_images.append(item)
    recommendations: dict[str, dict[str, Any]] = {}
    for image in images:
        matches = [
            item for item in layout_images if _filename_matches(image["filename"], item.image_path)
        ]
        if len(matches) != 1:
            recommendations[image["vault_path"]] = {
                "position": "center",
                "wrap": False,
                "reason": "layout_image_unmatched" if not matches else "layout_image_ambiguous",
                "confidence": 0.0,
                "layout_matches": len(matches),
            }
            continue
        match = matches[0]
        result = infer_float_candidate(match, by_page[match.page])
        result.update(
            {
                "page": match.page,
                "layout_image": match.image_path.replace("\\", "/"),
                "bbox": list(match.bbox),
            }
        )
        recommendations[image["vault_path"]] = result
    return recommendations


def plugin_enabled(vault_root: Path) -> bool:
    enabled_path = vault_root / ".obsidian" / "community-plugins.json"
    try:
        enabled = read_json(enabled_path, [])
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(enabled, list) and IMAGE_CONVERTER_ID in enabled


def synchronize(
    *,
    vault_root: Path,
    markdown_path: Path,
    layout_dir: Path | None = None,
    write: bool = False,
    overwrite_existing: bool = False,
    cache_path: Path | None = None,
) -> dict[str, Any]:
    vault_root = vault_root.resolve()
    markdown_path = markdown_path.resolve()
    note_path = vault_relative(markdown_path, vault_root)
    text = markdown_path.read_text(encoding="utf-8")
    images = list(iter_markdown_images(text, markdown_path, vault_root))
    layout_items = load_layout_items(layout_dir.resolve() if layout_dir else None)
    recommendations = layout_recommendations(images, layout_items)
    cache_path = (cache_path or vault_root / ".obsidian" / DEFAULT_CACHE_NAME).resolve()
    enabled = plugin_enabled(vault_root)

    report: dict[str, Any] = {
        "ok": True,
        "write": write,
        "plugin_enabled": enabled,
        "note_path": note_path,
        "cache_path": str(cache_path),
        "layout_dir": str(layout_dir.resolve()) if layout_dir else None,
        "images": len(images),
        "layout_items": len(layout_items),
        "float_candidates": 0,
        "entries_added": 0,
        "entries_overwritten": 0,
        "entries_preserved": 0,
        "changed": False,
        "reload_required": False,
        "backup_path": None,
        "items": [],
        "warnings": [],
    }
    if not enabled:
        report["ok"] = False
        report["warnings"].append("Image Converter is not enabled in this vault")
        return report

    try:
        cache = read_json(cache_path, {})
    except (OSError, json.JSONDecodeError) as exc:
        report["ok"] = False
        report["warnings"].append(f"alignment cache is invalid JSON: {exc}")
        return report
    if not isinstance(cache, dict):
        report["ok"] = False
        report["warnings"].append("alignment cache root must be an object")
        return report
    original_bytes = cache_path.read_bytes() if cache_path.is_file() else b""
    note_cache = cache.setdefault(note_path, {})
    if not isinstance(note_cache, dict):
        report["ok"] = False
        report["warnings"].append("alignment cache note entry must be an object")
        return report

    for image in images:
        image_path = image["vault_path"]
        runtime_image_path = image_converter_runtime_path(image_path)
        cache_key = image_converter_hash(f"{note_path}:{runtime_image_path}", 0)
        recommendation = recommendations[image_path]
        if recommendation.get("wrap"):
            report["float_candidates"] += 1
        desired = {
            "position": "center",
            "width": "",
            "height": "",
            "wrap": False,
        }
        existing = note_cache.get(cache_key)
        item_report = {
            "image": image_path,
            "plugin_runtime_path": runtime_image_path,
            "cache_key": cache_key,
            "applied": {"position": "center", "wrap": False},
            "layout_recommendation": recommendation,
        }
        if isinstance(existing, dict) and not overwrite_existing:
            report["entries_preserved"] += 1
            item_report["status"] = "preserved_existing"
            item_report["existing"] = existing
        else:
            if isinstance(existing, dict):
                desired["width"] = str(existing.get("width") or "")
                desired["height"] = str(existing.get("height") or "")
                report["entries_overwritten"] += 1
                item_report["status"] = "would_overwrite" if not write else "overwritten"
            else:
                report["entries_added"] += 1
                item_report["status"] = "would_add" if not write else "added"
            note_cache[cache_key] = desired
        report["items"].append(item_report)

    report["changed"] = bool(report["entries_added"] or report["entries_overwritten"])
    if not write or not report["changed"]:
        return report

    current_bytes = cache_path.read_bytes() if cache_path.is_file() else b""
    if hashlib.sha256(current_bytes).digest() != hashlib.sha256(original_bytes).digest():
        report["ok"] = False
        report["warnings"].append("alignment cache changed during synchronization; refusing to overwrite")
        return report
    if cache_path.is_file():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        backup_path = cache_path.with_name(
            cache_path.name + f".bak-{stamp}-before-paper-card-layout"
        )
        shutil.copy2(cache_path, backup_path)
        report["backup_path"] = str(backup_path)
    atomic_write_json(cache_path, cache)
    report["reload_required"] = True
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault-root", required=True)
    parser.add_argument("--markdown-path", required=True)
    parser.add_argument("--layout-dir")
    parser.add_argument("--cache-path")
    parser.add_argument("--overwrite-existing", action="store_true")
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    try:
        report = synchronize(
            vault_root=Path(args.vault_root),
            markdown_path=Path(args.markdown_path),
            layout_dir=Path(args.layout_dir) if args.layout_dir else None,
            cache_path=Path(args.cache_path) if args.cache_path else None,
            overwrite_existing=args.overwrite_existing,
            write=args.write,
        )
    except Exception as exc:
        report = {"ok": False, "error": str(exc)}
    json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
