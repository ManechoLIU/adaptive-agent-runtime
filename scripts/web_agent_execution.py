#!/usr/bin/env python3
"""Runtime-owned Web Agent execution health and recovery adapter."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

try:
    from scripts.assignment_runtime import apply_runtime_receipt, apply_observed_progress, evaluate_lease, load_runtime_state
    from scripts import terminal_continuation
    from scripts import web_lifecycle_bridge
    from scripts.reviewer_supervisor import validate_verdict
    from scripts.project_state import adaptive_delivery_state_dir
except ModuleNotFoundError:
    from assignment_runtime import apply_runtime_receipt, apply_observed_progress, evaluate_lease, load_runtime_state
    import terminal_continuation
    import web_lifecycle_bridge
    from reviewer_supervisor import validate_verdict
    from project_state import adaptive_delivery_state_dir

UTC = timezone.utc
STRONG_HOST_SOURCES = {"chatgpt_host_event"}
WEAK_UI_SOURCES = {"ai_bridge_browser_tab"}
TERMINAL_STATES = {"completed", "interrupted", "missing", "failed"}


def _iso(now: datetime) -> str:
    return (now if now.tzinfo else now.replace(tzinfo=UTC)).astimezone(UTC).isoformat()


def _registered_controller(repo: Path, registry_path: Path, controller_id: str) -> None:
    registered = web_lifecycle_bridge._registered_controller_for_common_dir(repo, registry_path)
    if registered != controller_id:
        raise PermissionError("Web execution event must target the existing registered logical Controller")


def _attestation(
    event: dict[str, Any],
    *,
    host_verifier: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any] | None] | None,
) -> dict[str, Any]:
    att = event.get("attestation")
    if not isinstance(att, dict) or att.get("kind") != "web_execution_state":
        raise ValueError("machine Web execution attestation is required")
    conversation = str(event.get("conversation_id") or "").strip()
    if not conversation or att.get("conversation_id") != conversation:
        raise ValueError("Web execution attestation conversation identity mismatch")
    observation_id = str(att.get("observation_id") or "").strip()
    if not observation_id:
        raise ValueError("Web execution attestation observation identity is required")
    source = str(att.get("source") or "").strip()
    if source in WEAK_UI_SOURCES:
        raise ValueError("weak browser UI evidence cannot establish or renew a canonical Web execution lease")
    if source not in STRONG_HOST_SOURCES:
        raise ValueError("untrusted Web execution attestation source")
    if host_verifier is None:
        raise ValueError("verified host provenance is required; self-asserted host attestation fails closed")
    verified = host_verifier(dict(att), dict(event))
    if not isinstance(verified, dict):
        raise ValueError("verified host provenance is required")
    if verified.get("verified") is not True or verified.get("fresh") is not True or verified.get("replay") is not False:
        raise ValueError("host attestation must be verified, fresh, and non-replayed")
    if str(verified.get("observation_id") or "") != observation_id:
        raise ValueError("host verifier observation identity mismatch")
    return att


def _current_lease(repo: Path, assignment_id: str, conversation_id: str) -> dict[str, Any]:
    lease = load_runtime_state(repo).get("leases", {}).get(assignment_id)
    if not isinstance(lease, dict):
        raise ValueError("Web execution has no canonical Assignment runtime lease")
    if lease.get("session_id") != conversation_id:
        raise ValueError("Web execution identity does not match current Assignment attempt")
    return lease


def _immutable_git_commit(repo: Path, candidate: str) -> str:
    value = str(candidate or "").strip()
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value):
        raise ValueError("Web reviewer candidate_revision must be an immutable Git commit")
    try:
        resolved = subprocess.check_output(
            ["git", "-C", str(repo), "rev-parse", "--verify", f"{value}^{{commit}}"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError("Web reviewer candidate_revision must be an immutable Git commit") from exc
    if resolved != value:
        raise ValueError("Web reviewer candidate_revision must be the exact immutable Git commit")
    return resolved


def _start_receipt(repo: Path, event: dict[str, Any], now: datetime) -> dict[str, Any]:
    assignment = event.get("assignment")
    if not isinstance(assignment, dict):
        raise ValueError("Web execution start requires Assignment contract")
    required = ("assignment_id", "task_id", "agent_id", "provider", "worktree", "primary_goal", "success_criteria", "owned_scope", "strategy")
    missing = [key for key in required if assignment.get(key) in (None, "", [])]
    if missing:
        raise ValueError("Web execution Assignment missing contract: " + ", ".join(missing))
    conversation_id = str(event.get("conversation_id") or "").strip()
    if conversation_id == str(event.get("controller_id") or "").strip():
        raise ValueError("Web conversation identity must never be used as controller identity")
    role = str(assignment.get("role") or "writer").strip().lower()
    candidate = str(assignment.get("candidate_revision") or "").strip() or None
    if role == "reviewer":
        if not candidate:
            raise ValueError("Web reviewer requires immutable candidate_revision")
        candidate = _immutable_git_commit(repo, candidate)
    attempt = int(assignment.get("attempt", 1))
    receipt = {
        "event_type": "assignment_started",
        "assignment_id": assignment["assignment_id"],
        "task_id": assignment["task_id"],
        "agent_id": assignment["agent_id"],
        "provider": assignment["provider"],
        "session_id": conversation_id,
        "worktree": assignment["worktree"],
        "issued_at": _iso(now),
        "attempt": attempt,
        "lease_id": str(assignment.get("lease_id") or f"{assignment['assignment_id']}:web:attempt:{attempt}"),
        "event_seq": 1,
        "receipt_id": f"web:{assignment['assignment_id']}:{attempt}:1",
        "assignment_contract_version": int(assignment.get("assignment_contract_version", 2)),
        "side_effect": assignment.get("side_effect"),
        "idempotency_key": assignment.get("idempotency_key"),
        "primary_goal": assignment["primary_goal"],
        "success_criteria": assignment["success_criteria"],
        "owned_scope": assignment["owned_scope"],
        "strategy": assignment["strategy"],
        "progress_deadline_minutes": assignment.get("progress_deadline_minutes"),
        "execution_transport": "web",
        "execution_role": role,
        "candidate_revision": candidate,
        "exclusive_execution_key": f"task:{assignment['task_id']}",
        "host_attestation_id": event["attestation"]["observation_id"],
    }
    if receipt["progress_deadline_minutes"] is None:
        receipt.pop("progress_deadline_minutes")
    return receipt


def _review_delivery(lease: dict[str, Any], event: dict[str, Any]) -> str:
    if lease.get("execution_role") != "reviewer":
        return str(event.get("delivery_outcome") or "unresolved")
    verdict = event.get("review_verdict")
    if not isinstance(verdict, dict):
        raise ValueError("Web reviewer completion requires structured terminal verdict")
    expected = str(lease.get("candidate_revision") or "")
    try:
        validate_verdict(verdict, expected)
    except ValueError as exc:
        message = str(exc).replace("candidate HEAD", "candidate revision")
        raise ValueError(message) from exc
    claimed = str(event.get("delivery_outcome") or ("pass" if verdict["verdict"] == "PASS" else "fail"))
    expected_outcome = "pass" if verdict["verdict"] == "PASS" else "fail"
    if claimed != expected_outcome:
        raise ValueError("Web reviewer delivery outcome disagrees with structured verdict")
    return claimed


def _git_progress_snapshot(worktree: str | Path) -> dict[str, str]:
    root = Path(worktree).expanduser().resolve()
    try:
        head = subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip()
        status = subprocess.check_output(
            ["git", "-C", str(root), "status", "--porcelain=v1", "--untracked-files=all"],
            text=True, stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError("Web runtime watchdog cannot verify the Assignment worktree") from exc
    return {
        "last_observed_head": head,
        "last_observed_status_sha256": hashlib.sha256(status.encode("utf-8")).hexdigest(),
    }


def _watchdog_log_path(repo: Path, assignment_id: str, attempt: int) -> Path:
    digest = hashlib.sha256(f"{assignment_id}:{attempt}".encode("utf-8")).hexdigest()
    return adaptive_delivery_state_dir(repo) / "web-agent-watchdog" / f"{digest}.log"


def _default_watchdog_launcher(
    *, repo: Path, registry_path: Path, assignment_id: str, attempt: int, lease_id: str,
) -> dict[str, Any]:
    log_path = _watchdog_log_path(repo, assignment_id, attempt)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = log_path.open("ab")
    try:
        process = subprocess.Popen(
            [
                sys.executable, str(Path(__file__).resolve()), "watch",
                "--repo", str(repo), "--registry", str(registry_path),
                "--assignment-id", assignment_id, "--attempt", str(attempt),
                "--lease-id", lease_id,
            ],
            stdin=subprocess.DEVNULL, stdout=handle, stderr=handle,
            start_new_session=True, close_fds=True,
        )
    finally:
        handle.close()
    return {"launched": True, "pid": process.pid, "log_path": str(log_path)}


def _dispatch_start_receipt(
    *, repo: Path, controller_id: str, conversation_id: str, assignment: dict[str, Any],
    now: datetime, attempt: int, lease_id: str,
) -> dict[str, Any]:
    required = (
        "assignment_id", "task_id", "agent_id", "provider", "worktree", "primary_goal",
        "success_criteria", "owned_scope", "strategy",
    )
    missing = [key for key in required if assignment.get(key) in (None, "", [])]
    if missing:
        raise ValueError("Web execution Assignment missing contract: " + ", ".join(missing))
    if not conversation_id or conversation_id == controller_id:
        raise ValueError("Web conversation execution identity must be distinct from Controller identity")
    role = str(assignment.get("role") or "writer").strip().lower()
    candidate = str(assignment.get("candidate_revision") or "").strip() or None
    if role == "reviewer":
        if not candidate:
            raise ValueError("Web reviewer requires immutable candidate_revision")
        candidate = _immutable_git_commit(repo, candidate)
    snapshot = _git_progress_snapshot(assignment["worktree"])
    receipt = {
        "event_type": "assignment_started",
        "assignment_id": assignment["assignment_id"], "task_id": assignment["task_id"],
        "agent_id": assignment["agent_id"], "provider": assignment["provider"],
        "session_id": conversation_id, "worktree": assignment["worktree"],
        "issued_at": _iso(now), "attempt": attempt, "lease_id": lease_id, "event_seq": 1,
        "receipt_id": f"web-runtime:{assignment['assignment_id']}:{attempt}:1",
        "assignment_contract_version": int(assignment.get("assignment_contract_version", 2)),
        "side_effect": assignment.get("side_effect"), "idempotency_key": assignment.get("idempotency_key"),
        "primary_goal": assignment["primary_goal"], "success_criteria": assignment["success_criteria"],
        "owned_scope": assignment["owned_scope"], "strategy": assignment["strategy"],
        "execution_transport": "web", "execution_role": role, "candidate_revision": candidate,
        "exclusive_execution_key": f"task:{assignment['task_id']}",
        "health_mode": "progress_watchdog",
        "baseline_head": snapshot["last_observed_head"],
        **snapshot,
    }
    if assignment.get("progress_deadline_minutes") is not None:
        receipt["progress_deadline_minutes"] = assignment["progress_deadline_minutes"]
    return receipt


def start_web_assignment(
    *, repo: str | Path, registry_path: str | Path, controller_id: str, conversation_id: str,
    assignment: dict[str, Any], now: datetime | None = None,
    watchdog_launcher: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Record a Controller-dispatched Web attempt without claiming Host generation liveness."""
    repo_path = Path(repo).expanduser().resolve()
    registry = Path(registry_path).expanduser().resolve()
    now = now or datetime.now(UTC)
    _registered_controller(repo_path, registry, controller_id)
    attempt = int(assignment.get("attempt", 1))
    assignment_id = str(assignment.get("assignment_id") or "").strip()
    if not assignment_id:
        raise ValueError("Web execution Assignment requires assignment_id")
    lease_id = str(assignment.get("lease_id") or f"{assignment_id}:web:attempt:{attempt}")
    receipt = _dispatch_start_receipt(
        repo=repo_path, controller_id=controller_id, conversation_id=conversation_id,
        assignment=assignment, now=now, attempt=attempt, lease_id=lease_id,
    )
    runtime = apply_runtime_receipt(repo_path, receipt, now=now)
    lease = runtime["leases"][assignment_id]
    launcher = watchdog_launcher or _default_watchdog_launcher
    watchdog = launcher(
        repo=repo_path, registry_path=registry, assignment_id=assignment_id,
        attempt=attempt, lease_id=lease_id,
    )
    return {
        "assignment_id": assignment_id, "attempt": attempt, "lease_id": lease_id,
        "controller_id": controller_id, "runtime_state": evaluate_lease(lease, now=now)["state"],
        "watchdog": watchdog,
    }


