#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


RECEIPT_DIRECTORY = "adaptive-delivery"
RECEIPT_FILE = "verification-receipts.json"
FORCE_REASONS = ("output-unavailable", "receipt-unverifiable", "user-requested")


def git_path(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ValueError(result.stderr.strip() or "not a Git repository")
    return result.stdout.strip()


def repository_paths(root: Path) -> list[Path]:
    result = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
        ],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        raise ValueError(result.stderr.decode(errors="replace").strip())
    return [root / os.fsdecode(item) for item in result.stdout.split(b"\0") if item]


def update_path_digest(digest: Any, label: str, path: Path) -> None:
    digest.update(label.encode("utf-8", errors="surrogateescape"))
    digest.update(b"\0")
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        digest.update(b"missing\0")
        return

    digest.update(f"{metadata.st_mode:o}".encode("ascii"))
    digest.update(b"\0")
    if path.is_symlink():
        digest.update(b"symlink\0")
        digest.update(os.fsencode(os.readlink(path)))
        return
    if not path.is_file():
        digest.update(b"non-file\0")
        return

    digest.update(b"file\0")
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)


def candidate_snapshot(root: Path, extra_inputs: Sequence[str] = ()) -> str:
    root = root.resolve()
    digest = hashlib.sha256()
    for path in sorted(repository_paths(root), key=lambda item: os.fsencode(item)):
        update_path_digest(digest, path.relative_to(root).as_posix(), path)
    for raw_path in sorted(extra_inputs):
        path = Path(raw_path)
        if not path.is_absolute():
            path = root / path
        update_path_digest(digest, f"input:{raw_path}", path)
    return digest.hexdigest()


def receipt_path(root: Path) -> Path:
    raw_git_directory = git_path(root, "rev-parse", "--git-dir")
    git_directory = Path(raw_git_directory)
    if not git_directory.is_absolute():
        git_directory = root / git_directory
    return git_directory.resolve() / RECEIPT_DIRECTORY / RECEIPT_FILE


def load_receipts(path: Path) -> dict[str, dict[str, str]]:
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"invalid verification receipt store: {path}")
    return data


def save_receipts(path: Path, receipts: dict[str, dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(receipts, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def command_digest(command: Sequence[str]) -> str:
    encoded = json.dumps(list(command), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def run_verification(
    root: Path,
    *,
    check_id: str,
    command: Sequence[str],
    extra_inputs: Sequence[str] = (),
    force_reason: str | None = None,
) -> int:
    root = root.resolve()
    if not root.is_dir():
        raise ValueError(f"project root does not exist or is not a directory: {root}")
    if not command:
        raise ValueError("verification command is required after --")

    snapshot = candidate_snapshot(root, extra_inputs)
    receipt_store = receipt_path(root)
    receipts = load_receipts(receipt_store)
    command_hash = command_digest(command)
    existing = receipts.get(check_id)

    if (
        force_reason is None
        and existing is not None
        and existing.get("status") == "passed"
        and existing.get("snapshot") == snapshot
        and existing.get("command_hash") == command_hash
    ):
        print(f"verification: reused check_id={check_id} snapshot={snapshot}")
        return 0

    result = subprocess.run(list(command), cwd=root, check=False)
    if result.returncode != 0:
        receipts.pop(check_id, None)
        save_receipts(receipt_store, receipts)
        print(
            f"verification: failed check_id={check_id} exit={result.returncode}",
            file=sys.stderr,
        )
        return result.returncode

    final_snapshot = candidate_snapshot(root, extra_inputs)
    receipt = {
        "status": "passed",
        "snapshot": final_snapshot,
        "command_hash": command_hash,
        "verified_at": datetime.now(timezone.utc).isoformat(),
    }
    if force_reason is not None:
        receipt["force_reason"] = force_reason
    receipts[check_id] = receipt
    save_receipts(receipt_store, receipts)
    print(f"verification: executed check_id={check_id} snapshot={final_snapshot}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="按候选内容快照复用验证证据，阻止无理由重复执行。"
    )
    subparsers = parser.add_subparsers(dest="action", required=True)
    run_parser = subparsers.add_parser("run", help="执行或复用一个验证命令")
    run_parser.add_argument("root", help="Git 项目根目录")
    run_parser.add_argument("--check-id", required=True, help="稳定的验证项标识")
    run_parser.add_argument(
        "--input",
        action="append",
        default=[],
        help="额外纳入快照的环境或配置文件；可重复",
    )
    run_parser.add_argument(
        "--force-reason",
        choices=FORCE_REASONS,
        help="仅在旧输出不可用、收据不可校验或用户明确要求时重跑",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    raw_arguments = list(argv) if argv is not None else sys.argv[1:]
    if "--" in raw_arguments:
        separator = raw_arguments.index("--")
        parser_arguments = raw_arguments[:separator]
        command = raw_arguments[separator + 1 :]
    else:
        parser_arguments = raw_arguments
        command = []
    args = build_parser().parse_args(parser_arguments)
    try:
        return run_verification(
            Path(args.root),
            check_id=args.check_id,
            command=command,
            extra_inputs=args.input,
            force_reason=args.force_reason,
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"verification: error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
