#!/usr/bin/env python3
"""Ephemeral runtime lease evidence for adaptive-delivery assignments."""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

UTC = timezone.utc
EVENTS = {"assignment_started", "assignment_heartbeat", "assignment_progress", "assignment_terminal"}
TERMINAL_STATES = {"completed", "failed", "cancelled", "disconnected"}
OUTCOMES = {"success", "failed", "blocked", "cancelled", "recoverable_failure"}
TRANSIENT_RETRY_CLASSES = {"rate_limit", "transport_error", "provider_5xx", "capacity", "lease_timeout"}
MAX_ATTEMPTS = 3
IDENTITY_FIELDS = ("assignment_id", "task_id", "agent_id", "provider", "session_id", "worktree")

@dataclass(frozen=True)
class RuntimePolicy:
    heartbeat_ttl_minutes: int = 20
    progress_deadline_minutes: int = 30
    progress_grace_minutes: int = 15

def _dt(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()

def runtime_state_path(repo: str | Path) -> Path:
    return Path(repo) / ".git" / "adaptive-delivery" / "runtime-assignments.json"

def load_runtime_state(repo: str | Path) -> dict[str, Any]:
    path = runtime_state_path(repo)
    if not path.exists():
        return {"schema_version": 1, "leases": {}}
    return json.loads(path.read_text(encoding="utf-8"))

def save_runtime_state(repo: str | Path, state: dict[str, Any]) -> None:
    path = runtime_state_path(repo); path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload); handle.flush(); os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)

def apply_receipt(state: dict[str, Any], receipt: dict[str, Any], now: datetime | None = None, policy: RuntimePolicy | None = None) -> dict[str, Any]:
    policy = policy or RuntimePolicy(); now = now or datetime.now(UTC)
    event = receipt.get("event_type")
    if event not in EVENTS: raise ValueError(f"unsupported runtime event: {event}")
    missing = [f for f in IDENTITY_FIELDS if not receipt.get(f)]
    if missing: raise ValueError("runtime receipt missing identity: " + ", ".join(missing))
    issued = _dt(receipt.get("issued_at") or now)
    out = json.loads(json.dumps(state or {"schema_version": 1, "leases": {}})); out.setdefault("schema_version", 1); leases = out.setdefault("leases", {})
    aid = receipt["assignment_id"]; existing = leases.get(aid)
    attempt = int(receipt.get("attempt", 1)); lease_id = str(receipt.get("lease_id") or f"{aid}:attempt:{attempt}"); event_seq = int(receipt.get("event_seq", 1))
    if attempt < 1 or event_seq < 1: raise ValueError("attempt and event_seq must be positive")
    if existing:
        mismatches = [f for f in IDENTITY_FIELDS if existing.get(f) != receipt.get(f)]
        if mismatches: raise ValueError("runtime receipt identity mismatch: " + ", ".join(mismatches))
        current_attempt = int(existing.get("attempt", 1))
        if attempt < current_attempt: raise ValueError("stale runtime attempt")
        if attempt == current_attempt and lease_id != existing.get("lease_id"): raise ValueError("runtime lease_id mismatch")
        if attempt == current_attempt and event_seq <= int(existing.get("last_event_seq", 0)): raise ValueError("runtime event_seq must increase")
    if event == "assignment_started":
        if existing and attempt <= int(existing.get("attempt", 1)): raise ValueError("new runtime start requires a higher attempt")
        lease = {f: receipt[f] for f in IDENTITY_FIELDS}
        lease.update({"schema_version": 1, "attempt": attempt, "lease_id": lease_id, "last_event_seq": event_seq, "pid": receipt.get("pid"), "baseline_head": receipt.get("baseline_head"), "started_at": _iso(issued), "last_heartbeat_at": _iso(issued), "last_progress_at": _iso(issued), "last_observed_head": receipt.get("last_observed_head") or receipt.get("baseline_head"), "last_observed_status_sha256": receipt.get("last_observed_status_sha256"), "terminal_state": None, "terminal_at": None, "lease_expires_at": _iso(issued + timedelta(minutes=policy.heartbeat_ttl_minutes)), "progress_deadline_at": _iso(issued + timedelta(minutes=policy.progress_deadline_minutes)), "evidence_receipt_id": receipt.get("receipt_id")})
        leases[aid] = lease; return out
    if not existing: raise ValueError("runtime receipt has no started lease")
    lease = existing
    lease["last_event_seq"] = event_seq
    if event in {"assignment_heartbeat", "assignment_progress"}:
        lease["last_heartbeat_at"] = _iso(issued); lease["lease_expires_at"] = _iso(issued + timedelta(minutes=policy.heartbeat_ttl_minutes))
    if event == "assignment_progress":
        lease["last_progress_at"] = _iso(issued); lease["progress_deadline_at"] = _iso(issued + timedelta(minutes=policy.progress_deadline_minutes))
        for field in ("last_observed_head", "last_observed_status_sha256"):
            if field in receipt: lease[field] = receipt[field]
    if event == "assignment_terminal":
        terminal = receipt.get("terminal_state")
        if terminal not in TERMINAL_STATES: raise ValueError(f"invalid terminal state: {terminal}")
        outcome = receipt.get("outcome"); summary = str(receipt.get("summary", "")).strip()
        if outcome not in OUTCOMES or not summary: raise ValueError("terminal receipt requires structured outcome and summary")
        if not isinstance(receipt.get("evidence"), list) or not isinstance(receipt.get("artifacts"), list): raise ValueError("terminal receipt requires evidence and artifacts lists")
        if not str(receipt.get("next_action", "")).strip() or not str(receipt.get("retry_class", "")).strip(): raise ValueError("terminal receipt requires next_action and retry_class")
        lease["terminal_state"] = terminal; lease["terminal_at"] = _iso(issued); lease["outcome"] = outcome; lease["summary"] = summary
        lease["evidence"] = receipt["evidence"]; lease["artifacts"] = receipt["artifacts"]; lease["next_action"] = receipt["next_action"]; lease["retry_class"] = receipt["retry_class"]
    lease["evidence_receipt_id"] = receipt.get("receipt_id") or lease.get("evidence_receipt_id")
    return out

