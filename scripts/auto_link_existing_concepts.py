#!/usr/bin/env python3
"""Safely link existing Obsidian concepts in a paper's Chinese reading layer.

Only note stems and explicit frontmatter aliases may be written automatically.
Heading, parenthetical, and body-derived terms are retained in the report for
review, never promoted to automatic links.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from safe_atomic_io import atomic_write_json, atomic_write_text
from typing import Iterable


EXCLUDED_DIRS = {
    ".obsidian", ".codex", ".tmp", ".vscode", "__pycache__", "_resources", "_附件", "backup",
    "Template", "Excalidraw", "hover-notes-images", "media-lib", "Clippings", "diary", "周报", "计划",
    "成长", "课程", "中医资料", "bili-vision-notes-skill", "tools", "trendradar", "video-notes",
    "medianote", "99-Codex-Memory", "$", "%TEMP%",
}
GENERIC_ASCII = {
    "abstract", "action", "actions", "agent", "agents", "algorithm", "approach", "data", "dataset",
    "datasets", "experiment", "experiments", "framework", "function", "item", "items", "method",
    "methods", "model", "models", "object", "objects", "paper", "policy", "policies", "result",
    "results", "robot", "robots", "state", "states", "system", "systems", "task", "tasks",
    "teleoperation", "training", "trajectory", "trajectories", "value",
}
GENERIC_CJK = {
    "动作", "方法", "模型", "算法", "结果", "数据", "实现", "数学", "系统", "状态", "策略", "价值",
    "估计", "公式", "过程", "相关", "行动", "类型", "类别", "类", "函数", "方程", "定义", "定理", "证明",
    "问题", "理论", "分析", "框架", "结构", "工具", "目标", "示例", "例子", "学习", "控制", "优化", "机器人",
    "数据集", "物品", "最直观", "信息量",
}
MOJIBAKE_MARKERS = ("鍙", "浜", "琛", "璁", "鏈", "鈥", "銆", "�")
INDEX_SCHEMA_VERSION = 1
PROTECTED_RE = re.compile(r"(\[\[[^\]]+\]\]|\$[^$\n]+\$|`[^`]+`|https?://\S+|<sup>,</sup>)")
WIKILINK_RE = re.compile(r"\[\[([^\]#|]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")


@dataclass(frozen=True)
class Candidate:
    surface: str
    target: str
    provenance: str
    auto_write: bool
    case_sensitive: bool


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def split_frontmatter(text: str) -> tuple[str, str]:
    match = re.match(r"(?s)^---\r?\n(.*?)\r?\n---\r?\n", text)
    return (match.group(1), text[match.end() :]) if match else ("", text)


def parse_yaml_list(frontmatter: str, key: str) -> list[str]:
    values: list[str] = []
    block = re.search(rf"(?ms)^{re.escape(key)}:\s*\n((?:\s+-\s*.*\n?)+)", frontmatter)
    if block:
        for line in block.group(1).splitlines():
            item = re.match(r"\s+-\s*(.+?)\s*$", line)
            if item:
                values.append(item.group(1).strip().strip("\"'"))
    inline = re.search(rf"(?m)^{re.escape(key)}:\s*\[(.*?)\]\s*$", frontmatter)
    if inline:
        values.extend(value.strip().strip("\"'") for value in inline.group(1).split(","))
    single = re.search(rf"(?m)^{re.escape(key)}:\s*(.+?)\s*$", frontmatter)
    if single and not block and not inline:
        values.append(single.group(1).strip().strip("\"'"))
    return [value for value in values if value]


def has_cjk(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", text))


def is_generic_surface(surface: str, denylist: set[str]) -> bool:
    value = surface.strip()
    if not value or value in denylist or value.lower() in {word.lower() for word in denylist}:
        return True
    if re.fullmatch(r"[\d.\-]+", value):
        return True
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", value):
        return value.lower() in GENERIC_ASCII or (len(value) < 3 and value not in {"MDP", "RL"})
    if has_cjk(value):
        return value in GENERIC_CJK or (len(value) < 3 and value not in {"方差"})
    return False


def is_paper_note(relative: Path, frontmatter: str) -> bool:
    parts = set(relative.parts)
    tags = {tag.lower() for tag in parse_yaml_list(frontmatter, "tags")}
    if ("paper" in tags or "论文" in tags) and "concept" not in tags:
        return True
    if "提示词" in relative.stem:
        return True
    if "论文" not in parts:
        return False
    return "概念" not in str(relative) and "-资料" not in str(relative) and "concept" not in tags


def derived_surfaces(text: str) -> list[str]:
    _, body = split_frontmatter(text)
    values: list[str] = []
    for heading in re.findall(r"(?m)^#{1,3}\s+(.+?)\s*$", body[:3000]):
        clean = re.sub(r"\[\[([^\]|]+)\|?([^\]]*)\]\]", lambda match: match.group(2) or match.group(1), heading)
        clean = re.sub(r"[#`*_]", "", clean).strip()
        if clean:
            values.append(clean)
        for group in re.findall(r"[（(]([^（）()]{2,80})[）)]", clean):
            values.append(group.strip())
            values.extend(part.strip() for part in re.split(r"[,;/，；、]", group))
    return [value for value in values if value]


def note_surfaces(path: Path, frontmatter: str, text: str, denylist: set[str]) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for surface, provenance in [(path.stem, "stem"), *[(value, "alias") for value in parse_yaml_list(frontmatter, "aliases")], *[(value, "derived_heading") for value in derived_surfaces(text)]]:
        value = surface.strip()
        if not value or len(value) > 80 or is_generic_surface(value, denylist):
            continue
        result.append((value, provenance))
    return result


def iter_notes(vault_root: Path) -> Iterable[Path]:
    for path in vault_root.rglob("*.md"):
        relative = path.relative_to(vault_root)
        if ".bak-" in path.name or path.name.endswith(".tmp") or any(part in EXCLUDED_DIRS for part in relative.parts):
            continue
        yield path


def read_index(path: Path | None) -> dict[str, dict[str, object]]:
    if path is None or not path.is_file():
        return {}
    try:
        value = json.loads(read_text(path))
        if value.get("schema_version") != INDEX_SCHEMA_VERSION:
            return {}
        entries = value.get("entries")
        return entries if isinstance(entries, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def write_index(path: Path | None, entries: dict[str, dict[str, object]]) -> None:
    if path is None:
        return
    atomic_write_json(path, {"schema_version": INDEX_SCHEMA_VERSION, "entries": entries})


def index_entry(path: Path, relative: Path, denylist: set[str], cached: dict[str, object] | None) -> dict[str, object] | None:
    stat = path.stat()
    if cached and cached.get("mtime_ns") == stat.st_mtime_ns and cached.get("size") == stat.st_size:
        return cached
    try:
        text = read_text(path)
    except UnicodeDecodeError:
        return None
    frontmatter, _ = split_frontmatter(text[:5000])
    return {
        "mtime_ns": stat.st_mtime_ns,
        "size": stat.st_size,
        "target": path.stem,
        "is_paper": is_paper_note(relative, frontmatter),
        "surfaces": note_surfaces(path, frontmatter, text, denylist),
    }


def build_candidates(vault_root: Path, markdown_path: Path, denylist: set[str], index_cache: Path | None = None) -> tuple[list[Candidate], dict[str, list[str]], list[dict[str, str]]]:
    targets: dict[str, list[Path]] = {}
    surfaces: dict[str, dict[str, set[str]]] = {}
    rejected: list[dict[str, str]] = []
    cached_entries = read_index(index_cache)
    refreshed_entries: dict[str, dict[str, object]] = {}
    for path in iter_notes(vault_root):
        if path.resolve() == markdown_path.resolve():
            continue
        relative = path.relative_to(vault_root)
        key = relative.as_posix()
        entry = index_entry(path, relative, denylist, cached_entries.get(key))
        if entry is None:
            continue
        refreshed_entries[key] = entry
        if bool(entry["is_paper"]):
            continue
        target = str(entry["target"])
        targets.setdefault(target, []).append(path)
        for surface, provenance in entry["surfaces"]:
            surfaces.setdefault(surface, {}).setdefault(target, set()).add(provenance)
    write_index(index_cache, refreshed_entries)

    ambiguous_targets = {target for target, paths in targets.items() if len(paths) > 1}
    ambiguous_surfaces: dict[str, list[str]] = {}
    candidates: list[Candidate] = []
    for surface, target_map in surfaces.items():
        usable = {target: origins for target, origins in target_map.items() if target not in ambiguous_targets}
        if len(usable) != 1:
            ambiguous_surfaces[surface] = sorted(usable)
            continue
        target, origins = next(iter(usable.items()))
        if "stem" in origins:
            provenance, auto_write = "stem", True
        elif "alias" in origins:
            provenance, auto_write = "alias", True
        else:
            provenance, auto_write = "derived_heading", False
        candidates.append(Candidate(surface, target, provenance, auto_write, bool(re.fullmatch(r"[A-Z0-9_-]{2,}", surface))))
    candidates.sort(key=lambda item: (len(item.surface), item.surface), reverse=True)
    return candidates, ambiguous_surfaces, rejected


def compile_pattern(candidate: Candidate) -> re.Pattern[str]:
    flags = 0 if candidate.case_sensitive else re.IGNORECASE
    if re.search(r"[A-Za-z0-9]", candidate.surface):
        return re.compile(r"(?<![A-Za-z0-9_])" + re.escape(candidate.surface) + r"(?![A-Za-z0-9_])", flags)
    return re.compile(re.escape(candidate.surface))


def link_for(target: str, original: str) -> str:
    return f"[[{target}]]" if target == original else f"[[{target}|{original}]]"


def extract_targets(line: str) -> set[str]:
    return {match.group(1).split("/")[-1] for match in WIKILINK_RE.finditer(line)}


def link_segment(segment: str, candidates: list[Candidate], section_seen: set[str], counts: dict[str, int]) -> str:
    matches: list[tuple[int, int, Candidate, str]] = []
    occupied: list[tuple[int, int]] = []
    for candidate in candidates:
        if not candidate.auto_write or candidate.target in section_seen:
            continue
        match = compile_pattern(candidate).search(segment)
        if match:
            matches.append((match.start(), match.end(), candidate, match.group(0)))
    pieces: list[str] = []
    position = 0
    for start, end, candidate, original in sorted(matches, key=lambda item: (item[0], -(item[1] - item[0]))):
        if any(not (end <= used_start or start >= used_end) for used_start, used_end in occupied):
            continue
        pieces.append(segment[position:start])
        pieces.append(link_for(candidate.target, original))
        position = end
        occupied.append((start, end))
        section_seen.add(candidate.target)
        counts[candidate.target] = counts.get(candidate.target, 0) + 1
    return segment if not pieces else "".join(pieces) + segment[position:]


def should_process(line: str, in_references: bool) -> bool:
    stripped = line.strip()
    return bool(stripped and not in_references and has_cjk(line) and not stripped.startswith((">", "!", "|", "<", "#")))


def process_markdown(text: str, candidates: list[Candidate]) -> tuple[str, dict[str, int], list[dict[str, object]]]:
    output: list[str] = []
    counts: dict[str, int] = {}
    changes: list[dict[str, object]] = []
    frontmatter = code = math = references = False
    section_seen: set[str] = set()
    for number, raw in enumerate(text.splitlines(keepends=True), 1):
        line = raw.rstrip("\r\n")
        newline = raw[len(line) :]
        stripped = line.strip()
        if number == 1 and stripped == "---":
            frontmatter = True
        elif frontmatter and stripped == "---":
            frontmatter = False
        elif stripped.startswith("```"):
            code = not code
        elif stripped == "$$":
            math = not math
        elif re.match(r"^#\s+References\b", stripped, re.IGNORECASE):
            references = True
        elif stripped.startswith("#"):
            section_seen = set()
        if frontmatter or code or math or not should_process(line, references):
            output.append(raw)
            continue
        parts: list[str] = []
        cursor = 0
        for protected in PROTECTED_RE.finditer(line):
            parts.append(link_segment(line[cursor : protected.start()], candidates, section_seen, counts))
            parts.append(protected.group(0))
            cursor = protected.end()
        parts.append(link_segment(line[cursor:], candidates, section_seen, counts))
        after = "".join(parts)
        section_seen.update(extract_targets(after))
        if after != line:
            changes.append({"line": number, "before": line, "after": after})
        output.append(after + newline)
    return "".join(output), counts, changes


def markers(text: str) -> dict[str, int]:
    return {
        "display_math": len(re.findall(r"(?m)^\s*\$\$\s*$", text)),
        "code_fences": len(re.findall(r"(?m)^```", text)),
        "images": len(re.findall(r"!\[[^\]]*\]\([^)]+\)", text)),
        "reference_blocks": len(re.findall(r"\^ref-", text)),
    }


def validate_change(before: str, after: str, vault_root: Path) -> list[str]:
    errors: list[str] = []
    before_markers, after_markers = markers(before), markers(after)
    for key, value in before_markers.items():
        if after_markers[key] != value:
            errors.append(f"{key} changed: {value} -> {after_markers[key]}")
    if after_markers["display_math"] % 2 or after_markers["code_fences"] % 2:
        errors.append("unbalanced math or code delimiters")
    for marker in MOJIBAKE_MARKERS:
        if marker in after:
            errors.append(f"mojibake marker remains: {marker}")
    for target in {match.group(1).split("/")[-1] for match in WIKILINK_RE.finditer(after)} - {match.group(1).split("/")[-1] for match in WIKILINK_RE.finditer(before)}:
        if not any(path.stem == target for path in iter_notes(vault_root)):
            errors.append(f"new link target does not exist: {target}")
    return errors


def review_candidates(text: str, candidates: list[Candidate]) -> list[dict[str, object]]:
    report: list[dict[str, object]] = []
    for candidate in candidates:
        if candidate.auto_write:
            continue
        flags = 0 if candidate.case_sensitive else re.IGNORECASE
        occurrences = len(re.findall(re.escape(candidate.surface), text, flags))
        if occurrences:
            report.append({"surface": candidate.surface, "target": candidate.target, "provenance": candidate.provenance, "status": "requires_review", "occurrence_count": occurrences})
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault-root", required=True)
    parser.add_argument("--markdown-path", required=True)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--backup", action="store_true")
    parser.add_argument("--denylist-file")
    parser.add_argument("--index-cache", help="Incremental cache for safe stem/alias concept candidates.")
    parser.add_argument("--max-sample-changes", type=int, default=12)
    args = parser.parse_args()
    vault_root, markdown_path = Path(args.vault_root).resolve(), Path(args.markdown_path).resolve()
    denylist = set(GENERIC_CJK) | set(GENERIC_ASCII)
    if args.denylist_file:
        denylist.update(line.strip() for line in Path(args.denylist_file).read_text(encoding="utf-8").splitlines() if line.strip() and not line.lstrip().startswith("#"))
    before = read_text(markdown_path)
    candidates, ambiguous, rejected = build_candidates(vault_root, markdown_path, denylist, Path(args.index_cache).resolve() if args.index_cache else None)
    after, counts, changes = process_markdown(before, candidates)
    errors = validate_change(before, after, vault_root) if changes else []
    backup_path: Path | None = None
    if args.write and changes and not errors:
        if args.backup:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            backup_path = markdown_path.with_name(markdown_path.name + f".bak-{stamp}-before-concept-links")
            shutil.copy2(markdown_path, backup_path)
        atomic_write_text(markdown_path, after, min_bytes=20)
    report = {
        "changed": bool(changes),
        "wrote": bool(args.write and changes and not errors),
        "markdown_path": str(markdown_path),
        "backup_path": str(backup_path) if backup_path else None,
        "auto_candidate_count": sum(candidate.auto_write for candidate in candidates),
        "review_candidate_count": sum(not candidate.auto_write for candidate in candidates),
        "ambiguous_surfaces": ambiguous,
        "rejected_surfaces": rejected,
        "linked_counts": counts,
        "changed_line_count": len(changes),
        "sample_changes": changes[: args.max_sample_changes],
        "review_candidates": review_candidates(before, candidates),
        "validation_errors": errors,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
