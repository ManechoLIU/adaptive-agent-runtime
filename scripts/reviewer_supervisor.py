#!/usr/bin/env python3
from pathlib import Path
import json
import os
import subprocess
import tempfile


def git_common_state_root(repo: Path) -> Path:
    repo = Path(repo).resolve()
    raw = subprocess.check_output(
        ["git", "rev-parse", "--git-common-dir"], cwd=repo, text=True
    ).strip()
    common = Path(raw)
    if not common.is_absolute():
        common = (repo / common).resolve()
    return common / "adaptive-delivery" / "reviewer-runs"


def validate_verdict(payload: dict, expected_head: str) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("verdict payload must be an object")
    if payload.get("reviewed_head") != expected_head:
        raise ValueError("reviewed_head does not match candidate HEAD")
    if payload.get("verdict") not in {"PASS", "FINDINGS"}:
        raise ValueError("verdict must be PASS or FINDINGS")
    for field in ("critical", "important", "minor"):
        if not isinstance(payload.get(field), list):
            raise ValueError(f"{field} must be a list")
    return payload


def atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
