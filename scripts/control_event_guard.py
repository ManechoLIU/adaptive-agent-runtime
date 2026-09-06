#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

try:
    from scripts.lint_governance import task_records, task_rows
except ModuleNotFoundError:
    from lint_governance import task_records, task_rows
try:
    from scripts.controller_state import derive_runnable_tasks
except ModuleNotFoundError:
    from controller_state import derive_runnable_tasks
try:
    from scripts.route_contract import (
        ROUTE_DECISIONS,
        canonical_safe_fallback_errors,
        delegated_route_contract_errors,
        route_policy_errors,
        route_scope_errors,
    )
except ModuleNotFoundError:
    from route_contract import (
        ROUTE_DECISIONS,
        canonical_safe_fallback_errors,
        delegated_route_contract_errors,
        route_policy_errors,
        route_scope_errors,
    )

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
EXECUTION_MODES = {"delegated", "controller"}
CONTROLLER_EXCEPTION_REASONS = {
    "shared_contract_unstable",
    "unsafe_to_split",
    "active_wip_recovery",
    "low_risk_tiny_change",
}
CONTROLLER_CYCLE_EVIDENCE_DIRECTORY = "controller-cycle-evidence"
LEDGER_SUCCESS_STATES = {"DONE"}
CONTROL_LOOP_STEPS = (
    "read_project_facts",
    "consume_pending_events",
    "derive_project_runnable",
    "derive_pending_slices",
    "compute_capacity_and_conflicts",
    "decide_every_runnable",
    "dispatch_available_capacity",
    "execute_controller_actions",
    "recompute_after_actions",
)


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


def runtime_occupied_task_ids(repo: Path, work_in_flight: dict[str, str]) -> set[str]:
    """Return ledger WIP tasks that still have a canonical nonterminal Runtime lease."""
    try:
        from scripts.assignment_runtime import load_runtime_state
    except ModuleNotFoundError:
        from assignment_runtime import load_runtime_state

    runtime = load_runtime_state(Path(repo).expanduser().resolve())
    leases = runtime.get("leases", {}) if isinstance(runtime, dict) else {}
    if not isinstance(leases, dict):
        return set()
    occupied: set[str] = set()
    tracked = set(work_in_flight)
    for lease in leases.values():
        if not isinstance(lease, dict) or lease.get("terminal_state"):
            continue
        task_id = str(lease.get("task_id", "")).strip()
        if task_id in tracked:
            occupied.add(task_id)
    return occupied


