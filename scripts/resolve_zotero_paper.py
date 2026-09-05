#!/usr/bin/env python3
"""Resolve one paper from ordered Vault sources, then a read-only Zotero snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import sys
import unicodedata
import uuid
from pathlib import Path
from typing import Any


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compact_print(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def clean_title(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", value)
    value = re.sub(r"\s+", " ", value).strip().rstrip(".")
    if not value:
        raise ValueError("title becomes empty after Windows filename normalization")
    return value[:180]


def canonical_title(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value)).strip().casefold()


def filename_title_key(value: str) -> str:
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", value).casefold(), flags=re.UNICODE)


def read_frontmatter_title(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return None
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for line in lines[1:]:
        if line.strip() == "---":
            break
        match = re.match(r"^\s*title\s*:\s*(.*?)\s*$", line, flags=re.IGNORECASE)
        if not match:
            continue
        value = match.group(1).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        return value.strip() or None
    return None


def vault_markdown_matches(vault_root: Path, directory_name: str, title: str) -> list[dict[str, str]]:
    directory = (vault_root / directory_name).resolve()
    if not directory.is_dir():
        return []
    exact_key = canonical_title(title)
    filename_key = filename_title_key(title)
    matches: list[dict[str, str]] = []
    for path in sorted(directory.rglob("*.md"), key=lambda candidate: str(candidate).casefold()):
        frontmatter_title = read_frontmatter_title(path)
        matched_by: str | None = None
        if frontmatter_title is not None and canonical_title(frontmatter_title) == exact_key:
            matched_by = "frontmatter_title"
        elif filename_title_key(path.stem) == filename_key:
            matched_by = "normalized_filename"
        if matched_by:
            matches.append({"path": str(path.resolve()), "matched_by": matched_by})
    return matches


def resolve_vault_markdown(
    vault_root: Path,
    title: str,
    target_directory: str,
    source_directories: tuple[str, ...],
) -> tuple[int, dict[str, Any]] | None:
    search_order = [*source_directories, "Zotero"]
    for directory_name in source_directories:
        matches = vault_markdown_matches(vault_root, directory_name, title)
        if len(matches) > 1:
            return 2, {
                "ok": False,
                "error": "multiple_vault_markdown_matches",
                "source_scope": directory_name,
                "source_search_order": search_order,
                "candidates": matches[:3],
            }
        if not matches:
            continue
        source_path = Path(matches[0]["path"]).resolve()
        target_note = (vault_root / target_directory / f"{clean_title(title)}.md").resolve()
        target_exists = target_note.exists()
        in_place_required = source_path == target_note
        payload = {
            "ok": not target_exists or in_place_required,
            "source_kind": "vault_markdown",
            "source_scope": directory_name,
            "source_search_order": search_order,
            "matched_by": matches[0]["matched_by"],
            "input_markdown": str(source_path),
            "target_note": str(target_note),
            "target_exists": target_exists,
            "in_place_required": in_place_required,
        }
        if target_exists and not in_place_required:
            payload["error"] = "target_note_exists"
            return 2, payload
        return 0, payload
    return None


def copy_snapshot(data_dir: Path, snapshot_root: Path) -> tuple[Path, Path]:
    source = data_dir / "zotero.sqlite"
    if not source.is_file():
        raise ValueError(f"Zotero database does not exist: {source}")
    snapshot_dir = snapshot_root / f"zotero-snapshot-{uuid.uuid4().hex}"
    snapshot_dir.mkdir(parents=True, exist_ok=False)
    for suffix in ("", "-wal", "-shm"):
        candidate = data_dir / f"zotero.sqlite{suffix}"
        if candidate.is_file():
            shutil.copy2(candidate, snapshot_dir / candidate.name)
    return snapshot_dir / "zotero.sqlite", snapshot_dir


def remove_snapshot(snapshot_dir: Path | None, snapshot_root: Path) -> None:
    if snapshot_dir is None or not snapshot_dir.exists():
        return
    resolved = snapshot_dir.resolve()
    root = snapshot_root.resolve()
    if resolved.parent != root or not resolved.name.startswith("zotero-snapshot-"):
        raise RuntimeError(f"refusing to clean unexpected snapshot directory: {resolved}")
    for child in resolved.iterdir():
        if child.is_file():
            child.unlink()
        else:
            raise RuntimeError(f"unexpected nested snapshot entry: {child}")
    resolved.rmdir()


TITLE_QUERY = """
SELECT DISTINCT i.itemID, i.key, idv.value
FROM items AS i
JOIN itemData AS id ON id.itemID = i.itemID
JOIN itemDataValues AS idv ON idv.valueID = id.valueID
JOIN fields AS f ON f.fieldID = id.fieldID
WHERE f.fieldName = 'title'
  AND NOT EXISTS (SELECT 1 FROM deletedItems d WHERE d.itemID = i.itemID)
  AND lower(trim(idv.value)) = lower(trim(?))
