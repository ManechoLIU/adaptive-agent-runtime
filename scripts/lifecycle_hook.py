#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

try:
    from lint_governance import task_records, task_rows
except ModuleNotFoundError:
    from scripts.lint_governance import task_records, task_rows
try:
    from controller_state import derive_runnable_tasks, project_task_state
except ModuleNotFoundError:
    from scripts.controller_state import derive_runnable_tasks, project_task_state
try:
    from control_event_guard import snapshot_continuation_debt_labels
except ModuleNotFoundError:
    from scripts.control_event_guard import snapshot_continuation_debt_labels

try:
    from rule_handshake import derive_rule_wake_policy, evaluate_rule_handshake
except ModuleNotFoundError:
    from scripts.rule_handshake import derive_rule_wake_policy, evaluate_rule_handshake

try:
    from controller_self_check import render_controller_self_check
except ModuleNotFoundError:
    from scripts.controller_self_check import render_controller_self_check

try:
    import controller_target_guard as target_guard
except ModuleNotFoundError:
    from scripts import controller_target_guard as target_guard
try:
    import agent_target_resolution as agent_target
except ModuleNotFoundError:
    from scripts import agent_target_resolution as agent_target
try:
    import goal_display_sync
except ModuleNotFoundError:
    from scripts import goal_display_sync


STATE_ROOT = Path(
    os.environ.get(
        "AD_LIFECYCLE_STATE_DIR",
        str(Path.home() / ".codex" / "state" / "adaptive-delivery-lifecycle"),
    )
).expanduser()
REGISTRY_PATH = Path(
    os.environ.get(
        "AD_CONTROLLER_REGISTRY",
        str(Path.home() / ".codex" / "adaptive-delivery-controllers.json"),
    )
).expanduser()
LEDGER_NAMES = ("TASK_LEDGER.md", "PROJECT_STATUS.md")
CONTROLLER_SURFACES_KEY = "__controller_surfaces__"
CONTROLLER_SESSIONS_KEY = "__controller_sessions__"
CONTROLLER_TARGETS_KEY = "__controller_targets__"
DESKTOP_SESSION_HOST = "desktop_codex"
MAX_TOOL_TRACE_ENTRIES = 128
# First Stop with Continuation Debt must block. A later Stop on a non-duty
# host may end the physical Host turn only after same-controller Desktop
# reentry is CONFIRMED. Desktop on-duty never uses that escape hatch.
MIN_STOPS_BEFORE_CONFIRMED_HOST_END = 2
FORBIDDEN_REENTRY_HANDOFF_STATES = {"requested", "delegated", "degraded"}
FORBIDDEN_REENTRY_DELIVERY_STATES = {
    "supervisor_already_managing_or_deferred",
    "supervisor_arm_failed",
}
FORBIDDEN_REENTRY_RESUME_STATES = {
    "RESUME_DEFERRED_ACTIVE_WRITER",
    "RESUME_SUPERSEDED",
    "RESUME_FAILED",
    "DESKTOP_HOST_ADAPTER_UNAVAILABLE",
}
CONFIRMED_DESKTOP_REENTRY_RESULTS = {"CONFIRMED"}
CONFIRMED_DESKTOP_REENTRY_STATES = {
    "RESUME_SUCCEEDED",
    "reentry_confirmed",
    "CONFIRMED",
}
RUNTIME_WEB_TURN_LEASE_CONTRACT = "runtime_web_turn_lease_v1"
LEGACY_WEB_TURN_IDS = {"web-ai-bridge"}
DESKTOP_CANARY_PATH = Path(
    os.environ.get(
        "AD_DESKTOP_CANARY_PATH",
        str(Path.home() / ".codex" / "state" / "adaptive-delivery-desktop-canary.json"),
    )
).expanduser()
CODEX_HOOKS_PATH = Path(
    os.environ.get("AD_CODEX_HOOKS_PATH", str(Path.home() / ".codex" / "hooks.json"))
).expanduser()
DESKTOP_CANARY_SEQUENCE = (
    "pre_tool_allowed",
    "post_tool_observed",
    "receipt_latched",
    "same_turn_continuation_invalidated_receipt",
    "post_invalidation_tool_observed",
    "stop_observed",
    "post_stop_receipt_latched",
    "post_stop_continuation_invalidated_receipt",
)
DESKTOP_CANARY_OBSERVATIONS = set(DESKTOP_CANARY_SEQUENCE)



def controller_self_check_context() -> str:
    skill_root = Path(__file__).resolve().parents[1]
    try:
        return render_controller_self_check(skill_root)
    except (OSError, UnicodeDecodeError, ValueError) as error:
        return f"Controller Self-Check unavailable from installed scoring model: {error}; do not infer or calculate a score."

