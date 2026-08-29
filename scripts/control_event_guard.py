#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

from lint_governance import task_rows

DECISIONS = {"active", "deferred", "blocked"}
CANDIDATE_DECISIONS = {
    "review",
    "integrate",
    "rework",
    "queued",
    "blocked",
    "superseded",
    "absorbed",
    "parked",
}
RETAINED_CANDIDATE_DECISIONS = {"absorbed", "parked"}
DEFER_REASON_CODES = {
    "capacity",
    "file_conflict",
    "shared_environment",
    "ordered_integration",
    "external_blocker",
    "authorization",
}
HARD_DEFER_REASON_CODES = DEFER_REASON_CODES - {"capacity"}


def ready_ledger_package_ids(ledger: Path) -> set[str]:
    return {
        identifier
        for identifier, status in task_rows(ledger.read_text(encoding="utf-8"))
        if status == "READY"
    }


def open_ledger_package_ids(ledger: Path) -> set[str]:
    return {
        identifier
        for identifier, status in task_rows(ledger.read_text(encoding="utf-8"))
        if status in {"PENDING", "READY", "ACTIVE", "RECOVERING", "VERIFY", "BLOCKED"}
    }


def work_in_flight_ledger_packages(ledger: Path) -> dict[str, str]:
    return {
        identifier: status
        for identifier, status in task_rows(ledger.read_text(encoding="utf-8"))
        if status in {"ACTIVE", "RECOVERING"}
    }


def current_goal_ledger_ids(ledger: Path, open_ids: set[str] | None = None) -> set[str]:
    text = ledger.read_text(encoding="utf-8")
    match = re.search(r"^- 当前 Goal：\s*(.+?)\s*$", text, re.MULTILINE)
    if not match:
        return set()
    identifiers = open_ids if open_ids is not None else open_ledger_package_ids(ledger)
    value = match.group(1)
    return {
        identifier
        for identifier in identifiers
        if re.search(
            rf"(?<![A-Za-z0-9_-]){re.escape(identifier)}(?![A-Za-z0-9_-])",
            value,
        )
    }


GOAL_WORD = re.compile(r"(?:\bgoal\b|里程碑|当前\s*Goal|目标)", re.I)
GOAL_CLOSE_WORD = re.compile(r"(?:close(?:d|ure)?|complete(?:d)?|done|结束|完成|闭合|关闭|收口)", re.I)


def event_closes_goal(snapshot: dict[str, Any]) -> bool:
    contract = snapshot.get("event_contract")
    chunks: list[str] = []
    if isinstance(contract, dict):
        for field in ("event_type", "primary_task", "terminal_receipt"):
            value = contract.get(field)
            if isinstance(value, str):
                chunks.append(value)
    actions = snapshot.get("event_actions")
    if isinstance(actions, list):
        for action in actions:
            if not isinstance(action, dict):
                continue
            value = action.get("action")
            if isinstance(value, str):
                chunks.append(value.replace("_", " ").replace("-", " "))
    text = " ".join(chunks)
    return bool(GOAL_WORD.search(text) and GOAL_CLOSE_WORD.search(text))


