#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import subprocess
import sys
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



def controller_self_check_context() -> str:
    skill_root = Path(__file__).resolve().parents[1]
    try:
        return render_controller_self_check(skill_root)
    except (OSError, UnicodeDecodeError, ValueError) as error:
        return f"Controller Self-Check unavailable from installed scoring model: {error}; do not infer or calculate a score."

def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


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
    if "control_event_guard.py" not in command:
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
        rule_text = (
            f" 规则更新待加载：{revision}；摘要：{summary}；影响：{impact}；停止条件：{stop}。"
            "先读取已安装的新规则，再执行 "
            f"python3 ~/.agents/skills/adaptive-delivery/scripts/rule_handshake.py ack{repo_arg}{session_arg} --revision {revision}，随后同步现有台账规则版本行。"
        )
    elif state == "ledger_stale":
        rule_text = f" 已有 LOADED ACK {revision}，但台账规则版本仍旧；先把现有规则版本行同步到精确 revision {revision}。"
    elif state == "integrity_error":
        errors = "; ".join(str(item) for item in handshake.get("errors", []))
        rule_text = f" Adaptive Delivery 安装完整性失败：{errors}；禁止 ACK 或启动受影响 Assignment。"
    actions = ["请立即核对真实 main / 台账 / live Agent"]
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
    event_host = str(event.get("controller_host", "")).strip()
    state["controller_host"] = event_host if event_host in {"web", "desktop_codex"} else "desktop_codex"
    event_name = event.get("hook_event_name")
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
        state.update(
            {
                "pending_control_event": bool(triggers),
                "triggers": triggers,
                "stop_continuations": 0,
            }
        )
        if triggers and not prior_pending:
            state["wake_generation"] = prior_generation + 1
        if not triggers:
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
        )
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

    if successful_control_receipt(event, snapshot) and not snapshot.get("runnable_ids", snapshot.get("ready_ids")):
        if wake_policy == "after_event" and str(handshake.get("state", "")) == "pending_ack":
            rule_triggers = [item for item in lifecycle_triggers(snapshot, None) if item.startswith("rule_update_pending:")]
            state.update({
                "pending_control_event": bool(rule_triggers),
                "triggers": rule_triggers,
                "stop_continuations": 0,
                "rule_wake_policy": "after_event",
                "pending_terminal_receipts": [],
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
    pending = bool(state.get("pending_control_event")) or bool(triggers)
    if wake_policy == "next_turn" and event_name != "SessionStart" and not _non_rule_triggers(triggers):
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
        )
        if snapshot.get("runnable_ids", snapshot.get("ready_ids")) or continuations <= 1:
            return {"decision": "block", "reason": reason}, state
        return {
            "continue": False,
            "stopReason": "Adaptive Delivery controller lifecycle gate failed closed.",
            "systemMessage": reason,
        }, state
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


def registered_root(session_id: str) -> Path | None:
    registry = load_json(REGISTRY_PATH)
    value = registry.get(session_id)
    if not isinstance(value, str) or not value.strip():
        return None
    return Path(value).expanduser().resolve()


def registered_controller_surface(session_id: str, expected_root: Path) -> Path | None:
    registry = load_json(REGISTRY_PATH)
    surfaces = registry.get(CONTROLLER_SURFACES_KEY)
    if surfaces is None:
        return expected_root.resolve()
    if not isinstance(surfaces, dict):
        return None
    value = surfaces.get(session_id)
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


def run_hook() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, TypeError):
        return 0
    if not isinstance(event, dict):
        return 0
    session_id = str(event.get("session_id", "")).strip()
    cwd = Path(str(event.get("cwd", "."))).expanduser().resolve()
    expected_root = registered_root(session_id)
    if expected_root is None:
        return 0
    snapshot = project_snapshot(cwd)
    if snapshot is None or not controller_event_is_managed(event, cwd, expected_root):
        return 0
    path = state_path(session_id)
    output, next_state = evaluate_event(
        event,
        snapshot=snapshot,
        prior_state=load_json(path),
    )
    write_json(path, next_state)
    if output:
        print(json.dumps(output, ensure_ascii=False))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Turn Adaptive Delivery controller lifecycle changes into Codex hook events."
    )
    parser.add_argument("--register-controller", nargs=2, metavar=("SESSION_ID", "REPO"))
    args = parser.parse_args(argv)
    if args.register_controller:
        session_id, repo = args.register_controller
        root = Path(repo).expanduser().resolve()
        if not (root / ".git").exists() and not (root / "TASK_LEDGER.md").is_file():
            parser.error("REPO must be the canonical project checkout")
        register_controller(session_id, root)
        print(f"adaptive-delivery lifecycle controller registered: {session_id} -> {root}")
        return 0
    return run_hook()


if __name__ == "__main__":
    raise SystemExit(main())