def evaluate_lease(lease: dict[str, Any] | None, now: datetime | None = None, policy: RuntimePolicy | None = None, process_probe: Callable[[int], bool] | None = None) -> dict[str, Any]:
    policy = policy or RuntimePolicy(); now = now or datetime.now(UTC)
    if not lease: return {"state": "unknown", "reason": "missing_runtime_lease"}
    if lease.get("terminal_state"): return {"state": "terminal", "reason": f"terminal:{lease['terminal_state']}"}
    pid = lease.get("pid")
    if pid is not None and process_probe is not None and not process_probe(int(pid)):
        return {"state": "unhealthy", "reason": "process_not_alive"}
    expires = _dt(lease["lease_expires_at"])
    if now > expires: return {"state": "unhealthy", "reason": "lease_expired"}
    deadline = _dt(lease["progress_deadline_at"])
    if now > deadline + timedelta(minutes=policy.progress_grace_minutes): return {"state": "unhealthy", "reason": "progress_stale_beyond_grace"}
    if now > deadline: return {"state": "progress_stale", "reason": "progress_deadline_exceeded"}
    return {"state": "healthy", "reason": "runtime_evidence_current"}


def retry_decision(retry_class: str, attempt: int, base_delay_seconds: int = 5) -> dict[str, Any]:
    retry = retry_class in TRANSIENT_RETRY_CLASSES and attempt < MAX_ATTEMPTS
    if not retry:
        return {"retry": False, "reason": "non_retryable" if retry_class not in TRANSIENT_RETRY_CLASSES else "attempt_budget_exhausted"}
    # Deterministic envelope; caller adds random jitter within this range.
    delay = base_delay_seconds * (2 ** max(0, attempt - 1))
    return {"retry": True, "next_attempt": attempt + 1, "backoff_seconds": delay, "jitter_max_seconds": max(1, delay // 2)}
