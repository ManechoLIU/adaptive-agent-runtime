#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shlex
import tempfile
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPT_DIR.parent
_added_skill_root = False
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))
    _added_skill_root = True
try:
    from scripts import lifecycle_hook as lifecycle
    from scripts import web_lifecycle_bridge as web_bridge
    from scripts.assignment_runtime import load_runtime_state, runtime_state_path
finally:
    if _added_skill_root:
        try:
            sys.path.remove(str(SKILL_ROOT))
        except ValueError:
            pass


def _load_terminal_receipt(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"terminal receipt is unreadable: {exc}") from exc
    if not isinstance(value, dict) or value.get("event_type") != "external_agent_terminal":
        raise ValueError("terminal receipt must be an external_agent_terminal object")
    return value


def _verify_assignment_bound_receipt(repo: Path, receipt: dict[str, Any]) -> None:
    assignment_id = str(receipt.get("assignment_id") or "").strip()
    if not assignment_id:
        return
    required = ("task_id", "agent_id", "session_id", "attempt", "lease_id")
    missing = [field for field in required if receipt.get(field) in (None, "")]
    if missing:
        raise PermissionError("assignment-bound terminal receipt is missing current canonical runtime lease identity")
    lease = load_runtime_state(repo).get("leases", {}).get(assignment_id)
    if not isinstance(lease, dict):
        raise PermissionError("assignment-bound terminal receipt does not match current canonical runtime lease")
    expected = {
        "task_id": str(lease.get("task_id") or ""),
        "agent_id": str(lease.get("agent_id") or ""),
        "session_id": str(lease.get("session_id") or ""),
        "attempt": int(lease.get("attempt", 0)),
        "lease_id": str(lease.get("lease_id") or ""),
    }
    actual = {
        "task_id": str(receipt.get("task_id") or ""),
        "agent_id": str(receipt.get("agent_id") or ""),
        "session_id": str(receipt.get("session_id") or ""),
        "attempt": int(receipt.get("attempt", 0)),
        "lease_id": str(receipt.get("lease_id") or ""),
    }
    if actual != expected:
        raise PermissionError("assignment-bound terminal receipt does not match current canonical runtime lease")
    if not str(lease.get("terminal_state") or "").strip():
        raise PermissionError("assignment-bound terminal receipt requires a canonical runtime terminal state")


def _handoff_unconfirmed_wake_to_existing_supervisor(
    *,
    lifecycle_state: dict[str, Any],
    wake_receipt: dict[str, Any] | None,
    controller_id: str,
    controller_repo: Path,
    registry_path: Path,
    codex: str,
) -> bool:
    if lifecycle_state.get("pending_control_event") is not True:
        return False
    if lifecycle_state.get("requires_user") is True:
        return False
    if web_bridge.wake_receipt_confirmed(wake_receipt):
        return False
    return bool(web_bridge.ensure_continuation_supervisor(
        lifecycle_state=lifecycle_state,
        session_id=controller_id,
        repo=controller_repo,
        registry=registry_path,
        codex=codex,
    ))


def consume_terminal_receipt(
    *,
    repo: Path,
    receipt_path: Path,
    registry_path: Path = web_bridge.DEFAULT_REGISTRY,
    wake_dispatcher: Callable[..., dict[str, Any] | None] | None = None,
    codex: str = "/opt/homebrew/bin/codex",
    dispatch_wake: bool = True,
) -> dict[str, Any]:
    repo = Path(repo).expanduser().resolve()
    receipt_path = Path(receipt_path).expanduser().resolve()
    registry_path = Path(registry_path).expanduser()
    receipt = _load_terminal_receipt(receipt_path)

    receipt_repo = str(receipt.get("repo") or "").strip()
    if not receipt_repo:
        raise ValueError("terminal receipt repository identity is required")
    try:
        if web_bridge._git_common_dir(Path(receipt_repo).expanduser().resolve()) != web_bridge._git_common_dir(repo):
            raise PermissionError("terminal receipt repository does not match continuation repository")
    except Exception as exc:
        if isinstance(exc, PermissionError):
            raise
        raise ValueError(f"cannot verify terminal receipt repository: {exc}") from exc

    _verify_assignment_bound_receipt(repo, receipt)

    controller_id = web_bridge._registered_controller_for_common_dir(repo, registry_path)
    if not controller_id:
        raise PermissionError("terminal continuation requires exactly one registered Controller")
    registry = web_bridge.load_json(registry_path)
    registered_repo = registry.get(controller_id)
    if not isinstance(registered_repo, str) or not registered_repo.strip():
        raise PermissionError("registered Controller repository is missing")
    controller_repo = Path(registered_repo).expanduser().resolve()

    snapshot = lifecycle.project_snapshot(controller_repo)
    if snapshot is None:
        raise RuntimeError("cannot snapshot registered Controller repository")
    state_path = lifecycle.state_path(controller_id)
    agent_id = str(receipt.get("agent_id") or "").strip() or f"external:{receipt_path.stem}"
    event = {
        "hook_event_name": "SubagentStop",
        "session_id": controller_id,
        "event_source": "external_agent_terminal",
        "agent_id": agent_id,
        "cwd": str(controller_repo),
        "terminal_receipt": str(receipt_path),
    }
    _, lifecycle_state = lifecycle.persist_event_state(
        state_path, event, snapshot, preserve_controller_host=True
    )

    wake_receipt = None
    supervisor_armed = False
    if dispatch_wake:
        dispatcher = wake_dispatcher or web_bridge.dispatch_pending_lifecycle_wake
        wake_receipt = dispatcher(
            lifecycle_state=lifecycle_state,
            session_id=controller_id,
            repo=controller_repo,
            registry=registry_path,
            codex=codex,
        )
        supervisor_armed = _handoff_unconfirmed_wake_to_existing_supervisor(
            lifecycle_state=lifecycle_state,
            wake_receipt=wake_receipt,
            controller_id=controller_id,
            controller_repo=controller_repo,
            registry_path=registry_path,
            codex=codex,
        )
    return {
        "controller_id": controller_id,
        "terminal_receipt": str(receipt_path),
        "lifecycle_state_path": str(state_path),
        "wake_result": wake_receipt,
        "pending_control_event": bool(lifecycle_state.get("pending_control_event")),
        "supervisor_armed": supervisor_armed,
    }



