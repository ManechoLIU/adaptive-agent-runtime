#!/usr/bin/env python3
"""Machine handshake between installed Adaptive Agent Runtime rules and one project controller."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
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
    "scripts/controller_target_guard.py",
    "scripts/lifecycle_hook.py",
    "scripts/rule_handshake.py",
    "scripts/run_external_agent.mjs",
    "scripts/web_lifecycle_bridge.py",
    "references/agent-delivery-contract.md",
    "references/agent-model-routing.md",
}

LIVE_E2E_CRITICAL_FILES = CRITICAL_WAKE_FILES | {
    "scripts/controller_health.py",
    "scripts/terminal_continuation.py",
    "scripts/web_agent_health_supervisor.py",
    "scripts/web_reentry_adapter.py",
}
LIVE_E2E_ACCEPTANCE_NAME = "runtime-live-e2e-acceptance.json"


def derive_rule_wake_policy(
    status: dict[str, Any],
    *,
    assignment_liveness: dict[str, Any] | None = None,
) -> str | None:
    state = str(status.get("state", ""))
    if state == "pending_live_e2e":
        return "after_event"
    if state != "pending_ack":
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


def live_e2e_acceptance_path(repo: str | Path) -> Path:
    return adaptive_delivery_state_dir(repo) / LIVE_E2E_ACCEPTANCE_NAME


def load_live_e2e_acceptance(repo: str | Path) -> dict[str, Any]:
    return _read_json(live_e2e_acceptance_path(repo))


def _parse_utc_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _path_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (OSError, ValueError):
        return False


def _live_e2e_acceptance_errors(
    repo: str | Path,
    acceptance: dict[str, Any],
    *,
    manifest_path: Path,
    installed_revision: str,
    controller_session_id: str,
    registry_path: str | Path | None,
    validate_current_target: bool = False,
) -> list[str]:
    errors: list[str] = []
    if acceptance.get("status") != "accepted":
        errors.append("live E2E acceptance status is not accepted")
    if str(acceptance.get("installed_revision") or "").strip() != installed_revision:
        errors.append("live E2E acceptance revision does not match installed revision")
    if str(acceptance.get("controller_session_id") or "").strip() != controller_session_id:
        errors.append("live E2E acceptance Controller does not match loaded Controller")
    if str(acceptance.get("manifest_sha256") or "").strip() != _sha256(manifest_path):
        errors.append("live E2E acceptance manifest hash does not match installed manifest")

    state_root = adaptive_delivery_state_dir(repo).resolve()
    wake_path_text = str(acceptance.get("wake_evidence_path") or "").strip()
    cycle_path_text = str(acceptance.get("cycle_evidence_path") or "").strip()
    wake_path = Path(wake_path_text).expanduser() if wake_path_text else None
    cycle_path = Path(cycle_path_text).expanduser() if cycle_path_text else None
    if wake_path is None or not wake_path.is_file() or not _path_within(wake_path, state_root):
        errors.append("live E2E wake evidence is missing or outside project runtime state")
        wake = {}
    else:
        wake = _read_json(wake_path)
        if str(acceptance.get("wake_evidence_sha256") or "").strip() != _sha256(wake_path):
            errors.append("live E2E wake evidence hash mismatch")
    if cycle_path is None or not cycle_path.is_file() or not _path_within(cycle_path, state_root):
        errors.append("live E2E cycle evidence is missing or outside project runtime state")
        cycle = {}
    else:
        cycle = _read_json(cycle_path)
        if str(acceptance.get("cycle_evidence_sha256") or "").strip() != _sha256(cycle_path):
            errors.append("live E2E cycle evidence hash mismatch")

    selected_host = str(wake.get("selected_host") or "").strip()
    wake_target = str(wake.get("execution_target_session_id") or "").strip()
    wake_generation = wake.get("target_generation")
    wake_ownership_generation = wake.get("ownership_generation")
    wake_completed_ms = wake.get("completed_at_unix_ms")
    if wake.get("result") != "CONFIRMED":
        errors.append("live E2E wake evidence is not confirmed")
    if str(wake.get("controller_id") or "").strip() != controller_session_id:
        errors.append("live E2E wake evidence Controller mismatch")
    if selected_host not in {"web", "desktop_codex"}:
        errors.append("live E2E wake evidence host is invalid")
    if not wake_target or not isinstance(wake_generation, int) or isinstance(wake_generation, bool):
        errors.append("live E2E wake evidence target or generation is missing")
    if (
        not isinstance(wake_ownership_generation, int)
        or isinstance(wake_ownership_generation, bool)
        or wake_ownership_generation <= 0
    ):
        errors.append("live E2E wake evidence ownership generation is missing")

    registry = _read_json(Path(registry_path).expanduser().resolve() if registry_path else DEFAULT_REGISTRY)
    registered_repo = registry.get(controller_session_id)
    try:
        same_repo = isinstance(registered_repo, str) and git_common_dir(registered_repo) == git_common_dir(repo)
    except (OSError, ValueError):
        same_repo = False
    if not same_repo:
        errors.append("live E2E acceptance Controller is not uniquely registered for this repository")
    if validate_current_target:
        targets = registry.get("__controller_targets__")
        controller_targets = targets.get(controller_session_id) if isinstance(targets, dict) else None
        target_record = controller_targets.get(selected_host) if isinstance(controller_targets, dict) else None
        if isinstance(target_record, dict):
            if target_record.get("status") != "active":
                errors.append("live E2E current target is not active")
            if str(target_record.get("session_id") or "").strip() != wake_target:
                errors.append("live E2E wake target does not match current target")
            if target_record.get("generation") != wake_generation:
                errors.append("live E2E wake generation does not match current target generation")
        elif selected_host:
            sessions = registry.get("__controller_sessions__")
            controller_sessions = sessions.get(controller_session_id) if isinstance(sessions, dict) else None
            aliases = controller_sessions.get(selected_host) if isinstance(controller_sessions, dict) else None
            aliases = [aliases] if isinstance(aliases, str) else aliases
            bound = [str(value).strip() for value in aliases or [] if isinstance(value, str) and value.strip()]
            if bound or wake_target != controller_session_id or wake_generation != 0:
                errors.append("live E2E wake lacks an explicit current target")
        ownerships = registry.get("__controller_execution_ownership__")
        ownership = (
            ownerships.get(controller_session_id)
            if isinstance(ownerships, dict)
            else None
        )
        if not isinstance(ownership, dict):
            errors.append("live E2E current execution ownership is missing")
        else:
            if str(ownership.get("active_host") or "").strip() != selected_host:
                errors.append("live E2E wake host does not match current execution ownership")
            if (
                str(ownership.get("execution_target_session_id") or "").strip()
                != wake_target
            ):
                errors.append("live E2E wake target does not match current execution ownership")
            if ownership.get("generation") != wake_ownership_generation:
                errors.append("live E2E wake ownership generation does not match current ownership generation")

    if cycle.get("record_kind") != "controller_cycle_evidence":
        errors.append("live E2E cycle evidence record kind is invalid")
    if str(cycle.get("controller_id") or "").strip() != controller_session_id:
        errors.append("live E2E cycle evidence Controller mismatch")
    if cycle.get("terminal_status") != "CLOSED" or cycle.get("validation_errors") != []:
        errors.append("live E2E cycle evidence is not a clean CLOSED cycle")
    cycle_time = _parse_utc_timestamp(cycle.get("recorded_at"))
    wake_time = (
        datetime.fromtimestamp(wake_completed_ms / 1000, tz=UTC)
        if isinstance(wake_completed_ms, int) and not isinstance(wake_completed_ms, bool)
        else None
    )
    if cycle_time is None or wake_time is None or cycle_time < wake_time:
        errors.append("live E2E CLOSED cycle does not occur after confirmed wake")
    return errors


def _live_e2e_changed_files(
    manifest: dict[str, Any],
    *,
    effective_impact: str,
    changed_files: list[str],
) -> list[str]:
    if effective_impact != "live_assignments":
        return []
    return sorted(set(changed_files) & LIVE_E2E_CRITICAL_FILES)


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


def _unacked_change_impact(manifest: dict[str, Any], loaded_revision: str | None) -> tuple[str, list[str]]:
    declared = str(manifest.get("impact", "")).strip() or "live_assignments"
    installed = str(manifest.get("revision", "")).strip()
    if not loaded_revision or not installed or loaded_revision == installed:
        changed = list(manifest.get("changed_files", [])) if isinstance(manifest.get("changed_files"), list) else []
        return declared, changed
    source_text = str(manifest.get("source_root", "")).strip()
    if not source_text:
        return "live_assignments", []
    source = Path(source_text).expanduser().resolve()
    try:
        completed = subprocess.run(
            ["git", "-C", str(source), "diff", "--name-only", f"{loaded_revision}..{installed}"],
            check=True, capture_output=True, text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "live_assignments", []
    changed = sorted(line.strip() for line in completed.stdout.splitlines() if line.strip())
    return ("live_assignments" if set(changed) & CRITICAL_WAKE_FILES else "none"), changed


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
    manifest_path = install_manifest_path(skill_root)
    manifest = load_install_manifest(skill_root)
    if not manifest:
        return {"state": "unmanaged", "blocking": False, "installed_revision": None, "loaded_revision": None}
    details = _manifest_details(manifest)
    errors = installation_integrity_errors(skill_root, manifest)
    state = load_rule_state(repo)
    loaded = str(state.get("loaded_revision", "")).strip() or None
    effective_impact, unacked_changed_files = _unacked_change_impact(manifest, loaded)
    result = {
        **details,
        "effective_impact": effective_impact,
        "unacked_changed_files": unacked_changed_files,
        "loaded_revision": loaded,
        "controller_session_id": state.get("controller_session_id"),
    }
    if errors:
        return {**result, "state": "integrity_error", "blocking": True, "errors": errors}
    installed = details["installed_revision"]
    if not installed or loaded != installed:
        return {**result, "state": "pending_ack", "blocking": effective_impact == "live_assignments"}
    if not _ledger_has_revision(_ledger_path(repo, ledger), installed):
        return {**result, "state": "ledger_stale", "blocking": effective_impact == "live_assignments"}
    derived_live_e2e_changed_files = _live_e2e_changed_files(
        manifest, effective_impact=effective_impact, changed_files=unacked_changed_files
    )
    state_tracks_live_e2e = "live_e2e_required" in state
    live_e2e_required = (
        bool(state.get("live_e2e_required"))
        if state_tracks_live_e2e
        else bool(derived_live_e2e_changed_files)
    )
    state_changed_files = state.get("live_e2e_required_changed_files")
    live_e2e_changed_files = (
        sorted({str(item) for item in state_changed_files if str(item).strip()})
        if live_e2e_required and isinstance(state_changed_files, list)
        else derived_live_e2e_changed_files
    )
    live_e2e_required_since_revision = (
        str(state.get("live_e2e_required_since_revision") or "").strip() or installed
        if live_e2e_required
        else None
    )
    if live_e2e_required:
        deferred_revision = str(state.get("live_e2e_deferred_revision") or "").strip()
        deferred_reason = str(state.get("live_e2e_deferred_reason") or "").strip()
        deferred_controller = str(state.get("live_e2e_deferred_by_controller") or "").strip()
        if (
            deferred_revision == installed
            and deferred_reason
            and deferred_controller == str(state.get("controller_session_id") or "").strip()
        ):
            return {
                **result,
                "state": "current_deferred_live_e2e",
                "blocking": False,
                "live_e2e_required": True,
                "live_e2e_changed_files": live_e2e_changed_files,
                "live_e2e_required_since_revision": live_e2e_required_since_revision,
                "live_e2e_deferred_revision": deferred_revision,
                "live_e2e_deferred_reason": deferred_reason,
                "live_e2e_deferred_at": state.get("live_e2e_deferred_at"),
                "manifest_sha256": _sha256(manifest_path),
            }
        acceptance = load_live_e2e_acceptance(repo)
        acceptance_errors = _live_e2e_acceptance_errors(
            repo,
            acceptance,
            manifest_path=manifest_path,
            installed_revision=installed,
            controller_session_id=str(state.get("controller_session_id") or "").strip(),
            registry_path=registry_path,
        )
        if acceptance_errors:
            return {
                **result,
                "state": "pending_live_e2e",
                "blocking": True,
                "live_e2e_changed_files": live_e2e_changed_files,
                "live_e2e_required_since_revision": live_e2e_required_since_revision,
                "live_e2e_errors": acceptance_errors,
            }
    return {**result, "state": "current", "blocking": False, "manifest_sha256": _sha256(manifest_path)}


def accept_live_e2e(
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
        raise ValueError("installed Adaptive Agent Runtime manifest is missing")
    installed = str(manifest.get("revision") or "").strip()
    if revision != installed:
        raise ValueError("live E2E revision does not match installed revision")
    errors = installation_integrity_errors(skill_root, manifest)
    if errors:
        raise ValueError("installation integrity failed: " + "; ".join(errors))

    state = load_rule_state(repo)
    if str(state.get("loaded_revision") or "").strip() != installed:
        raise ValueError("live E2E requires exact installed revision to be ACKed first")
    if str(state.get("controller_session_id") or "").strip() != controller_session_id:
        raise ValueError("live E2E Controller does not match the ACKed Controller")
    if not _ledger_has_revision(_ledger_path(repo), installed):
        raise ValueError("live E2E requires the project ledger to load the exact installed revision")

    registry_file = Path(registry_path).expanduser().resolve() if registry_path else DEFAULT_REGISTRY
    registry = _read_json(registry_file)
    registered = registry.get(controller_session_id)
    try:
        same_repo = isinstance(registered, str) and git_common_dir(registered) == git_common_dir(repo)
    except (OSError, ValueError):
        same_repo = False
    if not same_repo:
        raise ValueError("live E2E requires the unique registered Controller for this repository")

    state_root = adaptive_delivery_state_dir(repo)
    wake_source = state_root / "controller-wake-receipt.json"
    wake = _read_json(wake_source)
    if not wake:
        raise ValueError("live E2E requires a confirmed Controller wake receipt")
    wake_ms = wake.get("completed_at_unix_ms")
    if not isinstance(wake_ms, int) or isinstance(wake_ms, bool):
        raise ValueError("live E2E wake receipt has no machine completion time")
    acknowledged_at = _parse_utc_timestamp(state.get("acknowledged_at"))
    if acknowledged_at is None:
        raise ValueError("live E2E requires a machine timestamp for the exact rule ACK")
    wake_time = datetime.fromtimestamp(wake_ms / 1000, tz=UTC)
    if wake_time < acknowledged_at:
        raise ValueError("live E2E confirmed wake predates the exact rule ACK")

    cycle_dir = state_root / "controller-cycle-evidence"
    candidates: list[tuple[datetime, Path]] = []
    if cycle_dir.is_dir():
        for path in cycle_dir.glob("*.json"):
            cycle = _read_json(path)
            recorded = _parse_utc_timestamp(cycle.get("recorded_at"))
            if (
                cycle.get("record_kind") == "controller_cycle_evidence"
                and str(cycle.get("controller_id") or "").strip() == controller_session_id
                and cycle.get("terminal_status") == "CLOSED"
                and cycle.get("validation_errors") == []
                and recorded is not None
                and recorded >= datetime.fromtimestamp(wake_ms / 1000, tz=UTC)
            ):
                candidates.append((recorded, path))
    if not candidates:
        raise ValueError("live E2E requires a clean CLOSED Controller cycle after confirmed wake")
    _recorded, cycle_path = max(candidates, key=lambda item: item[0])

    evidence_dir = state_root / "runtime-live-e2e-evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    wake_snapshot = evidence_dir / f"{installed}.wake.json"
    encoded_wake = json.dumps(wake, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    fd, temporary = tempfile.mkstemp(
        prefix=f"{installed}.wake.", suffix=".tmp", dir=evidence_dir
    )
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(encoded_wake)
            handle.flush()
            os.fsync(handle.fileno())
        provisional = {
            "schema_version": 1,
            "status": "accepted",
            "installed_revision": installed,
            "controller_session_id": controller_session_id,
            "accepted_at": (now or datetime.now(UTC)).astimezone(UTC).isoformat(),
            "manifest_sha256": _sha256(manifest_path),
            "wake_evidence_path": str(temporary_path.resolve()),
            "wake_evidence_sha256": _sha256(temporary_path),
            "cycle_evidence_path": str(cycle_path.resolve()),
            "cycle_evidence_sha256": _sha256(cycle_path),
        }
        acceptance_errors = _live_e2e_acceptance_errors(
            repo,
            provisional,
            manifest_path=manifest_path,
            installed_revision=installed,
            controller_session_id=controller_session_id,
            registry_path=registry_file,
            validate_current_target=True,
        )
        if acceptance_errors:
            raise ValueError("live E2E acceptance failed: " + "; ".join(acceptance_errors))
        if wake_snapshot.exists():
            if wake_snapshot.read_text(encoding="utf-8") != encoded_wake:
                raise ValueError("immutable live E2E wake snapshot already exists with different content")
            temporary_path.unlink()
        else:
            os.replace(temporary_path, wake_snapshot)
        receipt = {
            **provisional,
            "wake_evidence_path": str(wake_snapshot.resolve()),
            "wake_evidence_sha256": _sha256(wake_snapshot),
        }
        final_errors = _live_e2e_acceptance_errors(
            repo,
            receipt,
            manifest_path=manifest_path,
            installed_revision=installed,
            controller_session_id=controller_session_id,
            registry_path=registry_file,
            validate_current_target=True,
        )
        if final_errors:
            raise ValueError("live E2E acceptance failed: " + "; ".join(final_errors))
        _write_json_atomic(live_e2e_acceptance_path(repo), receipt)
        updated_state = {
            **state,
            "live_e2e_required": False,
            "live_e2e_required_since_revision": None,
            "live_e2e_required_changed_files": [],
            "live_e2e_accepted_revision": installed,
            "live_e2e_accepted_at": receipt["accepted_at"],
        }
        _write_json_atomic(rule_state_path(repo), updated_state)
        return receipt
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def defer_live_e2e(
    repo: str | Path,
    controller_session_id: str,
    revision: str,
    *,
    reason: str,
    skill_root: str | Path | None = None,
    registry_path: str | Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    reason = str(reason or "").strip()
    if not reason:
        raise ValueError("live E2E deferral reason is required")
    manifest = load_install_manifest(skill_root)
    if not manifest:
        raise ValueError("installed Adaptive Agent Runtime manifest is missing")
    installed = str(manifest.get("revision", "")).strip()
    if revision != installed:
        raise ValueError("requested revision does not match installed revision")
    state = load_rule_state(repo)
    if str(state.get("loaded_revision") or "").strip() != installed:
        raise ValueError("live E2E deferral requires the installed revision to be loaded first")
    if not bool(state.get("live_e2e_required")):
        raise ValueError("live E2E deferral requires pending live E2E debt")
    if str(state.get("controller_session_id") or "").strip() != controller_session_id:
        raise ValueError("live E2E deferral requires the loaded Controller")

    registry_file = Path(registry_path).expanduser().resolve() if registry_path else DEFAULT_REGISTRY
    registry = _read_json(registry_file)
    registered = registry.get(controller_session_id)
    if not isinstance(registered, str) or not registered.strip():
        raise ValueError("live E2E deferral requires a registered controller session")
    try:
        if git_common_dir(registered) != git_common_dir(repo):
            raise ValueError("live E2E deferral Controller is not registered for this repository")
    except (OSError, ValueError):
        raise ValueError("live E2E deferral Controller is not registered for this repository") from None

    deferred_at = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
    updated = {
        **state,
        "live_e2e_deferred_revision": installed,
        "live_e2e_deferred_by_controller": controller_session_id,
        "live_e2e_deferred_reason": reason,
        "live_e2e_deferred_at": deferred_at,
    }
    _write_json_atomic(rule_state_path(repo), updated)
    return {
        "schema_version": 1,
        "status": "deferred",
        "installed_revision": installed,
        "controller_session_id": controller_session_id,
        "reason": reason,
        "deferred_at": deferred_at,
    }


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
        raise ValueError("installed Adaptive Agent Runtime manifest is missing")
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

    prior_state = load_rule_state(repo)
    prior_loaded = str(prior_state.get("loaded_revision") or "").strip() or None
    prior_required = bool(prior_state.get("live_e2e_required"))
    prior_acceptance = load_live_e2e_acceptance(repo)
    if (
        prior_required
        and prior_loaded
        and prior_acceptance.get("status") == "accepted"
        and str(prior_acceptance.get("installed_revision") or "").strip() == prior_loaded
        and str(prior_acceptance.get("controller_session_id") or "").strip() == controller_session_id
    ):
        prior_required = False
    ack_effective_impact, ack_changed_files = _unacked_change_impact(manifest, prior_loaded)
    new_live_e2e_files = _live_e2e_changed_files(
        manifest, effective_impact=ack_effective_impact, changed_files=ack_changed_files
    )
    live_e2e_required = prior_required or bool(new_live_e2e_files)
    prior_required_files = prior_state.get("live_e2e_required_changed_files")
    inherited_files = (
        {str(item) for item in prior_required_files if str(item).strip()}
        if prior_required and isinstance(prior_required_files, list)
        else set()
    )
    required_files = sorted(inherited_files | set(new_live_e2e_files))
    required_since = (
        str(prior_state.get("live_e2e_required_since_revision") or "").strip()
        if prior_required
        else ""
    )
    if live_e2e_required and not required_since:
        required_since = installed

    receipt = {
        "schema_version": 1,
        "installed_revision": installed,
        "loaded_revision": installed,
        "controller_session_id": controller_session_id,
        "acknowledged_at": (now or datetime.now(UTC)).astimezone(UTC).isoformat(),
        "manifest_sha256": _sha256(manifest_path),
        "live_e2e_required": live_e2e_required,
        "live_e2e_required_since_revision": required_since or None,
        "live_e2e_required_changed_files": required_files,
        "live_e2e_accepted_revision": prior_state.get("live_e2e_accepted_revision"),
        "live_e2e_accepted_at": prior_state.get("live_e2e_accepted_at"),
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
    parser = argparse.ArgumentParser(description="Inspect or acknowledge Adaptive Agent Runtime rule revision state.")
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
    live_e2e = sub.add_parser("accept-live-e2e")
    live_e2e.add_argument("--repo", required=True)
    live_e2e.add_argument("--controller-session", required=True)
    live_e2e.add_argument("--revision", required=True)
    live_e2e.add_argument("--skill-root")
    live_e2e.add_argument("--registry")
    defer_e2e = sub.add_parser("defer-live-e2e")
    defer_e2e.add_argument("--repo", required=True)
    defer_e2e.add_argument("--controller-session", required=True)
    defer_e2e.add_argument("--revision", required=True)
    defer_e2e.add_argument("--reason", required=True)
    defer_e2e.add_argument("--skill-root")
    defer_e2e.add_argument("--registry")
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
        if args.command == "accept-live-e2e":
            result = accept_live_e2e(
                args.repo,
                args.controller_session,
                args.revision,
                skill_root=args.skill_root,
                registry_path=args.registry,
            )
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "defer-live-e2e":
            result = defer_live_e2e(
                args.repo,
                args.controller_session,
                args.revision,
                reason=args.reason,
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
