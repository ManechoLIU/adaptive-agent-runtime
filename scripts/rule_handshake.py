#!/usr/bin/env python3
"""Machine handshake between installed Adaptive Delivery rules and one project controller."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

try:
    from project_state import adaptive_delivery_state_dir, git_common_dir, repository_root
except ModuleNotFoundError:
    from scripts.project_state import adaptive_delivery_state_dir, git_common_dir, repository_root

UTC = timezone.utc
MANIFEST_NAME = ".adaptive-delivery-install.json"
LEDGER_NAMES = ("TASK_LEDGER.md", "PROJECT_STATUS.md")
CRITICAL_WAKE_FILES = {
    "scripts/assignment_runtime.py",
    "scripts/assignment_lease_guard.py",
    "scripts/control_event_guard.py",
    "scripts/lifecycle_hook.py",
    "scripts/rule_handshake.py",
    "scripts/run_external_agent.mjs",
    "scripts/web_lifecycle_bridge.py",
    "references/agent-delivery-contract.md",
    "references/agent-model-routing.md",
}


def derive_rule_wake_policy(
    status: dict[str, Any],
    *,
    assignment_liveness: dict[str, Any] | None = None,
) -> str | None:
    if str(status.get("state", "")) != "pending_ack":
        return None
    if str(status.get("impact", "")) != "live_assignments":
        return "next_turn"
    changed = {str(item) for item in status.get("changed_files", []) if str(item).strip()}
    live = assignment_liveness if isinstance(assignment_liveness, dict) else {}
    has_live_assignment = any(
        isinstance(value, dict)
        and str(value.get("ledger_state", "")).upper() in {"ACTIVE", "RECOVERING"}
        and str(value.get("state", "")).lower() != "terminal"
        for value in live.values()
    )
    if has_live_assignment and bool(changed & CRITICAL_WAKE_FILES):
        return "immediate"
    return "after_event"


DEFAULT_REGISTRY = Path(
    os.environ.get(
        "AD_CONTROLLER_REGISTRY",
        str(Path.home() / ".codex" / "adaptive-delivery-controllers.json"),
    )
).expanduser()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
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


def installed_skill_root(skill_root: str | Path | None = None) -> Path:
    return Path(skill_root).expanduser().resolve() if skill_root else Path(__file__).resolve().parents[1]


def install_manifest_path(skill_root: str | Path | None = None) -> Path:
    return installed_skill_root(skill_root) / MANIFEST_NAME


def load_install_manifest(skill_root: str | Path | None = None) -> dict[str, Any]:
    return _read_json(install_manifest_path(skill_root))


def installation_integrity_errors(skill_root: str | Path | None, manifest: dict[str, Any]) -> list[str]:
    root = installed_skill_root(skill_root)
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        return ["install manifest files map is missing"]
    errors: list[str] = []
    for relative, expected in sorted(files.items()):
        path = root / str(relative)
        if not path.is_file():
            errors.append(f"installed file missing: {relative}")
            continue
        if _sha256(path) != str(expected):
            errors.append(f"installed file hash mismatch: {relative}")
    return errors


def rule_state_path(repo: str | Path) -> Path:
    return adaptive_delivery_state_dir(repo) / "rule-handshake.json"


def load_rule_state(repo: str | Path) -> dict[str, Any]:
    return _read_json(rule_state_path(repo))


def _ledger_path(repo: str | Path, ledger: str | Path | None = None) -> Path | None:
    if ledger:
        path = Path(ledger).expanduser().resolve()
        return path if path.is_file() else None
    root = repository_root(repo)
    return next((root / name for name in LEDGER_NAMES if (root / name).is_file()), None)


def _ledger_has_revision(path: Path | None, revision: str) -> bool:
    if path is None:
        return False
    for line in path.read_text(encoding="utf-8").splitlines():
        if "规则版本" in line and revision in line:
            return True
    return False


def _manifest_details(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "installed_revision": str(manifest.get("revision", "")).strip() or None,
        "previous_revision": str(manifest.get("previous_revision", "")).strip() or None,
        "summary": str(manifest.get("summary", "")).strip(),
        "impact": str(manifest.get("impact", "")).strip() or "live_assignments",
        "stop_condition": str(manifest.get("stop_condition", "")).strip(),
        "changed_files": list(manifest.get("changed_files", [])) if isinstance(manifest.get("changed_files"), list) else [],
    }


def evaluate_rule_handshake(
    repo: str | Path,
    *,
    ledger: str | Path | None = None,
    skill_root: str | Path | None = None,
    registry_path: str | Path | None = None,
) -> dict[str, Any]:
    del registry_path  # registry is required for ACK identity, not status evaluation
    manifest_path = install_manifest_path(skill_root)
    manifest = load_install_manifest(skill_root)
    if not manifest:
        return {"state": "unmanaged", "blocking": False, "installed_revision": None, "loaded_revision": None}
    details = _manifest_details(manifest)
    impact = details["impact"]
    errors = installation_integrity_errors(skill_root, manifest)
    state = load_rule_state(repo)
    loaded = str(state.get("loaded_revision", "")).strip() or None
    result = {**details, "loaded_revision": loaded, "controller_session_id": state.get("controller_session_id")}
    if errors:
        return {**result, "state": "integrity_error", "blocking": True, "errors": errors}
    installed = details["installed_revision"]
    if not installed or loaded != installed:
        return {**result, "state": "pending_ack", "blocking": impact == "live_assignments"}
    if not _ledger_has_revision(_ledger_path(repo, ledger), installed):
        return {**result, "state": "ledger_stale", "blocking": impact == "live_assignments"}
    return {**result, "state": "current", "blocking": False, "manifest_sha256": _sha256(manifest_path)}


def acknowledge_rule_revision(
    repo: str | Path,
    controller_session_id: str,
    revision: str,
    *,
    skill_root: str | Path | None = None,
    registry_path: str | Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    manifest_path = install_manifest_path(skill_root)
    manifest = load_install_manifest(skill_root)
    if not manifest:
        raise ValueError("installed Adaptive Delivery manifest is missing")
    installed = str(manifest.get("revision", "")).strip()
    if revision != installed:
        raise ValueError("requested revision does not match installed revision")
    errors = installation_integrity_errors(skill_root, manifest)
    if errors:
        raise ValueError("installation integrity failed: " + "; ".join(errors))

    registry = _read_json(Path(registry_path).expanduser().resolve() if registry_path else DEFAULT_REGISTRY)
    registered = registry.get(controller_session_id)
    if not isinstance(registered, str) or not registered.strip():
        raise ValueError("loaded ACK requires a registered controller session")
    try:
        if git_common_dir(registered) != git_common_dir(repo):
            raise ValueError("loaded ACK controller is not registered for this repository")
    except (OSError, ValueError):
        raise ValueError("loaded ACK controller is not registered for this repository") from None

    receipt = {
        "schema_version": 1,
        "installed_revision": installed,
        "loaded_revision": installed,
        "controller_session_id": controller_session_id,
        "acknowledged_at": (now or datetime.now(UTC)).astimezone(UTC).isoformat(),
        "manifest_sha256": _sha256(manifest_path),
    }
    _write_json_atomic(rule_state_path(repo), receipt)
    return receipt


def launch_guard_errors(repo: str | Path, *, skill_root: str | Path | None = None, ledger: str | Path | None = None) -> list[str]:
    status = evaluate_rule_handshake(repo, skill_root=skill_root, ledger=ledger)
    if status.get("blocking"):
        revision = status.get("installed_revision") or "unknown"
        return [f"rule handshake {status.get('state')} for installed revision {revision}"]
    return []


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect or acknowledge Adaptive Delivery rule revision state.")
    sub = parser.add_subparsers(dest="command", required=True)
    status = sub.add_parser("status")
    status.add_argument("--repo", required=True)
    status.add_argument("--ledger")
    status.add_argument("--skill-root")
    ack = sub.add_parser("ack")
    ack.add_argument("--repo", required=True)
    ack.add_argument("--controller-session", required=True)
    ack.add_argument("--revision", required=True)
    ack.add_argument("--skill-root")
    ack.add_argument("--registry")
    guard = sub.add_parser("launch-guard")
    guard.add_argument("--repo", required=True)
    guard.add_argument("--ledger")
    guard.add_argument("--skill-root")
    args = parser.parse_args(argv)
    try:
        if args.command == "status":
            result = evaluate_rule_handshake(args.repo, ledger=args.ledger, skill_root=args.skill_root)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "ack":
            result = acknowledge_rule_revision(
                args.repo,
                args.controller_session,
                args.revision,
                skill_root=args.skill_root,
                registry_path=args.registry,
            )
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0
        errors = launch_guard_errors(args.repo, skill_root=args.skill_root, ledger=args.ledger)
        if errors:
            for error in errors:
                print(f"rule-handshake: blocked: {error}")
            return 1
        print("rule-handshake: allowed")
        return 0
    except (OSError, ValueError) as error:
        print(f"rule-handshake: blocked: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