def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=path.name + ".tmp.", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _normalized_pending_terminal_paths(state: dict[str, Any]) -> list[str]:
    pending_raw = state.get("pending_terminal_receipts", [])
    if not isinstance(pending_raw, list):
        raise ValueError("pending terminal receipt state is invalid")
    return sorted({
        str(value).strip()
        for value in pending_raw
        if isinstance(value, str) and str(value).strip()
    })


def _terminal_reconcile_execution_fence_locked(
    *, repo: Path, registry_path: Path, controller_id: str
) -> dict[str, Any]:
    registry = web_bridge.load_json(registry_path)
    ownership = web_bridge.target_guard.execution_ownership_record(
        registry, controller_id=controller_id
    )
    if ownership is None:
        raise PermissionError("canonical execution ownership is required for terminal reconciliation")
    ownership_host, ownership_target, ownership_generation = (
        web_bridge.target_guard.validate_execution_ownership_record(ownership)
    )
    if not ownership_host:
        raise PermissionError("canonical execution ownership host is required for terminal reconciliation")
    target_receipt = web_bridge.target_guard.resolve_execution_target(
        repo=repo, host=ownership_host, registry_path=registry_path
    )
    if target_receipt.get("controller_id") != controller_id:
        raise PermissionError("terminal reconciliation target does not belong to the registered Controller")
    if target_receipt.get("execution_target_session_id") != ownership_target:
        raise PermissionError("canonical execution ownership does not match terminal reconciliation target")
    if target_receipt.get("host") != ownership_host:
        raise PermissionError("canonical execution ownership host does not match terminal reconciliation target")
    target_generation = int(target_receipt.get("generation", 0) or 0)
    if target_generation != ownership_generation:
        raise PermissionError(
            "canonical target generation does not match execution ownership generation"
        )
    return {
        "execution_host": ownership_host,
        "execution_target_session_id": ownership_target,
        "target_generation": target_generation,
        "ownership_generation": ownership_generation,
    }


def _terminal_receipt_from_bytes(path: Path, payload: bytes) -> dict[str, Any]:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"terminal receipt is unreadable: {exc}") from exc
    if not isinstance(value, dict) or value.get("event_type") != "external_agent_terminal":
        raise ValueError("terminal receipt must be an external_agent_terminal object")
    return value


