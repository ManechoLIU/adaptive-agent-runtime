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
SCORE_HISTORY_FILE = "controller-score-history.jsonl"


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


def score_history_path(repo: str | Path) -> Path:
    return _git_common_dir(Path(repo).resolve()) / RECEIPT_DIRECTORY / SCORE_HISTORY_FILE


def append_score_history(repo: str | Path, record: dict[str, Any]) -> dict[str, Any]:
    target = score_history_path(repo)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(record)
    payload.setdefault("schema_version", 1)
    line = json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"
    with target.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
    return payload


def latest_score_history(repo: str | Path, *, controller_session_id: str) -> dict[str, Any] | None:
    target = score_history_path(repo)
    if not target.is_file():
        return None
    latest: dict[str, Any] | None = None
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        if str(value.get("controller_session_id", "")) != str(controller_session_id):
            continue
        latest = value
    return latest


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


def finalize_score(
    repo: str | Path,
    *,
    skill_root: str | Path,
    controller_session_id: str,
    turn_id: str,
    score: float,
    window_summary: str | None,
    message_sha256: str | None,
) -> dict[str, Any]:
    errors = consume_score_guard(repo, skill_root=skill_root)
    if errors:
        raise ValueError("score-guard failed: " + "; ".join(errors))
    record = {
        "schema_version": 1,
        "controller_session_id": str(controller_session_id),
        "turn_id": str(turn_id),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "score": float(score),
        "window_summary": window_summary,
        "model_sha256": scoring_model_sha256(skill_root),
        "message_sha256": message_sha256,
    }
    return append_score_history(repo, record)


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
    latest = sub.add_parser("latest-score")
    latest.add_argument("--repo", required=True)
    latest.add_argument("--controller-session", required=True)
    finalize = sub.add_parser("finalize-score")
    finalize.add_argument("--repo", required=True)
    finalize.add_argument("--controller-session", required=True)
    finalize.add_argument("--turn-id", default="")
    finalize.add_argument("--score", required=True, type=float)
    finalize.add_argument("--window-summary")
    finalize.add_argument("--message-sha256")
    args = parser.parse_args()
    installed_skill_root = Path(__file__).resolve().parents[1]
    if args.command == "record-read":
        content, receipt = read_and_record_model(args.repo, skill_root=installed_skill_root)
        text = content.decode("utf-8")
        print(text, end="" if text.endswith("\n") else "\n")
        print("--- controller-scoring-model-read-receipt ---")
        print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
        return 0
    if args.command == "latest-score":
        record = latest_score_history(args.repo, controller_session_id=args.controller_session)
        print("UNKNOWN" if record is None else json.dumps(record, ensure_ascii=False, sort_keys=True))
        return 0
    if args.command == "finalize-score":
        try:
            record = finalize_score(
                args.repo, skill_root=installed_skill_root, controller_session_id=args.controller_session,
                turn_id=args.turn_id, score=args.score, window_summary=args.window_summary,
                message_sha256=args.message_sha256,
            )
        except ValueError as error:
            print(f"controller scoring blocked: {error}")
            return 2
        print(json.dumps(record, ensure_ascii=False, sort_keys=True))
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
