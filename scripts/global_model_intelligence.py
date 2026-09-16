#!/usr/bin/env python3
"""Read-only cross-project model evidence collection and global analytics."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import tempfile
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

try:
    from scripts.project_model_score import (
        attach_benchmarks, build_decisions, classify_attribution, load_benchmarks, load_project_samples, score_model_groups, score_route_groups, _MODEL_DIMENSION_WEIGHTS, _sample_dimensions,
    )
    from scripts.project_state import git_common_dir, repository_root
except ModuleNotFoundError:  # direct script execution from scripts/
    from project_model_score import (
        attach_benchmarks, build_decisions, classify_attribution, load_benchmarks, load_project_samples, score_model_groups, score_route_groups, _MODEL_DIMENSION_WEIGHTS, _sample_dimensions,
    )
    from project_state import git_common_dir, repository_root

try:
    from scripts.provider_health import derive_route_health
except ModuleNotFoundError:  # direct script execution from scripts/
    from provider_health import derive_route_health


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
    """Score a model family from raw samples without erasing role semantics."""
    dimension_values: dict[str, list[tuple[float, float]]] = {name: [] for name in _MODEL_DIMENSION_WEIGHTS}
    quality_sample_count = 0
    for sample in samples:
        attribution, _ = classify_attribution(sample)
        if attribution not in {"model", "mixed"}:
            continue
        quality_sample_count += 1
        sample_weight = 0.5 if attribution == "mixed" else 1.0
        for dimension, value in _sample_dimensions(sample, attribution).items():
            if value is not None:
                dimension_values[dimension].append((float(value), sample_weight))

    dimensions: dict[str, float | None] = {}
    for dimension, values in dimension_values.items():
        if not values:
            dimensions[dimension] = None
            continue
        total_weight = sum(weight for _, weight in values)
        dimensions[dimension] = round(sum(value * weight for value, weight in values) / total_weight, 1)

    available = [(name, value) for name, value in dimensions.items() if value is not None]
    available_weight = sum(_MODEL_DIMENSION_WEIGHTS[name] for name, _ in available)
    score = None
    if available and available_weight:
        score = round(sum(float(value) * _MODEL_DIMENSION_WEIGHTS[name] for name, value in available) / available_weight, 1)
    coverage = round(available_weight / sum(_MODEL_DIMENSION_WEIGHTS.values()), 2)
    if quality_sample_count >= 5 and coverage >= 0.5:
        confidence = "high"
    elif quality_sample_count >= 3:
        confidence = "medium"
    else:
        confidence = "low"
    return {
        "identity": {"model": model},
        "project_model_score": score,
        "dimension_scores": dimensions,
        "dimension_coverage": coverage,
        "quality_sample_count": quality_sample_count,
        "confidence": confidence,
    }


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
    decisions = build_decisions(model_groups, route_groups, benchmarks or [])
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
        "decisions": decisions,
    }


def build_global_report_from_samples(
    repositories: Sequence[dict[str, str]],
    samples: list[dict[str, Any]],
    *,
    window_days: int | None,
    benchmarks: list[dict[str, Any]],
    diagnostics: list[dict[str, Any]],
    now: datetime | None = None,
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
    report = {
        "schema_version": 1,
        "scope": "global",
        "window_days": window_days,
        "generated_at": (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(),
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
    return apply_global_recommendations(report, samples, benchmarks=benchmarks, now=now)


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
        now=now,
    )

_ROUTE_STATE_LABELS = {
    "HEALTHY": "稳定",
    "DEGRADED": "执行链需优化",
    "OPEN": "执行链已熔断",
    "PROBE_REQUIRED": "修复后待复测",
}
_ROUTE_STATE_PRIORITY = {"HEALTHY": 0, "DEGRADED": 1, "PROBE_REQUIRED": 2, "OPEN": 3}
_ACTION_PRIORITY = {
    "KEEP": 0,
    "INSUFFICIENT_EVIDENCE": 1,
    "TUNE_EFFORT": 2,
    "CHANGE_ROLE": 3,
    "SWITCH_MODEL": 4,
    "CHANGE_ROUTE": 5,
}


def _route_tuple(mapping: dict[str, Any]) -> tuple[str, str, str, str]:
    return tuple(str(mapping.get(field) or "unknown") for field in ("provider", "model", "auth_mode", "execution_transport"))


def _preferred_roles(configuration_groups: list[dict[str, Any]], model: str) -> list[str]:
    role_scores: dict[str, tuple[float, int]] = {}
    for group in configuration_groups:
        identity = group.get("identity") if isinstance(group.get("identity"), dict) else {}
        if str(identity.get("model") or "") != model:
            continue
        score = group.get("project_model_score")
        count = int(group.get("quality_sample_count") or 0)
        role = str(identity.get("execution_role") or "unknown")
        if score is None or count < 3 or role == "unknown":
            continue
        current = role_scores.get(role)
        candidate = (float(score), count)
        if current is None or candidate > current:
            role_scores[role] = candidate
    if not role_scores:
        return []
    best = max(score for score, _ in role_scores.values())
    return [
        role for role, (score, _) in sorted(role_scores.items(), key=lambda item: (-item[1][0], -item[1][1], item[0]))
        if score >= best - 5
    ][:2]


def apply_global_recommendations(
    report: dict[str, Any],
    samples: list[dict[str, Any]],
    *,
    benchmarks: list[dict[str, Any]],
    now: datetime | None = None,
) -> dict[str, Any]:
    configuration_groups = report.get("configuration_groups", [])
    route_groups = report.get("route_groups", [])
    decisions = build_decisions(configuration_groups, route_groups, benchmarks or [])
    route_health = derive_route_health([copy.deepcopy(sample) for sample in samples], now=now)
    health_by_route = {_route_tuple(row.get("route") or {}): row for row in route_health}

    for decision in decisions:
        identity = decision.get("identity") if isinstance(decision.get("identity"), dict) else {}
        health = health_by_route.get(_route_tuple(identity))
        if health:
            decision["route_health_state"] = health.get("state")
            decision["route_status"] = _ROUTE_STATE_LABELS.get(str(health.get("state")), "待确认")

    for summary in report.get("model_summaries", []):
        model = str((summary.get("identity") or {}).get("model") or "")
        model_decisions = [decision for decision in decisions if str((decision.get("identity") or {}).get("model") or "") == model]
        model_health = [row for row in route_health if str((row.get("route") or {}).get("model") or "") == model]
        worst_health = max(model_health, key=lambda row: _ROUTE_STATE_PRIORITY.get(str(row.get("state")), -1), default=None)
        state = str(worst_health.get("state")) if worst_health else "HEALTHY"
        summary["route_health_state"] = state
        summary["route_status"] = _ROUTE_STATE_LABELS.get(state, "待确认")
        summary["preferred_roles"] = _preferred_roles(configuration_groups, model)

        quality_count = int(summary.get("quality_sample_count") or 0)
        score = summary.get("project_model_score")
        suggested_target = None
        if quality_count < 3 or score is None:
            action = "INSUFFICIENT_EVIDENCE"
            reason = "跨项目有效质量样本不足，继续积累后再判断是否换模。"
        else:
            actionable = [decision for decision in model_decisions if decision.get("action") != "INSUFFICIENT_EVIDENCE"]
            if actionable:
                selected = max(actionable, key=lambda item: _ACTION_PRIORITY.get(str(item.get("action")), -1))
                action = str(selected.get("action") or "KEEP")
                suggested_target = selected.get("suggested_target")
            else:
                action = "KEEP"
            if action == "CHANGE_ROUTE":
                reason = "模型质量有可用证据，但执行链可靠性拖后腿，优先修复或复测 route。"
            elif action == "CHANGE_ROLE":
                reason = "同一模型在其他角色的真实项目表现明显更强，建议调整岗位而不是直接换模。"
            elif action == "TUNE_EFFORT":
                reason = "较低 effort 在相近质量下更高效，建议调整推理档位。"
            elif action == "SWITCH_MODEL":
                reason = "当前 route 健康且同类任务中存在稳定更强的实战替代模型，可考虑换模。"
            else:
                reason = "跨项目实战证据支持继续使用当前模型。"
        summary["recommended_action"] = action
        summary["recommended_reason"] = reason
        summary["suggested_target"] = suggested_target

    report["decisions"] = decisions
    report["route_health"] = route_health
    return report

DEFAULT_REGISTRY = Path.home() / ".codex" / "adaptive-delivery-controllers.json"


def _atomic_write_text(path: Path, content: str) -> None:
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def cache_global_report(report: dict[str, Any], *, cache_dir: Path | None = None) -> dict[str, str]:
    root = (cache_dir or (Path.home() / ".codex" / "adaptive-delivery" / "model-intelligence")).expanduser().resolve()
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    latest = root / "latest.json"
    _atomic_write_text(latest, rendered)
    generated = str(report.get("generated_at") or datetime.now(timezone.utc).isoformat())
    try:
        stamp = datetime.fromisoformat(generated.replace("Z", "+00:00")).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    except ValueError:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    snapshot = root / "snapshots" / f"{stamp}.json"
    _atomic_write_text(snapshot, rendered)
    return {"latest": str(latest), "snapshot": str(snapshot)}


def _parse_now(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _build_cli_report(args: argparse.Namespace) -> dict[str, Any]:
    repositories, discovery_diagnostics = discover_repositories(
        Path(args.registry).expanduser(),
        [Path(value).expanduser() for value in (args.repo or [])],
    )
    benchmarks = load_benchmarks(Path(args.benchmark_json)) if args.benchmark_json else []
    return build_global_report(
        repositories,
        window_days=args.window_days,
        benchmarks=benchmarks,
        now=_parse_now(args.now),
        diagnostics=discovery_diagnostics,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Aggregate model performance across Runtime-enabled projects.")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("report", "Build a machine-readable global model intelligence report"),
        ("dashboard", "Render the global/project model intelligence dashboard"),
    ):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("--registry", default=str(DEFAULT_REGISTRY))
        command.add_argument("--repo", action="append", default=[])
        command.add_argument("--window-days", type=int, default=30)
        command.add_argument("--benchmark-json")
        command.add_argument("--now", help=argparse.SUPPRESS)
        if name == "report":
            command.add_argument("--json", dest="json_output")
        else:
            command.add_argument("--output", required=True)

    args = parser.parse_args(argv)
    report = _build_cli_report(args)
    cache_global_report(report)
    if args.command == "report":
        rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if args.json_output:
            _atomic_write_text(Path(args.json_output), rendered)
        else:
            print(rendered, end="")
        return 0

    try:
        from scripts.model_score_dashboard import render_dashboard
    except ModuleNotFoundError:  # direct script execution from scripts/
        from model_score_dashboard import render_dashboard
    _atomic_write_text(Path(args.output), render_dashboard(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