def reconcile_pending_terminal_receipts(
    *,
    repo: Path,
    registry_path: Path = web_bridge.DEFAULT_REGISTRY,
) -> dict[str, Any]:
    """Verify already-pending terminal receipts without replaying terminal events."""
    repo = Path(repo).expanduser().resolve()
    registry_path = Path(registry_path).expanduser()
    controller_id = web_bridge._registered_controller_for_common_dir(repo, registry_path)
    if not controller_id:
        raise PermissionError("terminal reconciliation requires exactly one registered Controller")
    registry = web_bridge.load_json(registry_path)
    registered_repo = registry.get(controller_id)
    if not isinstance(registered_repo, str) or not registered_repo.strip():
        raise PermissionError("registered Controller repository is missing")
    controller_repo = Path(registered_repo).expanduser().resolve()

    lifecycle_state_path = lifecycle.state_path(controller_id)
    lifecycle_lock_path = lifecycle_state_path.with_suffix(lifecycle_state_path.suffix + ".lock")
    registry_lock_path = web_bridge.target_guard.registry_lock_path(registry_path)
    lifecycle_lock_path.parent.mkdir(parents=True, exist_ok=True)
    registry_lock_path.parent.mkdir(parents=True, exist_ok=True)

    with lifecycle_lock_path.open("a+") as lifecycle_lock:
        fcntl.flock(lifecycle_lock.fileno(), fcntl.LOCK_SH)
        try:
            with registry_lock_path.open("a+") as registry_lock:
                fcntl.flock(registry_lock.fileno(), fcntl.LOCK_SH)
                try:
                    runtime_lock_path = runtime_state_path(controller_repo).with_name("runtime-assignments.lock")
                    runtime_lock_path.parent.mkdir(parents=True, exist_ok=True)
                    with runtime_lock_path.open("a+") as runtime_lock:
                        fcntl.flock(runtime_lock.fileno(), fcntl.LOCK_SH)
                        try:
                            state = lifecycle.load_json(lifecycle_state_path)
                            pending_paths = _normalized_pending_terminal_paths(state)
                            fence = _terminal_reconcile_execution_fence_locked(
                                repo=controller_repo, registry_path=registry_path, controller_id=controller_id
                            )

                            receipts: list[dict[str, Any]] = []
                            fingerprint_items: list[str] = []
                            for raw_path in pending_paths:
                                receipt_path = Path(raw_path).expanduser().resolve()
                                payload = receipt_path.read_bytes()
                                receipt = _terminal_receipt_from_bytes(receipt_path, payload)
                                receipt_repo = str(receipt.get("repo") or "").strip()
                                if not receipt_repo:
                                    raise ValueError("terminal receipt repository identity is required")
                                try:
                                    if web_bridge._git_common_dir(Path(receipt_repo).expanduser().resolve()) != web_bridge._git_common_dir(controller_repo):
                                        raise PermissionError("terminal receipt repository does not match continuation repository")
                                except Exception as exc:
                                    if isinstance(exc, PermissionError):
                                        raise
                                    raise ValueError(f"cannot verify terminal receipt repository: {exc}") from exc
                                assignment_id = str(receipt.get("assignment_id") or "").strip()
                                verification_state = "verified_legacy_unbound"
                                verification_error = None
                                action_suggestion = "executed"
                                if assignment_id:
                                    try:
                                        _verify_assignment_bound_receipt(controller_repo, receipt)
                                    except PermissionError as exc:
                                        verification_state = "legacy_unverifiable"
                                        verification_error = str(exc)
                                        action_suggestion = "blocked"
                                    else:
                                        verification_state = "verified_current"
                                digest = hashlib.sha256(payload).hexdigest()
                                fingerprint_items.append(f"{receipt_path}:{digest}:{verification_state}")
                                receipts.append({
                                    "path": str(receipt_path),
                                    "sha256": digest,
                                    "assignment_id": assignment_id or None,
                                    "task_id": str(receipt.get("task_id") or "").strip() or None,
                                    "agent_id": str(receipt.get("agent_id") or "").strip() or None,
                                    "session_id": str(receipt.get("session_id") or "").strip() or None,
                                    "attempt": receipt.get("attempt"),
                                    "lease_id": str(receipt.get("lease_id") or "").strip() or None,
                                    "delivery_outcome": str(receipt.get("delivery_outcome") or "").strip() or None,
                                    "exit_code": receipt.get("exit_code"),
                                    "summary": str(receipt.get("summary") or "")[:4000],
                                    "result_path": str(receipt.get("result_path") or "").strip() or None,
                                    "review_verdict": receipt.get("review_verdict") if isinstance(receipt.get("review_verdict"), dict) else None,
                                    "verification_state": verification_state,
                                    "verification_error": verification_error,
                                    "action_suggestion": action_suggestion,
                                })

                            fingerprint_source = "\n".join([
                                controller_id,
                                fence["execution_host"],
                                fence["execution_target_session_id"],
                                str(fence["target_generation"]),
                                str(fence["ownership_generation"]),
                                *sorted(fingerprint_items),
                            ])
                            reconcile_fingerprint = hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest()
                            result = {
                                "schema_version": 1,
                                "operation": "reconcile_pending_terminal_receipts",
                                "controller_id": controller_id,
                                "repo": str(controller_repo),
                                "pending_count": len(receipts),
                                "receipts": receipts,
                                **fence,
                                "reconcile_fingerprint": reconcile_fingerprint,
                                "clears_lifecycle_debt": False,
                                "close_condition": "successful control-cycle receipt",
                            }
                            audit_path = web_bridge._git_common_dir(controller_repo) / "adaptive-delivery" / "terminal-reconcile.json"
                            _write_json_atomic(audit_path, result)
                            return {**result, "audit_path": str(audit_path)}
                        finally:
                            fcntl.flock(runtime_lock.fileno(), fcntl.LOCK_UN)
                finally:
                    fcntl.flock(registry_lock.fileno(), fcntl.LOCK_UN)
        finally:
            fcntl.flock(lifecycle_lock.fileno(), fcntl.LOCK_UN)