def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json_sha256(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return sha256_bytes(encoded)


def _event_turn_id(event: dict[str, Any]) -> str:
    return str(event.get("turn_id", "")).strip()


def _host_owned_desktop_subagent_event(event: dict[str, Any]) -> bool:
    """Keep Host-created worker turns out of the Controller turn ledger.

    Desktop subagents inherit the parent task's ``session_id`` in Hook input,
    while their Host-owned rollout has a distinct ``session_meta.payload.id``.
    Treat the mismatch as a subagent only when the rollout itself proves both
    the inherited parent session and subagent provenance. Unknown or forged
    mismatches stay on the normal fail-closed Controller path.
    """
    parent_session_id = str(event.get("session_id") or "").strip()
    transcript = str(event.get("transcript_path") or "").strip()
    if not parent_session_id or not transcript:
        return False
    sessions = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "sessions"
    try:
        path = Path(transcript).resolve(strict=True)
        if not path.is_relative_to(sessions.resolve()):
            return False
        with path.open("rb") as stream:
            header = json.loads(stream.readline(65536))
        if header.get("type") != "session_meta":
            return False
        meta = header.get("payload")
        if not isinstance(meta, dict):
            return False
        execution_session_id = str(meta.get("id") or "").strip()
        inherited_session_id = str(meta.get("session_id") or "").strip()
        source = meta.get("source")
        subagent = source.get("subagent") if isinstance(source, dict) else None
        thread_spawn = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
        return bool(
            execution_session_id
            and execution_session_id != parent_session_id
            and inherited_session_id == parent_session_id
            and str(meta.get("thread_source") or "").strip() == "subagent"
            and isinstance(thread_spawn, dict)
        )
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def _desktop_turn_start(event: dict[str, Any]) -> dict[str, str] | None:
    """Attest a delegated turn against host-owned rollout records, not tool input.

    Codex task-to-task input need not emit UserPromptSubmit. The rollout format
    is not a stable API: unknown/missing/bounded-out evidence fails closed.
    This never synthesizes a SessionStart or a completed tool result.
    """
    if event.get("controller_host") != DESKTOP_SESSION_HOST:
        return None
    if event.get("hook_event_name") not in {"PreToolUse", "Stop"}:
        return None
    source_id = str(event.get("source_session_id") or "").strip()
    transcript = str(event.get("transcript_path") or "").strip()
    if not source_id or not transcript:
        return None
    sessions = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "sessions"
    try:
        path = Path(transcript).resolve(strict=True)
        if not path.is_relative_to(sessions.resolve()):
            return None
        with path.open("rb") as stream:
            header = json.loads(stream.readline(65536))
            if header.get("type") != "session_meta" or header.get("payload", {}).get("id") != source_id:
                return None
            size = stream.seek(0, os.SEEK_END)
            offset = max(0, size - 8 * 1024 * 1024)
            stream.seek(offset)
            if offset:
                stream.readline()  # Drop the partial first record.
            raw = stream.read()
        for line in reversed(raw.splitlines()):
            row = json.loads(line)
            if row.get("type") != "event_msg":
                continue
            payload = row.get("payload", {})
            kind = payload.get("type")
            if kind in {"turn_aborted", "task_complete"}:
                return None
            if kind == "task_started":
                if payload.get("turn_id") != _event_turn_id(event):
                    return None
                return {
                    "source": "codex_rollout_task_started",
                    "source_session_id": source_id,
                    "turn_id": _event_turn_id(event),
                    "transcript_path": str(path),
                    "record_sha256": sha256_bytes(line),
                }
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    return None


def _desktop_rollout_completed_items(
    event: dict[str, Any], state: dict[str, Any]
) -> list[dict[str, Any]]:
    """Return only current-turn terminal recoverable items from a trusted rollout."""
    if event.get("controller_host") != DESKTOP_SESSION_HOST:
        return []
    if event.get("hook_event_name") not in {"PreToolUse", "Stop"}:
        return []
    turn_id = _event_turn_id(event)
    if not turn_id or turn_id != str(state.get("active_turn_id", "")).strip():
        return []
    source_id = str(event.get("source_session_id") or "").strip()
    transcript = str(event.get("transcript_path") or "").strip()
    if not source_id or not transcript:
        return []
    sessions = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "sessions"
    try:
        path = Path(transcript).resolve(strict=True)
        if not path.is_relative_to(sessions.resolve()):
            return []
        with path.open("rb") as stream:
            header = json.loads(stream.readline(65536))
            if (
                header.get("type") != "session_meta"
                or header.get("payload", {}).get("id") != source_id
            ):
                return []
            size = stream.seek(0, os.SEEK_END)
            offset = max(0, size - 8 * 1024 * 1024)
            tail_was_truncated = offset > 0
            stream.seek(offset)
            if offset:
                stream.readline()
            raw = stream.read()
        started = False
        non_target_start_seen = False
        completed: list[dict[str, Any]] = []
        for line in raw.splitlines():
            row = json.loads(line)
            if row.get("type") != "event_msg":
                continue
            payload = row.get("payload")
            if not isinstance(payload, dict):
                continue
            kind = payload.get("type")
            payload_turn_id = str(payload.get("turn_id") or "").strip()
            if kind == "task_started":
                if payload_turn_id != turn_id:
                    # Once the target boundary has begun, or an unbounded
                    # completion has appeared, another turn start is later
                    # evidence and closes this recovery window.
                    if started or completed:
                        return []
                    # Before the target boundary, an observed non-target start
                    # may be an older turn. Retain that bounded state until a
                    # target start can establish a fresh boundary.
                    non_target_start_seen = True
                    continue
                # A completion before an observed target start cannot be
                # attributed to the freshly established target boundary.
                if completed:
                    return []
                started = True
                continue
            if payload_turn_id != turn_id:
                continue
            if kind in {"turn_aborted", "task_complete"}:
                return []
            if kind != "item_completed":
                continue
            item = payload.get("item")
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type") or "").strip()
            if not str(item.get("id") or "").strip():
                continue
            if item_type == "CommandExecution":
                terminal = str(item.get("status") or "").strip().lower()
                if terminal not in {"completed", "failed"}:
                    continue
            elif item_type != "FileChange":
                continue
            # Without a target start, a preceding non-target start proves this
            # completion is outside the only trusted bounded window.
            if non_target_start_seen and not started:
                return []
            completed.append(dict(item))
        if started:
            return completed
        return completed if tail_was_truncated and not non_target_start_seen else []
    except (OSError, ValueError, TypeError, AttributeError):
        return []


def _rollout_command_text(item: dict[str, Any]) -> str | None:
    command = item.get("command")
    if not isinstance(command, list) or len(command) != 3:
        return None
    shell = Path(str(command[0])).name
    if shell not in {"bash", "sh", "zsh"} or command[1] not in {"-c", "-lc"}:
        return None
    value = command[2]
    return value if isinstance(value, str) and value else None


def _rollout_tool_response(item: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item[key]
        for key in (
            "status", "stdout", "stderr", "aggregated_output", "formatted_output",
            "exit_code", "duration",
        )
        if key in item
    }


def _runtime_web_turn_lease(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    if value.get("contract") != RUNTIME_WEB_TURN_LEASE_CONTRACT:
        return None
    status = value.get("status")
    if status not in {"active", "ended"}:
        return None
    generation = value.get("generation")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        return None
    turn_id = str(value.get("turn_id") or "").strip()
    invocation_id = str(value.get("runtime_invocation_id") or "").strip()
    session_id = str(value.get("execution_target_session_id") or "").strip()
    target_generation = value.get("target_generation")
    ownership_generation = value.get("ownership_generation")
    if (
        not turn_id or not invocation_id or not session_id
        or isinstance(target_generation, bool) or not isinstance(target_generation, int) or target_generation < 1
        or isinstance(ownership_generation, bool) or not isinstance(ownership_generation, int) or ownership_generation < 1
    ):
        return None
    if status == "ended":
        ended_at = str(value.get("ended_at") or "").strip()
        end_reason = str(value.get("end_reason") or "").strip()
        evidence = str(value.get("end_evidence_sha256") or "").strip().lower()
        if (
            not ended_at or not end_reason or len(evidence) != 64
            or any(ch not in "0123456789abcdef" for ch in evidence)
        ):
            return None
    return dict(value)


def _runtime_web_turn_lease_matches_event(lease: dict[str, Any], event: dict[str, Any]) -> bool:
    return (
        str(lease.get("turn_id") or "").strip() == _event_turn_id(event)
        and str(lease.get("execution_target_session_id") or "").strip()
        == str(event.get("web_session_id") or event.get("source_session_id") or "").strip()
        and lease.get("target_generation") == event.get("controller_target_generation")
        and lease.get("ownership_generation") == event.get("controller_ownership_generation")
    )


def _validate_runtime_web_turn_lease_transition(
    state: dict[str, Any], event: dict[str, Any], incoming: dict[str, Any]
) -> str | None:
    if incoming.get("status") != "active":
        return "incoming Runtime Web turn lease must be active"
    if not _runtime_web_turn_lease_matches_event(incoming, event):
        return "incoming Runtime Web turn lease does not match verified Web event fences"
    prior = _runtime_web_turn_lease(state.get("web_turn_lease"))
    current_turn_id = str(state.get("active_turn_id") or "").strip()
    inflight = [str(item) for item in state.get("inflight_tool_use_ids", []) if str(item)]
    if prior is None:
        if incoming.get("generation") != 1:
            return "first Runtime Web turn lease generation must be 1"
        if current_turn_id and current_turn_id not in LEGACY_WEB_TURN_IDS:
            return "existing non-legacy turn cannot be replaced without a verified Runtime Web turn lease"
        if current_turn_id in LEGACY_WEB_TURN_IDS and inflight:
            return "legacy Web turn cannot migrate while tool evidence is inflight"
        return None
    if prior.get("status") == "active":
        if (
            prior.get("generation") != incoming.get("generation")
            or str(prior.get("turn_id") or "") != str(incoming.get("turn_id") or "")
            or str(prior.get("runtime_invocation_id") or "") != str(incoming.get("runtime_invocation_id") or "")
        ):
            return "active Runtime Web turn lease cannot be rotated before machine turn-end evidence"
        if not _runtime_web_turn_lease_matches_event(prior, event):
            return "active Runtime Web turn lease target or ownership fence is stale"
        return None
    if prior.get("status") == "ended":
        if incoming.get("generation") != int(prior["generation"]) + 1:
            return "Runtime Web turn lease generation is not the next monotonic generation"
        if str(prior.get("turn_id") or "") == str(incoming.get("turn_id") or ""):
            return "ended Runtime Web turn lease cannot be reused as the next turn"
        if inflight:
            return "ended Web turn still has inflight tool evidence"
        return None
    return "Runtime Web turn lease state is invalid"


def mark_runtime_web_turn_ended(
    *,
    controller_id: str,
    expected_turn_id: str,
    watcher_nonce: str,
    expected_session_id: str | None = None,
    expected_target_generation: int | None = None,
    expected_ownership_generation: int | None = None,
    end_reason: str,
    end_evidence_sha256: str,
    lifecycle_path: Path | None = None,
) -> dict[str, Any]:
    path = lifecycle_path or state_path(controller_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            state = load_json(path)
            lease = _runtime_web_turn_lease(state.get("web_turn_lease"))
            if lease is None:
                raise PermissionError("Runtime Web turn lease is unavailable")
            if lease.get("status") == "ended":
                return lease
            if (
                str(lease.get("turn_id") or "") != str(expected_turn_id or "").strip()
                or str(lease.get("watcher_nonce") or "") != str(watcher_nonce or "").strip()
                or str(state.get("active_turn_id") or "").strip() != str(expected_turn_id or "").strip()
                or (expected_session_id is not None and str(lease.get("execution_target_session_id") or "") != str(expected_session_id))
                or (expected_target_generation is not None and lease.get("target_generation") != expected_target_generation)
                or (expected_ownership_generation is not None and lease.get("ownership_generation") != expected_ownership_generation)
            ):
                raise PermissionError("stale Runtime Web turn-end watcher cannot mutate the current turn")
            lease["status"] = "ended"
            lease["ended_at"] = datetime.now(timezone.utc).isoformat()
            lease["end_reason"] = str(end_reason or "").strip()[:128]
            lease["end_evidence_sha256"] = str(end_evidence_sha256 or "").strip()[:128]
            state["web_turn_lease"] = lease
            write_json(path, state)
            return lease
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def record_runtime_web_turn_watcher_started(
    *, controller_id: str, expected_turn_id: str, watcher_nonce: str, pid: int,
    lifecycle_path: Path | None = None,
) -> dict[str, Any]:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid < 1:
        raise ValueError("Runtime Web turn watcher pid must be positive")
    path = lifecycle_path or state_path(controller_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            state = load_json(path)
            lease = _runtime_web_turn_lease(state.get("web_turn_lease"))
            if lease is None or lease.get("status") != "active":
                raise PermissionError("Runtime Web turn lease is not active")
            if (
                str(lease.get("turn_id") or "") != str(expected_turn_id or "").strip()
                or str(lease.get("watcher_nonce") or "") != str(watcher_nonce or "").strip()
            ):
                raise PermissionError("stale Runtime Web turn watcher cannot claim the current turn")
            lease["watcher_pid"] = pid
            lease["watcher_started_at"] = datetime.now(timezone.utc).isoformat()
            state["web_turn_lease"] = lease
            write_json(path, state)
            return lease
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _verified_web_turn_evidence(event: dict[str, Any]) -> dict[str, Any]:
    controller_id = str(
        event.get("controller_session_id") or event.get("controller_id") or event.get("session_id") or ""
    ).strip()
    source_session_id = str(
        event.get("web_session_id") or event.get("source_session_id") or ""
    ).strip()
    if not controller_id or not source_session_id:
        raise PermissionError("verified Web turn requires exact Controller and Web source session")
    identity = agent_target.logical_agent_identity(agent_type="controller", agent_id=controller_id)
    return agent_target.normalize_verified_execution_turn(
        event.get("verified_execution_turn"),
        expected_logical_agent=identity,
        expected_host="web",
        expected_execution_target_session_id=source_session_id,
        expected_target_generation=event.get("controller_target_generation"),
        expected_ownership_generation=event.get("controller_ownership_generation"),
    )


def _synthesize_desktop_turn_id(event: dict[str, Any], state: dict[str, Any]) -> str:
    source = str(event.get("source_session_id") or state.get("source_session_id") or "").strip()
    tool_use_id = str(event.get("tool_use_id") or "").strip()
    suffix = tool_use_id or "hook"
    return f"desktop-turn:{source}:{suffix}"


def _desktop_event_lacks_native_turn_id(event: dict[str, Any]) -> bool:
    turn_id = _event_turn_id(event)
    return (not turn_id) or turn_id == "None" or turn_id.startswith("web-turn:")


def _bind_desktop_empty_turn_id(state: dict[str, Any], event: dict[str, Any]) -> None:
    """Desktop hooks often omit turn_id or still carry a stale web-turn id; bind a stable id."""
    if event.get("controller_host") != DESKTOP_SESSION_HOST:
        return
    if not _desktop_event_lacks_native_turn_id(event):
        return
    current = str(state.get("active_turn_id") or "").strip()
    if current.startswith("desktop-turn:"):
        event["turn_id"] = current
        return
    if current.startswith("web-turn:"):
        event["turn_id"] = _synthesize_desktop_turn_id(event, state)


def _begin_turn(state: dict[str, Any], event: dict[str, Any]) -> str | None:
    _bind_desktop_empty_turn_id(state, event)
    turn_id = _event_turn_id(event)
    web_turn: dict[str, Any] | None = None
    is_web_machine_event = (
        str(event.get("controller_host") or "").strip() == "web"
        and str(event.get("execution_host") or "").strip() == "web"
        and str(event.get("event_source") or "").strip() == "web"
    )
    if (
        is_web_machine_event
        and event.get("hook_event_name") == "PostToolUse"
        and not isinstance(event.get("verified_execution_turn"), dict)
    ):
        state["adapter_fault"] = {
            "code": "unverified_web_tool_event",
            "turn_id": turn_id,
            "active_turn_id": str(state.get("active_turn_id", "")),
            "reason": "Web PostToolUse requires verified execution-turn evidence before turn-local mutation",
        }
        return "Web tool result rejected: verified execution-turn evidence is required."
    if not turn_id:
        return None
    if is_web_machine_event:
        try:
            web_turn = _verified_web_turn_evidence(event)
        except (PermissionError, ValueError) as exc:
            state["adapter_fault"] = {
                "code": "unverified_web_turn",
                "turn_id": turn_id,
                "active_turn_id": str(state.get("active_turn_id", "")),
                "reason": str(exc),
            }
            return "Web turn boundary rejected: Host-verified execution turn evidence is required."
        if web_turn["turn_id"] != turn_id:
            state["adapter_fault"] = {
                "code": "unverified_web_turn",
                "turn_id": turn_id,
                "active_turn_id": str(state.get("active_turn_id", "")),
                "reason": "event turn_id does not match verified execution turn",
            }
            return "Web turn boundary rejected: event turn_id does not match Host-verified execution turn."
    prior_lease = _runtime_web_turn_lease(state.get("web_turn_lease")) if web_turn is not None else None
    incoming_lease = _runtime_web_turn_lease(event.get("web_turn_lease")) if web_turn is not None else None
    if incoming_lease is not None:
        lease_error = _validate_runtime_web_turn_lease_transition(state, event, incoming_lease)
        if lease_error:
            state["adapter_fault"] = {
                "code": "runtime_web_turn_lease_rejected",
                "turn_id": turn_id,
                "active_turn_id": str(state.get("active_turn_id", "")),
                "reason": lease_error,
            }
            return "Web turn boundary rejected: " + lease_error
    current_turn_id = str(state.get("active_turn_id", ""))
    if (
        current_turn_id == turn_id
        and event.get("controller_host") == DESKTOP_SESSION_HOST
        and str(current_turn_id).startswith("web-turn:")
    ):
        # Desktop events that still carry the stale web-turn id are a new physical turn.
        turn_id = _synthesize_desktop_turn_id(event, state)
        event["turn_id"] = turn_id
    if current_turn_id == turn_id:
        if web_turn is not None:
            state["turn_start_evidence"] = dict(web_turn)
        if incoming_lease is not None:
            state["web_turn_lease"] = incoming_lease
        return None
    if (
        web_turn is not None
        and prior_lease is not None
        and prior_lease.get("status") == "active"
        and incoming_lease is None
    ):
        state["adapter_fault"] = {
            "code": "runtime_web_turn_lease_abandonment",
            "turn_id": turn_id,
            "active_turn_id": current_turn_id,
            "reason": "active Runtime Web turn lease cannot be abandoned by a direct Host turn",
        }
        return "Web turn boundary rejected: active Runtime Web turn lease requires machine end or verified successor transition."
    if web_turn is not None and event.get("hook_event_name") not in {"SessionStart", "UserPromptSubmit"}:
        state["adapter_fault"] = {
            "code": "web_turn_start_required",
            "turn_id": turn_id,
            "active_turn_id": current_turn_id,
            "reason": "verified Web PostToolUse cannot create a new turn without SessionStart/UserPromptSubmit",
        }
        return "Web turn boundary rejected: SessionStart/UserPromptSubmit is required before Web tool evidence."
    if web_turn is not None and current_turn_id and state.get("inflight_tool_use_ids"):
        inflight = [str(item) for item in state.get("inflight_tool_use_ids", []) if str(item)]
        state["adapter_fault"] = {
            "code": "inflight_tool_turn_boundary",
            "turn_id": turn_id,
            "active_turn_id": current_turn_id,
            "inflight_tool_use_ids": inflight,
        }
        return "Turn boundary rejected: prior Web turn still has inflight tool evidence."
    proof = None
    stale_web_on_desktop = (
        event.get("controller_host") == DESKTOP_SESSION_HOST
        and str(current_turn_id).startswith("web-turn:")
        and bool(turn_id)
        and not str(turn_id).startswith("web-turn:")
    )
    if current_turn_id and event.get("hook_event_name") not in {"SessionStart", "UserPromptSubmit"}:
        if stale_web_on_desktop:
            proof = {
                "source": "desktop_replaces_stale_web_turn",
                "turn_id": turn_id,
                "replaced_web_turn_id": current_turn_id,
            }
        else:
            proof = _desktop_turn_start(event)
            if proof is None and web_turn is None:
                return None
    prior_handoff = state.get("host_turn_handoff")
    if isinstance(prior_handoff, dict):
        state["last_host_turn_handoff"] = {
            **prior_handoff,
            "state": "reentered",
            "reentry_turn_id": turn_id,
        }
    state.pop("host_turn_handoff", None)
    state["stop_continuations"] = 0
    state.pop("stop_continuation_turn_id", None)
    state["active_turn_id"] = turn_id
    state["must_yield"] = False
    state["tool_trace"] = []
    state.pop("tool_trace_archive", None)
    state["tool_trace_overflow"] = False
    state["inflight_tool_use_ids"] = []
    state["inflight_tool_records"] = {}
    state.pop("control_receipt_inflight", None)
    state.pop("control_receipt_proposal", None)
    state.pop("goal_block_authorization", None)
    state.pop("goal_block_inflight", None)
    state.pop("receipt_turn_id", None)
    state.pop("receipt_tool_use_id", None)
    state.pop("adapter_fault", None)
    state["turn_start_evidence"] = dict(web_turn) if web_turn is not None else (proof or {
        "source": str(event.get("hook_event_name", "")), "turn_id": turn_id,
    })
    if incoming_lease is not None:
        state["web_turn_lease"] = incoming_lease
    elif web_turn is not None and prior_lease is not None and prior_lease.get("status") == "ended":
        state.pop("web_turn_lease", None)
    return None


def _turn_fault(state: dict[str, Any], event: dict[str, Any]) -> str | None:
    current = str(state.get("active_turn_id", ""))
    incoming = _event_turn_id(event)
    same_desktop_turn = (
        event.get("controller_host") == DESKTOP_SESSION_HOST
        and current.startswith("desktop-turn:")
        and _desktop_event_lacks_native_turn_id(event)
    )
    if current and incoming != current and not same_desktop_turn:
        return "unverified_turn_boundary"
    if state.get("tool_trace_overflow"):
        return "tool_trace_overflow"
    if event.get("hook_event_name") == "Stop" and state.get("inflight_tool_use_ids"):
        return "inflight_tools_at_stop"
    return None


def _adapter_fault_output(state: dict[str, Any], event: dict[str, Any], code: str) -> dict[str, Any]:
    state["adapter_fault"] = {
        "code": code, "turn_id": _event_turn_id(event),
        "active_turn_id": str(state.get("active_turn_id", "")),
    }
    reason = (
        f"Adaptive Agent Runtime adapter degraded: {code}. "
        "本回合机器证据无法闭合；保留 pending、工具轨迹与未返回工具，停止异常续作。"
        "不得重试收据、清空状态或把项目 Goal 标为 blocked；从可信新回合恢复并先对账检查点。"
    )
    if event.get("hook_event_name") == "PreToolUse":
        return _pre_tool_denial(reason)
    return {"continue": False, "stopReason": reason, "systemMessage": reason}


def continuation_debt_fingerprint(state: dict[str, Any]) -> str:
    snapshot = state.get("snapshot") if isinstance(state.get("snapshot"), dict) else {}
    return _json_sha256({
        "wake_generation": int(state.get("wake_generation", 0) or 0),
        "triggers": sorted(str(item) for item in state.get("triggers", []) if str(item)),
        "next_action": str(state.get("next_action") or ""),
        "requires_user": state.get("requires_user"),
        "pending_terminal_receipts": sorted(
            str(item) for item in state.get("pending_terminal_receipts", []) if str(item)
        ),
        "debt_labels": snapshot_continuation_debt_labels(snapshot),
    })


def _reentry_proofs(state: dict[str, Any]) -> list[dict[str, Any]]:
    proofs: list[dict[str, Any]] = []
    for key in (
        "desktop_reentry",
        "confirmed_desktop_reentry",
        "last_confirmed_desktop_reentry",
        "host_turn_handoff",
    ):
        value = state.get(key)
        if isinstance(value, dict):
            proofs.append(value)
    return proofs


def visible_same_controller_desktop_reentry_confirmed(
    state: dict[str, Any], event: dict[str, Any], *, debt_fingerprint: str
) -> bool:
    """True only when a visible same-controller Desktop reentry is already CONFIRMED.

    requested/delegated handoff, supervisor_already_managing_or_deferred, and
    RESUME_DEFERRED_ACTIVE_WRITER are not visible continuation.
    """
    event_controller = str(
        event.get("controller_session_id")
        or state.get("logical_controller_id")
        or state.get("session_id")
        or ""
    ).strip()
    event_target = str(
        event.get("source_session_id")
        or state.get("source_session_id")
        or event.get("session_id")
        or ""
    ).strip()
    for proof in _reentry_proofs(state):
        proof_state = str(proof.get("state") or "").strip()
        result = str(proof.get("result") or "").strip()
        delivery = str(proof.get("delivery_state") or "").strip()
        if proof_state in FORBIDDEN_REENTRY_HANDOFF_STATES:
            continue
        if proof_state in FORBIDDEN_REENTRY_RESUME_STATES:
            continue
        if delivery in FORBIDDEN_REENTRY_DELIVERY_STATES:
            continue
        confirmed = (
            result in CONFIRMED_DESKTOP_REENTRY_RESULTS
            or proof_state in CONFIRMED_DESKTOP_REENTRY_STATES
        )
        if not confirmed:
            continue
        if str(proof.get("debt_fingerprint") or "").strip() != debt_fingerprint:
            continue
        controller = str(
            proof.get("controller_id") or proof.get("logical_controller_id") or ""
        ).strip()
        target = str(
            proof.get("execution_target_session_id")
            or proof.get("desktop_current_target")
            or ""
        ).strip()
        if not controller and not target:
            continue
        if controller and event_controller and controller != event_controller:
            continue
        if target and event_target and target != event_target:
            continue
        return True
    return False


def _desktop_on_duty_source(event: dict[str, Any], state: dict[str, Any]) -> bool:
    """True when this event is the on-duty Desktop controller entry itself."""
    if event.get("controller_host") != DESKTOP_SESSION_HOST:
        return False
    source_id = str(event.get("source_session_id") or "").strip()
    duty_source = str(state.get("source_session_id") or "").strip()
    return bool(source_id) and source_id == duty_source


def _pending_stop_output(
    state: dict[str, Any], event: dict[str, Any], *, reason: str
) -> dict[str, Any]:
    """Keep the Host turn alive until debt is cleared or a non-duty reentry is CONFIRMED."""
    turn_id = _event_turn_id(event) or str(state.get("active_turn_id", "")).strip()
    prior_turn_id = str(state.get("stop_continuation_turn_id", "")).strip()
    prior_count = int(state.get("stop_continuations", 0) or 0)
    continuations = prior_count + 1 if turn_id and prior_turn_id == turn_id else 1
    state["stop_continuations"] = continuations
    if turn_id:
        state["stop_continuation_turn_id"] = turn_id
    debt_fingerprint = continuation_debt_fingerprint(state)
    # Web may end the physical turn after HMAC/desktop reentry CONFIRMED.
    # Desktop on-duty already is that entry; CONFIRMED must not idle it.
    confirmed = (
        not _desktop_on_duty_source(event, state)
        and bool(turn_id)
        and continuations >= MIN_STOPS_BEFORE_CONFIRMED_HOST_END
        and visible_same_controller_desktop_reentry_confirmed(
            state, event, debt_fingerprint=debt_fingerprint
        )
    )
    if not confirmed:
        state.pop("host_turn_handoff", None)
        return {"decision": "block", "reason": reason}

    state["host_turn_handoff"] = {
        "schema_version": 1,
        "state": "reentry_confirmed",
        "result": "CONFIRMED",
        "turn_id": turn_id,
        "wake_generation": int(state.get("wake_generation", 0) or 0),
        "debt_fingerprint": debt_fingerprint,
    }
    terminal_reason = (
        reason
        + " 同一 logical Controller 的 Desktop current target reentry 已 CONFIRMED，"
        "且 Continuation Debt 指纹未变；结束当前物理回合，债务仍保留。"
    )
    return {
        "continue": False,
        "stopReason": terminal_reason,
        "systemMessage": terminal_reason,
    }


def _is_control_guard_command(
    command: str,
    *,
    controller_session_id: str = "",
    cwd: str | Path | None = None,
) -> bool:
    if any(character in command for character in "\n\r;&|><#$`()"):
        return False
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    if len(tokens) < 4:
        return False
    declared_python = Path(tokens[0]).expanduser()
    if declared_python.is_absolute():
        resolved_python = declared_python.resolve()
    else:
        discovered_python = shutil.which(tokens[0])
        if not discovered_python:
            return False
        resolved_python = Path(discovered_python).resolve()
    if resolved_python != Path(sys.executable).resolve():
        return False
    expected_guard = Path(__file__).resolve().with_name("control_event_guard.py")
    declared_guard = Path(tokens[1]).expanduser()
    candidates: set[Path] = set()
    if declared_guard.is_absolute():
        candidates.add(declared_guard.resolve())
    else:
        skill_root = Path(__file__).resolve().parents[1]
        candidates.add((skill_root / declared_guard).resolve())
        candidates.add((expected_guard.parent / declared_guard).resolve())
        if cwd is not None:
            candidates.add((Path(cwd).expanduser().resolve() / declared_guard).resolve())
    if expected_guard not in candidates:
        return False
    if any(token in {";", "&&", "||", "|", "&", ">", "<", "#"} for token in tokens):
        return False
    if "--ledger" not in tokens:
        return False
    session_id = controller_session_id.strip()
    if session_id:
        try:
            index = tokens.index("--controller-session")
        except ValueError:
            return False
        if index + 1 >= len(tokens) or tokens[index + 1] != session_id:
            return False
    return True


def _goal_block_request(event: dict[str, Any]) -> bool:
    tool_name = str(event.get("tool_name", "")).strip().rsplit(".", 1)[-1]
    tool_input = event.get("tool_input")
    return (
        tool_name == "update_goal"
        and isinstance(tool_input, dict)
        and str(tool_input.get("status", "")).strip().lower() == "blocked"
    )


def _control_guard_proposal(command: str, *, cwd: str | Path | None) -> dict[str, Any] | None:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    if len(tokens) < 3 or tokens[2].startswith("-"):
        return None
    snapshot_path = Path(tokens[2]).expanduser()
    if not snapshot_path.is_absolute():
        snapshot_path = (Path(cwd or ".").expanduser().resolve() / snapshot_path).resolve()
    try:
        raw = snapshot_path.read_bytes()
        snapshot = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(snapshot, dict):
        return None
    event_contract = snapshot.get("event_contract")
    event_id = (
        str(event_contract.get("event_id") or "").strip()
        if isinstance(event_contract, dict)
        else ""
    )
    rollover = snapshot.get("goal_rollover") if isinstance(snapshot, dict) else None
    rollover = rollover if isinstance(rollover, dict) else {}
    proposal: dict[str, Any] = {
        "snapshot_path": str(snapshot_path),
        "snapshot_sha256": sha256_bytes(raw),
        "cycle_snapshot_sha256": _json_sha256(snapshot),
        "event_id": event_id,
        "goal_rollover_status": str(rollover.get("status", "")).strip().lower(),
    }
    try:
        repo_index = tokens.index("--repo")
        repo_path = Path(tokens[repo_index + 1]).expanduser()
        if not repo_path.is_absolute():
            repo_path = (Path(cwd or ".").expanduser().resolve() / repo_path).resolve()
        proposal["repo_path"] = str(repo_path)
    except (ValueError, IndexError):
        pass
    if proposal["goal_rollover_status"] != "rolled":
        return proposal
    try:
        ledger_index = tokens.index("--ledger")
        ledger_path = Path(tokens[ledger_index + 1]).expanduser()
        if not ledger_path.is_absolute():
            ledger_path = (Path(cwd or ".").expanduser().resolve() / ledger_path).resolve()
        ledger_raw = ledger_path.read_bytes()
        ledger_sha256 = sha256_bytes(ledger_raw)
        if ledger_sha256 != str(snapshot.get("ledger_sha256", "")).strip():
            return proposal
        ledger_text = ledger_raw.decode("utf-8")
        current_match = re.search(r"^- 当前 Goal：\s*(.+?)\s*$", ledger_text, re.MULTILINE)
        current_goal_id = str(rollover.get("current_goal_id", "")).strip()
        if current_match is None or not current_goal_id:
            return proposal
        current_goal_display = current_match.group(1).strip()
        if not re.search(
            rf"(?<![A-Za-z0-9_-]){re.escape(current_goal_id)}(?![A-Za-z0-9_-])",
            current_goal_display,
        ):
            return proposal
        project_root = Path(str(snapshot.get("root") or cwd or ".")).expanduser().resolve()
        proposal["goal_rollover"] = {
            "status": "rolled",
            "project_recomputed": rollover.get("project_recomputed") is True,
            "ledger_sha256": ledger_sha256,
            "closed_goal_id": str(rollover.get("closed_goal_id", "")).strip(),
            "current_goal_id": current_goal_id,
            "current_goal_display": current_goal_display,
            "project_name": ledger_path.parent.name or project_root.name,
        }
        proposal["ledger_path"] = str(ledger_path)
    except (OSError, UnicodeDecodeError, ValueError, IndexError):
        pass
    return proposal


def _verified_goal_rollover_proposal(
    proposal: object, *, tool_use_id: str
) -> dict[str, Any] | None:
    if not isinstance(proposal, dict):
        return None
    if str(proposal.get("tool_use_id", "")) != tool_use_id:
        return None
    rollover = proposal.get("goal_rollover")
    if not isinstance(rollover, dict) or rollover.get("status") != "rolled":
        return None
    snapshot_path = Path(str(proposal.get("snapshot_path", ""))).expanduser()
    ledger_path = Path(str(proposal.get("ledger_path", ""))).expanduser()
    try:
        snapshot_matches = (
            bool(proposal.get("snapshot_sha256"))
            and sha256_bytes(snapshot_path.read_bytes()) == proposal.get("snapshot_sha256")
        )
        ledger_matches = (
            bool(rollover.get("ledger_sha256"))
            and sha256_bytes(ledger_path.read_bytes()) == rollover.get("ledger_sha256")
        )
    except OSError:
        return None
    return dict(rollover) if snapshot_matches and ledger_matches else None


def _verified_project_block_proposal(
    proposal: object, *, tool_use_id: str
) -> bool:
    if not isinstance(proposal, dict):
        return False
    if str(proposal.get("tool_use_id", "")) != tool_use_id:
        return False
    if proposal.get("goal_rollover_status") != "project_blocked":
        return False
    path = Path(str(proposal.get("snapshot_path", ""))).expanduser()
    expected_sha256 = str(proposal.get("snapshot_sha256", ""))
    try:
        return bool(expected_sha256) and sha256_bytes(path.read_bytes()) == expected_sha256
    except OSError:
        return False


def _tool_use_id(event: dict[str, Any]) -> str:
    return str(event.get("tool_use_id", "")).strip()


def _verified_rollout_control_receipt(
    event: dict[str, Any], snapshot: dict[str, Any] | None
) -> bool:
    recovery = event.get("rollout_recovery")
    proposal = event.get("rollout_control_receipt_proposal")
    if not isinstance(recovery, dict) or not isinstance(proposal, dict):
        return False
    response = event.get("tool_response")
    if not isinstance(response, dict) or response.get("exit_code") != 0:
        return False
    if "control-event: allowed" not in str(response.get("stdout") or ""):
        return False
    tool_use_id = _tool_use_id(event)
    event_id = str(proposal.get("event_id") or "").strip()
    if (
        not tool_use_id
        or str(proposal.get("tool_use_id") or "").strip() != tool_use_id
        or str(recovery.get("tool_use_id") or "").strip() != tool_use_id
        or not event_id
        or not isinstance(snapshot, dict)
    ):
        return False
    root = Path(str(snapshot.get("root") or "")).expanduser()
    proposal_root = Path(str(proposal.get("repo_path") or "")).expanduser()
    try:
        root = root.resolve(strict=True)
        if proposal_root.resolve(strict=True) != root:
            return False
        try:
            from control_event_guard import controller_cycle_evidence_path
        except ModuleNotFoundError:
            from scripts.control_event_guard import controller_cycle_evidence_path
        evidence = json.loads(
            controller_cycle_evidence_path(root, event_id).read_text(encoding="utf-8")
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False
    controller_id = str(event.get("controller_session_id") or "").strip()
    return (
        isinstance(evidence, dict)
        and evidence.get("record_kind") == "controller_cycle_evidence"
        and str(evidence.get("evidence_id") or "").strip() == event_id
        and str(evidence.get("cycle_id") or "").strip() == event_id
        and str(evidence.get("controller_id") or "").strip() == controller_id
        and str(evidence.get("terminal_status") or "").strip().upper() == "CLOSED"
        and str(evidence.get("snapshot_sha256") or "").strip()
        == str(proposal.get("cycle_snapshot_sha256") or "")
        and str(evidence.get("main_revision") or "").strip()
        == str(snapshot.get("head") or "").strip()
        and str(evidence.get("ledger_sha256") or "").strip()
        == str(snapshot.get("ledger_sha256") or "").strip()
    )


def _archive_completed_tool_trace(
    state: dict[str, Any],
    entries: list[dict[str, Any]],
    *,
    turn_id: str,
) -> None:
    if not entries:
        return
    prior = state.get("tool_trace_archive")
    if not isinstance(prior, dict) or str(prior.get("turn_id") or "") != turn_id:
        prior = {"turn_id": turn_id, "entry_count": 0, "trace_sha256": ""}
    prior_count = prior.get("entry_count")
    if not isinstance(prior_count, int) or prior_count < 0:
        prior_count = 0
    prior_sha256 = str(prior.get("trace_sha256") or "")
    state["tool_trace_archive"] = {
        "turn_id": turn_id,
        "entry_count": prior_count + len(entries),
        "trace_sha256": _json_sha256({
            "previous_trace_sha256": prior_sha256,
            "entries": entries,
        }),
    }


def _compact_completed_tool_trace(state: dict[str, Any], trace: list[dict[str, Any]]) -> None:
    overflow = len(trace) - MAX_TOOL_TRACE_ENTRIES
    if overflow <= 0:
        state["tool_trace"] = trace
        return
    inflight = {
        str(item) for item in state.get("inflight_tool_use_ids", []) if str(item)
    }
    archived: list[dict[str, Any]] = []
    retained: list[dict[str, Any]] = []
    for item in trace:
        if len(archived) < overflow and str(item.get("tool_use_id") or "") not in inflight:
            archived.append(item)
        else:
            retained.append(item)
    if len(archived) != overflow:
        state["tool_trace_overflow"] = True
        state["tool_trace"] = trace
        return
    _archive_completed_tool_trace(
        state,
        archived,
        turn_id=str(state.get("active_turn_id") or ""),
    )
    state["tool_trace"] = retained


def _record_tool_trace(state: dict[str, Any], event: dict[str, Any]) -> None:
    if event.get("hook_event_name") != "PostToolUse":
        return
    tool_name = str(event.get("tool_name", "")).strip()
    if event.get("controller_host") == DESKTOP_SESSION_HOST and not _tool_use_id(event):
        return
    tool_input = event.get("tool_input")
    if not tool_name and not isinstance(tool_input, dict):
        return
    if isinstance(tool_input, dict):
        command = str(tool_input.get("command", ""))
        try:
            tokens = shlex.split(command)
        except ValueError:
            tokens = []
        script_indexes = [
            index
            for index, token in enumerate(tokens)
            if token.endswith("lifecycle_hook.py")
        ]
        if len(script_indexes) == 1:
            script_index = script_indexes[0]
            if (
                script_index == 1
                and len(tokens) == 4
                and tokens[script_index + 1] == "--print-machine-trace"
                and tokens[script_index + 2].strip()
            ):
                return
    turn_id = _event_turn_id(event) or str(state.get("active_turn_id", ""))
    input_sha256 = str(event.get("recovered_input_sha256") or "").strip()
    if not input_sha256:
        input_sha256 = _json_sha256(tool_input)
    tool_use_id = str(event.get("tool_use_id", "")).strip()
    if not tool_use_id:
        tool_use_id = f"derived:{turn_id}:{tool_name}:{input_sha256[:12]}"
    response = event.get("tool_response")
    response_status: Any = None
    if isinstance(response, dict):
        response_status = response.get("exit_code", response.get("isError", response.get("state")))
    entry = {
        "turn_id": turn_id,
        "tool_use_id": tool_use_id,
        "tool_name": tool_name or "unknown",
        "input_sha256": input_sha256,
        "response_status": response_status,
    }
    trace = [item for item in state.get("tool_trace", []) if isinstance(item, dict)]
    for index in range(len(trace) - 1, -1, -1):
        existing = trace[index]
        if (
            str(existing.get("turn_id") or "") == turn_id
            and str(existing.get("tool_use_id") or "") == tool_use_id
        ):
            updated = dict(existing)
            updated["response_status"] = response_status
            if not str(updated.get("tool_name") or ""):
                updated["tool_name"] = entry["tool_name"]
            if not str(updated.get("input_sha256") or ""):
                updated["input_sha256"] = entry["input_sha256"]
            trace[index] = updated
            state["tool_trace"] = trace
            return
    trace.append(entry)
    _compact_completed_tool_trace(state, trace)


def machine_trace_projection(state: dict[str, Any]) -> dict[str, Any]:
    turn_id = str(state.get("active_turn_id", "")).strip()
    trace: list[dict[str, Any]] = []
    for item in state.get("tool_trace", []):
        if not isinstance(item, dict) or str(item.get("turn_id", "")) != turn_id:
            continue
        trace.append(
            {
                "turn_id": turn_id,
                "tool_use_id": str(item.get("tool_use_id", "")),
                "tool_name": str(item.get("tool_name", "")),
                "input_sha256": str(item.get("input_sha256", "")),
                "response_status": item.get("response_status"),
            }
        )
    archive = state.get("tool_trace_archive")
    archived_entry_count = 0
    archived_trace_sha256 = ""
    if isinstance(archive, dict) and str(archive.get("turn_id") or "") == turn_id:
        count = archive.get("entry_count")
        if isinstance(count, int) and count >= 0:
            archived_entry_count = count
        archived_trace_sha256 = str(archive.get("trace_sha256") or "")
    return {
        "turn_id": turn_id,
        "tool_use_ids": [item["tool_use_id"] for item in trace],
        "archived_entry_count": archived_entry_count,
        "archived_trace_sha256": archived_trace_sha256,
        "trace_sha256": _json_sha256({
            "archived_entry_count": archived_entry_count,
            "archived_trace_sha256": archived_trace_sha256,
            "retained_trace": trace,
        }),
    }


def _pre_tool_denial(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


_COMMAND_EXECUTION_TOOLS = {
    "bash",
    "commandexecution",
    "exec_command",
    "shell",
    "shell_command",
}
_PERSISTENT_SCRIPT_NAMES = {
    "dev", "develop", "emulator", "preview", "runserver", "serve", "server",
    "simulator", "start", "storybook", "watch",
}
_PERSISTENT_EXECUTABLES = {
    "gunicorn",
    "nodemon",
    "simulator",
    "uvicorn",
    "vite",
    "watch",
    "webpack-dev-server",
}
_SHELL_SEPARATORS = {";", "&&", "||", "|", "&"}
_TIMEOUT_DURATION = re.compile(r"\d+(?:\.\d+)?(?:ms|s|m|h|d)?")


def _command_segments(command: str) -> list[list[str]]:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return []
    segments: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token in _SHELL_SEPARATORS or (
            token and set(token) <= {";", "&", "|"}
        ):
            if current:
                segments.append(current)
                current = []
            continue
        current.append(token)
    if current:
        segments.append(current)
    return segments


def _shell_command_substitutions(command: str) -> list[str]:
    substitutions: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(command):
        char = command[index]
        if char == "\\" and quote != "'":
            index += 2
            continue
        if char == "'":
            quote = None if quote == "'" else ("'" if quote is None else quote)
            index += 1
            continue
        if char == '"':
            quote = None if quote == '"' else ('"' if quote is None else quote)
            index += 1
            continue
        if (
            quote != "'"
            and command.startswith("$(", index)
            and not command.startswith("$((", index)
        ):
            start = index + 2
            cursor = start
            depth = 1
            inner_quote: str | None = None
            while cursor < len(command):
                inner = command[cursor]
                if inner == "\\" and inner_quote != "'":
                    cursor += 2
                    continue
                if inner == "'":
                    inner_quote = (
                        None if inner_quote == "'" else ("'" if inner_quote is None else inner_quote)
                    )
                elif inner == '"':
                    inner_quote = (
                        None if inner_quote == '"' else ('"' if inner_quote is None else inner_quote)
                    )
                elif inner_quote is None and inner == "(":
                    depth += 1
                elif inner_quote is None and inner == ")":
                    depth -= 1
                    if depth == 0:
                        substitutions.append(command[start:cursor])
                        index = cursor + 1
                        break
                cursor += 1
            else:
                index += 2
            continue
        if quote != "'" and char == "`":
            cursor = index + 1
            while cursor < len(command):
                if command[cursor] == "\\":
                    cursor += 2
                    continue
                if command[cursor] == "`":
                    substitutions.append(command[index + 1 : cursor])
                    index = cursor + 1
                    break
                cursor += 1
            else:
                index += 1
            continue
        index += 1
    return substitutions


def _shell_parenthesized_execution_groups(command: str) -> list[str]:
    groups: list[str] = []
    quote: str | None = None
    double_bracket = False
    index = 0
    while index < len(command):
        char = command[index]
        if char == "\\" and quote != "'":
            index += 2
            continue
        if char == "'":
            quote = None if quote == "'" else ("'" if quote is None else quote)
            index += 1
            continue
        if char == '"':
            quote = None if quote == '"' else ('"' if quote is None else quote)
            index += 1
            continue
        if quote is None and command.startswith("[[", index):
            double_bracket = True
            index += 2
            continue
        if quote is None and double_bracket and command.startswith("]]", index):
            double_bracket = False
            index += 2
            continue
        if quote is None and command.startswith("$((", index):
            cursor = index + 3
            depth = 2
            inner_quote: str | None = None
            while cursor < len(command):
                inner = command[cursor]
                if inner == "\\" and inner_quote != "'":
                    cursor += 2
                    continue
                if inner == "'":
                    inner_quote = (
                        None if inner_quote == "'" else ("'" if inner_quote is None else inner_quote)
                    )
                elif inner == '"':
                    inner_quote = (
                        None if inner_quote == '"' else ('"' if inner_quote is None else inner_quote)
                    )
                elif inner_quote is None and inner == "(":
                    depth += 1
                elif inner_quote is None and inner == ")":
                    depth -= 1
                    if depth == 0:
                        index = cursor + 1
                        break
                cursor += 1
            else:
                index += 3
            continue
        prefix = command[index : index + 2]
        process_prefix = quote is None and (
            prefix in {"<(", ">("}
            or (
                prefix == "=("
                and (
                    index == 0
                    or command[index - 1].isspace()
                    or command[index - 1] in {";", "|", "&", "("}
                )
            )
        )
        plain_group = (
            quote is None
            and not double_bracket
            and char == "("
            and (index == 0 or command[index - 1] not in "$<>=")
            and not (index >= 2 and command[index - 2 : index] == "$(")
        )
        if not process_prefix and not plain_group:
            index += 1
            continue
        start = index + (2 if process_prefix else 1)
        cursor = start
        depth = 1
        inner_quote: str | None = None
        while cursor < len(command):
            inner = command[cursor]
            if inner == "\\" and inner_quote != "'":
                cursor += 2
                continue
            if inner == "'":
                inner_quote = (
                    None if inner_quote == "'" else ("'" if inner_quote is None else inner_quote)
                )
            elif inner == '"':
                inner_quote = (
                    None if inner_quote == '"' else ('"' if inner_quote is None else inner_quote)
                )
            elif inner_quote is None and inner == "(":
                depth += 1
            elif inner_quote is None and inner == ")":
                depth -= 1
                if depth == 0:
                    groups.append(command[start:cursor])
                    index = cursor + 1
                    break
            cursor += 1
        else:
            index += 1
    return groups


def _strip_command_prefix(tokens: list[str]) -> list[str]:
    remaining = list(tokens)
    if remaining and Path(remaining[0]).name.lower() == "env":
        remaining.pop(0)
        while remaining:
            token = remaining[0]
            if token == "--":
                remaining.pop(0)
                break
            if token in {"-i", "--ignore-environment", "-0", "--null"}:
                remaining.pop(0)
                continue
            if token in {"-u", "--unset", "-C", "--chdir", "-P"}:
                remaining = remaining[2:] if len(remaining) > 1 else []
                continue
            if token.startswith(("--unset=", "--chdir=")):
                remaining.pop(0)
                continue
            if token in {"-S", "--split-string"} and len(remaining) > 1:
                try:
                    split = shlex.split(remaining[1], posix=True)
                except ValueError:
                    return []
                remaining = split + remaining[2:]
                break
            if token.startswith("-"):
                remaining.pop(0)
                continue
            break
    while remaining and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", remaining[0]):
        remaining.pop(0)
    while remaining and remaining[0] in {"(", "{"}:
        remaining.pop(0)
    if remaining and remaining[0] == "--":
        remaining.pop(0)
    return remaining


def _timeout_execution_payload(tokens: list[str]) -> list[str]:
    index = 1
    no_value = {"--preserve-status", "--foreground", "-v", "--verbose"}
    with_value = {"-k", "--kill-after", "-s", "--signal"}
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            index += 1
            break
        if token in no_value:
            index += 1
            continue
        if token in with_value:
            index += 2 if index + 1 < len(tokens) else 1
            continue
        if token.startswith(("--kill-after=", "--signal=")):
            index += 1
            continue
        if token.startswith("-"):
            return []
        break
    if index >= len(tokens) or _TIMEOUT_DURATION.fullmatch(tokens[index]) is None:
        return []
    return tokens[index + 1 :]


def _unwrap_execution_target(tokens: list[str]) -> list[str]:
    remaining = _strip_command_prefix(tokens)
    while remaining:
        executable = Path(remaining[0]).name.lower()
        if executable in {"timeout", "gtimeout"}:
            if _bounded_timeout_command(remaining):
                return []
            remaining = _strip_command_prefix(_timeout_execution_payload(remaining))
            continue
        if executable == "command" and len(remaining) > 1 and remaining[1] in {"-v", "-V"}:
            return []
        if executable not in {
            "arch", "builtin", "caffeinate", "command", "exec", "nohup", "time", "nice",
        }:
            return remaining
        index = 1
        while index < len(remaining) and remaining[index].startswith("-"):
            token = remaining[index]
            if token == "--":
                index += 1
                break
            if executable == "exec" and token == "-a" and index + 1 < len(remaining):
                index += 2
                continue
            if executable == "arch" and token in {"-arch", "-d", "-e"} and index + 1 < len(remaining):
                index += 2
                continue
            if executable == "caffeinate" and token in {"-t", "-w"} and index + 1 < len(remaining):
                index += 2
                continue
            if executable in {"time", "nice"} and token in {
                "-f", "--format", "-o", "--output", "-n", "--adjustment",
            } and index + 1 < len(remaining):
                index += 2
                continue
            index += 1
        remaining = _strip_command_prefix(remaining[index:])
    return []


def _code_stdin_sink(tokens: list[str]) -> bool:
    target = _unwrap_execution_target(tokens)
    if not target:
        return False
    executable = target[0] if target[0] == "." else Path(target[0]).name.lower()
    if executable in {"bash", "sh", "zsh"}:
        return _shell_reads_pipeline_stdin(target)
    return executable in {".", "source"} and any(
        token in {"-", "/dev/fd/0", "/dev/stdin"} for token in target[1:]
    )


def _shell_reads_pipeline_stdin(tokens: list[str]) -> bool:
    remaining = _strip_command_prefix(tokens)
    while remaining and remaining[0] in {"(", "{"}:
        remaining.pop(0)
        remaining = _strip_command_prefix(remaining)
    if not remaining:
        return False
    executable = Path(remaining[0]).name.lower()
    if executable in {"timeout", "gtimeout"} and _bounded_timeout_command(remaining):
        return False
    remaining = _unwrap_execution_target(remaining)
    if not remaining:
        return False
    executable = Path(remaining[0]).name.lower()
    if executable not in {"bash", "sh", "zsh"}:
        return False
    index = 1
    force_stdin = False
    while index < len(remaining):
        token = remaining[index]
        if token in {")", "}"}:
            index += 1
            continue
        if token == "--":
            index += 1
            break
        if token in {"-c", "--command"} or (
            token.startswith("-") and not token.startswith("--") and "c" in token[1:]
        ):
            return False
        if token == "-n" or (
            token.startswith("-") and not token.startswith("--") and "n" in token[1:]
        ):
            return False
        if token == "-s" or (
            token.startswith("-") and not token.startswith("--") and "s" in token[1:]
        ):
            force_stdin = True
            index += 1
            continue
        if token in {"-O", "-o"} and index + 1 < len(remaining):
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        return force_stdin
    return force_stdin or index >= len(remaining) or all(
        token in {")", "}"} for token in remaining[index:]
    )


def _pipeline_executes_shell_input(command: str) -> bool:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return False
    for index, token in enumerate(tokens):
        if token not in {"|", "|&"}:
            continue
        consumer: list[str] = []
        for candidate in tokens[index + 1 :]:
            if candidate in _SHELL_SEPARATORS or (
                candidate and set(candidate) <= {";", "&", "|"}
            ):
                break
            consumer.append(candidate)
        if _code_stdin_sink(consumer):
            return True
    return False


def _redirection_executes_shell_input(command: str) -> bool:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()<>=")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return False
    for index, token in enumerate(tokens):
        if token not in {"<(", ">(", "=("}:
            continue
        prefix: list[str] = []
        for candidate in reversed(tokens[:index]):
            if candidate in _SHELL_SEPARATORS or (
                candidate and set(candidate) <= {";", "&", "|"}
            ):
                break
            prefix.insert(0, candidate)
        target = _unwrap_execution_target(prefix)
        code_file_sink = bool(target) and (
            target[0] == "."
            or Path(target[0]).name.lower() in {"bash", "sh", "source", "zsh"}
        )
        if token == "=(" and not code_file_sink:
            continue
        inner: list[str] = []
        depth = 1
        for candidate in tokens[index + 1 :]:
            if candidate in {"(", "<(", ">(", "=("}:
                depth += 1
            elif candidate == ")":
                depth -= 1
                if depth == 0:
                    break
            inner.append(candidate)
        if code_file_sink or _shell_reads_pipeline_stdin(inner) or _persistent_foreground_command(
            " ".join(inner)
        ):
            return True
    segment_start = 0
    while segment_start < len(tokens):
        segment_end = segment_start
        while segment_end < len(tokens):
            candidate = tokens[segment_end]
            if candidate in _SHELL_SEPARATORS or (
                candidate and set(candidate) <= {";", "&", "|"}
            ):
                break
            segment_end += 1
        current = tokens[segment_start:segment_end]
        stripped: list[str] = []
        index = 0
        had_input_redirection = False
        while index < len(current):
            token = current[index]
            if (
                token.isdigit()
                and index + 1 < len(current)
                and current[index + 1].startswith("<")
            ):
                index += 1
                continue
            if token.startswith("<") and token != "<(":
                had_input_redirection = True
                index += 1
                if index < len(current) and current[index] in {"<(", ">(", "=("}:
                    depth = 1
                    index += 1
                    while index < len(current) and depth:
                        if current[index] in {"(", "<(", ">(", "=("}:
                            depth += 1
                        elif current[index] == ")":
                            depth -= 1
                        index += 1
                elif index < len(current):
                    index += 1
                continue
            stripped.append(token)
            index += 1
        if had_input_redirection and _code_stdin_sink(stripped):
            return True
        segment_start = segment_end + 1
    return False


def _persistent_runner_payload(tokens: list[str]) -> bool:
    remaining = list(tokens)
    value_options = {
        "-p", "--package", "--cache", "--cwd", "--dir", "--prefix", "--workspace",
        "--script-shell",
    }
    while remaining:
        token = remaining[0].lower()
        if token == "--":
            remaining.pop(0)
            break
        if token in {"-c", "--call"} and len(remaining) > 1:
            return (
                "$" in remaining[1]
                or "`" in remaining[1]
                or _persistent_foreground_command(remaining[1])
            )
        if token.startswith(("--call=", "-c=")):
            payload = remaining[0].split("=", 1)[1]
            return "$" in payload or "`" in payload or _persistent_foreground_command(payload)
        if token in value_options:
            remaining = remaining[2:] if len(remaining) > 1 else []
            continue
        if token.startswith("-"):
            remaining.pop(0)
            continue
        break
    return _persistent_foreground_segment(remaining)


def _persistent_script_name(value: str) -> bool:
    parts = [
        part for part in re.split(r"[:/._-]+", value.strip().lower()) if part
    ]
    if not parts:
        return False
    part_set = set(parts)
    strong_persistent = {
        "dev", "develop", "emulator", "preview", "runserver", "serve",
        "simulator", "start", "watch",
    }
    if part_set & strong_persistent:
        return True
    safe_semantics = {"build", "check", "lint", "smoke", "test", "typecheck"}
    if part_set & safe_semantics:
        return False
    return bool(part_set & {"server", "storybook"})


def _bounded_timeout_command(tokens: list[str]) -> bool:
    index = 1
    no_value = {"--preserve-status", "--foreground", "-v", "--verbose"}
    with_value = {"-k", "--kill-after", "-s", "--signal"}
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            index += 1
            break
        if token in no_value:
            index += 1
            continue
        if token in with_value:
            if index + 1 >= len(tokens):
                return False
            index += 2
            continue
        if token.startswith(("--kill-after=", "--signal=")):
            index += 1
            continue
        if token.startswith("-"):
            return False
        break
    if index + 1 >= len(tokens):
        return False
    match = _TIMEOUT_DURATION.fullmatch(tokens[index])
    if match is None:
        return False
    numeric = re.match(r"\d+(?:\.\d+)?", tokens[index])
    return numeric is not None and float(numeric.group(0)) > 0


def _persistent_signature_anywhere(tokens: list[str]) -> bool:
    """Conservatively catch explicit server signatures behind unknown wrappers."""
    if not tokens:
        return False
    names = [Path(token).name.lower() for token in tokens]
    if names[0] in {
        "awk", "cat", "echo", "find", "git", "grep", "head", "ls", "printf",
        "rg", "sed", "tail", "type", "whereis", "which",
    }:
        return False
    for index, name in enumerate(names):
        if name == "--watch":
            return True
        if name in _PERSISTENT_EXECUTABLES:
            if index + 1 < len(tokens) and tokens[index + 1] in {
                "--help", "-h", "--version", "-v", "-V"
            }:
                continue
            return True
        if index > 0 and name in {"pnpm", "npm", "yarn", "bun", "npx", "bunx"}:
            if _persistent_foreground_segment(tokens[index:]):
                return True
        if name == "tsx" and "watch" in names[index + 1 :]:
            return True
        if name.startswith("python") and names[index + 1 : index + 3] == ["-m", "http.server"]:
            return True
        if name == "next" and index + 1 < len(names) and names[index + 1] == "dev":
            return True
        if name == "next" and index + 1 < len(names) and names[index + 1] == "start":
            return True
        if name == "webpack" and index + 1 < len(names) and names[index + 1] in {"serve", "watch"}:
            return True
        if name == "flask" and index + 1 < len(names) and names[index + 1] == "run":
            return True
        if name == "make" and any(
            _persistent_script_name(candidate)
            for candidate in names[index + 1 :]
            if not candidate.startswith("-")
        ):
            return True
        if index > 0 and _persistent_script_name(name):
            return True
    return False


def _xargs_execution_payload(tokens: list[str]) -> list[str]:
    remaining = list(tokens[1:])
    value_options = {
        "-E", "--eof", "-I", "--replace", "-J", "-L", "--max-lines", "-n",
        "--max-args", "-P", "--max-procs", "-s", "--max-chars",
    }
    while remaining:
        token = remaining[0]
        if token == "--":
            return remaining[1:]
        if token in value_options:
            remaining = remaining[2:] if len(remaining) > 1 else []
            continue
        if token.startswith(tuple(f"{option}=" for option in value_options)):
            remaining.pop(0)
            continue
        if token.startswith("-"):
            remaining.pop(0)
            continue
        break
    return remaining


def _persistent_foreground_segment(tokens: list[str]) -> bool:
    tokens = _strip_command_prefix(tokens)
    if not tokens:
        return False
    executable = Path(tokens[0]).name.lower()
    if executable in {"[", "[["}:
        return False
    if executable == "builtin":
        return _persistent_foreground_segment(tokens[1:])
    if executable == "eval":
        payload = tokens[1:]
        if not payload:
            return False
        if any("$" in token or "`" in token for token in payload):
            return True
        return _persistent_foreground_command(" ".join(payload))
    if executable == "xargs":
        payload = _unwrap_execution_target(_xargs_execution_payload(tokens))
        if not payload:
            return False
        if Path(payload[0]).name.lower() in {"bash", "sh", "zsh"}:
            return True
        return _persistent_foreground_segment(payload)
    if executable in {"timeout", "gtimeout"}:
        if _bounded_timeout_command(tokens):
            return False
        return _persistent_signature_anywhere(tokens)
    if executable in {"bash", "sh", "zsh"}:
        for index, token in enumerate(tokens[1:], start=1):
            if token == "-n" or (
                token.startswith("-")
                and not token.startswith("--")
                and "n" in token[1:]
            ):
                return False
            if (
                token.startswith("-")
                and not token.startswith("--")
                and "c" in token[1:]
                and index + 1 < len(tokens)
            ):
                payload = tokens[index + 1]
                return (
                    "$" in payload
                    or "`" in payload
                    or _persistent_foreground_command(payload)
                )
        return False
    lowered = [token.lower() for token in tokens]
    if len(tokens) == 2 and tokens[1] in {
        "--help", "-h", "--version", "-v", "-V"
    }:
        return False
    if executable in {"command", "exec", "nohup", "time"}:
        index = 1
        if executable == "command" and len(tokens) > 1 and tokens[1] in {"-v", "-V"}:
            return False
        while index < len(tokens) and tokens[index].startswith("-"):
            if tokens[index] == "--":
                index += 1
                break
            if executable == "exec" and tokens[index] == "-a" and index + 1 < len(tokens):
                index += 2
                continue
            if executable == "time" and tokens[index] in {
                "-f", "--format", "-o", "--output"
            } and index + 1 < len(tokens):
                index += 2
                continue
            index += 1
        return _persistent_foreground_segment(tokens[index:])
    if executable == "nice":
        index = 1
        while index < len(tokens) and tokens[index].startswith("-"):
            token = lowered[index]
            if token in {"-n", "--adjustment"} and index + 1 < len(tokens):
                index += 2
                continue
            index += 1
        return _persistent_foreground_segment(tokens[index:])
    if executable == "corepack" and len(tokens) > 1:
        index = 1
        while index < len(tokens) and tokens[index].startswith("-"):
            index += 1
        if index < len(tokens) and lowered[index] in {"pnpm", "npm", "yarn", "bun"}:
            return _persistent_foreground_segment(tokens[index:])
        return False
    if executable in {"npx", "bunx"} and len(tokens) > 1:
        return _persistent_runner_payload(tokens[1:])
    if executable == "yarn" and len(tokens) > 2 and lowered[1] == "dlx":
        return _persistent_runner_payload(tokens[2:])
    if executable == "tsx" and "watch" in lowered[1:]:
        return True
    if (
        executable.startswith("python")
        and len(lowered) >= 3
        and lowered[1:3] == ["-m", "http.server"]
    ):
        return True
    if executable == "next" and len(lowered) > 1 and lowered[1] in {"dev", "start"}:
        return True
    if executable == "webpack" and len(lowered) > 1 and lowered[1] in {"serve", "watch"}:
        return True
    if executable == "flask" and len(lowered) > 1 and lowered[1] == "run":
        return True
    if executable == "make" and any(
        _persistent_script_name(token)
        for token in lowered[1:]
        if not token.startswith("-")
    ):
        return True
    if executable == "find":
        for index, token in enumerate(tokens[1:], start=1):
            if (
                token in {"-exec", "-execdir"}
                and index + 1 < len(tokens)
                and _persistent_foreground_segment(tokens[index + 1 :])
            ):
                return True
        return False
    if "--watch" in lowered or executable in _PERSISTENT_EXECUTABLES:
        return True
    if executable in {"pnpm", "npm", "yarn", "bun"}:
        index = 1
        while index < len(lowered):
            token = lowered[index]
            if token in {
                "--filter", "--dir", "-c", "--cwd", "--prefix", "--workspace", "-w"
            } and index + 1 < len(lowered):
                index += 2
                continue
            if token.startswith(("--filter=", "--dir=", "--cwd=", "--prefix=", "--workspace=")):
                index += 1
                continue
            if token.startswith("-"):
                index += 1
                continue
            break
        command = lowered[index] if index < len(lowered) else ""
        if command == "run" and index + 1 < len(lowered):
            command = lowered[index + 1]
        if _persistent_script_name(command):
            return True
        if command in {"exec", "dlx", "x"} and index + 1 < len(tokens):
            return _persistent_runner_payload(tokens[index + 1 :])
        return False
    return _persistent_signature_anywhere(tokens)


def _persistent_foreground_command(command: str) -> bool:
    if _pipeline_executes_shell_input(command) or _redirection_executes_shell_input(command):
        return True
    if any(
        _persistent_foreground_command(substitution)
        for substitution in (
            _shell_command_substitutions(command)
            + _shell_parenthesized_execution_groups(command)
        )
    ):
        return True
    return any(
        _persistent_foreground_segment(segment)
        for segment in _command_segments(command)
    )


def registered_controller_foreground_denial(
    tool_name: Any, tool_input: Any
) -> str | None:
    """Reject an unbounded foreground server only for the current Controller.

    The caller performs the exact current-target fence before using this result.
    The reason is deliberately constant so command contents never enter receipts.
    """
    normalized_tool = (
        str(tool_name or "").strip().rsplit(".", 1)[-1].rsplit("__", 1)[-1].lower()
    )
    if normalized_tool not in _COMMAND_EXECUTION_TOOLS or not isinstance(tool_input, dict):
        return None
    initial_yield = tool_input.get("yield_time_ms")
    if isinstance(initial_yield, (int, float)) and not isinstance(initial_yield, bool):
        if 0 < initial_yield <= 5000:
            return None
    command = tool_input.get("command", tool_input.get("cmd"))
    if not isinstance(command, str) or not command.strip():
        return None
    if not _persistent_foreground_command(command):
        return None
    return (
        "Registered Controller cannot run an unbounded development/watch process "
        "in foreground command execution; use a persistent terminal, an initial "
        "yield of at most 5000 ms, or an explicitly bounded exit."
    )


def _desktop_canary_identity(
    *, hooks_path: Path, skill_root: Path | None
) -> dict[str, Any]:
    root = (skill_root or Path(__file__).resolve().parents[1]).resolve()
    lifecycle = root / "scripts" / "lifecycle_hook.py"
    target_guard_path = root / "scripts" / "controller_target_guard.py"
    return {
        "schema_version": 7,
        "skill_root": str(root),
        "hooks_sha256": sha256_bytes(hooks_path.read_bytes()),
        "lifecycle_sha256": sha256_bytes(lifecycle.read_bytes()),
        "controller_target_guard_sha256": sha256_bytes(target_guard_path.read_bytes()),
    }


def arm_desktop_canary(
    controller_session_id: str,
    *,
    repo: Path | None = None,
    registry_path: Path = REGISTRY_PATH,
    execution_target_session_id: str | None = None,
    target_generation: int | None = None,
    ownership_generation: int | None = None,
    canary_path: Path = DESKTOP_CANARY_PATH,
    hooks_path: Path = CODEX_HOOKS_PATH,
    skill_root: Path | None = None,
) -> dict[str, Any]:
    controller_id = controller_session_id.strip()
    if not controller_id:
        raise ValueError("controller session is required to arm desktop canary")
    canonical_repo: str | None = None
    controller_registry_path: str | None = None
    if repo is not None:
        canonical_root = canonical_main_root(repo)
        if canonical_root is None:
            raise ValueError("desktop canary requires a canonical Git project")
        with target_guard.locked_registry(registry_path) as registry:
            _validated_controller_registry(
                registry=registry,
                controller_id=controller_id,
                canonical_root=canonical_root,
            )
            if target_guard.unique_controller_id_for_repo_in_registry(
                canonical_root, registry
            ) != controller_id:
                raise PermissionError(
                    "desktop canary requires the unique project Controller"
                )
            target_record = target_guard.target_record(
                registry, controller_id=controller_id, host=DESKTOP_SESSION_HOST
            )
            ownership_record = target_guard.execution_ownership_record(
                registry, controller_id=controller_id
            )
            if target_record is None or ownership_record is None:
                raise PermissionError(
                    "desktop canary requires current target and execution ownership records"
                )
            target_status, registered_target, registered_target_generation = (
                target_guard.validate_target_record(
                    target_record, host=DESKTOP_SESSION_HOST
                )
            )
            ownership_host, ownership_target, registered_ownership_generation = (
                target_guard.validate_execution_ownership_record(ownership_record)
            )
            if (
                target_status != "active"
                or ownership_host != DESKTOP_SESSION_HOST
                or not registered_target
                or registered_target != ownership_target
            ):
                raise PermissionError(
                    "desktop canary target does not match current execution ownership"
                )
        requested = (
            (execution_target_session_id, registered_target, "execution target session"),
            (target_generation, registered_target_generation, "target generation"),
            (ownership_generation, registered_ownership_generation, "ownership generation"),
        )
        for supplied, registered, label in requested:
            if supplied is not None and supplied != registered:
                raise PermissionError(f"{label} does not match the controller registry")
        execution_target_session_id = registered_target
        target_generation = registered_target_generation
        ownership_generation = registered_ownership_generation
        canonical_repo = str(canonical_root.resolve())
        controller_registry_path = str(registry_path.expanduser().resolve())
    if (
        execution_target_session_id is None
        or target_generation is None
        or ownership_generation is None
    ):
        raise ValueError(
            "schema 7 desktop canary requires exact target and ownership generations"
        )
    target_session_id = str(execution_target_session_id).strip()
    if not target_session_id:
        raise ValueError("execution target session is required to arm desktop canary")
    for generation, label in (
        (target_generation, "target generation"),
        (ownership_generation, "ownership generation"),
    ):
        if generation is not None and (
            isinstance(generation, bool) or not isinstance(generation, int) or generation < 1
        ):
            raise ValueError(f"{label} must be a positive integer")
    identity = _desktop_canary_identity(
        hooks_path=hooks_path, skill_root=skill_root
    )
    now = datetime.now(timezone.utc).isoformat()
    receipt = {
        **identity,
        "status": "armed",
        "controller_id": controller_id,
        "controller_session_id": controller_id,
        "execution_target_session_id": target_session_id,
        "target_generation": target_generation,
        "ownership_generation": ownership_generation,
        "canonical_repo": canonical_repo,
        "controller_registry_path": controller_registry_path,
        "run_id": secrets.token_hex(16),
        "sequence_index": 0,
        "observations": [],
        "armed_at": now,
        "updated_at": now,
    }
    canary_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = canary_path.with_suffix(canary_path.suffix + ".lock")
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            write_json(canary_path, receipt)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return receipt


def record_desktop_canary_observation(
    event: dict[str, Any],
    output: dict[str, Any],
    state: dict[str, Any],
    *,
    canary_path: Path = DESKTOP_CANARY_PATH,
    hooks_path: Path = CODEX_HOOKS_PATH,
    skill_root: Path | None = None,
) -> dict[str, Any]:
    identity = _desktop_canary_identity(
        hooks_path=hooks_path, skill_root=skill_root
    )
    canary_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = canary_path.with_suffix(canary_path.suffix + ".lock")
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            current = load_json(canary_path)
            if any(current.get(key) != value for key, value in identity.items()):
                return current
            if current.get("status") not in {"armed", "pending"}:
                return current
            session_id = str(
                event.get("source_session_id") or event.get("session_id", "")
            ).strip()
            execution_target_session_id = str(
                current.get("execution_target_session_id")
                or current.get("controller_session_id", "")
            ).strip()
            if session_id != execution_target_session_id:
                return current
            expected_target_generation = current.get("target_generation")
            if (
                expected_target_generation is not None
                and event.get("controller_target_generation") != expected_target_generation
            ):
                return current
            if str(event.get("controller_session_id") or "").strip() != str(
                current.get("controller_id") or ""
            ).strip():
                return current
            if event.get("controller_ownership_generation") != current.get(
                "ownership_generation"
            ):
                return current
            if event.get("controller_registry_path") != current.get(
                "controller_registry_path"
            ):
                return current
            observations = [
                str(item) for item in current.get("observations", []) if str(item).strip()
            ]
            event_name = str(event.get("hook_event_name", ""))
            turn_id = _event_turn_id(event) or str(state.get("active_turn_id", ""))
            decision = output.get("hookSpecificOutput")
            denied = (
                isinstance(decision, dict)
                and decision.get("permissionDecision") == "deny"
            )
            index = int(current.get("sequence_index", 0) or 0)
            observation = ""
            if index == 0 and event_name == "PreToolUse" and not denied and turn_id:
                observation = "pre_tool_allowed"
                current["first_turn_id"] = turn_id
            elif index == 1 and event_name == "PostToolUse" and state.get("must_yield") is not True:
                observation = "post_tool_observed"
            elif (
                index == 2
                and event_name == "PostToolUse"
                and state.get("must_yield") is True
                and str(state.get("receipt_turn_id", "")) == turn_id
            ):
                observation = "receipt_latched"
                current["receipt_latched_turn_id"] = turn_id
            elif (
                index == 3
                and event_name == "PreToolUse"
                and not denied
                and turn_id == str(current.get("receipt_latched_turn_id", ""))
                and state.get("must_yield") is not True
                and state.get("pending_control_event") is True
                and "post_receipt_action_started" in state.get("triggers", [])
            ):
                observation = "same_turn_continuation_invalidated_receipt"
            elif (
                index == 4
                and event_name == "PostToolUse"
                and turn_id == str(current.get("receipt_latched_turn_id", ""))
                and state.get("must_yield") is not True
                and state.get("pending_control_event") is True
                and "post_receipt_action_started" in state.get("triggers", [])
            ):
                observation = "post_invalidation_tool_observed"
            elif index == 5 and event_name == "Stop":
                observation = "stop_observed"
            elif (
                index == 6
                and event_name == "PostToolUse"
                and state.get("must_yield") is True
                and str(state.get("receipt_turn_id", "")) == turn_id
                and turn_id == str(current.get("receipt_latched_turn_id", ""))
            ):
                observation = "post_stop_receipt_latched"
            elif (
                index == 7
                and event_name == "PreToolUse"
                and not denied
                and turn_id == str(current.get("receipt_latched_turn_id", ""))
                and state.get("must_yield") is not True
                and state.get("pending_control_event") is True
                and "post_receipt_action_started" in state.get("triggers", [])
            ):
                observation = "post_stop_continuation_invalidated_receipt"
            if observation:
                observations.append(observation)
                current["sequence_index"] = index + 1
                current["status"] = "pending"
            current["observations"] = observations
            if int(current.get("sequence_index", 0) or 0) == len(DESKTOP_CANARY_SEQUENCE):
                current["status"] = "passed"
                current["completed_at"] = datetime.now(timezone.utc).isoformat()
            current["last_turn_id"] = turn_id
            current["updated_at"] = datetime.now(timezone.utc).isoformat()
            write_json(canary_path, current)
            return current
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


LOCAL_GIT_TIMEOUT_SECONDS = 2.0


def run_git(
    root: Path, *args: str, deadline_monotonic: float | None = None
) -> str:
    timeout: float | None = None
    if deadline_monotonic is not None:
        target_guard._require_before_deadline(
            deadline_monotonic, operation="Runtime snapshot Git"
        )
        timeout = min(
            LOCAL_GIT_TIMEOUT_SECONDS,
            max(0.001, deadline_monotonic - time.monotonic()),
        )
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError("Runtime snapshot Git exceeded its deadline/timeout") from exc
    return completed.stdout.strip()


def _run_git_with_optional_deadline(
    root: Path, *args: str, deadline_monotonic: float | None
) -> str:
    if deadline_monotonic is None:
        return run_git(root, *args)
    return run_git(root, *args, deadline_monotonic=deadline_monotonic)


def git_common_dir(
    cwd: Path, *, deadline_monotonic: float | None = None
) -> Path | None:
    try:
        value = _run_git_with_optional_deadline(
            cwd, "rev-parse", "--git-common-dir",
            deadline_monotonic=deadline_monotonic,
        )
    except TimeoutError:
        raise
    except (OSError, subprocess.CalledProcessError, ValueError):
        return None
    if not value:
        return None
    path = Path(value)
    return (path if path.is_absolute() else cwd / path).resolve()


def canonical_main_root(
    cwd: Path, *, deadline_monotonic: float | None = None
) -> Path | None:
    try:
        worktrees = _run_git_with_optional_deadline(
            cwd, "worktree", "list", "--porcelain",
            deadline_monotonic=deadline_monotonic,
        )
    except TimeoutError:
        raise
    except (OSError, subprocess.CalledProcessError, ValueError):
        return None
    worktree: Path | None = None
    for line in worktrees.splitlines():
        if line.startswith("worktree "):
            worktree = Path(line.removeprefix("worktree ")).resolve()
        elif line == "branch refs/heads/main" and worktree is not None:
            return worktree
    return None


def project_snapshot(
    cwd: Path, *, deadline_monotonic: float | None = None
) -> dict[str, Any] | None:
    try:
        invocation_root = Path(_run_git_with_optional_deadline(
            cwd, "rev-parse", "--show-toplevel",
            deadline_monotonic=deadline_monotonic,
        )).resolve()
    except TimeoutError:
        raise
    except (OSError, subprocess.CalledProcessError, ValueError):
        return None
    common_dir = git_common_dir(
        invocation_root, deadline_monotonic=deadline_monotonic
    )
    root = canonical_main_root(
        invocation_root, deadline_monotonic=deadline_monotonic
    )
    if common_dir is None or root is None or git_common_dir(
        root, deadline_monotonic=deadline_monotonic
    ) != common_dir:
        return None
    ledger = next((root / name for name in LEDGER_NAMES if (root / name).is_file()), None)
    if ledger is None:
        return None
    text = ledger.read_text(encoding="utf-8")
    try:
        from ledger_consistency_guard import validate_ledger
    except ModuleNotFoundError:
        from scripts.ledger_consistency_guard import validate_ledger

    ledger_errors = validate_ledger(text)
    ledger_rows = task_rows(text)
    ledger_states = {identifier: status for identifier, status in ledger_rows}
    ready_ids = sorted(
        identifier
        for identifier, status in ledger_rows
        if status == "READY"
    )
    runnable_projection = derive_runnable_tasks(task_records(text))
    runnable_ids = list(runnable_projection["runnable_task_ids"])
    derived_slices = dict(runnable_projection.get("derived_slices", {}))
    try:
        from ledger_consistency_guard import parent_child_projection
    except ModuleNotFoundError:
        from scripts.ledger_consistency_guard import parent_child_projection
    parent_child = parent_child_projection(ledger_states)
    status = _run_git_with_optional_deadline(
        root, "status", "--porcelain=v1", "--untracked-files=no",
        deadline_monotonic=deadline_monotonic,
    )
    try:
        from control_event_guard import unmerged_worktree_candidates
    except ModuleNotFoundError:
        from scripts.control_event_guard import unmerged_worktree_candidates

    target_guard._require_before_deadline(
        deadline_monotonic, operation="Runtime snapshot candidate scan"
    )
    candidates = unmerged_worktree_candidates(root)
    target_guard._require_before_deadline(
        deadline_monotonic, operation="Runtime snapshot candidate scan completion"
    )
    try:
        from assignment_runtime import evaluate_lease, load_runtime_state, select_current_lease
    except ModuleNotFoundError:
        from scripts.assignment_runtime import evaluate_lease, load_runtime_state, select_current_lease

    runtime_state = load_runtime_state(root)
    leases = runtime_state.get("leases", {}) if isinstance(runtime_state, dict) else {}
    assignment_liveness: dict[str, dict[str, Any]] = {}
    for task_id, ledger_state in ledger_states.items():
        if ledger_state not in {"ACTIVE", "RECOVERING"}:
            continue
        matching = [
            lease for lease in leases.values()
            if isinstance(lease, dict)
            and str(lease.get("task_id", "")).strip() == task_id
        ] if isinstance(leases, dict) else []
        lease = select_current_lease(matching)
        if lease is None:
            assignment_liveness[task_id] = {"ledger_state": ledger_state, "state": "unknown", "reason": "missing_runtime_lease"}
            continue
        decision = evaluate_lease(lease)
        assignment_liveness[task_id] = {"ledger_state": ledger_state, **decision}
    task_projection = {
        task_id: project_task_state(
            ledger_state,
            runtime=assignment_liveness.get(task_id),
        )
        for task_id, ledger_state in ledger_states.items()
    }
    try:
        rule_handshake = evaluate_rule_handshake(root, ledger=ledger)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        rule_handshake = {"state": "integrity_error", "blocking": True, "installed_revision": None, "errors": [str(error)]}

    controller_corrections: list[dict[str, Any]] = []
    try:
        registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        registry = {}
    owners = sorted(
        controller_id
        for controller_id, registered_path in registry.items()
        if isinstance(controller_id, str)
        and not controller_id.startswith("__")
        and isinstance(registered_path, str)
        and Path(registered_path).expanduser().resolve() == root
    ) if isinstance(registry, dict) else []
    if len(owners) == 1:
        try:
            from control_event_guard import open_controller_corrections
        except ModuleNotFoundError:
            from scripts.control_event_guard import open_controller_corrections
        controller_corrections = open_controller_corrections(root, owners[0])
    try:
        head = _run_git_with_optional_deadline(
            root, "rev-parse", "HEAD", deadline_monotonic=deadline_monotonic
        )
    except TimeoutError:
        raise
    except (OSError, subprocess.CalledProcessError, ValueError):
        head = None
    return {
        "root": str(root),
        "git_common_dir": str(common_dir),
        "ledger": str(ledger),
        "head": head,
        "ledger_sha256": sha256_bytes(ledger.read_bytes()),
        "worktree_status_sha256": sha256_bytes(status.encode("utf-8")),
        "ready_ids": ready_ids,
        "runnable_ids": runnable_ids,
        "runnable_exclusions": runnable_projection["exclusions"],
        "derived_slices": derived_slices,
        "task_states": ledger_states,
        "unfinished_child_ids": list(parent_child["unfinished_child_ids"]),
        "open_parent_ids": list(parent_child["open_parent_ids"]),
        "candidate_revisions": sorted(candidates.values()),
        "ledger_errors": ledger_errors,
        "assignment_liveness": assignment_liveness,
        "task_projection": task_projection,
        "controller_corrections": controller_corrections,
        "control_loop_required": True,
        "rule_handshake": rule_handshake,
    }


def successful_control_receipt(
    event: dict[str, Any], snapshot: dict[str, Any] | None = None,
    *, trusted_host_terminal_commit: dict[str, Any] | None = None,
) -> bool:
    handshake = snapshot.get("rule_handshake", {}) if isinstance(snapshot, dict) else {}
    if isinstance(handshake, dict) and handshake.get("blocking") is True:
        policy = derive_rule_wake_policy(
            handshake, assignment_liveness=(snapshot or {}).get("assignment_liveness", {})
        )
        if policy != "after_event":
            return False
    if event.get("hook_event_name") != "PostToolUse":
        return False
    if event.get("controller_host") == "web":
        commit = trusted_host_terminal_commit
        tuple_value = commit.get("tuple") if isinstance(commit, dict) else None
        return bool(
            isinstance(commit, dict)
            and commit.get("schema_version") == 1
            and commit.get("provenance") == "host_tool_terminal_commit_v1"
            and isinstance(tuple_value, dict)
            and tuple_value.get("controller_id")
            == str(event.get("controller_session_id") or "").strip()
            and tuple_value.get("host") == "web"
            and tuple_value.get("execution_target_session_id")
            == str(event.get("source_session_id") or "").strip()
            and tuple_value.get("turn_id") == _event_turn_id(event)
            and tuple_value.get("target_generation")
            == event.get("controller_target_generation")
            and tuple_value.get("ownership_generation")
            == event.get("controller_ownership_generation")
            and tuple_value.get("host_tool_execution_id") == _tool_use_id(event)
            and commit.get("terminal_receipt_sha256")
            and commit.get("guard_contract_sha256")
        )
    if event.get("controller_host") == DESKTOP_SESSION_HOST and not _tool_use_id(event):
        return False
    tool_input = event.get("tool_input")
    if not isinstance(tool_input, dict):
        return False
    command = str(tool_input.get("command", ""))
    controller_session_id = str(event.get("controller_session_id", "")).strip()
    if not _is_control_guard_command(
        command,
        controller_session_id=controller_session_id,
        cwd=event.get("cwd"),
    ):
        return False
    if snapshot and snapshot.get("candidate_revisions") and "--repo" not in command:
        return False
    response = event.get("tool_response")
    if not isinstance(response, dict) or response.get("exit_code") not in (None, 0):
        return False
    output = json.dumps(response, ensure_ascii=False)
    if "control-event: allowed" not in output:
        return False
    if event.get("rollout_recovery") is not None:
        return _verified_rollout_control_receipt(event, snapshot)
    return True


def _reconcile_desktop_rollout(
    state: dict[str, Any],
    event: dict[str, Any],
    snapshot: dict[str, Any],
    *,
    recovered_event_observer: Callable[
        [dict[str, Any], dict[str, Any], dict[str, Any]], None
    ] | None = None,
) -> dict[str, Any]:
    inflight = {
        str(item) for item in state.get("inflight_tool_use_ids", []) if str(item)
    }
    if not inflight:
        return state
    records = state.get("inflight_tool_records")
    records = records if isinstance(records, dict) else {}
    for item in _desktop_rollout_completed_items(event, state):
        tool_use_id = str(item.get("id") or "").strip()
        if tool_use_id not in inflight:
            continue
        record = records.get(tool_use_id)
        if not isinstance(record, dict):
            continue
        if str(record.get("turn_id") or "").strip() != _event_turn_id(event):
            continue
        item_type = str(item.get("type") or "").strip()
        if item_type == "CommandExecution":
            command = _rollout_command_text(item)
            if command is None:
                continue
            expected_command_sha256 = str(record.get("command_sha256") or "").strip()
            if (
                not expected_command_sha256
                or sha256_bytes(command.encode("utf-8")) != expected_command_sha256
            ):
                continue
            recovered_input = {"command": command}
        elif item_type == "FileChange":
            if str(record.get("tool_name") or "").strip() != "apply_patch":
                continue
            recovered_input = {}
        else:
            continue
        proposal = state.get("control_receipt_proposal")
        is_control_receipt = (
            str(state.get("control_receipt_inflight") or "").strip() == tool_use_id
        )
        recovered = {
            key: event[key]
            for key in (
                "session_id", "controller_session_id", "source_session_id",
                "controller_host", "controller_target_generation",
                "controller_ownership_generation", "controller_registry_path",
                "transcript_path", "cwd",
            )
            if key in event
        }
        recovered.update({
            "hook_event_name": "PostToolUse",
            "turn_id": _event_turn_id(event),
            "tool_name": str(record.get("tool_name") or "Bash"),
            "tool_use_id": tool_use_id,
            "tool_input": recovered_input,
            "tool_response": _rollout_tool_response(item),
            "rollout_recovery": {
                "source": "codex_rollout_item_completed",
                "tool_use_id": tool_use_id,
            },
        })
        input_sha256 = str(record.get("input_sha256") or "").strip()
        if input_sha256:
            recovered["recovered_input_sha256"] = input_sha256
        if is_control_receipt and isinstance(proposal, dict):
            recovered["rollout_control_receipt_proposal"] = dict(proposal)
        recovered_output, state = evaluate_event(
            recovered, snapshot=snapshot, prior_state=state
        )
        if recovered_event_observer is not None:
            recovered_event_observer(recovered, recovered_output, state)
        inflight = {
            str(value)
            for value in state.get("inflight_tool_use_ids", [])
            if str(value)
        }
        if is_control_receipt and state.get("must_yield") is not True:
            state.pop("control_receipt_proposal", None)
    return state


def lifecycle_triggers(
    snapshot: dict[str, Any], prior_state: dict[str, Any] | None
) -> list[str]:
    previous = prior_state.get("snapshot") if isinstance(prior_state, dict) else None
    previous = previous if isinstance(previous, dict) else None

    def newly_present(field: str) -> set[str]:
        current = {str(item) for item in snapshot.get(field, [])}
        if previous is None:
            return current
        return current - {str(item) for item in previous.get(field, [])}

    triggers = [f"READY:{identifier}" for identifier in newly_present("ready_ids")]
    triggers.extend(f"RUNNABLE:{identifier}" for identifier in newly_present("runnable_ids") if identifier not in set(snapshot.get("ready_ids", [])))
    triggers.extend(
        f"CANDIDATE:{revision}" for revision in newly_present("candidate_revisions")
    )
    triggers.extend(
        f"LEDGER_INVALID:{error}" for error in snapshot.get("ledger_errors", [])
    )
    corrections = snapshot.get("controller_corrections", [])
    if isinstance(corrections, list):
        for correction in corrections:
            if not isinstance(correction, dict):
                continue
            fingerprint = str(correction.get("fingerprint", "")).strip()
            if fingerprint:
                triggers.append(f"CORRECTION:{fingerprint}")
    liveness = snapshot.get("assignment_liveness", {})
    if isinstance(liveness, dict):
        for task_id, decision in liveness.items():
            if not isinstance(decision, dict):
                continue
            ledger_state = str(decision.get("ledger_state", "")).upper()
            state = str(decision.get("state", ""))
            reason = str(decision.get("reason", ""))
            if state == "budget_exhausted":
                triggers.append(f"recovery_budget_exhausted:{task_id}")
            elif ledger_state == "ACTIVE" and state == "unhealthy":
                label = "active_lease_expired" if reason == "lease_expired" else "assignment_became_unhealthy"
                triggers.append(f"{label}:{task_id}")
            elif ledger_state == "ACTIVE" and state == "terminal":
                triggers.append(f"agent_session_terminal:{task_id}")
            elif state == "progress_stale":
                triggers.append(f"active_without_progress:{task_id}")
            elif ledger_state == "RECOVERING" and state in {"unhealthy", "unknown", "terminal"}:
                triggers.append(f"recovery_stalled:{task_id}")
    handshake = snapshot.get("rule_handshake", {})
    if isinstance(handshake, dict):
        rule_state = str(handshake.get("state", ""))
        revision = str(handshake.get("installed_revision", "")).strip() or "unknown"
        if rule_state == "pending_ack":
            triggers.append(f"rule_update_pending:{revision}")
        elif rule_state == "ledger_stale":
            triggers.append(f"rule_ledger_stale:{revision}")
        elif rule_state == "pending_live_e2e":
            triggers.append(f"rule_live_e2e_pending:{revision}")
        elif rule_state == "integrity_error":
            triggers.append(f"rule_install_integrity_error:{revision}")

    if previous is not None:
        for field, label in (
            ("head", "main_head_changed"),
            ("ledger_sha256", "ledger_changed"),
            ("worktree_status_sha256", "main_worktree_changed"),
            ("ready_ids", "ready_set_changed"),
            ("candidate_revisions", "candidate_queue_changed"),
        ):
            if previous.get(field) != snapshot.get(field):
                triggers.append(label)
    return sorted(set(triggers))


WAKE_ENTRY_POINT = "wake_existing_controller"


def pending_event_fingerprint(state: dict[str, Any]) -> str:
    value = {
        "pending_control_event": state.get("pending_control_event") is True,
        "triggers": state.get("triggers", []),
        "wake_generation": int(state.get("wake_generation", 0) or 0),
    }
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return sha256_bytes(encoded)


def pending_wake_request(state: dict[str, Any]) -> dict[str, Any] | None:
    """Generic wake request for any pending controller event. Trigger type is evidence, not policy."""
    if not isinstance(state, dict) or state.get("pending_control_event") is not True:
        return None
    return {
        "entry_point": WAKE_ENTRY_POINT,
        "pending_control_event": True,
        "triggers": list(state.get("triggers", [])),
        "event_fingerprint": pending_event_fingerprint(state),
        "session_id": str(state.get("session_id", "")),
        "controller_host": state.get("controller_host"),
    }


def _non_rule_triggers(triggers: list[str] | set[str]) -> set[str]:
    return {
        str(item) for item in triggers
        if not str(item).startswith((
            "rule_update_pending:",
            "rule_ledger_stale:",
            "rule_live_e2e_pending:",
            "rule_install_integrity_error:",
        ))
    }



def _controller_action_context(event: dict[str, Any]) -> dict[str, str]:
    host = str(event.get("execution_host") or event.get("controller_host") or "").strip()
    controller_id = str(
        event.get("controller_session_id") or event.get("controller_id") or event.get("session_id") or ""
    ).strip()
    if host == "web":
        source = str(
            event.get("web_session_id") or event.get("source_session_id") or event.get("session_id") or ""
        ).strip()
    else:
        source = str(event.get("source_session_id") or event.get("session_id") or "").strip()
    return {
        "session_id": controller_id,
        "source_session_id": source,
        "execution_host": host,
    }


def continuation_reason(
    triggers: list[str],
    ready_ids: list[str],
    candidate_revisions: list[str],
    runnable_ids: list[str] | None = None,
    *,
    rule_handshake: dict[str, Any] | None = None,
    root: str | None = None,
    session_id: str | None = None,
    source_session_id: str | None = None,
    execution_host: str | None = None,
    next_action: str | None = None,
) -> str:
    runnable_ids = list(runnable_ids or ready_ids)
    ready = ", ".join(runnable_ids) if runnable_ids else "无可执行工作"
    trigger_text = ", ".join(triggers) if triggers else "未闭合控制事件"
    candidates = ", ".join(revision[:9] for revision in candidate_revisions) or "无未处理候选"
    rule_text = ""
    handshake = rule_handshake if isinstance(rule_handshake, dict) else {}
    state = str(handshake.get("state", ""))
    revision = str(handshake.get("installed_revision", "")).strip()
    if state == "pending_ack":
        summary = str(handshake.get("summary", "")).strip()
        impact = str(handshake.get("impact", "")).strip()
        stop = str(handshake.get("stop_condition", "")).strip()
        repo_arg = f" --repo {root}" if root else " --repo <repo>"
        session_arg = f" --controller-session {session_id}" if session_id else " --controller-session <controller-session>"
        host_arg = f" --execution-host {execution_host}" if execution_host else " --execution-host <web|desktop_codex>"
        source_arg = f" --source-session {source_session_id}" if source_session_id else " --source-session <current-execution-session>"
        handshake_script = Path(__file__).resolve().parent / "rule_handshake.py"
        rule_text = (
            f" 规则更新待加载：{revision}；摘要：{summary}；影响：{impact}；停止条件：{stop}。"
            "先读取已安装的新规则，再执行 "
            f'python3 "{handshake_script}" ack{repo_arg}{session_arg}{host_arg}{source_arg} --revision {revision}，随后同步现有台账规则版本行。'
        )
    elif state == "ledger_stale":
        rule_text = f" 已有 LOADED ACK {revision}，但台账规则版本仍旧；先把现有规则版本行同步到精确 revision {revision}。"
    elif state == "pending_live_e2e":
        repo_arg = f" --repo {root}" if root else " --repo <repo>"
        session_arg = f" --controller-session {session_id}" if session_id else " --controller-session <controller-session>"
        host_arg = f" --execution-host {execution_host}" if execution_host else " --execution-host <web|desktop_codex>"
        source_arg = f" --source-session {source_session_id}" if source_session_id else " --source-session <current-execution-session>"
        handshake_script = Path(__file__).resolve().parent / "rule_handshake.py"
        rule_text = (
            f" Runtime {revision} 已 ACK 且台账已同步，但真实续接 E2E 尚未闭合。"
            "当前同一 Controller 只允许安全控制回合，不得启动新的 Assignment；完成真实 confirmed wake 后的 CLOSED control cycle，再执行 "
            f'python3 "{handshake_script}" accept-live-e2e{repo_arg}{session_arg}{host_arg}{source_arg} --revision {revision}。'
        )
    elif state == "integrity_error":
        errors = "; ".join(str(item) for item in handshake.get("errors", []))
        rule_text = f" Adaptive Agent Runtime 安装完整性失败：{errors}；禁止 ACK 或启动受影响 Assignment。"
    actions = ["请立即核对真实 main / 台账 / live Agent"]
    pending_next_action = str(next_action or "").strip()
    if pending_next_action:
        actions.append(f"先完成已持久化的明确下一步：{pending_next_action}")
    if candidate_revisions:
        actions.append("处理候选审查、集成、验收")
    if runnable_ids:
        actions.append("处理可执行工作派发与 ACK；若客观上无法派发，必须把对应任务明确转为 BLOCKED 并记录可验证原因")
    if any(str(item).startswith("OPEN_CHILD:") for item in triggers):
        actions.append("处理开放父行下的未完成子项；父行不得在子项未 CLOSED/DONE 时结束")
    if any(str(item).startswith(("ACTIVE:", "VERIFY:", "RECOVERING:")) for item in triggers):
        actions.append("处理仍处于 ACTIVE/VERIFY/RECOVERING 的未完成工作，不得 idle/Yield")
    if runnable_ids or any(
        str(item).startswith(
            ("READY:", "RUNNABLE:", "OPEN_CHILD:", "ACTIVE:", "VERIFY:", "RECOVERING:")
        )
        for item in triggers
    ):
        actions.append(UNFINISHED_CONTINUATION_INSTRUCTION)
    actions.append("随后用 control_event_guard.py 生成通过收据")
    lifecycle = (
        "Adaptive Agent Runtime 生命周期门检测到尚未闭合的控制事件。"
        f"触发：{trigger_text}；当前可执行：{ready}；候选：{candidates}。"
        + "；".join(actions) + "。"
        "不得仅把动作写成下一事件后停止。" + rule_text
    )
    return lifecycle + "\n\n" + controller_self_check_context()


OBSERVATION_STATUS_QUERY = re.compile(
    r"(?:"
    r"进度(?:怎么样|如何|情况)?|工作(?:怎么样|情况如何|情况怎么样)?|"
    r"履职(?:情况)?(?:如何|怎么样)?|完成了(?:吗|什么)|好了吗|卡住了吗|"
    r"台账.{0,20}任务|还有没有任务|"
    r"status(?:\s+update)?|progress(?:\s+update)?|how(?:'s| is) it going"
    r")",
    re.IGNORECASE,
)
UNFINISHED_CONTINUATION_INSTRUCTION = "未完成项仍在，继续派发/验收"


KNOWN_NEXT_ACTION_MESSAGE = re.compile(
    r"(?:下一步|接下来|next(?:\s+step)?|then)"
    r".{0,80}?"
    r"(?:集成|派发|恢复|验收|验证|重算|回归|修复|dispatch|integrat|recover|verify|recompute|review)",
    re.IGNORECASE,
)


def _snapshot_has_immediate_controller_work(snapshot: dict[str, Any]) -> bool:
    if snapshot_continuation_debt_labels(snapshot):
        return True
    if snapshot.get("runnable_ids") or snapshot.get("candidate_revisions"):
        return True
    corrections = snapshot.get("controller_corrections")
    if isinstance(corrections, list) and corrections:
        return True
    liveness = snapshot.get("assignment_liveness")
    if isinstance(liveness, dict):
        for value in liveness.values():
            if not isinstance(value, dict):
                continue
            if str(value.get("state", "")).strip().lower() in {
                "terminal", "unhealthy", "progress_stale", "budget_exhausted"
            }:
                return True
    return False


def _assistant_declares_executable_next_action(
    event: dict[str, Any], snapshot: dict[str, Any]
) -> bool:
    if event.get("hook_event_name") != "Stop":
        return False
    message = str(event.get("last_assistant_message", "")).strip()
    return bool(
        message
        and KNOWN_NEXT_ACTION_MESSAGE.search(message)
        and _snapshot_has_immediate_controller_work(snapshot)
    )


def _is_observation_status_query(event: dict[str, Any]) -> bool:
    if event.get("hook_event_name") != "UserPromptSubmit":
        return False
    prompt = str(event.get("prompt", "")).strip()
    if not prompt or len(prompt) > 160:
        return False
    return bool(OBSERVATION_STATUS_QUERY.search(prompt))


def _unfinished_debt_labels(snapshot: dict[str, Any] | None) -> list[str]:
    if not isinstance(snapshot, dict):
        return []
    return list(snapshot_continuation_debt_labels(snapshot))


def _has_unfinished_continuation_debt(
    snapshot: dict[str, Any] | None,
    triggers: list[str] | set[str] | None = None,
) -> bool:
    if _unfinished_debt_labels(snapshot):
        return True
    return any(
        str(item).startswith(
            ("READY:", "RUNNABLE:", "OPEN_CHILD:", "ACTIVE:", "VERIFY:", "RECOVERING:")
        )
        for item in (triggers or [])
    )


def _observation_continuation_context(
    event: dict[str, Any],
    snapshot: dict[str, Any],
    state: dict[str, Any],
) -> str:
    pending_next_action = str(state.get("next_action") or "").strip()
    context = continuation_reason(
        list(state.get("triggers", [])),
        list(snapshot.get("ready_ids", [])),
        list(snapshot.get("candidate_revisions", [])),
        runnable_ids=list(snapshot.get("runnable_ids", snapshot.get("ready_ids", []))),
        rule_handshake=snapshot.get("rule_handshake"),
        root=snapshot.get("root"),
        **_controller_action_context(event),
        next_action=pending_next_action if pending_next_action and state.get("requires_user") is False else None,
    )
    if UNFINISHED_CONTINUATION_INSTRUCTION not in context:
        context += " " + UNFINISHED_CONTINUATION_INSTRUCTION + "。"
    return context


def _mark_yield_rejected(
    state: dict[str, Any], event: dict[str, Any], *, reason: str, trigger: str | None = None
) -> None:
    """Persist a rejected logical Yield so detached continuation cannot lose the edge."""
    state["pending_control_event"] = True
    if state.get("requires_user") is not True:
        state["requires_user"] = False
    triggers = {
        str(item) for item in state.get("triggers", []) if str(item).strip()
    }
    triggers.add("YIELD_GATE_REJECTED")
    if trigger:
        triggers.add(trigger)
    state["triggers"] = sorted(triggers)
    state["yield_rejected"] = True
    state["yield_rejected_turn_id"] = _event_turn_id(event)
    state["yield_rejected_reason"] = reason[:2048]
    state["yield_rejection_count"] = int(state.get("yield_rejection_count", 0) or 0) + 1


def evaluate_event(
    event: dict[str, Any],
    *,
    snapshot: dict[str, Any] | None,
    prior_state: dict[str, Any] | None,
    trusted_host_terminal_commit: dict[str, Any] | None = None,
    recovered_event_observer: Callable[
        [dict[str, Any], dict[str, Any], dict[str, Any]], None
    ] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if snapshot is None:
        return {}, {}
    state = dict(prior_state or {})
    prior_pending = bool((prior_state or {}).get("pending_control_event"))
    prior_generation = int((prior_state or {}).get("wake_generation", 0) or 0)
    if prior_pending and prior_generation <= 0:
        prior_generation = 1
    state["wake_generation"] = prior_generation
    state["snapshot"] = snapshot
    state["session_id"] = str(event.get("session_id", ""))
    state["source_session_id"] = str(
        event.get("source_session_id", event.get("session_id", ""))
    )
    event_host = str(event.get("controller_host", "")).strip()
    state["controller_host"] = event_host if event_host in {"web", "desktop_codex"} else "desktop_codex"
    goal_rebind_contract = event.get("goal_rebind_contract")
    if isinstance(goal_rebind_contract, dict):
        try:
            target_generation = event.get("controller_target_generation")
            if (
                str(goal_rebind_contract.get("controller_id") or "").strip()
                != str(event.get("controller_session_id") or "").strip()
                or str(goal_rebind_contract.get("execution_target_session_id") or "").strip()
                != state["source_session_id"]
                or str(goal_rebind_contract.get("host") or "").strip()
                != state["controller_host"]
                or goal_rebind_contract.get("target_generation") != target_generation
            ):
                raise ValueError("target Goal rebind binding changed")
            rebound = goal_display_sync.start_target_goal_rebind(
                state.get("goal_display_sync")
                if isinstance(state.get("goal_display_sync"), dict)
                else None,
                goal_rebind_contract,
                controller_id=str(event.get("controller_session_id") or "").strip(),
                source_session_id=state["source_session_id"],
                host=state["controller_host"],
                target_generation=target_generation,
                turn_id=_event_turn_id(event),
                host_capabilities=None,
            )
            completed_rebinds = {
                str(item)
                for item in state.get("goal_rebind_completed_fingerprints", [])
                if str(item).strip()
            }
            if (
                isinstance(rebound, dict)
                and str(rebound.get("fingerprint") or "") in completed_rebinds
            ):
                rebound = None
        except ValueError as error:
            rebound = {
                "schema_version": 2,
                "sync_kind": "target_goal_rebind",
                "status": "degraded",
                "reason": "TARGET_GOAL_REBIND_CONTRACT_INVALID",
                "detail": str(error),
            }
        if rebound is not None:
            state["goal_display_sync"] = rebound
            if rebound.get("status") != "completed":
                state["pending_control_event"] = True
                triggers = {
                    str(item) for item in state.get("triggers", []) if str(item).strip()
                }
                triggers.add("target_goal_rebind:" + str(rebound.get("status") or "unknown"))
                state["triggers"] = sorted(triggers)
    event_name = event.get("hook_event_name")
    turn_boundary_fault = _begin_turn(state, event)
    if turn_boundary_fault:
        return {"decision": "block", "reason": turn_boundary_fault}, state
    if "must_yield" not in state:
        state["must_yield"] = False
    if "tool_trace" not in state:
        state["tool_trace"] = []
    if event_name in {"PreToolUse", "Stop"}:
        state = _reconcile_desktop_rollout(
            state,
            event,
            snapshot,
            recovered_event_observer=recovered_event_observer,
        )
    if _is_observation_status_query(event):
        state["observation_query_only"] = True
        state["observation_query_turn_id"] = _event_turn_id(event)
        remaining_debt = _unfinished_debt_labels(snapshot)
        triggers = {
            str(item) for item in state.get("triggers", []) if str(item).strip()
        } | set(remaining_debt)
        keep_scheduling = bool(remaining_debt) or bool(state.get("pending_control_event")) or bool(triggers)
        if keep_scheduling and _has_unfinished_continuation_debt(snapshot, triggers):
            state["triggers"] = sorted(triggers)
            state["pending_control_event"] = True
            if state.get("requires_user") is not True:
                state["requires_user"] = False
            if not prior_pending:
                state["wake_generation"] = prior_generation + 1
            context = _observation_continuation_context(event, snapshot, state)
            return {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": context,
                }
            }, state
        if keep_scheduling:
            state["triggers"] = sorted(triggers)
            state["pending_control_event"] = True
        return {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": (
                    "Status query is observation-only. Report the current fact snapshot without "
                    "treating this user message as a continue/resume/scheduling signal. Existing "
                    "pending_control_event, runnable work, continuation debt, and Controller scoring "
                    "state remain governed by their pre-existing machine facts."
                ),
            }
        }, state

    if event_name in {"PreToolUse", "Stop"}:
        fault = _turn_fault(state, event)
        if fault:
            return _adapter_fault_output(state, event, fault), state
    if _assistant_declares_executable_next_action(event, snapshot):
        state["must_yield"] = False
        state.pop("receipt_turn_id", None)
        state.pop("receipt_tool_use_id", None)
        state["pending_control_event"] = True
        current_triggers = {
            str(item) for item in state.get("triggers", []) if str(item).strip()
        }
        current_triggers.add("KNOWN_NEXT_ACTION_NOT_EXECUTED")
        state["triggers"] = sorted(current_triggers)
        reason = (
            "Hard Yield Gate: Controller 已明确给出当前可执行的下一动作，"
            "必须先执行、hard BLOCK/DEFER，或完成事实收敛与 project-wide recompute；"
            "不得一边声明下一步一边 Yield。"
        )
        _mark_yield_rejected(
            state, event, reason=reason, trigger="KNOWN_NEXT_ACTION_NOT_EXECUTED"
        )
        return _pending_stop_output(state, event, reason=reason), state

    if event_name == "Stop" and snapshot.get("control_loop_required") is True:
        active_turn = str(state.get("active_turn_id", "")).strip()
        receipt_turn = str(state.get("receipt_turn_id", "")).strip()
        if state.get("must_yield") is not True or not active_turn or receipt_turn != active_turn:
            pending_triggers = ", ".join(
                str(item) for item in state.get("triggers", []) if str(item).strip()
            )
            reason = (
                "Controller Stop/Yield blocked: current turn requires one successful "
                "project-wide control-loop receipt proving complete runnable, candidate, "
                "recovery, Controller-action, and correction closure before Stop/Yield."
            )
            if pending_triggers:
                reason += " Pending triggers: " + pending_triggers + "."
            _mark_yield_rejected(
                state, event, reason="control-loop gate rejected: " + reason
            )
            return _pending_stop_output(state, event, reason=reason), state
    if event_name == "PostToolUse" and _event_turn_id(event) and _event_turn_id(event) != str(state.get("active_turn_id", "")):
        # A delayed result cannot unlock a newer turn or contaminate its trace.
        state["adapter_fault"] = {
            "code": "unmatched_tool_result", "turn_id": _event_turn_id(event),
            "active_turn_id": str(state.get("active_turn_id", "")),
        }
        return {"systemMessage": "Adaptive Agent Runtime: unmatched tool result; current turn evidence was not changed."}, state
    if event_name == "PreToolUse":
        pending_display_sync = state.get("goal_display_sync")
        if (
            isinstance(pending_display_sync, dict)
            and pending_display_sync.get("status") not in {None, "completed"}
        ):
            authorized_sync, denial = goal_display_sync.authorize_goal_display_sync_tool(
                pending_display_sync, event
            )
            if denial:
                return _pre_tool_denial(denial), state
            state["goal_display_sync"] = authorized_sync
        if _goal_block_request(event):
            turn_id = _event_turn_id(event) or str(state.get("active_turn_id", ""))
            authorization = state.get("goal_block_authorization")
            authorized = (
                isinstance(authorization, dict)
                and str(authorization.get("turn_id", "")) == turn_id
            )
            tool_use_id = _tool_use_id(event)
            if not authorized or not tool_use_id:
                return _pre_tool_denial(
                    "系统 Goal blocked 必须在同一回合先取得通过 project_blocked 全项目扫描的控制收据。"
                ), state
            state.pop("goal_block_authorization", None)
            state["goal_block_inflight"] = tool_use_id
            inflight = [
                str(item) for item in state.get("inflight_tool_use_ids", []) if str(item)
            ]
            if tool_use_id not in inflight:
                inflight.append(tool_use_id)
            state["inflight_tool_use_ids"] = inflight
            return {}, state
        if state.get("must_yield") is True:
            turn_id = _event_turn_id(event) or str(state.get("active_turn_id", "")).strip()
            receipt_turn_id = str(state.get("receipt_turn_id", "")).strip()
            if not turn_id or receipt_turn_id != turn_id:
                return _pre_tool_denial(
                    "Controller must_yield state is not backed by a current-turn control receipt; "
                    "fail closed and recompute the project-wide control loop before further tools."
                ), state
            state["must_yield"] = False
            state.pop("receipt_turn_id", None)
            state.pop("receipt_tool_use_id", None)
            state.pop("goal_block_authorization", None)
            state["pending_control_event"] = True
            current_triggers = {
                str(item) for item in state.get("triggers", []) if str(item).strip()
            }
            current_triggers.add("post_receipt_action_started")
            state["triggers"] = sorted(current_triggers)
            state["receipt_invalidated_reason"] = "same_turn_mandatory_continuation"
        inflight = [
            str(item) for item in state.get("inflight_tool_use_ids", []) if str(item)
        ]
        tool_use_id = _tool_use_id(event)
        tool_input = event.get("tool_input")
        command = str(tool_input.get("command", "")) if isinstance(tool_input, dict) else ""
        controller_session_id = str(event.get("controller_session_id", "")).strip()
        is_guard = _is_control_guard_command(
            command,
            controller_session_id=controller_session_id,
            cwd=event.get("cwd"),
        )
        if is_guard and inflight:
            return _pre_tool_denial(
                "控制收据必须串行执行；当前仍有已放行工具尚未返回。"
            ), state
        if not is_guard and state.get("control_receipt_inflight"):
            return _pre_tool_denial(
                "控制收据正在执行；在其 PostToolUse 闭合前禁止并发放行其他工具。"
            ), state
        if tool_use_id and tool_use_id not in inflight:
            inflight.append(tool_use_id)
        state["inflight_tool_use_ids"] = inflight
        if tool_use_id:
            records = state.get("inflight_tool_records")
            records = dict(records) if isinstance(records, dict) else {}
            record: dict[str, Any] = {
                "turn_id": _event_turn_id(event),
                "tool_name": str(event.get("tool_name") or ""),
                "input_sha256": _json_sha256(tool_input),
            }
            if command:
                record["command_sha256"] = sha256_bytes(command.encode("utf-8"))
            records[tool_use_id] = record
            state["inflight_tool_records"] = records
        if is_guard:
            state["control_receipt_inflight"] = tool_use_id or "unknown"
            proposal = _control_guard_proposal(command, cwd=event.get("cwd"))
            if proposal is not None and tool_use_id:
                state["control_receipt_proposal"] = {
                    **proposal,
                    "tool_use_id": tool_use_id,
                }
            else:
                state.pop("control_receipt_proposal", None)
        return {}, state
    if event_name == "PostToolUse":
        tool_use_id = _tool_use_id(event)
        guard_result_missing = (
            bool(tool_use_id)
            and str(state.get("control_receipt_inflight", "")) == tool_use_id
            and not isinstance(event.get("tool_response"), dict)
        )
        # Desktop can emit a same-ID PostToolUse callback before attaching a tool
        # result. Keep an armed control receipt fenced until the native transcript
        # can recover the completed command on Stop; otherwise that empty callback
        # would discard the only binding between the command and its proposal.
        if not guard_result_missing:
            state["inflight_tool_use_ids"] = [
                str(item)
                for item in state.get("inflight_tool_use_ids", [])
                if str(item) and str(item) != tool_use_id
            ]
            if str(state.get("control_receipt_inflight", "")) == tool_use_id:
                state.pop("control_receipt_inflight", None)
            if tool_use_id:
                records = state.get("inflight_tool_records")
                if isinstance(records, dict):
                    records = dict(records)
                    records.pop(tool_use_id, None)
                    state["inflight_tool_records"] = records
        pending_display_sync = state.get("goal_display_sync")
        if (
            isinstance(pending_display_sync, dict)
            and pending_display_sync.get("status") not in {None, "completed", "degraded"}
        ):
            observed_sync = goal_display_sync.observe_goal_display_sync_result(
                pending_display_sync, event
            )
            state["goal_display_sync"] = observed_sync
            if (
                observed_sync.get("status") == "completed"
                and observed_sync.get("sync_kind") == "target_goal_rebind"
            ):
                completed = {
                    str(item)
                    for item in state.get("goal_rebind_completed_fingerprints", [])
                    if str(item).strip()
                }
                fingerprint = str(observed_sync.get("fingerprint") or "").strip()
                if fingerprint:
                    completed.add(fingerprint)
                state["goal_rebind_completed_fingerprints"] = sorted(completed)[-32:]
    _record_tool_trace(state, event)
    event_next_action = str(event.get("next_action") or "").strip()
    event_requires_user = event.get("requires_user")
    if event_name == "PostToolUse" and event_next_action and isinstance(event_requires_user, bool):
        state["next_action"] = event_next_action
        state["requires_user"] = event_requires_user
        current_triggers = {str(item) for item in state.get("triggers", [])}
        if event_requires_user is False:
            current_triggers.add("next_action_pending")
        else:
            current_triggers.discard("next_action_pending")
            prior_event_triggers = {str(item) for item in (prior_state or {}).get("triggers", [])}
            pending_receipts = [item for item in state.get("pending_terminal_receipts", []) if str(item).strip()]
            if prior_event_triggers.issubset({"next_action_pending"}) and not pending_receipts:
                state["pending_control_event"] = False
        state["triggers"] = sorted(current_triggers)
    handshake = snapshot.get("rule_handshake", {}) if isinstance(snapshot.get("rule_handshake"), dict) else {}
    prior_nonrule_pending = bool(_non_rule_triggers(set((prior_state or {}).get("triggers", []))))
    wake_policy = derive_rule_wake_policy(
        handshake,
        assignment_liveness=snapshot.get("assignment_liveness", {}),
    )
    if wake_policy:
        state["rule_wake_policy"] = wake_policy
    else:
        state.pop("rule_wake_policy", None)

    if event_name == "SessionStart":
        triggers = lifecycle_triggers(snapshot, None)
        triggers = sorted(set(triggers) | set(snapshot_continuation_debt_labels(snapshot)))
        pending_terminal_receipts = [
            str(item) for item in state.get("pending_terminal_receipts", [])
            if isinstance(item, str) and item.strip()
        ]
        pending_next_action = str(state.get("next_action") or "").strip()
        continuation_pending = bool(pending_next_action) and state.get("requires_user") is False
        if pending_terminal_receipts:
            triggers = sorted(set(triggers) | set(str(item) for item in state.get("triggers", [])) | {"terminal_receipt_pending"})
        if continuation_pending:
            triggers = sorted(set(triggers) | set(str(item) for item in state.get("triggers", [])) | {"next_action_pending"})
        state.update(
            {
                "pending_control_event": bool(triggers) or bool(pending_terminal_receipts) or continuation_pending,
                "triggers": triggers,
                "stop_continuations": 0,
                "pending_terminal_receipts": pending_terminal_receipts,
            }
        )
        if state["pending_control_event"] and not prior_pending:
            state["wake_generation"] = prior_generation + 1
        if not state["pending_control_event"]:
            return {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": controller_self_check_context(),
                }
            }, state
        context = continuation_reason(
            triggers,
            list(snapshot.get("ready_ids", [])),
            list(snapshot.get("candidate_revisions", [])),
            runnable_ids=list(snapshot.get("runnable_ids", snapshot.get("ready_ids", []))),
            rule_handshake=snapshot.get("rule_handshake"), root=snapshot.get("root"),
            **_controller_action_context(event),
            next_action=pending_next_action if continuation_pending else None,
        )
        if pending_terminal_receipts:
            context += " 待处理 terminal receipt：" + "；".join(pending_terminal_receipts) + "。恢复后先读取这些持久结果再继续。"
        return {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": context,
            }
        }, state

    if event_name == "SubagentStop":
        # A child terminal is a new controller scheduling boundary. Recompute the full
        # current actionable snapshot rather than only carrying triggers that were
        # already pending before the child finished.
        triggers = set(state.get("triggers", [])) | set(lifecycle_triggers(snapshot, None))
        agent_id = str(event.get("agent_id", "unknown"))
        trigger = f"subagent_stopped:{agent_id}"
        triggers.add(trigger)
        terminal_receipt = str(event.get("terminal_receipt", "")).strip()
        pending_terminal_receipts = [
            str(item) for item in state.get("pending_terminal_receipts", [])
            if isinstance(item, str) and item.strip()
        ]
        if terminal_receipt and terminal_receipt not in pending_terminal_receipts:
            pending_terminal_receipts.append(terminal_receipt)
        state.update(
            {
                "pending_control_event": True,
                "triggers": sorted(triggers),
                "stop_continuations": 0,
                "pending_terminal_receipts": pending_terminal_receipts,
            }
        )
        if not prior_pending:
            state["wake_generation"] = prior_generation + 1
        return {}, state

    if successful_control_receipt(
        event, snapshot,
        trusted_host_terminal_commit=trusted_host_terminal_commit,
    ):
        receipt_turn_id = _event_turn_id(event) or str(state.get("active_turn_id", ""))
        control_receipt_proposal = state.get("control_receipt_proposal")
        project_block_authorized = _verified_project_block_proposal(
            control_receipt_proposal, tool_use_id=_tool_use_id(event)
        )
        state.pop("control_receipt_proposal", None)
        state["must_yield"] = True
        state["receipt_turn_id"] = receipt_turn_id
        if event.get("controller_host") == "web":
            state["receipt_tool_use_id"] = _tool_use_id(event)
        else:
            state.pop("receipt_tool_use_id", None)
        if project_block_authorized:
            state["goal_block_authorization"] = {
                "turn_id": receipt_turn_id,
                "receipt_tool_use_id": _tool_use_id(event),
            }
        else:
            state.pop("goal_block_authorization", None)
        activated_display_sync: dict[str, Any] | None = None
        if (
            isinstance(control_receipt_proposal, dict)
            and control_receipt_proposal.get("goal_rollover_status") == "rolled"
        ):
            rollover_contract = _verified_goal_rollover_proposal(
                control_receipt_proposal, tool_use_id=_tool_use_id(event)
            )
            try:
                if rollover_contract is None:
                    raise ValueError("validated ledger Goal display is unavailable")
                activated_display_sync = goal_display_sync.start_goal_display_sync(
                    state.get("goal_display_sync")
                    if isinstance(state.get("goal_display_sync"), dict)
                    else None,
                    rollover_contract,
                    controller_id=str(event.get("controller_session_id", "")).strip(),
                    source_session_id=str(event.get("source_session_id", "")).strip(),
                    host=str(event.get("controller_host", "")).strip(),
                    target_generation=event.get("controller_target_generation"),
                    turn_id=receipt_turn_id,
                    host_capabilities=(
                        None if event.get("controller_host") == DESKTOP_SESSION_HOST else set()
                    ),
                )
            except ValueError as exc:
                activated_display_sync = {
                    "schema_version": 1,
                    "receipt_id": "goal-display-sync:degraded",
                    "status": "degraded",
                    "reason": "GOAL_DISPLAY_CONTRACT_UNAVAILABLE",
                    "detail_sha256": _json_sha256(str(exc)),
                    "controller_id": str(event.get("controller_session_id", "")).strip(),
                    "execution_target_session_id": str(event.get("source_session_id", "")).strip(),
                    "host": str(event.get("controller_host", "")).strip(),
                    "target_generation": event.get("controller_target_generation"),
                    "rollover_turn_id": receipt_turn_id,
                    "steps": [],
                }
            if activated_display_sync is not None:
                state["goal_display_sync"] = activated_display_sync
        if (
            isinstance(activated_display_sync, dict)
            and activated_display_sync.get("status") != "completed"
        ):
            trigger = "goal_display_sync:" + str(
                activated_display_sync.get("receipt_id", "pending")
            )
            state.update({
                "pending_control_event": True,
                "triggers": sorted({trigger} | set(_unfinished_debt_labels(snapshot))),
                "stop_continuations": 0,
                "pending_terminal_receipts": [],
                "next_action": "",
                "requires_user": False,
            })
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": (
                        "Ledger Goal rollover 已验证，但宿主显示同步仍是 Continuation Debt。"
                        "按顺序完成旧系统 Goal update_goal(status=complete)、"
                        "用台账当前 Goal 精确文本 create_goal、再为当前总控任务 set_thread_title；"
                        "随后用 get_goal 与 list_threads 回读精确 Goal/标题。"
                        "每步必须由同一 exact Controller target/generation 的 PostToolUse 回执闭合；"
                        "回读失败不得重放已经成功的写操作。"
                    ),
                }
            }, state
        handshake_state = str(handshake.get("state", ""))
        rule_triggers: list[str] = []
        if wake_policy == "after_event" and handshake_state in {"pending_ack", "pending_live_e2e"}:
            rule_prefix = (
                "rule_update_pending:"
                if handshake_state == "pending_ack"
                else "rule_live_e2e_pending:"
            )
            rule_triggers = [
                item for item in lifecycle_triggers(snapshot, None)
                if item.startswith(rule_prefix)
            ]
            state["rule_wake_policy"] = "after_event"
        remaining_debt = _unfinished_debt_labels(snapshot)
        triggers = sorted(set(remaining_debt) | set(rule_triggers))
        pending = bool(triggers)
        state.update({
            "pending_control_event": pending,
            "triggers": triggers,
            "stop_continuations": 0,
            "pending_terminal_receipts": [],
            "next_action": "",
            "requires_user": False,
        })
        if pending and not prior_pending:
            state["wake_generation"] = prior_generation + 1
        return {}, state

    detected = lifecycle_triggers(snapshot, prior_state)
    prior_triggers = {str(item) for item in state.get("triggers", [])}
    transient_prefixes = (
        "rule_update_pending:", "rule_ledger_stale:", "rule_live_e2e_pending:",
        "rule_install_integrity_error:",
        "LEDGER_INVALID:",
        "active_lease_expired:", "assignment_became_unhealthy:", "agent_session_terminal:",
        "active_without_progress:", "recovery_stalled:", "recovery_budget_exhausted:",
    )
    prior_triggers = {item for item in prior_triggers if not item.startswith(transient_prefixes)}
    current_ready = {str(item) for item in snapshot.get("ready_ids", [])}
    current_runnable = {str(item) for item in snapshot.get("runnable_ids", snapshot.get("ready_ids", []))}
    current_candidates = {str(item) for item in snapshot.get("candidate_revisions", [])}
    current_debt = set(snapshot_continuation_debt_labels(snapshot))
    current_unfinished = {
        item.removeprefix("OPEN_CHILD:")
        for item in current_debt
        if item.startswith("OPEN_CHILD:")
    }
    current_active = {
        item.removeprefix("ACTIVE:")
        for item in current_debt
        if item.startswith("ACTIVE:")
    }
    current_verify = {
        item.removeprefix("VERIFY:")
        for item in current_debt
        if item.startswith("VERIFY:")
    }
    current_recovering = {
        item.removeprefix("RECOVERING:")
        for item in current_debt
        if item.startswith("RECOVERING:")
    }
    current_corrections = {
        str(item.get("fingerprint", "")).strip()
        for item in snapshot.get("controller_corrections", [])
        if isinstance(item, dict) and str(item.get("fingerprint", "")).strip()
    }
    prior_triggers = {
        item for item in prior_triggers
        if not (item.startswith("READY:") and item.removeprefix("READY:") not in current_ready)
        and not (item.startswith("RUNNABLE:") and item.removeprefix("RUNNABLE:") not in current_runnable)
        and not (item.startswith("CANDIDATE:") and item.removeprefix("CANDIDATE:") not in current_candidates)
        and not (item.startswith("CORRECTION:") and item.removeprefix("CORRECTION:") not in current_corrections)
        and not (item.startswith("OPEN_CHILD:") and item.removeprefix("OPEN_CHILD:") not in current_unfinished)
        and not (item.startswith("ACTIVE:") and item.removeprefix("ACTIVE:") not in current_active)
        and not (item.startswith("VERIFY:") and item.removeprefix("VERIFY:") not in current_verify)
        and not (item.startswith("RECOVERING:") and item.removeprefix("RECOVERING:") not in current_recovering)
    }
    triggers = sorted(prior_triggers | set(detected) | current_debt)
    pending_next_action = str(state.get("next_action") or "").strip()
    continuation_pending = bool(pending_next_action) and state.get("requires_user") is False
    if continuation_pending and "next_action_pending" not in triggers:
        triggers = sorted(set(triggers) | {"next_action_pending"})
    pending = bool(state.get("pending_control_event")) or bool(triggers) or continuation_pending or bool(current_debt)
    if wake_policy == "next_turn" and event_name != "SessionStart" and not _non_rule_triggers(triggers) and not continuation_pending:
        pending = False
    if wake_policy == "after_event" and prior_nonrule_pending:
        pending = True
    state.update({"pending_control_event": pending, "triggers": triggers})
    if pending and not prior_pending:
        state["wake_generation"] = prior_generation + 1

    if event_name == "PostToolUse":
        progress_labels = {
            "main_head_changed",
            "ledger_changed",
            "main_worktree_changed",
            "ready_set_changed",
            "candidate_queue_changed",
        }
        if progress_labels.intersection(detected):
            state["stop_continuations"] = 0
        if not pending:
            return {}, state
        context = continuation_reason(
            triggers,
            list(snapshot.get("ready_ids", [])),
            list(snapshot.get("candidate_revisions", [])),
            runnable_ids=list(snapshot.get("runnable_ids", snapshot.get("ready_ids", []))),
            rule_handshake=snapshot.get("rule_handshake"), root=snapshot.get("root"),
            **_controller_action_context(event),
            next_action=pending_next_action if continuation_pending else None,
        )
        return {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": context,
            }
        }, state

    if event_name == "Stop" and pending:
        reason = continuation_reason(
            triggers,
            list(snapshot.get("ready_ids", [])),
            list(snapshot.get("candidate_revisions", [])),
            runnable_ids=list(snapshot.get("runnable_ids", snapshot.get("ready_ids", []))),
            rule_handshake=snapshot.get("rule_handshake"), root=snapshot.get("root"),
            **_controller_action_context(event),
            next_action=pending_next_action if continuation_pending else None,
        )
        return _pending_stop_output(state, event, reason=reason), state
    return {}, state