ORDER BY i.itemID
"""


def exact_items(connection: sqlite3.Connection, title: str) -> list[tuple[int, str, str]]:
    return [(int(row[0]), str(row[1]), str(row[2])) for row in connection.execute(TITLE_QUERY, (title,))]


def title_candidates(connection: sqlite3.Connection, title: str) -> list[dict[str, str]]:
    words = [word for word in re.split(r"\W+", title, flags=re.UNICODE) if len(word) >= 4]
    needle = " ".join(words[:5]) if words else title.strip()
    rows = connection.execute(
        """
        SELECT DISTINCT i.key, idv.value
        FROM items i
        JOIN itemData id ON id.itemID = i.itemID
        JOIN itemDataValues idv ON idv.valueID = id.valueID
        JOIN fields f ON f.fieldID = id.fieldID
        WHERE f.fieldName = 'title'
          AND NOT EXISTS (SELECT 1 FROM deletedItems d WHERE d.itemID = i.itemID)
          AND lower(idv.value) LIKE lower(?)
        ORDER BY length(idv.value), idv.value
        LIMIT 3
        """,
        (f"%{needle}%",),
    ).fetchall()
    if not rows and words:
        rows = connection.execute(
            """
            SELECT DISTINCT i.key, idv.value
            FROM items i
            JOIN itemData id ON id.itemID = i.itemID
            JOIN itemDataValues idv ON idv.valueID = id.valueID
            JOIN fields f ON f.fieldID = id.fieldID
            WHERE f.fieldName = 'title'
              AND NOT EXISTS (SELECT 1 FROM deletedItems d WHERE d.itemID = i.itemID)
              AND lower(idv.value) LIKE lower(?)
            ORDER BY length(idv.value), idv.value
            LIMIT 3
            """,
            (f"%{words[0]}%",),
        ).fetchall()
    return [{"item_key": str(row[0]), "title": str(row[1])} for row in rows[:3]]


def attachment_rows(connection: sqlite3.Connection, parent_item_id: int) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT ai.key, ia.path, ia.contentType, ia.linkMode
        FROM itemAttachments ia
        JOIN items ai ON ai.itemID = ia.itemID
        WHERE ia.parentItemID = ?
          AND NOT EXISTS (SELECT 1 FROM deletedItems d WHERE d.itemID = ia.itemID)
          AND (lower(coalesce(ia.contentType, '')) = 'application/pdf'
               OR lower(coalesce(ia.path, '')) LIKE '%.pdf')
        ORDER BY ai.itemID
        """,
        (parent_item_id,),
    ).fetchall()
    return [
        {"attachment_key": str(row[0]), "stored_path": str(row[1] or ""), "content_type": str(row[2] or ""), "link_mode": int(row[3])}
        for row in rows
    ]


def resolve_attachment(data_dir: Path, row: dict[str, Any]) -> Path:
    stored = row["stored_path"]
    if stored.startswith("storage:"):
        return (data_dir / "storage" / row["attachment_key"] / stored[len("storage:") :]).resolve()
    candidate = Path(stored)
    if candidate.is_absolute():
        return candidate.resolve()
    raise ValueError(f"unsupported linked attachment path: {stored}")


def find_reusable_package(vault_root: Path, item_key: str, pdf_sha256: str, parse_fingerprint: str | None) -> str | None:
    package_root = vault_root / ".tmp" / "mineru-zotero" / item_key
    if not package_root.is_dir():
        return None
    for manifest_path in sorted(package_root.rglob("source-manifest.json"), reverse=True):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        integrity = manifest.get("integrity") or {}
        producer = manifest.get("producer") or {}
        if integrity.get("source_file_sha256") != pdf_sha256:
            continue
        if parse_fingerprint and producer.get("parse_options_fingerprint") != parse_fingerprint:
            continue
        source_md = manifest_path.parent / str((manifest.get("artifacts") or {}).get("markdown") or "source.md")
        if source_md.is_file():
            return str(manifest_path.parent.resolve())
    return None