def _manual_fenced_web_binding_locked(
    *,
    repo: Path,
    controller_id: str,
    registry_path: Path,
    manual_lease_path: Path,
    now_unix: int,
) -> dict[str, Any]:
    registry = web_bridge.load_json(registry_path)
    target = web_bridge.target_guard.target_record(
        registry, controller_id=controller_id, host="web"
    )
    if not isinstance(target, dict):
        raise PermissionError("manual control reconciliation requires an explicit current Web target")
    status, target_session, target_generation = web_bridge.target_guard.validate_target_record(
        target, host="web"
    )
    if status != "active" or not target_session:
        raise PermissionError("manual control reconciliation requires an active current Web target")
    if not web_bridge.registered_controller_session(
        controller_id=controller_id, session_id=target_session, host="web", registry_path=registry_path
    ):
        raise PermissionError("current Web target is not a member of the registered Controller lineage")
    if (
        target.get("provenance") != "manual_user_authorized"
        or target.get("binding_mode") != "temporary"
        or target.get("host_attested") is not False
    ):
        raise PermissionError(
            "manual control reconciliation is limited to manual_user_authorized temporary non-host-attested targets"
        )
    ownership = web_bridge.target_guard.execution_ownership_record(
        registry, controller_id=controller_id
    )
    if ownership is None:
        raise PermissionError("canonical execution ownership is required for manual control reconciliation")
    ownership_host, ownership_target, ownership_generation = (
        web_bridge.target_guard.validate_execution_ownership_record(ownership)
    )
    if (
        ownership_host != "web"
        or ownership_target != target_session
        or ownership_generation != target_generation
    ):
        raise PermissionError(
            "manual control reconciliation target does not match canonical execution ownership generation"
        )

    lease_payload = web_bridge.load_json(manual_lease_path)
    leases = lease_payload.get("leases") if isinstance(lease_payload, dict) else None
    lease = leases.get(controller_id) if isinstance(leases, dict) else None
    if not isinstance(lease, dict):
        raise PermissionError("an unexpired matching manual Web resume lease is required")
    if (
        lease.get("provenance") != "manual_user_authorized"
        or lease.get("mode") != "resume_only"
        or lease.get("controller_id") not in (None, controller_id)
        or str(lease.get("web_session_id") or "").strip() != target_session
    ):
        raise PermissionError("manual Web resume lease does not match the current Controller target")
    expires_at = lease.get("expires_at_unix")
    authorized_at = lease.get("authorized_at_unix")
    if (
        not isinstance(expires_at, int)
        or isinstance(expires_at, bool)
        or not isinstance(authorized_at, int)
        or isinstance(authorized_at, bool)
        or authorized_at <= 0
        or expires_at <= now_unix
    ):
        raise PermissionError("manual Web resume lease is expired or invalid")
    rotated_at = lease.get("rotated_at_unix")
    if rotated_at is not None:
        if (
            not isinstance(rotated_at, int)
            or isinstance(rotated_at, bool)
            or rotated_at <= 0
            or rotated_at > now_unix
        ):
            raise PermissionError("manual Web resume lease rotation timestamp is invalid")
        lease_effective_at = max(authorized_at, rotated_at)
    else:
        lease_effective_at = authorized_at
    lease_repo_raw = str(lease.get("repo") or "").strip()
    if not lease_repo_raw:
        raise PermissionError("manual Web resume lease repository is missing")
    lease_repo = Path(lease_repo_raw).expanduser().resolve()
    if web_bridge._git_common_dir(lease_repo) != web_bridge._git_common_dir(repo):
        raise PermissionError("manual Web resume lease belongs to another repository")
    return {
        "execution_host": "web",
        "execution_target_session_id": target_session,
        "target_generation": target_generation,
        "ownership_generation": ownership_generation,
        "authorized_at_unix": authorized_at,
        "lease_effective_at_unix": lease_effective_at,
        "expires_at_unix": expires_at,
        "binding_verification": "manual_fenced_not_host_attested",
    }


def _terminal_reconcile_audit_locked(
    *,
    repo: Path,
    controller_id: str,
    pending_paths: list[str],
    binding: dict[str, Any],
) -> tuple[dict[str, Any], Path]:
    audit_path = web_bridge._git_common_dir(repo) / "adaptive-delivery" / "terminal-reconcile.json"
    audit = web_bridge.load_json(audit_path)
    if not audit:
        raise PermissionError("terminal-reconcile audit is required before control-cycle closure")
    expected = {
        "controller_id": controller_id,
        "repo": str(repo.resolve()),
        "execution_host": "web",
        "execution_target_session_id": binding["execution_target_session_id"],
        "target_generation": binding["target_generation"],
        "ownership_generation": binding["ownership_generation"],
        "clears_lifecycle_debt": False,
        "close_condition": "successful control-cycle receipt",
    }
    for field, value in expected.items():
        if audit.get(field) != value:
            raise PermissionError(f"terminal-reconcile audit {field} does not match current fenced state")
    receipts = audit.get("receipts")
    if not isinstance(receipts, list) or int(audit.get("pending_count", -1)) != len(pending_paths):
        raise PermissionError("terminal-reconcile audit does not match current pending terminal debt")
    by_path = {
        str(item.get("path") or "").strip(): item
        for item in receipts
        if isinstance(item, dict) and str(item.get("path") or "").strip()
    }
    if set(by_path) != set(pending_paths):
        raise PermissionError("terminal-reconcile audit receipt set does not match lifecycle pending debt")
    for raw_path in pending_paths:
        path = Path(raw_path).expanduser().resolve()
        item = by_path[raw_path]
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if item.get("sha256") != digest:
            raise PermissionError("terminal-reconcile audit receipt hash is stale")
        verification_state = str(item.get("verification_state") or "")
        action = str(item.get("action_suggestion") or "")
        if verification_state == "legacy_unverifiable":
            if action != "blocked":
                raise PermissionError("legacy_unverifiable terminal receipt must remain blocked")
        elif verification_state in {"verified_current", "verified_legacy_unbound"}:
            if action != "executed":
                raise PermissionError("verified terminal receipt must remain executed in reconciliation")
        else:
            raise PermissionError("terminal-reconcile audit contains unsupported verification state")
    return audit, audit_path


