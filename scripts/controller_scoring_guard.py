#!/usr/bin/env python3
"""Fail-closed guard for controller performance scoring model reads."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MODEL_RELATIVE_PATH = Path("references/controller-performance-scoring.md")
RECEIPT_DIRECTORY = "adaptive-delivery"
RECEIPT_FILE = "controller-scoring-model-read.json"


def _git_common_dir(repo: Path) -> Path:
    completed = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--git-common-dir"],
        check=True,
        capture_output=True,
        text=True,
    )
    path = Path(completed.stdout.strip())
    return (repo / path).resolve() if not path.is_absolute() else path.resolve()


def receipt_path(repo: str | Path) -> Path:
    return _git_common_dir(Path(repo).resolve()) / RECEIPT_DIRECTORY / RECEIPT_FILE


def scoring_model_path(skill_root: str | Path) -> Path:
    return Path(skill_root).resolve() / MODEL_RELATIVE_PATH


def scoring_model_sha256(skill_root: str | Path) -> str:
    return hashlib.sha256(scoring_model_path(skill_root).read_bytes()).hexdigest()


def record_model_read(repo: str | Path, *, skill_root: str | Path) -> dict[str, Any]:
    model = scoring_model_path(skill_root)
    # Reading the bytes here is intentional: the receipt proves the exact model content was consumed.
    digest = hashlib.sha256(model.read_bytes()).hexdigest()
    receipt = {
        "schema_version": 1,
        "model_path": str(model),
        "model_sha256": digest,
        "read_at": datetime.now(timezone.utc).isoformat(),
    }
    target = receipt_path(repo)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return receipt


def _load_receipt(repo: str | Path) -> dict[str, Any] | None:
    target = receipt_path(repo)
    if not target.is_file():
        return None
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def score_guard_errors(repo: str | Path, *, skill_root: str | Path) -> list[str]:
    receipt = _load_receipt(repo)
    if receipt is None:
        return ["controller scoring blocked: scoring model read receipt is missing or unreadable"]
    expected = scoring_model_sha256(skill_root)
    if receipt.get("model_sha256") != expected:
        return ["controller scoring blocked: scoring model read receipt is stale for the installed model"]
    if Path(str(receipt.get("model_path", ""))).resolve() != scoring_model_path(skill_root):
        return ["controller scoring blocked: scoring model read receipt points to a different model"]
    return []


def main() -> int:
    parser = argparse.ArgumentParser(description="Record and verify the mandatory controller scoring model read.")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("record-read", "score-guard"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--repo", required=True)
        cmd.add_argument("--skill-root", default=str(Path(__file__).resolve().parents[1]))
    args = parser.parse_args()
    if args.command == "record-read":
        model = scoring_model_path(args.skill_root)
        content = model.read_text(encoding="utf-8")
        receipt = record_model_read(args.repo, skill_root=args.skill_root)
        print(content, end="" if content.endswith("\n") else "\n")
        print("--- controller-scoring-model-read-receipt ---")
        print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
        return 0
    errors = score_guard_errors(args.repo, skill_root=args.skill_root)
    if errors:
        for error in errors:
            print(error)
        return 2
    print("controller scoring: allowed; exact installed scoring model read receipt is current")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
