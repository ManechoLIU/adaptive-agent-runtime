#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


COLLABORATIVE_DOCUMENTS = (
    "AGENTS.md",
    "PROJECT_STATUS.md",
    "SPEC.md",
    "DESIGN.md",
    "TECHNICAL.md",
    "EVOLUTION.md",
)
CORE_DOCUMENTS = ("SPEC.md", "DESIGN.md", "TECHNICAL.md", "EVOLUTION.md")
PROFILES = {
    "collaborative": COLLABORATIVE_DOCUMENTS,
    "core": CORE_DOCUMENTS,
}
TEMPLATES = Path(__file__).resolve().parents[1] / "assets" / "templates"


@dataclass(frozen=True)
class InitReport:
    created: tuple[str, ...]
    skipped: tuple[str, ...]


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
    missing: list[str] = []

    for name in selected:
        target = root / name
        if target.is_symlink():
            raise ValueError(f"document target must not be a symbolic link: {name}")
        if target.exists():
            if not target.is_file():
                raise ValueError(f"document target must be a regular file: {name}")
            skipped.append(name)
            continue
        missing.append(name)

    for name in missing:
        target = root / name
        try:
            with target.open("xb") as output:
                output.write((TEMPLATES / name).read_bytes())
        except FileExistsError:
            if target.is_symlink():
                raise ValueError(f"document target must not be a symbolic link: {name}")
            if not target.is_file():
                raise ValueError(f"document target must be a regular file: {name}")
            skipped.append(name)
        else:
            created.append(name)

    return InitReport(tuple(created), tuple(skipped))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="初始化与项目规模相称的 Adaptive Delivery 文档。"
    )
    parser.add_argument("root", nargs="?", default=".", help="项目根目录")
    parser.add_argument(
        "--profile",
        choices=tuple(PROFILES),
        default="collaborative",
        help="collaborative 创建六文件，core 创建四个核心文档",
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