def project_wide_dispatch_projection(ledger: Path) -> dict[str, Any]:
    """Derive one control-event scheduling view from the entire canonical TASK_LEDGER."""
    text = ledger.read_text(encoding="utf-8")
    records = task_records(text)
    rows = task_rows(text)
    task_states = {identifier: status for identifier, status in rows}
    ready_ids = {identifier for identifier, status in rows if status == "READY"}
    runnable_projection = derive_runnable_tasks(records)
    derived_runnable_ids = {
        str(item).strip()
        for item in runnable_projection.get("runnable_task_ids", [])
        if str(item).strip()
    }
    open_ids = {
        identifier
        for identifier, status in rows
        if status in {"PENDING", "READY", "ACTIVE", "RECOVERING", "VERIFY", "BLOCKED"}
    }
    work_in_flight = {
        identifier: status
        for identifier, status in rows
        if status in {"ACTIVE", "RECOVERING"}
    }
    return {
        "ledger_text": text,
        "records": records,
        "task_states": task_states,
        "ready_ids": ready_ids,
        "derived_runnable_ids": derived_runnable_ids,
        "runnable_exclusions": dict(runnable_projection.get("exclusions", {})),
        "derived_slices": dict(runnable_projection.get("derived_slices", {})),
        "open_ids": open_ids,
        "goal_ids": current_goal_ledger_ids(ledger, open_ids),
        "work_in_flight": work_in_flight,
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


def git_common_dir(root: Path) -> Path:
    common = Path(run_git(root, "rev-parse", "--git-common-dir").stdout.strip())
    return (root / common).resolve() if not common.is_absolute() else common.resolve()


def controller_cycle_evidence_path(root: Path, evidence_id: str) -> Path:
    identifier = str(evidence_id or "").strip()
    if not identifier or len(identifier) > 256:
        raise ValueError("controller cycle evidence id is missing or too long")
    digest = hashlib.sha256(identifier.encode("utf-8")).hexdigest()
    return (
        git_common_dir(Path(root).expanduser().resolve())
        / "adaptive-delivery"
        / CONTROLLER_CYCLE_EVIDENCE_DIRECTORY
        / f"{digest}.json"
    )


DEVIATION_LEVEL_NUMBER = {"L1": 1, "L2": 2, "L3": 3, "L4": 4}


def _controller_cycle_records(root: Path, controller_id: str) -> list[dict[str, Any]]:
    common = git_common_dir(Path(root).expanduser().resolve()) / "adaptive-delivery"
    evidence_dir = common / CONTROLLER_CYCLE_EVIDENCE_DIRECTORY
    records: list[dict[str, Any]] = []
    for path in sorted(evidence_dir.glob("*.json")) if evidence_dir.is_dir() else []:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(value, dict):
            continue
        if str(value.get("controller_id", "")).strip() != str(controller_id).strip():
            continue
        if value.get("record_kind") != "controller_cycle_evidence":
            continue
        records.append(value)
    return sorted(
        records,
        key=lambda item: (
            str(item.get("recorded_at", "")),
            str(item.get("evidence_id", "")),
        ),
    )


def _deviation_rule(error: str) -> tuple[str, int, str, str]:
    text = str(error).strip().lower()
    if "omitted ready packages" in text or "omitted derived runnable packages" in text:
        return (
            "project_runnable_omission", 3, "controller",
            "recompute_project_and_dispatch_all_runnable",
        )
    if "idle dispatch capacity remains" in text:
        return (
            "idle_capacity_underdispatch", 3, "controller",
            "recompute_capacity_and_dispatch_nonconflicting_runnable",
        )
    if "active dispatch decisions exceed available capacity" in text:
        return (
            "capacity_overdispatch", 2, "controller",
            "recompute_capacity_and_remove_excess_dispatch",
        )
    if "continuation debt remains open" in text:
        return (
            "CONTINUATION_DEBT_NOT_CLEARED", 3, "controller",
            "consume_all_mandatory_successors_and_recompute",
        )
    if "fact_projection_drift" in text:
        return (
            "FACT_PROJECTION_DRIFT", 3, "controller",
            "converge_ledger_runtime_git_and_receipts_then_recompute",
        )
    if "integration_not_continued" in text:
        return (
            "INTEGRATION_NOT_CONTINUED", 2, "controller",
            "verify_current_main_converge_facts_and_recompute",
        )
    if "post_integration_recompute_missing" in text:
        return (
            "POST_INTEGRATION_RECOMPUTE_MISSING", 3, "controller",
            "recompute_project_after_integration",
        )
    if "known_next_action" in text:
        return (
            "KNOWN_NEXT_ACTION_NOT_EXECUTED", 2, "controller",
            "execute_or_hard_defer_known_next_action",
        )
    if "required review" in text or "review pass" in text or "review fail" in text:
        return (
            "unconsumed_reviewer", 2, "controller",
            "consume_reviewer_and_recompute_project",
        )
    if "candidate" in text or "integration" in text or "acceptance" in text:
        return (
            "unconsumed_candidate", 2, "controller",
            "consume_candidate_integration_acceptance_and_recompute",
        )
    if "active runtime unhealthy" in text or "recovering runtime stalled" in text or "recovery" in text:
        return (
            "recovery_action_omission", 2, "controller",
            "execute_canonical_recovery_and_recompute",
        )
    if "route" in text or "provider" in text or "model" in text:
        return (
            "routing_deviation", 3, "controller",
            "rederive_canonical_route_and_redispatch",
        )
    if "duplicate" in text or "already active" in text or "exclusive execution" in text:
        return (
            "duplicate_dispatch", 3, "controller",
            "reconcile_duplicate_dispatch_and_preserve_single_execution",
        )
    if "owned_files" in text or "file conflict" in text or "scope" in text or "worktree" in text:
        return (
            "scope_or_conflict_deviation", 3, "controller",
            "rederive_owned_scope_conflicts_and_dispatch_plan",
        )
    if "evidence" in text or "verdict" in text or "pass" in text or "done" in text:
        return (
            "unsupported_conclusion", 3, "controller",
            "invalidate_unsupported_conclusion_and_reverify",
        )
    if "blocked" in text or "reason_code" in text or "hard constraint" in text:
        return (
            "incorrect_block_or_defer", 2, "controller",
            "reclassify_blocker_from_machine_facts_and_recompute",
        )
    if "machine_trace" in text or "tool trace" in text:
        return (
            "controller_trace_deviation", 3, "controller_adapter",
            "repair_controller_control_receipt_trace_and_recompute",
        )
    if "integrity" in text and "rule" in text:
        return (
            "runtime_integrity_deviation", 3, "runtime",
            "repair_runtime_integrity_before_control_continues",
        )
    return (
        "control_contract_deviation", 2, "controller",
        "recompute_full_control_loop_and_submit_complete_receipt",
    )


def _deviation_scope(error: str) -> list[str]:
    task_ids = sorted(set(re.findall(
        r"(?<![A-Za-z0-9_-])([A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+)(?![A-Za-z0-9_-])",
        str(error),
    )))
    return task_ids[:16] or ["project"]


def _deviation_fingerprint(code: str, responsibility: str) -> str:
    encoded = json.dumps(
        {"code": code, "responsibility": responsibility},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:24]


def classify_controller_deviations(
    root: Path,
    controller_id: str,
    validation_errors: Sequence[str],
) -> list[dict[str, Any]]:
    historical: dict[str, int] = {}
    for record in _controller_cycle_records(root, controller_id):
        if str(record.get("terminal_status", "")).upper() != "FAILED":
            continue
        deviations = record.get("controller_deviations")
        if not isinstance(deviations, list):
            continue
        for deviation in deviations:
            if not isinstance(deviation, dict):
                continue
            fingerprint = str(deviation.get("fingerprint", "")).strip()
            if fingerprint:
                historical[fingerprint] = historical.get(fingerprint, 0) + 1

    grouped: dict[str, dict[str, Any]] = {}
    for raw_error in validation_errors:
        error = str(raw_error).strip()
        if not error:
            continue
        code, base_level, responsibility, action = _deviation_rule(error)
        fingerprint = _deviation_fingerprint(code, responsibility)
        value = grouped.setdefault(fingerprint, {
            "fingerprint": fingerprint,
            "deviation_code": code,
            "responsibility": responsibility,
            "affected_scope": [],
            "validation_errors": [],
            "base_level": base_level,
            "mandatory_action": action,
        })
        value["validation_errors"].append(error)
        value["affected_scope"] = sorted(set(
            list(value["affected_scope"]) + _deviation_scope(error)
        ))

    output: list[dict[str, Any]] = []
    for fingerprint in sorted(grouped):
        value = grouped[fingerprint]
        recurrence_count = historical.get(fingerprint, 0) + 1
        level_number = min(4, int(value["base_level"]) + recurrence_count - 1)
        level = f"L{level_number}"
        output.append({
            "fingerprint": fingerprint,
            "deviation_code": value["deviation_code"],
            "responsibility": value["responsibility"],
            "affected_scope": value["affected_scope"],
            "validation_errors": value["validation_errors"],
            "recurrence_count": recurrence_count,
            "level": level,
            "correction": {
                "mandatory": True,
                "executable": True,
                "projection": "canonical_project_control",
                "action": value["mandatory_action"],
                "requires_project_wide_recompute": level_number >= 3,
                "requires_unique_controller_handoff": level_number >= 4,
            },
        })
    return output


def open_controller_corrections(
    root: Path, controller_id: str
) -> list[dict[str, Any]]:
    open_by_fingerprint: dict[str, dict[str, Any]] = {}
    for record in _controller_cycle_records(root, controller_id):
        status = str(record.get("terminal_status", "")).upper()
        if status == "FAILED":
            deviations = record.get("controller_deviations")
            if isinstance(deviations, list):
                for deviation in deviations:
                    if not isinstance(deviation, dict):
                        continue
                    fingerprint = str(deviation.get("fingerprint", "")).strip()
                    if fingerprint:
                        open_by_fingerprint[fingerprint] = dict(deviation)
        elif status == "CLOSED":
            resolved = record.get("resolved_correction_fingerprints")
            if isinstance(resolved, list):
                for fingerprint in resolved:
                    open_by_fingerprint.pop(str(fingerprint).strip(), None)
    return [open_by_fingerprint[key] for key in sorted(open_by_fingerprint)]


def _resolved_correction_fingerprints(snapshot: dict[str, Any]) -> list[str]:
    actions = snapshot.get("correction_actions")
    if not isinstance(actions, list):
        return []
    return sorted({
        str(action.get("fingerprint", "")).strip()
        for action in actions
        if isinstance(action, dict)
        and str(action.get("decision", "")).strip().lower() == "corrected"
        and str(action.get("fingerprint", "")).strip()
    })


def _known_controller_incident_ids(root: Path, controller_id: str) -> set[str]:
    common = git_common_dir(Path(root).expanduser().resolve()) / "adaptive-delivery"
    incident_ids: set[str] = set()
    evidence_dir = common / CONTROLLER_CYCLE_EVIDENCE_DIRECTORY
    for path in sorted(evidence_dir.glob("*.json")) if evidence_dir.is_dir() else []:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(value, dict):
            continue
        if str(value.get("controller_id", "")).strip() != controller_id:
            continue
        if str(value.get("terminal_status", "")).upper().strip() != "FAILED":
            continue
        cycle_id = str(value.get("cycle_id", "")).strip()
        if cycle_id:
            incident_ids.add(cycle_id)
    history = common / "controller-score-history.jsonl"
    try:
        lines = history.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        if str(value.get("controller_session_id", "")).strip() != controller_id:
            continue
        if value.get("record_kind") != "cycle":
            continue
        if str(value.get("terminal_status", "")).upper().strip() != "FAILED":
            continue
        try:
            score = float(value.get("score"))
        except (TypeError, ValueError):
            continue
        cycle_id = str(value.get("cycle_id", "")).strip()
        if score <= 49 and cycle_id:
            incident_ids.add(cycle_id)
    return incident_ids


def _cycle_outcome_level(
    snapshot: dict[str, Any],
    *,
    terminal_status: str,
    integrated_revisions: set[str] | None,
) -> str:
    if terminal_status != "CLOSED":
        return "L0"
    if event_closes_goal(snapshot):
        return "L4"
    if integrated_revisions:
        return "L3"
    return "L2"


def _governance_incident_severity(
    snapshot: dict[str, Any],
    *,
    terminal_status: str,
    validation_errors: Sequence[str],
    integrated_revisions: set[str] | None,
) -> str:
    """Only post-integration gate failures create automatic 49-point debt."""
    if terminal_status != "FAILED" or not integrated_revisions:
        return "none"
    combined = " ".join(str(error).lower() for error in validation_errors)
    major_markers = (
        "required review",
        "review pass",
        "review fail",
        "regression_evidence",
        "main_revision does not match",
    )
    return "major" if any(marker in combined for marker in major_markers) else "none"


def persist_controller_cycle_evidence(
    root: Path,
    snapshot: dict[str, Any],
    *,
    controller_id: str,
    ledger_sha256: str,
    main_revision: str,
    terminal_status: str,
    validation_errors: Sequence[str],
    integrated_revisions: set[str] | None = None,
    ledger_open_ids: set[str] | None = None,
    ledger_task_states: dict[str, str] | None = None,
) -> tuple[dict[str, Any], Path]:
    """Persist one immutable machine-attested controller event outcome."""
    contract = snapshot.get("event_contract")
    if not isinstance(contract, dict):
        raise ValueError("controller cycle evidence requires event_contract")
    evidence_id = str(contract.get("event_id", "")).strip()
    controller = str(controller_id or "").strip()
    status = str(terminal_status or "").upper().strip()
    if not controller:
        raise ValueError("controller cycle evidence requires a controller id")
    if status not in {"CLOSED", "FAILED"}:
        raise ValueError("controller cycle evidence status must be CLOSED or FAILED")
    errors = [str(error).strip() for error in validation_errors if str(error).strip()]
    if status == "FAILED" and not errors:
        raise ValueError("failed controller cycle evidence requires validation errors")
    if status == "CLOSED" and errors:
        raise ValueError("closed controller cycle evidence cannot contain validation errors")
    terminal_receipt = str(contract.get("terminal_receipt", "")).strip()
    evidence_summary = (
        terminal_receipt if status == "CLOSED" else "; ".join(errors)
    )[:500].strip()
    if not evidence_summary:
        raise ValueError("controller cycle evidence requires a machine outcome summary")
    snapshot_bytes = json.dumps(
        snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    outcome_level = _cycle_outcome_level(
        snapshot,
        terminal_status=status,
        integrated_revisions=integrated_revisions,
    )
    clearance_markers = {
        field: str(contract.get(field, "")).strip()
        for field in (
            "corrects_incident",
            "alignment_for_incident",
            "post_incident_closure_for",
        )
        if str(contract.get(field, "")).strip()
    }
    if clearance_markers:
        if status != "CLOSED":
            raise ValueError("risk clearance markers require a CLOSED controller event")
        known_incidents = _known_controller_incident_ids(root, controller)
        unknown = sorted(set(clearance_markers.values()) - known_incidents)
        if unknown:
            raise ValueError("risk clearance marker references an unknown incident: " + ", ".join(unknown))
    if "corrects_incident" in clearance_markers:
        correction_revision = str(contract.get("correction_revision", "")).strip()
        if not correction_revision or correction_revision not in set(integrated_revisions or set()):
            raise ValueError(
                "correction clearance requires an integrated correction revision on current main"
            )
        candidates = snapshot.get("candidate_packages")
        candidate = next((
            item for item in candidates
            if isinstance(item, dict)
            and str(item.get("revision", "")).strip() == correction_revision
        ), None) if isinstance(candidates, list) else None
        if not isinstance(candidate, dict):
            raise ValueError("correction clearance requires the integrated correction candidate")
        if (
            str(candidate.get("decision", "")).lower().strip() != "integrate"
            or candidate.get("integrated_this_event") is not True
            or str(candidate.get("main_revision", "")).strip() != str(main_revision).strip()
            or not str(candidate.get("regression_evidence", "")).strip()
            or not str(candidate.get("acceptance_evidence", "")).strip()
        ):
            raise ValueError(
                "correction clearance requires current-main integration, regression, and acceptance evidence"
            )
        author_task_id = str(candidate.get("author_task_id", "")).strip()
        correction_task_id = str(candidate.get("task_id", "")).strip()
        candidate_review_task_id = str(candidate.get("review_task_id", "")).strip()
        reviews = snapshot.get("required_reviews")
        pass_reviews = [
            review for review in reviews
            if isinstance(review, dict)
            and str(review.get("candidate_revision", "")).strip() == correction_revision
            and str(review.get("verdict", "")).upper().strip() == "PASS"
            and review.get("delivered_ack") is True
        ] if isinstance(reviews, list) else []
        matching_pass_reviews = [
            review for review in pass_reviews
            if str(review.get("task_id", "")).strip() == candidate_review_task_id
        ]
        if not author_task_id or author_task_id != correction_task_id:
            raise ValueError(
                "correction clearance requires the author task bound to the correction task"
            )
        if (
            not candidate_review_task_id
            or not matching_pass_reviews
            or candidate_review_task_id == author_task_id
        ):
            raise ValueError("correction clearance requires a non-author PASS review")
        if ledger_task_states is None:
            raise ValueError(
                "correction clearance requires machine-derived current ledger task states"
            )
        identity_states = {
            correction_task_id: str(ledger_task_states.get(correction_task_id, "")).upper(),
            candidate_review_task_id: str(
                ledger_task_states.get(candidate_review_task_id, "")
            ).upper(),
        }
        invalid_identities = sorted(
            task_id
            for task_id, task_status in identity_states.items()
            if task_status not in LEDGER_SUCCESS_STATES
        )
        if invalid_identities:
            raise ValueError(
                "correction clearance requires successful current ledger task records for: "
                + ", ".join(invalid_identities)
            )
        if ledger_open_ids is None or any(
            task_id in ledger_open_ids for task_id in identity_states
        ):
            raise ValueError(
                "correction clearance requires correction and review tasks closed in the current ledger"
            )
        branch_remote = run_git(
            Path(root).expanduser().resolve(),
            "config",
            "--get",
            "branch.main.remote",
            check=False,
        )
        branch_merge = run_git(
            Path(root).expanduser().resolve(),
            "config",
            "--get",
            "branch.main.merge",
            check=False,
        )
        tracking_configured = bool(
            branch_remote.stdout.strip() or branch_merge.stdout.strip()
        )
        upstream = run_git(
            Path(root).expanduser().resolve(),
            "rev-parse",
            "--verify",
            "main@{upstream}",
            check=False,
        )
        if tracking_configured:
            if (
                not branch_remote.stdout.strip()
                or not branch_merge.stdout.strip()
                or upstream.returncode != 0
                or not upstream.stdout.strip()
            ):
                raise ValueError(
                    "correction clearance tracked remote is configured but unavailable"
                )
            if upstream.stdout.strip() != str(main_revision).strip():
                raise ValueError(
                    "correction clearance requires current main aligned with its tracked remote"
                )
        clearance_markers["correction_revision"] = correction_revision
        clearance_markers["risk_clearance_contract_version"] = 2
    if "alignment_for_incident" in clearance_markers:
        rule_update = snapshot.get("rule_update")
        if not isinstance(rule_update, dict):
            raise ValueError("alignment clearance requires a complete rule ACK receipt")
        revision = str(rule_update.get("revision", "")).strip()
        affected = rule_update.get("affected_tasks")
        acknowledged = rule_update.get("acknowledged_tasks")
        if (
            not revision
            or not isinstance(affected, list)
            or not affected
            or not isinstance(acknowledged, list)
            or {str(item).strip() for item in affected}
            != {str(item).strip() for item in acknowledged}
        ):
            raise ValueError("alignment clearance requires a complete rule ACK receipt")
    if (
        "post_incident_closure_for" in clearance_markers
        and outcome_level not in {"L3", "L4"}
    ):
        raise ValueError("post-incident closure clearance requires machine-derived L3 or L4 outcome")
    if "post_incident_closure_for" in clearance_markers:
        correction_evidence_id = str(
            contract.get("depends_on_correction_evidence_id", "")
        ).strip()
        if not correction_evidence_id:
            raise ValueError(
                "post-incident closure requires the exact correction evidence dependency"
            )
        correction_path = controller_cycle_evidence_path(root, correction_evidence_id)
        try:
            correction_receipt = json.loads(correction_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(
                f"post-incident closure correction evidence is missing: {error}"
            ) from error
        if (
            not isinstance(correction_receipt, dict)
            or correction_receipt.get("record_kind") != "controller_cycle_evidence"
            or str(correction_receipt.get("controller_id", "")).strip() != controller
            or str(correction_receipt.get("terminal_status", "")).upper().strip() != "CLOSED"
            or str(correction_receipt.get("corrects_incident", "")).strip()
            != clearance_markers["post_incident_closure_for"]
        ):
            raise ValueError(
                "post-incident closure correction evidence does not match this incident"
            )
        if correction_receipt.get("risk_clearance_contract_version") != 2:
            raise ValueError(
                "post-incident closure requires a strong correction evidence receipt"
            )
        clearance_markers["depends_on_correction_evidence_id"] = correction_evidence_id
    controller_deviations = (
        classify_controller_deviations(root, controller, errors)
        if status == "FAILED"
        else []
    )
    resolved_corrections = (
        _resolved_correction_fingerprints(snapshot)
        if status == "CLOSED"
        else []
    )
    if status == "CLOSED" and resolved_corrections:
        correction_errors = validate_correction_actions(
            snapshot, open_controller_corrections(root, controller)
        )
        if correction_errors:
            raise ValueError(
                "generic correction closure failed: " + "; ".join(correction_errors)
            )
    payload: dict[str, Any] = {
        "schema_version": 1,
        "record_kind": "controller_cycle_evidence",
        "evidence_id": evidence_id,
        "controller_id": controller,
        "cycle_id": evidence_id,
        "event_type": str(contract.get("event_type", "")).strip(),
        "primary_task": str(contract.get("primary_task", "")).strip(),
        "terminal_status": status,
        "evidence_summary": evidence_summary,
        "outcome_level": outcome_level,
        "governance_incident_severity": _governance_incident_severity(
            snapshot,
            terminal_status=status,
            validation_errors=errors,
            integrated_revisions=integrated_revisions,
        ),
        "main_revision": str(main_revision or "").strip(),
        "ledger_sha256": str(ledger_sha256 or "").strip(),
        "snapshot_sha256": hashlib.sha256(snapshot_bytes).hexdigest(),
        "validation_errors": errors,
        "controller_deviations": controller_deviations,
        "resolved_correction_fingerprints": resolved_corrections,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    payload.update(clearance_markers)
    target = controller_cycle_evidence_path(root, evidence_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        try:
            existing = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"immutable controller cycle evidence is unreadable: {error}") from error
        comparable_existing = dict(existing) if isinstance(existing, dict) else {}
        comparable_payload = dict(payload)
        comparable_existing.pop("recorded_at", None)
        comparable_payload.pop("recorded_at", None)
        if comparable_existing != comparable_payload:
            raise ValueError("immutable controller cycle evidence cannot be rewritten")
        return existing, target
    serialized = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    try:
        with target.open("x", encoding="utf-8") as handle:
            handle.write(serialized)
            handle.flush()
    except FileExistsError:
        return persist_controller_cycle_evidence(
            root,
            snapshot,
            controller_id=controller,
            ledger_sha256=ledger_sha256,
            main_revision=main_revision,
            terminal_status=status,
            validation_errors=errors,
            integrated_revisions=integrated_revisions,
            ledger_open_ids=ledger_open_ids,
            ledger_task_states=ledger_task_states,
        )
    return payload, target


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
    runtime_repo: str | Path | None = None,
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
        execution_mode = str(assignment.get("execution_mode", "")).strip().lower()
        if execution_mode not in EXECUTION_MODES:
            errors.append(
                f"{task_id or f'new_assignments[{index}]'} requires execution_mode delegated or controller"
            )
        owned_files = assignment.get("owned_files")
        if not isinstance(owned_files, list) or not owned_files:
            errors.append(f"{task_id or f'new_assignments[{index}]'} requires non-empty owned_files")
        else:
            normalized_files = [str(item).strip() for item in owned_files]
            if any(not item for item in normalized_files) or len(set(normalized_files)) != len(normalized_files):
                errors.append(
                    f"{task_id or f'new_assignments[{index}]'} owned_files must contain unique non-empty paths"
                )

        route = assignment.get("route")
        errors.extend(route_scope_errors(task_id, owned_files, route))
        if not isinstance(route, dict):
            if execution_mode == "delegated":
                errors.append(f"{task_id} delegated assignment requires route")
            elif execution_mode == "controller":
                errors.append(f"{task_id} controller execution requires route")
            continue
        for field in ("decision", "policy_class", "provider", "model", "auth_mode"):
            if not str(route.get(field, "")).strip():
                errors.append(f"{task_id} route requires {field}")
        policy_source = route.get("policy_source")
        if not isinstance(policy_source, dict):
            errors.append(f"{task_id} route requires policy_source")
        else:
            for field in ("path", "sha256"):
                if not str(policy_source.get(field, "")).strip():
                    errors.append(f"{task_id} route policy_source requires {field}")
        errors.extend(route_policy_errors(task_id, route))

        route_decision = str(route.get("decision", "")).strip().lower()
        if route_decision not in ROUTE_DECISIONS:
            errors.append(
                f"{task_id} route decision must be default, safe_fallback, or controller_exception"
            )
        if execution_mode == "delegated" and route_decision == "controller_exception":
            errors.append(f"{task_id} delegated assignment cannot use controller_exception")
        if execution_mode == "controller":
            if route_decision != "controller_exception":
                errors.append(f"{task_id} controller execution requires controller_exception route decision")
            exception = assignment.get("controller_exception")
            if not isinstance(exception, dict):
                errors.append(f"{task_id} controller execution requires controller_exception")
            else:
                reason_code = str(exception.get("reason_code", "")).strip().lower()
                if reason_code not in CONTROLLER_EXCEPTION_REASONS:
                    errors.append(
                        f"{task_id} controller_exception reason_code must be one of: "
                        + ", ".join(sorted(CONTROLLER_EXCEPTION_REASONS))
                    )
                for field in ("reason", "stop_condition"):
                    if not str(exception.get(field, "")).strip():
                        errors.append(f"{task_id} controller_exception requires {field}")
        if route_decision == "safe_fallback":
            errors.extend(
                canonical_safe_fallback_errors(
                    task_id, route, runtime_repo=runtime_repo
                )
            )
    return errors


def _known_next_action_id(next_action: str) -> str:
    digest = hashlib.sha256(str(next_action).strip().encode("utf-8")).hexdigest()[:16]
    return f"known_next_action:{digest}"


def _controller_lifecycle_state(controller_id: str) -> dict[str, Any]:
    try:
        from scripts.lifecycle_hook import load_json as load_lifecycle_json, state_path
    except ModuleNotFoundError:
        from lifecycle_hook import load_json as load_lifecycle_json, state_path
    return load_lifecycle_json(state_path(controller_id))


def mandatory_continuation_projection(
    snapshot: dict[str, Any],
    *,
    ledger_task_states: dict[str, str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Derive mandatory successors from event/candidate state without a second scheduler."""
    ledger_task_states = {
        str(task_id): str(state).upper()
        for task_id, state in (ledger_task_states or {}).items()
    }
    actions: dict[str, dict[str, Any]] = {}
    raw_candidates = snapshot.get("candidate_packages", [])
    candidates = {
        str(item.get("revision", "")).strip(): item
        for item in raw_candidates
        if isinstance(item, dict) and str(item.get("revision", "")).strip()
    } if isinstance(raw_candidates, list) else {}

    raw_reviews = snapshot.get("required_reviews", [])
    if isinstance(raw_reviews, list):
        for review in raw_reviews:
            if not isinstance(review, dict):
                continue
            verdict = str(review.get("verdict", "")).strip().upper()
            revision = str(review.get("candidate_revision", "")).strip()
            review_id = str(review.get("id", "")).strip() or "unnamed"
            if verdict not in {"PASS", "FAIL"} or not revision:
                continue
            candidate = candidates.get(revision)
            if candidate is None:
                actions[f"review_result:{review_id}"] = {
                    "type": "review_result",
                    "review_id": review_id,
                    "candidate_revision": revision,
                    "reason": "verdict_not_consumed",
                }
                continue
            decision = str(candidate.get("decision", "")).strip().lower()
            if verdict == "PASS" and decision not in {"integrate", "queued"}:
                actions[f"integration:{revision}"] = {
                    "type": "integration",
                    "candidate_revision": revision,
                    "reason": "review_pass_requires_integration",
                }
            if verdict == "FAIL" and decision != "rework":
                actions[f"rework:{revision}"] = {
                    "type": "rework",
                    "candidate_revision": revision,
                    "reason": "review_fail_requires_correction",
                }

    for revision, candidate in candidates.items():
        decision = str(candidate.get("decision", "")).strip().lower()
        if decision != "integrate" or candidate.get("integrated_this_event") is not True:
            continue
        task_id = str(candidate.get("task_id", "")).strip() or revision
        if candidate.get("current_main_verified") is not True:
            actions[f"current_main_verify:{revision}"] = {
                "type": "current_main_verify",
                "candidate_revision": revision,
                "task_id": task_id,
            }
            continue
        if candidate.get("fact_converged") is not True:
            actions[f"fact_convergence:{task_id}"] = {
                "type": "fact_convergence",
                "candidate_revision": revision,
                "task_id": task_id,
                "reason": "FACT_PROJECTION_DRIFT",
            }
            continue
        if candidate.get("post_integration_recomputed") is not True:
            actions[f"post_integration_recompute:{revision}"] = {
                "type": "post_integration_recompute",
                "candidate_revision": revision,
                "task_id": task_id,
            }
            continue
        if ledger_task_states.get(task_id) in {"ACTIVE", "RECOVERING", "VERIFY"}:
            actions[f"fact_convergence:{task_id}"] = {
                "type": "fact_convergence",
                "candidate_revision": revision,
                "task_id": task_id,
                "reason": "FACT_PROJECTION_DRIFT",
            }
    return actions


def validate_mandatory_continuations(
    snapshot: dict[str, Any],
    *,
    ledger_task_states: dict[str, str] | None = None,
) -> list[str]:
    errors: list[str] = []
    raw_candidates = snapshot.get("candidate_packages", [])
    candidates = [
        item for item in raw_candidates if isinstance(item, dict)
    ] if isinstance(raw_candidates, list) else []
    for candidate in candidates:
        revision = str(candidate.get("revision", "")).strip() or "unknown"
        task_id = str(candidate.get("task_id", "")).strip() or revision
        if (
            str(candidate.get("decision", "")).strip().lower() != "integrate"
            or candidate.get("integrated_this_event") is not True
        ):
            continue
        if candidate.get("current_main_verified") is not True:
            errors.append(
                f"INTEGRATION_NOT_CONTINUED: {revision} requires current-main verification after integration"
            )
            continue
        if not traceable_runtime_evidence(candidate.get("current_main_verification_evidence")):
            errors.append(
                f"INTEGRATION_NOT_CONTINUED: {revision} current-main verification requires traceable evidence"
            )
        if candidate.get("fact_converged") is not True:
            errors.append(
                f"FACT_PROJECTION_DRIFT: {task_id} facts must converge after current-main verification"
            )
            continue
        if not traceable_runtime_evidence(candidate.get("fact_convergence_evidence")):
            errors.append(
                f"FACT_PROJECTION_DRIFT: {task_id} fact convergence requires traceable evidence"
            )
        if candidate.get("post_integration_recomputed") is not True:
            errors.append(
                f"POST_INTEGRATION_RECOMPUTE_MISSING: {revision} requires post-integration project-wide recompute"
            )
            continue
        if not traceable_runtime_evidence(candidate.get("post_integration_recompute_evidence")):
            errors.append(
                f"POST_INTEGRATION_RECOMPUTE_MISSING: {revision} project-wide recompute requires traceable evidence"
            )
        if ledger_task_states is not None:
            state = str(ledger_task_states.get(task_id, "")).upper()
            if state in {"ACTIVE", "RECOVERING", "VERIFY"}:
                errors.append(
                    f"FACT_PROJECTION_DRIFT: {task_id} remains {state} after verified integration and convergence"
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


def traceable_runtime_evidence(value: Any) -> bool:
    token = str(value or "").strip()
    if ":" not in token:
        return False
    scheme, locator = token.split(":", 1)
    return scheme in {"receipt", "artifact"} and bool(locator.strip())


def _string_list_set(value: Any) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {str(item).strip() for item in value if str(item).strip()}


def validate_correction_actions(
    snapshot: dict[str, Any],
    expected_corrections: Sequence[dict[str, Any]],
) -> list[str]:
    errors: list[str] = []
    raw_corrections = snapshot.get("correction_actions")
    if not isinstance(raw_corrections, list):
        raw_corrections = []
    by_fp = {
        str(item.get("fingerprint", "")).strip(): item
        for item in raw_corrections
        if isinstance(item, dict) and str(item.get("fingerprint", "")).strip()
    }
    expected_by_fp = {
        str(item.get("fingerprint", "")).strip(): item
        for item in expected_corrections
        if isinstance(item, dict) and str(item.get("fingerprint", "")).strip()
    }
    for fingerprint, correction in sorted(expected_by_fp.items()):
        action = by_fp.get(fingerprint)
        if not isinstance(action, dict):
            errors.append(f"mandatory correction {fingerprint} is not completed")
            continue
        if str(action.get("decision", "")).strip().lower() != "corrected":
            errors.append(f"mandatory correction {fingerprint} must be corrected before closure")
            continue
        required_action = str(
            (correction.get("correction") or {}).get("action", "")
        ).strip() if isinstance(correction.get("correction"), dict) else ""
        if required_action and str(action.get("action", "")).strip() != required_action:
            errors.append(f"mandatory correction {fingerprint} action does not match derived correction")
        if not traceable_runtime_evidence(action.get("execution_evidence")):
            errors.append(f"mandatory correction {fingerprint} requires execution evidence")
        if not traceable_runtime_evidence(action.get("verification_evidence")):
            errors.append(f"mandatory correction {fingerprint} requires independent verification evidence")
        executed_by = str(action.get("executed_by", "")).strip()
        verified_by = str(action.get("verified_by", "")).strip()
        if not executed_by or not verified_by or executed_by == verified_by:
            errors.append(
                f"mandatory correction {fingerprint} requires distinct execution and verification actors"
            )
        if str(correction.get("level", "")).strip().upper() == "L4":
            if not traceable_runtime_evidence(action.get("unique_controller_handoff_evidence")):
                errors.append(
                    f"mandatory correction {fingerprint} L4 requires existing unique-Controller handoff evidence"
                )
    extra_corrections = sorted(set(by_fp) - set(expected_by_fp))
    if extra_corrections:
        errors.append(
            "control cycle contains non-canonical correction actions: "
            + ", ".join(extra_corrections)
        )
    return errors


def validate_control_loop_receipt(
    snapshot: dict[str, Any],
    *,
    expected_ledger_sha256: str | None,
    expected_runnable_ids: set[str],
    expected_candidate_revisions: set[str],
    expected_controller_action_ids: set[str],
    expected_corrections: Sequence[dict[str, Any]],
) -> list[str]:
    errors: list[str] = []
    loop = snapshot.get("control_loop_receipt")
    if not isinstance(loop, dict):
        errors.append("control_loop_receipt is required for Controller Stop/Yield closure")
        loop = {}
    if str(loop.get("scope", "")).strip() != "project_wide":
        errors.append("control_loop_receipt.scope must be project_wide")
    completed_steps = loop.get("completed_steps")
    if completed_steps != list(CONTROL_LOOP_STEPS):
        errors.append(
            "control_loop_receipt.completed_steps must match the fixed Controller control loop in order: "
            + ", ".join(CONTROL_LOOP_STEPS)
        )
    if expected_ledger_sha256 is not None and str(loop.get("ledger_sha256", "")).strip() != expected_ledger_sha256:
        errors.append("control_loop_receipt ledger_sha256 does not match the current ledger")
    expected_sets = {
        "runnable_ids": set(expected_runnable_ids),
        "candidate_revisions": set(expected_candidate_revisions),
        "controller_action_ids": set(expected_controller_action_ids),
        "correction_fingerprints": {
            str(item.get("fingerprint", "")).strip()
            for item in expected_corrections
            if isinstance(item, dict) and str(item.get("fingerprint", "")).strip()
        },
    }
    for field, expected in expected_sets.items():
        actual = _string_list_set(loop.get(field))
        if actual != expected:
            errors.append(
                f"control_loop_receipt {field} does not match canonical project projection: "
                f"expected {sorted(expected)}, got {sorted(actual)}"
            )
    if loop.get("recomputed_after_actions") is not True:
        errors.append("control_loop_receipt requires recomputed_after_actions=true")

    raw_actions = snapshot.get("controller_actions")
    if not isinstance(raw_actions, list):
        errors.append("controller_actions must enumerate every immediate Controller action")
        raw_actions = []
    actual_action_ids: set[str] = set()
    resolved_action_ids: set[str] = set()
    for index, action in enumerate(raw_actions):
        if not isinstance(action, dict):
            errors.append(f"controller_actions[{index}] must be an object")
            continue
        action_id = str(action.get("id", "")).strip()
        if not action_id:
            errors.append(f"controller_actions[{index}].id is required")
            continue
        if action_id in actual_action_ids:
            errors.append(f"duplicate controller action: {action_id}")
        actual_action_ids.add(action_id)
        decision = str(action.get("decision", "")).strip().lower()
        if decision == "executed":
            if not traceable_runtime_evidence(action.get("evidence")):
                errors.append(f"controller action {action_id} executed requires traceable evidence")
            else:
                resolved_action_ids.add(action_id)
        elif decision in {"blocked", "deferred"}:
            reason_code = str(action.get("reason_code", "")).strip().lower()
            if reason_code not in HARD_DEFER_REASON_CODES:
                errors.append(
                    f"controller action {action_id} {decision} requires a hard reason_code"
                )
            if not str(action.get("reason", "")).strip():
                errors.append(f"controller action {action_id} {decision} requires exact reason")
            if not traceable_runtime_evidence(action.get("evidence")):
                errors.append(f"controller action {action_id} {decision} requires traceable evidence")
            if decision == "deferred" and not str(action.get("next_checkpoint", "")).strip():
                errors.append(
                    f"controller action {action_id} deferred requires next_checkpoint"
                )
            if (
                reason_code in HARD_DEFER_REASON_CODES
                and str(action.get("reason", "")).strip()
                and traceable_runtime_evidence(action.get("evidence"))
                and (decision != "deferred" or str(action.get("next_checkpoint", "")).strip())
            ):
                resolved_action_ids.add(action_id)
        else:
            errors.append(
                f"controller action {action_id} decision must be executed, hard blocked, or hard deferred"
            )
    missing_actions = sorted(expected_controller_action_ids - actual_action_ids)
    extra_actions = sorted(actual_action_ids - expected_controller_action_ids)
    if missing_actions:
        errors.append("control cycle omitted controller actions: " + ", ".join(missing_actions))
    if extra_actions:
        errors.append("control cycle contains non-canonical controller actions: " + ", ".join(extra_actions))

    debt_ids = _string_list_set(loop.get("continuation_debt_ids"))
    if debt_ids != set(expected_controller_action_ids):
        errors.append(
            "control_loop_receipt continuation_debt_ids does not match canonical mandatory successors: "
            f"expected {sorted(expected_controller_action_ids)}, got {sorted(debt_ids)}"
        )
    open_debt_ids = _string_list_set(loop.get("open_continuation_debt_ids"))
    expected_open_debt = set(expected_controller_action_ids) - resolved_action_ids
    if open_debt_ids != expected_open_debt:
        errors.append(
            "control_loop_receipt open_continuation_debt_ids does not match unresolved actions: "
            f"expected {sorted(expected_open_debt)}, got {sorted(open_debt_ids)}"
        )
    if expected_open_debt:
        errors.append(
            "continuation debt remains open: " + ", ".join(sorted(expected_open_debt))
        )

    errors.extend(validate_correction_actions(snapshot, expected_corrections))
    return errors


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
    ledger_task_states: dict[str, str] | None = None,
    derived_runnable_ids: set[str] | None = None,
    expected_machine_trace: dict[str, Any] | None = None,
    expected_runtime_occupied_task_ids: set[str] | None = None,
    expected_candidate_revisions: set[str] | None = None,
    expected_controller_action_ids: set[str] | None = None,
    expected_corrections: Sequence[dict[str, Any]] | None = None,
    require_control_loop_receipt: bool = False,
    runtime_repo: str | Path | None = None,
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
    if expected_machine_trace is not None:
        declared_trace = snapshot.get("machine_trace")
        if not isinstance(declared_trace, dict):
            errors.append("machine_trace is required for a registered controller receipt")
        else:
            for field in ("turn_id", "tool_use_ids", "trace_sha256"):
                if declared_trace.get(field) != expected_machine_trace.get(field):
                    errors.append(
                        f"machine_trace.{field} does not match the observed controller tool trace"
                    )

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
    projection = snapshot.get("capacity_projection")
    if ledger_work_in_flight is not None:
        if not isinstance(projection, dict):
            errors.append("capacity_projection is required for machine-derived available_slots")
        else:
            if str(projection.get("source", "")).strip() != "host_runtime":
                errors.append("capacity_projection.source must be host_runtime")
            if not traceable_runtime_evidence(projection.get("evidence")):
                errors.append("capacity_projection requires traceable evidence")
            total_slots = projection.get("total_slots")
            if not isinstance(total_slots, int) or isinstance(total_slots, bool) or total_slots < 0:
                errors.append("capacity_projection.total_slots must be a non-negative integer")
            occupied = projection.get("occupied_task_ids")
            occupied_ids: set[str] = set()
            if not isinstance(occupied, list):
                errors.append("capacity_projection.occupied_task_ids must be a list")
            else:
                occupied_ids = {str(item).strip() for item in occupied if str(item).strip()}
                if len(occupied_ids) != len(occupied):
                    errors.append(
                        "capacity_projection.occupied_task_ids must contain unique non-empty task IDs"
                    )
                expected_occupied = (
                    set(expected_runtime_occupied_task_ids)
                    if expected_runtime_occupied_task_ids is not None
                    else set(ledger_work_in_flight)
                )
                if occupied_ids != expected_occupied:
                    errors.append(
                        "capacity_projection occupied tasks do not match canonical Runtime nonterminal assignments"
                    )
            if isinstance(total_slots, int) and not isinstance(total_slots, bool) and total_slots >= 0:
                projected_slots = max(0, total_slots - len(occupied_ids))
                if slots != projected_slots:
                    errors.append(
                        f"available_slots does not match machine projection: expected {projected_slots}"
                    )

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
            if reason_code in HARD_DEFER_REASON_CODES:
                if not traceable_runtime_evidence(package.get("evidence")):
                    errors.append(
                        f"{package_id} {decision} decision requires traceable evidence"
                    )
                if decision == "deferred" and not str(package.get("next_checkpoint", "")).strip():
                    errors.append(
                        f"{package_id} deferred decision requires next_checkpoint"
                    )
            else:
                deferred_without_hard_constraint.append(package_id)

    if isinstance(slots, int) and active_decisions > slots:
        errors.append(
            f"active dispatch decisions exceed available capacity: {active_decisions} > {slots}"
        )
    if isinstance(slots, int) and slots > active_decisions and deferred_without_hard_constraint:
        errors.append(
            "idle dispatch capacity remains for project-wide runnable packages without a hard constraint: "
            + ", ".join(sorted(deferred_without_hard_constraint))
        )

    if ledger_ready_ids is not None:
        missing = sorted(ledger_ready_ids - snapshot_ready_ids)
        allowed_dispatch_ids = set(ledger_ready_ids) | set(derived_runnable_ids or set())
        extra = sorted(snapshot_ready_ids - allowed_dispatch_ids)
        if missing:
            errors.append("control event omitted READY packages: " + ", ".join(missing))
        if extra:
            errors.append("control event contains non-runnable packages: " + ", ".join(extra))
    if derived_runnable_ids is not None:
        missing_derived = sorted(set(derived_runnable_ids) - snapshot_ready_ids)
        if missing_derived:
            errors.append("control event omitted derived runnable packages: " + ", ".join(missing_derived))

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
                runtime_repo=runtime_repo,
            )
        )
    errors.extend(validate_review_transitions(snapshot, expected_main_revision=expected_main_revision))
    errors.extend(
        validate_mandatory_continuations(
            snapshot,
            ledger_task_states=ledger_task_states,
        )
    )
    errors.extend(
        validate_goal_rollover(
            snapshot, ledger_open_ids=ledger_open_ids, ledger_goal_ids=ledger_goal_ids
        )
    )
    if require_control_loop_receipt:
        errors.extend(
            validate_control_loop_receipt(
                snapshot,
                expected_ledger_sha256=expected_ledger_sha256,
                expected_runnable_ids=set(derived_runnable_ids or ledger_ready_ids or set()),
                expected_candidate_revisions=set(expected_candidate_revisions or set()),
                expected_controller_action_ids=set(expected_controller_action_ids or set()),
                expected_corrections=list(expected_corrections or []),
            )
        )
    return errors



def canonical_web_dispatch_errors(repo: Path, snapshot: dict[str, Any]) -> list[str]:
    """Require delegated Web spawns to be bound to a real child session in canonical Runtime state."""
    assignments = snapshot.get("new_assignments", [])
    if not isinstance(assignments, list):
        return []
    try:
        from scripts.project_state import adaptive_delivery_state_dir
        from scripts.assignment_runtime import load_runtime_state
    except ModuleNotFoundError:
        from project_state import adaptive_delivery_state_dir
        from assignment_runtime import load_runtime_state
    dispatch_path = adaptive_delivery_state_dir(repo) / "web-agent-dispatches.json"
    try:
        dispatch_doc = json.loads(dispatch_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        dispatch_doc = {}
    dispatches = dispatch_doc.get("dispatches", {}) if isinstance(dispatch_doc, dict) else {}
    if not isinstance(dispatches, dict):
        dispatches = {}
    runtime = load_runtime_state(repo)
    leases = runtime.get("leases", {}) if isinstance(runtime, dict) else {}
    errors: list[str] = []
    for index, assignment in enumerate(assignments):
        if not isinstance(assignment, dict):
            continue
        route = assignment.get("route")
        provider = str(route.get("provider") or "").strip().lower() if isinstance(route, dict) else ""
        transport = str(assignment.get("execution_transport") or "").strip().lower()
        if transport != "web" and provider not in {"chatgpt_web", "chatgpt-web", "web"}:
            continue
        task_id = str(assignment.get("task_id") or f"new_assignments[{index}]").strip()
        assignment_id = str(assignment.get("assignment_id") or "").strip()
        declared = assignment.get("runtime_dispatch")
        if not assignment_id or not isinstance(declared, dict):
            errors.append(f"{task_id} Web delegated assignment requires machine-verified canonical Runtime dispatch")
            continue
        dispatch_id = str(declared.get("dispatch_id") or "").strip()
        ticket = dispatches.get(dispatch_id)
        lease = leases.get(assignment_id) if isinstance(leases, dict) else None
        if (
            not dispatch_id
            or not isinstance(ticket, dict)
            or ticket.get("state") != "bound"
            or ticket.get("assignment_id") != assignment_id
            or not isinstance(lease, dict)
            or lease.get("task_id") != assignment.get("task_id")
            or lease.get("execution_transport") != "web"
            or str(lease.get("session_id") or "") != str(ticket.get("conversation_id") or "")
            or str(lease.get("lease_id") or "") != str(declared.get("lease_id") or "")
            or str(ticket.get("conversation_id") or "") != str(declared.get("conversation_id") or "")
            or declared.get("state") != "bound"
        ):
            errors.append(f"{task_id} Web delegated assignment lacks matching canonical Runtime dispatch/lease evidence")
    return errors


def canonical_rule_handshake_errors(
    repo: Path,
    ledger: Path,
    *,
    snapshot: dict[str, Any] | None = None,
    handshake_evaluator: Any | None = None,
    wake_policy_resolver: Any | None = None,
) -> list[str]:
    try:
        if handshake_evaluator is None:
            try:
                from rule_handshake import evaluate_rule_handshake as handshake_evaluator
            except ModuleNotFoundError:
                from scripts.rule_handshake import evaluate_rule_handshake as handshake_evaluator
        status = handshake_evaluator(repo, ledger=ledger)
    except (OSError, ValueError) as error:
        return [f"rule handshake integrity check failed: {error}"]
    if status.get("blocking") is True:
        try:
            if wake_policy_resolver is None:
                try:
                    from rule_handshake import derive_rule_wake_policy as wake_policy_resolver
                except ModuleNotFoundError:
                    from scripts.rule_handshake import derive_rule_wake_policy as wake_policy_resolver
            event_snapshot = snapshot if isinstance(snapshot, dict) else {}
            wake_policy = wake_policy_resolver(
                status, assignment_liveness=event_snapshot.get("assignment_liveness", {})
            )
            new_assignments = event_snapshot.get("new_assignments", [])
            if wake_policy == "after_event" and isinstance(new_assignments, list) and not new_assignments:
                return []
        except (OSError, ValueError):
            pass
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


def observed_machine_trace_from_state(state: dict[str, Any]) -> dict[str, Any]:
    if state.get("tool_trace_overflow") is True:
        raise ValueError("registered controller machine trace overflowed in the current turn")
    try:
        from lifecycle_hook import machine_trace_projection
    except ModuleNotFoundError:
        from scripts.lifecycle_hook import machine_trace_projection
    projection = machine_trace_projection(state)
    if not projection.get("turn_id"):
        raise ValueError("registered controller has no active turn machine trace")
    return projection


def observed_machine_trace(
    controller_session: str,
    *,
    state_root: Path | None = None,
) -> dict[str, Any]:
    try:
        from lifecycle_hook import load_json, state_path
    except ModuleNotFoundError:
        from scripts.lifecycle_hook import load_json, state_path

    session_id = controller_session.strip()
    if not session_id:
        raise ValueError("controller session is required for machine trace validation")
    path = state_path(session_id)
    if state_root is not None:
        path = state_root / path.name
    state = load_json(path)
    return observed_machine_trace_from_state(state)


def resolve_controller_trace_session(
    repo: Path,
    declared_session: str | None,
    *,
    registry_path: Path | None = None,
) -> str | None:
    try:
        from lifecycle_hook import REGISTRY_PATH, load_json
    except ModuleNotFoundError:
        from scripts.lifecycle_hook import REGISTRY_PATH, load_json

    root = repo.expanduser().resolve()
    registry = load_json(registry_path or REGISTRY_PATH)
    owners = sorted(
        session_id
        for session_id, registered_path in registry.items()
        if isinstance(session_id, str)
        and not session_id.startswith("__")
        and isinstance(registered_path, str)
        and Path(registered_path).expanduser().resolve() == root
    )
    declared = str(declared_session or "").strip()
    if len(owners) > 1:
        raise ValueError("canonical repository has ambiguous registered controllers")
    if not owners:
        if declared:
            raise ValueError("--controller-session is not registered for this repository")
        return None
    owner = owners[0]
    if not declared:
        raise ValueError(
            f"registered controller receipt requires --controller-session {owner}"
        )
    if declared != owner:
        raise ValueError("--controller-session does not own this canonical repository")
    return owner


def canonical_controller_action_projection(
    repo: Path,
    *,
    controller_id: str,
    candidates: dict[str, str] | None,
    required_review_ids: set[str],
    work_in_flight: dict[str, str],
    corrections: Sequence[dict[str, Any]],
    snapshot: dict[str, Any] | None = None,
    ledger_task_states: dict[str, str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Derive immediate Controller-owned actions from existing canonical facts."""
    actions: dict[str, dict[str, Any]] = {}
    for revision in sorted(set((candidates or {}).values())):
        actions[f"candidate:{revision}"] = {
            "type": "candidate",
            "candidate_revision": revision,
        }
    for review_id in sorted(required_review_ids):
        actions[f"review:{review_id}"] = {"type": "review", "review_id": review_id}

    try:
        from scripts.assignment_runtime import evaluate_lease, load_runtime_state
    except ModuleNotFoundError:
        from assignment_runtime import evaluate_lease, load_runtime_state
    runtime = load_runtime_state(repo)
    leases = runtime.get("leases", {}) if isinstance(runtime, dict) else {}

    candidate_by_worktree = {
        str(Path(path).expanduser().resolve()): str(revision).strip()
        for path, revision in (candidates or {}).items()
        if str(path).strip() and str(revision).strip()
    }
    active_by_worktree: dict[str, list[dict[str, Any]]] = {}
    if isinstance(leases, dict):
        for lease in leases.values():
            if not isinstance(lease, dict) or lease.get("terminal_state"):
                continue
            worktree = str(lease.get("worktree", "")).strip()
            if not worktree:
                continue
            active_by_worktree.setdefault(
                str(Path(worktree).expanduser().resolve()), []
            ).append(lease)
    for worktree, revision in sorted(candidate_by_worktree.items()):
        matching = active_by_worktree.get(worktree, [])
        if not matching:
            continue
        lease = max(matching, key=lambda item: int(item.get("attempt", 0) or 0))
        observed_head = str(lease.get("last_observed_head") or "").strip()
        recorded_candidate = str(lease.get("candidate_revision") or "").strip()
        if observed_head == revision and recorded_candidate == revision:
            continue
        assignment_id = str(lease.get("assignment_id") or "").strip()
        if not assignment_id:
            continue
        actions[f"control_plane_reconcile:{assignment_id}"] = {
            "type": "control_plane_reconcile",
            "assignment_id": assignment_id,
            "task_id": str(lease.get("task_id") or "").strip(),
            "worktree": worktree,
            "expected_candidate_revision": revision,
            "observed_runtime_head": observed_head,
            "recorded_candidate_revision": recorded_candidate or None,
            "reason": "FACT_PROJECTION_DRIFT",
        }

    for task_id in sorted(work_in_flight):
        matching = [
            lease for lease in leases.values()
            if isinstance(lease, dict) and str(lease.get("task_id", "")).strip() == task_id
        ] if isinstance(leases, dict) else []
        if not matching:
            actions[f"recovery:{task_id}"] = {
                "type": "recovery",
                "task_id": task_id,
                "reason": "missing_runtime_lease",
            }
            continue
        lease = max(matching, key=lambda item: int(item.get("attempt", 0) or 0))
        health = evaluate_lease(lease)
        if str(health.get("state", "")) in {"unhealthy", "budget_exhausted", "terminal"}:
            actions[f"recovery:{task_id}"] = {
                "type": "recovery",
                "task_id": task_id,
                "reason": str(health.get("reason", "") or health.get("state", "")),
            }

    lifecycle_state = _controller_lifecycle_state(controller_id)
    pending_receipts = lifecycle_state.get("pending_terminal_receipts", [])
    if isinstance(pending_receipts, list):
        for value in pending_receipts:
            receipt = str(value).strip()
            if not receipt:
                continue
            key = hashlib.sha256(receipt.encode("utf-8")).hexdigest()[:16]
            actions[f"terminal_receipt:{key}"] = {
                "type": "terminal_receipt",
                "receipt": receipt,
            }

    next_action = str(lifecycle_state.get("next_action") or "").strip()
    if next_action and lifecycle_state.get("requires_user") is False:
        action_id = _known_next_action_id(next_action)
        actions[action_id] = {
            "type": "known_next_action",
            "next_action": next_action,
        }

    for action_id, action in mandatory_continuation_projection(
        snapshot or {},
        ledger_task_states=ledger_task_states,
    ).items():
        actions[action_id] = action

    for correction in corrections:
        if not isinstance(correction, dict):
            continue
        fingerprint = str(correction.get("fingerprint", "")).strip()
        if fingerprint:
            actions[f"correction:{fingerprint}"] = {
                "type": "correction",
                "fingerprint": fingerprint,
                "level": str(correction.get("level", "")),
            }
    return actions


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
        "--controller-session",
        help="registered controller session whose observed tool trace must match this receipt",
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
        projection = project_wide_dispatch_projection(ledger)
        ledger_text = str(projection["ledger_text"])
        ledger_task_states = dict(projection["task_states"])
        ready_ids = set(projection["ready_ids"])
        derived_runnable_ids = set(projection["derived_runnable_ids"])
        open_ids = set(projection["open_ids"])
        goal_ids = set(projection["goal_ids"])
        work_in_flight = dict(projection["work_in_flight"])
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
        trace_session = (
            resolve_controller_trace_session(repo_root, args.controller_session)
            if repo_root is not None
            else str(args.controller_session or "").strip() or None
        )
        expected_machine_trace = (
            observed_machine_trace(trace_session) if trace_session else None
        )
        expected_corrections = (
            open_controller_corrections(repo_root, trace_session)
            if repo_root is not None and trace_session
            else []
        )
        expected_candidate_revisions = set((candidates or {}).values())
        expected_runtime_occupied = (
            runtime_occupied_task_ids(repo_root, work_in_flight)
            if repo_root is not None
            else None
        )
        expected_controller_actions = (
            canonical_controller_action_projection(
                repo_root,
                controller_id=trace_session,
                candidates=candidates,
                required_review_ids=set(args.require_review),
                work_in_flight=work_in_flight,
                corrections=expected_corrections,
                snapshot=snapshot,
                ledger_task_states=ledger_task_states,
            )
            if repo_root is not None and trace_session
            else {}
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
        ledger_task_states=ledger_task_states,
        derived_runnable_ids=derived_runnable_ids,
        expected_machine_trace=expected_machine_trace,
        expected_runtime_occupied_task_ids=expected_runtime_occupied,
        expected_candidate_revisions=expected_candidate_revisions,
        expected_controller_action_ids=set(expected_controller_actions),
        expected_corrections=expected_corrections,
        require_control_loop_receipt=bool(trace_session),
        runtime_repo=repo_root,
    )
    if repo_root is not None:
        errors.extend(canonical_rule_handshake_errors(repo_root, ledger, snapshot=snapshot))
        errors.extend(canonical_web_dispatch_errors(repo_root, snapshot))
    from ledger_consistency_guard import validate_ledger

    errors.extend(
        f"ledger consistency: {error}"
        for error in validate_ledger(ledger.read_text(encoding="utf-8"))
    )
    for error in errors:
        print(f"control-event: blocked: {error}")
    if errors:
        if repo_root is not None and trace_session and main_revision:
            try:
                persist_controller_cycle_evidence(
                    repo_root,
                    snapshot,
                    controller_id=trace_session,
                    ledger_sha256=current_ledger_sha256,
                    main_revision=main_revision,
                    terminal_status="FAILED",
                    validation_errors=errors,
                    integrated_revisions=integrated_revisions,
                    ledger_open_ids=open_ids,
                    ledger_task_states=ledger_task_states,
                )
            except (OSError, ValueError) as error:
                print(f"control-event: blocked: failed to persist machine cycle evidence: {error}")
        return 1
    if args.repo:
        raw_candidates = snapshot.get("candidate_packages", [])
        if isinstance(raw_candidates, list):
            try:
                record_candidate_lifecycle(Path(args.repo).expanduser().resolve(), [c for c in raw_candidates if isinstance(c, dict)])
            except OSError as error:
                print(f"control-event: blocked: failed to persist candidate lifecycle: {error}")
                return 1
    if repo_root is not None and trace_session and main_revision:
        try:
            evidence, _ = persist_controller_cycle_evidence(
                repo_root,
                snapshot,
                controller_id=trace_session,
                ledger_sha256=current_ledger_sha256,
                main_revision=main_revision,
                terminal_status="CLOSED",
                validation_errors=[],
                integrated_revisions=integrated_revisions,
                ledger_open_ids=open_ids,
                ledger_task_states=ledger_task_states,
            )
        except (OSError, ValueError) as error:
            print(f"control-event: blocked: failed to persist machine cycle evidence: {error}")
            return 1
    print(
        "control-event: allowed; declared READY, candidate, review and rule ACK decisions are complete"
        + (
            f"; cycle evidence receipt={evidence['evidence_id']}"
            if repo_root is not None and trace_session and main_revision
            else ""
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