def _control_snapshot_path(command: str, *, repo: Path, controller_id: str) -> Path:
    if not lifecycle._is_control_guard_command(
        command, controller_session_id=controller_id, cwd=repo
    ):
        raise PermissionError("AI-Bridge audit receipt is not a canonical control_event_guard command")
    tokens = shlex.split(command)
    if len(tokens) < 3 or tokens[2] == "-" or tokens[2].startswith("-"):
        raise PermissionError("manual control reconciliation requires a durable control-cycle snapshot file")
    path = Path(tokens[2]).expanduser()
    if not path.is_absolute():
        path = (repo / path).resolve()
    return path.resolve()


def _validate_control_snapshot_reconciles_terminal_debt(
    *, snapshot_path: Path, reconcile_path: Path, pending_paths: list[str]
) -> dict[str, Any]:
    try:
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PermissionError(f"control-cycle snapshot is unavailable: {exc}") from exc
    if not isinstance(snapshot, dict):
        raise PermissionError("control-cycle snapshot must be an object")
    evidence = "artifact:" + str(reconcile_path.resolve())
    capacity = snapshot.get("capacity_projection")
    if not isinstance(capacity, dict) or capacity.get("evidence") != evidence:
        raise PermissionError("control-cycle snapshot is not bound to the current terminal-reconcile audit")
    expected_action_ids = {
        "terminal_receipt:" + hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]
        for path in pending_paths
    }
    loop = snapshot.get("control_loop_receipt")
    if not isinstance(loop, dict):
        raise PermissionError("control-cycle snapshot is missing control_loop_receipt")
    for field in ("controller_action_ids", "continuation_debt_ids"):
        values = loop.get(field)
        if not isinstance(values, list) or {str(item) for item in values} != expected_action_ids:
            raise PermissionError(f"control-cycle snapshot {field} does not match current terminal debt")
    open_debt = loop.get("open_continuation_debt_ids")
    if not isinstance(open_debt, list) or open_debt:
        raise PermissionError("control-cycle snapshot still has open terminal continuation debt")
    actions = snapshot.get("controller_actions")
    if not isinstance(actions, list):
        raise PermissionError("control-cycle snapshot is missing controller_actions")
    seen: set[str] = set()
    for action in actions:
        if not isinstance(action, dict):
            raise PermissionError("control-cycle snapshot contains malformed controller action")
        action_id = str(action.get("id") or "").strip()
        if action_id not in expected_action_ids:
            raise PermissionError("control-cycle snapshot contains non-terminal controller action")
        if action_id in seen:
            raise PermissionError("control-cycle snapshot contains duplicate controller action")
        seen.add(action_id)
        if action.get("evidence") != evidence:
            raise PermissionError("terminal controller action is not bound to terminal-reconcile audit")
        if str(action.get("decision") or "").strip().lower() not in {"executed", "blocked"}:
            raise PermissionError("terminal controller action is not fully resolved")
    if seen != expected_action_ids:
        raise PermissionError("control-cycle snapshot omitted terminal controller actions")
    return snapshot


def _latest_allowed_control_receipt(
    *,
    audit_log: Path,
    repo: Path,
    controller_id: str,
    web_session_id: str,
    lifecycle_snapshot: dict[str, Any],
    authorized_at_unix: int,
    expires_at_unix: int,
    reconcile_path: Path,
    pending_paths: list[str],
) -> tuple[dict[str, Any], dict[str, Any], Path, dict[str, Any]]:
    if not audit_log.is_file():
        raise PermissionError("AI-Bridge durable audit log is unavailable")
    selected: tuple[int, dict[str, Any], dict[str, Any], Path, dict[str, Any]] | None = None
    with audit_log.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                receipt = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(receipt, dict):
                continue
            occurred_ms = int(receipt.get("occurredAtUnixMs", 0) or 0)
            if occurred_ms < authorized_at_unix * 1000 or occurred_ms >= expires_at_unix * 1000:
                continue
            event = web_bridge.translate_receipt(
                receipt, session_id=controller_id, repo=repo, web_session_id=web_session_id
            )
            if event is None or not lifecycle.successful_control_receipt(event, lifecycle_snapshot):
                continue
            command = web_bridge.extract_command(receipt)
            try:
                control_snapshot_path = _control_snapshot_path(
                    command, repo=repo, controller_id=controller_id
                )
                control_snapshot = _validate_control_snapshot_reconciles_terminal_debt(
                    snapshot_path=control_snapshot_path,
                    reconcile_path=reconcile_path,
                    pending_paths=pending_paths,
                )
            except PermissionError:
                continue
            if selected is None or occurred_ms > selected[0]:
                selected = (occurred_ms, receipt, event, control_snapshot_path, control_snapshot)
    if selected is None:
        raise PermissionError(
            "no durable AI-Bridge control-event allowed receipt is bound to the current terminal reconciliation"
        )
    return selected[1], selected[2], selected[3], selected[4]