def _automatic_recovery_decision(lease: dict[str, Any], health: dict[str, Any]) -> dict[str, Any]:
    if health.get("state") == "budget_exhausted":
        return {"eligible": False, "reason": "recovery_budget_exhausted"}
    if health.get("state") != "unhealthy":
        return {"eligible": False, "reason": "runtime_not_unhealthy"}
    if int(lease.get("side_effect_contract_version", 1)) < 2:
        return {"eligible": False, "reason": "legacy_side_effect_unknown"}
    if lease.get("side_effect") is not False:
        return {"eligible": False, "reason": "unknown_side_effect_requires_reconciliation"}
    return {"eligible": True, "reason": "side_effect_free_runtime_timeout"}


def _runtime_continuation_result(
    *, repo: Path, registry_path: Path, event_source: str,
    consumer: Callable[..., dict[str, Any]] | None,
) -> dict[str, Any]:
    runtime_consumer = consumer or terminal_continuation.notify_runtime_change
    try:
        return {
            "runtime_continuation": runtime_consumer(
                repo=repo, registry_path=registry_path, event_source=event_source
            )
        }
    except (OSError, ValueError, PermissionError, RuntimeError, subprocess.SubprocessError) as exc:
        # The lifecycle transaction may already have persisted pending_control_event.
        # Keep the canonical unhealthy/terminal lease intact and let the detached
        # watchdog retry the same Controller wake rather than losing the recovery edge.
        return {"wake_error": f"{type(exc).__name__}: {exc}"}