def load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def persist_confirmed_desktop_native_wake(
    repo: Path, event: dict[str, Any], state: dict[str, Any]
) -> None:
    """Normalize one fenced Desktop UserPromptSubmit into the shared wake receipt."""
    if event.get("hook_event_name") != "UserPromptSubmit":
        return
    controller_id = str(event.get("controller_session_id") or "").strip()
    source_session_id = str(event.get("source_session_id") or "").strip()
    target_generation = event.get("controller_target_generation")
    ownership_generation = event.get("controller_ownership_generation")
    common_dir = git_common_dir(repo)
    if (
        not controller_id
        or not source_session_id
        or common_dir is None
        or not isinstance(target_generation, int)
        or isinstance(target_generation, bool)
        or target_generation < 0
        or not isinstance(ownership_generation, int)
        or isinstance(ownership_generation, bool)
        or ownership_generation <= 0
    ):
        return
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    write_json(
        common_dir / "adaptive-delivery" / "controller-wake-receipt.json",
        {
            "schema_version": 1,
            "canonical_common_dir": str(common_dir),
            "controller_id": controller_id,
            "event_fingerprint": pending_event_fingerprint(state),
            "preferred_host": DESKTOP_SESSION_HOST,
            "selected_host": DESKTOP_SESSION_HOST,
            "decision": "RESUME_CURRENT_HOST",
            "reason": (
                "native UserPromptSubmit matched the current Desktop target and "
                "execution ownership fence"
            ),
            "started_at_unix_ms": now_ms,
            "completed_at_unix_ms": now_ms,
            "operation": "desktop_native_user_prompt",
            "result": "CONFIRMED",
            "pending_control_event": state.get("pending_control_event") is True,
            "execution_target_session_id": source_session_id,
            "target_generation": target_generation,
            "ownership_generation": ownership_generation,
            "target_mode": "explicit_current",
            "host_attested": True,
        },
    )


