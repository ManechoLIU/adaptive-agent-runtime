#!/usr/bin/env python3
"""Canonical Git-backed state paths shared by all linked worktrees."""
from __future__ import annotations

import subprocess
from pathlib import Path


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def repository_root(repo: str | Path) -> Path:
    path = Path(repo).expanduser().resolve()
    return Path(_git(path, "rev-parse", "--show-toplevel")).resolve()


def git_common_dir(repo: str | Path) -> Path:
    root = repository_root(repo)
    raw = Path(_git(root, "rev-parse", "--git-common-dir"))
    if not raw.is_absolute():
        raw = root / raw
    return raw.resolve()


def adaptive_delivery_state_dir(repo: str | Path) -> Path:
    return git_common_dir(repo) / "adaptive-delivery"
