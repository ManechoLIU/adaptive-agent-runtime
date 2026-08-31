#!/usr/bin/env python3
"""Pure controller-health projection and evidence-gated wake decisions."""
from __future__ import annotations

from typing import Any


HEALTH_STATES = {"ACTIVE", "DEFERRED", "DEGRADED", "FALLBACK_NEEDED", "DEAD"}
WAKE_DECISIONS = {
    "NOOP_ACTIVE",
    "DEFER",
    "RESUME_CURRENT_HOST",
    "FALLBACK_PEER_HOST",
    "DEAD_BLOCK",
}
ELIGIBLE_FAILURES = {
    "usage_limit_exceeded",
    "quota_exhausted",
    "model_unavailable",
    "service_unavailable",
    "auth_invalid",
    "runtime_unavailable",
}

_ACTIONABLE_RESUME_STATES = {
    "RESUME_ACTIONABLE",
    "RESUME_AVAILABLE",
    "RESUME_READY",
    "RESUME_RETRYABLE",
}
_ACTIVE_RESUME_STATES = {"RESUME_ACTIVE", "RESUME_SUCCEEDED"}


def _single_binding(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    binding = value.strip()
    return binding or None


def _binding(facts: dict[str, Any]) -> dict[str, str] | None:
    registered_controller = _single_binding(facts.get("registered_controller"))
    canonical_common_dir = _single_binding(facts.get("canonical_common_dir"))
    controller_host = _single_binding(facts.get("controller_host"))
    if not registered_controller or not canonical_common_dir or not controller_host:
        return None
    return {
        "registered_controller": registered_controller,
        "canonical_common_dir": canonical_common_dir,
        "controller_host": controller_host,
    }


def _health(
    state: str,
    facts: dict[str, Any],
    binding: dict[str, str] | None,
    *,
    current_host_actionable: bool = False,
    no_safe_path: bool = False,
) -> dict[str, Any]:
    return {
        "state": state,
        "binding": binding,
        "controller_host": binding["controller_host"] if binding else None,
        "current_host_actionable": current_host_actionable,
        "no_safe_path": no_safe_path,
        "evidence": dict(facts),
    }


def _positive_no_safe_path(facts: dict[str, Any]) -> bool:
    if facts.get("unknown_side_effect") is True or facts.get("partial_write") is True:
        return False
    return (
        facts.get("fallback_eligible") is False
        or facts.get("peer_host_available") is False
        or facts.get("fallback_safe") is False
    )


def derive_controller_health(facts: dict[str, Any]) -> dict[str, Any]:
    """Project controller liveness from facts without mutating controller state."""
    if not isinstance(facts, dict):
        return _health("DEAD", {}, None)

    binding = _binding(facts)
    if binding is None:
        return _health("DEAD", facts, None)

    if facts.get("active_writer") is True:
        return _health("DEFERRED", facts, binding)

    resume_state = str(facts.get("resume_state", "")).strip().upper()
    if facts.get("controller_execution_active") is True or resume_state in _ACTIVE_RESUME_STATES:
        return _health("ACTIVE", facts, binding)

    if facts.get("pending_control_event") is not True:
        return _health("ACTIVE", facts, binding)

    if facts.get("resume_actionable") is True or resume_state in _ACTIONABLE_RESUME_STATES:
        return _health("DEGRADED", facts, binding, current_host_actionable=True)

    failed = resume_state == "RESUME_FAILED"
    failure_class = str(facts.get("failure_class", "")).strip().lower()
    peer_host = _single_binding(facts.get("peer_host"))
    peer_fallback_is_safe = (
        failed
        and facts.get("active_writer") is False
        and failure_class in ELIGIBLE_FAILURES
        and facts.get("fallback_eligible") is True
        and facts.get("peer_host_available") is True
        and facts.get("fallback_safe") is True
        and facts.get("unknown_side_effect") is not True
        and facts.get("partial_write") is not True
        and peer_host is not None
        and peer_host != binding["controller_host"]
    )
    if peer_fallback_is_safe:
        return _health("FALLBACK_NEEDED", facts, binding)

    no_safe_path = _positive_no_safe_path(facts)
    if (
        failed
        and facts.get("active_writer") is False
        and facts.get("failure_conclusive") is True
        and no_safe_path
    ):
        return _health("DEAD", facts, binding, no_safe_path=True)

    return _health("DEGRADED", facts, binding, no_safe_path=no_safe_path)


def _validated_projection(health: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(health, dict) or not isinstance(health.get("evidence"), dict):
        return None
    projection = derive_controller_health(health["evidence"])
    fields = ("state", "binding", "current_host_actionable", "no_safe_path", "evidence")
    if any(health.get(field) != projection[field] for field in fields):
        return None
    return projection


def decide_controller_wake(health: dict[str, Any]) -> dict[str, Any]:
    """Choose a non-replacing wake action from a validated health projection."""
    projection = _validated_projection(health)
    if projection is None:
        return {"decision": "DEAD_BLOCK", "selected_host": None}

    state = projection["state"]
    controller_host = projection["controller_host"]
    if state == "ACTIVE":
        return {"decision": "NOOP_ACTIVE", "selected_host": None}
    if state == "DEFERRED":
        return {"decision": "DEFER", "selected_host": None}
    if state == "FALLBACK_NEEDED":
        return {"decision": "FALLBACK_PEER_HOST", "selected_host": projection["evidence"]["peer_host"]}
    if state == "DEGRADED" and projection["current_host_actionable"] is True:
        return {"decision": "RESUME_CURRENT_HOST", "selected_host": controller_host}
    if state == "DEGRADED":
        return {"decision": "DEFER", "selected_host": None}
    return {"decision": "DEAD_BLOCK", "selected_host": None}
