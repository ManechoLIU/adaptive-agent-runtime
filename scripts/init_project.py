#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


COLLABORATIVE_DOCUMENTS = (
    "AGENTS.md",
    "TASK_LEDGER.md",
    "SPEC.md",
    "DESIGN.md",
    "TECHNICAL.md",
    "EVOLUTION.md",
)
DURABLE_DOCUMENTS = (
    "AGENTS.md",
    "TASK_LEDGER.md",
    "MEMORY.md",
    "WIKI_INDEX.md",
    "SKILL.md",
    "SPEC.md",
    "DESIGN.md",
    "TECHNICAL.md",
    "EVOLUTION.md",
)
CORE_DOCUMENTS = ("SPEC.md", "DESIGN.md", "TECHNICAL.md", "EVOLUTION.md")
PROFILES = {
    "collaborative": COLLABORATIVE_DOCUMENTS,
    "durable": DURABLE_DOCUMENTS,
    "core": CORE_DOCUMENTS,
}
TEMPLATES = Path(__file__).resolve().parents[1] / "assets" / "templates"
TEMPLATE_SOURCE_NAMES = {
    "SKILL.md": "PROJECT_SKILL.md",
}
DURABLE_DIRECTORIES = (
    "raw_sources",
    "wiki",
    "logs",
    "logs/ingestion",
)


@dataclass(frozen=True)
class InitReport:
    created: tuple[str, ...]
    skipped: tuple[str, ...]
    created_directories: tuple[str, ...] = ()
    skipped_directories: tuple[str, ...] = ()


def initialize_project(
    root: Path,
    *,
    profile: str = "collaborative",
    include_design: bool = True,
) -> InitReport:
    if not root.exists() or not root.is_dir():
        raise ValueError("project root does not exist or is not a directory")
    if profile not in PROFILES:
        raise ValueError(f"unknown profile: {profile}")

    selected = tuple(
        name for name in PROFILES[profile] if include_design or name != "DESIGN.md"
    )
    created: list[str] = []
    skipped: list[str] = []
    created_directories: list[str] = []
    skipped_directories: list[str] = []
    missing: list[str] = []
    missing_directories: list[str] = []

    for name in selected:
        target = root / name
        if name == "TASK_LEDGER.md":
            legacy_ledger = root / "PROJECT_STATUS.md"
            if target.is_symlink():
                raise ValueError(
                    "document target must not be a symbolic link: TASK_LEDGER.md"
                )
            if target.exists() and not target.is_file():
                raise ValueError(
                    "document target must be a regular file: TASK_LEDGER.md"
                )
            if legacy_ledger.is_symlink():
                raise ValueError(
                    "legacy ledger target must not be a symbolic link: PROJECT_STATUS.md"
                )
            if legacy_ledger.exists():
                if not legacy_ledger.is_file():
                    raise ValueError(
                        "legacy ledger target must be a regular file: PROJECT_STATUS.md"
                    )
                if target.exists():
                    raise ValueError(
                        "both task ledgers exist; reconcile TASK_LEDGER.md and "
                        "PROJECT_STATUS.md before initialization"
                    )
                skipped.append("TASK_LEDGER.md (using existing PROJECT_STATUS.md)")
                continue
        if target.is_symlink():
            raise ValueError(f"document target must not be a symbolic link: {name}")
        if target.exists():
            if not target.is_file():
                raise ValueError(f"document target must be a regular file: {name}")
            skipped.append(name)
            continue
        missing.append(name)

    if profile == "durable":
        for relative_path in DURABLE_DIRECTORIES:
            target = root / relative_path
            if target.is_symlink():
                raise ValueError(
                    f"directory target must not be a symbolic link: {relative_path}"
                )
            if target.exists():
                if not target.is_dir():
                    raise ValueError(
                        f"directory target must be a directory: {relative_path}"
                    )
                skipped_directories.append(relative_path)
                continue
            missing_directories.append(relative_path)

    for relative_path in missing_directories:
        target = root / relative_path
        try:
            target.mkdir()
        except FileExistsError:
            if target.is_symlink():
                raise ValueError(
                    f"directory target must not be a symbolic link: {relative_path}"
                )
            if not target.is_dir():
                raise ValueError(
                    f"directory target must be a directory: {relative_path}"
                )
            skipped_directories.append(relative_path)
        else:
            created_directories.append(relative_path)

    for name in missing:
        target = root / name
        source = TEMPLATES / TEMPLATE_SOURCE_NAMES.get(name, name)
        try:
            with target.open("xb") as output:
                output.write(source.read_bytes())
        except FileExistsError:
            if target.is_symlink():
                raise ValueError(f"document target must not be a symbolic link: {name}")
            if not target.is_file():
                raise ValueError(f"document target must be a regular file: {name}")
            skipped.append(name)
        else:
            created.append(name)

    return InitReport(
        tuple(created),
        tuple(skipped),
        tuple(created_directories),
        tuple(skipped_directories),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="初始化与项目规模相称的 Adaptive Agent Runtime 文档。"
    )
    parser.add_argument("root", nargs="?", default=".", help="项目根目录")
    parser.add_argument(
        "--profile",
        choices=tuple(PROFILES),
        default="collaborative",
        help=(
            "collaborative 创建六个协作文档，durable 再增加上下文治理文档与目录，"
            "core 创建四个核心文档"
        ),
    )
    parser.add_argument("--without-design", action="store_true", help="不创建 DESIGN.md")
    args = parser.parse_args(argv)

    try:
        report = initialize_project(
            Path(args.root).resolve(),
            profile=args.profile,
            include_design=not args.without_design,
        )
    except ValueError as error:
        parser.error(str(error))

    print("created: " + (", ".join(report.created) or "none"))
    print("skipped: " + (", ".join(report.skipped) or "none"))
    print(
        "created directories: "
        + (", ".join(report.created_directories) or "none")
    )
    print(
        "skipped directories: "
        + (", ".join(report.skipped_directories) or "none")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