def resolve(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    data_dir = Path(args.zotero_data_dir).resolve()
    vault_root = Path(args.vault_root).resolve()
    snapshot_root = Path(args.snapshot_directory).resolve()
    source_directories = tuple(dict.fromkeys(getattr(args, "vault_source_directories", None) or ("Clippings", args.target_directory)))
    vault_result = resolve_vault_markdown(vault_root, args.title, args.target_directory, source_directories)
    if vault_result is not None:
        return vault_result
    zotero_metadata = {
        "source_kind": "zotero_lookup",
        "source_scope": "Zotero",
        "source_search_order": [*source_directories, "Zotero"],
    }
    snapshot_root.mkdir(parents=True, exist_ok=True)
    snapshot_dir: Path | None = None
    try:
        database, snapshot_dir = copy_snapshot(data_dir, snapshot_root)
        connection = sqlite3.connect(database)
        try:
            matches = exact_items(connection, args.title)
            if not matches:
                return 2, {**zotero_metadata, "ok": False, "error": "no_exact_title_match", "candidates": title_candidates(connection, args.title)}
            if len(matches) != 1:
                return 2, {
                    **zotero_metadata,
                    "ok": False,
                    "error": "multiple_exact_title_matches",
                    "candidates": [{"item_key": row[1], "title": row[2]} for row in matches[:3]],
                }
            item_id, item_key, matched_title = matches[0]
            attachments = attachment_rows(connection, item_id)
        finally:
            connection.close()
        if len(attachments) != 1:
            return 2, {
                **zotero_metadata,
                "ok": False,
                "error": "no_pdf_attachment" if not attachments else "multiple_pdf_attachments",
                "item_key": item_key,
                "attachment_count": len(attachments),
                "attachments": [{"attachment_key": row["attachment_key"], "stored_path": row["stored_path"]} for row in attachments[:3]],
            }
        attachment = attachments[0]
        pdf_path = resolve_attachment(data_dir, attachment)
        if not pdf_path.is_file() or pdf_path.suffix.lower() != ".pdf":
            return 2, {**zotero_metadata, "ok": False, "error": "pdf_missing_or_invalid_extension", "item_key": item_key, "pdf_path": str(pdf_path)}
        if attachment["content_type"] and attachment["content_type"].lower() != "application/pdf":
            return 2, {**zotero_metadata, "ok": False, "error": "invalid_pdf_mime", "item_key": item_key, "mime": attachment["content_type"]}
        pdf_bytes = pdf_path.stat().st_size
        if pdf_bytes <= 0:
            return 2, {**zotero_metadata, "ok": False, "error": "empty_pdf", "item_key": item_key, "pdf_path": str(pdf_path)}
        pdf_sha256 = sha256_file(pdf_path)
        target_note = (vault_root / args.target_directory / f"{clean_title(matched_title)}.md").resolve()
        target_exists = target_note.exists()
        payload = {
            **zotero_metadata,
            "ok": not target_exists,
            "source_kind": "zotero_pdf",
            "item_key": item_key,
            "attachment_key": attachment["attachment_key"],
            "pdf_path": str(pdf_path),
            "pdf_bytes": pdf_bytes,
            "pdf_sha256": pdf_sha256,
            "target_note": str(target_note),
            "target_exists": target_exists,
            "reusable_source_package": find_reusable_package(vault_root, item_key, pdf_sha256, args.parse_options_fingerprint),
        }
        if target_exists:
            payload["error"] = "target_note_exists"
            return 2, payload
        return 0, payload
    finally:
        remove_snapshot(snapshot_dir, snapshot_root)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--title", required=True)
    parser.add_argument("--zotero-data-dir", required=True)
    parser.add_argument("--vault-root", required=True)
    parser.add_argument("--target-directory", default="论文")
    parser.add_argument(
        "--vault-source-directory",
        action="append",
        dest="vault_source_directories",
        help="Ordered Vault Markdown source directory; repeat to override the default Clippings then target-directory order.",
    )
    parser.add_argument("--snapshot-directory", required=True)
    parser.add_argument("--parse-options-fingerprint")
    args = parser.parse_args()
    try:
        code, payload = resolve(args)
    except Exception as exc:
        code, payload = 2, {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    compact_print(payload)
    return code


if __name__ == "__main__":
    sys.exit(main())