def watch_web_assignment_once(
    *, repo: str | Path, registry_path: str | Path, assignment_id: str,
    expected_attempt: int, expected_lease_id: str, now: datetime | None = None,
    runtime_change_consumer: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """One Host-independent health pass using canonical lease + locally verified progress."""
    repo_path = Path(repo).expanduser().resolve()
    registry = Path(registry_path).expanduser().resolve()
    now = now or datetime.now(UTC)
    state = load_runtime_state(repo_path)
    lease = state.get("leases", {}).get(assignment_id)
    if not isinstance(lease, dict):
        raise ValueError("Web runtime watchdog has no canonical Assignment lease")
    if int(lease.get("attempt", 0)) != int(expected_attempt) or str(lease.get("lease_id") or "") != str(expected_lease_id):
        return {"assignment_id": assignment_id, "superseded": True, "runtime_state": "superseded"}
    if lease.get("execution_transport") != "web" or lease.get("health_mode") != "progress_watchdog":
        raise ValueError("Web runtime watchdog requires a progress-watchdog Web lease")
    if lease.get("terminal_state"):
        health = evaluate_lease(lease, now=now)
        result = {
            "assignment_id": assignment_id, "runtime_state": health["state"],
            "reason": health["reason"], "progress_observed": False,
            "auto_recovery_eligible": False,
        }
        result.update(_runtime_continuation_result(
            repo=repo_path, registry_path=registry,
            event_source=f"web_assignment_terminal:{assignment_id}:attempt:{expected_attempt}",
            consumer=runtime_change_consumer,
        ))
        return result
    observed = _git_progress_snapshot(lease["worktree"])
    state, changed = apply_observed_progress(
        repo_path, assignment_id=assignment_id, expected_attempt=expected_attempt,
        expected_lease_id=expected_lease_id, observed=observed, now=now,
    )
    lease = state["leases"][assignment_id]
    health = evaluate_lease(lease, now=now)
    recovery = _automatic_recovery_decision(lease, health)
    result = {
        "assignment_id": assignment_id, "runtime_state": health["state"], "reason": health["reason"],
        "progress_observed": changed, "auto_recovery_eligible": recovery["eligible"],
        "recovery_reason": recovery["reason"],
    }
    if health["state"] in {"unhealthy", "budget_exhausted"}:
        result.update(_runtime_continuation_result(
            repo=repo_path, registry_path=registry,
            event_source=f"web_assignment_{health['state']}:{assignment_id}:attempt:{expected_attempt}",
            consumer=runtime_change_consumer,
        ))
    return result


def recover_web_assignment(
    *, repo: str | Path, registry_path: str | Path, controller_id: str, assignment_id: str,
    conversation_id: str, now: datetime | None = None,
    watchdog_launcher: Callable[..., dict[str, Any]] | None = None,
    lease_id_factory: Callable[[str, int], str] | None = None,
) -> dict[str, Any]:
    """Create the next fenced attempt when the woken Controller has a replacement execution session."""
    repo_path = Path(repo).expanduser().resolve()
    registry = Path(registry_path).expanduser().resolve()
    now = now or datetime.now(UTC)
    _registered_controller(repo_path, registry, controller_id)
    state = load_runtime_state(repo_path)
    current = state.get("leases", {}).get(assignment_id)
    if not isinstance(current, dict):
        raise ValueError("Web recovery requires a canonical Assignment lease")
    health = evaluate_lease(current, now=now)
    decision = _automatic_recovery_decision(current, health)
    if not decision["eligible"]:
        if decision["reason"] == "unknown_side_effect_requires_reconciliation":
            raise ValueError("unknown side effect requires reconciliation before recovery")
        if decision["reason"] == "recovery_budget_exhausted":
            raise ValueError("recovery budget exhausted; strategy change requires a new execution lineage")
        raise ValueError(f"Web recovery is not allowed: {decision['reason']}")
    next_attempt = int(current.get("attempt", 1)) + 1
    factory = lease_id_factory or (lambda aid, attempt: f"{aid}:web:attempt:{attempt}:{uuid.uuid4().hex}")
    new_lease_id = factory(assignment_id, next_attempt)
    assignment = {
        "assignment_id": assignment_id, "task_id": current["task_id"], "agent_id": current["agent_id"],
        "provider": current["provider"], "worktree": current["worktree"],
        "primary_goal": current["primary_goal"], "success_criteria": current["success_criteria"],
        "owned_scope": current["owned_scope"], "strategy": current["strategy"],
        "assignment_contract_version": int(current.get("side_effect_contract_version", 2)),
        "side_effect": current.get("side_effect"), "idempotency_key": current.get("idempotency_key"),
        "progress_deadline_minutes": int(current.get("progress_deadline_minutes") or 30),
        "role": current.get("execution_role") or "writer", "candidate_revision": current.get("candidate_revision"),
    }
    receipt = _dispatch_start_receipt(
        repo=repo_path, controller_id=controller_id, conversation_id=conversation_id,
        assignment=assignment, now=now, attempt=next_attempt, lease_id=new_lease_id,
    )
    runtime = apply_runtime_receipt(repo_path, receipt, now=now)
    lease = runtime["leases"][assignment_id]
    launcher = watchdog_launcher or _default_watchdog_launcher
    watchdog = launcher(
        repo=repo_path, registry_path=registry, assignment_id=assignment_id,
        attempt=next_attempt, lease_id=new_lease_id,
    )
    return {
        "assignment_id": assignment_id, "attempt": next_attempt, "lease_id": new_lease_id,
        "controller_id": controller_id, "runtime_state": evaluate_lease(lease, now=now)["state"],
        "watchdog": watchdog,
    }


def watch_web_assignment(
    *, repo: str | Path, registry_path: str | Path, assignment_id: str,
    expected_attempt: int, expected_lease_id: str, poll_seconds: float = 15.0,
    runtime_change_consumer: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if poll_seconds < 0.1:
        raise ValueError("watchdog poll interval must be at least 0.1 seconds")
    while True:
        result = watch_web_assignment_once(
            repo=repo, registry_path=registry_path, assignment_id=assignment_id,
            expected_attempt=expected_attempt, expected_lease_id=expected_lease_id,
            runtime_change_consumer=runtime_change_consumer,
        )
        if result.get("superseded"):
            return result
        if result.get("runtime_state") in {"terminal", "unhealthy", "budget_exhausted"} and not result.get("wake_error"):
            return result
        time.sleep(poll_seconds)


def apply_web_execution_event(
    *,
    repo: str | Path,
    registry_path: str | Path,
    event: dict[str, Any],
    now: datetime | None = None,
    runtime_change_consumer: Callable[..., dict[str, Any]] | None = None,
    host_verifier: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    """Translate one trusted Web-host observation into the existing runtime state machine."""
    if not isinstance(event, dict):
        raise ValueError("Web execution event must be an object")
    repo = Path(repo).expanduser().resolve(); registry = Path(registry_path).expanduser().resolve()
    now = now or datetime.now(UTC)
    controller_id = str(event.get("controller_id") or "").strip()
    conversation_id = str(event.get("conversation_id") or "").strip()
    if not controller_id or not conversation_id:
        raise ValueError("controller_id and Web conversation execution identity are required")
    _registered_controller(repo, registry, controller_id)
    state = str(event.get("state") or "").strip().lower()
    terminal = state in TERMINAL_STATES
    att = _attestation(event, host_verifier=host_verifier)

    if state == "started":
        if att.get("state") != "running":
            raise ValueError("Web execution start requires host-observed running state")
        receipt = _start_receipt(repo, event, now)
        runtime = apply_runtime_receipt(repo, receipt, now=now)
        lease = runtime["leases"][receipt["assignment_id"]]
        return {"assignment_id": receipt["assignment_id"], "runtime_state": evaluate_lease(lease, now=now)["state"], "controller_id": controller_id}

    assignment_id = str(event.get("assignment_id") or "").strip()
    if not assignment_id:
        raise ValueError("Web execution observation requires assignment_id")
    lease = _current_lease(repo, assignment_id, conversation_id)
    attempt = int(event.get("attempt", lease.get("attempt", 1)))
    lease_id = str(event.get("lease_id") or lease.get("lease_id") or "")
    if attempt != int(lease.get("attempt", 1)):
        raise ValueError("Web execution event must match the current runtime attempt")
    if lease_id != str(lease.get("lease_id") or ""):
        raise ValueError("Web execution event must match the current runtime lease")
    seq = int(lease.get("last_event_seq", 0)) + 1
    base = {
        "assignment_id": assignment_id, "task_id": lease["task_id"], "agent_id": lease["agent_id"],
        "provider": lease["provider"], "session_id": conversation_id, "worktree": lease["worktree"],
        "issued_at": _iso(now), "attempt": attempt, "lease_id": lease_id, "event_seq": seq,
        "receipt_id": f"web:{assignment_id}:{attempt}:{seq}", "host_attestation_id": att["observation_id"],
    }
    if state == "heartbeat":
        if att.get("state") != "running":
            raise ValueError("heartbeat requires observed running state")
        receipt = {"event_type": "assignment_heartbeat", **base}
    elif state == "progress":
        if att.get("state") != "running":
            raise ValueError("progress requires observed running state")
        receipt = {"event_type": "assignment_progress", **base}
        for key in ("last_observed_head", "last_observed_status_sha256", "evidence_receipt_id", "artifact_fingerprint", "blocker_evidence_fingerprint"):
            if event.get(key) not in (None, ""):
                receipt[key] = event[key]
    elif terminal:
        if state in {"interrupted", "missing"}:
            raise ValueError("Web interruption/missing signal is not authoritative terminal evidence; runtime progress watchdog owns recovery")
        if att.get("state") != state:
            raise ValueError("terminal attestation state mismatch")
        if state == "completed":
            delivery = _review_delivery(lease, event)
            terminal_state, transport, retry_class = "completed", "completed", str(event.get("retry_class") or "none")
        else:
            delivery = str(event.get("delivery_outcome") or "unresolved")
            terminal_state, transport, retry_class = ("disconnected" if state in {"interrupted", "missing"} else "failed"), "failed", str(event.get("retry_class") or "transport_error")
        receipt = {
            "event_type": "assignment_terminal", **base,
            "terminal_state": terminal_state, "transport_outcome": transport, "delivery_outcome": delivery,
            "summary": str(event.get("summary") or f"Web execution {state}"),
            "evidence": list(event.get("evidence") or []), "artifacts": list(event.get("artifacts") or []),
            "next_action": str(event.get("next_action") or ("reconcile Web execution" if terminal_state != "completed" else "continue controller reconciliation")),
            "retry_class": retry_class,
            "side_effect": bool(lease.get("side_effect")), "idempotency_key": lease.get("idempotency_key"),
            "result_unknown": bool(lease.get("side_effect")) if terminal_state != "completed" else bool(event.get("result_unknown", False)),
        }
        if lease.get("execution_role") == "reviewer" and event.get("review_verdict") is not None:
            receipt["review_verdict"] = json.loads(json.dumps(event["review_verdict"]))
        if event.get("reconciliation_evidence") is not None:
            receipt["reconciliation_evidence"] = event["reconciliation_evidence"]
    else:
        raise ValueError(f"unsupported Web execution state: {state}")

    runtime = apply_runtime_receipt(repo, receipt, now=now)
    current = runtime["leases"][assignment_id]
    health = evaluate_lease(current, now=now)
    result = {"assignment_id": assignment_id, "runtime_state": health["state"], "controller_id": controller_id}
    if not terminal and health["state"] in {"progress_stale", "unhealthy", "budget_exhausted"}:
        runtime_consumer = runtime_change_consumer or terminal_continuation.notify_runtime_change
        result["runtime_continuation"] = runtime_consumer(
            repo=repo, registry_path=registry, event_source=f"web_assignment_{health['state']}"
        )
    if terminal:
        runtime_consumer = runtime_change_consumer or terminal_continuation.notify_runtime_change
        result["runtime_continuation"] = runtime_consumer(
            repo=repo, registry_path=registry, event_source=f"web_assignment_terminal:{assignment_id}:attempt:{attempt}"
        )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Runtime-owned Web Agent execution recovery adapter.")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("start", "recover"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--repo", required=True); cmd.add_argument("--registry", required=True)
    watch = sub.add_parser("watch")
    watch.add_argument("--repo", required=True); watch.add_argument("--registry", required=True)
    watch.add_argument("--assignment-id", required=True); watch.add_argument("--attempt", required=True, type=int)
    watch.add_argument("--lease-id", required=True); watch.add_argument("--poll-seconds", type=float, default=15.0)
    legacy = sub.add_parser("host-event")
    legacy.add_argument("--repo", required=True); legacy.add_argument("--registry", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "watch":
            result = watch_web_assignment(
                repo=args.repo, registry_path=args.registry, assignment_id=args.assignment_id,
                expected_attempt=args.attempt, expected_lease_id=args.lease_id, poll_seconds=args.poll_seconds,
            )
        else:
            event = json.load(sys.stdin)
            if not isinstance(event, dict):
                raise ValueError("Web execution input must be an object")
            if args.command == "start":
                result = start_web_assignment(
                    repo=args.repo, registry_path=args.registry,
                    controller_id=str(event.get("controller_id") or ""),
                    conversation_id=str(event.get("conversation_id") or ""),
                    assignment=event.get("assignment") if isinstance(event.get("assignment"), dict) else {},
                )
            elif args.command == "recover":
                result = recover_web_assignment(
                    repo=args.repo, registry_path=args.registry,
                    controller_id=str(event.get("controller_id") or ""),
                    assignment_id=str(event.get("assignment_id") or ""),
                    conversation_id=str(event.get("conversation_id") or ""),
                )
            else:
                result = apply_web_execution_event(repo=args.repo, registry_path=args.registry, event=event)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, ValueError, PermissionError, json.JSONDecodeError) as error:
        print(f"web-agent-execution: blocked: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
