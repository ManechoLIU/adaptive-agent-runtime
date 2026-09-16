"""Read-only project model evidence normalization and scoring surfaces.

This module deliberately consumes canonical Runtime evidence instead of creating
another project state store. Higher-level scoring and dashboard functions are
added incrementally behind tests.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from scripts.assignment_runtime import load_runtime_state

UTC = timezone.utc
_REASONING_EFFORTS = {"low", "medium", "high", "xhigh", "max"}
_REASONING_RE = re.compile(r"(?:^|[;,:\s])reasoning_effort\s*=\s*(low|medium|high|xhigh|max)(?:$|[;,:\s])", re.I)


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse_reasoning_effort(lease: dict[str, Any]) -> str:
    direct = str(lease.get("reasoning_effort") or "").strip().lower()
    if direct in _REASONING_EFFORTS:
        return direct
    strategy = str(lease.get("strategy") or "")
    match = _REASONING_RE.search(strategy)
    return match.group(1).lower() if match else "unknown"


def _route_value(lease: dict[str, Any], field: str) -> str:
    value = lease.get(field)
    if isinstance(value, str) and value.strip():
        return value.strip()
    route = lease.get("route_contract")
    if isinstance(route, dict):
        value = route.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "unknown"


def normalize_lease_sample(project: str, assignment_id: str, lease: dict[str, Any]) -> dict[str, Any] | None:
    terminal_at = _parse_datetime(lease.get("terminal_at"))
    terminal_state = str(lease.get("terminal_state") or "").strip().lower()
    if terminal_at is None or not terminal_state:
        return None

    model = _route_value(lease, "model")
    if model == "unknown":
        return None

    started_at = _parse_datetime(lease.get("started_at"))
    elapsed_seconds = None
    if started_at is not None and terminal_at >= started_at:
        elapsed_seconds = (terminal_at - started_at).total_seconds()

    identity = {
        "project": str(project),
        "provider": _route_value(lease, "provider"),
        "model": model,
        "auth_mode": _route_value(lease, "auth_mode"),
        "reasoning_effort": parse_reasoning_effort(lease),
        "execution_role": str(lease.get("execution_role") or "unknown").strip() or "unknown",
        "policy_class": str(lease.get("policy_class") or "unknown").strip() or "unknown",
        "execution_transport": str(lease.get("execution_transport") or "unknown").strip() or "unknown",
    }

    return {
        "assignment_id": str(lease.get("assignment_id") or assignment_id),
        "task_id": str(lease.get("task_id") or ""),
        "identity": identity,
        "started_at": started_at.isoformat() if started_at else None,
        "terminal_at": terminal_at.isoformat(),
        "elapsed_seconds": elapsed_seconds,
        "terminal_state": terminal_state,
        "transport_outcome": str(lease.get("transport_outcome") or "unknown").strip().lower(),
        "delivery_outcome": str(lease.get("delivery_outcome") or "unknown").strip().lower(),
        "failure_class": str(lease.get("failure_class") or "").strip().lower() or None,
        "outcome_code": str(lease.get("outcome_code") or "").strip().upper() or None,
        "retry_class": str(lease.get("retry_class") or "").strip().lower() or None,
        "retry_safe": lease.get("retry_safe") if isinstance(lease.get("retry_safe"), bool) else None,
        "result_unknown": bool(lease.get("result_unknown")),
        "recovery_count": int(lease.get("recovery_count") or 0),
        "candidate_revision": lease.get("candidate_revision"),
        "review_verdict": lease.get("review_verdict") if isinstance(lease.get("review_verdict"), dict) else None,
        "evidence": list(lease.get("evidence") or []),
        "artifacts": list(lease.get("artifacts") or []),
        "summary": str(lease.get("summary") or ""),
    }


def load_project_samples(
    repo: Path | str,
    *,
    window_days: int | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    repo_path = Path(repo).resolve()
    state = load_runtime_state(repo_path)
    leases = state.get("leases") if isinstance(state, dict) else None
    if not isinstance(leases, dict):
        return []

    cutoff = None
    if window_days is not None:
        if window_days < 1:
            raise ValueError("window_days must be >= 1")
        current = now or datetime.now(tz=UTC)
        if current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        cutoff = current.astimezone(UTC) - timedelta(days=window_days)

    samples: list[dict[str, Any]] = []
    for assignment_id, lease in leases.items():
        if not isinstance(lease, dict):
            continue
        sample = normalize_lease_sample(repo_path.name, str(assignment_id), lease)
        if sample is None:
            continue
        if cutoff is not None:
            terminal_at = _parse_datetime(sample["terminal_at"])
            if terminal_at is None or terminal_at < cutoff:
                continue
        samples.append(sample)

    samples.sort(key=lambda item: (item["terminal_at"], item["assignment_id"]))
    return samples

_MODEL_DIMENSION_WEIGHTS = {
    "delivery_success": 35.0,
    "first_pass_quality": 25.0,
    "verification_strength": 20.0,
    "efficiency": 10.0,
    "recovery_rework": 10.0,
}

_EXTERNAL_FAILURE_MARKERS = (
    "quota",
    "usage_limit",
    "rate_limit",
    "service_unavailable",
    "provider_unavailable",
    "account_disabled",
    "credential_revoked",
    "auth_invalid",
    "authentication_failed",
)

_INFRA_FAILURE_MARKERS = (
    "provider_timeout",
    "cli_launch_timeout",
    "first_output_timeout",
    "generation_stalled",
    "heartbeat_timeout",
    "process_group_cleanup_failed",
    "review_process_stuck",
    "review_timeout",
    "review_no_verdict",
    "review_output_invalid",
    "tool_unavailable",
    "runtime_unavailable",
    "bridge_unavailable",
    "host_unavailable",
    "sandbox",
    "transport_failure",
    "local_precheck_failed",
    "no_valid_result",
    "receipt",
)

_MODEL_FAILURE_MARKERS = (
    "boundary_violation",
    "semantic_failure",
    "verification_failed",
    "model_contradicted_by_local_evidence",
)


def classify_attribution(sample: dict[str, Any]) -> tuple[str, list[str]]:
    """Classify one terminal sample before any scoring is applied.

    Structured terminal fields dominate prose.  The classifier is deliberately
    conservative: ambiguous/caller cancellations remain ``unknown`` instead of
    being charged to the model.
    """

    failure = str(sample.get("failure_class") or "").strip().lower()
    outcome = str(sample.get("outcome_code") or "").strip().lower()
    retry = str(sample.get("retry_class") or "").strip().lower()
    delivery = str(sample.get("delivery_outcome") or "").strip().lower()
    transport = str(sample.get("transport_outcome") or "").strip().lower()
    terminal = str(sample.get("terminal_state") or "").strip().lower()
    combined = " ".join(part for part in (failure, outcome, retry) if part)

    if bool(sample.get("result_unknown")):
        return "infrastructure", ["result_unknown"]

    if retry == "cancelled_by_controller" or failure == "parent_cancelled":
        return "unknown", ["controller_cancelled"]

    external_reasons = [marker for marker in _EXTERNAL_FAILURE_MARKERS if marker in combined]
    infra_reasons = [marker for marker in _INFRA_FAILURE_MARKERS if marker in combined]
    model_reasons = [marker for marker in _MODEL_FAILURE_MARKERS if marker in combined]

    semantic_fail = delivery == "fail" or bool(model_reasons)
    if semantic_fail and (external_reasons or infra_reasons):
        return "mixed", ["semantic_delivery_fail", *model_reasons, *external_reasons, *infra_reasons]
    if external_reasons:
        return "external", external_reasons
    if infra_reasons:
        return "infrastructure", infra_reasons
    if semantic_fail:
        reasons = ["semantic_delivery_fail"] if delivery == "fail" else []
        return "model", [*reasons, *model_reasons]

    if delivery == "pass" and transport == "completed" and terminal == "completed":
        return "model", ["validated_delivery_pass"]

    if transport in {"failed", "blocked"}:
        return "infrastructure", [f"transport_{transport}"]

    return "unknown", ["insufficient_structured_evidence"]


def _traceable_locator(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    scheme, sep, locator = value.partition(":")
    return bool(sep and scheme in {"git", "test-log", "artifact", "receipt", "file", "green-test"} and locator.strip())


def _verification_strength(sample: dict[str, Any]) -> float | None:
    evidence = [value for value in sample.get("evidence", []) if _traceable_locator(value)]
    artifacts = [value for value in sample.get("artifacts", []) if _traceable_locator(value)]
    has_test = any(str(value).startswith(("test-log:", "green-test:")) for value in evidence)
    if has_test and artifacts:
        return 100.0
    if has_test:
        return 85.0
    if artifacts and evidence:
        return 75.0
    if artifacts:
        return 65.0
    if evidence:
        return 50.0
    return 0.0


def _first_pass_quality(sample: dict[str, Any]) -> float | None:
    if str(sample.get("identity", {}).get("execution_role") or "") == "reviewer":
        return None
    reviews = sample.get("candidate_reviews")
    if not isinstance(reviews, list) or not reviews:
        return None
    values: list[float] = []
    for review in reviews:
        if not isinstance(review, dict):
            continue
        verdict = str(review.get("verdict") or "").strip().upper()
        critical = int(review.get("critical") or 0)
        important = int(review.get("important") or 0)
        if verdict == "PASS" and critical == 0 and important == 0:
            values.append(100.0)
        elif critical > 0:
            values.append(0.0)
        elif important > 0 or verdict in {"FAIL", "FINDINGS"}:
            values.append(25.0)
    return sum(values) / len(values) if values else None


def _sample_dimensions(sample: dict[str, Any], attribution: str) -> dict[str, float | None]:
    if attribution not in {"model", "mixed"}:
        return {key: None for key in _MODEL_DIMENSION_WEIGHTS}

    delivery = str(sample.get("delivery_outcome") or "").lower()
    delivery_score = {"pass": 100.0, "fail": 0.0, "blocked": 25.0, "unresolved": 0.0}.get(delivery)
    recovery_score = None
    failure = str(sample.get("failure_class") or "").lower()
    if any(marker in failure for marker in _MODEL_FAILURE_MARKERS):
        recovery_score = 0.0 if int(sample.get("recovery_count") or 0) > 0 else 25.0

    return {
        "delivery_success": delivery_score,
        "first_pass_quality": _first_pass_quality(sample),
        "verification_strength": _verification_strength(sample),
        "efficiency": None,
        "recovery_rework": recovery_score,
    }


def _model_group_key(sample: dict[str, Any]) -> tuple[str, ...]:
    identity = sample["identity"]
    return tuple(
        str(identity.get(field) or "unknown")
        for field in (
            "project",
            "provider",
            "model",
            "auth_mode",
            "reasoning_effort",
            "execution_role",
            "policy_class",
            "execution_transport",
        )
    )


def _identity_from_key(key: tuple[str, ...]) -> dict[str, str]:
    fields = (
        "project",
        "provider",
        "model",
        "auth_mode",
        "reasoning_effort",
        "execution_role",
        "policy_class",
        "execution_transport",
    )
    return dict(zip(fields, key))


def score_model_groups(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for sample in samples:
        grouped.setdefault(_model_group_key(sample), []).append(sample)

    results: list[dict[str, Any]] = []
    for key, group_samples in grouped.items():
        attribution_counts = {name: 0 for name in ("model", "infrastructure", "external", "mixed", "unknown")}
        dimension_values: dict[str, list[tuple[float, float]]] = {name: [] for name in _MODEL_DIMENSION_WEIGHTS}
        quality_sample_count = 0
        for sample in group_samples:
            attribution, reasons = classify_attribution(sample)
            sample["attribution"] = attribution
            sample["attribution_reasons"] = reasons
            attribution_counts[attribution] += 1
            if attribution in {"model", "mixed"}:
                quality_sample_count += 1
                sample_weight = 0.5 if attribution == "mixed" else 1.0
                for dimension, value in _sample_dimensions(sample, attribution).items():
                    if value is not None:
                        dimension_values[dimension].append((value, sample_weight))

        dimensions: dict[str, float | None] = {}
        for dimension, values in dimension_values.items():
            if not values:
                dimensions[dimension] = None
                continue
            weighted_sum = sum(value * weight for value, weight in values)
            weight_sum = sum(weight for _, weight in values)
            dimensions[dimension] = round(weighted_sum / weight_sum, 1)

        available = [(name, value) for name, value in dimensions.items() if value is not None]
        total_dimension_weight = sum(_MODEL_DIMENSION_WEIGHTS[name] for name, _ in available)
        score = None
        if available and total_dimension_weight:
            score = round(
                sum(value * _MODEL_DIMENSION_WEIGHTS[name] for name, value in available) / total_dimension_weight,
                1,
            )
        coverage = round(total_dimension_weight / sum(_MODEL_DIMENSION_WEIGHTS.values()), 2)
        if quality_sample_count >= 5 and coverage >= 0.5:
            confidence = "high"
        elif quality_sample_count >= 3:
            confidence = "medium"
        else:
            confidence = "low"

        results.append(
            {
                "identity": _identity_from_key(key),
                "project_model_score": score,
                "dimension_scores": dimensions,
                "dimension_coverage": coverage,
                "quality_sample_count": quality_sample_count,
                "total_sample_count": len(group_samples),
                "excluded_infrastructure_count": attribution_counts["infrastructure"],
                "excluded_external_count": attribution_counts["external"],
                "mixed_count": attribution_counts["mixed"],
                "unknown_count": attribution_counts["unknown"],
                "confidence": confidence,
            }
        )

    results.sort(
        key=lambda item: (
            item["identity"]["model"],
            item["identity"]["reasoning_effort"],
            item["identity"]["execution_role"],
            item["identity"]["policy_class"],
        )
    )
    return results


def _route_group_key(sample: dict[str, Any]) -> tuple[str, str, str, str]:
    identity = sample["identity"]
    return (
        str(identity.get("provider") or "unknown"),
        str(identity.get("model") or "unknown"),
        str(identity.get("auth_mode") or "unknown"),
        str(identity.get("execution_transport") or "unknown"),
    )


def score_route_groups(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    for sample in samples:
        grouped.setdefault(_route_group_key(sample), []).append(sample)

    results: list[dict[str, Any]] = []
    for key, group_samples in grouped.items():
        eligible = 0
        successes = 0
        failures = 0
        unknown = 0
        result_unknown_count = 0
        failure_classes: dict[str, int] = {}
        for sample in group_samples:
            attribution, _ = classify_attribution(sample)
            if sample.get("result_unknown"):
                result_unknown_count += 1
            if attribution == "unknown":
                unknown += 1
                continue
            eligible += 1
            if attribution == "model" and sample.get("transport_outcome") == "completed" and not sample.get("result_unknown"):
                successes += 1
            else:
                failures += 1
                failure = str(sample.get("failure_class") or attribution)
                failure_classes[failure] = failure_classes.get(failure, 0) + 1
        score = round(100.0 * successes / eligible, 1) if eligible else None
        provider, model, auth_mode, execution_transport = key
        results.append(
            {
                "route": {
                    "provider": provider,
                    "model": model,
                    "auth_mode": auth_mode,
                    "execution_transport": execution_transport,
                },
                "route_reliability_score": score,
                "eligible_attempts": eligible,
                "successful_transport_attempts": successes,
                "failed_transport_attempts": failures,
                "unknown_attempts": unknown,
                "result_unknown_count": result_unknown_count,
                "failure_classes": dict(sorted(failure_classes.items())),
            }
        )
    results.sort(key=lambda item: tuple(item["route"].values()))
    return results