def registered_controller_id(session_id: str) -> str | None:
    session_id = session_id.strip()
    if not session_id:
        return None
    registry = load_json(REGISTRY_PATH)
    return target_guard.active_source_controller_id(
        registry,
        source_session_id=session_id,
        host=DESKTOP_SESSION_HOST,
    )


def registered_root(session_id: str) -> Path | None:
    registry = load_json(REGISTRY_PATH)
    controller_id = registered_controller_id(session_id)
    value = registry.get(controller_id) if controller_id is not None else None
    if not isinstance(value, str) or not value.strip():
        return None
    return Path(value).expanduser().resolve()


def registry_controller_root_matches(
    registry: dict[str, Any], *, controller_id: str, expected_root: Path
) -> bool:
    value = registry.get(controller_id)
    if not isinstance(value, str) or not value.strip():
        return False
    registered_root = Path(value).expanduser().resolve()
    expected_root = expected_root.expanduser().resolve()
    expected_common_dir = git_common_dir(expected_root)
    return (
        registered_root == expected_root
        and expected_common_dir is not None
        and git_common_dir(registered_root) == expected_common_dir
    )


def registered_controller_surface(session_id: str, expected_root: Path) -> Path | None:
    registry = load_json(REGISTRY_PATH)
    controller_id = registered_controller_id(session_id)
    if controller_id is None:
        return None
    surfaces = registry.get(CONTROLLER_SURFACES_KEY)
    if surfaces is None:
        return expected_root.resolve()
    if not isinstance(surfaces, dict):
        return None
    if controller_id not in surfaces:
        return expected_root.resolve()
    value = surfaces.get(controller_id)
    if not isinstance(value, str) or not value.strip():
        return None
    return Path(value).expanduser().resolve()


