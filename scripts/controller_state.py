#!/usr/bin/env python3
"""Pure controller-facing lifecycle projection for legacy task states."""
from __future__ import annotations

import re

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
CLOSED_LEGACY_STATES = {"DONE", "SUPERSEDED"}
EXTERNAL_GATE_RE = re.compile(
    r"(?:授权|凭证|付费|真机|真实\s*(?:AppID|环境|服务|邮箱|模型)|等待\s*(?:用户|机主|外部|credential|appid|domain)|credential|appid|api\s*key|domain|production\s+(?:environment|credential)|external\s+(?:condition|authorization))",
    re.IGNORECASE,
)
DEPENDENCY_GATE_RE = re.compile(
    r"(?:依赖|depends?\s+on|dependency|仅在.+?(?:后|完成|集成)|等待.+?(?:完成|集成)|\bafter\b)",
    re.IGNORECASE,
)
SHORTHAND_TASK_REF_RE = re.compile(r"(?<![A-Za-z0-9_-])F\d+(?:-[A-Z0-9]+)+(?![A-Za-z0-9_-])", re.IGNORECASE)


def _mentioned_task_ids(text: str, declared_ids: set[str], self_id: str) -> set[str]:
    mentions: set[str] = set()
    lowered = text.casefold()
    for candidate in sorted(declared_ids, key=len, reverse=True):
        if candidate == self_id:
            continue
        pattern = rf"(?<![A-Za-z0-9_-]){re.escape(candidate)}(?![A-Za-z0-9_-])"
        if re.search(pattern, text, re.IGNORECASE):
            mentions.add(candidate)
        elif len(candidate) == 1 and candidate.casefold() in lowered.split():
            mentions.add(candidate)
    return mentions


def derive_runnable_tasks(records: list[dict[str, str]]) -> dict[str, object]:
    """Derive dispatchable work from the canonical task rows only.

    READY is always runnable. PENDING becomes runnable only when every task ID it
    references is closed and the row does not declare an external/environment
    gate. The result is an ephemeral projection, never a second task state.
    """
    status_by_id = {str(r.get("id", "")).strip(): str(r.get("status", "")).strip().upper() for r in records if str(r.get("id", "")).strip()}
    declared_ids = set(status_by_id)
    runnable: list[str] = []
    exclusions: dict[str, list[str]] = {}
    for record in records:
        task_id = str(record.get("id", "")).strip()
        status = str(record.get("status", "")).strip().upper()
        if not task_id:
            continue
        if status == "READY":
            runnable.append(task_id)
            continue
        if status != "PENDING":
            continue
        text = str(record.get("next_action", ""))
        dependencies = _mentioned_task_ids(text, declared_ids, task_id)
        open_dependencies = sorted(dep for dep in dependencies if status_by_id.get(dep) not in CLOSED_LEGACY_STATES)
        reasons: list[str] = []
        if open_dependencies:
            reasons.append("open_dependencies:" + ",".join(open_dependencies))
        elif (DEPENDENCY_GATE_RE.search(text) or SHORTHAND_TASK_REF_RE.search(text)) and not dependencies:
            reasons.append("unresolved_dependency_gate")
        if EXTERNAL_GATE_RE.search(text):
            reasons.append("external_or_environment_gate")
        if reasons:
            exclusions[task_id] = reasons
        else:
            runnable.append(task_id)
    return {"runnable_task_ids": sorted(set(runnable)), "exclusions": exclusions}


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
