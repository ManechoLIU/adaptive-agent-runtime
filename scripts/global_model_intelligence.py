#!/usr/bin/env python3
"""Read-only cross-project model evidence collection and global analytics."""

from __future__ import annotations

import copy
import hashlib
import json
import statistics
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

try:
    from scripts.project_model_score import (
        attach_benchmarks, classify_attribution, load_project_samples, score_model_groups, score_route_groups,
    )
    from scripts.project_state import git_common_dir, repository_root
except ModuleNotFoundError:  # direct script execution from scripts/
    from project_model_score import (
        attach_benchmarks, classify_attribution, load_project_samples, score_model_groups, score_route_groups,
    )
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

_GLOBAL_CONFIG_FIELDS = (
    "provider",
    "model",
    "auth_mode",
    "reasoning_effort",
    "execution_role",
    "policy_class",
    "execution_transport",
)


def _clone_with_project(sample: dict[str, Any], project: str) -> dict[str, Any]:
    cloned = copy.deepcopy(sample)
    cloned["identity"] = dict(cloned.get("identity") or {})
    cloned["identity"]["project"] = project
    return cloned


def _family_score(samples: Sequence[dict[str, Any]], model: str) -> dict[str, Any]:
    clones: list[dict[str, Any]] = []
    for sample in samples:
        cloned = copy.deepcopy(sample)
        identity = dict(cloned.get("identity") or {})
        identity.update({
            "project": "GLOBAL",
            "provider": "mixed",
            "model": model,
            "auth_mode": "mixed",
            "reasoning_effort": "mixed",
            "execution_role": "mixed",
            "policy_class": "mixed",
            "execution_transport": "mixed",
        })
        cloned["identity"] = identity
        clones.append(cloned)
    if not clones:
        return {
            "project_model_score": None,
            "dimension_scores": {},
            "dimension_coverage": 0.0,
            "quality_sample_count": 0,
            "confidence": "low",
        }
    return score_model_groups(clones)[0]


def _project_breakdown_for_model(samples: Sequence[dict[str, Any]], model: str) -> list[dict[str, Any]]:
    by_project: dict[str, list[dict[str, Any]]] = {}
    for sample in samples:
        identity = sample.get("identity") if isinstance(sample.get("identity"), dict) else {}
        if str(identity.get("model") or "") != model:
            continue
        by_project.setdefault(str(identity.get("project") or "unknown"), []).append(sample)
    rows: list[dict[str, Any]] = []
    for project, project_samples in sorted(by_project.items()):
        scored = _family_score(project_samples, model)
        counts = {name: 0 for name in ("model", "infrastructure", "external", "mixed", "unknown")}
        for sample in project_samples:
            counts[classify_attribution(sample)[0]] += 1
        rows.append({
            "project": project,
            "project_model_score": scored.get("project_model_score"),
            "quality_sample_count": counts["model"] + counts["mixed"],
            "total_sample_count": len(project_samples),
            "excluded_infrastructure_count": counts["infrastructure"],
            "excluded_external_count": counts["external"],
            "unknown_count": counts["unknown"],
        })
    return rows