def register_controller(session_id: str, root: Path) -> None:
    canonical_root = canonical_main_root(root)
    if canonical_root is None:
        raise ValueError("controller registration requires a canonical main worktree")
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_path = REGISTRY_PATH.with_suffix(REGISTRY_PATH.suffix + ".lock")
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            registry = load_json(REGISTRY_PATH)
            existing_root = registry.get(session_id)
            if existing_root is not None:
                if not isinstance(existing_root, str) or not existing_root.strip():
                    raise ValueError("controller session has an invalid registered repository")
                if git_common_dir(Path(existing_root)) != git_common_dir(canonical_root):
                    raise ValueError(
                        "controller session is already registered to a different repository"
                    )
            sessions = registry.get(CONTROLLER_SESSIONS_KEY)
            if isinstance(sessions, dict):
                for controller_id, controller_sessions in sessions.items():
                    if controller_id == session_id or not isinstance(controller_sessions, dict):
                        continue
                    desktop_sessions = controller_sessions.get(DESKTOP_SESSION_HOST)
                    if isinstance(desktop_sessions, str):
                        desktop_sessions = [desktop_sessions]
                    if isinstance(desktop_sessions, list) and session_id in desktop_sessions:
                        raise ValueError(
                            "controller session is already bound as a desktop entry to another Controller"
                        )
            for registered_session, registered_path in registry.items():
                if registered_session == session_id or not isinstance(registered_path, str):
                    continue
                if Path(registered_path).expanduser().resolve() == canonical_root.resolve():
                    raise ValueError("canonical project already has a different controller session")
            registry[session_id] = str(canonical_root)
            surfaces = registry.get(CONTROLLER_SURFACES_KEY)
            if surfaces is None:
                surfaces = {}
            elif not isinstance(surfaces, dict):
                raise ValueError("controller surface registry is invalid")
            surfaces[session_id] = str(root.resolve())
            registry[CONTROLLER_SURFACES_KEY] = surfaces
            write_json(REGISTRY_PATH, registry)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def bind_desktop_session(
    *, controller_id: str, desktop_session_id: str, repo: Path
) -> None:
    controller_id = controller_id.strip()
    desktop_session_id = desktop_session_id.strip()
    if not controller_id or not desktop_session_id:
        raise ValueError("controller-id and desktop-session-id are required")
    canonical_root = canonical_main_root(repo)
    if canonical_root is None:
        raise ValueError("desktop session binding requires a canonical Git project")
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_path = REGISTRY_PATH.with_suffix(REGISTRY_PATH.suffix + ".lock")
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            registry = load_json(REGISTRY_PATH)
            registered_path = registry.get(controller_id)
            if (
                not isinstance(registered_path, str)
                or Path(registered_path).expanduser().resolve() != canonical_root.resolve()
            ):
                raise PermissionError(
                    "desktop session binding requires the registered Controller for this repository"
                )
            _reject_cross_controller_desktop_owner(
                registry=registry,
                controller_id=controller_id,
                desktop_session_id=desktop_session_id,
            )
            sessions = registry.get(CONTROLLER_SESSIONS_KEY)
            if not isinstance(sessions, dict):
                sessions = {}
            controller_sessions = sessions.get(controller_id)
            if not isinstance(controller_sessions, dict):
                controller_sessions = {}
            desktop_sessions = controller_sessions.get(DESKTOP_SESSION_HOST)
            if isinstance(desktop_sessions, str):
                desktop_sessions = [desktop_sessions]
            if not isinstance(desktop_sessions, list):
                desktop_sessions = []
            normalized = [
                value
                for value in desktop_sessions
                if isinstance(value, str) and value.strip()
            ]
            if desktop_session_id not in normalized:
                normalized.append(desktop_session_id)
            controller_sessions[DESKTOP_SESSION_HOST] = normalized
            sessions[controller_id] = controller_sessions
            registry[CONTROLLER_SESSIONS_KEY] = sessions
            write_json(REGISTRY_PATH, registry)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _desktop_target_generation(record: object) -> int:
    if record is None:
        return 0
    if not isinstance(record, dict):
        raise ValueError("desktop Controller target record is invalid")
    generation = record.get("generation")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise ValueError("desktop Controller target generation is invalid")
    return generation


