#!/usr/bin/env python3
"""Windows-safe validated atomic writes with recovery fallback."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable


TextValidator = Callable[[str], None]


def validate_json_text(content: str) -> None:
    json.loads(content)


def validate_nonempty_text(content: str) -> None:
    if not content.strip():
        raise ValueError("text output is empty")


def validate_markdown_text(content: str) -> None:
    if len(content.encode("utf-8")) < 20:
        raise ValueError("Markdown output is unexpectedly small")
    if not content.startswith("---\n"):
        raise ValueError("Markdown output must begin with frontmatter")
    if "\n# " not in content:
        raise ValueError("Markdown output has no H1 title")


def _temporary_path(path: Path, label: str) -> Path:
    return path.with_name(f".{path.name}.{label}-{next(tempfile._get_candidate_names())}")


def _promote_with_windows_fallback(temporary: Path, path: Path) -> str:
    try:
        os.replace(temporary, path)
        return "direct_replace"
    except PermissionError:
        recovery = _temporary_path(path, "recovery")
        had_target = path.exists()
        if had_target:
            shutil.copy2(path, recovery)
        try:
            path.unlink(missing_ok=True)
            os.replace(temporary, path)
        except Exception:
            path.unlink(missing_ok=True)
            if had_target and recovery.is_file():
                shutil.copy2(recovery, path)
            raise
        finally:
            recovery.unlink(missing_ok=True)
        return "windows_unlink_replace"


def atomic_write_bytes(path: Path, content: bytes, *, min_bytes: int = 1) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = _temporary_path(path, "tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if temporary.stat().st_size < min_bytes:
            raise ValueError(f"refusing to write unexpectedly small file: {path}")
        return _promote_with_windows_fallback(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_text(
    path: Path,
    content: str,
    *,
    validator: TextValidator | None = None,
    min_bytes: int = 1,
) -> str:
    if validator is not None:
        validator(content)
    return atomic_write_bytes(path, content.encode("utf-8"), min_bytes=min_bytes)


def atomic_write_json(path: Path, value: Any) -> str:
    content = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    return atomic_write_text(path, content, validator=validate_json_text, min_bytes=2)