def score_global_model_summaries(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Produce one presentation-level quality summary per canonical model.

    Quality evidence is capped per project before scoring so a single high-volume
    repository cannot silently dominate the cross-project headline.
    """

    models = sorted({
        str((sample.get("identity") or {}).get("model") or "")
        for sample in samples
        if isinstance(sample.get("identity"), dict) and (sample.get("identity") or {}).get("model")
    })
    results: list[dict[str, Any]] = []
    for model in models:
        model_samples = [sample for sample in samples if str((sample.get("identity") or {}).get("model") or "") == model]
        attribution_counts = {name: 0 for name in ("model", "infrastructure", "external", "mixed", "unknown")}
        quality_by_project: dict[str, list[dict[str, Any]]] = {}
        for sample in model_samples:
            attribution = classify_attribution(sample)[0]
            attribution_counts[attribution] += 1
            if attribution in {"model", "mixed"}:
                project = str((sample.get("identity") or {}).get("project") or "unknown")
                quality_by_project.setdefault(project, []).append(sample)

        non_zero_counts = [len(items) for items in quality_by_project.values() if items]
        project_cap = max(1, int(statistics.median(non_zero_counts))) if non_zero_counts else 0
        capped_quality: list[dict[str, Any]] = []
        for project in sorted(quality_by_project):
            ordered = sorted(
                quality_by_project[project],
                key=lambda item: (str(item.get("terminal_at") or ""), str(item.get("assignment_id") or "")),
            )
            capped_quality.extend(ordered[-project_cap:] if project_cap else [])

        scored = _family_score(capped_quality, model)
        results.append({
            "identity": {"model": model},
            "project_model_score": scored.get("project_model_score"),
            "dimension_scores": scored.get("dimension_scores", {}),
            "dimension_coverage": scored.get("dimension_coverage", 0.0),
            "quality_sample_count": len(capped_quality),
            "raw_quality_sample_count": attribution_counts["model"] + attribution_counts["mixed"],
            "total_sample_count": len(model_samples),
            "excluded_infrastructure_count": attribution_counts["infrastructure"],
            "excluded_external_count": attribution_counts["external"],
            "mixed_count": attribution_counts["mixed"],
            "unknown_count": attribution_counts["unknown"],
            "projects_observed": len({str((sample.get("identity") or {}).get("project") or "unknown") for sample in model_samples}),
            "project_contribution_cap": project_cap,
            "confidence": scored.get("confidence", "low"),
            "project_breakdown": _project_breakdown_for_model(model_samples, model),
        })
    return results


def _global_config_key(identity: dict[str, Any]) -> tuple[str, ...]:
    return tuple(str(identity.get(field) or "unknown") for field in _GLOBAL_CONFIG_FIELDS)


def score_global_configuration_groups(
    samples: list[dict[str, Any]],
    *,
    benchmarks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    clones = [_clone_with_project(sample, "GLOBAL") for sample in samples]
    groups = score_model_groups(clones)
    groups = attach_benchmarks(groups, benchmarks) if benchmarks else groups
    by_key_project: dict[tuple[str, ...], dict[str, list[dict[str, Any]]]] = {}
    for sample in samples:
        identity = sample.get("identity") if isinstance(sample.get("identity"), dict) else {}
        key = _global_config_key(identity)
        project = str(identity.get("project") or "unknown")
        by_key_project.setdefault(key, {}).setdefault(project, []).append(sample)

    for group in groups:
        identity = group.get("identity") if isinstance(group.get("identity"), dict) else {}
        key = _global_config_key(identity)
        breakdown: list[dict[str, Any]] = []
        for project, project_samples in sorted(by_key_project.get(key, {}).items()):
            project_group = score_model_groups([copy.deepcopy(sample) for sample in project_samples])[0]
            breakdown.append({
                "project": project,
                "project_model_score": project_group.get("project_model_score"),
                "quality_sample_count": project_group.get("quality_sample_count", 0),
                "total_sample_count": project_group.get("total_sample_count", 0),
            })
        group["projects_observed"] = len(breakdown)
        group["project_breakdown"] = breakdown
    return groups


def _project_report_from_samples(project: str, project_samples: list[dict[str, Any]], benchmarks: list[dict[str, Any]]) -> dict[str, Any]:
    model_groups = score_model_groups([copy.deepcopy(sample) for sample in project_samples])
    model_groups = attach_benchmarks(model_groups, benchmarks) if benchmarks else model_groups
    route_groups = score_route_groups([copy.deepcopy(sample) for sample in project_samples])
    attribution_counts = {name: 0 for name in ("model", "infrastructure", "external", "mixed", "unknown")}
    for sample in project_samples:
        attribution_counts[classify_attribution(sample)[0]] += 1
    return {
        "scope": "project",
        "project": project,
        "summary": {
            "terminal_samples": len(project_samples),
            "quality_scored_samples": attribution_counts["model"] + attribution_counts["mixed"],
            "observed_model_configurations": len(model_groups),
            "infrastructure_failures_excluded_from_model_score": attribution_counts["infrastructure"],
            "external_failures_excluded_from_model_score": attribution_counts["external"],
            "unknown_samples": attribution_counts["unknown"],
        },
        "model_groups": model_groups,
        "route_groups": route_groups,
    }


def build_global_report_from_samples(
    repositories: Sequence[dict[str, str]],
    samples: list[dict[str, Any]],
    *,
    window_days: int | None,
    benchmarks: list[dict[str, Any]],
    diagnostics: list[dict[str, Any]],
) -> dict[str, Any]:
    model_summaries = score_global_model_summaries(samples)
    configuration_groups = score_global_configuration_groups(samples, benchmarks=benchmarks)
    route_groups = score_route_groups([copy.deepcopy(sample) for sample in samples])
    project_reports: dict[str, dict[str, Any]] = {}
    by_project: dict[str, list[dict[str, Any]]] = {}
    attribution_counts = {name: 0 for name in ("model", "infrastructure", "external", "mixed", "unknown")}
    for sample in samples:
        project = str((sample.get("identity") or {}).get("project") or "unknown")
        by_project.setdefault(project, []).append(sample)
        attribution_counts[classify_attribution(sample)[0]] += 1
    for project, project_samples in sorted(by_project.items()):
        project_reports[project] = _project_report_from_samples(project, project_samples, benchmarks)
    return {
        "schema_version": 1,
        "scope": "global",
        "window_days": window_days,
        "projects": list(repositories),
        "summary": {
            "projects_observed": len(repositories),
            "terminal_samples": len(samples),
            "quality_scored_samples": attribution_counts["model"] + attribution_counts["mixed"],
            "models_observed": len(model_summaries),
            "infrastructure_failures_excluded_from_model_score": attribution_counts["infrastructure"],
            "external_failures_excluded_from_model_score": attribution_counts["external"],
            "unknown_samples": attribution_counts["unknown"],
        },
        "model_summaries": model_summaries,
        "configuration_groups": configuration_groups,
        "route_groups": route_groups,
        "project_reports": project_reports,
        "diagnostics": diagnostics,
    }


def build_global_report(
    repositories: Sequence[dict[str, str]],
    *,
    window_days: int | None,
    benchmarks: list[dict[str, Any]],
    now: datetime | None = None,
    diagnostics: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    samples, sample_diagnostics = collect_global_samples(repositories, window_days=window_days, now=now)
    return build_global_report_from_samples(
        repositories,
        samples,
        window_days=window_days,
        benchmarks=benchmarks,
        diagnostics=[*(diagnostics or []), *sample_diagnostics],
    )