def _require_expected_desktop_generation(
    record: object, *, expected_generation: int
) -> int:
    if (
        isinstance(expected_generation, bool)
        or not isinstance(expected_generation, int)
        or expected_generation < 0
    ):
        raise ValueError("expected_generation must be a non-negative integer")
    generation = _desktop_target_generation(record)
    if expected_generation != generation:
        raise PermissionError(
            f"expected_generation {expected_generation} does not match current generation {generation}"
        )
    return generation


def _validated_controller_registry(
    *, registry: dict[str, Any], controller_id: str, canonical_root: Path
) -> None:
    registered_path = registry.get(controller_id)
    if (
        not isinstance(registered_path, str)
        or Path(registered_path).expanduser().resolve() != canonical_root.resolve()
    ):
        raise PermissionError(
            "desktop session lifecycle change requires the registered Controller for this repository"
        )


def _reject_cross_controller_desktop_owner(
    *, registry: dict[str, Any], controller_id: str, desktop_session_id: str
) -> None:
    direct_owner = registry.get(desktop_session_id)
    if isinstance(direct_owner, str) and desktop_session_id != controller_id:
        raise PermissionError(
            "Desktop Controller Session is already a registered Controller"
        )
    sessions = registry.get(CONTROLLER_SESSIONS_KEY)
    if not isinstance(sessions, dict):
        sessions = {}
    for candidate_controller, controller_sessions in sessions.items():
        if candidate_controller == controller_id or not isinstance(controller_sessions, dict):
            continue
        desktop_sessions = controller_sessions.get(DESKTOP_SESSION_HOST)
        if isinstance(desktop_sessions, str):
            desktop_sessions = [desktop_sessions]
        if isinstance(desktop_sessions, list) and desktop_session_id in desktop_sessions:
            raise PermissionError(
                "Desktop Controller Session is already bound to another Controller"
            )
    targets = registry.get(CONTROLLER_TARGETS_KEY)
    if not isinstance(targets, dict):
        return
    for candidate_controller, controller_targets in targets.items():
        if candidate_controller == controller_id or not isinstance(controller_targets, dict):
            continue
        candidate_target = controller_targets.get(DESKTOP_SESSION_HOST)
        if (
            isinstance(candidate_target, dict)
            and candidate_target.get("status") == "active"
            and str(candidate_target.get("session_id") or "").strip() == desktop_session_id
        ):
            raise PermissionError(
                "Desktop Controller Session is already active for another Controller"
            )


