#!/usr/bin/env python3
"""Install one exact Adaptive Delivery revision with a machine-readable manifest."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

UTC = timezone.utc
PRODUCT_NAME = "Adaptive Agent Runtime"
SKILL_ID = "adaptive-delivery"
PRODUCT_SLUG = "adaptive-agent-runtime"
LEGACY_SKILL_IDS = ("adaptive-delivery",)
DEFAULT_AI_BRIDGE_EXECUTABLE = Path("/Applications/AI-Bridge.app/Contents/MacOS/ai-bridge")
DEFAULT_CODEX_HOOKS = Path.home() / ".codex" / "hooks.json"
MANIFEST_NAME = ".adaptive-delivery-install.json"
IMPACTS = {"none", "live_assignments"}



def detect_host_capabilities(
    *,
    codex_executable: str | Path | None = None,
    ai_bridge_executable: str | Path = DEFAULT_AI_BRIDGE_EXECUTABLE,
    hooks_file: str | Path = DEFAULT_CODEX_HOOKS,
) -> dict[str, dict[str, str]]:
    codex_path = Path(codex_executable).expanduser() if codex_executable else None
    if codex_path is None:
        discovered = shutil.which("codex")
        codex_path = Path(discovered) if discovered else None
    bridge_path = Path(ai_bridge_executable).expanduser()
    hooks_path = Path(hooks_file).expanduser()

    desktop = (
        {
            "status": "degraded",
            "adapter": "codex-native",
            "reason": "hook trust/activation must be verified by the host",
        }
        if codex_path is not None and codex_path.is_file()
        else {
            "status": "blocked",
            "adapter": "codex-native",
            "reason": "codex executable not detected",
        }
    )
    if desktop["status"] == "degraded" and hooks_path.is_file():
        desktop["reason"] = "hooks file detected; host trust/activation still requires verification"

    web = (
        {"status": "enabled", "adapter": "ai-bridge", "mode": "local_bridge", "reason": "AI-Bridge executable detected"}
        if bridge_path.is_file()
        else {
            "status": "degraded",
            "adapter": "none",
            "mode": "pure_web_file",
            "reason": "AI-Bridge not detected; local repo/runtime access is unavailable",
        }
    )
    return {
        "core": {"status": "enabled", "adapter": "adaptive-agent-runtime", "reason": "core governance is host-neutral"},
        "desktop_adapter": desktop,
        "web_local_adapter": web,
    }

def _git(source: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(source), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _tracked_files(source: Path) -> list[str]:
    raw = subprocess.run(
        ["git", "-C", str(source), "ls-files", "-z"],
        check=True,
        capture_output=True,
    ).stdout
    return sorted(item.decode("utf-8") for item in raw.split(b"\0") if item)


def _changed_files(source: Path, previous_revision: str | None, revision: str, tracked: list[str]) -> list[str]:
    if not previous_revision:
        return tracked
    exists = subprocess.run(
        ["git", "-C", str(source), "cat-file", "-e", f"{previous_revision}^{{commit}}"],
        capture_output=True,
    )
    if exists.returncode != 0:
        return tracked
    output = _git(source, "diff", "--name-only", f"{previous_revision}..{revision}")
    return sorted(line for line in output.splitlines() if line.strip())


def install_skill(
    source: str | Path,
    target: str | Path,
    *,
    summary: str,
    impact: str,
    stop_condition: str,
    previous_revision: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    source_path = Path(source).expanduser().resolve()
    target_path = Path(target).expanduser().resolve()
    if impact not in IMPACTS:
        raise ValueError("impact must be none or live_assignments")
    if not summary.strip() or not stop_condition.strip():
        raise ValueError("summary and stop_condition are required")
    if source_path == target_path or source_path in target_path.parents:
        raise ValueError("target must be outside the source repository")
    if _git(source_path, "status", "--porcelain=v1", "--untracked-files=no"):
        raise ValueError("source repository must be tracked-clean before installation")

    revision = _git(source_path, "rev-parse", "HEAD")
    tracked = _tracked_files(source_path)
    target_path.mkdir(parents=True, exist_ok=True)
    prior_manifest = _read_manifest(target_path / MANIFEST_NAME)
    prior_files = prior_manifest.get("files", {}) if isinstance(prior_manifest.get("files"), dict) else {}
    prior_revision = previous_revision or str(prior_manifest.get("revision", "")).strip() or None

    tracked_set = set(tracked)
    for relative in prior_files:
        if relative in tracked_set:
            continue
        old = target_path / relative
        if old.is_file() or old.is_symlink():
            old.unlink()

    hashes: dict[str, str] = {}
    for relative in tracked:
        src = source_path / relative
        dst = target_path / relative
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        hashes[relative] = _sha256(dst)

    installed_at = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "product_name": PRODUCT_NAME,
        "skill_id": SKILL_ID,
        "product_slug": PRODUCT_SLUG,
        "legacy_skill_ids": list(LEGACY_SKILL_IDS),
        "revision": revision,
        "previous_revision": prior_revision,
        "installed_at": installed_at,
        "source_root": str(source_path),
        "summary": summary.strip(),
        "impact": impact,
        "stop_condition": stop_condition.strip(),
        "changed_files": _changed_files(source_path, prior_revision, revision, tracked),
        "files": hashes,
    }
    _write_json_atomic(target_path / MANIFEST_NAME, manifest)

    for relative, expected in hashes.items():
        if _sha256(target_path / relative) != expected:
            raise ValueError(f"installed file hash mismatch: {relative}")
    return manifest


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install an exact Adaptive Delivery revision with manifest evidence.")
    parser.add_argument("--source", required=True)
    parser.add_argument("--target", default=str(Path.home() / ".agents" / "skills" / "adaptive-delivery"))
    parser.add_argument("--summary", required=True)
    parser.add_argument("--impact", required=True, choices=sorted(IMPACTS))
    parser.add_argument("--stop-condition", required=True)
    parser.add_argument("--previous-revision")
    args = parser.parse_args(argv)
    try:
        manifest = install_skill(
            args.source,
            args.target,
            summary=args.summary,
            impact=args.impact,
            stop_condition=args.stop_condition,
            previous_revision=args.previous_revision,
        )
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"adaptive-delivery-install: blocked: {error}")
        return 1
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
