#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from lint_governance import task_rows

DECISIONS = {"active", "deferred", "blocked"}
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


def ledger_sha256(ledger: Path) -> str:
    return hashlib.sha256(ledger.read_bytes()).hexdigest()


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
    return errors


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
        current_ledger_sha256 = ledger_sha256(ledger)
        if args.affected_task and not args.rule_revision:
            raise ValueError("--affected-task requires --rule-revision")
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
    )
    for error in errors:
        print(f"control-event: blocked: {error}")
    if errors:
        return 1
    print("control-event: allowed; declared READY, review and rule ACK decisions are complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
