#!/usr/bin/env python3
"""Durable host-observation contract for Controller Goal/title display sync.

This module does not call host APIs or schedule another process.  The existing
lifecycle hook uses it to order and attest the three Controller-owned host tool
calls that follow an already validated ledger Goal rollover.
"""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any


REQUIRED_HOST_CAPABILITIES = {"update_goal", "create_goal", "set_thread_title"}
STEP_BY_STATUS = {
    "pending_update_goal": "update_goal_complete",
    "pending_create_goal": "create_goal",
    "pending_thread_title": "set_thread_title",
}
NEXT_STATUS = {
    "update_goal_complete": "pending_create_goal",
    "create_goal": "pending_thread_title",
    "set_thread_title": "completed",
}


def _json_sha256(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _tool_kind(value: Any) -> str:
    name = str(value or "").strip().rsplit(".", 1)[-1]
    return name.rsplit("__", 1)[-1]


def _positive_generation(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def _binding_error(receipt: dict[str, Any], event: dict[str, Any]) -> str | None:
    expected = {
        "controller_session_id": receipt.get("controller_id"),
        "source_session_id": receipt.get("execution_target_session_id"),
        "controller_host": receipt.get("host"),
    }
    for field, value in expected.items():
        if str(event.get(field, "")).strip() != str(value or "").strip():
            return f"Goal display sync exact {field} changed; fail closed."
    generation = _positive_generation(event.get("controller_target_generation"))
    if generation != receipt.get("target_generation"):
        return "Goal display sync target generation changed; fail closed."
    return None


def start_goal_display_sync(
    existing: dict[str, Any] | None,
    rollover: dict[str, Any],
    *,
    controller_id: str,
    source_session_id: str,
    host: str,
    target_generation: int | None,
    turn_id: str,
    host_capabilities: set[str],
) -> dict[str, Any] | None:
    """Create one idempotent receipt only for a validated rolled Goal."""
    if str(rollover.get("status", "")).strip().lower() != "rolled":
        return None
    if rollover.get("project_recomputed") is not True:
        raise ValueError("Goal display sync requires project_recomputed=true")
    ledger_sha256 = str(rollover.get("ledger_sha256", "")).strip()
    closed_goal_id = str(rollover.get("closed_goal_id", "")).strip()
    current_goal_id = str(rollover.get("current_goal_id", "")).strip()
    objective = str(rollover.get("current_goal_display", "")).strip()
    project_name = str(rollover.get("project_name", "")).strip()
    generation = _positive_generation(target_generation)
    if not all((ledger_sha256, closed_goal_id, current_goal_id, objective, project_name)):
        raise ValueError("Goal display sync rollover contract is incomplete")
    if closed_goal_id == current_goal_id or current_goal_id not in objective:
        raise ValueError("Goal display sync current Goal does not match the ledger display")
    fingerprint = _json_sha256({
        "controller_id": controller_id,
        "execution_target_session_id": source_session_id,
        "host": host,
        "target_generation": generation,
        "ledger_sha256": ledger_sha256,
        "closed_goal_id": closed_goal_id,
        "current_goal_id": current_goal_id,
        "objective": objective,
    })
    if isinstance(existing, dict) and existing.get("fingerprint") == fingerprint:
        return existing
    receipt: dict[str, Any] = {
        "schema_version": 1,
        "receipt_id": "goal-display-sync:" + fingerprint[:24],
        "fingerprint": fingerprint,
        "status": "pending_update_goal",
        "controller_id": controller_id,
        "execution_target_session_id": source_session_id,
        "host": host,
        "target_generation": generation,
        "ledger_sha256": ledger_sha256,
        "closed_goal_id": closed_goal_id,
        "current_goal_id": current_goal_id,
        "objective": objective,
        "thread_title": f"{project_name} 总控｜{objective}",
        "rollover_turn_id": turn_id,
        "steps": [],
    }
    if generation is None:
        receipt.update({
            "status": "degraded",
            "reason": "CURRENT_TARGET_GENERATION_UNAVAILABLE",
        })
    elif not REQUIRED_HOST_CAPABILITIES.issubset(host_capabilities):
        receipt.update({
            "status": "degraded",
            "reason": "HOST_GOAL_DISPLAY_CAPABILITY_UNAVAILABLE",
        })
    return receipt


def _expected_tool_error(receipt: dict[str, Any], event: dict[str, Any]) -> str | None:
    status = str(receipt.get("status", ""))
    expected_step = STEP_BY_STATUS.get(status)
    if expected_step is None:
        if status == "degraded":
            return "Goal display sync host capability is degraded; host tools are unavailable."
        return None
    kind = _tool_kind(event.get("tool_name"))
    tool_input = event.get("tool_input")
    if not isinstance(tool_input, dict):
        return f"Goal display sync requires {expected_step} next."
    if expected_step == "update_goal_complete":
        valid = kind == "update_goal" and str(tool_input.get("status", "")).lower() == "complete"
        expected_name = "update_goal(status=complete)"
    elif expected_step == "create_goal":
        valid = kind == "create_goal" and str(tool_input.get("objective", "")).strip() == receipt.get("objective")
        expected_name = "create_goal with the exact ledger current Goal"
    else:
        target = str(tool_input.get("threadId", tool_input.get("thread_id", ""))).strip()
        valid_target = not target or target == receipt.get("execution_target_session_id")
        valid = (
            kind == "set_thread_title"
            and str(tool_input.get("title", "")).strip() == receipt.get("thread_title")
            and valid_target
        )
        expected_name = "set_thread_title with the exact Controller task title"
    if not valid:
        return f"Goal display sync requires {expected_name} next."
    return None


def authorize_goal_display_sync_tool(
    receipt: dict[str, Any], event: dict[str, Any]
) -> tuple[dict[str, Any], str | None]:
    if receipt.get("status") == "completed":
        return receipt, None
    binding_error = _binding_error(receipt, event)
    if binding_error:
        return receipt, binding_error
    expected_error = _expected_tool_error(receipt, event)
    if expected_error:
        return receipt, expected_error
    tool_use_id = str(event.get("tool_use_id", "")).strip()
    if not tool_use_id:
        return receipt, "Goal display sync host tool requires a tool_use_id."
    if isinstance(receipt.get("inflight"), dict):
        return receipt, "Goal display sync already has an inflight host tool."
    next_receipt = copy.deepcopy(receipt)
    next_receipt["inflight"] = {
        "step": STEP_BY_STATUS[str(receipt.get("status"))],
        "tool_use_id": tool_use_id,
        "turn_id": str(event.get("turn_id", "")).strip(),
        "input_sha256": _json_sha256(event.get("tool_input")),
    }
    return next_receipt, None


def observe_goal_display_sync_result(
    receipt: dict[str, Any], event: dict[str, Any]
) -> dict[str, Any]:
    binding_error = _binding_error(receipt, event)
    if binding_error:
        return receipt
    inflight = receipt.get("inflight")
    tool_use_id = str(event.get("tool_use_id", "")).strip()
    if not isinstance(inflight, dict) or inflight.get("tool_use_id") != tool_use_id:
        return receipt
    next_receipt = copy.deepcopy(receipt)
    next_receipt.pop("inflight", None)
    response = event.get("tool_response")
    success = (
        isinstance(response, dict)
        and response.get("isError") is not True
        and response.get("exit_code") in (None, 0)
    )
    if not success:
        next_receipt["last_failure"] = {
            "step": inflight.get("step"),
            "tool_use_id": tool_use_id,
            "response_sha256": _json_sha256(response),
        }
        return next_receipt
    step = dict(inflight)
    step["response_sha256"] = _json_sha256(response)
    steps = [item for item in next_receipt.get("steps", []) if isinstance(item, dict)]
    if not any(item.get("tool_use_id") == tool_use_id for item in steps):
        steps.append(step)
    next_receipt["steps"] = steps
    next_receipt["status"] = NEXT_STATUS[str(step["step"])]
    next_receipt.pop("last_failure", None)
    if next_receipt["status"] == "completed":
        next_receipt["completed_turn_id"] = str(event.get("turn_id", "")).strip()
    return next_receipt
