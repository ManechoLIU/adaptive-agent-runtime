#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Sequence


PROGRESS_STATES = {"READY", "ACTIVE", "RECOVERING", "VERIFY"}
OPEN_STATES = PROGRESS_STATES | {"PENDING", "BLOCKED"}
STATUS_PATTERN = re.compile(
    r"(?<![A-Z])(" + "|".join(sorted(OPEN_STATES | {"DONE", "SUPERSEDED"})) + r")(?![A-Z])"
)


def open_ledger_package_ids(ledger: Path) -> set[str]:
    identifiers: set[str] = set()
    for line in ledger.read_text(encoding="utf-8").splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = [cell.strip().strip("`") for cell in line.strip().strip("|").split("|")]
        if not cells or not cells[0] or set(cells[0]) <= {"-", ":", " "}:
            continue
        states = set(STATUS_PATTERN.findall(" | ".join(cells[1:])))
        if states & OPEN_STATES:
            identifiers.add(cells[0])
    return identifiers


def validate_snapshot(
    snapshot: dict[str, Any], *, ledger_package_ids: set[str] | None = None
) -> list[str]:
    errors: list[str] = []
    if snapshot.get("project_scope_scan") is not True:
        errors.append("project_scope_scan must be true")
    if not str(snapshot.get("ledger_revision", "")).strip():
        errors.append("ledger_revision is required")

    packages = snapshot.get("open_packages")
    if not isinstance(packages, list) or not packages:
        errors.append("open_packages must enumerate every open ledger package")
        packages = []

    snapshot_ids = {
        str(package.get("id", "")).strip()
        for package in packages
        if isinstance(package, dict) and str(package.get("id", "")).strip()
    }
    if ledger_package_ids is not None:
        missing = sorted(ledger_package_ids - snapshot_ids)
        extra = sorted(snapshot_ids - ledger_package_ids)
        if missing:
            errors.append("scan omitted ledger packages: " + ", ".join(missing))
        if extra:
            errors.append("scan contains packages absent from ledger: " + ", ".join(extra))

    external_conditions: set[str] = set()
    for index, package in enumerate(packages):
        if not isinstance(package, dict):
            errors.append(f"open_packages[{index}] must be an object")
            continue
        package_id = str(package.get("id", "")).strip() or f"index {index}"
        state = str(package.get("state", "")).strip().upper()
        can_progress = package.get("can_progress")
        reason = str(package.get("reason", "")).strip()
        condition = str(package.get("external_condition_id", "")).strip()

        if can_progress is True:
            errors.append(f"{package_id} can still make progress")
        if state in PROGRESS_STATES:
            errors.append(f"{package_id} is still {state}")
        if can_progress is not False:
            errors.append(f"{package_id} must declare can_progress=false")
        if not reason:
            errors.append(f"{package_id} requires an exact blocking reason")
        if not condition:
            errors.append(f"{package_id} requires external_condition_id")
        else:
            external_conditions.add(condition)

    for field in ("live_tasks", "pending_candidates", "controller_actions"):
        value = snapshot.get(field)
        if not isinstance(value, int) or value < 0:
            errors.append(f"{field} must be a non-negative integer")
        elif value:
            errors.append(f"{field} must be zero before blocking")

    if packages and len(external_conditions) != 1:
        errors.append("all open packages must wait on the same external condition")
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
        description="阻止总控在仍有项目级可执行工作时把系统 Goal 标为 blocked。"
    )
    parser.add_argument(
        "snapshot",
        nargs="?",
        default="-",
        help="临时项目存活扫描 JSON；默认从 stdin 读取，不写入项目文件",
    )
    parser.add_argument(
        "--ledger",
        required=True,
        help="当前项目的唯一 TASK_LEDGER.md 或 PROJECT_STATUS.md",
    )
    args = parser.parse_args(argv)
    try:
        snapshot = load_snapshot(args.snapshot)
        ledger = Path(args.ledger).resolve()
        if not ledger.is_file():
            raise ValueError("ledger path must be an existing file")
        ledger_package_ids = open_ledger_package_ids(ledger)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"preblock: invalid snapshot: {error}")
        return 2

    errors = validate_snapshot(snapshot, ledger_package_ids=ledger_package_ids)
    for error in errors:
        print(f"preblock: blocked: {error}")
    if errors:
        return 1
    condition = snapshot["open_packages"][0]["external_condition_id"]
    print(
        "preblock: allowed; project-wide scan found no executable counterexample "
        f"and all open packages wait on {condition}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
