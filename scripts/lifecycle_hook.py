#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

from lint_governance import task_rows


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


def project_snapshot(cwd: Path) -> dict[str, Any] | None:
    try:
        root = Path(run_git(cwd, "rev-parse", "--show-toplevel")).resolve()
        branch = run_git(root, "branch", "--show-current")
    except (OSError, subprocess.CalledProcessError, ValueError):
        return None
    if branch != "main":
        return None
    ledger = next((root / name for name in LEDGER_NAMES if (root / name).is_file()), None)
    if ledger is None:
        return None
    text = ledger.read_text(encoding="utf-8")
    ready_ids = sorted(
        identifier
        for identifier, status in task_rows(text)
        if status == "READY"
    )
    status = run_git(root, "status", "--porcelain=v1", "--untracked-files=no")
    return {
        "root": str(root),
        "ledger": str(ledger),
        "head": run_git(root, "rev-parse", "HEAD"),
        "ledger_sha256": sha256_bytes(ledger.read_bytes()),
        "worktree_status_sha256": sha256_bytes(status.encode("utf-8")),
        "ready_ids": ready_ids,
    }


def successful_control_receipt(event: dict[str, Any]) -> bool:
    if event.get("hook_event_name") != "PostToolUse":
        return False
    tool_input = event.get("tool_input")
    if not isinstance(tool_input, dict):
        return False
    command = str(tool_input.get("command", ""))
    if "control_event_guard.py" not in command:
        return False
    response = event.get("tool_response")
    if not isinstance(response, dict) or response.get("exit_code") not in (None, 0):
        return False
    output = json.dumps(response, ensure_ascii=False)
    return "control-event: allowed" in output


def lifecycle_triggers(
    snapshot: dict[str, Any], prior_state: dict[str, Any] | None
) -> list[str]:
    triggers = [f"READY:{identifier}" for identifier in snapshot.get("ready_ids", [])]
    previous = prior_state.get("snapshot") if isinstance(prior_state, dict) else None
    if isinstance(previous, dict):
        for field, label in (
            ("head", "main_head_changed"),
            ("ledger_sha256", "ledger_changed"),
            ("worktree_status_sha256", "main_worktree_changed"),
        ):
            if previous.get(field) != snapshot.get(field):
                triggers.append(label)
    return sorted(set(triggers))


def continuation_reason(triggers: list[str], ready_ids: list[str]) -> str:
    ready = ", ".join(ready_ids) if ready_ids else "无新增 READY"
    trigger_text = ", ".join(triggers) if triggers else "未闭合控制事件"
    return (
        "Adaptive Delivery 生命周期门检测到尚未闭合的控制事件。"
        f"触发：{trigger_text}；当前 READY：{ready}。"
        "请立即核对真实 main / 台账 / live Agent，处理候选审查、集成、验收、"
        "READY 派发与 ACK；随后用 control_event_guard.py 生成通过收据。"
        "不得仅把动作写成下一事件后停止。"
    )


def evaluate_event(
    event: dict[str, Any],
    *,
    snapshot: dict[str, Any] | None,
    prior_state: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if snapshot is None:
        return {}, {}
    state = dict(prior_state or {})
    state["snapshot"] = snapshot
    state["session_id"] = str(event.get("session_id", ""))
    event_name = event.get("hook_event_name")

    if event_name == "SessionStart":
        triggers = lifecycle_triggers(snapshot, None)
        state.update(
            {
                "pending_control_event": bool(triggers),
                "triggers": triggers,
                "stop_continuations": 0,
            }
        )
        if not triggers:
            return {}, state
        context = continuation_reason(triggers, list(snapshot.get("ready_ids", [])))
        return {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": context,
            }
        }, state

    if event_name == "SubagentStop":
        triggers = set(state.get("triggers", []))
        agent_id = str(event.get("agent_id", "unknown"))
        triggers.add(f"subagent_stopped:{agent_id}")
        state.update(
            {
                "pending_control_event": True,
                "triggers": sorted(triggers),
                "stop_continuations": 0,
            }
        )
        return {}, state

    if successful_control_receipt(event) and not snapshot.get("ready_ids"):
        state.update(
            {
                "pending_control_event": False,
                "triggers": [],
                "stop_continuations": 0,
            }
        )
        return {}, state

    detected = lifecycle_triggers(snapshot, prior_state)
    triggers = sorted(set(state.get("triggers", [])) | set(detected))
    pending = bool(state.get("pending_control_event")) or bool(triggers)
    state.update({"pending_control_event": pending, "triggers": triggers})

    if event_name == "PostToolUse":
        if not pending:
            return {}, state
        context = continuation_reason(triggers, list(snapshot.get("ready_ids", [])))
        return {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": context,
            }
        }, state

    if event_name == "Stop" and pending:
        continuations = int(state.get("stop_continuations", 0)) + 1
        state["stop_continuations"] = continuations
        reason = continuation_reason(triggers, list(snapshot.get("ready_ids", [])))
        if continuations <= 3:
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


def register_controller(session_id: str, root: Path) -> None:
    registry = load_json(REGISTRY_PATH)
    registry[session_id] = str(root.resolve())
    write_json(REGISTRY_PATH, registry)


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
    if snapshot is None or Path(snapshot["root"]).resolve() != expected_root:
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
