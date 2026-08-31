#!/usr/bin/env python3
"""Pure controller-health projection and wake decisions."""
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


def _health(
    state: str,
    controller_host: str | None,
    *,
    current_host_actionable: bool = False,
) -> dict[str, Any]:
    return {
        "state": state,
        "controller_host": controller_host,
        "current_host_actionable": current_host_actionable,
    }


def derive_controller_health(facts: dict[str, Any]) -> dict[str, Any]:
    """Project controller liveness from facts without mutating controller state."""
    if not isinstance(facts, dict):
        return _health("DEAD", None)

    registered_controller = _single_binding(facts.get("registered_controller"))
    controller_host = _single_binding(facts.get("controller_host"))
    if not registered_controller or not controller_host:
        return _health("DEAD", controller_host)

    if facts.get("active_writer") is True:
        return _health("DEFERRED", controller_host)

    resume_state = str(facts.get("resume_state", "")).strip().upper()
    if facts.get("controller_execution_active") is True or resume_state in _ACTIVE_RESUME_STATES:
        return _health("ACTIVE", controller_host)

    if facts.get("pending_control_event") is not True:
        return _health("ACTIVE", controller_host)

    if facts.get("resume_actionable") is True or resume_state in _ACTIONABLE_RESUME_STATES:
        return _health("DEGRADED", controller_host, current_host_actionable=True)

    failed = resume_state == "RESUME_FAILED"
    failure_class = str(facts.get("failure_class", "")).strip().lower()
    peer_fallback_is_safe = (
        failed
        and failure_class in ELIGIBLE_FAILURES
        and facts.get("fallback_eligible") is True
        and facts.get("peer_host_available") is True
        and facts.get("fallback_safe") is True
        and facts.get("unknown_side_effect") is not True
        and facts.get("partial_write") is not True
    )
    if peer_fallback_is_safe:
        return _health("FALLBACK_NEEDED", controller_host)

    if failed and facts.get("failure_conclusive") is True:
        return _health("DEAD", controller_host)

    return _health("DEGRADED", controller_host)


def decide_controller_wake(health: dict[str, Any]) -> dict[str, Any]:
    """Choose a non-replacing wake action from a health projection."""
    state = health.get("state") if isinstance(health, dict) else None
    controller_host = health.get("controller_host") if isinstance(health, dict) else None
    if state == "ACTIVE":
        return {"decision": "NOOP_ACTIVE", "selected_host": None}
    if state == "DEFERRED":
        return {"decision": "DEFER", "selected_host": None}
    if state == "FALLBACK_NEEDED":
        return {"decision": "FALLBACK_PEER_HOST", "selected_host": "desktop_codex"}
    if state == "DEGRADED" and health.get("current_host_actionable") is True:
        return {"decision": "RESUME_CURRENT_HOST", "selected_host": controller_host}
    if state == "DEGRADED":
        return {"decision": "DEFER", "selected_host": None}
    return {"decision": "DEAD_BLOCK", "selected_host": None}