def _desktop_goal_rebind_contract_is_current(
    contract: dict[str, Any], repo: Path
) -> bool:
    """True when a desktop target rebind still names the ledger's current Goal."""
    objective = str(contract.get("objective") or "").strip()
    if not objective:
        return False
    current = _current_goal_rebind_contract(repo)
    if current is None:
        return False
    return objective == str(current.get("objective") or "").strip()


def _current_goal_rebind_contract(repo: Path) -> dict[str, Any] | None:
    ledger = repo / "TASK_LEDGER.md"
    try:
        raw = ledger.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    match = re.search(r"^- 当前 Goal：\s*(.+?)\s*$", text, re.MULTILINE)
    if match is None:
        return None
    objective = match.group(1).strip().removesuffix("。").strip()
    if objective.startswith("`") and objective.endswith("`") and len(objective) > 1:
        objective = objective[1:-1].strip()
    if not objective or objective.lower() in {"none", "null", "无", "无当前 goal"}:
        return None
    return {
        "schema_version": 1,
        "ledger_path": str(ledger.resolve()),
        "ledger_sha256": sha256_bytes(raw),
        "objective": objective,
        "project_name": repo.name,
    }


def replace_desktop_session(
    *, controller_id: str, desktop_session_id: str, repo: Path, expected_generation: int,
    expected_ownership_generation: int | None = None,
    registry_path: Path | None = None,
) -> dict[str, Any]:
    controller_id = controller_id.strip()
    desktop_session_id = desktop_session_id.strip()
    if not controller_id or not desktop_session_id:
        raise ValueError("controller-id and desktop-session-id are required")
    canonical_root = canonical_main_root(repo)
    if canonical_root is None:
        raise ValueError("desktop session replacement requires a canonical Git project")
    registry_path = REGISTRY_PATH if registry_path is None else registry_path.expanduser()
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = registry_path.with_suffix(registry_path.suffix + ".lock")
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            registry = load_json(registry_path)
            _validated_controller_registry(
                registry=registry,
                controller_id=controller_id,
                canonical_root=canonical_root,
            )
            target_guard.require_no_active_outbound_lease(
                registry, controller_id=controller_id, host=DESKTOP_SESSION_HOST
            )
            _reject_cross_controller_desktop_owner(
                registry=registry,
                controller_id=controller_id,
                desktop_session_id=desktop_session_id,
            )

            sessions = registry.get(CONTROLLER_SESSIONS_KEY)
            if not isinstance(sessions, dict):
                sessions = {}
            controller_sessions = sessions.get(controller_id)
            if not isinstance(controller_sessions, dict):
                controller_sessions = {}
            desktop_sessions = controller_sessions.get(DESKTOP_SESSION_HOST)
            if isinstance(desktop_sessions, str):
                desktop_sessions = [desktop_sessions]
            if not isinstance(desktop_sessions, list):
                desktop_sessions = []
            retained_desktop_sessions = list(dict.fromkeys(
                value.strip()
                for value in desktop_sessions
                if isinstance(value, str) and value.strip()
            ))
            if (
                desktop_session_id != controller_id
                and desktop_session_id not in retained_desktop_sessions
            ):
                retained_desktop_sessions.append(desktop_session_id)
            controller_sessions[DESKTOP_SESSION_HOST] = retained_desktop_sessions
            sessions[controller_id] = controller_sessions
            registry[CONTROLLER_SESSIONS_KEY] = sessions

            targets = registry.get(CONTROLLER_TARGETS_KEY)
            if targets is None:
                targets = {}
            elif not isinstance(targets, dict):
                raise ValueError("controller target registry is invalid")
            controller_targets = targets.get(controller_id)
            if controller_targets is None:
                controller_targets = {}
            elif not isinstance(controller_targets, dict):
                raise ValueError("Controller target map is invalid")
            prior = controller_targets.get(DESKTOP_SESSION_HOST)
            generation = _require_expected_desktop_generation(
                prior, expected_generation=expected_generation
            ) + 1
            target = {
                "status": "active",
                "session_id": desktop_session_id,
                "generation": generation,
            }
            goal_rebind = _current_goal_rebind_contract(canonical_root)
            if goal_rebind is not None:
                goal_rebind.update({
                    "controller_id": controller_id,
                    "execution_target_session_id": desktop_session_id,
                    "host": DESKTOP_SESSION_HOST,
                    "target_generation": generation,
                })
                target["goal_rebind"] = goal_rebind
            controller_targets[DESKTOP_SESSION_HOST] = target
            targets[controller_id] = controller_targets
            registry[CONTROLLER_TARGETS_KEY] = targets
            ownership_claim = None
            claim_generation = expected_ownership_generation
            if claim_generation is None:
                prior_ownership = target_guard.execution_ownership_record(
                    registry, controller_id=controller_id
                )
                already_on_duty = (
                    isinstance(prior_ownership, dict)
                    and str(prior_ownership.get("active_host") or "") == DESKTOP_SESSION_HOST
                    and str(prior_ownership.get("execution_target_session_id") or "").strip()
                    == desktop_session_id
                )
                if not already_on_duty:
                    if prior_ownership is None:
                        claim_generation = 0
                    else:
                        _host, _target, claim_generation = (
                            target_guard.validate_execution_ownership_record(prior_ownership)
                        )
            if claim_generation is not None:
                ownership_claim = target_guard._claim_controller_host_in_registry(
                    registry,
                    controller_id=controller_id,
                    requested_host=DESKTOP_SESSION_HOST,
                    requested_target_session_id=desktop_session_id,
                    expected_generation=claim_generation,
                    provenance="desktop_entry",
                )
            write_json(registry_path, registry)
            return {
                "controller_id": controller_id,
                "controller_session_id": controller_id,
                "execution_target_session_id": desktop_session_id,
                "host": DESKTOP_SESSION_HOST,
                "repo": str(canonical_root.resolve()),
                **target,
                **({"goal_rebind": goal_rebind} if goal_rebind is not None else {}),
                **({"ownership_generation": ownership_claim["generation"]} if ownership_claim else {}),
            }
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def unbind_desktop_session(
    *, controller_id: str, desktop_session_id: str, repo: Path, expected_generation: int
) -> dict[str, Any]:
    controller_id = controller_id.strip()
    desktop_session_id = desktop_session_id.strip()
    if not controller_id or not desktop_session_id:
        raise ValueError("controller-id and desktop-session-id are required")
    canonical_root = canonical_main_root(repo)
    if canonical_root is None:
        raise ValueError("desktop session unbind requires a canonical Git project")
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_path = REGISTRY_PATH.with_suffix(REGISTRY_PATH.suffix + ".lock")
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            registry = load_json(REGISTRY_PATH)
            _validated_controller_registry(
                registry=registry,
                controller_id=controller_id,
                canonical_root=canonical_root,
            )
            target_guard.require_no_active_outbound_lease(
                registry, controller_id=controller_id, host=DESKTOP_SESSION_HOST
            )
            _reject_cross_controller_desktop_owner(
                registry=registry,
                controller_id=controller_id,
                desktop_session_id=desktop_session_id,
            )

            sessions = registry.get(CONTROLLER_SESSIONS_KEY)
            if not isinstance(sessions, dict):
                sessions = {}
            controller_sessions = sessions.get(controller_id)
            if not isinstance(controller_sessions, dict):
                controller_sessions = {}
            desktop_sessions = controller_sessions.get(DESKTOP_SESSION_HOST)
            if isinstance(desktop_sessions, str):
                desktop_sessions = [desktop_sessions]
            if not isinstance(desktop_sessions, list):
                desktop_sessions = []
            controller_sessions[DESKTOP_SESSION_HOST] = [
                value
                for value in desktop_sessions
                if isinstance(value, str) and value.strip() and value != desktop_session_id
            ]
            sessions[controller_id] = controller_sessions
            registry[CONTROLLER_SESSIONS_KEY] = sessions

            targets = registry.get(CONTROLLER_TARGETS_KEY)
            if targets is None:
                targets = {}
            elif not isinstance(targets, dict):
                raise ValueError("controller target registry is invalid")
            controller_targets = targets.get(controller_id)
            if controller_targets is None:
                controller_targets = {}
            elif not isinstance(controller_targets, dict):
                raise ValueError("Controller target map is invalid")
            prior = controller_targets.get(DESKTOP_SESSION_HOST)
            prior_generation = _require_expected_desktop_generation(
                prior, expected_generation=expected_generation
            )
            current = (
                str(prior.get("session_id") or "").strip()
                if isinstance(prior, dict) and prior.get("status") == "active"
                else ""
            )
            if prior is None or current == desktop_session_id:
                target = {
                    "status": "unbound",
                    "session_id": None,
                    "generation": prior_generation + 1,
                }
                controller_targets[DESKTOP_SESSION_HOST] = target
            else:
                target = dict(prior)
            targets[controller_id] = controller_targets
            registry[CONTROLLER_TARGETS_KEY] = targets
            write_json(REGISTRY_PATH, registry)
            return {
                "controller_id": controller_id,
                "controller_session_id": controller_id,
                "execution_target_session_id": target.get("session_id"),
                "host": DESKTOP_SESSION_HOST,
                "repo": str(canonical_root.resolve()),
                **target,
            }
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _desktop_codex_entry_for_repo(session_id: str, expected_root: Path) -> bool:
    """True when session_id is the current desktop_codex target for this repo."""
    session_id = session_id.strip()
    if not session_id:
        return False
    controller_id = registered_controller_id(session_id)
    if controller_id is None or registered_root(session_id) != expected_root.resolve():
        return False
    registry = load_json(REGISTRY_PATH)
    try:
        aliases = target_guard.host_sessions(
            registry, controller_id=controller_id, host=DESKTOP_SESSION_HOST
        )
        if session_id in aliases:
            return True
        record = target_guard.target_record(
            registry, controller_id=controller_id, host=DESKTOP_SESSION_HOST
        )
        if record is None:
            return False
        status, target_session_id, _generation = target_guard.validate_target_record(
            record, host=DESKTOP_SESSION_HOST
        )
    except (PermissionError, ValueError):
        return False
    return status == "active" and target_session_id == session_id


def _snapshot_matches_expected_root(
    snapshot: dict[str, Any] | None, expected_root: Path
) -> bool:
    expected = expected_root.resolve()
    if snapshot is None or Path(snapshot["root"]).resolve() != expected:
        return False
    return snapshot.get("git_common_dir") == str(git_common_dir(expected))


def controller_event_is_managed(
    event: dict[str, Any],
    cwd: Path,
    expected_root: Path,
    *,
    snapshot: dict[str, Any] | None = None,
) -> bool:
    session_id = str(event.get("session_id", "")).strip()
    expected = expected_root.resolve()
    if not session_id or registered_root(session_id) != expected:
        return False
    try:
        invocation_root = Path(run_git(cwd, "rev-parse", "--show-toplevel")).resolve()
    except (OSError, subprocess.CalledProcessError, ValueError):
        invocation_root = None
    desktop_entry = _desktop_codex_entry_for_repo(session_id, expected)
    if invocation_root is None:
        # Codex may report a space-alias cwd that does not exist on disk.
        if cwd.exists() or not desktop_entry:
            return False
        if snapshot is None:
            snapshot = project_snapshot(expected)
        return _snapshot_matches_expected_root(snapshot, expected)
    if not (
        (desktop_entry and invocation_root == expected)
        or registered_controller_surface(session_id, expected_root) == invocation_root
    ):
        return False
    if snapshot is None:
        snapshot = project_snapshot(cwd)
    return _snapshot_matches_expected_root(snapshot, expected)


def state_path(session_id: str) -> Path:
    safe_id = "".join(character for character in session_id if character.isalnum() or character in "-_")
    return STATE_ROOT / f"{safe_id or 'unknown'}.json"


def persist_event_state(
    path: Path, event: dict[str, Any], snapshot: dict[str, Any], *,
    preserve_controller_host: bool = False,
    prior_state_validator: Callable[[dict[str, Any]], None] | None = None,
    recovered_event_observer: Callable[
        [dict[str, Any], dict[str, Any], dict[str, Any]], None
    ] | None = None,
    deadline_monotonic: float | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    target_guard._require_before_deadline(
        deadline_monotonic, operation="lifecycle state directory creation"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    target_guard._require_before_deadline(
        deadline_monotonic, operation="lifecycle lock file open"
    )
    with lock_path.open("a+") as lock:
        target_guard._acquire_receipt_lock(
            lock.fileno(), deadline_monotonic=deadline_monotonic,
            label="lifecycle event lock",
        )
        try:
            target_guard._require_before_deadline(
                deadline_monotonic, operation="lifecycle state load"
            )
            previous = load_json(path)
            if prior_state_validator is not None:
                prior_state_validator(previous)
            persisted_event = dict(event)
            if preserve_controller_host:
                prior_host = str(previous.get("controller_host") or "desktop_codex").strip()
                persisted_event["controller_host"] = prior_host if prior_host in {"web", "desktop_codex"} else "desktop_codex"
            evaluate_kwargs: dict[str, Any] = {}
            if recovered_event_observer is not None:
                evaluate_kwargs["recovered_event_observer"] = recovered_event_observer
            output, next_state = evaluate_event(
                persisted_event,
                snapshot=snapshot,
                prior_state=previous,
                **evaluate_kwargs,
            )
            if previous.get("active_turn_id") and previous.get("active_turn_id") != next_state.get("active_turn_id"):
                # Preserve unresolved old evidence before rotating the current-turn
                # projection. An archive failure must not silently discard it.
                archived = {key: previous[key] for key in (
                    "active_turn_id", "source_session_id", "tool_trace", "tool_trace_archive", "tool_trace_overflow",
                    "inflight_tool_use_ids", "inflight_tool_records", "control_receipt_inflight", "must_yield",
                    "receipt_turn_id", "adapter_fault", "web_turn_lease",
                ) if key in previous}
                archived["replaced_by"] = next_state.get("turn_start_evidence")
                try:
                    target_guard._require_before_deadline(
                        deadline_monotonic, operation="lifecycle turn archive"
                    )
                    with path.with_suffix(".turns.jsonl").open("a", encoding="utf-8") as archive:
                        archive.write(json.dumps(archived, ensure_ascii=False) + "\n")
                        archive.flush()
                        os.fsync(archive.fileno())
                except OSError:
                    output = _adapter_fault_output(previous, event, "turn_archive_unavailable")
                    return output, previous
            target_guard._write_registry(
                path, next_state, deadline_monotonic=deadline_monotonic
            )
            return output, next_state
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def arm_host_turn_handoff(
    *,
    path: Path,
    lifecycle_state: dict[str, Any],
    controller_id: str,
    repo: Path,
    registry: Path,
    supervisor_ensurer: Any = None,
) -> dict[str, Any]:
    """Delegate durable debt before the current Host turn is allowed to terminate."""
    requested = lifecycle_state.get("host_turn_handoff")
    if not isinstance(requested, dict) or requested.get("state") != "requested":
        return {}
    if supervisor_ensurer is None:
        try:
            from scripts import web_lifecycle_bridge as bridge
        except ModuleNotFoundError:
            import web_lifecycle_bridge as bridge
        supervisor_ensurer = bridge.ensure_continuation_supervisor
    try:
        supervisor_started = bool(supervisor_ensurer(
            lifecycle_state=lifecycle_state,
            session_id=controller_id,
            repo=repo,
            registry=registry,
            codex=None,
            delay_seconds=1.0,
        ))
        delivery_state = (
            "supervisor_started"
            if supervisor_started
            else "supervisor_already_managing_or_deferred"
        )
        handoff_state = "delegated"
        failure_class = None
    except (OSError, ValueError, PermissionError, RuntimeError, subprocess.SubprocessError) as exc:
        delivery_state = "supervisor_arm_failed"
        handoff_state = "degraded"
        failure_class = type(exc).__name__

    expected_turn_id = str(requested.get("turn_id") or "")
    expected_fingerprint = str(requested.get("debt_fingerprint") or "")
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            current = load_json(path)
            current_handoff = current.get("host_turn_handoff")
            if not isinstance(current_handoff, dict) or (
                str(current_handoff.get("turn_id") or "") != expected_turn_id
                or str(current_handoff.get("debt_fingerprint") or "")
                != expected_fingerprint
            ):
                return {}
            recorded = {
                **current_handoff,
                "state": handoff_state,
                "delivery_state": delivery_state,
                "recorded_at": datetime.now(timezone.utc).isoformat(),
            }
            if failure_class is not None:
                recorded["failure_class"] = failure_class
            else:
                recorded.pop("failure_class", None)
            current["host_turn_handoff"] = recorded
            write_json(path, current)
            return recorded
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)



def process_verified_web_event(
    event: dict[str, Any],
    *,
    registry_path: Path = REGISTRY_PATH,
    lifecycle_path: Path | None = None,
    deadline_monotonic: float | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Persist one Host-attested Web event through the canonical lifecycle state machine.

    This is the Web security boundary. It re-derives current Controller/target/ownership
    facts from the authoritative registry before allowing the pure lifecycle transition.
    """
    if not isinstance(event, dict):
        raise PermissionError("verified Web lifecycle event must be an object")
    target_guard._require_before_deadline(
        deadline_monotonic, operation="verified Web lifecycle validation"
    )
    forbidden_internal_fields = {
        "_runtime_host_terminal_commit", "host_tool_terminal_commit",
        "verified_host_tool_receipt", "internal_trust", "trusted_internal_event",
    }
    supplied_internal = forbidden_internal_fields.intersection(event)
    if supplied_internal:
        raise PermissionError(
            "generic Web lifecycle event cannot supply internal Host trust fields: "
            + ", ".join(sorted(supplied_internal))
        )
    if str(event.get("controller_host") or "").strip() != "web":
        raise PermissionError("verified Web lifecycle event requires controller_host=web")
    if str(event.get("execution_host") or "").strip() != "web":
        raise PermissionError("verified Web lifecycle event requires execution_host=web")
    if str(event.get("event_source") or "").strip() != "web":
        raise PermissionError("verified Web lifecycle event requires event_source=web")
    controller_id = str(
        event.get("controller_session_id") or event.get("controller_id") or event.get("session_id") or ""
    ).strip()
    source_session_id = str(event.get("web_session_id") or event.get("source_session_id") or "").strip()
    if not controller_id or not source_session_id:
        raise PermissionError("verified Web lifecycle event requires exact Controller and Web source session")
    cwd = Path(str(event.get("cwd") or ".")).expanduser().resolve()
    snapshot = project_snapshot(cwd, deadline_monotonic=deadline_monotonic)
    if snapshot is None:
        raise PermissionError("verified Web lifecycle event is outside a governed project")
    expected_root = Path(str(snapshot["root"])).expanduser().resolve()
    registry_path = Path(registry_path).expanduser()
    normalized_event = dict(event)
    normalized_event["controller_id"] = controller_id
    normalized_event["controller_session_id"] = controller_id
    normalized_event["session_id"] = controller_id
    normalized_event["source_session_id"] = source_session_id
    normalized_event["web_session_id"] = source_session_id
    normalized_event["controller_host"] = "web"
    normalized_event["execution_host"] = "web"
    normalized_event["event_source"] = "web"
    normalized_event["cwd"] = str(expected_root)
    normalized_event["controller_registry_path"] = str(registry_path.expanduser().resolve())

    target_guard._require_before_deadline(
        deadline_monotonic, operation="verified Web registry lock"
    )
    with target_guard.locked_registry(
        registry_path, deadline_monotonic=deadline_monotonic
    ) as registry:
        unique_controller_id = target_guard.unique_controller_id_for_repo_in_registry(
            expected_root, registry
        )
        if unique_controller_id != controller_id:
            raise PermissionError("verified Web lifecycle event does not belong to the unique project Controller")
        if not registry_controller_root_matches(
            registry, controller_id=controller_id, expected_root=expected_root
        ):
            raise PermissionError("verified Web lifecycle event repository does not match Controller root")
        if target_guard.active_source_controller_id(
            registry, source_session_id=source_session_id, host="web"
        ) != controller_id:
            raise PermissionError("verified Web lifecycle event source is not the current Web execution target")
        target_record = target_guard.target_record(
            registry, controller_id=controller_id, host="web"
        )
        if target_record is None:
            raise PermissionError("verified Web lifecycle event requires an explicit current Web target")
        target_status, target_session_id, target_generation = target_guard.validate_target_record(
            target_record, host="web"
        )
        if target_status != "active" or target_session_id != source_session_id:
            raise PermissionError("verified Web lifecycle event target is stale or mismatched")
        goal_rebind_contract = target_record.get("goal_rebind")
        if isinstance(goal_rebind_contract, dict):
            normalized_event["goal_rebind_contract"] = goal_rebind_contract
        ownership = target_guard.execution_ownership_record(registry, controller_id=controller_id)
        if ownership is None:
            raise PermissionError("verified Web lifecycle event requires current execution ownership")
        ownership_host, ownership_session_id, ownership_generation = (
            target_guard.validate_execution_ownership_record(ownership)
        )
        if ownership_host != "web" or ownership_session_id != source_session_id:
            raise PermissionError("verified Web lifecycle event ownership is stale or mismatched")
        normalized_event["controller_target_generation"] = target_generation
        normalized_event["controller_ownership_generation"] = ownership_generation
        # Validate the signed/derived turn against authoritative current fences before state mutation.
        _verified_web_turn_evidence(normalized_event)
        path = lifecycle_path or state_path(controller_id)
        def validate_web_stop(current_state: dict[str, Any]) -> None:
            if normalized_event.get("hook_event_name") == "Stop":
                if current_state.get("must_yield") is not True:
                    return
                commits = current_state.get("host_tool_terminal_commits")
                receipts = registry.get(target_guard.CONTROLLER_HOST_TOOL_RECEIPTS_KEY)
                controller_receipts = (
                    receipts.get(controller_id) if isinstance(receipts, dict) else None
                )
                web_receipts = (
                    controller_receipts.get("web")
                    if isinstance(controller_receipts, dict) else None
                )
                matching_closed = False
                if isinstance(commits, dict) and isinstance(web_receipts, dict):
                    execution_id = str(current_state.get("receipt_tool_use_id") or "").strip()
                    commit = commits.get(execution_id)
                    if execution_id:
                        tuple_value = commit.get("tuple") if isinstance(commit, dict) else None
                        record = web_receipts.get(execution_id)
                        if (
                            isinstance(tuple_value, dict)
                            and tuple_value.get("controller_id") == controller_id
                            and tuple_value.get("execution_target_session_id") == source_session_id
                            and tuple_value.get("turn_id") == current_state.get("receipt_turn_id")
                            and tuple_value.get("target_generation") == target_generation
                            and tuple_value.get("ownership_generation") == ownership_generation
                            and isinstance(record, dict)
                            and record.get("state") == "CLOSED"
                            and record.get("tuple") == tuple_value
                            and record.get("guard_evidence_sha256")
                            == commit.get("guard_contract_sha256")
                        ):
                            matching_closed = True
                if not matching_closed:
                    raise PermissionError(
                        "Web Stop requires the matching Host tool receipt CAS to be CLOSED"
                    )
        return persist_event_state(
            path, normalized_event, snapshot,
            prior_state_validator=validate_web_stop,
            deadline_monotonic=deadline_monotonic,
        )


def process_verified_host_tool_terminal(
    *, repo: Path, controller_id: str, verified_turn: dict[str, Any],
    host_terminal_receipt: dict[str, Any], guard_evidence: dict[str, Any],
    guard_evidence_id: str, guard_evidence_file_sha256: str,
    snapshot_path: Path, command: str, lifecycle_path: Path,
    registry_path: Path = REGISTRY_PATH,
    deadline_monotonic: float | None = None,
) -> dict[str, Any]:
    """Apply one Host-authenticated terminal through the atomic private boundary."""
    repo = Path(repo).expanduser().resolve()
    lifecycle_path = Path(lifecycle_path).expanduser()
    target_guard._require_before_deadline(
        deadline_monotonic, operation="verified Host terminal validation"
    )
    normalized_turn = agent_target.normalize_verified_execution_turn(
        verified_turn,
        expected_logical_agent=agent_target.logical_agent_identity(
            agent_type="controller", agent_id=controller_id
        ),
        expected_host="web",
    )

    def transition(
        prior_state: dict[str, Any], terminal_commit: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        snapshot = project_snapshot(repo, deadline_monotonic=deadline_monotonic)
        if snapshot is None or Path(str(snapshot.get("root") or "")).resolve() != repo:
            raise PermissionError("Host terminal project snapshot is unavailable or mismatched")
        event = {
            "hook_event_name": "PostToolUse",
            "session_id": controller_id,
            "controller_id": controller_id,
            "controller_session_id": controller_id,
            "source_session_id": normalized_turn["execution_target_session_id"],
            "web_session_id": normalized_turn["execution_target_session_id"],
            "controller_host": "web", "execution_host": "web", "event_source": "web",
            "controller_target_generation": normalized_turn["target_generation"],
            "controller_ownership_generation": normalized_turn["ownership_generation"],
            "verified_execution_turn": normalized_turn,
            "turn_id": normalized_turn["turn_id"],
            "cwd": str(repo), "tool_name": "run_command",
            "tool_use_id": terminal_commit["tuple"]["host_tool_execution_id"],
            "tool_input": {"command": command, "cwd": str(repo)},
            "tool_response": {"state": "CLOSED"},
        }
        return evaluate_event(
            event, snapshot=snapshot, prior_state=prior_state,
            trusted_host_terminal_commit=terminal_commit,
        )

    return target_guard.commit_host_tool_execution(
        repo=repo, controller_id=controller_id, verified_turn=normalized_turn,
        host_terminal_receipt=host_terminal_receipt, guard_evidence=guard_evidence,
        guard_evidence_id=guard_evidence_id,
        guard_evidence_file_sha256=guard_evidence_file_sha256,
        snapshot_path=Path(snapshot_path), lifecycle_path=lifecycle_path,
        lifecycle_transition=transition, registry_path=Path(registry_path),
        deadline_monotonic=deadline_monotonic,
    )


def run_hook() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, TypeError):
        return 0
    if not isinstance(event, dict):
        return 0
    if _host_owned_desktop_subagent_event(event):
        return 0
    source_session_id = str(event.get("session_id", "")).strip()
    cwd = Path(str(event.get("cwd", "."))).expanduser().resolve()
    controller_id = registered_controller_id(source_session_id)
    expected_root = registered_root(source_session_id)
    if controller_id is None or expected_root is None:
        if event.get("hook_event_name") != "PreToolUse":
            return 0
        try:
            spawn_contract = target_guard.collaboration_spawn_contract(
                tool_name=event.get("tool_name"),
                tool_input=event.get("tool_input"),
            )
            if spawn_contract is None:
                return 0
            snapshot = project_snapshot(cwd)
            if snapshot is None:
                return 0
            session_repo = Path(str(snapshot["root"])).expanduser().resolve()
            try:
                from scripts.web_agent_execution import require_prepared_web_dispatch
            except ModuleNotFoundError:
                from web_agent_execution import require_prepared_web_dispatch
            require_prepared_web_dispatch(
                repo=session_repo,
                controller_id=None,
                delegator_session_id=source_session_id,
                task_name=spawn_contract["task_name"],
                expected_model=spawn_contract["model"],
                expected_agent_type=spawn_contract["agent_type"],
            )
            return 0
        except (OSError, ValueError, PermissionError, subprocess.SubprocessError) as exc:
            print(json.dumps(_pre_tool_denial(
                f"Web Agent dispatch gate rejected collaboration.spawn_agent: {exc}"
            ), ensure_ascii=False))
            return 0
    snapshot = project_snapshot(cwd)
    if (
        snapshot is None
        and not cwd.exists()
        and _desktop_codex_entry_for_repo(source_session_id, expected_root)
    ):
        snapshot = project_snapshot(expected_root)
    if snapshot is None or not controller_event_is_managed(
        event, cwd, expected_root, snapshot=snapshot
    ):
        return 0
    normalized_event = dict(event)
    normalized_event["source_session_id"] = source_session_id
    normalized_event["controller_session_id"] = controller_id
    normalized_event["session_id"] = controller_id
    normalized_event["controller_host"] = DESKTOP_SESSION_HOST
    foreground_denial = None
    if normalized_event.get("hook_event_name") == "PreToolUse":
        foreground_denial = registered_controller_foreground_denial(
            normalized_event.get("tool_name"), normalized_event.get("tool_input")
        )
    if foreground_denial is not None:
        # Fence the alias before denying. Writer/Reviewer and stale Controller
        # aliases stay outside this Controller-only host-execution policy.
        with target_guard.locked_registry(REGISTRY_PATH) as registry:
            if target_guard.active_source_controller_id(
                registry,
                source_session_id=source_session_id,
                host=DESKTOP_SESSION_HOST,
            ) != controller_id or not registry_controller_root_matches(
                registry, controller_id=controller_id, expected_root=expected_root
            ):
                return 0
        print(json.dumps(_pre_tool_denial(foreground_denial), ensure_ascii=False))
        return 0
    outbound_lease_acquired = False
    post_outbound_request: tuple[str, str] | None = None
    if normalized_event.get("hook_event_name") == "PreToolUse":
        try:
            spawn_contract = target_guard.collaboration_spawn_contract(
                tool_name=normalized_event.get("tool_name"),
                tool_input=normalized_event.get("tool_input"),
            )
            if spawn_contract is not None:
                try:
                    from scripts.web_agent_execution import require_prepared_web_dispatch
                except ModuleNotFoundError:
                    from web_agent_execution import require_prepared_web_dispatch
                require_prepared_web_dispatch(
                    repo=expected_root,
                    controller_id=controller_id,
                    task_name=spawn_contract["task_name"],
                    expected_model=spawn_contract["model"],
                    expected_agent_type=spawn_contract["agent_type"],
                )
        except (OSError, ValueError, PermissionError, subprocess.SubprocessError) as exc:
            print(json.dumps(_pre_tool_denial(
                f"Web Agent dispatch gate rejected collaboration.spawn_agent: {exc}"
            ), ensure_ascii=False))
            return 0
        try:
            outbound_request = target_guard.codex_app_outbound_request(
                tool_name=normalized_event.get("tool_name"),
                tool_input=normalized_event.get("tool_input"),
            )
            if outbound_request is not None:
                action, target_session_id = outbound_request
                target_guard.acquire_outbound_lease(
                    repo=expected_root,
                    host=DESKTOP_SESSION_HOST,
                    action=action,
                    target_session_id=target_session_id,
                    tool_use_id=_tool_use_id(normalized_event),
                    source_session_id=source_session_id,
                    registry_path=REGISTRY_PATH,
                )
                outbound_lease_acquired = True
        except (OSError, ValueError, PermissionError, subprocess.SubprocessError) as exc:
            print(json.dumps(_pre_tool_denial(
                f"Controller target guard rejected outbound task action: {exc}"
            ), ensure_ascii=False))
            return 0
    if normalized_event.get("hook_event_name") == "PostToolUse":
        try:
            post_outbound_request = target_guard.codex_app_outbound_request(
                tool_name=normalized_event.get("tool_name"),
                tool_input=normalized_event.get("tool_input"),
            )
        except (OSError, ValueError, PermissionError, subprocess.SubprocessError):
            post_outbound_request = None
    path = state_path(controller_id)
    canary_ownership_current = False
    normalized_event["controller_registry_path"] = str(
        REGISTRY_PATH.expanduser().resolve()
    )
    with target_guard.locked_registry(REGISTRY_PATH) as registry:
        try:
            unique_controller_id = target_guard.unique_controller_id_for_repo_in_registry(
                expected_root, registry
            )
        except (OSError, ValueError, PermissionError):
            return 0
        if unique_controller_id != controller_id or target_guard.active_source_controller_id(
            registry,
            source_session_id=source_session_id,
            host=DESKTOP_SESSION_HOST,
        ) != controller_id or not registry_controller_root_matches(
            registry, controller_id=controller_id, expected_root=expected_root
        ):
            return 0
        target_record = target_guard.target_record(
            registry, controller_id=controller_id, host=DESKTOP_SESSION_HOST
        )
        if target_record is not None:
            try:
                target_status, target_session_id, target_generation = (
                    target_guard.validate_target_record(
                        target_record, host=DESKTOP_SESSION_HOST
                    )
                )
            except (PermissionError, ValueError):
                return 0
            if target_status != "active" or target_session_id != source_session_id:
                return 0
            normalized_event["controller_target_generation"] = target_generation
            goal_rebind_contract = target_record.get("goal_rebind")
            if isinstance(goal_rebind_contract, dict) and _desktop_goal_rebind_contract_is_current(
                goal_rebind_contract, expected_root
            ):
                normalized_event["goal_rebind_contract"] = goal_rebind_contract
        ownership_record = target_guard.execution_ownership_record(
            registry, controller_id=controller_id
        )
        if ownership_record is not None:
            try:
                ownership_host, ownership_target, ownership_generation = (
                    target_guard.validate_execution_ownership_record(ownership_record)
                )
            except (PermissionError, ValueError):
                pass
            else:
                canary_ownership_current = (
                    ownership_host == DESKTOP_SESSION_HOST
                    and ownership_target == source_session_id
                )
                if canary_ownership_current:
                    normalized_event["controller_ownership_generation"] = ownership_generation
        recovered_canary_observations: list[
            tuple[dict[str, Any], dict[str, Any], dict[str, Any]]
        ] = []
        output, next_state = persist_event_state(
            path,
            normalized_event,
            snapshot,
            recovered_event_observer=lambda recovered_event, recovered_output, recovered_state: (
                recovered_canary_observations.append(
                    (recovered_event, recovered_output, dict(recovered_state))
                )
            ),
        )
        if canary_ownership_current:
            try:
                persist_confirmed_desktop_native_wake(
                    expected_root, normalized_event, next_state
                )
            except OSError:
                # Fail closed: without a durable receipt the release handshake
                # remains pending, while ordinary lifecycle handling can continue.
                pass
    if (
        output.get("continue") is False
        and isinstance(next_state.get("host_turn_handoff"), dict)
        and next_state["host_turn_handoff"].get("state") == "requested"
    ):
        arm_host_turn_handoff(
            path=path,
            lifecycle_state=next_state,
            controller_id=controller_id,
            repo=expected_root,
            registry=REGISTRY_PATH,
        )
    if post_outbound_request is not None and _tool_use_id(normalized_event):
        action, target_session_id = post_outbound_request
        try:
            target_guard.release_outbound_lease(
                repo=expected_root,
                host=DESKTOP_SESSION_HOST,
                tool_use_id=_tool_use_id(normalized_event),
                expected_action=action,
                expected_target_session_id=target_session_id,
                registry_path=REGISTRY_PATH,
            )
        except (OSError, ValueError, PermissionError, subprocess.SubprocessError):
            pass
    if (
        outbound_lease_acquired
        and output.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"
    ):
        target_guard.release_outbound_lease(
            repo=expected_root,
            host=DESKTOP_SESSION_HOST,
            tool_use_id=_tool_use_id(normalized_event),
            expected_action=action,
            expected_target_session_id=target_session_id,
            registry_path=REGISTRY_PATH,
        )
    if canary_ownership_current:
        try:
            for recovered_event, recovered_output, recovered_state in recovered_canary_observations:
                record_desktop_canary_observation(
                    recovered_event, recovered_output, recovered_state
                )
            record_desktop_canary_observation(normalized_event, output, next_state)
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    if output:
        print(json.dumps(output, ensure_ascii=False))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Turn Adaptive Agent Runtime controller lifecycle changes into Codex hook events."
    )
    parser.add_argument("--register-controller", nargs=2, metavar=("SESSION_ID", "REPO"))
    parser.add_argument(
        "--bind-desktop-session",
        nargs=3,
        metavar=("CONTROLLER_ID", "DESKTOP_SESSION_ID", "REPO"),
    )
    parser.add_argument(
        "--replace-desktop-session",
        nargs=3,
        metavar=("CONTROLLER_ID", "DESKTOP_SESSION_ID", "REPO"),
    )
    parser.add_argument(
        "--unbind-desktop-session",
        nargs=3,
        metavar=("CONTROLLER_ID", "DESKTOP_SESSION_ID", "REPO"),
    )
    parser.add_argument(
        "--expected-generation",
        type=int,
        help="required current desktop target generation for replace or unbind",
    )
    parser.add_argument(
        "--print-machine-trace",
        metavar="CONTROLLER_SESSION_ID",
        help="print the current turn machine trace projection for a control receipt",
    )
    parser.add_argument(
        "--arm-desktop-canary",
        metavar="CONTROLLER_SESSION_ID",
        help="arm one ordered live desktop hook canary for the registered controller",
    )
    parser.add_argument("--repo", help="canonical project checkout for canary binding")
    args = parser.parse_args(argv)
    if args.arm_desktop_canary:
        if not args.repo:
            print("--repo is required with --arm-desktop-canary", file=sys.stderr)
            return 2
        try:
            receipt = arm_desktop_canary(
                args.arm_desktop_canary,
                repo=Path(args.repo).expanduser().resolve(),
            )
        except PermissionError as error:
            print(str(error), file=sys.stderr)
            return 78
        except (OSError, ValueError) as error:
            print(str(error), file=sys.stderr)
            return 2
        print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
        return 0
    if args.print_machine_trace:
        state = load_json(state_path(args.print_machine_trace))
        if state.get("tool_trace_overflow") is True:
            print(
                "registered controller machine trace overflowed in the current turn",
                file=sys.stderr,
            )
            return 2
        projection = machine_trace_projection(state)
        if not projection.get("turn_id"):
            print("registered controller has no active turn machine trace", file=sys.stderr)
            return 2
        print(json.dumps(projection, ensure_ascii=False, sort_keys=True))
        return 0
    if args.register_controller:
        session_id, repo = args.register_controller
        root = Path(repo).expanduser().resolve()
        if not (root / ".git").exists() and not (root / "TASK_LEDGER.md").is_file():
            parser.error("REPO must be the canonical project checkout")
        register_controller(session_id, root)
        print(f"adaptive-delivery lifecycle controller registered: {session_id} -> {root}")
        return 0
    if args.bind_desktop_session:
        controller_id, desktop_session_id, repo = args.bind_desktop_session
        root = Path(repo).expanduser().resolve()
        try:
            bind_desktop_session(
                controller_id=controller_id,
                desktop_session_id=desktop_session_id,
                repo=root,
            )
        except PermissionError as error:
            print(str(error), file=sys.stderr)
            return 78
        except (OSError, ValueError) as error:
            print(str(error), file=sys.stderr)
            return 2
        print(
            json.dumps(
                {
                    "controller_id": controller_id,
                    "controller_session_id": controller_id,
                    "desktop_session_id": desktop_session_id,
                    "event_source": DESKTOP_SESSION_HOST,
                },
                ensure_ascii=False,
            )
        )
        return 0
    if args.replace_desktop_session:
        controller_id, desktop_session_id, repo = args.replace_desktop_session
        if args.expected_generation is None:
            parser.error("--expected-generation is required with --replace-desktop-session")
        try:
            receipt = replace_desktop_session(
                controller_id=controller_id,
                desktop_session_id=desktop_session_id,
                repo=Path(repo).expanduser().resolve(),
                expected_generation=args.expected_generation,
            )
        except PermissionError as error:
            print(str(error), file=sys.stderr)
            return 78
        except (OSError, ValueError) as error:
            print(str(error), file=sys.stderr)
            return 2
        print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
        return 0
    if args.unbind_desktop_session:
        controller_id, desktop_session_id, repo = args.unbind_desktop_session
        if args.expected_generation is None:
            parser.error("--expected-generation is required with --unbind-desktop-session")
        try:
            receipt = unbind_desktop_session(
                controller_id=controller_id,
                desktop_session_id=desktop_session_id,
                repo=Path(repo).expanduser().resolve(),
                expected_generation=args.expected_generation,
            )
        except PermissionError as error:
            print(str(error), file=sys.stderr)
            return 78
        except (OSError, ValueError) as error:
            print(str(error), file=sys.stderr)
            return 2
        print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
        return 0
    return run_hook()


if __name__ == "__main__":
    raise SystemExit(main())
