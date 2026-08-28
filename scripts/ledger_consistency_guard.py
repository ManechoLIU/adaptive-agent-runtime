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


def validate_ledger(text: str) -> list[str]:
    errors: list[str] = []
    if RUNTIME_CAPACITY_POINTER.search(text):
        errors.append(
            "runtime capacity and live assignment counts belong in the ephemeral control receipt, not the ledger header"
        )
    records = task_records(text)
    identifiers = [record["id"] for record in records]
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
    elif open_ids and not referenced_ids(checkpoint, open_ids):
        errors.append("next visible checkpoint must reference at least one open task ID")

    for label in ("当前阻塞", "规则版本"):
        if not pointer(text, label):
            errors.append(f"{label} is required")

    for record in records:
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