def _immutable_closed_cycle_evidence(
    *, repo: Path, controller_id: str, control_snapshot: dict[str, Any]
) -> tuple[dict[str, Any], Path]:
    contract = control_snapshot.get("event_contract")
    if not isinstance(contract, dict):
        raise PermissionError("control-cycle snapshot is missing event_contract")
    if str(contract.get("event_type") or "").strip() != "terminal_debt_reconciliation":
        raise PermissionError(
            "manual terminal closure requires event_type=terminal_debt_reconciliation"
        )
    cycle_id = str(contract.get("event_id") or "").strip()
    if not cycle_id or len(cycle_id) > 256:
        raise PermissionError("control-cycle snapshot event_id is missing or invalid")
    digest = hashlib.sha256(cycle_id.encode("utf-8")).hexdigest()
    evidence_path = (
        web_bridge._git_common_dir(repo)
        / "adaptive-delivery"
        / "controller-cycle-evidence"
        / f"{digest}.json"
    )
    evidence = web_bridge.load_json(evidence_path)
    if not evidence:
        raise PermissionError("immutable CLOSED controller cycle evidence is required")
    canonical_snapshot = json.dumps(
        control_snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    expected_hash = hashlib.sha256(canonical_snapshot).hexdigest()
    checks = {
        "record_kind": "controller_cycle_evidence",
        "controller_id": controller_id,
        "cycle_id": cycle_id,
        "evidence_id": cycle_id,
        "terminal_status": "CLOSED",
        "snapshot_sha256": expected_hash,
        "event_type": str(contract.get("event_type") or "").strip(),
        "primary_task": str(contract.get("primary_task") or "").strip(),
    }
    for field, value in checks.items():
        if evidence.get(field) != value:
            raise PermissionError(f"immutable controller cycle evidence {field} does not match the allowed snapshot")
    validation_errors = evidence.get("validation_errors")
    if not isinstance(validation_errors, list) or validation_errors:
        raise PermissionError("immutable controller cycle evidence is not a clean CLOSED result")
    snapshot_ledger = str(control_snapshot.get("ledger_sha256") or "").strip()
    if snapshot_ledger and evidence.get("ledger_sha256") != snapshot_ledger:
        raise PermissionError("immutable controller cycle evidence ledger hash does not match the allowed snapshot")
    candidate_revision = str(contract.get("candidate_revision") or "").strip()
    if candidate_revision and evidence.get("main_revision") != candidate_revision:
        raise PermissionError("immutable controller cycle evidence main revision does not match the allowed snapshot")
    return evidence, evidence_path


def _persist_reconciled_control_event_locked(
    *,
    state_path: Path,
    event: dict[str, Any],
    snapshot: dict[str, Any],
    previous: dict[str, Any],
    closure_evidence: dict[str, Any],
) -> dict[str, Any]:
    # A historical allowed control receipt proves only that the reconciled terminal debt
    # was closed at that earlier control boundary. It must never replay the generic
    # successful-control branch because doing so would erase facts that appeared later.
    # Rebuild canonical current triggers from the fresh snapshot, then preserve only
    # non-terminal lifecycle edges that cannot be reconstructed from snapshot fields.
    next_state = dict(previous)
    next_state["snapshot"] = snapshot
    next_state["pending_terminal_receipts"] = []

    current_triggers = set(lifecycle.lifecycle_triggers(snapshot, None))
    preserved_edge_triggers = {
        "main_head_changed",
        "ledger_changed",
        "main_worktree_changed",
        "ready_set_changed",
        "candidate_queue_changed",
        "YIELD_GATE_REJECTED",
        "KNOWN_NEXT_ACTION_NOT_EXECUTED",
        "post_receipt_action_started",
        "RUNTIME_CONTINUATION_DEBT",
    }
    for raw in previous.get("triggers", []):
        value = str(raw).strip()
        if value in preserved_edge_triggers:
            current_triggers.add(value)

    pending_next_action = str(previous.get("next_action") or "").strip()
    continuation_pending = bool(pending_next_action) and previous.get("requires_user") is False
    if continuation_pending:
        current_triggers.add("next_action_pending")

    next_state["triggers"] = sorted(current_triggers)
    next_state["pending_control_event"] = bool(
        current_triggers
        or continuation_pending
        or previous.get("requires_user") is True
    )
    next_state["manual_control_reconcile"] = dict(closure_evidence)
    lifecycle.write_json(state_path, next_state)
    return next_state


def reconcile_control_cycle_closure(
    *,
    repo: Path,
    registry_path: Path = web_bridge.DEFAULT_REGISTRY,
    audit_log: Path = web_bridge.DEFAULT_AUDIT_LOG,
    manual_lease_path: Path = web_bridge.DEFAULT_MANUAL_WEB_LEASES,
    now_unix: int | None = None,
) -> dict[str, Any]:
    """Close already-reconciled terminal debt under a manual fenced Web binding.

    This is intentionally narrower than translate-receipt: it never establishes or
    upgrades Web identity and accepts no caller-supplied receipt or Web session id.
    """
    repo = Path(repo).expanduser().resolve()
    registry_path = Path(registry_path).expanduser()
    audit_log = Path(audit_log).expanduser()
    manual_lease_path = Path(manual_lease_path).expanduser()
    controller_id = web_bridge._registered_controller_for_common_dir(repo, registry_path)
    if not controller_id:
        raise PermissionError("manual control reconciliation requires exactly one registered Controller")
    state_path = lifecycle.state_path(controller_id)
    lifecycle_lock_path = state_path.with_suffix(state_path.suffix + ".lock")
    registry_lock_path = web_bridge.target_guard.registry_lock_path(registry_path)
    runtime_lock_path = runtime_state_path(repo).with_name("runtime-assignments.lock")
    manual_lock_path = manual_lease_path.with_suffix(manual_lease_path.suffix + ".lock")
    for path in (lifecycle_lock_path, registry_lock_path, runtime_lock_path, manual_lock_path):
        path.parent.mkdir(parents=True, exist_ok=True)
    now = int(time.time() if now_unix is None else now_unix)

    with lifecycle_lock_path.open("a+") as lifecycle_lock:
        fcntl.flock(lifecycle_lock.fileno(), fcntl.LOCK_EX)
        try:
            with registry_lock_path.open("a+") as registry_lock:
                fcntl.flock(registry_lock.fileno(), fcntl.LOCK_SH)
                try:
                    with runtime_lock_path.open("a+") as runtime_lock:
                        fcntl.flock(runtime_lock.fileno(), fcntl.LOCK_SH)
                        try:
                            with manual_lock_path.open("a+") as manual_lock:
                                fcntl.flock(manual_lock.fileno(), fcntl.LOCK_SH)
                                try:
                                    previous = lifecycle.load_json(state_path)
                                    pending_paths = _normalized_pending_terminal_paths(previous)
                                    if not pending_paths:
                                        prior_closure = previous.get("manual_control_reconcile")
                                        if (
                                            isinstance(prior_closure, dict)
                                            and prior_closure.get("operation") == "reconcile_control_cycle_closure"
                                            and prior_closure.get("controller_id") == controller_id
                                            and prior_closure.get("repo") == str(repo)
                                            and prior_closure.get("host_attested") is False
                                            and prior_closure.get("strong_web_identity_established") is False
                                            and int(prior_closure.get("closed_terminal_receipt_count", 0) or 0) > 0
                                        ):
                                            return {
                                                **prior_closure,
                                                "pending_control_event": bool(previous.get("pending_control_event")),
                                                "lifecycle_evidence_path": str(state_path),
                                                "idempotent": True,
                                            }
                                        raise PermissionError("no pending terminal debt exists for control-cycle reconciliation")
                                    binding = _manual_fenced_web_binding_locked(
                                        repo=repo, controller_id=controller_id,
                                        registry_path=registry_path,
                                        manual_lease_path=manual_lease_path, now_unix=now,
                                    )
                                    terminal_audit, terminal_audit_path = _terminal_reconcile_audit_locked(
                                        repo=repo, controller_id=controller_id,
                                        pending_paths=pending_paths, binding=binding,
                                    )
                                    snapshot = lifecycle.project_snapshot(repo)
                                    if snapshot is None:
                                        raise RuntimeError("cannot snapshot Controller repository for control reconciliation")
                                    receipt, event, control_snapshot_path, control_snapshot = _latest_allowed_control_receipt(
                                        audit_log=audit_log, repo=repo, controller_id=controller_id,
                                        web_session_id=binding["execution_target_session_id"],
                                        lifecycle_snapshot=snapshot,
                                        authorized_at_unix=binding["lease_effective_at_unix"],
                                        expires_at_unix=binding["expires_at_unix"],
                                        reconcile_path=terminal_audit_path,
                                        pending_paths=pending_paths,
                                    )
                                    cycle_evidence, cycle_evidence_path = _immutable_closed_cycle_evidence(
                                        repo=repo, controller_id=controller_id,
                                        control_snapshot=control_snapshot,
                                    )
                                    closure_evidence = {
                                        "schema_version": 1,
                                        "operation": "reconcile_control_cycle_closure",
                                        "controller_id": controller_id,
                                        "repo": str(repo),
                                        "control_receipt_id": str(receipt.get("receiptId") or ""),
                                        "control_snapshot_path": str(control_snapshot_path),
                                        "controller_cycle_evidence_path": str(cycle_evidence_path),
                                        "controller_cycle_snapshot_sha256": cycle_evidence.get("snapshot_sha256"),
                                        "terminal_reconcile_fingerprint": terminal_audit.get("reconcile_fingerprint"),
                                        "closed_terminal_receipt_count": len(pending_paths),
                                        **binding,
                                        "host_attested": False,
                                        "strong_web_identity_established": False,
                                        "reconciled_at_unix": now,
                                    }
                                    next_state = _persist_reconciled_control_event_locked(
                                        state_path=state_path, event=event,
                                        snapshot=snapshot, previous=previous,
                                        closure_evidence=closure_evidence,
                                    )
                                    closure = {
                                        **closure_evidence,
                                        "pending_control_event": bool(next_state.get("pending_control_event")),
                                    }
                                    closure_path = web_bridge._git_common_dir(repo) / "adaptive-delivery" / "control-reconcile.json"
                                    try:
                                        _write_json_atomic(closure_path, closure)
                                        audit_path_value: str | None = str(closure_path)
                                    except OSError:
                                        audit_path_value = None
                                    return {
                                        **closure,
                                        "audit_path": audit_path_value,
                                        "lifecycle_evidence_path": str(state_path),
                                        "idempotent": False,
                                    }
                                finally:
                                    fcntl.flock(manual_lock.fileno(), fcntl.LOCK_UN)
                        finally:
                            fcntl.flock(runtime_lock.fileno(), fcntl.LOCK_UN)
                finally:
                    fcntl.flock(registry_lock.fileno(), fcntl.LOCK_UN)
        finally:
            fcntl.flock(lifecycle_lock.fileno(), fcntl.LOCK_UN)

def notify_runtime_change(
    *,
    repo: Path,
    registry_path: Path = web_bridge.DEFAULT_REGISTRY,
    wake_dispatcher: Callable[..., dict[str, Any] | None] | None = None,
    codex: str = "/opt/homebrew/bin/codex",
    event_source: str = "assignment_runtime_watchdog",
) -> dict[str, Any]:
    """Recompute canonical lifecycle state after a non-terminal runtime health change."""
    repo = Path(repo).expanduser().resolve()
    registry_path = Path(registry_path).expanduser()
    controller_id = web_bridge._registered_controller_for_common_dir(repo, registry_path)
    if not controller_id:
        raise PermissionError("runtime continuation requires exactly one registered Controller")
    registry = web_bridge.load_json(registry_path)
    registered_repo = registry.get(controller_id)
    if not isinstance(registered_repo, str) or not registered_repo.strip():
        raise PermissionError("registered Controller repository is missing")
    controller_repo = Path(registered_repo).expanduser().resolve()
    snapshot = lifecycle.project_snapshot(controller_repo)
    if snapshot is None:
        raise RuntimeError("cannot snapshot registered Controller repository")
    state_path = lifecycle.state_path(controller_id)
    event = {
        "hook_event_name": "AssignmentRuntime",
        "session_id": controller_id,
        "event_source": event_source,
        "cwd": str(controller_repo),
    }
    _, lifecycle_state = lifecycle.persist_event_state(
        state_path, event, snapshot, preserve_controller_host=True
    )
    wake_receipt = None
    supervisor_armed = False
    if lifecycle_state.get("pending_control_event") is True:
        dispatcher = wake_dispatcher or web_bridge.dispatch_pending_lifecycle_wake
        wake_receipt = dispatcher(
            lifecycle_state=lifecycle_state, session_id=controller_id, repo=controller_repo,
            registry=registry_path, codex=codex,
        )
        supervisor_armed = _handoff_unconfirmed_wake_to_existing_supervisor(
            lifecycle_state=lifecycle_state,
            wake_receipt=wake_receipt,
            controller_id=controller_id,
            controller_repo=controller_repo,
            registry_path=registry_path,
            codex=codex,
        )
    return {
        "controller_id": controller_id,
        "lifecycle_state_path": str(state_path),
        "wake_result": wake_receipt,
        "pending_control_event": bool(lifecycle_state.get("pending_control_event")),
        "supervisor_armed": supervisor_armed,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Continue the existing Controller from a durable external-agent terminal receipt")
    subparsers = parser.add_subparsers(dest="command", required=True)
    consume = subparsers.add_parser("consume")
    consume.add_argument("--repo", required=True)
    consume.add_argument("--receipt", required=True)
    consume.add_argument("--registry", default=str(web_bridge.DEFAULT_REGISTRY))
    consume.add_argument("--codex", default="/opt/homebrew/bin/codex")
    reconcile = subparsers.add_parser("reconcile-pending")
    reconcile.add_argument("--repo", required=True)
    reconcile.add_argument("--registry", default=str(web_bridge.DEFAULT_REGISTRY))
    close_cycle = subparsers.add_parser("reconcile-control-cycle")
    close_cycle.add_argument("--repo", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "reconcile-control-cycle":
        try:
            result = reconcile_control_cycle_closure(repo=Path(args.repo))
        except (OSError, ValueError, PermissionError, RuntimeError) as exc:
            print(str(exc), file=sys.stderr)
            return 78
        print(json.dumps(result, ensure_ascii=False))
        return 0
    if args.command == "reconcile-pending":
        try:
            result = reconcile_pending_terminal_receipts(
                repo=Path(args.repo), registry_path=Path(args.registry)
            )
        except (OSError, ValueError, PermissionError, RuntimeError) as exc:
            print(str(exc), file=sys.stderr)
            return 78
        print(json.dumps(result, ensure_ascii=False))
        return 0
    if args.command != "consume":
        return 2
    wake_child = os.environ.get("AD_TERMINAL_CONTINUATION_WAKE_CHILD") == "1"
    try:
        result = consume_terminal_receipt(
            repo=Path(args.repo),
            receipt_path=Path(args.receipt),
            registry_path=Path(args.registry),
            codex=args.codex,
            dispatch_wake=wake_child,
        )
    except (OSError, ValueError, PermissionError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 78
    print(json.dumps(result, ensure_ascii=False))
    if wake_child:
        # The durable terminal result is already staged. The first wake either succeeded
        # or was handed to the existing lifecycle continuation supervisor.
        return 0
    env = dict(os.environ)
    env["AD_TERMINAL_CONTINUATION_WAKE_CHILD"] = "1"
    try:
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "consume", "--repo", args.repo,
             "--receipt", args.receipt, "--registry", args.registry, "--codex", args.codex],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=env, start_new_session=True, close_fds=True,
        )
    except OSError as exc:
        # The pending lifecycle state remains durable for the existing Supervisor's next
        # observation; do not turn a completed Agent into a failed Assignment attempt.
        print(f"terminal wake launch deferred: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