def validate_goal_rollover(
    snapshot: dict[str, Any],
    *,
    ledger_open_ids: set[str] | None,
    ledger_goal_ids: set[str] | None,
) -> list[str]:
    if not event_closes_goal(snapshot):
        return []
    errors: list[str] = []
    rollover = snapshot.get("goal_rollover")
    if not isinstance(rollover, dict):
        return [
            "goal_rollover is required when the event closes a Goal; recompute the project and roll to the next Goal or prove project-wide blocking"
        ]
    status = str(rollover.get("status", "")).strip().lower()
    if status not in {"rolled", "project_blocked", "project_complete"}:
        errors.append("goal_rollover.status must be rolled, project_blocked, or project_complete")
    if rollover.get("project_recomputed") is not True:
        errors.append("goal_rollover.project_recomputed=true is required")
    closed_goal_id = str(rollover.get("closed_goal_id", "")).strip()
    if not closed_goal_id:
        errors.append("goal_rollover.closed_goal_id is required")
    contract = snapshot.get("event_contract")
    contract_text = json.dumps(contract, ensure_ascii=False) if isinstance(contract, dict) else ""
    if closed_goal_id and closed_goal_id not in contract_text:
        errors.append("goal_rollover.closed_goal_id must match the closing event contract")

    open_ids = ledger_open_ids or set()
    goal_ids = ledger_goal_ids or set()
    if status == "rolled":
        current_goal_id = str(rollover.get("current_goal_id", "")).strip()
        if not current_goal_id:
            errors.append("goal_rollover.current_goal_id is required for rolled status")
        elif current_goal_id == closed_goal_id:
            errors.append("goal_rollover must move to a different current Goal after closure")
        elif current_goal_id not in open_ids:
            errors.append("goal_rollover.current_goal_id must be an open ledger package")
        if current_goal_id and current_goal_id not in goal_ids:
            errors.append("goal_rollover.current_goal_id must match the ledger current Goal")
    elif status == "project_blocked":
        blocked_scan = rollover.get("blocked_scan")
        if not isinstance(blocked_scan, dict):
            errors.append("goal_rollover.blocked_scan is required for project_blocked status")
        else:
            from preblock_guard import validate_snapshot as validate_preblock

            errors.extend(
                "goal_rollover blocked scan: " + error
                for error in validate_preblock(blocked_scan, ledger_package_ids=open_ids)
            )
    elif status == "project_complete":
        if open_ids:
            errors.append(
                "goal_rollover project_complete requires no open ledger packages: "
                + ", ".join(sorted(open_ids))
            )
    return errors


def ledger_sha256(ledger: Path) -> str:
    return hashlib.sha256(ledger.read_bytes()).hexdigest()


def run_git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=check,
        capture_output=True,
        text=True,
    )


CANDIDATE_STATE_ROOT = Path(
    os.environ.get(
        "AD_CANDIDATE_STATE_DIR",
        str(Path.home() / ".codex" / "state" / "adaptive-delivery-candidates"),
    )
).expanduser()


