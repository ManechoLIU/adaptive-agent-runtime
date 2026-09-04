#!/usr/bin/env python3
"""Runtime-owned Web Agent execution health and recovery adapter."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import tempfile
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
    from scripts.web_agent_events import structured_subagent_events
except ModuleNotFoundError:
    from assignment_runtime import apply_runtime_receipt, apply_observed_progress, evaluate_lease, load_runtime_state
    import terminal_continuation
    import web_lifecycle_bridge
    from reviewer_supervisor import validate_verdict
    from project_state import adaptive_delivery_state_dir
    from web_agent_events import structured_subagent_events

UTC = timezone.utc
STRONG_HOST_SOURCES = {"chatgpt_host_event"}
WEAK_UI_SOURCES = {"ai_bridge_browser_tab"}
TERMINAL_STATES = {"completed", "interrupted", "missing", "failed"}
WEB_EXECUTION_PROVIDERS = {"chatgpt_web"}


def _trusted_host_execution_verifier():
    """Return a Host-owned execution verifier when one is installed; absent by default."""
    return None


def _iso(now: datetime) -> str:
    return (now if now.tzinfo else now.replace(tzinfo=UTC)).astimezone(UTC).isoformat()


def _registered_controller(repo: Path, registry_path: Path, controller_id: str) -> None:
    registered = web_lifecycle_bridge._registered_controller_for_common_dir(repo, registry_path)
    if registered != controller_id:
        raise PermissionError("Web execution event must target the existing registered logical Controller")


def _attestation(event: dict[str, Any]) -> dict[str, Any]:
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
    host_verifier = _trusted_host_execution_verifier()
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


def _validate_web_assignment_route(
    assignment: dict[str, Any], *, runtime_repo: str | Path
) -> dict[str, Any]:
    """Bind Web execution to the same canonical delegated route contract as every other executor."""
    try:
        from scripts.route_contract import delegated_route_contract_errors
    except ModuleNotFoundError:
        from route_contract import delegated_route_contract_errors

    task_id = str(assignment.get("task_id") or "").strip() or "unknown"
    route = assignment.get("route")
    errors = delegated_route_contract_errors(
        task_id,
        assignment.get("owned_scope"),
        route,
        runtime_repo=runtime_repo,
    )
    if errors:
        raise PermissionError("Web Assignment route rejected: " + "; ".join(errors))
    assert isinstance(route, dict)
    for field in ("provider", "model"):
        actual = str(assignment.get(field) or "").strip()
        routed = str(route.get(field) or "").strip()
        if actual != routed:
            raise PermissionError(
                f"Web Assignment {field} does not match canonical route: {actual} != {routed}"
            )
    provider = str(assignment.get("provider") or "").strip()
    if provider not in WEB_EXECUTION_PROVIDERS:
        raise PermissionError(
            f"Web execution transport cannot execute provider {provider}; "
            "dispatch through the canonical provider executor instead"
        )
    return route


def _start_receipt(repo: Path, event: dict[str, Any], now: datetime) -> dict[str, Any]:
    assignment = event.get("assignment")
    if not isinstance(assignment, dict):
        raise ValueError("Web execution start requires Assignment contract")
    required = ("assignment_id", "task_id", "agent_id", "provider", "model", "agent_type", "worktree", "primary_goal", "success_criteria", "owned_scope", "strategy")
    missing = [key for key in required if assignment.get(key) in (None, "", [])]
    if missing:
        raise ValueError("Web execution Assignment missing contract: " + ", ".join(missing))
    route = _validate_web_assignment_route(assignment, runtime_repo=repo)
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
        "model": assignment["model"],
        "agent_type": assignment["agent_type"],
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
        "auth_mode": str(route.get("auth_mode") or "").strip() or None,
        "policy_class": str(route.get("policy_class") or "").strip() or None,
        "route_decision": str(route.get("decision") or "").strip() or None,
        "route_contract": json.loads(json.dumps(route)),
        "exclusive_execution_key": f"task:{assignment['task_id']}",
        "exclusive_execution_keys": [
            f"task:{assignment['task_id']}",
            *([f"worktree:{Path(assignment['worktree']).expanduser().resolve()}"] if role == "writer" else []),
        ],
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
        "assignment_id", "task_id", "agent_id", "provider", "model", "agent_type",
        "worktree", "primary_goal", "success_criteria", "owned_scope", "strategy",
    )
    missing = [key for key in required if assignment.get(key) in (None, "", [])]
    if missing:
        raise ValueError("Web execution Assignment missing contract: " + ", ".join(missing))
    route = _validate_web_assignment_route(assignment, runtime_repo=repo)
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
        "model": assignment["model"], "agent_type": assignment["agent_type"],
        "session_id": conversation_id, "worktree": assignment["worktree"],
        "issued_at": _iso(now), "attempt": attempt, "lease_id": lease_id, "event_seq": 1,
        "receipt_id": f"web-runtime:{assignment['assignment_id']}:{attempt}:1",
        "assignment_contract_version": int(assignment.get("assignment_contract_version", 2)),
        "side_effect": assignment.get("side_effect"), "idempotency_key": assignment.get("idempotency_key"),
        "primary_goal": assignment["primary_goal"], "success_criteria": assignment["success_criteria"],
        "owned_scope": assignment["owned_scope"], "strategy": assignment["strategy"],
        "execution_transport": "web", "execution_role": role, "candidate_revision": candidate,
        "auth_mode": str(route.get("auth_mode") or "").strip() or None,
        "policy_class": str(route.get("policy_class") or "").strip() or None,
        "route_decision": str(route.get("decision") or "").strip() or None,
        "route_contract": json.loads(json.dumps(route)),
        "exclusive_execution_key": f"task:{assignment['task_id']}",
        "exclusive_execution_keys": [
            f"task:{assignment['task_id']}",
            *([f"worktree:{Path(assignment['worktree']).expanduser().resolve()}"] if role == "writer" else []),
        ],
        "health_mode": "progress_watchdog",
        "baseline_head": snapshot["last_observed_head"],
        **snapshot,
    }
    if assignment.get("progress_deadline_minutes") is not None:
        receipt["progress_deadline_minutes"] = assignment["progress_deadline_minutes"]
    return receipt



def _dispatch_state_path(repo: Path) -> Path:
    return adaptive_delivery_state_dir(repo) / "web-agent-dispatches.json"


def _load_dispatch_state(repo: Path) -> dict[str, Any]:
    path = _dispatch_state_path(repo)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema_version": 1, "dispatches": {}}
    if not isinstance(value, dict) or not isinstance(value.get("dispatches"), dict):
        return {"schema_version": 1, "dispatches": {}}
    return value


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _mutate_dispatch_state(repo: Path, mutation: Callable[[dict[str, Any]], Any]) -> Any:
    path = _dispatch_state_path(repo)
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            state = _load_dispatch_state(repo)
            result = mutation(state)
            _atomic_write_json(path, state)
            return result
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _health_supervisor_is_ready(
    probe: Callable[[], bool] | None = None,
    *,
    watchdog_launcher: Callable[..., dict[str, Any]] | None = None,
) -> bool:
    if probe is not None:
        return bool(probe())
    if watchdog_launcher is not None:
        # Backward-compatible/injected execution owner for tests and embedding. The
        # production CLI never injects this and therefore requires the global service.
        return True
    try:
        from scripts.web_agent_health_supervisor import health_supervisor_ready
    except ModuleNotFoundError:
        from web_agent_health_supervisor import health_supervisor_ready
    return bool(health_supervisor_ready())


def _machine_event_source_context() -> dict[str, Any]:
    try:
        from scripts.web_agent_events import machine_event_source_status
    except ModuleNotFoundError:
        from web_agent_events import machine_event_source_status
    value = machine_event_source_status()
    return dict(value) if isinstance(value, dict) else {
        "ready": False, "reason": "machine_event_source_status_invalid",
    }


def _machine_event_source_is_ready(
    probe: Callable[[], bool] | None = None,
) -> bool:
    # Legacy probe arguments are intentionally ignored. A local caller cannot
    # self-assert production machine-event trust.
    return bool(_machine_event_source_context().get("ready"))


def _verified_machine_event_paths(requested: list[str | Path]) -> list[Path]:
    context = _machine_event_source_context()
    if context.get("ready") is not True:
        raise RuntimeError(
            "trusted Web Assignment machine event source is not ready; binding fails closed"
        )
    trusted = {
        str(Path(value).expanduser().resolve(strict=False))
        for value in context.get("event_paths", [])
        if isinstance(value, str) and value.strip()
    }
    if not trusted:
        raise RuntimeError("trusted Web machine event source did not attest event paths")
    resolved = [Path(value).expanduser().resolve(strict=False) for value in requested]
    untrusted = [str(path) for path in resolved if str(path) not in trusted]
    if untrusted:
        raise PermissionError(
            "Web bind event path is not attested by the trusted machine event source: "
            + ", ".join(untrusted)
        )
    return resolved


def prepare_web_assignment_dispatch(
    *, repo: str | Path, registry_path: str | Path, controller_id: str, task_name: str,
    assignment: dict[str, Any], now: datetime | None = None,
    health_probe: Callable[[], bool] | None = None,
    event_source_probe: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Create a durable pre-spawn ticket; no child execution may be represented before a real started event."""
    repo_path = Path(repo).expanduser().resolve()
    registry = Path(registry_path).expanduser().resolve()
    now = now or datetime.now(UTC)
    _registered_controller(repo_path, registry, controller_id)
    if not _health_supervisor_is_ready(health_probe):
        raise RuntimeError("Web Assignment health supervisor is not ready; dispatch fails closed")
    if not _machine_event_source_is_ready(event_source_probe):
        raise RuntimeError("Web Assignment machine event source is not ready; dispatch fails closed")
    task_name = str(task_name or "").strip()
    if not task_name:
        raise ValueError("Web dispatch requires a non-empty task_name")
    # Validate the complete contract before a host spawn is allowed.
    _dispatch_start_receipt(
        repo=repo_path, controller_id=controller_id, conversation_id="__pending_web_child__",
        assignment=assignment, now=now, attempt=int(assignment.get("attempt", 1)),
        lease_id=str(assignment.get("lease_id") or f"{assignment.get('assignment_id')}:web:attempt:{int(assignment.get('attempt', 1))}"),
    )
    dispatch_id = uuid.uuid4().hex

    def mutate(state: dict[str, Any]) -> dict[str, Any]:
        dispatches = state.setdefault("dispatches", {})
        assignment_id = str(assignment.get("assignment_id") or "").strip()
        for record in dispatches.values():
            if not isinstance(record, dict):
                continue
            if record.get("state") == "pending" and (
                record.get("assignment_id") == assignment_id or record.get("task_name") == task_name
            ):
                raise ValueError("Web dispatch already has a pending Runtime ticket")
        ticket = {
            "dispatch_id": dispatch_id,
            "state": "pending",
            "controller_id": controller_id,
            "task_name": task_name,
            "assignment_id": assignment_id,
            "assignment": json.loads(json.dumps(assignment)),
            "prepared_at": _iso(now),
        }
        dispatches[dispatch_id] = ticket
        return dict(ticket)

    return _mutate_dispatch_state(repo_path, mutate)


def require_prepared_web_dispatch(
    *, repo: str | Path, controller_id: str, task_name: str,
    expected_model: str, expected_agent_type: str,
    health_probe: Callable[[], bool] | None = None,
    event_source_probe: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    repo_path = Path(repo).expanduser().resolve()
    if not _health_supervisor_is_ready(health_probe):
        raise PermissionError("Web Assignment health supervisor is not ready")
    if not _machine_event_source_is_ready(event_source_probe):
        raise PermissionError("Web Assignment machine event source is not ready")
    task_name = str(task_name or "").strip()
    candidates = [
        record for record in _load_dispatch_state(repo_path).get("dispatches", {}).values()
        if isinstance(record, dict)
        and record.get("state") == "pending"
        and record.get("controller_id") == controller_id
        and record.get("task_name") == task_name
    ]
    if len(candidates) != 1:
        raise PermissionError("collaboration.spawn_agent requires exactly one prepared canonical Web Runtime dispatch ticket")
    ticket = dict(candidates[0])
    assignment = ticket.get("assignment")
    if not isinstance(assignment, dict):
        raise PermissionError("prepared Web Runtime dispatch lost its Assignment contract")
    expected = str(expected_model or "").strip()
    if not expected:
        raise PermissionError("collaboration.spawn_agent requires observed model")
    prepared = str(assignment.get("model") or "").strip()
    if expected != prepared:
        raise PermissionError(
            f"collaboration.spawn_agent model does not match prepared Runtime Assignment: {expected} != {prepared}"
        )
    expected_type = str(expected_agent_type or "").strip()
    if not expected_type:
        raise PermissionError("collaboration.spawn_agent requires observed agent_type")
    prepared_type = str(assignment.get("agent_type") or "").strip()
    if expected_type != prepared_type:
        raise PermissionError(
            f"collaboration.spawn_agent agent_type does not match prepared Runtime Assignment: {expected_type} != {prepared_type}"
        )
    return ticket



def _persist_observed_dispatch(
    *, repo: Path, dispatch_id: str, controller_id: str, assignment_id: str,
    observed: dict[str, Any], now: datetime,
) -> dict[str, Any]:
    conversation_id = str(observed.get("conversation_id") or "").strip()
    observation_id = str(observed.get("observation_id") or "").strip()
    if not conversation_id or not observation_id:
        raise ValueError("machine-observed Web dispatch is missing child session identity")
    def mutate(state: dict[str, Any]) -> dict[str, Any]:
        record = state.setdefault("dispatches", {}).get(dispatch_id)
        if not isinstance(record, dict) or record.get("state") not in {"pending", "observed"}:
            raise ValueError("Web dispatch ticket is not available for machine observation")
        if record.get("controller_id") != controller_id or record.get("assignment_id") != assignment_id:
            raise PermissionError("machine-observed Web dispatch does not match its Runtime ticket")
        if record.get("state") == "observed":
            if (
                record.get("conversation_id") != conversation_id
                or record.get("observation_id") != observation_id
            ):
                raise ValueError("Web dispatch ticket has conflicting machine observation")
            return dict(record)
        source = str(observed.get("source") or "").strip()
        if source not in {"collaboration_session_event", "chatgpt_host_event"}:
            raise PermissionError("machine-observed Web dispatch has untrusted provenance")
        record.update({
            "state": "observed",
            "conversation_id": conversation_id,
            "observation_id": observation_id,
            "call_id": observed.get("call_id"),
            "observed_at": _iso(now),
            "observation_source": source,
        })
        return dict(record)
    return _mutate_dispatch_state(repo, mutate)


def _mark_dispatch_bound(
    *, repo: Path, dispatch_id: str, conversation_id: str,
    observation_id: str, call_id: str | None, now: datetime,
) -> None:
    def mutate(current: dict[str, Any]) -> None:
        record = current.setdefault("dispatches", {}).get(dispatch_id)
        if not isinstance(record, dict) or record.get("state") != "observed":
            raise ValueError("Web dispatch ticket changed while binding")
        if (
            str(record.get("conversation_id") or "") != conversation_id
            or str(record.get("observation_id") or "") != observation_id
        ):
            raise ValueError("Web dispatch binding does not match persisted observation")
        record.update({
            "state": "bound",
            "conversation_id": conversation_id,
            "observation_id": observation_id,
            "call_id": call_id,
            "bound_at": _iso(now),
        })
    _mutate_dispatch_state(repo, mutate)


def _verify_persisted_replacement_session_proof(
    *, repo: Path, controller_id: str, assignment_id: str,
    conversation_id: str, proof: dict[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(proof, dict):
        raise ValueError("replacement execution session requires persisted machine-observed dispatch proof")
    dispatch_id = str(proof.get("dispatch_id") or "").strip()
    observation_id = str(proof.get("observation_id") or "").strip()
    proof_source = str(proof.get("source") or "").strip()
    if proof_source not in {"collaboration_session_event", "chatgpt_host_event"} or not dispatch_id or not observation_id:
        raise ValueError("replacement execution session proof must identify a persisted machine-observed dispatch")
    ticket = _load_dispatch_state(repo).get("dispatches", {}).get(dispatch_id)
    if (
        not isinstance(ticket, dict)
        or ticket.get("state") != "observed"
        or ticket.get("controller_id") != controller_id
        or ticket.get("assignment_id") != assignment_id
        or ticket.get("observation_source") != proof_source
        or str(ticket.get("conversation_id") or "") != conversation_id
        or str(ticket.get("observation_id") or "") != observation_id
        or str(proof.get("conversation_id") or "") != conversation_id
    ):
        raise ValueError("replacement execution session proof does not match persisted machine-observed dispatch")
    return dict(ticket)


def _event_time(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _structured_started_for_ticket(ticket: dict[str, Any], event_paths: list[str | Path]) -> dict[str, Any]:
    prepared_at = _event_time(ticket.get("prepared_at"))
    if prepared_at is None:
        raise ValueError("Web dispatch ticket has no valid prepared_at timestamp")
    candidates = []
    for event in structured_subagent_events(event_paths):
        if event.get("kind") != "started" or event.get("task_name") != ticket.get("task_name"):
            continue
        observed_at = _event_time(event.get("timestamp"))
        if observed_at is None or observed_at < prepared_at:
            continue
        candidates.append(event)
    if len(candidates) != 1:
        raise ValueError("Web dispatch requires exactly one structured machine started observation newer than the Runtime ticket")
    return candidates[0]


def bind_web_assignment_dispatch(
    *, repo: str | Path, registry_path: str | Path, controller_id: str, dispatch_id: str,
    event_paths: list[str | Path], now: datetime | None = None,
    watchdog_launcher: Callable[..., dict[str, Any]] | None = None,
    health_probe: Callable[[], bool] | None = None,
    event_source_probe: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Bind a pre-spawn ticket to the machine-observed child thread and create its canonical lease."""
    repo_path = Path(repo).expanduser().resolve()
    registry = Path(registry_path).expanduser().resolve()
    now = now or datetime.now(UTC)
    _registered_controller(repo_path, registry, controller_id)
    if not _health_supervisor_is_ready(health_probe, watchdog_launcher=watchdog_launcher):
        raise RuntimeError("Web Assignment health supervisor is not ready; binding fails closed")
    paths = _verified_machine_event_paths(event_paths)

    state = _load_dispatch_state(repo_path)
    ticket = state.get("dispatches", {}).get(dispatch_id)
    if not isinstance(ticket, dict) or ticket.get("state") != "pending":
        raise ValueError("Web dispatch ticket is missing, consumed, or not pending")
    if ticket.get("controller_id") != controller_id:
        raise PermissionError("Web dispatch ticket belongs to a different logical Controller")
    observed = _structured_started_for_ticket(ticket, paths)
    conversation_id = str(observed.get("conversation_id") or "").strip()
    assignment = ticket.get("assignment")
    if not isinstance(assignment, dict):
        raise ValueError("Web dispatch ticket lost its Assignment contract")
    assignment_id = str(ticket.get("assignment_id") or "")
    observed_model = str(observed.get("model") or "").strip()
    observed_agent_type = str(observed.get("agent_type") or "").strip()
    if observed_model != str(assignment.get("model") or "").strip():
        raise PermissionError("machine-observed Web Agent model does not match the prepared Runtime Assignment")
    if observed_agent_type != str(assignment.get("agent_type") or "").strip():
        raise PermissionError("machine-observed Web Agent type does not match the prepared Runtime Assignment")
    _persist_observed_dispatch(
        repo=repo_path, dispatch_id=dispatch_id, controller_id=controller_id,
        assignment_id=assignment_id, observed=observed, now=now,
    )

    existing = load_runtime_state(repo_path).get("leases", {}).get(assignment_id)
    if isinstance(existing, dict):
        result = recover_web_assignment(
            repo=repo_path, registry_path=registry, controller_id=controller_id,
            assignment_id=assignment_id, conversation_id=conversation_id,
            now=now,
            watchdog_launcher=watchdog_launcher,
            health_probe=health_probe,
            event_source_probe=event_source_probe,
            replacement_session_proof={
                "source": "collaboration_session_event",
                "dispatch_id": dispatch_id,
                "observation_id": observed.get("observation_id"),
                "conversation_id": conversation_id,
            },
        )
    else:
        result = _start_bound_web_assignment(
            repo=repo_path, registry_path=registry, controller_id=controller_id,
            conversation_id=conversation_id, assignment=assignment, dispatch_id=dispatch_id, now=now,
            watchdog_launcher=watchdog_launcher,
            health_probe=health_probe,
        )

    _mark_dispatch_bound(
        repo=repo_path,
        dispatch_id=dispatch_id,
        conversation_id=conversation_id,
        observation_id=str(observed.get("observation_id") or ""),
        call_id=str(observed.get("call_id") or "") or None,
        now=now,
    )
    return {**result, "dispatch_id": dispatch_id, "conversation_id": conversation_id, "observation": observed}


def _start_bound_web_assignment(
    *, repo: str | Path, registry_path: str | Path, controller_id: str, conversation_id: str,
    assignment: dict[str, Any], dispatch_id: str, now: datetime | None = None,
    watchdog_launcher: Callable[..., dict[str, Any]] | None = None,
    health_probe: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Create the canonical lease only from a persisted observed dispatch ticket."""
    repo_path = Path(repo).expanduser().resolve()
    registry = Path(registry_path).expanduser().resolve()
    now = now or datetime.now(UTC)
    _registered_controller(repo_path, registry, controller_id)
    if not _health_supervisor_is_ready(health_probe, watchdog_launcher=watchdog_launcher):
        raise RuntimeError("Web Assignment health supervisor is not ready; active lease was not created")
    assignment_id = str(assignment.get("assignment_id") or "").strip()
    ticket = _load_dispatch_state(repo_path).get("dispatches", {}).get(str(dispatch_id or ""))
    if (
        not isinstance(ticket, dict)
        or ticket.get("state") != "observed"
        or ticket.get("controller_id") != controller_id
        or ticket.get("assignment_id") != assignment_id
        or str(ticket.get("conversation_id") or "") != conversation_id
        or str(ticket.get("observation_id") or "").strip() == ""
    ):
        raise PermissionError(
            "Web Assignment start requires a prepared dispatch with persisted verified started observation"
        )
    attempt = int(assignment.get("attempt", 1))
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


def start_web_assignment(
    *, repo: str | Path, registry_path: str | Path, controller_id: str, conversation_id: str,
    assignment: dict[str, Any], now: datetime | None = None,
    watchdog_launcher: Callable[..., dict[str, Any]] | None = None,
    health_probe: Callable[[], bool] | None = None,
    event_source_probe: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Compatibility entry point: direct Web start is permanently fail-closed."""
    raise PermissionError(
        "direct Web start is disabled; use prepared dispatch + verified started observation + bind"
    )


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
    replacement_session_proof: dict[str, Any] | None = None,
    health_probe: Callable[[], bool] | None = None,
    event_source_probe: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Create the next fenced attempt when the woken Controller has a replacement execution session."""
    repo_path = Path(repo).expanduser().resolve()
    registry = Path(registry_path).expanduser().resolve()
    now = now or datetime.now(UTC)
    _registered_controller(repo_path, registry, controller_id)
    if not _health_supervisor_is_ready(health_probe, watchdog_launcher=watchdog_launcher):
        raise RuntimeError("Web Assignment health supervisor is not ready; recovery fails closed")
    if not _machine_event_source_is_ready(event_source_probe):
        raise RuntimeError("Web Assignment machine event source is not ready; recovery fails closed")
    state = load_runtime_state(repo_path)
    current = state.get("leases", {}).get(assignment_id)
    if not isinstance(current, dict):
        raise ValueError("Web recovery requires a canonical Assignment lease")
    conversation_id = str(conversation_id or "").strip()
    if not conversation_id or conversation_id == str(current.get("session_id") or ""):
        raise ValueError("replacement execution session must differ from the previous Assignment session")
    health = evaluate_lease(current, now=now)
    decision = _automatic_recovery_decision(current, health)
    if not decision["eligible"]:
        if decision["reason"] == "unknown_side_effect_requires_reconciliation":
            raise ValueError("unknown side effect requires reconciliation before recovery")
        if decision["reason"] == "recovery_budget_exhausted":
            raise ValueError("recovery budget exhausted; strategy change requires a new execution lineage")
        raise ValueError(f"Web recovery is not allowed: {decision['reason']}")
    _verify_persisted_replacement_session_proof(
        repo=repo_path, controller_id=controller_id, assignment_id=assignment_id,
        conversation_id=conversation_id, proof=replacement_session_proof,
    )
    next_attempt = int(current.get("attempt", 1)) + 1
    factory = lease_id_factory or (lambda aid, attempt: f"{aid}:web:attempt:{attempt}:{uuid.uuid4().hex}")
    new_lease_id = factory(assignment_id, next_attempt)
    assignment = {
        "assignment_id": assignment_id, "task_id": current["task_id"], "agent_id": current["agent_id"],
        "provider": current["provider"], "model": current["model"], "agent_type": current["agent_type"],
        "worktree": current["worktree"],
        "primary_goal": current["primary_goal"], "success_criteria": current["success_criteria"],
        "owned_scope": current["owned_scope"], "strategy": current["strategy"],
        "assignment_contract_version": int(current.get("side_effect_contract_version", 2)),
        "side_effect": current.get("side_effect"), "idempotency_key": current.get("idempotency_key"),
        "progress_deadline_minutes": int(current.get("progress_deadline_minutes") or 30),
        "role": current.get("execution_role") or "writer", "candidate_revision": current.get("candidate_revision"),
        "route": json.loads(json.dumps(current.get("route_contract"))) if isinstance(current.get("route_contract"), dict) else None,
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
            continuation = result.get("runtime_continuation")
            wake_receipt = continuation.get("wake_result") if isinstance(continuation, dict) else None
            if web_lifecycle_bridge.wake_receipt_confirmed(wake_receipt):
                return result
        time.sleep(poll_seconds)



def _structured_terminal_projection(kind: str) -> tuple[str, str, str]:
    normalized = str(kind or "").strip().lower()
    if normalized == "completed":
        return "completed", "completed", "none"
    if normalized in {"cancelled", "interrupted"}:
        return "cancelled", "cancelled", "none"
    if normalized == "disconnected":
        return "disconnected", "failed", "transport_error"
    if normalized == "failed":
        return "failed", "failed", "transport_error"
    raise ValueError(f"unsupported structured SubAgentActivity terminal: {normalized}")


def _structured_terminal_receipt_path(repo: Path, lease: dict[str, Any]) -> Path:
    key = f"{lease['assignment_id']}:{int(lease['attempt'])}:{lease['lease_id']}"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return adaptive_delivery_state_dir(repo) / "web-agent-terminal" / f"{digest}.json"


def _external_terminal_receipt(
    *, repo: Path, lease: dict[str, Any], observation: dict[str, Any]
) -> dict[str, Any]:
    terminal = str(lease.get("terminal_state") or "")
    return {
        "schema_version": 1,
        "event_type": "external_agent_terminal",
        "engine": "collaboration",
        "repo": str(repo),
        "cwd": str(lease["worktree"]),
        "exit_code": 0 if terminal == "completed" else 1,
        "summary": str(lease.get("summary") or f"structured collaboration child {terminal}"),
        "delivery_outcome": str(lease.get("delivery_outcome") or "unresolved"),
        "assignment_id": lease["assignment_id"],
        "task_id": lease["task_id"],
        "agent_id": lease["agent_id"],
        "provider": lease["provider"],
        "model": lease.get("model"),
        "agent_type": lease.get("agent_type"),
        "session_id": lease["session_id"],
        "attempt": int(lease["attempt"]),
        "lease_id": lease["lease_id"],
        "completed_at": str(lease.get("terminal_at") or ""),
        "machine_terminal_observation_id": str(observation.get("observation_id") or ""),
        "machine_terminal_source": "collaboration_session_event",
    }


def _ingest_verified_structured_subagent_terminal(
    *,
    repo: str | Path,
    assignment_id: str,
    observation: dict[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Persist a terminal only after the lifecycle reconciler verified Host-attested source paths."""
    repo_path = Path(repo).expanduser().resolve()
    now = now or datetime.now(UTC)
    if not isinstance(observation, dict) or observation.get("source") != "collaboration_session_event":
        raise ValueError("structured Web terminal requires a collaboration session observation")
    conversation_id = str(observation.get("conversation_id") or "").strip()
    observation_id = str(observation.get("observation_id") or "").strip()
    if not conversation_id or not observation_id:
        raise ValueError("structured Web terminal requires machine session and observation identity")
    state = load_runtime_state(repo_path)
    lease = state.get("leases", {}).get(assignment_id)
    if not isinstance(lease, dict):
        raise ValueError("structured Web terminal has no canonical Assignment lease")
    if lease.get("execution_transport") != "web":
        raise ValueError("structured Web terminal requires a canonical Web Assignment lease")
    if conversation_id != str(lease.get("session_id") or ""):
        raise ValueError("structured Web terminal session does not match the canonical Assignment session")
    terminal_state, transport_outcome, retry_class = _structured_terminal_projection(
        str(observation.get("kind") or "")
    )
    duplicate = bool(lease.get("terminal_state"))
    if duplicate:
        if str(lease.get("terminal_state") or "") != terminal_state:
            raise ValueError("structured Web terminal conflicts with immutable canonical terminal state")
    else:
        event_seq = int(lease.get("last_event_seq", 0)) + 1
        result_unknown = (
            int(lease.get("side_effect_contract_version", 1)) < 2
            or lease.get("side_effect") is not False
        )
        receipt = {
            "event_type": "assignment_terminal",
            "assignment_id": assignment_id,
            "task_id": lease["task_id"],
            "agent_id": lease["agent_id"],
            "provider": lease["provider"],
            "session_id": lease["session_id"],
            "worktree": lease["worktree"],
            "issued_at": _iso(now),
            "attempt": int(lease["attempt"]),
            "lease_id": lease["lease_id"],
            "event_seq": event_seq,
            "receipt_id": f"collaboration-terminal:{assignment_id}:{lease['attempt']}:{event_seq}",
            "terminal_state": terminal_state,
            "transport_outcome": transport_outcome,
            "delivery_outcome": "unresolved",
            "summary": f"machine structured SubAgentActivity {str(observation.get('kind') or '').lower()}",
            "evidence": [],
            "artifacts": [],
            "next_action": "wake same Controller to reconcile child result and recompute runnable work",
            "retry_class": retry_class,
            "result_unknown": result_unknown,
        }
        if int(lease.get("side_effect_contract_version", 1)) >= 2:
            receipt["side_effect"] = bool(lease.get("side_effect"))
            receipt["idempotency_key"] = lease.get("idempotency_key")
        state = apply_runtime_receipt(repo_path, receipt, now=now)
        lease = state["leases"][assignment_id]

    receipt_path = _structured_terminal_receipt_path(repo_path, lease)
    payload = _external_terminal_receipt(repo=repo_path, lease=lease, observation=observation)
    if receipt_path.exists():
        try:
            existing = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"existing structured terminal receipt is unreadable: {exc}") from exc
        for field in ("assignment_id", "task_id", "agent_id", "provider", "model", "agent_type", "session_id", "attempt", "lease_id"):
            if existing.get(field) != payload.get(field):
                raise ValueError("existing structured terminal receipt conflicts with canonical Assignment attempt")
    else:
        _atomic_write_json(receipt_path, payload)
    return {
        "assignment_id": assignment_id,
        "attempt": int(lease["attempt"]),
        "lease_id": lease["lease_id"],
        "terminal_state": lease["terminal_state"],
        "terminal_receipt": str(receipt_path),
        "duplicate": duplicate,
    }


def ingest_structured_subagent_terminal(
    *,
    repo: str | Path,
    assignment_id: str,
    observation: dict[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Compatibility entry point: caller-supplied structured terminal is fail-closed."""
    raise PermissionError(
        "direct structured Web terminal ingest is disabled; use trusted lifecycle reconciliation"
    )


def apply_web_execution_event(
    *,
    repo: str | Path,
    registry_path: str | Path,
    event: dict[str, Any],
    now: datetime | None = None,
    runtime_change_consumer: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compatibility entry point: caller-supplied Web Host events are fail-closed."""
    raise PermissionError(
        "direct Web Host event ingest is disabled; only the installed Host verifier adapter may "
        "invoke the private verified-event path"
    )


def _apply_verified_web_execution_event(
    *,
    repo: str | Path,
    registry_path: str | Path,
    event: dict[str, Any],
    now: datetime | None = None,
    runtime_change_consumer: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Translate one independently Host-verified observation into canonical runtime."""
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
    att = _attestation(event)

    if state == "started":
        if att.get("state") != "running":
            raise ValueError("Web execution start requires host-observed running state")
        dispatch_id = str(event.get("dispatch_id") or "").strip()
        if not dispatch_id:
            raise PermissionError("verified Host started event requires a prepared dispatch ticket")
        ticket = _load_dispatch_state(repo).get("dispatches", {}).get(dispatch_id)
        if (
            not isinstance(ticket, dict)
            or ticket.get("state") != "pending"
            or ticket.get("controller_id") != controller_id
        ):
            raise PermissionError("verified Host started event has no matching pending prepared dispatch ticket")
        assignment = ticket.get("assignment")
        if not isinstance(assignment, dict):
            raise PermissionError("prepared Web dispatch lost its Assignment contract")
        assignment_id = str(ticket.get("assignment_id") or "").strip()
        supplied = event.get("assignment")
        if isinstance(supplied, dict):
            for field in ("assignment_id", "task_id", "provider", "model", "agent_type"):
                if str(supplied.get(field) or "") != str(assignment.get(field) or ""):
                    raise PermissionError(f"Host started Assignment {field} does not match prepared dispatch")
        observed_model = str(event.get("model") or "").strip()
        observed_agent_type = str(event.get("agent_type") or "").strip()
        if observed_model != str(assignment.get("model") or "").strip():
            raise PermissionError("Host-observed Web Agent model does not match prepared Runtime Assignment")
        if observed_agent_type != str(assignment.get("agent_type") or "").strip():
            raise PermissionError("Host-observed Web Agent type does not match prepared Runtime Assignment")
        observed = {
            "source": "chatgpt_host_event",
            "conversation_id": conversation_id,
            "observation_id": str(att.get("observation_id") or ""),
            "call_id": str(event.get("call_id") or "") or None,
            "task_name": str(ticket.get("task_name") or ""),
            "model": observed_model,
            "agent_type": observed_agent_type,
        }
        _persist_observed_dispatch(
            repo=repo,
            dispatch_id=dispatch_id,
            controller_id=controller_id,
            assignment_id=assignment_id,
            observed=observed,
            now=now,
        )
        result = _start_bound_web_assignment(
            repo=repo,
            registry_path=registry,
            controller_id=controller_id,
            conversation_id=conversation_id,
            assignment=assignment,
            dispatch_id=dispatch_id,
            now=now,
            watchdog_launcher=lambda **_: {"launched": False, "reason": "host_event_adapter_owns_observation"},
            health_probe=lambda: True,
        )
        _mark_dispatch_bound(
            repo=repo,
            dispatch_id=dispatch_id,
            conversation_id=conversation_id,
            observation_id=str(att.get("observation_id") or ""),
            call_id=str(event.get("call_id") or "") or None,
            now=now,
        )
        return {**result, "dispatch_id": dispatch_id, "conversation_id": conversation_id}

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
    for name in ("prepare", "recover", "bind"):
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
            if args.command == "prepare":
                result = prepare_web_assignment_dispatch(
                    repo=args.repo, registry_path=args.registry,
                    controller_id=str(event.get("controller_id") or ""),
                    task_name=str(event.get("task_name") or ""),
                    assignment=event.get("assignment") if isinstance(event.get("assignment"), dict) else {},
                )
            elif args.command == "recover":
                result = recover_web_assignment(
                    repo=args.repo, registry_path=args.registry,
                    controller_id=str(event.get("controller_id") or ""),
                    assignment_id=str(event.get("assignment_id") or ""),
                    conversation_id=str(event.get("conversation_id") or ""),
                )
            elif args.command == "bind":
                paths = event.get("event_paths")
                if not isinstance(paths, list) or not all(isinstance(item, str) and item for item in paths):
                    raise ValueError("Web bind requires event_paths")
                result = bind_web_assignment_dispatch(
                    repo=args.repo, registry_path=args.registry,
                    controller_id=str(event.get("controller_id") or ""),
                    dispatch_id=str(event.get("dispatch_id") or ""),
                    event_paths=paths,
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
