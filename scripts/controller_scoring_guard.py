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
RECEIPT_MAX_AGE_SECONDS = 1800


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


def _write_receipt(repo: str | Path, *, model: Path, content: bytes) -> dict[str, Any]:
    receipt = {
        "schema_version": 1,
        "model_path": str(model.resolve()),
        "model_sha256": hashlib.sha256(content).hexdigest(),
        "read_at": datetime.now(timezone.utc).isoformat(),
    }
    target = receipt_path(repo)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return receipt


def read_and_record_model(repo: str | Path, *, skill_root: str | Path) -> tuple[bytes, dict[str, Any]]:
    model = scoring_model_path(skill_root)
    content = model.read_bytes()
    return content, _write_receipt(repo, model=model, content=content)


def record_model_read(repo: str | Path, *, skill_root: str | Path) -> dict[str, Any]:
    _, receipt = read_and_record_model(repo, skill_root=skill_root)
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


def _receipt_age_seconds(receipt: dict[str, Any]) -> float | None:
    try:
        read_at = datetime.fromisoformat(str(receipt.get("read_at", "")))
    except ValueError:
        return None
    if read_at.tzinfo is None:
        return None
    return max(0.0, (datetime.now(timezone.utc) - read_at.astimezone(timezone.utc)).total_seconds())


def score_guard_errors(repo: str | Path, *, skill_root: str | Path) -> list[str]:
    receipt = _load_receipt(repo)
    if receipt is None:
        return ["controller scoring blocked: scoring model read receipt is missing or unreadable"]
    age = _receipt_age_seconds(receipt)
    if age is None or age > RECEIPT_MAX_AGE_SECONDS:
        return ["controller scoring blocked: scoring model read receipt is expired for the current scoring action"]
    expected = scoring_model_sha256(skill_root)
    if receipt.get("model_sha256") != expected:
        return ["controller scoring blocked: scoring model read receipt is stale for the installed model"]
    if Path(str(receipt.get("model_path", ""))).resolve() != scoring_model_path(skill_root):
        return ["controller scoring blocked: scoring model read receipt points to a different model"]
    return []


def consume_score_guard(repo: str | Path, *, skill_root: str | Path) -> list[str]:
    errors = score_guard_errors(repo, skill_root=skill_root)
    if errors:
        return errors
    receipt_path(repo).unlink()
    return []


def main() -> int:
    parser = argparse.ArgumentParser(description="Record and verify the mandatory controller scoring model read.")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("record-read", "score-guard"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--repo", required=True)
    args = parser.parse_args()
    installed_skill_root = Path(__file__).resolve().parents[1]
    if args.command == "record-read":
        content, receipt = read_and_record_model(args.repo, skill_root=installed_skill_root)
        text = content.decode("utf-8")
        print(text, end="" if text.endswith("\n") else "\n")
        print("--- controller-scoring-model-read-receipt ---")
        print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
        return 0
    errors = consume_score_guard(args.repo, skill_root=installed_skill_root)
    if errors:
        for error in errors:
            print(error)
        return 2
    print("controller scoring: allowed once; exact installed scoring model read receipt was current and has been consumed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