def candidate_state_path(root: Path, state_dir: Path | None = None) -> Path:
    canonical = Path(run_git(root, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
    key = hashlib.sha256(str(canonical).encode("utf-8")).hexdigest()[:24]
    return (state_dir or CANDIDATE_STATE_ROOT) / f"{key}.json"


def load_candidate_lifecycle(root: Path, state_dir: Path | None = None) -> dict[str, Any]:
    path = candidate_state_path(root, state_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema_version": 1, "worktrees": {}}
    if not isinstance(data, dict) or not isinstance(data.get("worktrees"), dict):
        return {"schema_version": 1, "worktrees": {}}
    return data


def write_candidate_lifecycle(
    root: Path, value: dict[str, Any], state_dir: Path | None = None
) -> None:
    path = candidate_state_path(root, state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def record_candidate_lifecycle(
    root: Path, candidate_packages: Sequence[dict[str, Any]], *, state_dir: Path | None = None
) -> None:
    lifecycle = load_candidate_lifecycle(root, state_dir)
    worktrees = lifecycle.setdefault("worktrees", {})
    if not isinstance(worktrees, dict):
        worktrees = {}
        lifecycle["worktrees"] = worktrees
    for candidate in candidate_packages:
        decision = str(candidate.get("decision", "")).strip().lower()
        if decision not in RETAINED_CANDIDATE_DECISIONS:
            continue
        worktree = str(Path(str(candidate.get("worktree", ""))).expanduser().resolve())
        revision = str(candidate.get("revision", "")).strip()
        record: dict[str, Any] = {
            "revision": revision,
            "state": decision,
            "retention_reason": str(candidate.get("retention_reason", "")).strip(),
        }
        if decision == "absorbed":
            record["absorbing_revision"] = str(candidate.get("absorbing_revision", "")).strip()
        else:
            record["reason_code"] = str(candidate.get("reason_code", "")).strip()
            record["wake_condition"] = str(candidate.get("wake_condition", "")).strip()
        worktrees[worktree] = record
    write_candidate_lifecycle(root, lifecycle, state_dir)


def worktree_candidate_inventory(
    root: Path, *, state_dir: Path | None = None
) -> dict[str, dict[str, Any]]:
    """Return live candidates plus retained terminal worktrees with exact-revision matching."""
    canonical = Path(
        run_git(root, "rev-parse", "--show-toplevel").stdout.strip()
    ).resolve()
    main_revision = run_git(canonical, "rev-parse", "main").stdout.strip()
    lifecycle = load_candidate_lifecycle(canonical, state_dir)
    retained_records = lifecycle.get("worktrees", {})
    if not isinstance(retained_records, dict):
        retained_records = {}
    porcelain = run_git(canonical, "worktree", "list", "--porcelain").stdout
    live: dict[str, Any] = {}
    retained: dict[str, Any] = {}
    path: Path | None = None
    revision = ""
    for line in porcelain.splitlines() + [""]:
        if line.startswith("worktree "):
            path = Path(line.removeprefix("worktree ")).resolve()
            revision = ""
        elif line.startswith("HEAD "):
            revision = line.removeprefix("HEAD ").strip()
        elif not line and path is not None and revision:
            if path != canonical:
                merged = run_git(
                    canonical,
                    "merge-base",
                    "--is-ancestor",
                    revision,
                    main_revision,
                    check=False,
                ).returncode == 0
                if not merged:
                    key = str(path)
                    record = retained_records.get(key)
                    if (
                        isinstance(record, dict)
                        and record.get("revision") == revision
                        and record.get("state") in RETAINED_CANDIDATE_DECISIONS
                    ):
                        retained[key] = dict(record)
                    else:
                        live[key] = revision
            path = None
            revision = ""
    return {"live": live, "retained": retained}


def unmerged_worktree_candidates(
    root: Path, *, state_dir: Path | None = None
) -> dict[str, str]:
    """Return only live unmerged worktree candidates; retained terminal states are excluded."""
    return dict(worktree_candidate_inventory(root, state_dir=state_dir)["live"])


def integrated_candidate_revisions(
    root: Path, snapshot: dict[str, Any], main_revision: str
) -> set[str]:
    merged: set[str] = set()
    raw = snapshot.get("candidate_packages", [])
    if not isinstance(raw, list):
        return merged
    for candidate in raw:
        if not isinstance(candidate, dict):
            continue
        if str(candidate.get("decision", "")).strip().lower() != "integrate":
            continue
        revision = str(candidate.get("revision", "")).strip()
        if not revision:
            continue
        result = run_git(
            root,
            "merge-base",
            "--is-ancestor",
            revision,
            main_revision,
            check=False,
        )
        if result.returncode == 0:
            merged.add(revision)
    return merged


def validate_candidate_queue(
    snapshot: dict[str, Any],
    *,
    expected_candidates: dict[str, str],
    expected_main_revision: str | None = None,
    expected_integrated_revisions: set[str] | None = None,
) -> list[str]:
    errors: list[str] = []
    raw_candidates = snapshot.get("candidate_packages")
    if not isinstance(raw_candidates, list):
        return ["candidate_packages must enumerate every unmerged worktree candidate"]

    seen: set[str] = set()
    integrated_transitions: set[str] = set()
    flow_counts: dict[str, int] = {}
    candidate_flows: set[str] = set()
    for index, candidate in enumerate(raw_candidates):
        if not isinstance(candidate, dict):
            errors.append(f"candidate_packages[{index}] must be an object")
            continue
        revision = str(candidate.get("revision", "")).strip()
        worktree = str(candidate.get("worktree", "")).strip()
        task_id = str(candidate.get("task_id", "")).strip()
        flow = str(candidate.get("integration_flow", "")).strip()
        decision = str(candidate.get("decision", "")).strip().lower()
        if not revision:
            errors.append(f"candidate_packages[{index}].revision is required")
            continue
        if revision in seen:
            errors.append(f"duplicate candidate revision: {revision}")
        seen.add(revision)
        integrated_transition = decision == "integrate" and candidate.get("integrated_this_event") is True
        if integrated_transition:
            integrated_transitions.add(revision)
        if expected_candidates.get(worktree) != revision and not integrated_transition:
            errors.append(f"{revision} worktree does not match the live worktree")
        if not task_id:
            errors.append(f"{revision} requires task_id")
        if not flow:
            errors.append(f"{revision} requires integration_flow")
        elif decision not in RETAINED_CANDIDATE_DECISIONS and not integrated_transition:
            candidate_flows.add(flow)
            flow_counts[flow] = flow_counts.get(flow, 0) + 1
        if decision not in CANDIDATE_DECISIONS:
            errors.append(
                f"{revision} decision must be review, integrate, rework, queued, blocked, superseded, absorbed, or parked"
            )
            continue
        if decision == "review":
            if not str(candidate.get("review_task_id", "")).strip():
                errors.append(f"{revision} review requires review_task_id")
            if candidate.get("delivered_ack") is not True:
                errors.append(f"{revision} review requires delivered_ack=true")
        elif decision == "integrate":
            if not str(candidate.get("controller_event_id", "")).strip():
                errors.append(f"{revision} integrate requires controller_event_id")
            if candidate.get("integrated_this_event") is not True:
                errors.append(f"{revision} integrate requires integrated_this_event=true")
            main_revision = str(candidate.get("main_revision", "")).strip()
            if not main_revision:
                errors.append(f"{revision} integrate requires main_revision")
            elif expected_main_revision is not None and main_revision != expected_main_revision:
                errors.append(f"{revision} main_revision does not match current main")
            if expected_main_revision is not None and (
                expected_integrated_revisions is None or revision not in expected_integrated_revisions
            ):
                errors.append(
                    f"{revision} integrate requires candidate revision to be an ancestor of current main"
                )
            if not str(candidate.get("regression_evidence", "")).strip():
                errors.append(f"{revision} integrate requires regression_evidence")
        elif decision == "rework":
            if not str(candidate.get("writer_task_id", "")).strip():
                errors.append(f"{revision} rework requires writer_task_id")
            if candidate.get("delivered_ack") is not True:
                errors.append(f"{revision} rework requires delivered_ack=true")
        elif decision == "queued":
            if str(candidate.get("reason_code", "")).strip().lower() not in {
                "capacity",
                "ordered_integration",
            }:
                errors.append(
                    f"{revision} queued requires reason_code capacity or ordered_integration"
                )
            if not str(candidate.get("next_checkpoint", "")).strip():
                errors.append(f"{revision} queued requires next_checkpoint")
        elif decision == "blocked":
            if str(candidate.get("reason_code", "")).strip().lower() not in {
                "shared_environment",
                "external_blocker",
                "authorization",
            }:
                errors.append(f"{revision} blocked requires a hard reason_code")
            if not str(candidate.get("wake_condition", "")).strip():
                errors.append(f"{revision} blocked requires wake_condition")
        elif decision == "superseded":
            if not str(candidate.get("superseding_revision", "")).strip():
                errors.append(f"{revision} superseded requires superseding_revision")
            if not str(candidate.get("cleanup_action", "")).strip():
                errors.append(f"{revision} superseded requires cleanup_action")
        elif decision == "absorbed":
            if not str(candidate.get("absorbing_revision", "")).strip():
                errors.append(f"{revision} absorbed requires absorbing_revision")
            if not str(candidate.get("retention_reason", "")).strip():
                errors.append(f"{revision} absorbed requires retention_reason")
        elif decision == "parked":
            if not str(candidate.get("reason_code", "")).strip():
                errors.append(f"{revision} parked requires reason_code")
            if not str(candidate.get("wake_condition", "")).strip():
                errors.append(f"{revision} parked requires wake_condition")
            if not str(candidate.get("retention_reason", "")).strip():
                errors.append(f"{revision} parked requires retention_reason")

    expected_revisions = set(expected_candidates.values())
    missing = sorted(expected_revisions - seen)
    extra = sorted(seen - expected_revisions - integrated_transitions)
    if missing:
        errors.append("control event omitted unmerged candidates: " + ", ".join(missing))
    if extra:
        errors.append("control event contains non-live candidates: " + ", ".join(extra))
    for flow, count in sorted(flow_counts.items()):
        if count > 1:
            errors.append(f"integration flow {flow} exceeds candidate WIP limit 1")

    assignments = snapshot.get("new_assignments")
    if not isinstance(assignments, list):
        errors.append("new_assignments must be a list")
        assignments = []
    for index, assignment in enumerate(assignments):
        if not isinstance(assignment, dict):
            errors.append(f"new_assignments[{index}] must be an object")
            continue
        task_id = str(assignment.get("task_id", "")).strip()
        flow = str(assignment.get("integration_flow", "")).strip()
        if not task_id or not flow:
            errors.append(f"new_assignments[{index}] requires task_id and integration_flow")
        elif flow in candidate_flows:
            errors.append(
                f"{task_id} cannot start: integration flow {flow} already has an unmerged candidate"
            )
    return errors


def validate_review_transitions(
    snapshot: dict[str, Any], *, expected_main_revision: str | None = None
) -> list[str]:
    errors: list[str] = []
    candidates = snapshot.get("candidate_packages")
    if not isinstance(candidates, list):
        return errors
    candidate_by_revision = {
        str(candidate.get("revision", "")).strip(): candidate
        for candidate in candidates
        if isinstance(candidate, dict) and str(candidate.get("revision", "")).strip()
    }
    reviews = snapshot.get("required_reviews", [])
    if not isinstance(reviews, list):
        return errors
    for review in reviews:
        if not isinstance(review, dict):
            continue
        verdict = str(review.get("verdict", "")).strip().upper()
        if not verdict:
            continue
        review_id = str(review.get("id", "")).strip() or "unnamed"
        revision = str(review.get("candidate_revision", "")).strip()
        if verdict not in {"PASS", "FAIL"}:
            errors.append(f"required review {review_id} verdict must be PASS or FAIL")
            continue
        if not revision:
            errors.append(f"required review {review_id} verdict requires candidate_revision")
            continue
        candidate = candidate_by_revision.get(revision)
        if candidate is None:
            errors.append(f"review {verdict} for {revision} requires candidate transition evidence")
            continue
        decision = str(candidate.get("decision", "")).strip().lower()
        if verdict == "PASS":
            ordered = (
                decision == "queued"
                and str(candidate.get("reason_code", "")).strip().lower() == "ordered_integration"
                and bool(str(candidate.get("next_checkpoint", "")).strip())
            )
            integrated = decision == "integrate" and candidate.get("integrated_this_event") is True
            if not (ordered or integrated):
                errors.append(
                    f"review PASS for {revision} requires completed integration or ordered integration queue"
                )
        else:
            rework = (
                decision == "rework"
                and bool(str(candidate.get("writer_task_id", "")).strip())
                and candidate.get("delivered_ack") is True
            )
            if not rework:
                errors.append(f"review FAIL for {revision} requires rework disposition")
    return errors


def string_set(value: Any, field: str, errors: list[str]) -> set[str]:
    if not isinstance(value, list):
        errors.append(f"{field} must be a list")
        return set()
    result = {str(item).strip() for item in value if str(item).strip()}
    if len(result) != len(value):
        errors.append(f"{field} must contain unique non-empty task IDs")
    return result


def validate_snapshot(
    snapshot: dict[str, Any],
    *,
    ledger_ready_ids: set[str] | None = None,
    expected_ledger_sha256: str | None = None,
    required_review_ids: set[str] | None = None,
    expected_rule_revision: str | None = None,
    affected_task_ids: set[str] | None = None,
    expected_candidates: dict[str, str] | None = None,
    expected_main_revision: str | None = None,
    expected_integrated_revisions: set[str] | None = None,
    ledger_open_ids: set[str] | None = None,
    ledger_goal_ids: set[str] | None = None,
    ledger_work_in_flight: dict[str, str] | None = None,
) -> list[str]:
    errors: list[str] = []
    contract = snapshot.get("event_contract")
    actions = snapshot.get("event_actions")
    if not isinstance(contract, dict):
        errors.append("event_contract is required")
    if not isinstance(actions, list) or not actions:
        errors.append("event_actions must be a non-empty list")
    elif isinstance(contract, dict):
        from event_scope_guard import classify_append

        for index, action in enumerate(actions):
            if not isinstance(action, dict):
                errors.append(f"event_actions[{index}] must be an object")
                continue
            decision, reasons = classify_append(contract, action)
            if decision != "SAME_EVENT":
                errors.append(
                    f"event_actions[{index}] is {decision}: " + "; ".join(reasons)
                )
    if snapshot.get("terminal_receipt_issued") is not True:
        errors.append("terminal_receipt_issued=true is required")

    liveness = snapshot.get("assignment_liveness", {})
    if liveness is not None and not isinstance(liveness, dict):
        errors.append("assignment_liveness must be an object")
    elif isinstance(liveness, dict):
        for task_id, decision in sorted(liveness.items()):
            if not isinstance(decision, dict):
                errors.append(f"assignment_liveness[{task_id}] must be an object")
                continue
            ledger_state = str(decision.get("ledger_state", "")).upper()
            runtime_state = str(decision.get("state", ""))
            reason = str(decision.get("reason", "")).strip() or "unknown"
            if ledger_state == "ACTIVE" and runtime_state not in {"healthy", "progress_stale"}:
                errors.append(f"ACTIVE runtime unhealthy: {task_id} ({reason})")
            if ledger_state == "RECOVERING" and runtime_state in {"unhealthy", "unknown", "terminal"}:
                errors.append(f"RECOVERING runtime stalled: {task_id} ({reason})")
        if ledger_work_in_flight is not None:
            expected_ids = set(ledger_work_in_flight)
            reported_ids = set(liveness)
            for task_id in sorted(expected_ids - reported_ids):
                errors.append(
                    f"assignment_liveness omitted {ledger_work_in_flight[task_id]} task: {task_id}"
                )
            for task_id in sorted(reported_ids - expected_ids):
                errors.append(f"assignment_liveness contains non-work-in-flight task: {task_id}")
            for task_id in sorted(expected_ids & reported_ids):
                decision = liveness.get(task_id)
                if not isinstance(decision, dict):
                    continue
                reported_state = str(decision.get("ledger_state", "")).upper()
                expected_state = ledger_work_in_flight[task_id]
                if reported_state != expected_state:
                    errors.append(
                        f"assignment_liveness ledger_state mismatch for {task_id}: "
                        f"expected {expected_state}, got {reported_state or 'missing'}"
                    )

    snapshot_sha = str(snapshot.get("ledger_sha256", "")).strip()
    if not snapshot_sha:
        errors.append("ledger_sha256 is required")
    elif expected_ledger_sha256 is not None and snapshot_sha != expected_ledger_sha256:
        errors.append("ledger_sha256 does not match the current ledger")
    slots = snapshot.get("available_slots")
    if not isinstance(slots, int) or slots < 0:
        errors.append("available_slots must be a non-negative integer")

    raw_ready = snapshot.get("ready_packages")
    if not isinstance(raw_ready, list):
        errors.append("ready_packages must enumerate every READY ledger package")
        raw_ready = []
    snapshot_ready_ids: set[str] = set()
    active_decisions = 0
    deferred_without_hard_constraint: list[str] = []
    for index, package in enumerate(raw_ready):
        if not isinstance(package, dict):
            errors.append(f"ready_packages[{index}] must be an object")
            continue
        package_id = str(package.get("id", "")).strip()
        if not package_id:
            errors.append(f"ready_packages[{index}].id is required")
            continue
        if package_id in snapshot_ready_ids:
            errors.append(f"duplicate READY package: {package_id}")
        snapshot_ready_ids.add(package_id)
        decision = str(package.get("decision", "")).strip().lower()
        if decision not in DECISIONS:
            errors.append(f"{package_id} decision must be active, deferred, or blocked")
            continue
        if decision == "active":
            active_decisions += 1
            if not str(package.get("task_id", "")).strip():
                errors.append(f"{package_id} active decision requires task_id")
            if package.get("delivered_ack") is not True:
                errors.append(f"{package_id} active decision requires delivered_ack=true")
        else:
            if not str(package.get("reason", "")).strip():
                errors.append(f"{package_id} {decision} decision requires an exact reason")
            reason_code = str(package.get("reason_code", "")).strip().lower()
            if reason_code not in DEFER_REASON_CODES:
                errors.append(
                    f"{package_id} {decision} decision requires reason_code: "
                    + ", ".join(sorted(DEFER_REASON_CODES))
                )
            if reason_code not in HARD_DEFER_REASON_CODES:
                deferred_without_hard_constraint.append(package_id)

    if isinstance(slots, int) and slots > active_decisions and deferred_without_hard_constraint:
        errors.append(
            "idle dispatch capacity remains for READY packages without a hard constraint: "
            + ", ".join(sorted(deferred_without_hard_constraint))
        )

    if ledger_ready_ids is not None:
        missing = sorted(ledger_ready_ids - snapshot_ready_ids)
        extra = sorted(snapshot_ready_ids - ledger_ready_ids)
        if missing:
            errors.append("control event omitted READY packages: " + ", ".join(missing))
        if extra:
            errors.append("control event contains non-READY packages: " + ", ".join(extra))

    raw_reviews = snapshot.get("required_reviews", [])
    if not isinstance(raw_reviews, list):
        errors.append("required_reviews must be a list")
        raw_reviews = []
    snapshot_review_ids: set[str] = set()
    for index, review in enumerate(raw_reviews):
        if not isinstance(review, dict):
            errors.append(f"required_reviews[{index}] must be an object")
            continue
        review_id = str(review.get("id", "")).strip() or f"index {index}"
        snapshot_review_ids.add(review_id)
        if not str(review.get("task_id", "")).strip():
            errors.append(f"required review {review_id} requires task_id")
        if review.get("delivered_ack") is not True:
            errors.append(f"required review {review_id} requires delivered_ack=true")
        if review.get("tdd_required") is True:
            for field in ("red_evidence", "candidate_revision", "green_evidence", "reviewer_counterexample"):
                if not str(review.get(field, "")).strip():
                    errors.append(f"required review {review_id} requires {field}")
            if review.get("red_green_same_case") is not True:
                errors.append(f"required review {review_id} requires red_green_same_case=true")
            if str(review.get("verdict", "")).strip().upper() not in {"PASS", "FAIL"}:
                errors.append(f"required review {review_id} requires verdict PASS or FAIL")
    if required_review_ids is not None:
        missing = sorted(required_review_ids - snapshot_review_ids)
        extra = sorted(snapshot_review_ids - required_review_ids)
        if missing:
            errors.append("control event omitted required reviews: " + ", ".join(missing))
        if extra:
            errors.append("control event contains undeclared reviews: " + ", ".join(extra))

    update = snapshot.get("rule_update")
    if expected_rule_revision is not None and update is None:
        errors.append("control event omitted the declared rule update")
    if update is not None:
        if not isinstance(update, dict):
            errors.append("rule_update must be an object")
        else:
            revision = str(update.get("revision", "")).strip()
            if not revision:
                errors.append("rule_update.revision is required")
            elif expected_rule_revision is not None and revision != expected_rule_revision:
                errors.append("rule_update.revision does not match the declared revision")
            affected = string_set(update.get("affected_tasks"), "rule_update.affected_tasks", errors)
            acknowledged = string_set(
                update.get("acknowledged_tasks"),
                "rule_update.acknowledged_tasks",
                errors,
            )
            missing = sorted(affected - acknowledged)
            extra = sorted(acknowledged - affected)
            if missing:
                errors.append("rule update missing loaded ACK: " + ", ".join(missing))
            if extra:
                errors.append("rule update ACK contains unaffected tasks: " + ", ".join(extra))
            if affected_task_ids is not None and affected != affected_task_ids:
                errors.append("rule_update.affected_tasks does not match declared affected tasks")
    if expected_candidates is not None:
        errors.extend(
            validate_candidate_queue(
                snapshot,
                expected_candidates=expected_candidates,
                expected_main_revision=expected_main_revision,
                expected_integrated_revisions=expected_integrated_revisions,
            )
        )
    errors.extend(validate_review_transitions(snapshot, expected_main_revision=expected_main_revision))
    errors.extend(
        validate_goal_rollover(
            snapshot, ledger_open_ids=ledger_open_ids, ledger_goal_ids=ledger_goal_ids
        )
    )
    return errors


def canonical_rule_handshake_errors(repo: Path, ledger: Path) -> list[str]:
    try:
        try:
            from rule_handshake import evaluate_rule_handshake
        except ModuleNotFoundError:
            from scripts.rule_handshake import evaluate_rule_handshake
        status = evaluate_rule_handshake(repo, ledger=ledger)
    except (OSError, ValueError) as error:
        return [f"rule handshake integrity check failed: {error}"]
    if status.get("blocking") is True:
        revision = status.get("installed_revision") or "unknown"
        return [f"rule handshake {status.get('state')} for installed revision {revision}"]
    return []


def load_snapshot(path: str) -> dict[str, Any]:
    if path == "-":
        data = json.load(sys.stdin)
    else:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("snapshot must be a JSON object")
    return data


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Validate one ephemeral controller event without adding another project document."
        )
    )
    parser.add_argument("snapshot", nargs="?", default="-", help="JSON file or stdin")
    parser.add_argument("--ledger", required=True, help="unique task ledger path")
    parser.add_argument(
        "--require-review",
        action="append",
        default=[],
        help="required reviewer ID; repeat for every reviewer required by this event",
    )
    parser.add_argument("--rule-revision", help="rule revision applied in this event")
    parser.add_argument(
        "--repo",
        help="canonical repository; when set, every unmerged worktree candidate must receive a decision",
    )
    parser.add_argument(
        "--affected-task",
        action="append",
        default=[],
        help="live task affected by --rule-revision; repeat as needed",
    )
    args = parser.parse_args(argv)
    try:
        snapshot = load_snapshot(args.snapshot)
        ledger = Path(args.ledger).resolve()
        if not ledger.is_file():
            raise ValueError("ledger path must be an existing file")
        ready_ids = ready_ledger_package_ids(ledger)
        open_ids = open_ledger_package_ids(ledger)
        goal_ids = current_goal_ledger_ids(ledger, open_ids)
        work_in_flight = work_in_flight_ledger_packages(ledger)
        current_ledger_sha256 = ledger_sha256(ledger)
        if args.affected_task and not args.rule_revision:
            raise ValueError("--affected-task requires --rule-revision")
        repo_root = Path(args.repo).expanduser().resolve() if args.repo else None
        candidates = unmerged_worktree_candidates(repo_root) if repo_root else None
        main_revision = run_git(repo_root, "rev-parse", "main").stdout.strip() if repo_root else None
        integrated_revisions = (
            integrated_candidate_revisions(repo_root, snapshot, main_revision)
            if repo_root and main_revision
            else None
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"control-event: invalid snapshot: {error}")
        return 2

    errors = validate_snapshot(
        snapshot,
        ledger_ready_ids=ready_ids,
        expected_ledger_sha256=current_ledger_sha256,
        required_review_ids=set(args.require_review),
        expected_rule_revision=args.rule_revision,
        affected_task_ids=set(args.affected_task) if args.rule_revision else None,
        expected_candidates=candidates,
        expected_main_revision=main_revision,
        expected_integrated_revisions=integrated_revisions,
        ledger_open_ids=open_ids,
        ledger_goal_ids=goal_ids,
        ledger_work_in_flight=work_in_flight,
    )
    if repo_root is not None:
        errors.extend(canonical_rule_handshake_errors(repo_root, ledger))
    from ledger_consistency_guard import validate_ledger

    errors.extend(
        f"ledger consistency: {error}"
        for error in validate_ledger(ledger.read_text(encoding="utf-8"))
    )
    for error in errors:
        print(f"control-event: blocked: {error}")
    if errors:
        return 1
    if args.repo:
        raw_candidates = snapshot.get("candidate_packages", [])
        if isinstance(raw_candidates, list):
            try:
                record_candidate_lifecycle(Path(args.repo).expanduser().resolve(), [c for c in raw_candidates if isinstance(c, dict)])
            except OSError as error:
                print(f"control-event: blocked: failed to persist candidate lifecycle: {error}")
                return 1
    print(
        "control-event: allowed; declared READY, candidate, review and rule ACK decisions are complete"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
