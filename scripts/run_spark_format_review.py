from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


ENV_KEYS_TO_REMOVE = (
    "CODEX_CI",
    "CODEX_INTERNAL_ORIGINATOR_OVERRIDE",
    "CODEX_PERMISSION_PROFILE",
    "CODEX_SHELL",
    "CODEX_THREAD_ID",
)


def worker_environment() -> dict[str, str]:
    environment = dict(os.environ)
    for key in ENV_KEYS_TO_REMOVE:
        environment.pop(key, None)
    return environment


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--guide", type=Path, required=True)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--model", default="gpt-5.3-codex-spark")
    parser.add_argument("--timeout-seconds", type=int, default=300)
    args = parser.parse_args()

    source = args.source.resolve()
    guide = args.guide.resolve()
    schema = args.schema.resolve()
    result = args.result.resolve()
    log = args.log.resolve()
    workflow_dir = source.parent
    if not source.is_file() or not guide.is_file() or not schema.is_file():
        raise SystemExit("source, guide, and schema must exist")

    # Explicit UTF-8 on both sides of the child-process protocol.
    guide_text = guide.read_text(encoding="utf-8")
    expected_hash = sha256(source)
    prompt = (
        guide_text.rstrip()
        + "\n\nAssignment\n"
        + f"Inspect this UTF-8 source read-only: {source}\n"
        + "On Windows PowerShell, read it with Get-Content -LiteralPath <path> -Encoding UTF8.\n"
        + "Do not read AGENTS.md, SKILL.md, configuration, credentials, or unrelated files.\n"
        + "Do not write or modify any file. Return only the JSON object required by the output schema.\n"
        + f"The expected source SHA-256 is {expected_hash}.\n"
    )

    sterile_dir = Path(tempfile.mkdtemp(prefix="codex-spark-format-review-"))
    last_message = workflow_dir / "spark-last-message.json"
    command = [
        shutil.which("codex.cmd") or shutil.which("codex.exe") or "codex",
        "exec",
        "--ephemeral",
        "--skip-git-repo-check",
        "--ignore-user-config",
        "--ignore-rules",
        "--model",
        args.model,
        "-c",
        'model_reasoning_effort="low"',
        "-c",
        'sandbox_mode="workspace-write"',
        "--sandbox",
        "workspace-write",
        "-C",
        str(sterile_dir),
        "--add-dir",
        str(workflow_dir),
        "--color",
        "never",
        "--output-schema",
        str(schema),
        "--output-last-message",
        str(last_message),
        "-",
    ]

    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=sterile_dir,
        creationflags=creationflags,
        env=worker_environment(),
    )
    try:
        output, _ = process.communicate(input=prompt, timeout=args.timeout_seconds)
    except subprocess.TimeoutExpired:
        process.kill()
        output, _ = process.communicate(timeout=30)
        output += "\nERROR: Spark formatting review timed out.\n"
    finally:
        shutil.rmtree(sterile_dir, ignore_errors=True)

    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(output or "worker emitted no output\n", encoding="utf-8", newline="\n")
    if process.returncode != 0:
        return process.returncode or 1
    if not last_message.is_file():
        raise SystemExit("Spark returned no structured final message")

    payload = json.loads(last_message.read_text(encoding="utf-8-sig"))
    if payload.get("source_sha256") != expected_hash:
        raise SystemExit("Spark source hash does not match the reviewed file")
    result.parent.mkdir(parents=True, exist_ok=True)
    result.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
