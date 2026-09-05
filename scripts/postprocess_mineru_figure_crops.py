from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path

from safe_atomic_io import atomic_write_bytes, atomic_write_json, atomic_write_text
from typing import Any

try:
    import pymupdf as fitz
    from PIL import Image, ImageChops
except ImportError:
    fitz = None
    Image = None
    ImageChops = None


IMAGE_GROUP_RE = re.compile(
    r"(?P<group>(?:(?:[ \t]*!\[[^\]]*\]\((?P<link>[^)\r\n]+)\)[ \t]*(?:\r?\n)(?:[ \t]*\r?\n)*){2,}))"
    r"(?=(?:>[ \t]*)?(?:Fig\.|Figure)[ \t]*(?P<fig>\d+)[ \t]*[:.])",
    re.IGNORECASE,
)
IMAGE_LINK_RE = re.compile(r"!\[[^\]]*\]\((?P<link>[^)\r\n]+)\)")
CROP_CACHE_SCHEMA_VERSION = 1
CROP_ALGORITHM_VERSION = 1
TRIM_PADDING = 10
TRIM_THRESHOLD = 10


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fingerprint_json(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def layout_sources_fingerprint(layout_dir: Path) -> str:
    root = layout_dir.resolve()
    records: list[dict[str, str]] = []
    for source in find_layout_sources(layout_dir):
        resolved = source.resolve()
        try:
            relative = resolved.relative_to(root).as_posix()
        except ValueError:
            relative = source.name
        records.append({"path": relative, "sha256": sha256_file(resolved)})
    return fingerprint_json(records)


def load_crop_cache(path: Path) -> dict[str, Any]:
    try:
        cache = load_json(path)
    except (OSError, ValueError, TypeError):
        cache = None
    if not isinstance(cache, dict) or cache.get("schema_version") != CROP_CACHE_SCHEMA_VERSION:
        return {"schema_version": CROP_CACHE_SCHEMA_VERSION, "entries": {}}
    entries = cache.get("entries")
    if not isinstance(entries, dict):
        return {"schema_version": CROP_CACHE_SCHEMA_VERSION, "entries": {}}
    return {"schema_version": CROP_CACHE_SCHEMA_VERSION, "entries": entries}


def find_origin_pdf(extract_dir: Path) -> Path | None:
    preferred = sorted(extract_dir.rglob("*_origin.pdf"))
    if preferred:
        return preferred[0]
    candidates = sorted(extract_dir.rglob("*.pdf"))
    return candidates[0] if candidates else None


def iter_content_items(value: Any, page_idx: int | None = None):
    if isinstance(value, list):
        # content_list_v2.json is often a list of page-level lists.
        if value and all(isinstance(item, dict) for item in value):
            for item in value:
                yield from iter_content_items(item, page_idx)
        else:
            for idx, item in enumerate(value):
                yield from iter_content_items(item, idx if isinstance(item, list) else page_idx)
    elif isinstance(value, dict):
        if "type" in value:
            item = dict(value)
            if "page_idx" not in item and page_idx is not None:
                item["page_idx"] = page_idx
            yield item
        for child_key in ("content", "children", "para_blocks", "blocks", "lines", "spans"):
            if child_key in value:
                yield from iter_content_items(value[child_key], page_idx)


def find_layout_sources(extract_dir: Path) -> list[Path]:
    result: list[Path] = []
    for pattern in ("*_content_list.json", "content_list_v2.json", "*_model.json", "layout.json"):
        result.extend(sorted(extract_dir.rglob(pattern)))
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in result:
        resolved = path.resolve()
        if resolved not in seen:
            unique.append(path)
            seen.add(resolved)
    return unique


def build_image_bbox_index(extract_dir: Path) -> dict[str, list[dict[str, Any]]]:
    index: dict[str, list[dict[str, Any]]] = {}
    for source in find_layout_sources(extract_dir):
        try:
            data = load_json(source)
        except Exception:
            continue
        for item in iter_content_items(data):
            if item.get("type") != "image":
                continue
            bbox = item.get("bbox")
            page_idx = item.get("page_idx")
            image_path = item.get("img_path") or item.get("image_path")
            if not image_path:
                image_source = item.get("content", {}).get("image_source", {}) if isinstance(item.get("content"), dict) else {}
                image_path = image_source.get("path")
            if not image_path or bbox is None or page_idx is None:
                continue
            basename = Path(str(image_path)).name
            index.setdefault(basename, []).append(
                {
                    "bbox": [float(v) for v in bbox],
                    "page_idx": int(page_idx),
                    "source": str(source),
                    "normalized_canvas": "content_list" in source.name,
                }
            )
    return index


def strip_wrappers(link: str) -> str:
    value = link.strip()
    if value.startswith("<") and value.endswith(">"):
        value = value[1:-1].strip()
    return value


def original_image_name(link: str, asset_prefix: str, asset_map: dict[str, str]) -> str | None:
    name = Path(strip_wrappers(link).replace("\\", "/")).name
    if "-pdf-crop." in name:
        return None
    if name in asset_map:
        return asset_map[name]
    prefix = f"{asset_prefix}-"
    if not name.lower().startswith(prefix.lower()):
        return None
    return name[len(prefix) :]


def relative_markdown_path(markdown_path: Path, target_path: Path) -> str:
    import os

    rel_text = os.path.relpath(target_path.resolve(), markdown_path.parent.resolve())
    return rel_text.replace("\\", "/")


def trim_white_margin(path: Path, padding: int = TRIM_PADDING, threshold: int = TRIM_THRESHOLD) -> None:
    with Image.open(path) as opened:
        image = opened.convert("RGB")
    white = Image.new("RGB", image.size, (255, 255, 255))
    diff = ImageChops.difference(image, white)
    mask = Image.eval(diff.convert("L"), lambda px: 255 if px > threshold else 0)
    bbox = mask.getbbox()
    if bbox is None:
        image.save(path)
        return
    left = max(0, bbox[0] - padding)
    top = max(0, bbox[1] - padding)
    right = min(image.width, bbox[2] + padding)
    bottom = min(image.height, bbox[3] + padding)
    image.crop((left, top, right, bottom)).save(path)


def crop_from_bbox(
    pdf: fitz.Document,
    page_idx: int,
    bboxes: list[list[float]],
    out_path: Path,
    render_scale: float,
    padding_points: float,
    normalized_canvas: bool | None = None,
) -> None:
    page = pdf[page_idx]
    page_width = float(page.rect.width)
    page_height = float(page.rect.height)

    # MinerU content-list bboxes are normalized to a 1000 x 1000 page canvas,
    # while layout/model bboxes may already be PDF points. Prefer source metadata
    # from the JSON index, and only fall back to magnitude when metadata is absent.
    # When normalized, scale x and y together; scaling only one axis shifts crops.
    max_x = max(box[2] for box in bboxes)
    max_y = max(box[3] for box in bboxes)
    if normalized_canvas is None:
        normalized_canvas = max_x > page_width * 1.25 or max_y > page_height * 1.25
    scale_x = page_width / 1000.0 if normalized_canvas else 1.0
    scale_y = page_height / 1000.0 if normalized_canvas else 1.0

    rect: fitz.Rect | None = None
    for box in bboxes:
        candidate = fitz.Rect(box[0] * scale_x, box[1] * scale_y, box[2] * scale_x, box[3] * scale_y)
        rect = candidate if rect is None else rect | candidate
    if rect is None:
        raise RuntimeError("empty bbox union")
    rect += (-padding_points, -padding_points, padding_points, padding_points)
    rect = rect & page.rect

    pix = page.get_pixmap(matrix=fitz.Matrix(render_scale, render_scale), clip=rect, alpha=False)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pix.save(str(out_path))
    trim_white_margin(out_path)


def render_crop_atomically(
    *,
    pdf: fitz.Document,
    page_idx: int,
    bboxes: list[list[float]],
    out_path: Path,
    render_scale: float,
    padding_points: float,
    normalized_canvas: bool,
) -> tuple[str, str]:
    """Render to a same-directory temporary PNG, verify it, then atomically promote it."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{out_path.name}.crop-",
        suffix=".png",
        dir=str(out_path.parent),
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    temporary.unlink(missing_ok=True)
    try:
        crop_from_bbox(
            pdf=pdf,
            page_idx=page_idx,
            bboxes=bboxes,
            out_path=temporary,
            render_scale=render_scale,
            padding_points=padding_points,
            normalized_canvas=normalized_canvas,
        )
        with Image.open(temporary) as image:
            image.verify()
        content = temporary.read_bytes()
        output_sha256 = hashlib.sha256(content).hexdigest()
        write_method = atomic_write_bytes(out_path, content, min_bytes=8)
        return output_sha256, write_method
    finally:
        temporary.unlink(missing_ok=True)


def process(args: argparse.Namespace) -> dict[str, Any]:
    if fitz is None or Image is None or ImageChops is None:
        return {"generated": [], "skipped": [{"reason": "PyMuPDF (fitz) and Pillow are required for figure crop"}]}
    extract_dir = Path(args.extract_dir) if args.extract_dir else None
    layout_dir = Path(args.layout_dir) if args.layout_dir else extract_dir
    if layout_dir is None:
        return {"generated": [], "skipped": [{"reason": "no extraction or layout directory supplied"}]}
    markdown_path = Path(args.markdown_path)
    resource_root = Path(args.resource_root)
    cache_path = (
        Path(args.cache_path)
        if getattr(args, "cache_path", None)
        else resource_root.parent / ".tmp" / "paper-card-cache" / "figure-crop-cache-v1.json"
    )
    pdf_path = Path(args.source_pdf) if args.source_pdf else find_origin_pdf(extract_dir)
    if pdf_path is None:
        return {"generated": [], "skipped": [{"reason": "no origin pdf found"}]}
    if not pdf_path.is_file():
        return {"generated": [], "skipped": [{"reason": f"source PDF does not exist: {pdf_path}"}]}

    image_index = build_image_bbox_index(layout_dir)
    if not image_index:
        return {"generated": [], "skipped": [{"reason": "no image bbox index found"}]}

    content = markdown_path.read_text(encoding="utf-8")
    asset_map: dict[str, str] = {}
    if args.asset_map:
        try:
            loaded = load_json(Path(args.asset_map))
            if isinstance(loaded, dict):
                asset_map = {str(key): str(value) for key, value in loaded.items()}
        except Exception:
            asset_map = {}
    replacements: list[tuple[tuple[int, int], str]] = []
    generated: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    cache = load_crop_cache(cache_path)
    cache_entries = cache["entries"]
    cache_hits = 0
    cache_misses = 0
    source_pdf_sha256: str | None = None
    layout_sha256 = layout_sources_fingerprint(layout_dir)

    pdf: fitz.Document | None = None
    try:
        for match in IMAGE_GROUP_RE.finditer(content):
            fig = match.group("fig")
            links = [m.group("link") for m in IMAGE_LINK_RE.finditer(match.group("group"))]
            original_names = [original_image_name(link, args.asset_prefix, asset_map) for link in links]
            if any(name is None for name in original_names):
                skipped.append({"figure": fig, "reason": "group contains non-MinerU or already-cropped image"})
                continue

            items: list[dict[str, Any]] = []
            missing: list[str] = []
            for name in original_names:
                hits = image_index.get(str(name), [])
                if not hits:
                    missing.append(str(name))
                else:
                    items.append(hits[0])
            if missing:
                skipped.append({"figure": fig, "reason": "missing bbox for image", "missing": missing})
                continue

            pages = {item["page_idx"] for item in items}
            if len(pages) != 1:
                skipped.append({"figure": fig, "reason": "image group spans multiple pages", "pages": sorted(pages)})
                continue

            page_idx = pages.pop()
            out_name = f"{args.asset_prefix}-fig{fig}-pdf-crop.png"
            out_path = resource_root / out_name
            if source_pdf_sha256 is None:
                source_pdf_sha256 = sha256_file(pdf_path)
            bboxes = [item["bbox"] for item in items]
            normalized_canvas = any(bool(item.get("normalized_canvas")) for item in items)
            crop_fingerprint = fingerprint_json(
                {
                    "algorithm_version": CROP_ALGORITHM_VERSION,
                    "source_pdf_sha256": source_pdf_sha256,
                    "layout_sources_sha256": layout_sha256,
                    "figure": str(fig),
                    "original_images": [str(name) for name in original_names],
                    "page_idx": page_idx,
                    "bboxes": bboxes,
                    "normalized_canvas": normalized_canvas,
                    "render_scale": args.render_scale,
                    "padding_points": args.padding_points,
                    "trim_padding": TRIM_PADDING,
                    "trim_threshold": TRIM_THRESHOLD,
                    "output_name": out_name,
                }
            )
            cached = cache_entries.get(crop_fingerprint)
            cache_hit = False
            output_sha256: str
            write_method: str | None = None
            if (
                isinstance(cached, dict)
                and cached.get("output") == out_name
                and isinstance(cached.get("sha256"), str)
                and out_path.is_file()
            ):
                output_sha256 = sha256_file(out_path)
                cache_hit = output_sha256 == cached["sha256"]
            if cache_hit:
                cache_hits += 1
            else:
                cache_misses += 1
                if pdf is None:
                    pdf = fitz.open(pdf_path)
                output_sha256, write_method = render_crop_atomically(
                    pdf=pdf,
                    page_idx=page_idx,
                    bboxes=bboxes,
                    out_path=out_path,
                    render_scale=args.render_scale,
                    padding_points=args.padding_points,
                    normalized_canvas=normalized_canvas,
                )
                cache_entries[crop_fingerprint] = {
                    "output": out_name,
                    "sha256": output_sha256,
                }
                atomic_write_json(cache_path, cache)

            rel = relative_markdown_path(markdown_path, out_path)
            replacement = f"![]({rel})\n\n"
            replacements.append((match.span("group"), replacement))
            generated.append(
                {
                    "figure": fig,
                    "page_idx": page_idx,
                    "images_replaced": len(links),
                    "output": str(out_path),
                    "fingerprint": crop_fingerprint,
                    "sha256": output_sha256,
                    "status": "cache_hit" if cache_hit else "generated",
                    "write_method": write_method,
                }
            )
    finally:
        if pdf is not None:
            pdf.close()

    if replacements:
        updated = content
        for (start, end), replacement in reversed(replacements):
            updated = updated[:start] + replacement + updated[end:]
        atomic_write_text(markdown_path, updated, min_bytes=20)

    return {
        "generated": generated,
        "skipped": skipped,
        "cache_hits": cache_hits,
        "cache_misses": cache_misses,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Create PDF figure crops from MinerU layout JSON and replace fragmented Markdown image groups.")
    parser.add_argument("--extract-dir")
    parser.add_argument("--source-pdf")
    parser.add_argument("--layout-dir")
    parser.add_argument("--asset-map")
    parser.add_argument("--cache-path")
    parser.add_argument("--markdown-path", required=True)
    parser.add_argument("--resource-root", required=True)
    parser.add_argument("--asset-prefix", required=True)
    parser.add_argument("--render-scale", type=float, default=3.0)
    parser.add_argument("--padding-points", type=float, default=2.0)
    args = parser.parse_args()
    if not args.extract_dir and not args.layout_dir:
        parser.error("one of --extract-dir or --layout-dir is required")
    if not args.extract_dir and not args.source_pdf:
        parser.error("--source-pdf is required when --extract-dir is omitted")

    result = process(args)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise
