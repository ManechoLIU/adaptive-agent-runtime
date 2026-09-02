#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import secrets
import shlex
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

try:
    from lint_governance import task_records, task_rows
except ModuleNotFoundError:
    from scripts.lint_governance import task_records, task_rows
try:
    from controller_state import derive_runnable_tasks, project_task_state
except ModuleNotFoundError:
    from scripts.controller_state import derive_runnable_tasks, project_task_state

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
    "session_started",
    "pre_tool_allowed",
    "post_tool_observed",
    "receipt_latched",
    "same_turn_denied",
    "stop_observed",
    "next_turn_allowed",
    "subagent_stop_observed",
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


def _begin_turn(state: dict[str, Any], event: dict[str, Any]) -> None:
    turn_id = _event_turn_id(event)
    if not turn_id:
        return
    current_turn_id = str(state.get("active_turn_id", ""))
    if current_turn_id == turn_id:
        return
    if current_turn_id and event.get("hook_event_name") not in {
        "SessionStart",
        "UserPromptSubmit",
    }:
        return
    state["active_turn_id"] = turn_id
    state["must_yield"] = False
    state["tool_trace"] = []
    state["tool_trace_overflow"] = False
    state["inflight_tool_use_ids"] = []
    state.pop("control_receipt_inflight", None)
    state.pop("receipt_turn_id", None)


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


def _tool_use_id(event: dict[str, Any]) -> str:
    return str(event.get("tool_use_id", "")).strip()


def _record_tool_trace(state: dict[str, Any], event: dict[str, Any]) -> None:
    if event.get("hook_event_name") != "PostToolUse":
        return
    tool_name = str(event.get("tool_name", "")).strip()
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
    trace.append(entry)
    if len(trace) > MAX_TOOL_TRACE_ENTRIES:
        state["tool_trace_overflow"] = True
    state["tool_trace"] = trace[-MAX_TOOL_TRACE_ENTRIES:]


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
    return {
        "turn_id": turn_id,
        "tool_use_ids": [item["tool_use_id"] for item in trace],
        "trace_sha256": _json_sha256(trace),
    }


def _pre_tool_denial(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _desktop_canary_identity(
    *, hooks_path: Path, skill_root: Path | None
) -> dict[str, Any]:
    root = (skill_root or Path(__file__).resolve().parents[1]).resolve()
    lifecycle = root / "scripts" / "lifecycle_hook.py"
    target_guard_path = root / "scripts" / "controller_target_guard.py"
    return {
        "schema_version": 3,
        "skill_root": str(root),
        "hooks_sha256": sha256_bytes(hooks_path.read_bytes()),
        "lifecycle_sha256": sha256_bytes(lifecycle.read_bytes()),
        "controller_target_guard_sha256": sha256_bytes(target_guard_path.read_bytes()),
    }


def arm_desktop_canary(
    controller_session_id: str,
    *,
    canary_path: Path = DESKTOP_CANARY_PATH,
    hooks_path: Path = CODEX_HOOKS_PATH,
    skill_root: Path | None = None,
) -> dict[str, Any]:
    session_id = controller_session_id.strip()
    if not session_id:
        raise ValueError("controller session is required to arm desktop canary")
    identity = _desktop_canary_identity(
        hooks_path=hooks_path, skill_root=skill_root
    )
    now = datetime.now(timezone.utc).isoformat()
    receipt = {
        **identity,
        "status": "armed",
        "controller_session_id": session_id,
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
            session_id = str(event.get("session_id", "")).strip()
            if session_id != str(current.get("controller_session_id", "")):
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
            if index == 0 and event_name == "SessionStart" and turn_id:
                observation = "session_started"
                current["first_turn_id"] = turn_id
            elif index == 1 and event_name == "PreToolUse" and not denied:
                observation = "pre_tool_allowed"
            elif index == 2 and event_name == "PostToolUse" and state.get("must_yield") is not True:
                observation = "post_tool_observed"
            elif (
                index == 3
                and event_name == "PostToolUse"
                and state.get("must_yield") is True
                and str(state.get("receipt_turn_id", "")) == turn_id
            ):
                observation = "receipt_latched"
                current["denied_turn_id"] = turn_id
            elif (
                index == 4
                and event_name == "PreToolUse"
                and denied
                and turn_id == str(current.get("denied_turn_id", ""))
            ):
                observation = "same_turn_denied"
            elif index == 5 and event_name == "Stop":
                observation = "stop_observed"
            elif (
                index == 6
                and event_name == "PreToolUse"
                and not denied
                and turn_id
                and turn_id != str(current.get("denied_turn_id", ""))
            ):
                observation = "next_turn_allowed"
            elif index == 7 and event_name == "SubagentStop":
                observation = "subagent_stop_observed"
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


def run_git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def git_common_dir(cwd: Path) -> Path | None:
    try:
        value = run_git(cwd, "rev-parse", "--git-common-dir")
    except (OSError, subprocess.CalledProcessError, ValueError):
        return None
    if not value:
        return None
    path = Path(value)
    return (path if path.is_absolute() else cwd / path).resolve()


def canonical_main_root(cwd: Path) -> Path | None:
    try:
        worktrees = run_git(cwd, "worktree", "list", "--porcelain")
    except (OSError, subprocess.CalledProcessError, ValueError):
        return None
    worktree: Path | None = None
    for line in worktrees.splitlines():
        if line.startswith("worktree "):
            worktree = Path(line.removeprefix("worktree ")).resolve()
        elif line == "branch refs/heads/main" and worktree is not None:
            return worktree
    return None


def project_snapshot(cwd: Path) -> dict[str, Any] | None:
    try:
        invocation_root = Path(run_git(cwd, "rev-parse", "--show-toplevel")).resolve()
    except (OSError, subprocess.CalledProcessError, ValueError):
        return None
    common_dir = git_common_dir(invocation_root)
    root = canonical_main_root(invocation_root)
    if common_dir is None or root is None or git_common_dir(root) != common_dir:
        return None
    ledger = next((root / name for name in LEDGER_NAMES if (root / name).is_file()), None)
    if ledger is None:
        return None
    text = ledger.read_text(encoding="utf-8")
    from ledger_consistency_guard import validate_ledger

    ledger_errors = validate_ledger(text)
    ready_ids = sorted(
        identifier
        for identifier, status in task_rows(text)
        if status == "READY"
    )
    runnable_projection = derive_runnable_tasks(task_records(text))
    runnable_ids = list(runnable_projection["runnable_task_ids"])
    status = run_git(root, "status", "--porcelain=v1", "--untracked-files=no")
    from control_event_guard import unmerged_worktree_candidates

    candidates = unmerged_worktree_candidates(root)
    try:
        from assignment_runtime import evaluate_lease, load_runtime_state
    except ModuleNotFoundError:
        from scripts.assignment_runtime import evaluate_lease, load_runtime_state

    runtime_state = load_runtime_state(root)
    leases = runtime_state.get("leases", {}) if isinstance(runtime_state, dict) else {}
    ledger_states = {identifier: status for identifier, status in task_rows(text)}
    assignment_liveness: dict[str, dict[str, Any]] = {}
    if isinstance(leases, dict):
        for lease in leases.values():
            if not isinstance(lease, dict):
                continue
            task_id = str(lease.get("task_id", "")).strip()
            ledger_state = ledger_states.get(task_id, "")
            if not task_id or ledger_state not in {"ACTIVE", "RECOVERING"}:
                continue
            decision = evaluate_lease(lease)
            assignment_liveness[task_id] = {"ledger_state": ledger_state, **decision}
    for task_id, ledger_state in ledger_states.items():
        if ledger_state in {"ACTIVE", "RECOVERING"} and task_id not in assignment_liveness:
            assignment_liveness[task_id] = {"ledger_state": ledger_state, "state": "unknown", "reason": "missing_runtime_lease"}
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
    return {
        "root": str(root),
        "git_common_dir": str(common_dir),
        "ledger": str(ledger),
        "head": run_git(root, "rev-parse", "HEAD"),
        "ledger_sha256": sha256_bytes(ledger.read_bytes()),
        "worktree_status_sha256": sha256_bytes(status.encode("utf-8")),
        "ready_ids": ready_ids,
        "runnable_ids": runnable_ids,
        "runnable_exclusions": runnable_projection["exclusions"],
        "candidate_revisions": sorted(candidates.values()),
        "ledger_errors": ledger_errors,
        "assignment_liveness": assignment_liveness,
        "task_projection": task_projection,
        "rule_handshake": rule_handshake,
    }


def successful_control_receipt(
    event: dict[str, Any], snapshot: dict[str, Any] | None = None
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
    return "control-event: allowed" in output


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
        if not str(item).startswith(("rule_update_pending:", "rule_ledger_stale:", "rule_install_integrity_error:"))
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
        handshake_script = Path(__file__).resolve().parent / "rule_handshake.py"
        rule_text = (
            f" 规则更新待加载：{revision}；摘要：{summary}；影响：{impact}；停止条件：{stop}。"
            "先读取已安装的新规则，再执行 "
            f'python3 "{handshake_script}" ack{repo_arg}{session_arg} --revision {revision}，随后同步现有台账规则版本行。'
        )
    elif state == "ledger_stale":
        rule_text = f" 已有 LOADED ACK {revision}，但台账规则版本仍旧；先把现有规则版本行同步到精确 revision {revision}。"
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
    actions.append("随后用 control_event_guard.py 生成通过收据")
    lifecycle = (
        "Adaptive Agent Runtime 生命周期门检测到尚未闭合的控制事件。"
        f"触发：{trigger_text}；当前可执行：{ready}；候选：{candidates}。"
        + "；".join(actions) + "。"
        "不得仅把动作写成下一事件后停止。" + rule_text
    )
    return lifecycle + "\n\n" + controller_self_check_context()


def evaluate_event(
    event: dict[str, Any],
    *,
    snapshot: dict[str, Any] | None,
    prior_state: dict[str, Any] | None,
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
    event_name = event.get("hook_event_name")
    _begin_turn(state, event)
    if "must_yield" not in state:
        state["must_yield"] = False
    if "tool_trace" not in state:
        state["tool_trace"] = []
    if event_name == "PreToolUse":
        if state.get("must_yield") is True:
            return _pre_tool_denial(
                "当前控制事件已经签发成功收据；同一回合必须立即结束，禁止继续调用工具。"
            ), state
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
        if is_guard:
            state["control_receipt_inflight"] = tool_use_id or "unknown"
        return {}, state
    if event_name == "PostToolUse":
        tool_use_id = _tool_use_id(event)
        state["inflight_tool_use_ids"] = [
            str(item)
            for item in state.get("inflight_tool_use_ids", [])
            if str(item) and str(item) != tool_use_id
        ]
        if str(state.get("control_receipt_inflight", "")) == tool_use_id:
            state.pop("control_receipt_inflight", None)
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
            rule_handshake=snapshot.get("rule_handshake"), root=snapshot.get("root"), session_id=str(event.get("session_id", "")),
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
        triggers = set(state.get("triggers", []))
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

    if successful_control_receipt(event, snapshot):
        receipt_turn_id = _event_turn_id(event) or str(state.get("active_turn_id", ""))
        state["must_yield"] = True
        state["receipt_turn_id"] = receipt_turn_id
        if wake_policy == "after_event" and str(handshake.get("state", "")) == "pending_ack":
            rule_triggers = [item for item in lifecycle_triggers(snapshot, None) if item.startswith("rule_update_pending:")]
            state.update({
                "pending_control_event": bool(rule_triggers),
                "triggers": rule_triggers,
                "stop_continuations": 0,
                "rule_wake_policy": "after_event",
                "pending_terminal_receipts": [],
                "next_action": "",
                "requires_user": False,
            })
            if rule_triggers and not prior_pending:
                state["wake_generation"] = prior_generation + 1
        else:
            state.update(
                {
                    "pending_control_event": False,
                    "triggers": [],
                    "stop_continuations": 0,
                    "pending_terminal_receipts": [],
                    "next_action": "",
                    "requires_user": False,
                }
            )
        return {}, state

    detected = lifecycle_triggers(snapshot, prior_state)
    prior_triggers = {str(item) for item in state.get("triggers", [])}
    transient_prefixes = (
        "rule_update_pending:", "rule_ledger_stale:", "rule_install_integrity_error:",
        "active_lease_expired:", "assignment_became_unhealthy:", "agent_session_terminal:",
        "active_without_progress:", "recovery_stalled:", "recovery_budget_exhausted:",
    )
    prior_triggers = {item for item in prior_triggers if not item.startswith(transient_prefixes)}
    current_ready = {str(item) for item in snapshot.get("ready_ids", [])}
    current_runnable = {str(item) for item in snapshot.get("runnable_ids", snapshot.get("ready_ids", []))}
    current_candidates = {str(item) for item in snapshot.get("candidate_revisions", [])}
    prior_triggers = {
        item for item in prior_triggers
        if not (item.startswith("READY:") and item.removeprefix("READY:") not in current_ready)
        and not (item.startswith("RUNNABLE:") and item.removeprefix("RUNNABLE:") not in current_runnable)
        and not (item.startswith("CANDIDATE:") and item.removeprefix("CANDIDATE:") not in current_candidates)
    }
    triggers = sorted(prior_triggers | set(detected))
    pending_next_action = str(state.get("next_action") or "").strip()
    continuation_pending = bool(pending_next_action) and state.get("requires_user") is False
    if continuation_pending and "next_action_pending" not in triggers:
        triggers = sorted(set(triggers) | {"next_action_pending"})
    pending = bool(state.get("pending_control_event")) or bool(triggers) or continuation_pending
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
            rule_handshake=snapshot.get("rule_handshake"), root=snapshot.get("root"), session_id=str(event.get("session_id", "")),
            next_action=pending_next_action if continuation_pending else None,
        )
        return {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": context,
            }
        }, state

    if event_name == "Stop" and pending:
        continuations = int(state.get("stop_continuations", 0)) + 1
        state["stop_continuations"] = continuations
        reason = continuation_reason(
            triggers,
            list(snapshot.get("ready_ids", [])),
            list(snapshot.get("candidate_revisions", [])),
            runnable_ids=list(snapshot.get("runnable_ids", snapshot.get("ready_ids", []))),
            rule_handshake=snapshot.get("rule_handshake"), root=snapshot.get("root"), session_id=str(event.get("session_id", "")),
            next_action=pending_next_action if continuation_pending else None,
        )
        return {"decision": "block", "reason": reason}, state
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


def replace_desktop_session(
    *, controller_id: str, desktop_session_id: str, repo: Path, expected_generation: int
) -> dict[str, Any]:
    controller_id = controller_id.strip()
    desktop_session_id = desktop_session_id.strip()
    if not controller_id or not desktop_session_id:
        raise ValueError("controller-id and desktop-session-id are required")
    canonical_root = canonical_main_root(repo)
    if canonical_root is None:
        raise ValueError("desktop session replacement requires a canonical Git project")
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
            controller_targets[DESKTOP_SESSION_HOST] = target
            targets[controller_id] = controller_targets
            registry[CONTROLLER_TARGETS_KEY] = targets
            write_json(REGISTRY_PATH, registry)
            return {
                "controller_id": controller_id,
                "controller_session_id": controller_id,
                "execution_target_session_id": desktop_session_id,
                "host": DESKTOP_SESSION_HOST,
                "repo": str(canonical_root.resolve()),
                **target,
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


def controller_event_is_managed(
    event: dict[str, Any], cwd: Path, expected_root: Path
) -> bool:
    session_id = str(event.get("session_id", "")).strip()
    if not session_id or registered_root(session_id) != expected_root.resolve():
        return False
    try:
        invocation_root = Path(run_git(cwd, "rev-parse", "--show-toplevel")).resolve()
    except (OSError, subprocess.CalledProcessError, ValueError):
        return False
    if registered_controller_surface(session_id, expected_root) != invocation_root:
        return False
    snapshot = project_snapshot(cwd)
    if snapshot is None or Path(snapshot["root"]).resolve() != expected_root.resolve():
        return False
    return snapshot.get("git_common_dir") == str(git_common_dir(expected_root))


def state_path(session_id: str) -> Path:
    safe_id = "".join(character for character in session_id if character.isalnum() or character in "-_")
    return STATE_ROOT / f"{safe_id or 'unknown'}.json"


def persist_event_state(
    path: Path, event: dict[str, Any], snapshot: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            output, next_state = evaluate_event(
                event,
                snapshot=snapshot,
                prior_state=load_json(path),
            )
            write_json(path, next_state)
            return output, next_state
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def run_hook() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, TypeError):
        return 0
    if not isinstance(event, dict):
        return 0
    source_session_id = str(event.get("session_id", "")).strip()
    cwd = Path(str(event.get("cwd", "."))).expanduser().resolve()
    controller_id = registered_controller_id(source_session_id)
    expected_root = registered_root(source_session_id)
    if controller_id is None or expected_root is None:
        return 0
    snapshot = project_snapshot(cwd)
    if snapshot is None or not controller_event_is_managed(event, cwd, expected_root):
        return 0
    normalized_event = dict(event)
    normalized_event["source_session_id"] = source_session_id
    normalized_event["controller_session_id"] = controller_id
    normalized_event["session_id"] = controller_id
    normalized_event["controller_host"] = DESKTOP_SESSION_HOST
    outbound_lease_acquired = False
    post_outbound_request: tuple[str, str] | None = None
    if normalized_event.get("hook_event_name") == "PreToolUse":
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
    with target_guard.locked_registry(REGISTRY_PATH) as registry:
        if target_guard.active_source_controller_id(
            registry,
            source_session_id=source_session_id,
            host=DESKTOP_SESSION_HOST,
        ) != controller_id or not registry_controller_root_matches(
            registry, controller_id=controller_id, expected_root=expected_root
        ):
            return 0
        output, next_state = persist_event_state(path, normalized_event, snapshot)
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
    try:
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
    args = parser.parse_args(argv)
    if args.arm_desktop_canary:
        receipt = arm_desktop_canary(args.arm_desktop_canary)
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
