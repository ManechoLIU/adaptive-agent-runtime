#!/usr/bin/env python3
"""Read-only cross-project model evidence collection and global analytics."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

try:
    from scripts.project_model_score import load_project_samples
    from scripts.project_state import git_common_dir, repository_root
except ModuleNotFoundError:  # direct script execution from scripts/
    from project_model_score import load_project_samples
    from project_state import git_common_dir, repository_root


def _project_id(common_dir: Path) -> str:
    return hashlib.sha256(str(common_dir).encode("utf-8")).hexdigest()[:16]


def _candidate_rows(registry_path: Path, explicit_repos: Sequence[Path]) -> list[tuple[str, Path]]:
    rows: list[tuple[str, Path]] = []
    path = Path(registry_path).expanduser()
    if path.exists():
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("controller registry must contain a JSON object")
        for key, value in payload.items():
            if str(key).startswith("__") or not isinstance(value, str) or not value.strip():
                continue
            rows.append(("registry", Path(value).expanduser()))
    for repo in explicit_repos:
        rows.append(("explicit", Path(repo).expanduser()))
    return rows


def discover_repositories(
    registry_path: Path,
    explicit_repos: Sequence[Path],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Discover bounded Runtime repositories and deduplicate linked worktrees."""

    discovered: dict[str, dict[str, str]] = {}
    diagnostics: list[dict[str, str]] = []
    try:
        candidates = _candidate_rows(Path(registry_path), explicit_repos)
    except Exception as exc:
        return [], [{"repo": str(registry_path), "source": "registry", "error": str(exc)}]

    for source, candidate in candidates:
        try:
            root = repository_root(candidate)
            common = git_common_dir(root)
        except Exception as exc:
            diagnostics.append({"repo": str(candidate), "source": source, "error": str(exc)})
            continue
        key = str(common)
        if key in discovered:
            prior = discovered[key]
            sources = set(prior["source"].split("+")) | {source}
            prior["source"] = "+".join(item for item in ("registry", "explicit") if item in sources)
            continue
        discovered[key] = {
            "project_id": _project_id(common),
            "project_root": str(root),
            "project_common_dir": str(common),
            "project_name": root.name,
            "source": source,
        }

    repositories = sorted(discovered.values(), key=lambda item: (item["project_name"], item["project_root"]))
    diagnostics.sort(key=lambda item: (item["source"], item["repo"]))
    return repositories, diagnostics


def collect_global_samples(
    repositories: Sequence[dict[str, str]],
    *,
    window_days: int | None,
    now: datetime | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Load project-local normalized samples while preserving origin metadata."""

    samples: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    for repository in repositories:
        repo = Path(repository["project_root"])
        try:
            project_samples = load_project_samples(repo, window_days=window_days, now=now)
        except Exception as exc:
            diagnostics.append({
                "repo": str(repo),
                "project_id": repository.get("project_id"),
                "error": str(exc),
            })
            continue
        for sample in project_samples:
            enriched = dict(sample)
            enriched["identity"] = dict(sample.get("identity") or {})
            enriched["project_id"] = repository["project_id"]
            enriched["project_root"] = repository["project_root"]
            enriched["project_common_dir"] = repository["project_common_dir"]
            enriched["identity"]["project"] = repository["project_name"]
            samples.append(enriched)
    samples.sort(key=lambda item: (
        str(item.get("project_id") or ""),
        str(item.get("terminal_at") or ""),
        str(item.get("assignment_id") or ""),
    ))
    return samples, diagnostics
