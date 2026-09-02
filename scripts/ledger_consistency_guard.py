#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Sequence

from lint_governance import pointer_ids, task_records


OPEN_STATES = {"PENDING", "READY", "ACTIVE", "RECOVERING", "VERIFY", "BLOCKED"}
WORK_IN_FLIGHT_STATES = {"ACTIVE", "RECOVERING"}
NONE_VALUES = {"无", "none", "None"}
RUNTIME_CAPACITY_POINTER = re.compile(
    r"^-\s*(?:容量\s*/\s*READY|当前容量|实时容量|当前\s*Writer|当前\s*Reviewer)\s*：",
    re.MULTILINE | re.IGNORECASE,
)


def pointer(text: str, label: str) -> str | None:
    match = re.search(rf"^- {re.escape(label)}：\s*(.+?)\s*$", text, re.MULTILINE)
    return match.group(1).strip() if match else None


def referenced_ids(value: str, identifiers: set[str]) -> set[str]:
    return {
        identifier
        for identifier in identifiers
        if re.search(
            rf"(?<![A-Za-z0-9_-]){re.escape(identifier)}(?![A-Za-z0-9_-])",
            value,
        )
    }


def has_owner(record: dict[str, str]) -> bool:
    combined = f"{record['owner']} {record['status_cell']}"
    if "待分配" in combined:
        return False
    return bool(
        record["owner"].strip()
        or "项目总控" in combined
        or "主 Agent" in combined
        or "/root/" in combined
        or bool(re.search(r"\b(?:Agent|Writer)\s+[A-Za-z0-9_/-]+", combined, re.I))
    )


TASK_LIKE_ID = re.compile(
    r"(?<![A-Za-z0-9_/-])([A-Z][A-Z0-9]*(?:-[A-Z0-9]+){2,})(?![A-Za-z0-9_/-])"
)


def looks_like_task_id(identifier: str) -> bool:
    return any(
        any(character.isalpha() for character in segment)
        and any(character.isdigit() for character in segment)
        for segment in identifier.split("-")
    )


RECOVERY_ACK = re.compile(r"(?:delivered\s+ACK|完整\s*(?:delivered\s*)?ACK|\bACK\b)", re.I)
RECOVERY_ASSIGNMENT = re.compile(
    r"(?:\bAssignment\b|[A-Z][A-Z0-9]*(?:-[A-Z0-9]+){2,})", re.I
)
RECOVERY_ACTION_EVIDENCE = re.compile(
    r"(?:已完成|已执行|已修复|已恢复|已复验|已合入|已提交|已回收).{0,100}"
    r"(?:PASS|FAIL|[0-9a-f]{7,40}|checkpoint|检查点|测试|验证|收据|main)",
    re.I,
)


def marked_assignment_ids(value: str, declared_ids: set[str]) -> set[str]:
    assignment_ids: set[str] = set()
    for match in re.finditer(
        r"\bAssignment\b\s*`?([A-Z][A-Z0-9]*(?:-[A-Z0-9]+){2,})`?",
        value,
        re.IGNORECASE,
    ):
        identifier = match.group(1)
        if any(identifier.startswith(f"{task_id}-") for task_id in declared_ids):
            assignment_ids.add(identifier)
    return assignment_ids


def undeclared_next_task_ids(record: dict[str, str], declared_ids: set[str]) -> set[str]:
    assignment_ids = marked_assignment_ids(record["next_action"], declared_ids)
    return {
        match.group(1)
        for match in TASK_LIKE_ID.finditer(record["next_action"])
        if looks_like_task_id(match.group(1))
        and match.group(1) not in declared_ids
        and match.group(1) not in assignment_ids
    }


def has_recovery_execution_binding(record: dict[str, str]) -> bool:
    row = record["row"]
    assignment_ack = bool(RECOVERY_ACK.search(row)) and bool(
        RECOVERY_ASSIGNMENT.search(record["owner"])
    )
    return assignment_ack or bool(RECOVERY_ACTION_EVIDENCE.search(row))


