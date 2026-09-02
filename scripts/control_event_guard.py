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

from lint_governance import task_records, task_rows
try:
    from controller_state import derive_runnable_tasks
except ModuleNotFoundError:
    from scripts.controller_state import derive_runnable_tasks

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
ROUTE_DECISIONS = {"default", "safe_fallback", "controller_exception"}
CONTROLLER_EXCEPTION_REASONS = {
    "shared_contract_unstable",
    "unsafe_to_split",
    "active_wip_recovery",
    "low_risk_tiny_change",
}
ROUTE_CLASS_MARKERS = {
    "backend": ("backend", "后端", "server"),
    "frontend": ("frontend", "前端", "web", "小程序", "miniapp"),
}
CONTROLLER_CYCLE_EVIDENCE_DIRECTORY = "controller-cycle-evidence"
LEDGER_SUCCESS_STATES = {"DONE"}


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
            missing_fallback: list[str] = []
            fallback_from = route.get("fallback_from")
            if not isinstance(fallback_from, dict):
                missing_fallback.append("fallback_from")
            else:
                for field in ("provider", "model", "auth_mode"):
                    if not str(fallback_from.get(field, "")).strip():
                        missing_fallback.append(f"fallback_from.{field}")
            if not str(route.get("failure_evidence", "")).strip():
                missing_fallback.append("failure_evidence")
            if route.get("prior_attempt_terminal") is not True:
                missing_fallback.append("prior_attempt_terminal=true")
            if route.get("result_unknown") is not False:
                missing_fallback.append("result_unknown=false")
            if missing_fallback:
                errors.append(
                    f"{task_id} safe fallback requires " + ", ".join(missing_fallback)
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


def route_policy_errors(task_id: str, route: dict[str, Any]) -> list[str]:
    source = route.get("policy_source")
    if not isinstance(source, dict):
        return []
    raw_path = str(source.get("path", "")).strip()
    expected_sha = str(source.get("sha256", "")).strip().lower()
    if not raw_path or not expected_sha:
        return []
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        return [f"{task_id} route policy_source.path must be absolute"]
    try:
        payload = path.read_bytes()
    except OSError as error:
        return [f"{task_id} route policy source is unreadable: {error}"]
    if hashlib.sha256(payload).hexdigest() != expected_sha:
        return [f"{task_id} route policy_source.sha256 does not match the policy file"]

    decision = str(route.get("decision", "")).strip().lower()
    if decision == "default":
        selected: Any = route
    elif decision == "safe_fallback":
        selected = route.get("fallback_from")
    elif decision == "controller_exception":
        selected = route.get("default_route")
        if not isinstance(selected, dict):
            return [f"{task_id} controller_exception route requires default_route"]
    else:
        return []
    if not isinstance(selected, dict):
        return []
    route_values = {
        field: str(selected.get(field, "")).strip()
        for field in ("provider", "model", "auth_mode")
    }
    if any(not value for value in route_values.values()):
        return []
    policy_class = str(route.get("policy_class", "")).strip().lower()
    markers = ROUTE_CLASS_MARKERS.get(policy_class, (policy_class,)) if policy_class else ()
    policy_text = payload.decode("utf-8", errors="replace")
    for line in policy_text.splitlines():
        lowered = line.casefold()
        if markers and not any(marker.casefold() in lowered for marker in markers):
            continue
        if all(
            re.search(
                rf"{field}\s*=\s*{re.escape(value)}(?=$|[^A-Za-z0-9_.-])",
                line,
                re.IGNORECASE,
            )
            for field, value in route_values.items()
        ):
            return []
    return [f"{task_id} route is not declared by policy source"]


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
    derived_runnable_ids: set[str] | None = None,
    expected_machine_trace: dict[str, Any] | None = None,
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
                expected_occupied = set(ledger_work_in_flight)
                if occupied_ids != expected_occupied:
                    errors.append(
                        "capacity_projection occupied tasks do not match machine work-in-flight projection"
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
            if reason_code not in HARD_DEFER_REASON_CODES:
                deferred_without_hard_constraint.append(package_id)

    if isinstance(slots, int) and slots > active_decisions and deferred_without_hard_constraint:
        errors.append(
            "idle dispatch capacity remains for READY packages without a hard constraint: "
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
            )
        )
    errors.extend(validate_review_transitions(snapshot, expected_main_revision=expected_main_revision))
    errors.extend(
        validate_goal_rollover(
            snapshot, ledger_open_ids=ledger_open_ids, ledger_goal_ids=ledger_goal_ids
        )
    )
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
        ledger_text = ledger.read_text(encoding="utf-8")
        ledger_records = task_records(ledger_text)
        ledger_task_states = {
            record["id"]: record["status"] for record in ledger_records
        }
        ready_ids = ready_ledger_package_ids(ledger)
        derived_runnable_ids = set(derive_runnable_tasks(ledger_records)["runnable_task_ids"])
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
        trace_session = (
            resolve_controller_trace_session(repo_root, args.controller_session)
            if repo_root is not None
            else str(args.controller_session or "").strip() or None
        )
        expected_machine_trace = (
            observed_machine_trace(trace_session) if trace_session else None
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
        derived_runnable_ids=derived_runnable_ids,
        expected_machine_trace=expected_machine_trace,
    )
    if repo_root is not None:
        errors.extend(canonical_rule_handshake_errors(repo_root, ledger, snapshot=snapshot))
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
