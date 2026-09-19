#!/usr/bin/env python3
"""Stable machine access to the canonical task ledger.

This module does not create a second ledger or persistent projection. It exposes
an in-memory JSON projection of the existing Markdown ledger and provides one
optimistic compare-and-swap update path guarded by the ledger SHA-256.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any, Sequence

try:
    from scripts.control_event_guard import project_wide_dispatch_projection
    from scripts.ledger_consistency_guard import pointer, validate_ledger
except ModuleNotFoundError:
    from control_event_guard import project_wide_dispatch_projection
    from ledger_consistency_guard import pointer, validate_ledger


SCHEMA_VERSION = 1


class StaleLedgerError(RuntimeError):
    """Raised when an update was prepared from an older ledger revision."""


def _require_regular_ledger(path: Path) -> Path:
    ledger = path.expanduser()
    try:
        info = ledger.lstat()
    except FileNotFoundError as error:
        raise ValueError("ledger path must be an existing file") from error
    if stat.S_ISLNK(info.st_mode):
        raise ValueError("ledger path must not be a symbolic link")
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("ledger path must be a regular file")
    return ledger.resolve()


def ledger_sha256(path: Path) -> str:
    ledger = _require_regular_ledger(path)
    return hashlib.sha256(ledger.read_bytes()).hexdigest()


def _work_item(record: dict[str, str]) -> dict[str, str]:
    status = str(record.get("status", "")).strip()
    owner = str(record.get("owner", "")).strip()
    status_cell = str(record.get("status_cell", "")).strip()
    if owner and owner == status_cell:
        owner = re.sub(
            rf"^\s*{re.escape(status)}\s*(?:/|｜)?\s*",
            "",
            owner.replace(chr(96), ""),
            flags=re.IGNORECASE,
        ).strip()
    return {
        "id": str(record.get("id", "")).strip(),
        "status": status,
        "owner": owner,
        "scope": str(record.get("scope", "")).strip(),
        "dependencies_blockers": str(record.get("dependencies_blockers", "")).strip(),
        "acceptance": str(record.get("acceptance", "")).strip(),
        "evidence": str(record.get("evidence", "")).strip(),
        "next_action": str(record.get("next_action", "")).strip(),
    }


def project_ledger(path: Path) -> dict[str, Any]:
    """Return a JSON-serializable view of the existing canonical projection."""
    ledger = _require_regular_ledger(path)
    revision_before = ledger_sha256(ledger)
    canonical = project_wide_dispatch_projection(ledger)
    revision_after = ledger_sha256(ledger)
    if revision_after != revision_before:
        raise StaleLedgerError(
            "ledger changed while building projection; fresh-read and retry"
        )

    text = str(canonical["ledger_text"])
    work_items = [_work_item(record) for record in canonical["records"]]
    validation_errors = validate_ledger(text)
    return {
        "schema_version": SCHEMA_VERSION,
        "projection_kind": "aar_ledger_projection_v1",
        "ledger_path": str(ledger),
        "ledger_sha256": revision_after,
        "ledger_revision": revision_after,
        "valid": not validation_errors,
        "validation_errors": validation_errors,
        "current_goal": pointer(text, "当前 Goal"),
        "next_visible_checkpoint": pointer(text, "下一可见检查点"),
        "current_blockers": pointer(text, "当前阻塞"),
        "rule_revision": pointer(text, "规则版本"),
        "work_items": work_items,
        "task_states": dict(canonical["task_states"]),
        "ready_ids": sorted(canonical["ready_ids"]),
        "open_ids": sorted(canonical["open_ids"]),
        "work_in_flight": dict(sorted(canonical["work_in_flight"].items())),
        "runnable_ids": sorted(canonical["derived_runnable_ids"]),
        "runnable_exclusions": dict(canonical["runnable_exclusions"]),
        "derived_slices": dict(canonical["derived_slices"]),
        "unfinished_work_ids": sorted(canonical["unfinished_work_ids"]),
        "unfinished_child_ids": sorted(canonical["unfinished_child_ids"]),
        "open_parent_ids": sorted(canonical["open_parent_ids"]),
        "parent_of": dict(canonical["parent_of"]),
        "children_by_parent": {
            parent_id: list(children)
            for parent_id, children in canonical["children_by_parent"].items()
        },
        "continuation_debt_ids": sorted(canonical["continuation_debt_ids"]),
        "continuation_debt_labels": list(canonical["continuation_debt_labels"]),
    }


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def apply_ledger(
    path: Path,
    *,
    expected_sha256: str,
    replacement_text: str,
) -> dict[str, Any]:
    """Atomically replace the ledger only when the expected revision is current.

    This is an optimistic CAS contract. A second SHA check immediately before
    os.replace prevents an update prepared from an older canonical ledger from
    silently overwriting newer project facts.
    """
    ledger = _require_regular_ledger(path)
    expected = str(expected_sha256 or "").strip().lower()
    if len(expected) != 64 or any(
        character not in "0123456789abcdef" for character in expected
    ):
        raise ValueError("expected_sha256 must be a 64-character lowercase SHA-256")

    current = ledger.read_bytes()
    current_sha = hashlib.sha256(current).hexdigest()
    if current_sha != expected:
        raise StaleLedgerError(
            f"stale ledger: expected {expected}, current {current_sha}"
        )

    errors = validate_ledger(replacement_text)
    if errors:
        raise ValueError("replacement ledger is invalid: " + "; ".join(errors))

    replacement = replacement_text.encode("utf-8")
    replacement_sha = hashlib.sha256(replacement).hexdigest()
    if replacement_sha == current_sha:
        return {
            "schema_version": SCHEMA_VERSION,
            "record_kind": "aar_ledger_apply_receipt_v1",
            "ledger_path": str(ledger),
            "previous_sha256": current_sha,
            "ledger_sha256": current_sha,
            "changed": False,
        }

    mode = stat.S_IMODE(ledger.stat().st_mode)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{ledger.name}.",
        suffix=".tmp",
        dir=str(ledger.parent),
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        view = memoryview(replacement)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1

        # Re-check after validation and temp-file construction so a concurrent
        # newer ledger cannot normally be replaced by this older prepared write.
        latest_sha = hashlib.sha256(ledger.read_bytes()).hexdigest()
        if latest_sha != expected:
            raise StaleLedgerError(
                f"stale ledger: expected {expected}, current {latest_sha}"
            )

        os.replace(temporary, ledger)
        _fsync_directory(ledger.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary.exists():
            temporary.unlink()

    return {
        "schema_version": SCHEMA_VERSION,
        "record_kind": "aar_ledger_apply_receipt_v1",
        "ledger_path": str(ledger),
        "previous_sha256": current_sha,
        "ledger_sha256": replacement_sha,
        "changed": True,
    }


def _read_replacement(path: str) -> str:
    if path == "-":
        import sys

        return sys.stdin.read()
    return Path(path).read_text(encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Inspect or revision-guard writes to the canonical AAR task ledger."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser(
        "inspect", help="print the ephemeral structured ledger projection as JSON"
    )
    inspect_parser.add_argument("ledger")

    apply_parser = subparsers.add_parser(
        "apply", help="replace the ledger only when the expected SHA-256 is still current"
    )
    apply_parser.add_argument("ledger")
    apply_parser.add_argument("--expected-sha256", required=True)
    apply_parser.add_argument(
        "--replacement",
        required=True,
        help="UTF-8 replacement file, or - to read replacement text from stdin",
    )

    args = parser.parse_args(argv)
    try:
        ledger = Path(args.ledger)
        if args.command == "inspect":
            print(
                json.dumps(
                    project_ledger(ledger),
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        receipt = apply_ledger(
            ledger,
            expected_sha256=args.expected_sha256,
            replacement_text=_read_replacement(args.replacement),
        )
        print(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (OSError, UnicodeError, ValueError, StaleLedgerError) as error:
        parser.exit(1, f"ledger-access: blocked: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
