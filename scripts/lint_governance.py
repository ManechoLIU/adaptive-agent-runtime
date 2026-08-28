#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Sequence
from urllib.parse import unquote


LEDGER_NAMES = ("TASK_LEDGER.md", "PROJECT_STATUS.md")
GOVERNANCE_NAMES = (
    "AGENTS.md",
    "TASK_LEDGER.md",
    "PROJECT_STATUS.md",
    "MEMORY.md",
    "WIKI_INDEX.md",
    "SPEC.md",
    "DESIGN.md",
    "TECHNICAL.md",
    "EVOLUTION.md",
)
LINK_PATTERN = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
STATUS_PATTERN = re.compile(
    r"(?<![A-Z])(PENDING|READY|ACTIVE|RECOVERING|VERIFY|BLOCKED|DONE|SUPERSEDED)(?![A-Z])"
)
TASK_ID_HEADERS = {"ID", "功能组"}


def markdown_cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def is_separator_row(cells: list[str]) -> bool:
    return bool(cells) and all(cell and set(cell) <= {"-", ":", " "} for cell in cells)


def task_identifier(cell: str) -> str:
    code_span = re.match(r"^`([^`]+)`", cell.strip())
    return code_span.group(1).strip() if code_span else cell.strip().strip("`")


def task_records(text: str) -> list[dict[str, str]]:
    """Return normalized records only from declared task tables.

    A task table must have an ID or 功能组 first column, a status column, and a
    Markdown separator row. Status words in evidence or prose cells are ignored.
    """
    lines = text.splitlines()
    rows: list[dict[str, str]] = []
    index = 0
    while index + 1 < len(lines):
        if not lines[index].lstrip().startswith("|"):
            index += 1
            continue
        header = markdown_cells(lines[index])
        separator = markdown_cells(lines[index + 1])
        if (
            not header
            or header[0] not in TASK_ID_HEADERS
            or not is_separator_row(separator)
        ):
            index += 1
            continue
        status_index = next(
            (position for position, cell in enumerate(header) if "状态" in cell),
            None,
        )
        if status_index is None:
            index += 1
            continue
        index += 2
        while index < len(lines) and lines[index].lstrip().startswith("|"):
            cells = markdown_cells(lines[index])
            if len(cells) > status_index and cells[0]:
                status = STATUS_PATTERN.search(cells[status_index])
                if status:
                    values = {
                        header[position]: cells[position]
                        for position in range(min(len(header), len(cells)))
                    }
                    owner = next(
                        (
                            value
                            for key, value in values.items()
                            if "负责人" in key or "owner" in key.lower()
                        ),
                        "",
                    )
                    next_action = next(
                        (
                            value
                            for key, value in values.items()
                            if "下一步" in key
                        ),
                        next(
                            (
                                value
                                for key, value in values.items()
                                if "证据" in key
                            ),
                            cells[-1] if cells else "",
                        ),
                    )
                    rows.append(
                        {
                            "id": task_identifier(cells[0]),
                            "status": status.group(1),
                            "status_cell": cells[status_index],
                            "owner": owner,
                            "next_action": next_action,
                            "row": " | ".join(cells),
                        }
                    )
            index += 1
    return rows


def task_rows(text: str) -> list[tuple[str, str]]:
    """Return the stable (task ID, status) compatibility view."""
    return [(record["id"], record["status"]) for record in task_records(text)]


def active_row_ids(text: str) -> list[str]:
    return [identifier for identifier, status in task_rows(text) if status == "ACTIVE"]


def work_in_flight_ids(text: str) -> list[str]:
    return [
        identifier
        for identifier, status in task_rows(text)
        if status in {"ACTIVE", "RECOVERING"}
    ]


def task_row_ids(text: str) -> list[str]:
    return [identifier for identifier, _ in task_rows(text)]


def pointer_ids(value: str) -> set[str]:
    return {
        item.strip().strip("`")
        for item in re.split(r"[、,，/]", value)
        if item.strip()
    }


def locate_ledger(root: Path) -> tuple[Path | None, list[str]]:
    existing = [root / name for name in LEDGER_NAMES if (root / name).is_file()]
    if len(existing) > 1:
        return None, [
            "both TASK_LEDGER.md and PROJECT_STATUS.md exist; reconcile to one control plane"
        ]
    if not existing:
        return None, ["no task ledger found"]
    return existing[0], []


def local_link_target(source: Path, raw_target: str) -> Path | None:
    target = raw_target.strip()
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1]
    target = unquote(target.split("#", 1)[0])
    if not target or target.startswith(("#", "http://", "https://", "mailto:")):
        return None
    return (source.parent / target).resolve()


def lint_project(root: Path, *, strict: bool = False) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    ledger, ledger_errors = locate_ledger(root)
    errors.extend(ledger_errors)

    if ledger is not None:
        text = ledger.read_text(encoding="utf-8")
        active_ids = work_in_flight_ids(text)
        active_rows = len(active_ids)
        task_ids = task_row_ids(text)
        duplicate_ids = sorted(
            identifier for identifier in set(task_ids) if task_ids.count(identifier) > 1
        )
        if duplicate_ids:
            warnings.append(
                f"{ledger.name} repeats task IDs across task tables: "
                + ", ".join(duplicate_ids)
            )

        active_pointer = re.search(r"^- 当前活动项：\s*(.+?)\s*$", text, re.MULTILINE)
        if active_pointer is not None and active_pointer.group(1) in {"无", "none", "None"} and active_rows:
            errors.append(
                f"{ledger.name} says no current activity but contains ACTIVE/RECOVERING rows"
            )
        elif active_pointer is not None and active_pointer.group(1) not in {"无", "none", "None"} and (
            not active_rows or pointer_ids(active_pointer.group(1)) != set(active_ids)
        ):
            errors.append(
                f"{ledger.name} current activity pointer does not match ACTIVE/RECOVERING rows"
            )
        required_pointers = {
            "current Goal": r"^- 当前 Goal：\s*(.+?)\s*$",
            "next visible checkpoint": r"^- 下一可见检查点：\s*(.+?)\s*$",
            "current blockers": r"^- 当前阻塞：\s*(.+?)\s*$",
            "governance revision": r"^- 规则版本：\s*(.+?)\s*$",
        }
        for label, pattern in required_pointers.items():
            match = re.search(pattern, text, re.MULTILINE)
            if match is None or match.group(1).strip() in {"", "-"}:
                warnings.append(f"{ledger.name} has no {label} pointer")

    for name in GOVERNANCE_NAMES:
        source = root / name
        if not source.is_file():
            continue
        text = source.read_text(encoding="utf-8")
        for raw_target in LINK_PATTERN.findall(text):
            target = local_link_target(source, raw_target)
            if target is not None and not target.exists():
                errors.append(f"broken local link in {name}: {raw_target}")

    if strict:
        errors.extend(warnings)
        warnings = []
    return errors, warnings


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="检查 Adaptive Delivery 项目的唯一台账、当前执行波次和治理文档链接。"
    )
    parser.add_argument("root", nargs="?", default=".", help="项目根目录")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="把缺少最小运行指针视为错误",
    )
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    if not root.exists() or not root.is_dir():
        parser.error("project root does not exist or is not a directory")

    errors, warnings = lint_project(root, strict=args.strict)
    for warning in warnings:
        print(f"warning: {warning}")
    for error in errors:
        print(f"error: {error}")
    if errors:
        return 1
    print("governance: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
