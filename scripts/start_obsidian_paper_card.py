#!/usr/bin/env python3
"""Resolve a paper title and start one bilingual paper-card run in one process."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from build_obsidian_paper_card import (
    NATIVE_SUBAGENT_BACKEND,
    build,
    compact_cli_report,
)
from resolve_zotero_paper import resolve


def start(args: argparse.Namespace) -> tuple[int, dict[str, object]]:
    resolve_args = argparse.Namespace(
        title=args.title,
        zotero_data_dir=args.zotero_data_dir,
        vault_root=args.vault_root,
        target_directory=args.target_directory,
        vault_source_directories=args.vault_source_directories,
        snapshot_directory=args.snapshot_directory,
        parse_options_fingerprint=args.parse_options_fingerprint,
    )
    code, source = resolve(resolve_args)
    if code:
        return code, {"ok": False, "status": "source_resolution_failed", "source": source}
    if source.get("source_kind") != "vault_markdown":
        return 3, {
            "ok": True,
            "status": "requires_markdown_parse",
            "source": source,
            "next_skill": "mineru-api-markdown",
        }

    build_args = argparse.Namespace(
        input_markdown=str(source["input_markdown"]),
        vault_root=args.vault_root,
        output_note=str(source["target_note"]),
        translation_mode="bilingual",
        concept_links=args.concept_links,
        source_package=None,
        resource_directory=args.resource_directory,
        stable_resource_roots=args.stable_resource_roots,
        bibtex_path=args.bibtex_path,
        translation_stage="run",
        workflow_dir=args.workflow_dir,
        translation_output=None,
        translator_fingerprint=None,
        worker_backend=NATIVE_SUBAGENT_BACKEND,
        translation_agent_role_file=args.translation_agent_role_file,
        translation_agent_task_name=None,
        worker_timeout_seconds=1800,
        worker_max_attempts=2,
        image_converter_layout=args.image_converter_layout,
        overwrite_image_converter_alignments=False,
        in_place=False,
        write=True,
    )
    report = compact_cli_report(build(build_args))
    return 0, {
        **report,
        "source_kind": source.get("source_kind"),
        "source_scope": source.get("source_scope"),
        "input_markdown": source.get("input_markdown"),
        "output_note": source.get("target_note"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--title", required=True)
    parser.add_argument("--zotero-data-dir", required=True)
    parser.add_argument("--vault-root", required=True)
    parser.add_argument("--target-directory", default="论文")
    parser.add_argument("--vault-source-directory", action="append", dest="vault_source_directories")
    parser.add_argument("--snapshot-directory", required=True)
    parser.add_argument("--parse-options-fingerprint")
    parser.add_argument("--workflow-dir", required=True)
    parser.add_argument("--translation-agent-role-file")
    parser.add_argument("--concept-links", choices=("off", "report", "write"), default="off")
    parser.add_argument("--resource-directory", default="_resources")
    parser.add_argument("--stable-resource-root", action="append", dest="stable_resource_roots")
    parser.add_argument("--bibtex-path")
    parser.add_argument("--image-converter-layout", choices=("off", "center"), default="center")
    args = parser.parse_args()
    try:
        code, payload = start(args)
    except Exception as exc:
        code, payload = 2, {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
