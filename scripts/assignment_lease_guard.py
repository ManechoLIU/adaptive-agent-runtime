#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

try:
    from scripts.assignment_runtime import evaluate_lease
except ModuleNotFoundError:
    from assignment_runtime import evaluate_lease


STATES = {"RESERVED", "ACKED", "ACTIVE", "CANDIDATE", "FROZEN", "TERMINAL"}
ACK_FIELDS = (
    "repository_root",
    "branch",
    "head",
    "status",
    "owned_files",
    "first_red",
    "stop_condition",
)


def nonempty_list(value: Any) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(item, str) and item.strip() for item in value)
        and len(set(value)) == len(value)
    )


def validate_assignment(
    assignment: dict[str, Any],
    runtime_state: dict[str, Any] | None = None,
    now: datetime | None = None,
    *,
    expected_assignment_id: str | None = None,
    expected_task_id: str | None = None,
    expected_agent_id: str | None = None,
    expected_repository_root: str | None = None,
    expected_branch: str | None = None,
    expected_head: str | None = None,
) -> list[str]:
    errors: list[str] = []
    assignment_id = str(assignment.get("assignment_id", "")).strip()
    agent_id = str(assignment.get("agent_id", "")).strip()
    state = str(assignment.get("state", "")).strip().upper()
    if not assignment_id:
        errors.append("assignment_id is required")
    if not agent_id:
        errors.append("agent_id is required")
    if state not in STATES:
        errors.append("state must be RESERVED, ACKED, ACTIVE, CANDIDATE, FROZEN, or TERMINAL")

    if state in {"ACKED", "ACTIVE", "CANDIDATE"}:
        if not isinstance(assignment.get("primary_goal"), str) or not assignment["primary_goal"].strip():
            errors.append("primary_goal is required")
        for field in ("success_criteria", "owned_scope"):
            if not nonempty_list(assignment.get(field)):
                errors.append(f"{field} must contain unique non-empty items")
        forbidden = assignment.get("forbidden_scope")
        if not isinstance(forbidden, list) or any(not isinstance(item, str) or not item.strip() for item in forbidden) or len(set(forbidden or [])) != len(forbidden or []):
            errors.append("forbidden_scope must be a list")
        parallelizable = assignment.get("parallelizable")
        if not isinstance(parallelizable, bool):
            errors.append("parallelizable must be true or false")
        elif parallelizable is False and not str(assignment.get("dependency_reason", "")).strip():
            errors.append("non-parallel assignment requires dependency_reason")

    observed = assignment.get("observed_modified_files", [])
    if not isinstance(observed, list) or any(
        not isinstance(item, str) or not item.strip() for item in observed
    ):
        errors.append("observed_modified_files must be a list of non-empty paths")
        observed = []
    elif len(set(observed)) != len(observed):
        errors.append("observed_modified_files must contain unique paths")

    ack = assignment.get("ack")
    complete_ack = isinstance(ack, dict)
    if complete_ack:
        for field in ACK_FIELDS:
            value = ack.get(field)
            if field == "owned_files":
                if not nonempty_list(value):
                    errors.append("ack.owned_files must contain unique non-empty paths")
                    complete_ack = False
            elif not isinstance(value, str) or not value.strip():
                errors.append(f"ack.{field} is required")
                complete_ack = False
    elif state in {"ACKED", "ACTIVE", "CANDIDATE"}:
        errors.append(f"{state} requires a complete delivered ACK")

    if state == "RESERVED" and observed:
        errors.append("RESERVED assignment cannot modify files before delivered ACK")
    if state in {"ACKED", "ACTIVE", "CANDIDATE"} and not complete_ack:
        if not any("complete delivered ACK" in error for error in errors):
            errors.append(f"{state} requires a complete delivered ACK")
    if complete_ack:
        unexpected = sorted(set(observed) - set(ack["owned_files"]))
        if unexpected:
            errors.append("modified files exceed assignment ownership: " + ", ".join(unexpected))

    if expected_assignment_id is not None and assignment_id != expected_assignment_id:
        errors.append("assignment_id does not match launch contract")
    if expected_task_id is not None and str(assignment.get("task_id", "")).strip() != expected_task_id:
        errors.append("task_id does not match launch contract")
    if expected_agent_id is not None and agent_id != expected_agent_id:
        errors.append("agent_id does not match launch contract")
    if isinstance(ack, dict):
        if expected_repository_root is not None:
            actual_root = str(Path(str(ack.get("repository_root", ""))).expanduser().resolve())
            launch_root = str(Path(expected_repository_root).expanduser().resolve())
            if actual_root != launch_root:
                errors.append("ack.repository_root does not match launch repository")
        if expected_branch is not None and str(ack.get("branch", "")).strip() != expected_branch:
            errors.append("ack.branch does not match launch branch")
        if expected_head is not None and str(ack.get("head", "")).strip() != expected_head:
            errors.append("ack.head does not match launch revision")

    previous = assignment.get("previous_assignment")
    if previous is not None:
        if not isinstance(previous, dict):
            errors.append("previous_assignment must be an object")
        else:
            previous_state = str(previous.get("state", "")).strip().upper()
            if previous_state not in {"FROZEN", "TERMINAL"}:
                errors.append("reused agent requires previous assignment to be FROZEN or TERMINAL")
            if previous.get("files_released") is not True:
                errors.append("reused agent requires previous files_released=true")
            if previous.get("worktree_released") is not True:
                errors.append("reused agent requires previous worktree_released=true")

    revision = str(assignment.get("candidate_revision", "")).strip()
    if state == "CANDIDATE" and not revision:
        errors.append("CANDIDATE requires candidate_revision")
    reviewer_revision = str(assignment.get("reviewer_for_revision", "")).strip()
    if str(assignment.get("role", "")).strip().lower() == "writer" and revision and reviewer_revision == revision:
        errors.append("writer is not non-author reviewer for the same candidate revision")
    if state == "FROZEN" and observed and not str(assignment.get("recovery_owner", "")).strip():
        errors.append("dirty FROZEN assignment requires one recovery_owner")

    if runtime_state is not None and state == "ACTIVE":
        lease = runtime_state.get("leases", {}).get(assignment_id) if isinstance(runtime_state, dict) else None
        if not lease:
            errors.append("ACTIVE requires current runtime lease")
        else:
            for field in ("assignment_id", "task_id", "agent_id", "worktree"):
                expected = str(assignment.get(field, "")).strip()
                if expected and str(lease.get(field, "")).strip() != expected:
                    errors.append(f"ACTIVE runtime lease identity mismatch: {field}")
            decision = evaluate_lease(lease, now=now)
            if decision["state"] not in {"healthy", "progress_stale"}:
                errors.append(f"ACTIVE runtime lease is {decision['state']}: {decision['reason']}")
    return errors


def load_json(path: str) -> dict[str, Any]:
    if path == "-":
        data = json.load(sys.stdin)
    else:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("assignment must be a JSON object")
    return data


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate one ephemeral Agent assignment lease before writes or reuse."
    )
    parser.add_argument("assignment", nargs="?", default="-", help="JSON file or stdin")
    parser.add_argument("--expected-assignment-id")
    parser.add_argument("--expected-task-id")
    parser.add_argument("--expected-agent-id")
    parser.add_argument("--expected-repository-root")
    parser.add_argument("--expected-branch")
    parser.add_argument("--expected-head")
    args = parser.parse_args(argv)
    try:
        assignment = load_json(args.assignment)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"assignment-lease: invalid input: {error}")
        return 2
    errors = validate_assignment(
        assignment,
        expected_assignment_id=args.expected_assignment_id,
        expected_task_id=args.expected_task_id,
        expected_agent_id=args.expected_agent_id,
        expected_repository_root=args.expected_repository_root,
        expected_branch=args.expected_branch,
        expected_head=args.expected_head,
    )
    for error in errors:
        print(f"assignment-lease: blocked: {error}")
    if errors:
        return 1
    print("assignment-lease: allowed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