def validate_ledger(text: str) -> list[str]:
    errors: list[str] = []
    if RUNTIME_CAPACITY_POINTER.search(text):
        errors.append(
            "runtime capacity and live assignment counts belong in the ephemeral control receipt, not the ledger header"
        )
    records = task_records(text)
    identifiers = [record["id"] for record in records]
    declared_ids = set(identifiers)
    duplicate_ids = sorted(
        identifier for identifier in set(identifiers) if identifiers.count(identifier) > 1
    )
    if duplicate_ids:
        errors.append("duplicate task IDs: " + ", ".join(duplicate_ids))
    open_ids = {
        record["id"] for record in records if record["status"] in OPEN_STATES
    }
    work_in_flight = {
        record["id"] for record in records if record["status"] in WORK_IN_FLIGHT_STATES
    }

    activity = pointer(text, "当前活动项")
    if activity is not None:
        declared = set() if activity in NONE_VALUES else pointer_ids(activity)
        if declared != work_in_flight:
            errors.append("current activity pointer does not match ACTIVE/RECOVERING task rows")

    goal = pointer(text, "当前 Goal")
    if not goal:
        errors.append("current Goal is required")
    elif open_ids and not referenced_ids(goal, open_ids):
        errors.append("current Goal must reference at least one open task ID")

    checkpoint = pointer(text, "下一可见检查点")
    if not checkpoint:
        errors.append("next visible checkpoint is required")
    else:
        if open_ids and not referenced_ids(checkpoint, open_ids):
            errors.append("next visible checkpoint must reference at least one open task ID")
        assignment_ids = marked_assignment_ids(checkpoint, declared_ids)
        for match in TASK_LIKE_ID.finditer(checkpoint):
            checkpoint_id = match.group(1)
            if (
                looks_like_task_id(checkpoint_id)
                and checkpoint_id not in declared_ids
                and checkpoint_id not in assignment_ids
            ):
                errors.append(
                    f"checkpoint references undeclared task ID {checkpoint_id}; add an explicit task row before dispatch"
                )

    for label in ("当前阻塞", "规则版本"):
        if not pointer(text, label):
            errors.append(f"{label} is required")

    for record in records:
        hidden_task_ids = sorted(undeclared_next_task_ids(record, declared_ids))
        for hidden_task_id in hidden_task_ids:
            errors.append(
                f"{record['id']} next action references undeclared task ID {hidden_task_id}; add an explicit task row before dispatch"
            )
        if record["status"] not in WORK_IN_FLIGHT_STATES:
            continue
        if not has_owner(record):
            errors.append(f"{record['id']} {record['status']} requires a unique owner")
        if record["status"] == "RECOVERING":
            next_action = record["next_action"]
            if not next_action.strip():
                errors.append(f"{record['id']} RECOVERING requires a recovery action/checkpoint")
            elif not re.search(
                r"恢复|复审|修复|补派|接管|重试|唤醒|检查点|checkpoint|回原|回两|继续",
                next_action,
                re.IGNORECASE,
            ):
                errors.append(f"{record['id']} RECOVERING next step lacks a recovery action/checkpoint")
            if not has_recovery_execution_binding(record):
                errors.append(
                    f"{record['id']} RECOVERING requires a delivered assignment ACK or a verifiable recovery action"
                )
    return errors


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate the canonical task table and the ledger's minimal runtime pointers."
    )
    parser.add_argument("ledger", help="TASK_LEDGER.md or PROJECT_STATUS.md")
    args = parser.parse_args(argv)
    ledger = Path(args.ledger).resolve()
    if not ledger.is_file():
        parser.error("ledger path must be an existing file")
    errors = validate_ledger(ledger.read_text(encoding="utf-8"))
    for error in errors:
        print(f"ledger-consistency: blocked: {error}")
    if errors:
        return 1
    print("ledger-consistency: allowed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
