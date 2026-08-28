#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Sequence


REQUIRED_CONTRACT_FIELDS = (
    "event_id",
    "event_type",
    "primary_task",
    "candidate_revision",
    "terminal_receipt",
)


def string_list(value: Any, field: str, errors: list[str]) -> list[str]:
    if not isinstance(value, list):
        errors.append(f"{field} must be a list")
        return []
    result = [str(item).strip() for item in value]
    if any(not item for item in result) or len(set(result)) != len(result):
        errors.append(f"{field} must contain unique non-empty values")
    return result


def validate_contract(contract: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for field in REQUIRED_CONTRACT_FIELDS:
        if not str(contract.get(field, "")).strip():
            errors.append(f"{field} is required")
    allowed_actions = string_list(contract.get("allowed_actions"), "allowed_actions", errors)
    string_list(contract.get("allowed_files"), "allowed_files", errors)
    if not allowed_actions:
        errors.append("allowed_actions must not be empty")
    return errors


def classify_append(
    contract: dict[str, Any], proposed: dict[str, Any]
) -> tuple[str, list[str]]:
    errors = validate_contract(contract)
    if errors:
        return "INVALID", errors

    reasons: list[str] = []
    action = str(proposed.get("action", "")).strip()
    task = str(proposed.get("primary_task", "")).strip()
    revision = str(proposed.get("candidate_revision", "")).strip()
    files = proposed.get("files")
    if not action:
        return "INVALID", ["proposed.action is required"]
    if not task:
        return "INVALID", ["proposed.primary_task is required"]
    if not revision:
        return "INVALID", ["proposed.candidate_revision is required"]
    if not isinstance(files, list):
        return "INVALID", ["proposed.files must be a list"]

    if task != str(contract["primary_task"]).strip():
        reasons.append("different primary task")
    if revision != str(contract["candidate_revision"]).strip():
        reasons.append("different candidate revision")
    if action not in set(contract["allowed_actions"]):
        reasons.append("action is outside allowed_actions")

    allowed_files = set(contract["allowed_files"])
    proposed_files = {str(item).strip() for item in files if str(item).strip()}
    if len(proposed_files) != len(files):
        return "INVALID", ["proposed.files must contain unique non-empty paths"]
    unexpected_files = sorted(proposed_files - allowed_files)
    if unexpected_files:
        reasons.append("files are outside allowed_files: " + ", ".join(unexpected_files))
    if proposed.get("required_to_close_current_state") is not True:
        reasons.append("action is not required to leave current state consistent and recoverable")
    if proposed.get("starts_new_implementation") is True:
        reasons.append("new implementation belongs to a new event")
    if proposed.get("waits_for_future_input") is True:
        reasons.append("future input must trigger a new event")

    return ("QUEUE_NEXT_EVENT", reasons) if reasons else ("SAME_EVENT", [])


def load_json(path: str) -> dict[str, Any]:
    if path == "-":
        data = json.load(sys.stdin)
    else:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("input must be a JSON object")
    return data


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Classify one proposed controller action before appending it to a short event."
    )
    parser.add_argument("input", nargs="?", default="-", help="JSON file or stdin")
    args = parser.parse_args(argv)
    try:
        payload = load_json(args.input)
        contract = payload.get("event_contract")
        proposed = payload.get("proposed_action")
        if not isinstance(contract, dict) or not isinstance(proposed, dict):
            raise ValueError("event_contract and proposed_action must be objects")
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"event-scope: INVALID: {error}")
        return 2

    decision, reasons = classify_append(contract, proposed)
    if decision == "SAME_EVENT":
        print("event-scope: SAME_EVENT")
        return 0
    print(f"event-scope: {decision}: " + "; ".join(reasons))
    return 3 if decision == "QUEUE_NEXT_EVENT" else 2


if __name__ == "__main__":
    raise SystemExit(main())
