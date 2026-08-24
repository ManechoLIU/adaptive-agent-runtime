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


def is_active_row(line: str) -> bool:
    if not line.lstrip().startswith("|"):
        return False
    cells = [cell.strip().strip("`") for cell in line.strip().strip("|").split("|")]
    return "ACTIVE" in cells


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
        active_rows = sum(1 for line in text.splitlines() if is_active_row(line))
        if active_rows > 1:
            errors.append(f"{ledger.name} has more than one ACTIVE work item")

        active_pointer = re.search(r"^- 当前活动项：\s*(.+?)\s*$", text, re.MULTILINE)
        next_pointer = re.search(r"^- 唯一下一项：\s*(.+?)\s*$", text, re.MULTILINE)
        if active_pointer is None:
            warnings.append(f"{ledger.name} has no current activity pointer")
        elif active_pointer.group(1) in {"无", "none", "None"} and active_rows:
            errors.append(
                f"{ledger.name} says no current activity but contains an ACTIVE row"
            )
        elif active_pointer.group(1) not in {"无", "none", "None"} and active_rows != 1:
            errors.append(
                f"{ledger.name} current activity pointer requires exactly one ACTIVE row"
            )
        if next_pointer is None:
            warnings.append(f"{ledger.name} has no unique next-action pointer")

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
        description="检查 Adaptive Delivery 项目的唯一台账、活动项和治理文档链接。"
    )
    parser.add_argument("root", nargs="?", default=".", help="项目根目录")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="把缺少当前活动项或唯一下一项指针视为错误",
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
