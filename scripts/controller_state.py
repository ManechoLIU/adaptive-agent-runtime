#!/usr/bin/env python3
"""Pure controller-facing lifecycle projection for legacy task states."""
from __future__ import annotations

from typing import Any

MAIN_STATES = {"READY", "ACTIVE", "VERIFY", "BLOCKED", "CLOSED"}
LEGACY_STATES = {
    "PENDING",
    "READY",
    "ACTIVE",
    "RECOVERING",
    "VERIFY",
    "BLOCKED",
    "DONE",
    "SUPERSEDED",
}
VERIFICATION_GATES = {"review", "integration", "regression", "acceptance", "release"}


def project_task_state(
    legacy_state: str,
    *,
    runtime: dict[str, Any] | None = None,
    verification_gate: str | None = None,
    closure_reason: str | None = None,
    dispatchable: bool | None = None,
) -> dict[str, object]:
    state = str(legacy_state).strip().upper()
    if state not in LEGACY_STATES:
        raise ValueError(f"unsupported ledger state: {legacy_state}")

    if state == "PENDING":
        main_state = "READY"
        projected_dispatchable = False if dispatchable is None else dispatchable
        health = None
        projected_closure = None
    elif state == "READY":
        main_state = "READY"
        projected_dispatchable = True if dispatchable is None else dispatchable
        health = None
        projected_closure = None
    elif state in {"ACTIVE", "RECOVERING"}:
        main_state = "ACTIVE"
        projected_dispatchable = None
        health = "recovering" if state == "RECOVERING" else "normal"
        projected_closure = None
        runtime_state = str((runtime or {}).get("state", "")).strip().lower()
        if runtime_state == "progress_stale":
            health = "stale"
        elif runtime_state == "budget_exhausted":
            health = "budget_exhausted"
        elif runtime_state in {"unhealthy", "unknown", "terminal"}:
            health = "recovering"
    elif state == "VERIFY":
        main_state = "VERIFY"
        projected_dispatchable = None
        health = None
        projected_closure = None
    elif state == "BLOCKED":
        main_state = "BLOCKED"
        projected_dispatchable = None
        health = None
        projected_closure = None
    else:
        main_state = "CLOSED"
        projected_dispatchable = None
        health = None
        projected_closure = closure_reason or ("done" if state == "DONE" else "superseded")

    gate = verification_gate
    if gate is not None:
        gate = str(gate).strip().lower()
        if gate not in VERIFICATION_GATES:
            raise ValueError(f"unsupported verification gate: {verification_gate}")
    if main_state != "VERIFY":
        gate = None
    if main_state != "CLOSED":
        projected_closure = None

    return {
        "main_state": main_state,
        "dispatchable": projected_dispatchable,
        "health": health,
        "verification_gate": gate,
        "closure_reason": projected_closure,
    }
