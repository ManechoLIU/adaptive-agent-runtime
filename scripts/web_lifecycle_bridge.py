#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import deque
from contextlib import contextmanager
import fcntl
import json
import os
import secrets
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Sequence

try:
    from controller_health import decide_controller_wake, derive_controller_health
except ModuleNotFoundError:
    from scripts.controller_health import decide_controller_wake, derive_controller_health

try:
    import controller_target_guard as target_guard
except ModuleNotFoundError:
    from scripts import controller_target_guard as target_guard

try:
    from web_reentry_adapter import (
        build_reentry_prompt, execute_web_reentry, resolve_reentry_session,
    )
except ModuleNotFoundError:
    from scripts.web_reentry_adapter import (
        build_reentry_prompt, execute_web_reentry, resolve_reentry_session,
    )


DEFAULT_REGISTRY = Path.home() / ".codex" / "adaptive-delivery-controllers.json"
DEFAULT_MANUAL_WEB_LEASES = Path.home() / ".codex" / "adaptive-delivery-web-controller-leases.json"
DEFAULT_PEER_ATTESTATION_VERIFIER_CONFIG = Path.home() / ".codex" / "adaptive-delivery-host-attestation-verifiers.json"
_PEER_HOST_ATTESTATION_VERIFIERS: dict[str, Callable[..., Any]] = {}
PEER_ATTESTATION_VERIFIER_TIMEOUT_SECONDS = 15
PEER_ATTESTATION_VERIFIER_OUTPUT_LIMIT = 64 * 1024


class PeerHostTransientUnavailable(RuntimeError):
    """Machine Host boundary exists but is temporarily unable to attest/deliver."""


_PEER_HOST_TRANSIENT_ERROR_MARKERS = (
    "exact chatgpt conversation target is unavailable",
    "target has no stable chatgpt conversation route",
    "connect enoent",
    "econnrefused",
    "browser machine command timed out",
    "debugger attach timed out",
    "target.gettargetinfo timed out",
    "page.getframetree timed out",
    "another debugger is already attached",
    "native host connection closed",
    "native host is unavailable",
    "socket hang up",
)


def _peer_host_error_is_transient(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return bool(text) and any(marker in text for marker in _PEER_HOST_TRANSIENT_ERROR_MARKERS)


DEFAULT_MANUAL_WEB_LEASE_TTL_SECONDS = 30 * 24 * 60 * 60
DEFAULT_AUDIT_LOG = (
    Path.home()
    / "Library"
    / "Application Support"
    / "ai.originone.gpt-bridge"
    / "audit"
    / "activity.redacted.jsonl"
)
AI_BRIDGE_EXECUTABLE = "/Applications/AI-Bridge.app/Contents/MacOS/ai-bridge"
DEFAULT_RUNTIME_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
DEFAULT_CODEX_HOOKS = Path.home() / ".codex" / "hooks.json"
DEFAULT_DESKTOP_CANARY = (
    Path.home() / ".codex" / "state" / "adaptive-delivery-desktop-canary.json"
)
STDERR_TAIL_LIMIT = 8192
LAUNCHER_LOG_LIMIT = 262144
RESTORE_STATIC_DOCUMENT_NAMES = ("AGENTS.md", "MEMORY.md", "WIKI_INDEX.md")
AUTHORITATIVE_DOCUMENT_NAMES = ("SKILL.md", "SPEC.md", "DESIGN.md", "TECHNICAL.md", "EVOLUTION.md")
RESTORE_DOCUMENT_LIMIT = 32768
AUTO_CONTINUATION_STALL_LIMIT = 3
WEB_REENTRY_TRANSIENT_RETRY_LIMIT = 6
NATIVE_RESUME_MAX_RUNTIME_SECONDS = 30 * 60
NATIVE_RESUME_COMPLETION_GRACE_SECONDS = 2.0
_HOST_OBSERVED_CANONICAL_TARGET_FOREGROUND = object()


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def canonical_root(path: str | Path) -> Path:
    candidate = Path(path).expanduser().resolve()
    completed = subprocess.run(
        ["git", "-C", str(candidate), "rev-parse", "--show-toplevel"],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode == 0 and completed.stdout.strip():
        return Path(completed.stdout.strip()).resolve()
    return candidate


def registered_controller_for_repo(repo: Path, registry_path: Path) -> str | None:
    repo = repo.resolve()
    registry = load_json(registry_path)
    matches: set[str] = set()
    for session_id, value in registry.items():
        if session_id == "__controller_surfaces__" or not isinstance(session_id, str):
            continue
        if isinstance(value, str) and Path(value).expanduser().resolve() == repo:
            matches.add(session_id)
    surfaces = registry.get("__controller_surfaces__")
    if isinstance(surfaces, dict):
        for session_id, value in surfaces.items():
            if isinstance(session_id, str) and isinstance(value, str) and Path(value).expanduser().resolve() == repo:
                matches.add(session_id)
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError(f"expected exactly one registered controller for {repo}, found {len(matches)}")
    return next(iter(matches))


def registered_controller_session(
    *, controller_id: str, session_id: str, host: str, registry_path: Path
) -> bool:
    registry = load_json(registry_path)
    sessions = registry.get("__controller_sessions__")
    if not isinstance(sessions, dict):
        return False
    owners: set[str] = set()
    for candidate_controller, controller_sessions in sessions.items():
        if not isinstance(candidate_controller, str) or not isinstance(controller_sessions, dict):
            continue
        host_sessions = controller_sessions.get(host)
        if isinstance(host_sessions, str):
            host_sessions = [host_sessions]
        if not isinstance(host_sessions, list):
            continue
        if session_id in {value for value in host_sessions if isinstance(value, str)}:
            owners.add(candidate_controller)
    return owners == {controller_id}


def bind_web_session_to_controller(
    *, repo: Path, controller_id: str, web_session_id: str, registry_path: Path
) -> dict[str, Any]:
    controller_id = controller_id.strip()
    web_session_id = web_session_id.strip()
    if not controller_id or not web_session_id:
        raise ValueError("controller-id and web-session-id are required")
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = registry_path.with_suffix(registry_path.suffix + ".lock")
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            registered = registered_controller_for_repo(repo, registry_path)
            if registered != controller_id:
                raise PermissionError("Web session binding requires the registered Controller for this repository")
            registry = load_json(registry_path)
            sessions = registry.get("__controller_sessions__")
            if not isinstance(sessions, dict):
                sessions = {}
            for candidate_controller, controller_sessions in sessions.items():
                if candidate_controller == controller_id or not isinstance(controller_sessions, dict):
                    continue
                host_sessions = controller_sessions.get("web")
                if isinstance(host_sessions, str):
                    host_sessions = [host_sessions]
                if isinstance(host_sessions, list) and web_session_id in host_sessions:
                    raise PermissionError("Web Controller Session is already bound to another Controller")
            controller_sessions = sessions.get(controller_id)
            if not isinstance(controller_sessions, dict):
                controller_sessions = {}
            web_sessions = controller_sessions.get("web")
            if isinstance(web_sessions, str):
                web_sessions = [web_sessions]
            if not isinstance(web_sessions, list):
                web_sessions = []
            normalized = [value for value in web_sessions if isinstance(value, str) and value.strip()]
            if web_session_id not in normalized:
                normalized.append(web_session_id)
            controller_sessions["web"] = normalized
            sessions[controller_id] = controller_sessions
            registry["__controller_sessions__"] = sessions
            _write_json_atomic_file(registry_path, registry)
            return registry
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _web_target_generation(record: object) -> int:
    if record is None:
        return 0
    if not isinstance(record, dict):
        raise ValueError("web Controller target record is invalid")
    try:
        _status, _session_id, generation = target_guard.validate_target_record(record, host="web")
    except PermissionError as exc:
        raise ValueError(str(exc)) from exc
    return generation


def _require_expected_web_generation(record: object, *, expected_generation: int) -> int:
    if isinstance(expected_generation, bool) or not isinstance(expected_generation, int) or expected_generation < 0:
        raise ValueError("expected_generation must be a non-negative integer")
    generation = _web_target_generation(record)
    if generation != expected_generation:
        raise PermissionError(
            f"expected_generation {expected_generation} does not match current generation {generation}"
        )
    return generation


def _reject_cross_controller_web_owner(
    *, registry: dict[str, Any], controller_id: str, web_session_id: str
) -> None:
    owners = target_guard._session_owners(registry, session_id=web_session_id, host="web")
    foreign = {owner for owner in owners if owner != controller_id}
    if foreign:
        raise PermissionError("Web Controller Session is already bound to another Controller")


def _is_strong_web_target_record(record: object) -> bool:
    if not isinstance(record, dict):
        return False
    return (
        record.get("host_attested") is True
        or (
            record.get("provenance") == "host_attested_same_controller_recovery"
            and record.get("identity_proof") == "host_attested_origin"
        )
    )


def replace_web_session(
    *,
    repo: Path,
    controller_id: str,
    web_session_id: str,
    expected_generation: int,
    expected_ownership_generation: int,
    registry_path: Path,
    lease_path: Path | None = None,
) -> dict[str, Any]:
    repo = canonical_root(repo)
    controller_id = controller_id.strip()
    web_session_id = web_session_id.strip()
    if not controller_id or not web_session_id:
        raise ValueError("controller-id and web-session-id are required")
    registry_path = registry_path.expanduser()
    lease_path = Path(lease_path or DEFAULT_MANUAL_WEB_LEASES).expanduser()
    lock_path = target_guard.registry_lock_path(registry_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            registered, registry = target_guard.registered_controller_for_repo(repo, registry_path)
            if registered != controller_id:
                raise PermissionError(
                    "Web session replacement requires the existing unique registered Controller for this repository"
                )
            target_guard.require_no_active_outbound_lease(
                registry, controller_id=controller_id, host="web"
            )
            _reject_cross_controller_web_owner(
                registry=registry, controller_id=controller_id, web_session_id=web_session_id
            )
            sessions = registry.get("__controller_sessions__")
            if not isinstance(sessions, dict):
                sessions = {}
            controller_sessions = sessions.get(controller_id)
            if not isinstance(controller_sessions, dict):
                controller_sessions = {}
            web_sessions = controller_sessions.get("web")
            if isinstance(web_sessions, str):
                web_sessions = [web_sessions]
            if not isinstance(web_sessions, list):
                web_sessions = []
            aliases = list(dict.fromkeys(
                value.strip() for value in web_sessions if isinstance(value, str) and value.strip()
            ))
            if web_session_id not in aliases:
                raise PermissionError(
                    "Web session replacement requires the new session to already belong to this Controller lineage"
                )

            targets = registry.get("__controller_targets__")
            if targets is None:
                targets = {}
            elif not isinstance(targets, dict):
                raise ValueError("controller target registry is invalid")
            controller_targets = targets.get(controller_id)
            if controller_targets is None:
                controller_targets = {}
            elif not isinstance(controller_targets, dict):
                raise ValueError("Controller target map is invalid")
            prior = controller_targets.get("web")
            current_generation = _require_expected_web_generation(
                prior, expected_generation=expected_generation
            )
            if isinstance(prior, dict):
                try:
                    prior_status, prior_target, _prior_generation = target_guard.validate_target_record(
                        prior, host="web"
                    )
                except PermissionError as exc:
                    raise ValueError(str(exc)) from exc
                if prior_status == "active" and _is_strong_web_target_record(prior):
                    raise PermissionError(
                        "manual Web session replacement cannot modify a Host-attested current target; "
                        "use canonical Host-attested session-start recovery"
                    )
            else:
                prior_status, prior_target = None, None
            if prior_status == "active" and prior_target == web_session_id:
                generation = current_generation
                target = dict(prior)
                idempotent = True
            else:
                generation = current_generation + 1
                target = {
                    "status": "active",
                    "session_id": web_session_id,
                    "generation": generation,
                    "provenance": "manual_user_authorized",
                    "binding_mode": "temporary",
                    "host_attested": False,
                }
                controller_targets["web"] = target
                targets[controller_id] = controller_targets
                registry["__controller_targets__"] = targets
                idempotent = False

            prior_ownership = target_guard.execution_ownership_record(
                registry, controller_id=controller_id
            )
            if prior_ownership is None:
                ownership_generation = 0
                ownership_host = None
                ownership_target = None
            else:
                ownership_host, ownership_target, ownership_generation = (
                    target_guard.validate_execution_ownership_record(prior_ownership)
                )
            if (
                isinstance(expected_ownership_generation, bool)
                or not isinstance(expected_ownership_generation, int)
                or expected_ownership_generation < 0
            ):
                raise ValueError("expected_ownership_generation must be a non-negative integer")
            if ownership_generation != expected_ownership_generation:
                raise PermissionError(
                    f"expected ownership generation {expected_ownership_generation} does not match current ownership generation {ownership_generation}"
                )
            if ownership_host == "web" and ownership_target == web_session_id:
                next_ownership_generation = ownership_generation
                ownership_changed = False
            else:
                ownership_receipt = target_guard._claim_controller_host_in_registry(
                    registry,
                    controller_id=controller_id,
                    requested_host="web",
                    requested_target_session_id=web_session_id,
                    expected_generation=expected_ownership_generation,
                    provenance="manual_user_authorized",
                )
                next_ownership_generation = int(ownership_receipt["generation"])
                ownership_changed = True

            lease_lock_path = lease_path.with_suffix(lease_path.suffix + ".lock")
            lease_lock_path.parent.mkdir(parents=True, exist_ok=True)
            with lease_lock_path.open("a+") as lease_lock:
                fcntl.flock(lease_lock.fileno(), fcntl.LOCK_EX)
                try:
                    lease_before = load_json(lease_path)
                    lease_after, resume_lease_rotated = _rotated_manual_web_resume_lease_payload(
                        lease_before,
                        repo=repo,
                        controller_id=controller_id,
                        web_session_id=web_session_id,
                        now_unix=int(time.time()),
                    )
                    if resume_lease_rotated:
                        _write_json_atomic_file(lease_path, lease_after)
                    if not idempotent or ownership_changed:
                        try:
                            _write_json_atomic_file(registry_path, registry)
                        except Exception:
                            if resume_lease_rotated:
                                _write_json_atomic_file(lease_path, lease_before)
                            raise
                finally:
                    fcntl.flock(lease_lock.fileno(), fcntl.LOCK_UN)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    binding = str(target.get("binding_mode") or "temporary")
    host_attested = bool(target.get("host_attested")) if "host_attested" in target else False
    return {
        "controller_id": controller_id,
        "controller_session_id": controller_id,
        "execution_target_session_id": web_session_id,
        "host": "web",
        "repo": str(repo.resolve()),
        **target,
        "binding": binding,
        "host_attested": host_attested,
        "resume_lease_rotated": resume_lease_rotated,
        "ownership_generation": next_ownership_generation,
        "idempotent": idempotent,
    }


def unbind_web_session(
    *,
    repo: Path,
    controller_id: str,
    web_session_id: str,
    expected_generation: int,
    expected_ownership_generation: int,
    registry_path: Path,
) -> dict[str, Any]:
    repo = canonical_root(repo)
    controller_id = controller_id.strip()
    web_session_id = web_session_id.strip()
    if not controller_id or not web_session_id:
        raise ValueError("controller-id and web-session-id are required")
    registry_path = registry_path.expanduser()
    lock_path = target_guard.registry_lock_path(registry_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            registered, registry = target_guard.registered_controller_for_repo(repo, registry_path)
            if registered != controller_id:
                raise PermissionError(
                    "Web session unbind requires the existing unique registered Controller for this repository"
                )
            target_guard.require_no_active_outbound_lease(
                registry, controller_id=controller_id, host="web"
            )
            _reject_cross_controller_web_owner(
                registry=registry, controller_id=controller_id, web_session_id=web_session_id
            )
            sessions = registry.get("__controller_sessions__")
            controller_sessions = sessions.get(controller_id) if isinstance(sessions, dict) else None
            web_sessions = controller_sessions.get("web") if isinstance(controller_sessions, dict) else None
            if isinstance(web_sessions, str):
                web_sessions = [web_sessions]
            aliases = {
                value.strip() for value in (web_sessions or [])
                if isinstance(value, str) and value.strip()
            }
            if web_session_id not in aliases:
                raise PermissionError(
                    "Web session unbind requires the session to belong to this Controller lineage"
                )
            targets = registry.get("__controller_targets__")
            if targets is None:
                targets = {}
            elif not isinstance(targets, dict):
                raise ValueError("controller target registry is invalid")
            controller_targets = targets.get(controller_id)
            if controller_targets is None:
                controller_targets = {}
            elif not isinstance(controller_targets, dict):
                raise ValueError("Controller target map is invalid")
            prior = controller_targets.get("web")
            generation = _require_expected_web_generation(
                prior, expected_generation=expected_generation
            )
            prior_ownership = target_guard.execution_ownership_record(
                registry, controller_id=controller_id
            )
            if prior_ownership is None:
                ownership_generation = 0
            else:
                _ownership_host, _ownership_target, ownership_generation = (
                    target_guard.validate_execution_ownership_record(prior_ownership)
                )
            if (
                isinstance(expected_ownership_generation, bool)
                or not isinstance(expected_ownership_generation, int)
                or expected_ownership_generation < 0
            ):
                raise ValueError("expected_ownership_generation must be a non-negative integer")
            if ownership_generation != expected_ownership_generation:
                raise PermissionError(
                    f"expected ownership generation {expected_ownership_generation} does not match current ownership generation {ownership_generation}"
                )
            current = None
            provenance = "manual_user_authorized"
            binding_mode = "temporary"
            if isinstance(prior, dict):
                status, current, _ = target_guard.validate_target_record(prior, host="web")
                if status == "active" and _is_strong_web_target_record(prior):
                    raise PermissionError(
                        "manual Web session unbind cannot modify a Host-attested current target; "
                        "use canonical Host-attested session recovery"
                    )
                if status != "active":
                    current = None
                provenance = str(prior.get("provenance") or provenance)
                binding_mode = str(prior.get("binding_mode") or binding_mode)
            if current and current != web_session_id:
                target = dict(prior)
                idempotent = True
            else:
                target = {
                    "status": "unbound",
                    "session_id": None,
                    "generation": generation + 1,
                    "provenance": provenance,
                    "binding_mode": binding_mode,
                    "host_attested": False,
                }
                controller_targets["web"] = target
                targets[controller_id] = controller_targets
                registry["__controller_targets__"] = targets
                _write_json_atomic_file(registry_path, registry)
                idempotent = False
            return {
                "controller_id": controller_id,
                "controller_session_id": controller_id,
                "execution_target_session_id": target.get("session_id"),
                "host": "web",
                "repo": str(repo.resolve()),
                **target,
                "binding": "temporary",
                "host_attested": False,
                "idempotent": idempotent,
            }
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def require_web_controller_session(
    *, controller_id: str, web_session_id: str | None, registry_path: Path
) -> str:
    value = str(web_session_id or "").strip()
    if not value:
        raise PermissionError(
            "verified Web Controller Session identity required; refusing repository-only Controller attribution"
        )
    registry = load_json(registry_path)
    record = target_guard.target_record(
        registry, controller_id=controller_id, host="web"
    )
    if record is None:
        # Web lineage is historical ownership only. Without an explicit current
        # target there is no authorized Web execution entry, even if exactly one
        # alias exists or it matches a legacy logical Controller identifier.
        verified = False
    else:
        verified = (
            record.get("host_attested") is not False
            and not (
                record.get("provenance") == "host_attested_same_controller_recovery"
                and record.get("identity_proof") != "host_attested_origin"
            )
            and target_guard.active_source_controller_id(
                registry, source_session_id=value, host="web"
            ) == controller_id
        )
    if not verified:
        raise PermissionError(
            "verified Web Controller Session identity required; refusing repository-only Controller attribution"
        )
    return value


def authorize_manual_web_session(
    *,
    repo: Path,
    controller_id: str,
    web_session_id: str,
    registry_path: Path,
    lease_path: Path,
    ttl_seconds: int,
) -> dict[str, Any]:
    if ttl_seconds <= 0:
        raise ValueError("ttl-seconds must be positive")
    registered = registered_controller_for_repo(repo, registry_path)
    if registered != controller_id:
        raise PermissionError("manual Web lease requires the registered Controller for this repository")
    try:
        require_web_controller_session(
            controller_id=controller_id,
            web_session_id=web_session_id,
            registry_path=registry_path,
        )
    except PermissionError as exc:
        raise PermissionError(
            "manual Web lease requires a Web session that must already be bound "
            "as the current verified Web session for this Controller"
        ) from exc

    now = int(time.time())
    record = {
        "repo": str(repo.resolve()),
        "controller_id": controller_id,
        "web_session_id": web_session_id,
        "authorized_at_unix": now,
        "expires_at_unix": now + ttl_seconds,
        "provenance": "manual_user_authorized",
        "mode": "resume_only",
    }
    lease_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = lease_path.with_suffix(lease_path.suffix + ".lock")
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            payload = load_json(lease_path)
            leases = payload.get("leases")
            if not isinstance(leases, dict):
                leases = {}
            leases[controller_id] = record
            payload["schema_version"] = 1
            payload["leases"] = leases
            _write_json_atomic_file(lease_path, payload)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return record



def _rotated_manual_web_resume_lease_payload(
    payload: dict[str, Any],
    *,
    repo: Path,
    controller_id: str,
    web_session_id: str,
    now_unix: int,
) -> tuple[dict[str, Any], bool]:
    leases = payload.get("leases")
    if not isinstance(leases, dict):
        return payload, False
    record = leases.get(controller_id)
    if not isinstance(record, dict):
        return payload, False
    if record.get("controller_id") not in (None, controller_id):
        return payload, False
    if record.get("provenance") != "manual_user_authorized" or record.get("mode") != "resume_only":
        return payload, False
    # Target rotation must not leave a same-Controller manual lease pointing at a
    # historical alias. Retarget the lease metadata even when it is already
    # expired/suspended; preserve authorization/expiry exactly and never renew it.
    expires_at = record.get("expires_at_unix")
    lease_repo_value = record.get("repo")
    if not isinstance(lease_repo_value, str) or not lease_repo_value.strip():
        return payload, False
    lease_repo = Path(lease_repo_value).expanduser().resolve()
    try:
        same_repo = _git_common_dir(lease_repo) == _git_common_dir(repo)
    except (OSError, subprocess.SubprocessError):
        same_repo = lease_repo == repo.resolve()
    if not same_repo:
        return payload, False
    current_session = str(record.get("web_session_id") or "").strip()
    if current_session == web_session_id:
        return payload, False
    updated = json.loads(json.dumps(payload))
    updated_leases = updated.setdefault("leases", {})
    updated_leases[controller_id] = {
        **record,
        "repo": str(repo.resolve()),
        "controller_id": controller_id,
        "web_session_id": web_session_id,
        "rotated_at_unix": now_unix,
    }
    updated["schema_version"] = 1
    return updated, True


def rotate_existing_manual_web_resume_lease(
    *,
    repo: Path,
    controller_id: str,
    web_session_id: str,
    lease_path: Path | None = None,
    now_unix: int | None = None,
) -> bool:
    """Keep an existing manual resume lease aligned with Web target rotation.

    This never creates or renews authorization. It retargets an existing lease record
    that belongs to the same logical Controller/repository, including an already
    expired or suspended record, while preserving its authorization time and expiry.
    """
    repo = canonical_root(repo)
    lease_path = Path(lease_path or DEFAULT_MANUAL_WEB_LEASES).expanduser()
    now = int(time.time() if now_unix is None else now_unix)
    lock_path = lease_path.with_suffix(lease_path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            payload = load_json(lease_path)
            updated, rotated = _rotated_manual_web_resume_lease_payload(
                payload,
                repo=repo,
                controller_id=controller_id,
                web_session_id=web_session_id,
                now_unix=now,
            )
            if rotated:
                _write_json_atomic_file(lease_path, updated)
            return rotated
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def resolve_manual_web_session(
    *, cwd: Path, registry_path: Path, lease_path: Path, now_unix: int | None = None
) -> str | None:
    repo = canonical_root(cwd)
    try:
        controller_id = registered_controller_for_repo(repo, registry_path)
    except ValueError:
        return None
    if controller_id is None:
        return None
    payload = load_json(lease_path)
    leases = payload.get("leases")
    if not isinstance(leases, dict):
        return None
    record = leases.get(controller_id)
    if not isinstance(record, dict):
        return None
    if record.get("provenance") != "manual_user_authorized" or record.get("mode") != "resume_only":
        return None
    if record.get("controller_id") not in (None, controller_id):
        return None
    session_id = record.get("web_session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        return None
    expires_at = record.get("expires_at_unix")
    if not isinstance(expires_at, int) or expires_at <= int(time.time() if now_unix is None else now_unix):
        return None
    lease_repo_value = record.get("repo")
    if not isinstance(lease_repo_value, str) or not lease_repo_value.strip():
        return None
    lease_repo = Path(lease_repo_value).expanduser().resolve()
    try:
        if _git_common_dir(lease_repo) != _git_common_dir(repo):
            return None
    except (OSError, subprocess.SubprocessError):
        if lease_repo != repo.resolve():
            return None
    try:
        require_web_controller_session(
            controller_id=controller_id,
            web_session_id=session_id,
            registry_path=registry_path,
        )
    except PermissionError:
        return None
    return session_id


def _controller_ownership_generation(registry_path: Path, controller_id: str) -> int:
    registry = load_json(registry_path)
    record = target_guard.execution_ownership_record(
        registry, controller_id=controller_id
    )
    if record is None:
        return 0
    _host, _target, generation = target_guard.validate_execution_ownership_record(record)
    return generation


def recover_same_controller_web_session(
    *,
    repo: Path,
    web_session_id: str,
    registry_path: Path,
    host_identity_receipt: dict[str, Any] | None,
) -> dict[str, Any]:
    """Rebind only the existing unique Controller after Host-attested Web identity proof."""
    repo = canonical_root(repo)
    registry_path = registry_path.expanduser()
    web_session_id = str(web_session_id or "").strip()
    if not web_session_id:
        identity = target_guard.controller_identity_projection(
            repo=repo,
            host="web",
            source_session_id=None,
            registry_path=registry_path,
        )
        return {
            "result": "DEFERRED",
            "state": "SAME_CONTROLLER_SESSION_RECOVERY",
            "reason": "HOST_SESSION_ID_UNAVAILABLE",
            "identity": identity,
        }

    identity = target_guard.controller_identity_projection(
        repo=repo,
        host="web",
        source_session_id=web_session_id,
        registry_path=registry_path,
    )
    project = identity["project_controller_state"]
    binding = identity["session_binding_state"]
    if project.get("project_controller") != "EXISTING":
        raise PermissionError(
            "same-controller recovery requires one existing unique project Controller"
        )
    controller_id = str(project.get("controller_id") or "").strip()
    if not controller_id:
        raise PermissionError("same-controller recovery has no existing controller_id")

    # Host attestation proves that a browser conversation exists and matches the
    # requested generation fence; it does not prove that an arbitrary historical
    # alias or unrelated project chat is the user's intended successor Controller.
    # Recovery therefore may only upgrade the already-canonical current Web target
    # in place. Any target session change requires the explicit replace-web-session
    # path first, which records a new manual/temporary current target.
    registry_snapshot = load_json(registry_path)
    current_target_record = target_guard.target_record(
        registry_snapshot, controller_id=controller_id, host="web"
    )
    if not isinstance(current_target_record, dict):
        raise PermissionError(
            "same-controller Web recovery requires an explicit canonical current target; "
            "use replace-web-session after explicit target authorization"
        )
    current_status, current_target_session, _current_target_generation = (
        target_guard.validate_target_record(current_target_record, host="web")
    )
    if current_status != "active" or current_target_session != web_session_id:
        raise PermissionError(
            "same-controller Web recovery cannot replace the canonical current target; "
            "historical/unbound sessions require explicit replace-web-session authorization"
        )

    if binding.get("verification") == "CONFLICT":
        raise PermissionError(
            "verified Web Controller Session identity required; "
            "same-controller recovery refuses conflicting session ownership"
        )

    current_registry = load_json(registry_path)
    current_target_record = target_guard.target_record(
        current_registry, controller_id=controller_id, host="web"
    )
    if isinstance(current_target_record, dict):
        current_status, current_target_session_id, current_target_generation = (
            target_guard.validate_target_record(current_target_record, host="web")
        )
        if (
            current_status == "active"
            and _is_strong_web_target_record(current_target_record)
            and current_target_session_id != web_session_id
        ):
            return {
                "result": "DEFERRED",
                "state": "SAME_CONTROLLER_SESSION_RECOVERY",
                "reason": "HOST_ATTESTED_CURRENT_TARGET_ALREADY_ACTIVE",
                "controller_id": controller_id,
                "current_execution_target_session_id": current_target_session_id,
                "current_target_generation": current_target_generation,
                "safe_control_actions_allowed": True,
                "identity": identity,
            }

    ownership_generation = _controller_ownership_generation(
        registry_path, controller_id
    )
    if binding.get("verification") == "VERIFIED":
        ownership = target_guard.claim_controller_host(
            repo=repo,
            controller_id=controller_id,
            requested_host="web",
            requested_target_session_id=web_session_id,
            expected_generation=ownership_generation,
            registry_path=registry_path,
            provenance="web_entry",
        )
        return {
            "result": "ALREADY_VERIFIED",
            "state": "VERIFIED",
            "controller_id": controller_id,
            "active_host": ownership["active_host"],
            "ownership_generation": ownership["generation"],
            "resume_lease_rotated": False,
            "identity": target_guard.controller_identity_projection(
                repo=repo, host="web", source_session_id=web_session_id,
                registry_path=registry_path,
            ),
        }

    verifier = _registered_peer_attestation_verifier("web")
    if not callable(verifier):
        return {
            "result": "DEFERRED",
            "state": "CONTROLLER_IDENTITY_DEGRADED",
            "reason": "HOST_IDENTITY_UNAVAILABLE",
            "controller_id": controller_id,
            "safe_control_actions_allowed": True,
            "identity": identity,
        }

    prior_generation = binding.get("target_generation")
    if not isinstance(prior_generation, int) or isinstance(prior_generation, bool):
        prior_generation = 0
    try:
        attested = verifier(
            controller_id=controller_id,
            host="web",
            expected_target_session_id=web_session_id,
            expected_target_generation=prior_generation,
            expected_target_mode="same_controller_session_recovery",
            expected_ownership_generation=ownership_generation,
            host_execution_receipt=host_identity_receipt,
            adapter_attempt={
                "operation": "controller_session_identity_recovery",
                "repo": str(repo.resolve()),
                "controller_id": controller_id,
                "execution_target_session_id": web_session_id,
            },
        )
    except Exception as exc:
        return {
            "result": "DEFERRED",
            "state": "CONTROLLER_IDENTITY_DEGRADED",
            "reason": "HOST_IDENTITY_VERIFIER_UNAVAILABLE",
            "controller_id": controller_id,
            "safe_control_actions_allowed": True,
            "verifier_error": str(exc),
            "identity": identity,
        }
    if attested is not True:
        raise PermissionError("Host Controller session identity attestation rejected")

    lock_path = target_guard.registry_lock_path(registry_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            registry = load_json(registry_path)
            matches = target_guard._matching_controller_ids_for_repo(repo, registry)
            if matches != [controller_id]:
                raise PermissionError(
                    "project Controller changed during same-controller session recovery"
                )
            target_guard.require_no_active_outbound_lease(
                registry, controller_id=controller_id, host="web"
            )
            current_record = target_guard.target_record(
                registry, controller_id=controller_id, host="web"
            )
            if current_record is None:
                current_generation = 0
            else:
                _current_status, _current_target, current_generation = (
                    target_guard.validate_target_record(current_record, host="web")
                )
            if current_generation != prior_generation:
                raise PermissionError(
                    "Web Controller target generation changed after identity attestation; "
                    "stale generation cannot recover the session"
                )
            owners = target_guard._session_owners(
                registry, session_id=web_session_id, host="web"
            )
            foreign = {owner for owner in owners if owner != controller_id}
            if foreign:
                raise PermissionError(
                    "Web session is already owned by another Controller"
                )

            sessions = registry.get("__controller_sessions__")
            if sessions is None:
                sessions = {}
            if not isinstance(sessions, dict):
                raise ValueError("controller session registry is invalid")
            controller_sessions = sessions.get(controller_id)
            if controller_sessions is None:
                controller_sessions = {}
            if not isinstance(controller_sessions, dict):
                raise ValueError("Controller session map is invalid")
            web_sessions = controller_sessions.get("web")
            if isinstance(web_sessions, str):
                web_sessions = [web_sessions]
            if web_sessions is None:
                web_sessions = []
            if not isinstance(web_sessions, list):
                raise ValueError("Controller Web session list is invalid")
            aliases = [
                value.strip()
                for value in web_sessions
                if isinstance(value, str) and value.strip()
            ]
            if web_session_id not in aliases:
                aliases.append(web_session_id)
            controller_sessions["web"] = list(dict.fromkeys(aliases))
            sessions[controller_id] = controller_sessions
            registry["__controller_sessions__"] = sessions

            targets = registry.get("__controller_targets__")
            if targets is None:
                targets = {}
            if not isinstance(targets, dict):
                raise ValueError("controller target registry is invalid")
            controller_targets = targets.get(controller_id)
            if controller_targets is None:
                controller_targets = {}
            if not isinstance(controller_targets, dict):
                raise ValueError("Controller target map is invalid")
            prior = controller_targets.get("web")
            if prior is None:
                generation = 1
            else:
                _status, _target, generation = target_guard.validate_target_record(
                    prior, host="web"
                )
                generation += 1
            receipt_fingerprint = __import__("hashlib").sha256(
                json.dumps(
                    host_identity_receipt or {},
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            controller_targets["web"] = {
                "status": "active",
                "session_id": web_session_id,
                "generation": generation,
                "provenance": "host_attested_same_controller_recovery",
                "binding_mode": "resume_only",
                "identity_proof": "host_attested_origin",
                "host_identity_receipt_sha256": receipt_fingerprint,
            }
            targets[controller_id] = controller_targets
            registry["__controller_targets__"] = targets
            ownership_claim = target_guard._claim_controller_host_in_registry(
                registry,
                controller_id=controller_id,
                requested_host="web",
                requested_target_session_id=web_session_id,
                expected_generation=ownership_generation,
                provenance="web_entry",
            )
            _write_json_atomic_file(registry_path, registry)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    recovered = target_guard.controller_identity_projection(
        repo=repo,
        host="web",
        source_session_id=web_session_id,
        registry_path=registry_path,
    )
    if recovered["session_binding_state"].get("verification") != "VERIFIED":
        raise RuntimeError("same-controller recovery did not produce a verified current session")
    return {
        "result": "RECOVERED",
        "state": "VERIFIED",
        "controller_id": controller_id,
        "execution_target_session_id": web_session_id,
        "resume_lease_rotated": False,
        "active_host": ownership_claim["active_host"],
        "ownership_generation": ownership_claim["generation"],
        "target_generation": recovered["session_binding_state"].get(
            "target_generation"
        ),
        "identity": recovered,
    }


def post_tool_event(
    *,
    session_id: str,
    repo: Path,
    command: str,
    exit_code: int | None = None,
    output: str = "",
    turn_id: str = "web-ai-bridge",
    web_session_id: str | None = None,
    execution_host: str = "web",
) -> dict[str, Any]:
    response: dict[str, Any] = {"output": output}
    if exit_code is not None:
        response["exit_code"] = exit_code
    return {
        "hook_event_name": "PostToolUse",
        "controller_host": execution_host,
        "execution_host": execution_host,
        "event_source": "web",
        "controller_id": session_id,
        "controller_session_id": session_id,
        "web_session_id": web_session_id,
        "session_id": session_id,
        "turn_id": turn_id,
        "cwd": str(repo),
        "tool_name": "AI-Bridge.shell_command",
        "tool_input": {"command": command},
        "tool_response": response,
    }


def extract_command(receipt: dict[str, Any]) -> str:
    detail = receipt.get("detail")
    if isinstance(detail, str) and detail.startswith("命令："):
        body = detail.removeprefix("命令：")
        for marker in (" · 工作目录：", "\n\n命令输出："):
            if marker in body:
                body = body.split(marker, 1)[0]
        if body.strip():
            return body.strip()
    target = receipt.get("targetLabel")
    return target.strip() if isinstance(target, str) else ""


def translate_receipt(
    receipt: dict[str, Any], *, session_id: str, repo: Path, web_session_id: str | None = None
) -> dict[str, Any] | None:
    if receipt.get("childTool") != "shell_command":
        return None
    if receipt.get("state") not in {"succeeded", "failed"}:
        return None
    root_label = receipt.get("rootLabel")
    if not isinstance(root_label, str) or not root_label.strip():
        return None
    if Path(root_label).expanduser().resolve() != repo:
        return None
    command = extract_command(receipt)
    if not command:
        return None
    detail = receipt.get("detail")
    output = detail if isinstance(detail, str) else ""
    receipt_id = str(receipt.get("receiptId") or "web-audit")
    return post_tool_event(
        session_id=session_id,
        repo=repo,
        command=command,
        output=output,
        turn_id=f"web-audit:{receipt_id}",
        web_session_id=web_session_id,
        execution_host="web",
    )



def _git_common_dir(repo: Path) -> Path:
    completed = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--git-common-dir"],
        check=True, capture_output=True, text=True,
    )
    value = Path(completed.stdout.strip())
    return (repo / value).resolve() if not value.is_absolute() else value.resolve()


def _restore_document(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    content = path.read_text(encoding="utf-8")
    truncated = len(content.encode("utf-8")) > RESTORE_DOCUMENT_LIMIT
    if truncated:
        encoded = content.encode("utf-8")[:RESTORE_DOCUMENT_LIMIT]
        content = encoded.decode("utf-8", errors="ignore")
    return {"name": path.name, "path": str(path.resolve()), "content": content, "truncated": truncated}


def _bounded_text(value: str, limit: int = RESTORE_DOCUMENT_LIMIT) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value, False
    return encoded[:limit].decode("utf-8", errors="ignore"), True


def _bounded_runtime_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"present": False, "path": str(path), "content": "", "truncated": False}
    content, truncated = _bounded_text(path.read_text(encoding="utf-8"))
    return {
        "present": True,
        "path": str(path),
        "content": content,
        "truncated": truncated,
        "sha256": __import__("hashlib").sha256(path.read_bytes()).hexdigest(),
    }

try:
    from scripts.project_context_guard import initialize_project_context
except ModuleNotFoundError:
    from project_context_guard import initialize_project_context


def _controller_restore_lifecycle(controller_id: str) -> dict[str, Any]:
    state = _load_lifecycle_state(controller_id)
    snapshot = state.get("snapshot") if isinstance(state.get("snapshot"), dict) else {}
    triggers = [
        str(item) for item in state.get("triggers", [])
        if str(item).strip()
    ]
    continuation_debt = state.get("continuation_debt")
    if not isinstance(continuation_debt, list):
        continuation_debt = []
    result = {
        "pending_control_event": state.get("pending_control_event") is True,
        "requires_user": state.get("requires_user") is True,
        "controller_host": str(state.get("controller_host") or "").strip() or None,
        "wake_generation": int(state.get("wake_generation", 0) or 0),
        "triggers": triggers,
        "next_action": str(state.get("next_action") or "").strip() or None,
        "continuation_debt": [
            item for item in continuation_debt if isinstance(item, (str, dict))
        ][:64],
        "runnable_ids": [
            str(item) for item in snapshot.get("runnable_ids", [])
            if str(item).strip()
        ][:128],
        "candidate_revisions": [
            str(item) for item in snapshot.get("candidate_revisions", [])
            if str(item).strip()
        ][:128],
    }
    result["resume_control_loop_required"] = bool(
        result["pending_control_event"]
        or result["triggers"]
        or result["next_action"]
        or result["continuation_debt"]
        or result["runnable_ids"]
        or result["candidate_revisions"]
    ) and not result["requires_user"]
    return result


def web_session_restore_payload(
    repo: Path, registry_path: Path, *, web_session_id: str | None = None
) -> dict[str, Any]:
    root = canonical_root(repo)
    controller = registered_controller_for_repo(root, registry_path)
    if controller is None:
        raise ValueError(f"no registered controller for {root}")
    ledger_name = "TASK_LEDGER.md" if (root / "TASK_LEDGER.md").is_file() else ("PROJECT_STATUS.md" if (root / "PROJECT_STATUS.md").is_file() else "TASK_LEDGER.md")
    restore_names = ("AGENTS.md", "SKILL.md", ledger_name, "MEMORY.md", "WIKI_INDEX.md")
    documents = [item for name in restore_names if (item := _restore_document(root / name)) is not None]
    authoritative_documents = [
        item for name in AUTHORITATIVE_DOCUMENT_NAMES
        if (item := _restore_document(root / name)) is not None
    ]
    head_result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], check=False, capture_output=True, text=True
    )
    head = head_result.stdout.strip() if head_result.returncode == 0 else None
    status = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain=v1", "--untracked-files=all"],
        check=True, capture_output=True, text=True,
    ).stdout
    status_content, status_truncated = _bounded_text(status)
    branch = subprocess.run(
        ["git", "-C", str(root), "branch", "--show-current"], check=True, capture_output=True, text=True
    ).stdout.strip()
    runtime_state = _git_common_dir(root) / "adaptive-delivery" / "runtime-assignments.json"
    runtime = _bounded_runtime_state(runtime_state)
    project_context = initialize_project_context(
        root,
        skill_root=Path(__file__).resolve().parents[1],
        controller_registry_path=registry_path,
        controller_host="web",
        source_session_id=web_session_id,
    )
    controller_identity = target_guard.controller_identity_projection(
        repo=root,
        host="web",
        source_session_id=web_session_id,
        registry_path=registry_path,
    )
    controller_lifecycle = _controller_restore_lifecycle(controller)
    return {
        "product": "Adaptive Agent Runtime",
        "project_root": str(root),
        "controller_id": controller,
        "project_controller_state": controller_identity["project_controller_state"],
        "session_binding_state": controller_identity["session_binding_state"],
        "controller_actions_allowed": controller_identity["controller_actions_allowed"],
        "controller_recovery": controller_identity["session_binding_state"].get(
            "recovery"
        ),
        "controller_lifecycle": controller_lifecycle,
        "resume_control_loop_required": controller_lifecycle[
            "resume_control_loop_required"
        ],
        "restore_order": [item["name"] for item in documents] + ["git_runtime"],
        "documents": documents,
        "authoritative_documents": authoritative_documents,
        "git": {
            "head": head,
            "branch": branch,
            "status": status_content,
            "status_truncated": status_truncated,
            "status_sha256": __import__("hashlib").sha256(status.encode("utf-8")).hexdigest(),
        },
        "runtime": runtime,
        "project_context": project_context,
        "runtime_state_path": str(runtime_state),
        "compact": "not restored unless an explicit handoff/compact is available",
    }


def classify_native_resume_failure(returncode: int, stdout: str, stderr: str) -> dict[str, Any]:
    combined = f"{stdout}\n{stderr}".casefold()
    if "already has an active writer" in combined:
        return {
            "state": "RESUME_DEFERRED_ACTIVE_WRITER",
            "pending_control_event": True,
            "failure_class": "active_writer_present",
            "fallback_eligible": False,
            "error_code": "WEB_LIFECYCLE_ACTIVE_WRITER",
        }
    if (
        "failed to deserialize stored thread item" in combined
        and ("unknown variant" in combined or "functioncalloutput" in combined)
    ):
        return {
            "state": "RESUME_TARGET_INCOMPATIBLE",
            "pending_control_event": True,
            "failure_class": "target_schema_incompatible",
            "fallback_eligible": False,
            "replacement_eligible": True,
            "error_code": "WEB_LIFECYCLE_TARGET_INCOMPATIBLE",
            "returncode": returncode,
        }
    failure_patterns = (
        ("usage_limit_exceeded", ("usage limit", "usage_limit_exceeded")),
        ("quota_exhausted", ("quota exhausted", "quota_exhausted")),
        ("model_unavailable", ("model unavailable", "model_unavailable")),
        ("service_unavailable", ("service unavailable", "service_unavailable")),
        ("auth_invalid", ("authentication", "auth_invalid", "unauthorized")),
        ("runtime_unavailable", ("runtime unavailable", "runtime_unavailable")),
    )
    failure_class = next((name for name, patterns in failure_patterns if any(pattern in combined for pattern in patterns)), "resume_failed")
    return {
        "state": "RESUME_FAILED",
        "pending_control_event": True,
        "failure_class": failure_class,
        "fallback_eligible": failure_class != "resume_failed",
        "error_code": "WEB_LIFECYCLE_RESUME_FAILED",
        "returncode": returncode,
    }

def _write_json_atomic_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def audit_records_from_cursor(audit_log: Path, cursor_path: Path) -> tuple[list[tuple[dict[str, Any], int]], int]:
    state = load_json(cursor_path)
    try:
        stat = audit_log.stat()
    except OSError:
        return [], 0
    inode = int(state.get("inode", 0) or 0)
    offset = int(state.get("offset", 0) or 0)
    if inode != stat.st_ino or offset < 0 or offset > stat.st_size:
        offset = 0
    records: list[tuple[dict[str, Any], int]] = []
    with audit_log.open("rb") as handle:
        handle.seek(offset)
        for raw_line in handle:
            next_offset = handle.tell()
            try:
                value = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                records.append((value, next_offset))
    return records, stat.st_ino



def _web_assignment_reconcile_state_path(repo: Path) -> Path:
    return _git_common_dir(repo) / "adaptive-delivery" / "web-assignment-reconcile.json"


def _load_web_assignment_reconcile_state(repo: Path) -> dict[str, Any]:
    state = load_json(_web_assignment_reconcile_state_path(repo))
    if not isinstance(state.get("attempts"), dict):
        state["attempts"] = {}
    state.setdefault("schema_version", 1)
    return state


def _write_web_assignment_reconcile_state(repo: Path, state: dict[str, Any]) -> None:
    _write_json_atomic_file(_web_assignment_reconcile_state_path(repo), state)


def _web_attempt_key(assignment_id: str, lease: dict[str, Any]) -> str:
    return f"{assignment_id}:{int(lease.get('attempt', 0))}:{str(lease.get('lease_id') or '')}"


def _wake_state_from_continuation(result: dict[str, Any] | None) -> str:
    if not isinstance(result, dict):
        return "pending"
    if wake_receipt_confirmed(result.get("wake_result")):
        return "confirmed"
    if result.get("pending_control_event") is False:
        return "confirmed"
    if result.get("supervisor_armed") is True:
        return "delegated"
    return "pending"


def controller_continuation_projection(
    *, repo: Path, controller_id: str, registry: Path
) -> dict[str, Any]:
    """Derive whether the existing logical Controller must keep running from canonical facts."""
    try:
        from scripts import lifecycle_hook as lifecycle
        from scripts import control_event_guard as control_guard
    except ModuleNotFoundError:
        import lifecycle_hook as lifecycle
        import control_event_guard as control_guard

    repo = Path(repo).expanduser().resolve()
    registry = Path(registry).expanduser().resolve()
    state_file = lifecycle.state_path(controller_id)
    lifecycle_state = lifecycle.load_json(state_file)
    host = resolve_controller_host(
        lifecycle_state, {}, load_json(registry), controller_id
    )
    requires_user = lifecycle_state.get("requires_user") is True
    runnable_ids: set[str] = set()
    action_ids: set[str] = set()
    active_assignment_task_ids: set[str] = set()

    ledger = next(
        (repo / name for name in ("TASK_LEDGER.md", "PROJECT_STATUS.md") if (repo / name).is_file()),
        None,
    )
    if ledger is not None:
        dispatch = control_guard.project_wide_dispatch_projection(ledger)
        runnable_ids = {
            str(item).strip()
            for item in dispatch.get("derived_runnable_ids", set())
            if str(item).strip()
        }
        candidates = control_guard.unmerged_worktree_candidates(repo)
        corrections = control_guard.open_controller_corrections(repo, controller_id)
        actions = control_guard.canonical_controller_action_projection(
            repo,
            controller_id=controller_id,
            candidates=candidates,
            required_review_ids=set(),
            work_in_flight=dict(dispatch.get("work_in_flight", {})),
            corrections=corrections,
            snapshot={},
            ledger_task_states=dict(dispatch.get("task_states", {})),
        )
        action_ids = {str(item) for item in actions}
        active_assignment_task_ids = control_guard.runtime_occupied_task_ids(
            repo, dict(dispatch.get("work_in_flight", {}))
        )

    debt_ids = {
        *(f"runnable:{task_id}" for task_id in sorted(runnable_ids)),
        *(f"controller_action:{action_id}" for action_id in sorted(action_ids)),
    }
    pending_next_action = str(lifecycle_state.get("next_action") or "").strip()
    if pending_next_action and not requires_user:
        debt_ids.add("known_next_action")
    if lifecycle_state.get("pending_control_event") is True:
        debt_ids.add("pending_control_event")

    should_continue = bool(debt_ids) and not requires_user
    if should_continue and lifecycle_state.get("pending_control_event") is not True:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        lock_path = state_file.with_suffix(state_file.suffix + ".lock")
        with lock_path.open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                current = lifecycle.load_json(state_file)
                if current.get("requires_user") is not True:
                    current["pending_control_event"] = True
                    current["requires_user"] = False
                    current["controller_host"] = host
                    current["wake_generation"] = int(current.get("wake_generation", 0) or 0) + 1
                    current["runtime_continuation_debt_ids"] = sorted(debt_ids)
                    triggers = {
                        str(item) for item in current.get("triggers", []) if str(item).strip()
                    }
                    triggers.add("RUNTIME_CONTINUATION_DEBT")
                    if str(current.get("next_action") or "").strip():
                        triggers.add("KNOWN_NEXT_ACTION_NOT_EXECUTED")
                    current["triggers"] = sorted(triggers)
                    current["yield_rejected"] = True
                    current["yield_rejected_reason"] = (
                        "canonical continuation debt remained after the prior host turn ended"
                    )
                    current["yield_recovery_source"] = "canonical_continuation_projection"
                    lifecycle.write_json(state_file, current)
                    lifecycle_state = current
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    return {
        "controller_id": controller_id,
        "controller_host": host,
        "requires_user": requires_user,
        "should_continue": should_continue,
        "debt_ids": sorted(debt_ids),
        "runnable_ids": sorted(runnable_ids),
        "runnable_count": len(runnable_ids),
        "active_assignment_task_ids": sorted(active_assignment_task_ids),
        "active_assignment_count": len(active_assignment_task_ids),
        "controller_action_ids": sorted(action_ids),
        "lifecycle_state": lifecycle_state,
    }


def reconcile_managed_web_assignments(
    *,
    repo: Path,
    controller_id: str,
    registry: Path,
    event_paths: Sequence[str | Path] | None = None,
    now: Any | None = None,
    terminal_consumer: Callable[..., dict[str, Any]] | None = None,
    runtime_change_consumer: Callable[..., dict[str, Any]] | None = None,
    event_source_probe: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Bridge managed collaboration child lifecycle into the existing Runtime continuation path."""
    from datetime import datetime, timezone
    try:
        from scripts.assignment_runtime import load_runtime_state
        from scripts import terminal_continuation
        from scripts.web_agent_events import structured_subagent_events, discover_recent_session_paths
        from scripts.web_agent_execution import (
            _load_dispatch_state,
            bind_web_assignment_dispatch,
            _ingest_verified_structured_subagent_terminal,
            _verified_machine_event_paths,
            watch_web_assignment_once,
        )
    except ModuleNotFoundError:
        from assignment_runtime import load_runtime_state
        import terminal_continuation
        from web_agent_events import structured_subagent_events, discover_recent_session_paths
        from web_agent_execution import (
            _load_dispatch_state,
            bind_web_assignment_dispatch,
            _ingest_verified_structured_subagent_terminal,
            _verified_machine_event_paths,
            watch_web_assignment_once,
        )

    repo = Path(repo).expanduser().resolve()
    registry = Path(registry).expanduser()
    actual_controller = _registered_controller_for_common_dir(repo, registry)
    if actual_controller != controller_id:
        raise PermissionError("managed Web Assignment reconciliation requires the registered logical Controller")
    now = now or datetime.now(timezone.utc)

    dispatch_state = _load_dispatch_state(repo)
    pending_dispatches = [
        record for record in dispatch_state.get("dispatches", {}).values()
        if isinstance(record, dict)
        and record.get("state") == "pending"
        and (
            (record.get("delegation_owner_kind", "controller") == "controller"
             and record.get("delegation_owner_id", record.get("controller_id")) == controller_id)
            or (record.get("delegation_owner_kind") == "session"
                and bool(str(record.get("delegation_owner_id") or "").strip()))
        )
    ]
    runtime = load_runtime_state(repo)
    web_leases = {
        assignment_id: lease
        for assignment_id, lease in runtime.get("leases", {}).items()
        if isinstance(lease, dict) and lease.get("execution_transport") == "web"
    }

    paths: list[Path]
    if event_paths is None:
        timestamps = [
            str(record.get("prepared_at") or "")
            for record in pending_dispatches
            if str(record.get("prepared_at") or "").strip()
        ]
        timestamps.extend(
            str(lease.get("started_at") or "")
            for lease in web_leases.values()
            if str(lease.get("started_at") or "").strip()
        )
        paths = discover_recent_session_paths(since_values=timestamps, now=now)
    else:
        paths = [Path(value).expanduser() for value in event_paths]
    source_error: str | None = None
    if paths:
        try:
            paths = _verified_machine_event_paths(paths)
        except (OSError, ValueError, PermissionError, RuntimeError) as exc:
            source_error = f"{type(exc).__name__}: {exc}"
            paths = []
    events = structured_subagent_events(paths)

    bound_dispatches: list[dict[str, Any]] = []
    binding_errors: list[dict[str, Any]] = []
    for ticket in pending_dispatches:
        dispatch_id = str(ticket.get("dispatch_id") or "")
        if not dispatch_id:
            continue
        if not any(
            event.get("kind") == "started"
            and event.get("task_name") == ticket.get("task_name")
            for event in events
        ):
            continue
        try:
            owner_kind = str(ticket.get("delegation_owner_kind") or "controller")
            owner_id = str(ticket.get("delegation_owner_id") or ticket.get("controller_id") or "").strip()
            bound_dispatches.append(bind_web_assignment_dispatch(
                repo=repo,
                registry_path=registry,
                controller_id=controller_id if owner_kind == "controller" else None,
                delegator_session_id=owner_id if owner_kind == "session" else None,
                dispatch_id=dispatch_id,
                event_paths=paths,
                now=now,
                health_probe=lambda: True,
                event_source_probe=event_source_probe,
                watchdog_launcher=lambda **_: {
                    "launched": False,
                    "reason": "global_health_supervisor_owns_periodic_observation",
                },
            ))
        except (OSError, ValueError, PermissionError, RuntimeError) as exc:
            binding_errors.append({"dispatch_id": dispatch_id, "error": f"{type(exc).__name__}: {exc}"})

    runtime = load_runtime_state(repo)
    web_leases = {
        assignment_id: lease
        for assignment_id, lease in runtime.get("leases", {}).items()
        if isinstance(lease, dict) and lease.get("execution_transport") == "web"
    }
    terminal_by_session: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.get("kind") not in {"completed", "failed", "cancelled", "interrupted", "disconnected"}:
            continue
        session_id = str(event.get("conversation_id") or "")
        if session_id:
            terminal_by_session[session_id] = event

    state_path = _web_assignment_reconcile_state_path(repo)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = state_path.with_suffix(state_path.suffix + ".lock")
    terminal_continuations: list[dict[str, Any]] = []
    health_results: list[dict[str, Any]] = []

    with lock_path.open("a+") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            reconcile_state = _load_web_assignment_reconcile_state(repo)
            attempts = reconcile_state.setdefault("attempts", {})
            for assignment_id, lease in list(web_leases.items()):
                key = _web_attempt_key(assignment_id, lease)
                record = attempts.setdefault(key, {
                    "assignment_id": assignment_id,
                    "attempt": int(lease.get("attempt", 0)),
                    "lease_id": str(lease.get("lease_id") or ""),
                })
                observation = terminal_by_session.get(str(lease.get("session_id") or ""))
                terminal_receipt: Path | None = None
                if observation is not None:
                    terminal_result = _ingest_verified_structured_subagent_terminal(
                        repo=repo,
                        assignment_id=assignment_id,
                        event_path=str(observation.get("source_path") or ""),
                        observation_id=str(observation.get("observation_id") or ""),
                        now=now,
                    )
                    lease = load_runtime_state(repo)["leases"][assignment_id]
                    terminal_receipt = Path(terminal_result["terminal_receipt"])
                    record["terminal_receipt"] = str(terminal_receipt)
                    record["terminal_observation_id"] = str(observation.get("observation_id") or "")
                elif str(record.get("terminal_receipt") or ""):
                    terminal_receipt = Path(str(record["terminal_receipt"]))

                if lease.get("terminal_state") and terminal_receipt is not None:
                    if lease.get("delegation_owner_kind") == "session":
                        if record.get("terminal_wake_state") == "session_parent":
                            continue
                        parent_session_id = str(lease.get("delegation_owner_id") or "").strip()
                        record["terminal_wake_state"] = "session_parent"
                        record["terminal_wake_error"] = None
                        terminal_continuations.append({
                            "assignment_id": assignment_id,
                            "controller_id": None,
                            "wake_state": "session_parent",
                            "delegation_parent_session_id": parent_session_id,
                            "terminal_receipt": str(terminal_receipt),
                            "error": None,
                        })
                        continue
                    if record.get("terminal_wake_state") in {"confirmed", "delegated"}:
                        continue
                    consumer = terminal_consumer or terminal_continuation.consume_terminal_receipt
                    try:
                        continuation = consumer(
                            repo=repo,
                            receipt_path=terminal_receipt,
                            registry_path=registry,
                        )
                        wake_state = _wake_state_from_continuation(continuation)
                        error = None
                    except (OSError, ValueError, PermissionError, RuntimeError, subprocess.SubprocessError) as exc:
                        continuation = None
                        wake_state = "pending"
                        error = f"{type(exc).__name__}: {exc}"
                    record["terminal_wake_state"] = wake_state
                    record["terminal_wake_error"] = error
                    terminal_continuations.append({
                        "assignment_id": assignment_id,
                        "controller_id": (
                            str(continuation.get("controller_id") or controller_id)
                            if isinstance(continuation, dict) else controller_id
                        ),
                        "wake_state": wake_state,
                        "terminal_receipt": str(terminal_receipt),
                        "error": error,
                    })
                    continue

                if lease.get("terminal_state"):
                    continue
                if record.get("health_wake_state") in {"confirmed", "delegated"}:
                    continue
                health = watch_web_assignment_once(
                    repo=repo,
                    registry_path=registry,
                    assignment_id=assignment_id,
                    expected_attempt=int(lease["attempt"]),
                    expected_lease_id=str(lease["lease_id"]),
                    now=now,
                    runtime_change_consumer=runtime_change_consumer,
                )
                wake_state = None
                if health.get("runtime_state") in {"unhealthy", "budget_exhausted"}:
                    continuation = health.get("runtime_continuation")
                    wake_state = _wake_state_from_continuation(
                        continuation if isinstance(continuation, dict) else None
                    )
                    record["health_wake_state"] = wake_state
                    record["health_reason"] = health.get("reason")
                health["wake_state"] = wake_state
                health_results.append(health)

            reconcile_state["last_observed_at"] = (
                now if getattr(now, "tzinfo", None) else now.replace(tzinfo=timezone.utc)
            ).astimezone(timezone.utc).isoformat()
            reconcile_state["controller_id"] = controller_id
            _write_web_assignment_reconcile_state(repo, reconcile_state)
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

    controller_continuation = controller_continuation_projection(
        repo=repo,
        controller_id=controller_id,
        registry=registry,
    )
    supervisor_armed = False
    if controller_continuation.get("should_continue") is True:
        supervisor_armed = ensure_continuation_supervisor(
            lifecycle_state=dict(controller_continuation.get("lifecycle_state") or {}),
            session_id=controller_id,
            repo=repo,
            registry=registry,
            codex="/opt/homebrew/bin/codex",
        )
    controller_continuation["supervisor_armed"] = supervisor_armed

    return {
        "controller_id": controller_id,
        "event_paths": [str(path) for path in paths],
        "bound_dispatches": bound_dispatches,
        "binding_errors": binding_errors,
        "machine_event_source_error": source_error,
        "terminal_continuations": terminal_continuations,
        "health": health_results,
        "controller_continuation": controller_continuation,
        "reconcile_state_path": str(state_path),
    }


def audit_receipts_from_cursor(audit_log: Path, cursor_path: Path) -> list[dict[str, Any]]:
    records, _ = audit_records_from_cursor(audit_log, cursor_path)
    return [receipt for receipt, _ in records]


def _audit_receipt_state_path(cursor_path: Path) -> Path:
    return cursor_path.with_suffix(cursor_path.suffix + ".receipts.json")


def _audit_receipt_key(receipt: dict[str, Any]) -> str:
    receipt_id = str(receipt.get("receiptId") or "").strip()
    if receipt_id:
        return receipt_id
    import hashlib
    canonical = json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _audit_receipt_status(cursor_path: Path, receipt: dict[str, Any]) -> str | None:
    data = load_json(_audit_receipt_state_path(cursor_path))
    receipts = data.get("receipts", {}) if isinstance(data, dict) else {}
    return receipts.get(_audit_receipt_key(receipt)) if isinstance(receipts, dict) else None


def _audit_receipt_wake_fingerprint(cursor_path: Path, receipt: dict[str, Any]) -> str | None:
    data = load_json(_audit_receipt_state_path(cursor_path))
    fingerprints = data.get("wake_fingerprints", {}) if isinstance(data, dict) else {}
    value = fingerprints.get(_audit_receipt_key(receipt)) if isinstance(fingerprints, dict) else None
    return value if isinstance(value, str) and value else None


def _set_audit_receipt_status(
    cursor_path: Path, receipt: dict[str, Any], status: str, *, wake_fingerprint: str | None = None
) -> None:
    path = _audit_receipt_state_path(cursor_path)
    data = load_json(path)
    receipts = data.get("receipts", {}) if isinstance(data.get("receipts"), dict) else {}
    fingerprints = data.get("wake_fingerprints", {}) if isinstance(data.get("wake_fingerprints"), dict) else {}
    key = _audit_receipt_key(receipt)
    receipts[key] = status
    if status == "wake_pending" and wake_fingerprint:
        fingerprints[key] = wake_fingerprint
    elif status != "wake_pending":
        fingerprints.pop(key, None)
    if len(receipts) > 256:
        keep = set(list(receipts.keys())[-256:])
        receipts = {key: value for key, value in receipts.items() if key in keep}
        fingerprints = {key: value for key, value in fingerprints.items() if key in keep}
    _write_json_atomic_file(path, {"receipts": receipts, "wake_fingerprints": fingerprints})


def _advance_audit_cursor(cursor_path: Path, inode: int, offset: int) -> None:
    _write_json_atomic_file(cursor_path, {"inode": inode, "offset": offset})


def computer_event_from_receipt(
    receipt: dict[str, Any],
    *,
    session_id: str,
    repo: Path,
    lease_path: Path,
    web_session_id: str | None = None,
    now_unix_ms: int | None = None,
) -> dict[str, Any] | None:
    if receipt.get("childTool") != "computer":
        return None
    if receipt.get("state") not in {"succeeded", "failed"}:
        return None
    lease = load_json(lease_path)
    if not lease:
        return None
    if lease.get("session_id") != session_id:
        return None
    if str(lease.get("repo") or "") != str(repo.resolve()):
        return None
    if web_session_id is not None and lease.get("web_session_id") != web_session_id:
        return None
    now_ms = int(time.time() * 1000) if now_unix_ms is None else now_unix_ms
    issued_ms = int(lease.get("issued_at_unix_ms", 0) or 0)
    expires_ms = int(lease.get("expires_at_unix_ms", 0) or 0)
    remaining = int(lease.get("remaining_uses", 0) or 0)
    receipt_ms = int(receipt.get("occurredAtUnixMs", 0) or 0)
    if remaining <= 0 or expires_ms <= now_ms:
        lease_path.unlink(missing_ok=True)
        return None
    if receipt_ms and issued_ms and receipt_ms < issued_ms:
        return None

    detail = receipt.get("detail")
    target = receipt.get("targetLabel")
    detail_text = detail if isinstance(detail, str) else ""
    normalized_detail = detail_text.casefold().strip()
    operation = ""
    for marker in ("电脑操作：", "电脑操作:", "computer operation:", "computer operation："):
        marker_index = normalized_detail.find(marker)
        if marker_index >= 0:
            operation = normalized_detail[marker_index + len(marker):].split("·", 1)[0].strip()
            break
    observational_operations = {"get_app_state", "get app state", "screenshot", "list_windows", "list windows", "observe"}
    requires_followup = operation not in observational_operations
    event = {
        "hook_event_name": "PostToolUse",
        "controller_host": "web",
        "execution_host": "web",
        "event_source": "web",
        "controller_id": session_id,
        "controller_session_id": session_id,
        "web_session_id": web_session_id or lease.get("web_session_id"),
        "session_id": session_id,
        "turn_id": f"web-audit:{receipt.get('receiptId') or 'computer'}",
        "cwd": str(repo.resolve()),
        "tool_name": "AI-Bridge.computer",
        "tool_input": {
            "detail": detail if isinstance(detail, str) else "",
            "target": target if isinstance(target, str) else "",
        },
        "tool_response": {"state": receipt.get("state")},
    }
    if requires_followup:
        event["next_action"] = "observe and read the result of the computer action before yielding"
        event["requires_user"] = False
    remaining -= 1
    if remaining <= 0:
        lease_path.unlink(missing_ok=True)
    else:
        lease["remaining_uses"] = remaining
        lease_path.write_text(json.dumps(lease, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return event


def write_computer_lease(
    *, lease_path: Path, session_id: str, web_session_id: str, repo: Path, ttl_seconds: int, uses: int
) -> dict[str, Any]:
    now_ms = int(time.time() * 1000)
    value = {
        "session_id": session_id,
        "web_session_id": web_session_id,
        "repo": str(repo.resolve()),
        "issued_at_unix_ms": now_ms,
        "expires_at_unix_ms": now_ms + ttl_seconds * 1000,
        "remaining_uses": uses,
    }
    lease_path.parent.mkdir(parents=True, exist_ok=True)
    lease_path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return value


def default_computer_lease_path(session_id: str) -> Path:
    return (
        Path.home()
        / ".codex"
        / "state"
        / "adaptive-delivery-web-lifecycle"
        / f"{session_id}.computer-lease.json"
    )


def bounded_tail(value: str, limit: int = STDERR_TAIL_LIMIT) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    marker = "\n...[diagnostic truncated]...\n"
    head_size = min(1024, max(1, limit // 4))
    tail_size = max(1, limit - head_size - len(marker))
    return text[:head_size] + marker + text[-tail_size:]


def native_runtime_env(runtime_path: str | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env["PATH"] = runtime_path or DEFAULT_RUNTIME_PATH
    return env


def codex_requires_node(codex: Path) -> bool:
    try:
        with codex.open("r", encoding="utf-8", errors="ignore") as handle:
            first = handle.readline(512)
    except OSError:
        return False
    return "/usr/bin/env node" in first


def write_auto_stop_state(state_path: Path, value: dict[str, Any]) -> None:
    _write_json_atomic_file(state_path, value)


def resolve_native_resume_target(
    *, session_id: str, repo: Path, registry: Path
) -> dict[str, Any]:
    receipt = target_guard.resolve_execution_target(
        repo=repo,
        host=target_guard.DESKTOP_SESSION_HOST,
        registry_path=registry,
    )
    if receipt.get("controller_id") != session_id:
        raise PermissionError(
            f"session {session_id} is not the registered Controller for {repo}"
        )
    return receipt


def preflight_native_resume(
    *, session_id: str, repo: Path, registry: Path, codex: str, runtime_path: str | None = None
) -> tuple[bool, str, dict[str, str]]:
    env = native_runtime_env(runtime_path)
    if not repo.is_dir():
        return False, f"repository does not exist: {repo}", env
    codex_path = Path(codex).expanduser()
    if not codex_path.is_file() or not os.access(codex_path, os.X_OK):
        return False, f"codex executable unavailable: {codex_path}", env
    if codex_requires_node(codex_path) and shutil.which("node", path=env["PATH"]) is None:
        return False, f"missing node runtime in PATH for {codex_path}: {env['PATH']}", env
    completed = subprocess.run(
        [str(codex_path), "--version"], check=False, capture_output=True, text=True, env=env, timeout=15
    )
    if completed.returncode != 0:
        detail = bounded_tail(completed.stderr or completed.stdout)
        return False, f"codex preflight failed ({completed.returncode}): {detail}", env
    return True, "", env


def rotate_launcher_log(path: Path, max_bytes: int = LAUNCHER_LOG_LIMIT) -> None:
    try:
        if not path.exists() or path.stat().st_size < max_bytes:
            return
        previous = path.with_suffix(path.suffix + ".1")
        previous.unlink(missing_ok=True)
        path.replace(previous)
    except OSError:
        return


def native_resume_command(
    *, codex: str, session_id: str, repo: Path, terminal_receipts: Sequence[str] | None = None,
    next_action: str | None = None,
) -> list[str]:
    prompt = (
        "Adaptive Agent Runtime Web Stop checkpoint. Continue this existing registered controller "
        "thread only; do not create or fork another controller. Reconcile any pending lifecycle "
        "control event against the real main, ledger, live tasks, READY queue and candidates. "
        "Even when pending_control_event is false, perform one project-wide Goal rollover check: "
        "if the just-closed Goal has completed and the project still has executable open work, "
        "recompute readiness and roll to the next Goal before yielding; if everything is blocked, "
        "require the project-wide blocking proof. Obey the installed lifecycle hooks and "
        "control_event_guard; if no pending control action or Goal rollover remains, stop without "
        "starting unrelated work."
    )
    receipts = [str(item).strip() for item in (terminal_receipts or []) if str(item).strip()]
    if receipts:
        prompt += " Pending terminal receipts: " + "; ".join(receipts) + ". Read these durable results first and continue from them."
    pending_next_action = str(next_action or "").strip()
    if pending_next_action:
        prompt += " Persisted non-user next action: " + pending_next_action + ". Complete it before yielding."
    return [codex, "exec", "--json", "-C", str(repo.resolve()), "resume", session_id, prompt]


def _terminate_process_group(
    process: subprocess.Popen[str], *, grace_seconds: float
) -> None:
    process_group_id = process.pid

    def group_exists() -> bool:
        try:
            os.killpg(process_group_id, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    process.poll()
    if not group_exists():
        return
    try:
        os.killpg(process_group_id, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + max(0.0, grace_seconds)
    while group_exists() and time.monotonic() < deadline:
        process.poll()
        time.sleep(0.01)
    process.poll()
    if not group_exists():
        if process.poll() is None:
            process.wait(timeout=max(1.0, grace_seconds))
        return
    try:
        os.killpg(process_group_id, signal.SIGKILL)
    except ProcessLookupError:
        pass
    if process.poll() is None:
        process.wait(timeout=max(1.0, grace_seconds))


def _collect_native_resume(
    process: subprocess.Popen[str], *, max_runtime_seconds: float,
    completion_grace_seconds: float,
) -> tuple[str, str, int, bool, bool]:
    stdout_lines: deque[str] = deque(maxlen=256)
    stderr_lines: deque[str] = deque(maxlen=256)
    turn_completed = threading.Event()

    def drain(stream: Any, output: deque[str], *, detect_completion: bool) -> None:
        if stream is None:
            return
        for line in stream:
            output.append(bounded_tail(str(line)))
            if not detect_completion:
                continue
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(event, dict) and event.get("type") == "turn.completed":
                turn_completed.set()

    stdout_thread = threading.Thread(
        target=drain, args=(process.stdout, stdout_lines),
        kwargs={"detect_completion": True}, daemon=True,
    )
    stderr_thread = threading.Thread(
        target=drain, args=(process.stderr, stderr_lines),
        kwargs={"detect_completion": False}, daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()

    deadline = time.monotonic() + max(0.0, max_runtime_seconds)
    completed_by_event = False
    timed_out = False
    while process.poll() is None:
        if turn_completed.wait(timeout=0.1):
            completed_by_event = True
            try:
                process.wait(timeout=max(0.0, completion_grace_seconds))
            except subprocess.TimeoutExpired:
                pass
            _terminate_process_group(
                process, grace_seconds=completion_grace_seconds
            )
            break
        if time.monotonic() >= deadline:
            timed_out = True
            _terminate_process_group(
                process, grace_seconds=completion_grace_seconds
            )
            break

    stdout_thread.join(timeout=max(1.0, completion_grace_seconds))
    stderr_thread.join(timeout=max(1.0, completion_grace_seconds))
    if not stdout_thread.is_alive() and not stderr_thread.is_alive():
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()
    stdout = bounded_tail("".join(stdout_lines))
    stderr = bounded_tail("".join(stderr_lines))
    if completed_by_event:
        return stdout, stderr, 0, True, False
    return (
        stdout,
        stderr,
        process.returncode if process.returncode is not None else 78,
        False,
        timed_out,
    )


def execute_native_resume(
    *,
    session_id: str,
    repo: Path,
    registry: Path,
    codex: str,
    runtime_path: str | None = None,
    terminal_receipts: Sequence[str] | None = None,
    next_action: str | None = None,
    supervisor_state_path: Path | None = None,
    supervisor_receipt_id: str | None = None,
    supervisor_token: str | None = None,
    max_runtime_seconds: float = NATIVE_RESUME_MAX_RUNTIME_SECONDS,
    completion_grace_seconds: float = NATIVE_RESUME_COMPLETION_GRACE_SECONDS,
) -> dict[str, Any]:
    """Run one bounded, preflighted same-thread native resume attempt."""
    try:
        ok, preflight_error, env = preflight_native_resume(
            session_id=session_id,
            repo=repo,
            registry=registry,
            codex=codex,
            runtime_path=runtime_path,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        ok, preflight_error, env = (
            False,
            f"native resume preflight error: {exc}",
            native_runtime_env(runtime_path),
        )
    if not ok:
        return {
            "operation": "native_resume",
            "controller_id": session_id,
            "command": native_resume_command(
                codex=codex,
                session_id=session_id,
                repo=repo,
                terminal_receipts=terminal_receipts,
                next_action=next_action,
            ),
            "result": "FAILED",
            "state": "RESUME_FAILED",
            "pending_control_event": True,
            "returncode": 78,
            "stderr_tail": bounded_tail(preflight_error),
            "error_code": "WEB_LIFECYCLE_RESUME_FAILED",
        }

    target_receipt: dict[str, Any] | None = None
    command = native_resume_command(
        codex=codex,
        session_id=session_id,
        repo=repo,
        terminal_receipts=terminal_receipts,
        next_action=next_action,
    )
    process: subprocess.Popen[str] | None = None
    try:
        with target_guard.locked_execution_target(
            repo=repo,
            host=target_guard.DESKTOP_SESSION_HOST,
            registry_path=registry,
        ) as locked_target:
            if locked_target.get("controller_id") != session_id:
                raise PermissionError(
                    f"session {session_id} is not the registered Controller for {repo}"
                )
            target_receipt = locked_target
            execution_target = str(target_receipt["execution_target_session_id"])
            command = native_resume_command(
                codex=codex,
                session_id=execution_target,
                repo=repo,
                terminal_receipts=terminal_receipts,
                next_action=next_action,
            )
            if (
                supervisor_state_path is not None
                and supervisor_receipt_id is not None
                and supervisor_token is not None
            ):
                with _owned_supervisor_state(
                    supervisor_state_path,
                    receipt_id=supervisor_receipt_id,
                    supervisor_token=supervisor_token,
                ) as owner:
                    if owner is None:
                        return {
                            "operation": "native_resume",
                            "controller_id": session_id,
                            "execution_target_session_id": execution_target,
                            "target_generation": target_receipt.get("generation"),
                            "target_mode": target_receipt.get("target_mode"),
                            "command": command,
                            "result": "DEFERRED",
                            "state": "RESUME_SUPERSEDED",
                            "pending_control_event": True,
                            "returncode": 0,
                            "failure_class": "supervisor_superseded",
                        }
                    process = subprocess.Popen(
                        command,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        env=env,
                        start_new_session=True,
                    )
            else:
                process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=env,
                    start_new_session=True,
                )
    except (OSError, ValueError, PermissionError, subprocess.SubprocessError) as exc:
        target_rejected = target_receipt is None
        return {
            "operation": "native_resume",
            "controller_id": session_id,
            **({} if target_receipt is None else {
                "execution_target_session_id": target_receipt.get("execution_target_session_id"),
                "target_generation": target_receipt.get("generation"),
                "target_mode": target_receipt.get("target_mode"),
            }),
            "command": command,
            "result": "FAILED",
            "state": "RESUME_FAILED",
            "pending_control_event": True,
            "returncode": 78,
            "stderr_tail": bounded_tail(
                ("Controller target guard rejected resume: " if target_rejected else "native resume execution error: ")
                + str(exc)
            ),
            "error_code": (
                "CONTROLLER_TARGET_REJECTED"
                if target_rejected
                else "WEB_LIFECYCLE_RESUME_FAILED"
            ),
        }

    try:
        if process is None:
            stdout, stderr, returncode, completed_by_event, timed_out = (
                "", "", 78, False, False
            )
        else:
            stdout, stderr, returncode, completed_by_event, timed_out = _collect_native_resume(
                process,
                max_runtime_seconds=max_runtime_seconds,
                completion_grace_seconds=completion_grace_seconds,
            )
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "operation": "native_resume",
            "controller_id": session_id,
            "execution_target_session_id": target_receipt.get("execution_target_session_id"),
            "target_generation": target_receipt.get("generation"),
            "target_mode": target_receipt.get("target_mode"),
            "command": command,
            "result": "FAILED",
            "state": "RESUME_FAILED",
            "pending_control_event": True,
            "returncode": 78,
            "stderr_tail": bounded_tail(f"native resume execution error: {exc}"),
            "error_code": "WEB_LIFECYCLE_RESUME_FAILED",
        }

    attempt: dict[str, Any] = {
        "operation": "native_resume",
        "controller_id": session_id,
        "execution_target_session_id": execution_target,
        "target_generation": target_receipt.get("generation"),
        "target_mode": target_receipt.get("target_mode"),
        "command": command,
        "pending_control_event": True,
        "returncode": returncode,
        "stdout_tail": bounded_tail(stdout),
        "stderr_tail": bounded_tail(stderr),
    }
    if completed_by_event:
        attempt["completion_source"] = "codex_turn_completed"
    if timed_out:
        attempt.update({
            "result": "FAILED",
            "state": "RESUME_FAILED",
            "failure_class": "native_resume_timeout",
            "error_code": "WEB_LIFECYCLE_RESUME_TIMEOUT",
            "host_returncode": returncode,
            "returncode": 124,
        })
        return attempt
    if returncode == 0:
        attempt.update({"result": "CONFIRMED", "state": "RESUME_SUCCEEDED"})
        return attempt
    attempt.update(classify_native_resume_failure(
        returncode, stdout, stderr
    ))
    if attempt.get("state") == "RESUME_DEFERRED_ACTIVE_WRITER":
        # This observation is produced only by the native Host command after
        # the exact Desktop target was resolved under the target guard. It is
        # not Controller identity attestation and must still pass canonical
        # target and ownership-generation fences before becoming confirmation.
        attempt["host_observation"] = _HOST_OBSERVED_CANONICAL_TARGET_FOREGROUND
    attempt["result"] = "DEFERRED" if attempt["state"] == "RESUME_DEFERRED_ACTIVE_WRITER" else "FAILED"
    return attempt


def desktop_host_reload_required(
    *,
    session_id: str,
    repo: Path,
    canary_path: Path = DEFAULT_DESKTOP_CANARY,
    hooks_path: Path = DEFAULT_CODEX_HOOKS,
    registry_path: Path = DEFAULT_REGISTRY,
) -> bool:
    """Require a fresh Desktop process when an exact armed canary has not started."""
    canary = load_json(canary_path)
    dot_git = repo.resolve() / ".git"
    common_dir = dot_git
    if dot_git.is_file():
        try:
            marker = dot_git.read_text(encoding="utf-8").strip()
        except OSError:
            marker = ""
        if marker.startswith("gitdir:"):
            git_dir = Path(marker.removeprefix("gitdir:").strip()).expanduser()
            if not git_dir.is_absolute():
                git_dir = (repo / git_dir).resolve()
            try:
                relative_common = (git_dir / "commondir").read_text(
                    encoding="utf-8"
                ).strip()
            except OSError:
                relative_common = ""
            common_dir = (
                (git_dir / relative_common).resolve()
                if relative_common
                else git_dir.resolve()
            )
    handshake = load_json(
        common_dir / "adaptive-delivery" / "rule-handshake.json"
    )
    hooks_sha256 = _file_sha256(hooks_path)
    installed_revision = str(handshake.get("installed_revision") or "").strip()
    try:
        with target_guard.locked_registry(registry_path) as registry:
            if target_guard.unique_controller_id_for_repo_in_registry(
                repo, registry
            ) != session_id:
                return False
            target = target_guard.target_record(
                registry,
                controller_id=session_id,
                host=target_guard.DESKTOP_SESSION_HOST,
            )
            ownership = target_guard.execution_ownership_record(
                registry, controller_id=session_id
            )
            target_status, target_session_id, target_generation = (
                target_guard.validate_target_record(
                    target or {}, host=target_guard.DESKTOP_SESSION_HOST
                )
            )
            ownership_host, ownership_target, ownership_generation = (
                target_guard.validate_execution_ownership_record(ownership or {})
            )
    except (OSError, PermissionError, ValueError):
        return False
    return (
        handshake.get("live_e2e_required") is True
        and bool(installed_revision)
        and installed_revision
        == str(handshake.get("loaded_revision") or "").strip()
        and canary.get("schema_version") == 4
        and str(canary.get("controller_id") or "").strip() == session_id
        and str(canary.get("controller_session_id") or "").strip() == session_id
        and str(canary.get("canonical_repo") or "").strip() == str(repo.resolve())
        and str(canary.get("controller_registry_path") or "").strip()
        == str(registry_path.expanduser().resolve())
        and target_status == "active"
        and target_session_id == canary.get("execution_target_session_id")
        and target_generation == canary.get("target_generation")
        and ownership_host == target_guard.DESKTOP_SESSION_HOST
        and ownership_target == target_session_id
        and ownership_generation == canary.get("ownership_generation")
        and canary.get("status") == "armed"
        and canary.get("sequence_index") == 0
        and canary.get("observations") == []
        and bool(hooks_sha256)
        and canary.get("hooks_sha256") == hooks_sha256
    )


def confirm_host_observed_desktop_foreground(
    *,
    attempt: dict[str, Any],
    lifecycle_state: dict[str, Any],
    ownership_fence: dict[str, Any] | None,
    host_reload_required: bool = False,
) -> dict[str, Any]:
    """Confirm an exact Desktop wake when the Host reports its writer is already active."""
    if (
        lifecycle_state.get("pending_control_event") is not True
        or lifecycle_state.get("requires_user") is True
        or attempt.get("result") != "DEFERRED"
        or attempt.get("state") != "RESUME_DEFERRED_ACTIVE_WRITER"
        or attempt.get("failure_class") != "active_writer_present"
        or attempt.get("host_observation") is not _HOST_OBSERVED_CANONICAL_TARGET_FOREGROUND
        or not isinstance(ownership_fence, dict)
        or ownership_fence.get("active_host") != target_guard.DESKTOP_SESSION_HOST
        or attempt.get("target_mode") != "explicit_current"
    ):
        return attempt

    execution_target = str(attempt.get("execution_target_session_id") or "").strip()
    target_generation = attempt.get("target_generation")
    ownership_target = str(
        ownership_fence.get("execution_target_session_id") or ""
    ).strip()
    ownership_generation = ownership_fence.get("generation")
    if (
        not execution_target
        or execution_target != ownership_target
        or not isinstance(target_generation, int)
        or isinstance(target_generation, bool)
        or target_generation <= 0
        or not isinstance(ownership_generation, int)
        or isinstance(ownership_generation, bool)
        or ownership_generation <= 0
    ):
        return attempt

    if host_reload_required:
        deferred = dict(attempt)
        deferred.update({
            "state": "WAITING_EXTERNAL_HOST_RELOAD",
            "host_reload_required": True,
            "activation_gate": "host_reload_required",
            "failure_class": "host_reload_required",
            "error_code": "DESKTOP_HOST_RELOAD_REQUIRED",
            "host_returncode": attempt.get("returncode"),
            "returncode": 0,
        })
        return deferred

    confirmed = dict(attempt)
    confirmed.update({
        "operation": "native_resume_already_foreground",
        "result": "CONFIRMED",
        "state": "RESUME_CONFIRMED_ALREADY_FOREGROUND",
        "pending_control_event": True,
        "host_returncode": attempt.get("returncode"),
        "returncode": 0,
        "ownership_generation": ownership_generation,
    })
    for key in (
        "error_code",
        "failure_class",
        "fallback_eligible",
        "replacement_eligible",
    ):
        confirmed.pop(key, None)
    return confirmed



def _thread_id_from_json_lines(output: str) -> str | None:
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        for key in ("thread_id", "session_id", "threadId", "sessionId"):
            value = event.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        payload = event.get("payload")
        if isinstance(payload, dict):
            for key in ("thread_id", "session_id", "threadId", "sessionId"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return None


def replace_desktop_execution_target(
    *, controller_id: str, desktop_session_id: str, repo: Path,
    expected_generation: int, expected_ownership_generation: int, registry: Path,
) -> dict[str, Any]:
    lifecycle = _lifecycle_module()
    return lifecycle.replace_desktop_session(
        controller_id=controller_id,
        desktop_session_id=desktop_session_id,
        repo=repo,
        expected_generation=expected_generation,
        expected_ownership_generation=expected_ownership_generation,
        registry_path=registry,
    )


def recover_incompatible_native_target(
    *, session_id: str, repo: Path, registry: Path, codex: str,
    failed_target_session_id: str, expected_generation: int,
    expected_ownership_generation: int,
    runtime_path: str | None = None,
    terminal_receipts: Sequence[str] | None = None,
    next_action: str | None = None,
    supervisor_state_path: Path | None = None,
    supervisor_receipt_id: str | None = None,
    supervisor_token: str | None = None,
) -> dict[str, Any]:
    """Replace an unreadable desktop execution target, never the logical Controller."""
    if (
        supervisor_token is not None
        and supervisor_state_path is not None
        and supervisor_receipt_id is not None
    ):
        with _owned_supervisor_state(
            supervisor_state_path,
            receipt_id=supervisor_receipt_id,
            supervisor_token=supervisor_token,
        ) as owner:
            if owner is None:
                return {
                    "operation": "native_target_recovery",
                    "controller_id": session_id,
                    "result": "DEFERRED",
                    "state": "RESUME_SUPERSEDED",
                    "pending_control_event": True,
                    "returncode": 0,
                    "failure_class": "supervisor_superseded",
                    "recovered_from_execution_target_session_id": failed_target_session_id,
                }
    try:
        ok, preflight_error, env = preflight_native_resume(
            session_id=session_id, repo=repo, registry=registry, codex=codex,
            runtime_path=runtime_path,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        ok, preflight_error, env = False, f"native target recovery preflight error: {exc}", native_runtime_env(runtime_path)
    if not ok:
        return {
            "operation": "native_target_recovery", "controller_id": session_id,
            "result": "FAILED", "state": "RESUME_TARGET_RECOVERY_FAILED",
            "pending_control_event": True, "returncode": 78,
            "stderr_tail": bounded_tail(preflight_error),
            "failure_class": "target_recovery_failed",
            "error_code": "WEB_LIFECYCLE_TARGET_RECOVERY_FAILED",
            "recovered_from_execution_target_session_id": failed_target_session_id,
        }
    bootstrap_prompt = (
        "Adaptive Agent Runtime execution-target recovery bootstrap for existing logical Controller "
        f"{session_id}. Do not create, appoint, or claim another logical Controller. Do not modify "
        "project files and do not start project work in this bootstrap turn. Reply exactly TARGET_READY."
    )
    bootstrap_command = [codex, "exec", "--json", "-C", str(repo.resolve()), bootstrap_prompt]
    try:
        if (
            supervisor_token is not None
            and supervisor_state_path is not None
            and supervisor_receipt_id is not None
        ):
            # Fence ownership immediately before launching the bootstrap, but never hold
            # the supervisor lock across external work. A newer generation must be able
            # to supersede this worker while Codex bootstraps; the post-bootstrap fence
            # below prevents the stale generation from replacing the canonical target.
            with _owned_supervisor_state(
                supervisor_state_path,
                receipt_id=supervisor_receipt_id,
                supervisor_token=supervisor_token,
            ) as owner:
                if owner is None:
                    return {
                        "operation": "native_target_recovery",
                        "controller_id": session_id,
                        "result": "DEFERRED",
                        "state": "RESUME_SUPERSEDED",
                        "pending_control_event": True,
                        "returncode": 0,
                        "failure_class": "supervisor_superseded",
                        "recovered_from_execution_target_session_id": failed_target_session_id,
                    }
        completed = subprocess.run(
            bootstrap_command, check=False, capture_output=True, text=True, env=env, timeout=120
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "operation": "native_target_recovery", "controller_id": session_id,
            "command": bootstrap_command, "result": "FAILED",
            "state": "RESUME_TARGET_RECOVERY_FAILED", "pending_control_event": True,
            "returncode": 78, "stderr_tail": bounded_tail(str(exc)),
            "failure_class": "target_recovery_failed",
            "error_code": "WEB_LIFECYCLE_TARGET_RECOVERY_FAILED",
            "recovered_from_execution_target_session_id": failed_target_session_id,
        }
    new_target = _thread_id_from_json_lines(completed.stdout)
    if completed.returncode != 0 or not new_target:
        return {
            "operation": "native_target_recovery", "controller_id": session_id,
            "command": bootstrap_command, "result": "FAILED",
            "state": "RESUME_TARGET_RECOVERY_FAILED", "pending_control_event": True,
            "returncode": completed.returncode or 78,
            "stdout_tail": bounded_tail(completed.stdout), "stderr_tail": bounded_tail(completed.stderr),
            "failure_class": "target_recovery_failed",
            "error_code": "WEB_LIFECYCLE_TARGET_RECOVERY_FAILED",
            "recovered_from_execution_target_session_id": failed_target_session_id,
        }
    if new_target in {session_id, failed_target_session_id}:
        return {
            "operation": "native_target_recovery", "controller_id": session_id,
            "command": bootstrap_command, "result": "FAILED",
            "state": "RESUME_TARGET_RECOVERY_FAILED", "pending_control_event": True,
            "returncode": 78,
            "stderr_tail": "replacement bootstrap did not produce a distinct execution target",
            "failure_class": "target_recovery_failed",
            "error_code": "WEB_LIFECYCLE_TARGET_RECOVERY_FAILED",
            "recovered_from_execution_target_session_id": failed_target_session_id,
        }
    try:
        if supervisor_token is not None and supervisor_state_path is not None and supervisor_receipt_id is not None:
            with _owned_supervisor_state(
                supervisor_state_path,
                receipt_id=supervisor_receipt_id,
                supervisor_token=supervisor_token,
            ) as owner:
                if owner is None:
                    return {
                        "operation": "native_target_recovery", "controller_id": session_id,
                        "result": "DEFERRED", "state": "RESUME_SUPERSEDED",
                        "pending_control_event": True, "returncode": 0,
                        "failure_class": "supervisor_superseded",
                        "recovered_from_execution_target_session_id": failed_target_session_id,
                        "candidate_execution_target_session_id": new_target,
                    }
                replacement = replace_desktop_execution_target(
                    controller_id=session_id, desktop_session_id=new_target, repo=repo,
                    expected_generation=expected_generation,
                    expected_ownership_generation=expected_ownership_generation,
                    registry=registry,
                )
        else:
            replacement = replace_desktop_execution_target(
                controller_id=session_id, desktop_session_id=new_target, repo=repo,
                expected_generation=expected_generation,
                expected_ownership_generation=expected_ownership_generation,
                registry=registry,
            )
    except (OSError, ValueError, PermissionError, subprocess.SubprocessError) as exc:
        return {
            "operation": "native_target_recovery", "controller_id": session_id,
            "command": bootstrap_command, "result": "FAILED",
            "state": "RESUME_TARGET_RECOVERY_FAILED", "pending_control_event": True,
            "returncode": 78, "stderr_tail": bounded_tail(f"target replacement rejected: {exc}"),
            "failure_class": "target_recovery_failed",
            "error_code": "WEB_LIFECYCLE_TARGET_RECOVERY_FAILED",
            "recovered_from_execution_target_session_id": failed_target_session_id,
            "candidate_execution_target_session_id": new_target,
        }
    resumed = execute_native_resume(
        session_id=session_id, repo=repo, registry=registry, codex=codex,
        runtime_path=runtime_path, terminal_receipts=terminal_receipts,
        next_action=next_action,
        supervisor_state_path=supervisor_state_path,
        supervisor_receipt_id=supervisor_receipt_id,
        supervisor_token=supervisor_token,
    )
    resumed = dict(resumed)
    resumed["operation"] = "native_target_recovery"
    resumed["recovered_from_execution_target_session_id"] = failed_target_session_id
    resumed["replacement_execution_target_session_id"] = new_target
    resumed["target_generation"] = replacement.get("generation", resumed.get("target_generation"))
    resumed["ownership_generation"] = replacement.get(
        "ownership_generation", resumed.get("ownership_generation")
    )
    return resumed


def wake_receipt_needs_auto_native_stop(receipt: object) -> bool:
    if not isinstance(receipt, dict):
        return False
    if receipt.get("result") == "DEFERRED":
        return True
    return (
        receipt.get("result") == "FAILED"
        and receipt.get("failure_class") == "target_schema_incompatible"
        and receipt.get("replacement_eligible") is True
    )

def controller_wake_lock_path(repo: Path) -> Path:
    return _git_common_dir(repo) / "adaptive-delivery" / "controller-wake.lock"


def _registered_controller_for_common_dir(repo: Path, registry_path: Path) -> str | None:
    common_dir = _git_common_dir(repo)
    registry = load_json(registry_path)
    matches: list[str] = []
    for session_id, registered_repo in registry.items():
        if not isinstance(session_id, str) or not isinstance(registered_repo, str):
            continue
        try:
            if _git_common_dir(Path(registered_repo).expanduser().resolve()) == common_dir:
                matches.append(session_id)
        except (OSError, subprocess.SubprocessError):
            continue
    if len(matches) != 1:
        return None
    return matches[0]


def _wake_event_fingerprint(lifecycle_state: dict[str, Any]) -> str:
    snapshot = lifecycle_state.get("snapshot")
    generation: dict[str, Any] = {}
    if isinstance(snapshot, dict):
        generation = {
            "head": snapshot.get("head"),
            "ledger_sha256": snapshot.get("ledger_sha256"),
            "worktree_status_sha256": snapshot.get("worktree_status_sha256"),
            "ready_ids": snapshot.get("ready_ids", []),
            "runnable_ids": snapshot.get("runnable_ids", []),
            "candidate_revisions": snapshot.get("candidate_revisions", []),
            "rule_handshake": snapshot.get("rule_handshake", {}),
        }
    value = {
        "pending_control_event": lifecycle_state.get("pending_control_event") is True,
        "triggers": lifecycle_state.get("triggers", []),
        "wake_generation": int(lifecycle_state.get("wake_generation", 0) or 0),
        "event_generation": generation,
        "next_action": str(lifecycle_state.get("next_action") or "").strip(),
        "requires_user": lifecycle_state.get("requires_user") is True,
        "pending_terminal_receipts": lifecycle_state.get("pending_terminal_receipts", []),
    }
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return __import__("hashlib").sha256(encoded).hexdigest()


def _file_sha256(path: Path) -> str:
    try:
        return __import__("hashlib").sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _bounded_adapter_operation(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return f"<non-text adapter operation: {type(value).__name__}>"
    return _bounded_text(value, 512)[0]


def _bounded_adapter_command(value: Any) -> list[str] | None:
    if not isinstance(value, (list, tuple)):
        return None
    remaining = STDERR_TAIL_LIMIT
    command: list[str] = []
    for argument in value[:64]:
        if not isinstance(argument, str):
            return None
        if remaining <= 0:
            break
        normalized, _ = _bounded_text(argument, min(1024, remaining))
        command.append(normalized)
        remaining -= len(normalized.encode("utf-8"))
    return command


def _bounded_adapter_diagnostics(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return f"<non-text adapter diagnostics: {type(value).__name__}>"
    return bounded_tail(value)


def _rejecting_peer_attestation_verifier(message: str) -> Callable[..., Any]:
    def reject(**_kwargs: Any) -> Any:
        raise PermissionError(message)
    return reject


def _external_peer_attestation_verifier(host: str) -> Callable[..., Any] | None:
    config_path = Path(DEFAULT_PEER_ATTESTATION_VERIFIER_CONFIG).expanduser()
    if not config_path.exists():
        return None
    try:
        config_stat = config_path.lstat()
        if config_path.is_symlink() or not config_path.is_file():
            raise PermissionError("registered Host verifier config must be a regular non-symlink file")
        if hasattr(os, "getuid") and config_stat.st_uid != os.getuid():
            raise PermissionError("registered Host verifier config owner mismatch")
        if config_stat.st_mode & 0o077:
            raise PermissionError("registered Host verifier config permissions must be 0600 or stricter")
        config = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(config, dict) or config.get("schema_version") != 1:
            raise PermissionError("registered Host verifier config schema is invalid")
        verifiers = config.get("verifiers")
        if not isinstance(verifiers, dict):
            raise PermissionError("registered Host verifier map is invalid")
        record = verifiers.get(host)
        if record is None:
            return None
        if not isinstance(record, dict):
            raise PermissionError("registered Host verifier record is invalid")
        if record.get("protocol") != "runtime_host_verifier_cli_v1":
            raise PermissionError("registered Host verifier protocol is unsupported")
        executable_raw = record.get("executable")
        digest = record.get("sha256")
        if not isinstance(executable_raw, str) or not executable_raw.strip():
            raise PermissionError("registered Host verifier executable is missing")
        executable = Path(executable_raw).expanduser()
        if not executable.is_absolute():
            raise PermissionError("registered Host verifier executable must be absolute")
        executable_stat = executable.lstat()
        if executable.is_symlink() or not executable.is_file():
            raise PermissionError("registered Host verifier executable must be a regular non-symlink file")
        if hasattr(os, "getuid") and executable_stat.st_uid != os.getuid():
            raise PermissionError("registered Host verifier executable owner mismatch")
        if executable_stat.st_mode & 0o111 == 0:
            raise PermissionError("registered Host verifier executable is not executable")
        actual_digest = __import__("hashlib").sha256(executable.read_bytes()).hexdigest()
        if not isinstance(digest, str) or len(digest) != 64 or not secrets.compare_digest(actual_digest, digest.lower()):
            raise PermissionError("registered Host verifier executable hash mismatch")
        bundle = record.get("bundle_sha256")
        if not isinstance(bundle, dict) or str(executable) not in bundle:
            raise PermissionError("registered Host verifier bundle hash manifest is incomplete")
        if len(bundle) > 64:
            raise PermissionError("registered Host verifier bundle hash manifest is too large")
        for bundle_path_raw, bundle_digest in bundle.items():
            if not isinstance(bundle_path_raw, str) or not bundle_path_raw.strip():
                raise PermissionError("registered Host verifier bundle path is invalid")
            bundle_path = Path(bundle_path_raw).expanduser()
            if not bundle_path.is_absolute():
                raise PermissionError("registered Host verifier bundle path must be absolute")
            bundle_stat = bundle_path.lstat()
            if bundle_path.is_symlink() or not bundle_path.is_file():
                raise PermissionError("registered Host verifier bundle member must be a regular non-symlink file")
            if hasattr(os, "getuid") and bundle_stat.st_uid != os.getuid():
                raise PermissionError("registered Host verifier bundle member owner mismatch")
            actual_bundle_digest = __import__("hashlib").sha256(bundle_path.read_bytes()).hexdigest()
            if (
                not isinstance(bundle_digest, str)
                or len(bundle_digest) != 64
                or not secrets.compare_digest(actual_bundle_digest, bundle_digest.lower())
            ):
                raise PermissionError("registered Host verifier bundle member hash mismatch")
    except Exception as exc:
        return _rejecting_peer_attestation_verifier(
            f"registered Host verifier configuration rejected: {exc}"
        )

    safe_env = {
        "HOME": str(Path.home()),
        "PATH": DEFAULT_RUNTIME_PATH,
        "LANG": "C.UTF-8",
    }

    def validate_pinned_bundle() -> None:
        for bundle_path_raw, bundle_digest in bundle.items():
            bundle_path = Path(bundle_path_raw).expanduser()
            bundle_stat = bundle_path.lstat()
            if bundle_path.is_symlink() or not bundle_path.is_file():
                raise PermissionError("registered Host verifier bundle member must remain a regular non-symlink file")
            if hasattr(os, "getuid") and bundle_stat.st_uid != os.getuid():
                raise PermissionError("registered Host verifier bundle member owner mismatch")
            actual_bundle_digest = __import__("hashlib").sha256(bundle_path.read_bytes()).hexdigest()
            if not secrets.compare_digest(actual_bundle_digest, str(bundle_digest).lower()):
                raise PermissionError("registered Host verifier bundle member hash mismatch")

    def run_cli(request: dict[str, Any]) -> dict[str, Any]:
        validate_pinned_bundle()
        try:
            completed = subprocess.run(
                [str(executable)],
                input=json.dumps(request, ensure_ascii=False, sort_keys=True) + "\n",
                text=True,
                capture_output=True,
                check=False,
                timeout=PEER_ATTESTATION_VERIFIER_TIMEOUT_SECONDS,
                env=safe_env,
            )
        except subprocess.TimeoutExpired as exc:
            raise PeerHostTransientUnavailable(
                f"registered Host verifier execution temporarily unavailable: {exc}"
            ) from exc
        except OSError as exc:
            if _peer_host_error_is_transient(exc):
                raise PeerHostTransientUnavailable(
                    f"registered Host verifier execution temporarily unavailable: {exc}"
                ) from exc
            raise PermissionError(f"registered Host verifier execution failed: {exc}") from exc
        except subprocess.SubprocessError as exc:
            raise PermissionError(f"registered Host verifier execution failed: {exc}") from exc
        if len(completed.stdout.encode("utf-8", errors="replace")) > PEER_ATTESTATION_VERIFIER_OUTPUT_LIMIT:
            raise PermissionError("registered Host verifier output exceeds limit")
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise PermissionError("registered Host verifier returned invalid JSON") from exc
        if completed.returncode != 0 or not isinstance(payload, dict) or payload.get("ok") is not True:
            error_detail = payload.get("error") if isinstance(payload, dict) else None
            if (
                isinstance(payload, dict)
                and payload.get("error_code") == "RUNTIME_HOST_VERIFIER_FAILED"
                and _peer_host_error_is_transient(error_detail)
            ):
                raise PeerHostTransientUnavailable(
                    "registered Host verifier temporarily unavailable: "
                    + str(error_detail or "machine request unavailable")
                )
            raise PermissionError(
                "registered Host verifier rejected machine request"
                + (f": {error_detail}" if error_detail else "")
            )
        return payload

    def verify(**kwargs: Any) -> Any:
        expected_host = str(kwargs.get("host") or "").strip()
        if expected_host != host:
            raise PermissionError("registered Host verifier host mismatch")
        conversation_id = str(kwargs.get("expected_target_session_id") or "").strip()
        target_generation = kwargs.get("expected_target_generation")
        ownership_generation = kwargs.get("expected_ownership_generation")
        if not conversation_id:
            raise PermissionError("registered Host verifier requires exact target session")
        for value, name in ((target_generation, "target generation"), (ownership_generation, "ownership generation")):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise PermissionError(f"registered Host verifier requires positive {name}")
        payload = run_cli({
            "operation": "attest_and_verify",
            "conversation_id": conversation_id,
            "target_generation": target_generation,
            "ownership_generation": ownership_generation,
        })
        verified = payload.get("verified_target")
        receipt = payload.get("host_receipt_id")
        if (
            not isinstance(verified, dict)
            or verified.get("provenance") != "runtime_host_verifier_v1"
            or verified.get("conversation_id") != conversation_id
            or verified.get("target_generation") != target_generation
            or verified.get("ownership_generation") != ownership_generation
            or not isinstance(receipt, str)
            or not receipt.strip()
            or len(receipt.encode("utf-8")) > 512
        ):
            raise PermissionError("registered Host verifier returned mismatched verified target")
        if kwargs.get("phase") == "pre_delivery":
            return {
                "origin_host": "chatgpt_web" if host == "web" else host,
                "origin_conversation_id": conversation_id,
                "origin_attested": True,
                "call_receipt": receipt.strip(),
            }
        return True

    def submit_reentry(**kwargs: Any) -> dict[str, Any]:
        if host != "web":
            raise PermissionError("registered Host submit adapter is Web-only")
        conversation_id = str(kwargs.get("execution_target_session_id") or "").strip()
        target_generation = kwargs.get("target_generation")
        ownership_generation = kwargs.get("ownership_generation")
        target_mode = str(kwargs.get("target_mode") or "").strip()
        origin = kwargs.get("host_origin_attestation")
        lifecycle_state = kwargs.get("lifecycle_state")
        if not conversation_id or not target_mode:
            raise PermissionError("registered Host submit adapter requires exact target and mode")
        for value, name in ((target_generation, "target generation"), (ownership_generation, "ownership generation")):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise PermissionError(f"registered Host submit adapter requires positive {name}")
        if (
            not isinstance(origin, dict)
            or origin.get("origin_host") != "chatgpt_web"
            or origin.get("origin_conversation_id") != conversation_id
            or origin.get("origin_attested") is not True
            or not isinstance(origin.get("call_receipt"), str)
            or not str(origin.get("call_receipt")).strip()
        ):
            raise PermissionError("registered Host submit adapter requires verified origin receipt")
        if not isinstance(lifecycle_state, dict):
            raise PermissionError("registered Host submit adapter requires lifecycle state")
        controller_id = str(kwargs.get("controller_id") or "").strip()
        if not controller_id:
            raise PermissionError("registered Host submit adapter requires controller_id")
        continuation_payload = build_reentry_prompt(
            controller_id=controller_id,
            lifecycle_state=lifecycle_state,
            terminal_receipts=lifecycle_state.get("pending_terminal_receipts", []),
        )
        wake_id = f"runtime_web_{secrets.token_hex(16)}"
        payload = run_cli({
            "operation": "submit_reentry",
            "conversation_id": conversation_id,
            "host_receipt_id": str(origin["call_receipt"]).strip(),
            "expected_target_generation": target_generation,
            "expected_ownership_generation": ownership_generation,
            "wake_id": wake_id,
            "wake_nonce": secrets.token_urlsafe(24),
            "continuation_payload": continuation_payload,
        })
        receipt = payload.get("reentry_receipt")
        if (
            not isinstance(receipt, dict)
            or receipt.get("provenance") != "browser_host_reentry_receipt_v1"
            or receipt.get("conversation_id") != conversation_id
            or receipt.get("target_generation") != target_generation
            or receipt.get("ownership_generation") != ownership_generation
            or receipt.get("wake_id") not in (None, wake_id)
        ):
            raise PermissionError("registered Host submit adapter returned mismatched receipt")
        result_class = receipt.get("result_class")
        dispatch_attempted = receipt.get("dispatch_attempted")
        submit_confirmed = receipt.get("submit_confirmed")
        retryable = receipt.get("retryable")
        auto_retry_allowed = receipt.get("auto_retry_allowed")
        if not all(isinstance(value, bool) for value in (dispatch_attempted, submit_confirmed, retryable, auto_retry_allowed)):
            raise PermissionError("registered Host submit adapter returned invalid result flags")
        common = {
            "operation": "web_reentry",
            "execution_target_session_id": conversation_id,
            "target_generation": target_generation,
            "ownership_generation": ownership_generation,
            "target_mode": target_mode,
            "delivery_authorization": "host_attested",
            "host_attested": True,
            "strong_web_identity_established": True,
            "host_execution_receipt": {
                "call_receipt": str(origin["call_receipt"]).strip(),
                "reentry_receipt": receipt,
            },
        }
        if result_class == "SUBMIT_CONFIRMED" and dispatch_attempted and submit_confirmed and not retryable and not auto_retry_allowed:
            return {**common, "result": "CONFIRMED", "state": "WEB_REENTRY_SUBMITTED", "returncode": 0}
        if result_class == "RESULT_UNKNOWN" and dispatch_attempted and not submit_confirmed and not retryable and not auto_retry_allowed:
            return {
                **common, "result": "BLOCKED", "state": "WEB_REENTRY_RESULT_UNKNOWN",
                "returncode": 78, "error_code": "WEB_REENTRY_RESULT_UNKNOWN",
                "failure_class": "web_reentry_result_unknown",
                "stderr_tail": "Host dispatch occurred but submit confirmation is unknown; automatic retry is forbidden",
            }
        if (
            result_class == "CONFIRMED_FAILURE_BEFORE_DISPATCH"
            and not dispatch_attempted
            and not submit_confirmed
            and auto_retry_allowed is retryable
        ):
            return {
                **common,
                "result": "DEFERRED" if retryable else "FAILED",
                "state": "WEB_REENTRY_PENDING" if retryable else "WEB_REENTRY_FAILED_BEFORE_DISPATCH",
                "returncode": 78 if retryable else 1,
                "error_code": "WEB_REENTRY_UNAVAILABLE" if retryable else "WEB_REENTRY_FAILED_BEFORE_DISPATCH",
                "failure_class": "web_reentry_unavailable" if retryable else "web_reentry_failed_before_dispatch",
            }
        raise PermissionError("registered Host submit adapter returned inconsistent result semantics")

    setattr(verify, "submit_reentry", submit_reentry)
    return verify


def _registered_peer_attestation_verifier(host: str) -> Callable[..., Any] | None:
    """Return only a verifier registered by this bridge's trusted host boundary."""
    builtin = _PEER_HOST_ATTESTATION_VERIFIERS.get(host)
    if callable(builtin):
        return builtin
    return _external_peer_attestation_verifier(host)


def _validated_web_origin_attestation(
    value: Any, *, expected_target_session_id: str
) -> dict[str, Any]:
    """Accept only a structured Host-origin proof before any Web delivery call."""
    if not isinstance(value, dict):
        raise PermissionError("Web Host origin attestation is not an object")
    call_receipt = value.get("call_receipt")
    if (
        value.get("origin_host") != "chatgpt_web"
        or value.get("origin_conversation_id") != expected_target_session_id
        or value.get("origin_attested") is not True
        or not isinstance(call_receipt, str)
        or not call_receipt.strip()
        or len(call_receipt.encode("utf-8")) > 512
    ):
        raise PermissionError(
            "Web Host origin attestation does not match the exact execution target"
        )
    return {
        "origin_host": "chatgpt_web",
        "origin_conversation_id": expected_target_session_id,
        "origin_attested": True,
        "call_receipt": call_receipt.strip(),
    }


def _execute_registered_web_host_reentry(
    *,
    session_id: str,
    repo: Path,
    registry: Path,
    lifecycle_state: dict[str, Any],
    runtime_path: str | None,
    ownership_fence: dict[str, Any] | None,
    verifier: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    verifier = verifier or _registered_peer_attestation_verifier("web")
    adapter = getattr(verifier, "submit_reentry", None) if callable(verifier) else None
    if not callable(verifier) or not callable(adapter):
        raise PermissionError("registered Host verifier/submit adapter is unavailable")
    if not isinstance(ownership_fence, dict):
        raise PermissionError("canonical Web execution ownership is missing")

    with target_guard.locked_execution_target(
        repo=repo,
        host="web",
        registry_path=registry,
    ) as current_target:
        if current_target.get("controller_id") != session_id:
            raise PermissionError("Web target does not belong to the registered Controller")
        current_registry = load_json(registry)
        current_web_record = target_guard.target_record(
            current_registry,
            controller_id=session_id,
            host="web",
        )
        if not isinstance(current_web_record, dict):
            raise PermissionError("canonical Web target record is missing")
        if (
            current_web_record.get("provenance")
            != "host_attested_same_controller_recovery"
            or current_web_record.get("identity_proof") != "host_attested_origin"
        ):
            raise PermissionError(
                "registered Host submit requires current Host-attested Web target"
            )

        expected_target = str(current_target["execution_target_session_id"])
        expected_generation = current_target.get("generation")
        expected_mode = current_target.get("target_mode")
        expected_ownership_generation = ownership_fence.get("generation")
        if (
            ownership_fence.get("active_host") != "web"
            or ownership_fence.get("execution_target_session_id") != expected_target
            or not isinstance(expected_generation, int)
            or isinstance(expected_generation, bool)
            or expected_generation < 1
            or not isinstance(expected_ownership_generation, int)
            or isinstance(expected_ownership_generation, bool)
            or expected_ownership_generation < 1
        ):
            raise PermissionError(
                "canonical Web execution ownership is missing or mismatched"
            )

        origin_attestation = _validated_web_origin_attestation(
            verifier(
                phase="pre_delivery",
                controller_id=session_id,
                host="web",
                expected_target_session_id=expected_target,
                expected_target_generation=expected_generation,
                expected_target_mode=expected_mode,
                expected_ownership_generation=expected_ownership_generation,
            ),
            expected_target_session_id=expected_target,
        )
        attempt = adapter(
            controller_id=session_id,
            session_id=expected_target,
            execution_target_session_id=expected_target,
            target_generation=expected_generation,
            target_mode=expected_mode,
            ownership_generation=expected_ownership_generation,
            repo=repo,
            registry=registry,
            lifecycle_state=lifecycle_state,
            runtime_path=runtime_path,
            host_origin_attestation=origin_attestation,
        )
        if not isinstance(attempt, dict):
            raise PermissionError(
                "registered Host submit adapter returned a non-object execution receipt"
            )
        host_execution_receipt = attempt.get("host_execution_receipt")
        if (
            attempt.get("execution_target_session_id") != expected_target
            or attempt.get("target_generation") != expected_generation
            or attempt.get("ownership_generation") != expected_ownership_generation
            or attempt.get("target_mode") != expected_mode
            or not isinstance(host_execution_receipt, dict)
            or host_execution_receipt.get("call_receipt")
            != origin_attestation["call_receipt"]
        ):
            raise PermissionError(
                "registered Host submit receipt does not match canonical target, ownership, and Host call receipt"
            )
        return attempt


def _wake_receipt(
    *,
    common_dir: Path,
    session_id: str,
    event_fingerprint: str,
    health: dict[str, Any],
    decision: str,
    selected_host: str | None,
    reason: str,
    operation: Any,
    result: str,
    command: Any = None,
    diagnostics: Any = None,
    error_code: Any = None,
    execution_target_session_id: str | None = None,
    target_generation: int | None = None,
    ownership_generation: int | None = None,
    target_mode: str | None = None,
    delivery_authorization: str | None = None,
    host_attested: bool | None = None,
    strong_web_identity_established: bool | None = None,
) -> dict[str, Any]:
    now = int(time.time() * 1000)
    receipt: dict[str, Any] = {
        "schema_version": 1,
        "canonical_common_dir": str(common_dir),
        "controller_id": session_id,
        "event_fingerprint": event_fingerprint,
        "health": health.get("state"),
        "preferred_host": health.get("controller_host"),
        "selected_host": selected_host,
        "decision": decision,
        "reason": bounded_tail(reason, 512),
        "started_at_unix_ms": now,
        "completed_at_unix_ms": now,
        "operation": _bounded_adapter_operation(operation),
        "result": result,
        # A host confirmation proves only the same-thread launch.  Lifecycle closure
        # remains the control-event guard's responsibility.
        "pending_control_event": True,
    }
    if isinstance(execution_target_session_id, str) and execution_target_session_id.strip():
        receipt["execution_target_session_id"] = execution_target_session_id.strip()
    if isinstance(target_generation, int) and target_generation >= 0:
        receipt["target_generation"] = target_generation
    if (
        isinstance(ownership_generation, int)
        and not isinstance(ownership_generation, bool)
        and ownership_generation > 0
    ):
        receipt["ownership_generation"] = ownership_generation
    if isinstance(target_mode, str) and target_mode.strip():
        receipt["target_mode"] = target_mode.strip()
    if isinstance(delivery_authorization, str) and delivery_authorization.strip():
        receipt["delivery_authorization"] = delivery_authorization.strip()
    if isinstance(host_attested, bool):
        receipt["host_attested"] = host_attested
    if isinstance(strong_web_identity_established, bool):
        receipt["strong_web_identity_established"] = strong_web_identity_established
    normalized_command = _bounded_adapter_command(command)
    if normalized_command is not None:
        receipt["command"] = normalized_command
    normalized_diagnostics = _bounded_adapter_diagnostics(diagnostics)
    if normalized_diagnostics:
        receipt["diagnostics"] = normalized_diagnostics
    if isinstance(error_code, str) and error_code.strip():
        receipt["error_code"] = _bounded_text(error_code.strip(), 128)[0]
    return receipt



def _manual_fenced_web_target_candidate(
    *, registry_data: dict[str, Any], controller_id: str
) -> bool:
    """Return whether the current registry target may enter built-in manual re-entry.

    This is only a narrow preflight. The Web re-entry adapter still performs the
    authoritative lease, lineage, target, and generation validation under locks.
    """
    target = target_guard.target_record(
        registry_data, controller_id=controller_id, host="web"
    )
    ownership = target_guard.execution_ownership_record(
        registry_data, controller_id=controller_id
    )
    if not isinstance(target, dict) or ownership is None:
        return False
    try:
        status, target_session, target_generation = target_guard.validate_target_record(
            target, host="web"
        )
        ownership_host, ownership_target, ownership_generation = (
            target_guard.validate_execution_ownership_record(ownership)
        )
    except (ValueError, PermissionError):
        return False
    return bool(
        status == "active"
        and target_session
        and target.get("provenance") == "manual_user_authorized"
        and target.get("binding_mode") == "temporary"
        and target.get("host_attested") is False
        and ownership_host == "web"
        and ownership_target == target_session
        and ownership_generation == target_generation
        and target_guard.active_source_controller_id(
            registry_data, source_session_id=target_session, host="web"
        )
        == controller_id
    )


def wake_existing_controller(
    *,
    lifecycle_state: dict[str, Any],
    session_id: str,
    repo: Path,
    registry: Path,
    codex: str,
    receipt_path: Path,
    host_facts: dict[str, Any],
    resume_adapters: dict[str, Callable[..., dict[str, Any]]] | None = None,
    runtime_path: str | None = None,
) -> dict[str, Any]:
    """Wake only the registered controller, using Task 1's pure decision policy."""
    repo = repo.resolve()
    try:
        common_dir = _git_common_dir(repo)
    except (OSError, subprocess.SubprocessError) as exc:
        health = derive_controller_health({})
        receipt = _wake_receipt(
            common_dir=repo,
            session_id=session_id,
            event_fingerprint=_wake_event_fingerprint(lifecycle_state),
            health=health,
            decision="DEAD_BLOCK",
            selected_host=None,
            reason=f"cannot resolve Git common-dir: {exc}",
            operation=None,
            result="BLOCKED",
        )
        _write_json_atomic_file(receipt_path, receipt)
        return receipt

    facts = dict(host_facts) if isinstance(host_facts, dict) else {}
    facts.update({
        "registered_controller": session_id,
        "canonical_common_dir": str(common_dir),
        "pending_control_event": lifecycle_state.get("pending_control_event") is True,
    })
    if _registered_controller_for_common_dir(repo, registry) != session_id:
        facts.pop("registered_controller", None)
    ownership_fence = _canonical_ownership_fence(
        repo=repo, registry=registry, session_id=session_id
    )
    if ownership_fence is not None:
        facts["controller_host"] = ownership_fence["active_host"]
    health = derive_controller_health(facts)
    wake = decide_controller_wake(health)
    decision = str(wake["decision"])
    selected_host = wake["selected_host"]
    fingerprint = _wake_event_fingerprint(lifecycle_state)
    reason = str(facts.get("failure_class") or health["state"])

    lock_path = common_dir / "adaptive-delivery" / "controller-wake.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = lock_path.open("a+")
    try:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            receipt = _wake_receipt(
                common_dir=common_dir,
                session_id=session_id,
                event_fingerprint=fingerprint,
                health=health,
                decision="DEFER",
                selected_host=None,
                reason="common_dir_wake_locked",
                operation=None,
                result="DEFERRED",
            )
            return receipt

        if decision == "NOOP_ACTIVE":
            receipt = _wake_receipt(
                common_dir=common_dir, session_id=session_id, event_fingerprint=fingerprint,
                health=health, decision=decision, selected_host=None, reason=reason,
                operation=None, result="CONFIRMED",
            )
        elif decision == "DEFER":
            receipt = _wake_receipt(
                common_dir=common_dir, session_id=session_id, event_fingerprint=fingerprint,
                health=health, decision=decision, selected_host=None, reason=reason,
                operation=None, result="DEFERRED",
            )
        elif decision == "DEAD_BLOCK":
            receipt = _wake_receipt(
                common_dir=common_dir, session_id=session_id, event_fingerprint=fingerprint,
                health=health, decision=decision, selected_host=None, reason=reason,
                operation=None, result="BLOCKED",
            )
        else:
            if selected_host == health.get("controller_host"):
                if selected_host == "desktop_codex":
                    attempt = execute_native_resume(
                        session_id=session_id, repo=repo, registry=registry, codex=codex,
                        runtime_path=runtime_path,
                        terminal_receipts=lifecycle_state.get("pending_terminal_receipts", []),
                        next_action=str(lifecycle_state.get("next_action") or "").strip() or None,
                    )
                elif selected_host == "web":
                    adapter = (resume_adapters or {}).get("web")
                    verifier = _registered_peer_attestation_verifier("web")
                    registered_submit = getattr(verifier, "submit_reentry", None) if callable(verifier) else None
                    if adapter is None and callable(registered_submit):
                        adapter = registered_submit
                    current_registry_for_web = load_json(registry)
                    manual_builtin_candidate = (
                        adapter is None
                        and _manual_fenced_web_target_candidate(
                            registry_data=current_registry_for_web, controller_id=session_id
                        )
                    )
                    if not callable(verifier) and not manual_builtin_candidate:
                        attempt = {
                            "operation": None,
                            "result": "DEFERRED",
                            "state": "RESUME_DEFERRED",
                            "returncode": 78,
                            "stderr_tail": (
                                "no registered host-attested verifier for supplied Web host adapter"
                            ),
                            "error_code": "WEB_HOST_ATTESTATION_VERIFIER_UNAVAILABLE",
                            "failure_class": "web_reentry_identity_unavailable",
                        }
                    else:
                        try:
                            if adapter is None:
                                attempt = execute_web_reentry(
                                    controller_id=session_id,
                                    repo=repo,
                                    registry_path=registry,
                                    lease_path=DEFAULT_MANUAL_WEB_LEASES,
                                    lifecycle_state=lifecycle_state,
                                    origin_verifier=verifier,
                                )
                            else:
                                if not callable(verifier):
                                    raise PermissionError(
                                        "custom Web host adapter requires registered Host attestation verifier"
                                    )
                                with target_guard.locked_execution_target(
                                    repo=repo,
                                    host="web",
                                    registry_path=registry,
                                ) as current_target:
                                    current_registry = load_json(registry)
                                    current_web_record = target_guard.target_record(
                                        current_registry,
                                        controller_id=session_id,
                                        host="web",
                                    )
                                    if (
                                        isinstance(current_web_record, dict)
                                        and current_web_record.get("provenance")
                                        == "host_attested_same_controller_recovery"
                                        and current_web_record.get("identity_proof")
                                        != "host_attested_origin"
                                    ):
                                        raise PermissionError(
                                            "legacy browser-tab Web identity record is not a trusted Host origin attestation"
                                        )
                                    expected_target = str(
                                        current_target["execution_target_session_id"]
                                    )
                                    expected_generation = current_target.get("generation")
                                    expected_mode = current_target.get("target_mode")
                                    expected_ownership_generation = (
                                        ownership_fence.get("generation")
                                        if isinstance(ownership_fence, dict)
                                        else None
                                    )
                                    if (
                                        not isinstance(ownership_fence, dict)
                                        or ownership_fence.get("active_host") != "web"
                                        or ownership_fence.get(
                                            "execution_target_session_id"
                                        )
                                        != expected_target
                                        or not isinstance(
                                            expected_ownership_generation, int
                                        )
                                        or isinstance(
                                            expected_ownership_generation, bool
                                        )
                                    ):
                                        raise PermissionError(
                                            "canonical Web execution ownership is missing or mismatched"
                                        )
                                    origin_attestation = _validated_web_origin_attestation(
                                        verifier(
                                            phase="pre_delivery",
                                            controller_id=session_id,
                                            host="web",
                                            expected_target_session_id=expected_target,
                                            expected_target_generation=expected_generation,
                                            expected_target_mode=expected_mode,
                                            expected_ownership_generation=expected_ownership_generation,
                                        ),
                                        expected_target_session_id=expected_target,
                                    )
                                    attempt = adapter(
                                        controller_id=session_id,
                                        session_id=expected_target,
                                        execution_target_session_id=expected_target,
                                        target_generation=expected_generation,
                                        target_mode=expected_mode,
                                        ownership_generation=expected_ownership_generation,
                                        repo=repo,
                                        registry=registry,
                                        lifecycle_state=lifecycle_state,
                                        runtime_path=runtime_path,
                                        host_origin_attestation=origin_attestation,
                                    )
                                    if not isinstance(attempt, dict):
                                        raise PermissionError(
                                            "Web host adapter returned a non-object execution receipt"
                                        )
                                    if (
                                        attempt.get("execution_target_session_id")
                                        != expected_target
                                        or attempt.get("target_generation")
                                        != expected_generation
                                        or attempt.get("ownership_generation")
                                        != expected_ownership_generation
                                        or attempt.get("target_mode") != expected_mode
                                        or not isinstance(
                                            attempt.get("host_execution_receipt"), dict
                                        )
                                        or attempt["host_execution_receipt"].get(
                                            "call_receipt"
                                        )
                                        != origin_attestation["call_receipt"]
                                    ):
                                        raise PermissionError(
                                            "Web host adapter receipt does not match canonical target, ownership, and Host call receipt"
                                        )
                        except Exception as exc:
                            if adapter is None:
                                attempt = {
                                    "operation": "web_reentry",
                                    "result": "DEFERRED",
                                    "state": "WEB_REENTRY_PENDING",
                                    "returncode": 78,
                                    "stderr_tail": f"web host adapter failed: {exc}",
                                    "error_code": "WEB_HOST_REENTRY_ADAPTER_FAILED",
                                    "failure_class": "web_reentry_unavailable",
                                }
                            else:
                                attempt = {
                                    "operation": "web_reentry",
                                    "result": "FAILED",
                                    "state": "WEB_REENTRY_IDENTITY_UNAVAILABLE",
                                    "returncode": 78,
                                    "stderr_tail": f"web host adapter failed: {exc}",
                                    "error_code": "WEB_HOST_ATTESTATION_INVALID",
                                    "failure_class": "web_reentry_identity_unavailable",
                                }
                    if not isinstance(attempt, dict):
                        attempt = {
                            "operation": "web_reentry",
                            "result": "DEFERRED",
                            "state": "WEB_REENTRY_PENDING",
                            "returncode": 78,
                            "stderr_tail": "web host adapter returned a non-object execution receipt",
                            "error_code": "WEB_HOST_REENTRY_ADAPTER_INVALID",
                            "failure_class": "web_reentry_unavailable",
                        }
                else:
                    attempt = {
                        "operation": None,
                        "result": "FAILED",
                        "state": "RESUME_FAILED",
                        "returncode": 1,
                        "stderr_tail": f"unsupported controller host {selected_host}",
                        "error_code": "CONTROLLER_HOST_UNSUPPORTED",
                    }
            else:
                adapter = (resume_adapters or {}).get(str(selected_host))
                verifier = _registered_peer_attestation_verifier(str(selected_host))
                if adapter is None:
                    attempt = {
                        "operation": None,
                        "result": "DEFERRED",
                        "state": "RESUME_DEFERRED",
                        "stderr_tail": f"no authorized adapter for peer host {selected_host}",
                    }
                elif not callable(verifier):
                    attempt = {
                        "operation": None,
                        "result": "DEFERRED",
                        "state": "RESUME_DEFERRED",
                        "stderr_tail": (
                            "no registered host-attested verifier for peer host "
                            f"{selected_host}"
                        ),
                        "error_code": "PEER_HOST_ATTESTATION_VERIFIER_UNAVAILABLE",
                    }
                else:
                    try:
                        with target_guard.locked_execution_target(
                            repo=repo,
                            host=str(selected_host),
                            registry_path=registry,
                        ) as peer_target:
                            if peer_target.get("controller_id") != session_id:
                                raise PermissionError(
                                    "peer target does not belong to the registered Controller"
                                )
                            expected_target = str(peer_target["execution_target_session_id"])
                            expected_generation = peer_target.get("generation")
                            try:
                                attempt = adapter(
                                    controller_id=session_id,
                                    session_id=expected_target,
                                    execution_target_session_id=expected_target,
                                    target_generation=expected_generation,
                                    target_mode=peer_target.get("target_mode"),
                                    repo=repo,
                                    registry=registry,
                                    codex=codex,
                                    runtime_path=runtime_path,
                                )
                            except Exception as exc:
                                attempt = {
                                    "operation": f"{selected_host}_resume",
                                    "result": "FAILED",
                                    "state": "RESUME_FAILED",
                                    "execution_target_session_id": expected_target,
                                    "target_generation": expected_generation,
                                    "target_mode": peer_target.get("target_mode"),
                                    "stderr_tail": f"peer host adapter failed: {exc}",
                                    "error_code": "PEER_HOST_ADAPTER_FAILED",
                                }
                            if not isinstance(attempt, dict):
                                attempt = {
                                    "operation": f"{selected_host}_resume",
                                    "result": "FAILED",
                                    "state": "RESUME_FAILED",
                                    "execution_target_session_id": expected_target,
                                    "target_generation": expected_generation,
                                    "target_mode": peer_target.get("target_mode"),
                                    "stderr_tail": "peer host returned a non-object execution receipt",
                                    "error_code": "PEER_HOST_ATTESTATION_INVALID",
                                }
                            elif (
                                attempt.get("execution_target_session_id") != expected_target
                                or attempt.get("target_generation") != expected_generation
                            ):
                                attempt = {
                                    "operation": f"{selected_host}_resume",
                                    "result": "FAILED",
                                    "state": "RESUME_FAILED",
                                    "execution_target_session_id": expected_target,
                                    "target_generation": expected_generation,
                                    "target_mode": peer_target.get("target_mode"),
                                    "stderr_tail": "peer host target receipt mismatch; outbound result rejected",
                                    "error_code": "CONTROLLER_TARGET_RECEIPT_MISMATCH",
                                }
                            else:
                                try:
                                    attested = verifier(
                                        controller_id=session_id,
                                        host=str(selected_host),
                                        expected_target_session_id=expected_target,
                                        expected_target_generation=expected_generation,
                                        expected_target_mode=peer_target.get("target_mode"),
                                        host_execution_receipt=attempt.get("host_execution_receipt"),
                                        adapter_attempt=attempt,
                                    )
                                except Exception as exc:
                                    attempt = {
                                        "operation": f"{selected_host}_resume",
                                        "result": "FAILED",
                                        "state": "RESUME_FAILED",
                                        "execution_target_session_id": expected_target,
                                        "target_generation": expected_generation,
                                        "target_mode": peer_target.get("target_mode"),
                                        "stderr_tail": f"peer host attestation verifier failed: {exc}",
                                        "error_code": "PEER_HOST_ATTESTATION_INVALID",
                                    }
                                else:
                                    if attested is not True:
                                        attempt = {
                                            "operation": f"{selected_host}_resume",
                                            "result": "FAILED",
                                            "state": "RESUME_FAILED",
                                            "execution_target_session_id": expected_target,
                                            "target_generation": expected_generation,
                                            "target_mode": peer_target.get("target_mode"),
                                            "stderr_tail": "peer host attestation rejected; outbound result rejected",
                                            "error_code": "PEER_HOST_ATTESTATION_REJECTED",
                                        }
                    except (OSError, ValueError, PermissionError, subprocess.SubprocessError) as exc:
                        attempt = {
                            "operation": f"{selected_host}_resume",
                            "result": "FAILED",
                            "state": "RESUME_FAILED",
                            "stderr_tail": f"host adapter target guard error: {exc}",
                            "error_code": "CONTROLLER_TARGET_REJECTED",
                        }
            if selected_host == "web" and attempt.get("result") == "CONFIRMED":
                try:
                    receipt = _validate_confirmed_web_reentry_receipt(
                        attempt=attempt,
                        session_id=session_id,
                        registry=registry,
                        ownership_fence=ownership_fence,
                    )
                except (OSError, ValueError, PermissionError) as exc:
                    attempt = {
                        "operation": "web_reentry",
                        "result": "FAILED",
                        "state": "RESUME_FAILED",
                        "returncode": 78,
                        "stderr_tail": str(exc),
                        "error_code": "CONTROLLER_TARGET_RECEIPT_MISMATCH",
                        "execution_target_session_id": None,
                        "target_generation": None,
                        "ownership_generation": None,
                    }
                else:
                    attempt.update(receipt)
            if not _canonical_ownership_fence_matches(
                ownership_fence, repo=repo, registry=registry, session_id=session_id
            ):
                attempt = {
                    "operation": attempt.get("operation"),
                    "result": "DEFERRED",
                    "state": "RESUME_SUPERSEDED_HOST_HANDOFF",
                    "returncode": 0,
                    "stderr_tail": "Controller host ownership changed while wake was in flight",
                    "error_code": "CONTROLLER_HOST_OWNERSHIP_SUPERSEDED",
                    "execution_target_session_id": attempt.get("execution_target_session_id"),
                    "target_generation": attempt.get("target_generation"),
                    "target_mode": attempt.get("target_mode"),
                }
            result = str(attempt.get("result", "FAILED"))
            receipt = _wake_receipt(
                common_dir=common_dir, session_id=session_id, event_fingerprint=fingerprint,
                health=health, decision=decision, selected_host=str(selected_host), reason=reason,
                operation=attempt.get("operation"), result=result,
                command=attempt.get("command"), diagnostics=attempt.get("stderr_tail"),
                error_code=attempt.get("error_code"),
                execution_target_session_id=attempt.get("execution_target_session_id"),
                target_generation=attempt.get("target_generation"),
                ownership_generation=(
                    ownership_fence.get("generation")
                    if isinstance(ownership_fence, dict)
                    else attempt.get("ownership_generation")
                ),
                target_mode=attempt.get("target_mode"),
                delivery_authorization=attempt.get("delivery_authorization"),
                host_attested=attempt.get("host_attested"),
                strong_web_identity_established=attempt.get("strong_web_identity_established"),
            )
        _write_json_atomic_file(receipt_path, receipt)
        return receipt
    finally:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        finally:
            lock.close()


def default_wake_receipt_path(repo: Path) -> Path:
    return _git_common_dir(repo) / "adaptive-delivery" / "controller-wake-receipt.json"


def persist_confirmed_auto_native_wake(
    *,
    lifecycle_state: dict[str, Any],
    session_id: str,
    repo: Path,
    registry: Path,
    attempt: dict[str, Any],
    state_path: Path,
    receipt_id: str,
    supervisor_token: str | None,
) -> bool:
    """Persist a target-fenced canonical receipt for a successful delayed native wake."""
    if attempt.get("result") != "CONFIRMED":
        return False
    execution_target = str(attempt.get("execution_target_session_id") or "").strip()
    generation = attempt.get("target_generation")
    ownership_generation = attempt.get("ownership_generation")
    if not execution_target or not isinstance(generation, int) or isinstance(generation, bool):
        return False
    if ownership_generation is not None and (
        not isinstance(ownership_generation, int)
        or isinstance(ownership_generation, bool)
        or ownership_generation <= 0
    ):
        return False

    common_dir = _git_common_dir(repo)
    wake_path = default_wake_receipt_path(repo)
    lock_path = common_dir / "adaptive-delivery" / "controller-wake.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            with target_guard.locked_execution_target(
                repo=repo,
                host=target_guard.DESKTOP_SESSION_HOST,
                registry_path=registry,
            ) as current_target:
                if (
                    current_target.get("controller_id") != session_id
                    or current_target.get("execution_target_session_id") != execution_target
                    or current_target.get("generation") != generation
                ):
                    return False
                if ownership_generation is not None:
                    registry_data = target_guard.load_json(registry)
                    ownership_record = target_guard.execution_ownership_record(
                        registry_data, controller_id=session_id
                    )
                    if ownership_record is None:
                        return False
                    ownership_host, ownership_target, current_ownership_generation = (
                        target_guard.validate_execution_ownership_record(ownership_record)
                    )
                    if (
                        ownership_host != target_guard.DESKTOP_SESSION_HOST
                        or ownership_target != execution_target
                        or current_ownership_generation != ownership_generation
                    ):
                        return False
                with _owned_supervisor_state(
                    state_path,
                    receipt_id=receipt_id,
                    supervisor_token=supervisor_token,
                ) as owner:
                    if owner is None:
                        return False
                    receipt = _wake_receipt(
                        common_dir=common_dir,
                        session_id=session_id,
                        event_fingerprint=_wake_event_fingerprint(lifecycle_state),
                        health={"state": "ACTIVE", "controller_host": "desktop_codex"},
                        decision="WAKE_EXISTING",
                        selected_host="desktop_codex",
                        reason="auto_native_stop_confirmed",
                        operation=attempt.get("operation"),
                        result="CONFIRMED",
                        command=attempt.get("command"),
                        diagnostics=attempt.get("stderr_tail"),
                        execution_target_session_id=execution_target,
                        target_generation=generation,
                        ownership_generation=ownership_generation,
                        target_mode=attempt.get("target_mode"),
                    )
                    _write_json_atomic_file(wake_path, receipt)
                    return True
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _lifecycle_module() -> Any:
    try:
        import lifecycle_hook as lifecycle
    except ModuleNotFoundError:
        from scripts import lifecycle_hook as lifecycle
    return lifecycle


def _load_lifecycle_state(session_id: str) -> dict[str, Any]:
    try:
        lifecycle = _lifecycle_module()
        loaded = lifecycle.load_json(lifecycle.state_path(session_id))
    except (OSError, ValueError, ModuleNotFoundError, ImportError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def dispatch_pending_lifecycle_wake(
    *,
    lifecycle_state: dict[str, Any],
    session_id: str,
    repo: Path,
    registry: Path,
    codex: str,
    receipt_path: Path | None = None,
    host_facts: dict[str, Any] | None = None,
    resume_adapters: dict[str, Callable[..., dict[str, Any]]] | None = None,
    runtime_path: str | None = None,
) -> dict[str, Any] | None:
    """Route any pending lifecycle event through the one Wake Supervisor path."""
    if not isinstance(lifecycle_state, dict) or lifecycle_state.get("pending_control_event") is not True:
        return None
    repo = repo.resolve()
    try:
        target_receipt = receipt_path or default_wake_receipt_path(repo)
    except (OSError, subprocess.SubprocessError):
        if receipt_path is None:
            return None
        target_receipt = receipt_path
    fingerprint = _wake_event_fingerprint(lifecycle_state)
    prior = load_json(target_receipt)
    facts = dict(host_facts) if isinstance(host_facts, dict) else {}
    controller_host = resolve_controller_host(
        lifecycle_state, facts, load_json(registry), session_id
    )
    facts.setdefault("controller_host", controller_host)
    try:
        current_common_dir = str(_git_common_dir(repo))
        current_registered = _registered_controller_for_common_dir(repo, registry)
    except (OSError, subprocess.SubprocessError):
        current_common_dir = None
        current_registered = None
    current_target = None
    if controller_host == "web":
        try:
            current_web_session = resolve_reentry_session(
                controller_id=session_id, repo=repo, registry_path=registry,
                lease_path=DEFAULT_MANUAL_WEB_LEASES,
            )
        except (OSError, ValueError, PermissionError):
            current_web_session = None
        if current_web_session:
            try:
                resolved_web_target = target_guard.resolve_execution_target(
                    repo=repo, host="web", registry_path=registry
                )
            except (OSError, ValueError, PermissionError):
                resolved_web_target = None
            if (
                isinstance(resolved_web_target, dict)
                and resolved_web_target.get("controller_id") == session_id
                and resolved_web_target.get("execution_target_session_id")
                == current_web_session
            ):
                current_target = resolved_web_target
    else:
        try:
            current_target = resolve_native_resume_target(
                session_id=session_id,
                repo=repo,
                registry=registry,
            )
        except (OSError, ValueError, PermissionError, subprocess.SubprocessError):
            current_target = None
    target_matches_prior = False
    if current_target is not None:
        current_target_id = current_target.get("execution_target_session_id")
        current_generation = current_target.get("generation")
        prior_target_id = prior.get("execution_target_session_id")
        prior_generation = prior.get("target_generation")
        if prior_target_id is None and current_target.get("target_mode") == "legacy_canonical":
            prior_target_id = session_id
        if prior_generation is None and current_target.get("target_mode") == "legacy_canonical":
            prior_generation = 0
        current_ownership_generation = None
        current_ownership_host = None
        current_ownership_target = None
        try:
            current_ownership = _canonical_ownership_fence(
                repo=repo, registry=registry, session_id=session_id
            )
        except (OSError, ValueError, PermissionError, subprocess.SubprocessError):
            current_ownership = None
        if isinstance(current_ownership, dict):
            current_ownership_generation = current_ownership.get("generation")
            current_ownership_host = current_ownership.get("active_host")
            current_ownership_target = current_ownership.get(
                "execution_target_session_id"
            )
        ownership_matches_prior = (
            current_ownership is None
            or (
                prior.get("ownership_generation") == current_ownership_generation
                and current_ownership_host == controller_host
                and current_ownership_target == current_target_id
            )
        )
        target_matches_prior = (
            prior_target_id == current_target_id
            and prior_generation == current_generation
            and prior.get("target_mode", current_target.get("target_mode")) == current_target.get("target_mode")
            and ownership_matches_prior
        )
    if (
        prior.get("event_fingerprint") == fingerprint
        and prior.get("result") == "CONFIRMED"
        and prior.get("controller_id") == session_id
        and current_registered == session_id
        and current_common_dir is not None
        and prior.get("canonical_common_dir") == current_common_dir
        and target_matches_prior
    ):
        debounced = dict(prior)
        debounced["debounced"] = True
        return debounced

    if (
        "resume_actionable" not in facts
        and "resume_state" not in facts
        and facts.get("controller_execution_active") is not True
        and facts.get("active_writer") is not True
    ):
        facts["resume_actionable"] = True
    return wake_existing_controller(
        lifecycle_state=lifecycle_state,
        session_id=session_id,
        repo=repo,
        registry=registry,
        codex=codex,
        receipt_path=target_receipt,
        host_facts=facts,
        resume_adapters=resume_adapters,
        runtime_path=runtime_path,
    )



def resolve_controller_host(
    lifecycle_state: dict[str, Any],
    host_facts: dict[str, Any],
    registry_data: dict[str, Any],
    session_id: str,
) -> str:
    ownership = target_guard.execution_ownership_record(
        registry_data if isinstance(registry_data, dict) else {},
        controller_id=session_id,
    )
    if ownership is not None:
        active_host, ownership_target, _ownership_generation = (
            target_guard.validate_execution_ownership_record(ownership)
        )
        host_target = target_guard.target_record(
            registry_data, controller_id=session_id, host=active_host
        )
        if host_target is None:
            aliases = target_guard.host_sessions(
                registry_data, controller_id=session_id, host=active_host
            )
            if aliases or ownership_target != session_id:
                raise PermissionError(
                    "canonical Controller ownership target is not current for its host"
                )
        else:
            status, current_target, _host_generation = (
                target_guard.validate_target_record(host_target, host=active_host)
            )
            if status != "active" or current_target != ownership_target:
                raise PermissionError(
                    "canonical Controller ownership target is not current for its host"
                )
        return active_host
    sessions = registry_data.get("__controller_sessions__", {}) if isinstance(registry_data, dict) else {}
    controller_sessions = sessions.get(session_id, {}) if isinstance(sessions, dict) else {}
    if not isinstance(controller_sessions, dict):
        controller_sessions = {}

    targets = registry_data.get("__controller_targets__", {}) if isinstance(registry_data, dict) else {}
    controller_targets = targets.get(session_id, {}) if isinstance(targets, dict) else {}
    if not isinstance(controller_targets, dict):
        controller_targets = {}

    explicit_active_hosts: set[str] = set()
    for host in ("web", "desktop_codex"):
        record = controller_targets.get(host)
        if not isinstance(record, dict):
            continue
        try:
            status, target_session, _generation = target_guard.validate_target_record(record, host=host)
        except (TypeError, ValueError):
            continue
        if status == "active" and target_session:
            explicit_active_hosts.add(host)

    preferred_host = None
    for value in (lifecycle_state.get("controller_host"), host_facts.get("controller_host")):
        host = str(value or "").strip()
        if host in {"web", "desktop_codex"}:
            preferred_host = host
            break

    if preferred_host is not None:
        if preferred_host in explicit_active_hosts:
            return preferred_host
        aliases = controller_sessions.get(preferred_host)
        if isinstance(aliases, str):
            aliases = [aliases]
        bound_aliases = [
            str(value).strip() for value in aliases or []
            if isinstance(value, str) and str(value).strip()
        ]
        legacy_preferred_is_routable = (
            preferred_host == "web" and len(bound_aliases) <= 1
        ) or (
            preferred_host == "desktop_codex" and not bound_aliases
        )
        if legacy_preferred_is_routable:
            return preferred_host
        if len(explicit_active_hosts) == 1:
            return next(iter(explicit_active_hosts))
        return preferred_host

    web_bound = isinstance(controller_sessions.get("web"), list) and any(str(x).strip() for x in controller_sessions.get("web", []))
    desktop_bound = isinstance(controller_sessions.get("desktop_codex"), list) and any(str(x).strip() for x in controller_sessions.get("desktop_codex", []))
    if len(explicit_active_hosts) == 1:
        return next(iter(explicit_active_hosts))
    if desktop_bound and not web_bound:
        return "desktop_codex"
    return "web"


def canonical_rule_wake_target(
    *, lifecycle_state: dict[str, Any], session_id: str, repo: Path, registry: Path
) -> dict[str, Any]:
    """Resolve only an explicit canonical current target for autonomous rule-update wake."""
    registry_data = load_json(registry)
    host = resolve_controller_host(lifecycle_state, {}, registry_data, session_id)
    receipt = target_guard.resolve_execution_target(
        repo=repo.resolve(), host=host, registry_path=registry
    )
    if receipt.get("controller_id") != session_id:
        raise PermissionError("rule wake target does not belong to the registered Controller")
    if receipt.get("target_mode") != "explicit_current":
        raise PermissionError(
            "explicit current execution target required for autonomous rule-update wake"
        )
    target = target_guard.target_record(
        registry_data, controller_id=session_id, host=host
    )
    if (
        host == "web"
        and isinstance(target, dict)
        and target.get("provenance") == "host_attested_same_controller_recovery"
        and target.get("identity_proof") != "host_attested_origin"
    ):
        raise PermissionError(
            "autonomous rule-update wake requires trusted Host origin proof for legacy Web recovery targets"
        )
    ownership = target_guard.execution_ownership_record(
        registry_data, controller_id=session_id
    )
    if ownership is None:
        raise PermissionError(
            "canonical execution ownership is required for autonomous rule-update wake"
        )
    ownership_host, ownership_target, ownership_generation = (
        target_guard.validate_execution_ownership_record(ownership)
    )
    if (
        ownership_host != host
        or ownership_target != receipt.get("execution_target_session_id")
    ):
        raise PermissionError(
            "canonical execution ownership does not match the rule wake target"
        )
    receipt = {**receipt, "ownership_generation": ownership_generation}
    return receipt


def wake_receipt_confirmed(receipt: dict[str, Any] | None) -> bool:
    return isinstance(receipt, dict) and receipt.get("result") == "CONFIRMED"

def successful_guard_event_from_receipt(
    receipt: dict[str, Any], *, session_id: str, repo: Path, web_session_id: str
) -> dict[str, Any] | None:
    event = translate_receipt(
        receipt, session_id=session_id, repo=repo, web_session_id=web_session_id
    )
    if event is None:
        return None
    try:
        from lifecycle_hook import successful_control_receipt
    except ModuleNotFoundError:
        from scripts.lifecycle_hook import successful_control_receipt
    if not successful_control_receipt(event):
        return None
    return event


def append_captured_event(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def dispatch_event_result(event: dict[str, Any]) -> dict[str, Any]:
    """Run the lifecycle hook and keep logical gate outcome separate from transport success."""
    hook = Path(__file__).resolve().with_name("lifecycle_hook.py")
    completed = subprocess.run(
        [sys.executable, str(hook)],
        input=json.dumps(event, ensure_ascii=False),
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.stdout:
        print(completed.stdout, end="")
    if completed.stderr:
        print(completed.stderr, file=sys.stderr, end="")
    lifecycle_output: dict[str, Any] = {}
    for line in reversed(completed.stdout.splitlines()):
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            lifecycle_output = parsed
            break
    yield_blocked = lifecycle_output.get("decision") == "block"
    return {
        "transport_returncode": int(completed.returncode),
        "yield_blocked": yield_blocked,
        "lifecycle_output": lifecycle_output,
        "reason": str(lifecycle_output.get("reason") or ""),
    }


def dispatch_event(event: dict[str, Any]) -> int:
    """Compatibility wrapper for callers that only care whether hook transport executed."""
    return int(dispatch_event_result(event)["transport_returncode"])


def complete_web_lifecycle_dispatch(
    *, dispatch_outcome: dict[str, Any], session_id: str, repo: Path, registry: Path,
    codex: str, receipt_prefix: str, runtime_path: str | None = None,
) -> int:
    """Finish a Web lifecycle transaction without losing a rejected logical Yield."""
    transport_returncode = int(dispatch_outcome.get("transport_returncode", 78) or 0)
    if transport_returncode != 0:
        return transport_returncode
    lifecycle_state = _load_lifecycle_state(session_id)
    wake_receipt = dispatch_pending_lifecycle_wake(
        lifecycle_state=lifecycle_state,
        session_id=session_id,
        repo=repo,
        registry=registry,
        codex=codex,
        runtime_path=runtime_path,
    )
    if lifecycle_state.get("pending_control_event") is True and not wake_receipt_confirmed(wake_receipt):
        if wake_receipt_needs_auto_native_stop(wake_receipt):
            schedule_auto_native_stop(
                session_id=session_id, repo=repo,
                receipt_id=f"{receipt_prefix}:{lifecycle_state.get('wake_generation', 0)}",
                registry=registry, codex=codex,
                delay_seconds=1.0, state_path=default_auto_stop_state_path(session_id),
                runtime_path=runtime_path,
            )
        return 78
    ensure_continuation_supervisor(
        lifecycle_state=lifecycle_state,
        session_id=session_id,
        repo=repo,
        registry=registry,
        codex=codex,
        runtime_path=runtime_path,
    )
    if dispatch_outcome.get("yield_blocked") is True:
        return 78
    return 0


def rule_wake_schedule_decision(lifecycle_state: dict[str, Any]) -> str:
    policy = str(lifecycle_state.get("rule_wake_policy", "")).strip()
    if policy == "immediate":
        return "schedule_now"
    if policy == "next_turn":
        return "natural_turn"
    if policy != "after_event":
        return "none"
    triggers = {str(item) for item in lifecycle_state.get("triggers", [])}
    non_rule = {
        item for item in triggers
        if not item.startswith(("rule_update_pending:", "rule_ledger_stale:", "rule_install_integrity_error:"))
    }
    return "wait_for_event" if non_rule else "schedule_now"


def _rule_revision_from_state(lifecycle_state: dict[str, Any]) -> str | None:
    snapshot = lifecycle_state.get("snapshot")
    if isinstance(snapshot, dict):
        handshake = snapshot.get("rule_handshake")
        if isinstance(handshake, dict):
            revision = str(handshake.get("installed_revision", "")).strip()
            if revision:
                return revision
    for trigger in lifecycle_state.get("triggers", []):
        text = str(trigger)
        if text.startswith("rule_update_pending:"):
            revision = text.split(":", 1)[1].strip()
            if revision:
                return revision
    return None


def _lifecycle_delivery_key(lifecycle_state: dict[str, Any]) -> str:
    for trigger in lifecycle_state.get("triggers", []):
        text = str(trigger)
        if text.startswith("rule_update_pending:"):
            revision = text.split(":", 1)[1].strip()
            if revision:
                return f"rule-update:{revision}"
    generation = int(lifecycle_state.get("wake_generation", 0) or 0)
    return f"wake-generation:{generation}"


def schedule_guarded_rule_wake(
    *,
    lifecycle_state: dict[str, Any],
    session_id: str,
    repo: Path,
    registry: Path,
    codex: str,
    delay_seconds: float,
    state_path: Path,
    capture_path: Path | None = None,
    runtime_path: str | None = None,
) -> dict[str, Any]:
    decision = rule_wake_schedule_decision(lifecycle_state)
    if decision != "schedule_now":
        return {"schedule": decision}
    try:
        target = canonical_rule_wake_target(
            lifecycle_state=lifecycle_state,
            session_id=session_id,
            repo=repo,
            registry=registry,
        )
    except (OSError, ValueError, PermissionError, RuntimeError) as exc:
        return {"schedule": "blocked", "reason": str(exc)}
    schedule = maybe_schedule_rule_wake(
        lifecycle_state=lifecycle_state,
        session_id=session_id,
        repo=repo,
        registry=registry,
        codex=codex,
        delay_seconds=delay_seconds,
        state_path=state_path,
        capture_path=capture_path,
        runtime_path=runtime_path,
    )
    return {
        "schedule": schedule,
        "host": target.get("host"),
        "execution_target_session_id": target.get("execution_target_session_id"),
        "target_generation": target.get("generation"),
        "ownership_generation": target.get("ownership_generation"),
    }


def maybe_schedule_rule_wake(
    *,
    lifecycle_state: dict[str, Any],
    session_id: str,
    repo: Path,
    registry: Path,
    codex: str,
    delay_seconds: float,
    state_path: Path,
    capture_path: Path | None = None,
    runtime_path: str | None = None,
) -> str:
    decision = rule_wake_schedule_decision(lifecycle_state)
    if decision != "schedule_now":
        return decision
    revision = _rule_revision_from_state(lifecycle_state)
    if not revision:
        return "none"
    receipt_id = f"rule-update:{revision}"
    existing = load_json(state_path)
    if existing.get("receipt_id") == receipt_id and existing.get("state") in {
        "RESUME_PENDING",
        "RESUME_CONFIRMED",
        "WAITING_FOR_CONTROLLER_PROGRESS",
        "WEB_REENTRY_SUBMITTED",
        "WEB_REENTRY_RESULT_UNKNOWN",
        "WEB_REENTRY_RETRY_EXHAUSTED",
        "RESUME_STALLED_NO_PROGRESS",
        "WAITING_EXTERNAL_HOST_RELOAD",
    }:
        return "already_scheduled"
    scheduled = schedule_auto_native_stop(
        session_id=session_id, repo=repo, receipt_id=receipt_id, registry=registry, codex=codex,
        delay_seconds=delay_seconds, state_path=state_path, capture_path=capture_path, runtime_path=runtime_path,
    )
    return "scheduled" if scheduled else "already_scheduled"


def refresh_rule_wake_state(*, session_id: str, repo: Path) -> dict[str, Any]:
    try:
        import lifecycle_hook as lifecycle
    except ModuleNotFoundError:
        from scripts import lifecycle_hook as lifecycle
    snapshot = lifecycle.project_snapshot(repo)
    if snapshot is None:
        return {}
    path = lifecycle.state_path(session_id)
    prior = lifecycle.load_json(path)
    event = post_tool_event(
        session_id=session_id, repo=repo, command="adaptive-delivery rule wake check", exit_code=0
    )
    _, next_state = lifecycle.evaluate_event(event, snapshot=snapshot, prior_state=prior)
    lifecycle.write_json(path, next_state)
    return next_state


def default_auto_stop_state_path(session_id: str) -> Path:
    return (
        Path.home()
        / ".codex"
        / "state"
        / "adaptive-delivery-web-lifecycle"
        / f"{session_id}.auto-stop.json"
    )


def auto_stop_supervisor_lock_path(state_path: Path) -> Path:
    return state_path.with_suffix(state_path.suffix + ".supervisor.lock")


def _pid_is_alive(pid: Any) -> bool:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _supervisor_token_is_current(
    state_path: Path, *, receipt_id: str, supervisor_token: str
) -> bool:
    state = load_json(state_path)
    return (
        state.get("receipt_id") == receipt_id
        and state.get("supervisor_receipt_id") == receipt_id
        and state.get("supervisor_token") == supervisor_token
    )


@contextmanager
def _owned_supervisor_state(
    state_path: Path, *, receipt_id: str, supervisor_token: str | None
):
    """Yield canonical state while fencing ownership-sensitive supervisor effects."""
    if not supervisor_token:
        yield load_json(state_path)
        return
    lock_path = auto_stop_supervisor_lock_path(state_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            state = load_json(state_path)
            if not (
                state.get("receipt_id") == receipt_id
                and state.get("supervisor_receipt_id") == receipt_id
                and state.get("supervisor_token") == supervisor_token
            ):
                yield None
                return
            yield state
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _release_supervisor_token(
    state_path: Path, *, receipt_id: str, supervisor_token: str
) -> None:
    lock_path = auto_stop_supervisor_lock_path(state_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            state = load_json(state_path)
            if (
                state.get("receipt_id") == receipt_id
                and state.get("supervisor_receipt_id") == receipt_id
                and state.get("supervisor_token") == supervisor_token
            ):
                for key in (
                    "supervisor_token",
                    "supervisor_receipt_id",
                    "supervisor_pid",
                    "supervisor_due_at_unix_ms",
                    "supervisor_spawned_at_unix_ms",
                ):
                    state.pop(key, None)
                write_auto_stop_state(state_path, state)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)



def _controller_web_wait_fence(
    *, registry: Path, controller_id: str
) -> dict[str, Any] | None:
    """Project only this Controller's Web target/ownership facts for wake de-duplication.

    Whole-registry hashes are intentionally excluded: unrelated Controller or metadata
    updates must never make an already-confirmed Web continuation eligible for re-submit.
    """
    registry_data = load_json(registry)
    target = target_guard.target_record(
        registry_data, controller_id=controller_id, host="web"
    )
    ownership = target_guard.execution_ownership_record(
        registry_data, controller_id=controller_id
    )
    if not isinstance(target, dict) or ownership is None:
        return None
    status, target_session, target_generation = target_guard.validate_target_record(
        target, host="web"
    )
    ownership_host, ownership_target, ownership_generation = (
        target_guard.validate_execution_ownership_record(ownership)
    )
    if (
        status != "active"
        or ownership_host != "web"
        or target_session != ownership_target
    ):
        return None
    return {
        "execution_target_session_id": target_session,
        "target_generation": target_generation,
        "ownership_generation": ownership_generation,
        "target_provenance": target.get("provenance"),
        "target_binding_mode": target.get("binding_mode"),
        "target_host_attested": target.get("host_attested"),
        "ownership_provenance": ownership.get("provenance"),
    }


def continuation_supervisor_needs_bootstrap(
    lifecycle_state: dict[str, Any],
    supervisor_state: dict[str, Any],
    *,
    current_registry_sha256: str | None = None,
    current_controller_wait_fence: dict[str, Any] | None = None,
) -> bool:
    if lifecycle_state.get("pending_control_event") is not True:
        return False
    if lifecycle_state.get("requires_user") is True:
        return False
    lifecycle_fingerprint = _wake_event_fingerprint(lifecycle_state)
    delivery_key = _lifecycle_delivery_key(lifecycle_state)
    terminal_key = str(supervisor_state.get("delivery_terminal_key") or "").strip()
    terminal_receipt = str(supervisor_state.get("delivery_terminal_receipt_id") or "").strip()
    terminal_outcome = str(supervisor_state.get("delivery_terminal_outcome") or "").strip()
    if (
        terminal_key == delivery_key
        and terminal_receipt
        and terminal_outcome in {"submit_confirmed", "result_unknown", "retry_exhausted"}
    ):
        return False
    legacy_receipt = str(supervisor_state.get("receipt_id") or "").strip()
    if (
        delivery_key.startswith("rule-update:")
        and legacy_receipt == delivery_key
        and str(supervisor_state.get("state") or "") in {
            "WAITING_FOR_CONTROLLER_PROGRESS",
            "WEB_REENTRY_SUBMITTED",
            "WEB_REENTRY_RESULT_UNKNOWN",
            "WEB_REENTRY_RETRY_EXHAUSTED",
            "RESUME_STALLED_NO_PROGRESS",
        }
    ):
        return False
    if (
        str(supervisor_state.get("state") or "") == "WAITING_FOR_CONTROLLER_PROGRESS"
        and str(supervisor_state.get("last_lifecycle_fingerprint") or "") == lifecycle_fingerprint
    ):
        waiting_fence = supervisor_state.get("waiting_controller_fence")
        if isinstance(waiting_fence, dict):
            if current_controller_wait_fence == waiting_fence:
                return False
            if current_controller_wait_fence is None:
                # Missing/invalid current target facts are not a reason to re-submit a
                # continuation that was already confirmed; fail closed until lifecycle changes.
                return False
        else:
            # Backward-compatible suppression for the immediately previous runtime revision.
            expected_target = str(supervisor_state.get("execution_target_session_id") or "").strip()
            expected_target_generation = supervisor_state.get("target_generation")
            expected_ownership_generation = supervisor_state.get("ownership_generation")
            if isinstance(current_controller_wait_fence, dict) and (
                current_controller_wait_fence.get("execution_target_session_id") == expected_target
                and current_controller_wait_fence.get("target_generation") == expected_target_generation
                and current_controller_wait_fence.get("ownership_generation") == expected_ownership_generation
            ):
                return False
            if current_controller_wait_fence is None:
                return False
    if (
        str(supervisor_state.get("state") or "") == "RESUME_STALLED_NO_PROGRESS"
        and str(supervisor_state.get("last_lifecycle_fingerprint") or "")
        == lifecycle_fingerprint
    ):
        return False
    if (
        str(supervisor_state.get("state") or "")
        in {"WEB_REENTRY_FAILED_BEFORE_DISPATCH", "WEB_REENTRY_RESULT_UNKNOWN"}
        and str(supervisor_state.get("last_lifecycle_fingerprint") or "")
        == lifecycle_fingerprint
    ):
        blocked_fence = supervisor_state.get("blocked_controller_fence")
        if isinstance(blocked_fence, dict):
            if current_controller_wait_fence == blocked_fence:
                return False
            if current_controller_wait_fence is None:
                # Once a non-retryable Host outcome is recorded, missing current
                # target facts are never a reason to try the same event again.
                return False
        else:
            # Upgrade compatibility for failure states persisted by the previous
            # Runtime revision before blocked_controller_fence existed.
            expected_target = str(
                supervisor_state.get("execution_target_session_id") or ""
            ).strip()
            expected_target_generation = supervisor_state.get("target_generation")
            expected_ownership_generation = supervisor_state.get("ownership_generation")
            if isinstance(current_controller_wait_fence, dict) and (
                current_controller_wait_fence.get("execution_target_session_id")
                == expected_target
                and current_controller_wait_fence.get("target_generation")
                == expected_target_generation
                and current_controller_wait_fence.get("ownership_generation")
                == expected_ownership_generation
            ):
                return False
            if current_controller_wait_fence is None:
                return False
    if (
        str(supervisor_state.get("state") or "")
        in {
            "WEB_REENTRY_IDENTITY_UNAVAILABLE",
            "WEB_REENTRY_TARGET_RECEIPT_MISMATCH",
        }
        and str(supervisor_state.get("last_lifecycle_fingerprint") or "")
        == _wake_event_fingerprint(lifecycle_state)
        and current_registry_sha256 is not None
        and str(supervisor_state.get("blocked_registry_sha256") or "")
        == current_registry_sha256
    ):
        return False
    active_states = {
        "RESUME_PENDING",
        "RESUME_REARMED",
        "RESUME_DEFERRED_ACTIVE_WRITER",
        "RESUME_RETRY_BACKOFF",
        "WEB_REENTRY_SUBMITTED",
        "WEB_REENTRY_MANUAL_FENCED_SUBMITTED",
        "WEB_REENTRY_DEFERRED_ACTIVE",
    }
    if str(supervisor_state.get("state") or "") not in active_states:
        return True
    receipt_id = str(supervisor_state.get("receipt_id") or "").strip()
    supervisor_receipt_id = str(
        supervisor_state.get("supervisor_receipt_id") or ""
    ).strip()
    supervisor_token = str(supervisor_state.get("supervisor_token") or "").strip()
    return not (
        receipt_id
        and supervisor_receipt_id == receipt_id
        and supervisor_token
        and _pid_is_alive(supervisor_state.get("supervisor_pid"))
    )


def ensure_continuation_supervisor(
    *,
    lifecycle_state: dict[str, Any],
    session_id: str,
    repo: Path,
    registry: Path,
    codex: str,
    runtime_path: str | None = None,
    delay_seconds: float = 1.0,
) -> bool:
    state_path = default_auto_stop_state_path(session_id)
    supervisor_state = load_json(state_path)
    try:
        current_controller_wait_fence = _controller_web_wait_fence(
            registry=registry, controller_id=session_id
        )
    except (OSError, ValueError, PermissionError):
        current_controller_wait_fence = None
    if not continuation_supervisor_needs_bootstrap(
        lifecycle_state,
        supervisor_state,
        current_registry_sha256=_file_sha256(registry),
        current_controller_wait_fence=current_controller_wait_fence,
    ):
        return False
    generation = int(lifecycle_state.get("wake_generation", 0) or 0)
    schedule_auto_native_stop(
        session_id=session_id,
        repo=repo,
        receipt_id=f"bootstrap:{generation}",
        registry=registry,
        codex=codex,
        delay_seconds=delay_seconds,
        state_path=state_path,
        runtime_path=runtime_path,
    )
    return True


def _schedule_auto_native_stop_locked(
    *,
    session_id: str,
    repo: Path,
    receipt_id: str,
    registry: Path,
    codex: str,
    delay_seconds: float,
    state_path: Path,
    capture_path: Path | None = None,
    runtime_path: str | None = None,
    force_rearm: bool = False,
    replace_supervisor_token: str | None = None,
) -> bool:
    """Schedule while the caller holds the supervisor lock."""
    prior = load_json(state_path)
    same_receipt = prior.get("receipt_id") == receipt_id
    terminal_receipt = str(prior.get("delivery_terminal_receipt_id") or "").strip()
    terminal_outcome = str(prior.get("delivery_terminal_outcome") or "").strip()
    legacy_terminal = str(prior.get("state") or "") in {
        "WAITING_FOR_CONTROLLER_PROGRESS",
        "WEB_REENTRY_SUBMITTED",
        "WEB_REENTRY_RESULT_UNKNOWN",
        "RESUME_STALLED_NO_PROGRESS",
        "WAITING_EXTERNAL_HOST_RELOAD",
    }
    if same_receipt and (
        (terminal_receipt == receipt_id and terminal_outcome in {"submit_confirmed", "result_unknown", "retry_exhausted"})
        or legacy_terminal
    ):
        prior["last_coalesced_at_unix_ms"] = int(time.time() * 1000)
        prior["terminal_delivery_coalesced_count"] = int(
            prior.get("terminal_delivery_coalesced_count", 0) or 0
        ) + 1
        write_auto_stop_state(state_path, prior)
        return False
    current_token = str(prior.get("supervisor_token") or "").strip()
    current_receipt = str(prior.get("supervisor_receipt_id") or "").strip()
    current_live = (
        bool(current_token)
        and current_receipt == receipt_id
        and _pid_is_alive(prior.get("supervisor_pid"))
    )
    if force_rearm:
        if (
            not replace_supervisor_token
            or current_token != replace_supervisor_token
            or current_receipt != receipt_id
        ):
            return False
    elif current_live:
        prior["coalesced_schedule_count"] = (
            int(prior.get("coalesced_schedule_count", 0) or 0) + 1
        )
        prior["last_coalesced_at_unix_ms"] = int(time.time() * 1000)
        write_auto_stop_state(state_path, prior)
        return False

    now_ms = int(time.time() * 1000)
    supervisor_token = secrets.token_hex(16)
    value = {
        "receipt_id": receipt_id,
        "session_id": session_id,
        "repo": str(repo.resolve()),
        "scheduled_at_unix_ms": now_ms,
        "state": "RESUME_PENDING",
        "pending_control_event": True,
        "retry_count": int(prior.get("retry_count", 0) or 0) if same_receipt else 0,
        "continuation_count": int(prior.get("continuation_count", 0) or 0) if same_receipt else 0,
        "unchanged_continuation_count": int(prior.get("unchanged_continuation_count", 0) or 0) if same_receipt else 0,
        "coalesced_schedule_count": int(prior.get("coalesced_schedule_count", 0) or 0) if same_receipt else 0,
        "supervisor_token": supervisor_token,
        "supervisor_receipt_id": receipt_id,
        "supervisor_pid": 0,
        "supervisor_due_at_unix_ms": now_ms + int(max(0.0, delay_seconds) * 1000),
        "supervisor_spawned_at_unix_ms": now_ms,
    }
    if same_receipt and prior.get("last_lifecycle_fingerprint"):
        value["last_lifecycle_fingerprint"] = str(prior["last_lifecycle_fingerprint"])
    if same_receipt and prior.get("approval_id"):
        value["approval_id"] = str(prior["approval_id"])
    if same_receipt and isinstance(prior.get("approval_expires_at_unix"), int):
        value["approval_expires_at_unix"] = int(prior["approval_expires_at_unix"])
    if same_receipt:
        value["approval_retry_count"] = int(prior.get("approval_retry_count", 0) or 0)
    if same_receipt and prior.get("failure_class") == "active_writer_present":
        value["last_deferred_state"] = "RESUME_DEFERRED_ACTIVE_WRITER"
        value["failure_class"] = "active_writer_present"
        value["stderr_tail"] = bounded_tail(str(prior.get("stderr_tail", "")))
    elif same_receipt and prior.get("failure_class") == "web_reentry_unavailable":
        value["failure_class"] = "web_reentry_unavailable"
        value["error_code"] = str(prior.get("error_code") or "WEB_REENTRY_UNAVAILABLE")
        value["stderr_tail"] = bounded_tail(str(prior.get("stderr_tail", "")))

    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "auto-native-stop",
        "--session-id", session_id,
        "--repo", str(repo.resolve()),
        "--receipt-id", receipt_id,
        "--registry", str(registry.expanduser()),
        "--codex", codex,
        "--delay-seconds", str(delay_seconds),
        "--state", str(state_path),
        "--runtime-path", runtime_path or DEFAULT_RUNTIME_PATH,
        "--supervisor-token", supervisor_token,
    ]
    write_auto_stop_state(state_path, value)
    if capture_path is not None:
        capture_path.write_text(
            json.dumps(command, ensure_ascii=False) + chr(10), encoding="utf-8"
        )
        return True

    launcher_log = state_path.with_suffix(state_path.suffix + ".launcher.log")
    rotate_launcher_log(launcher_log)
    launcher_log.parent.mkdir(parents=True, exist_ok=True)
    log_handle = launcher_log.open("ab", buffering=0)
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=log_handle,
            env=native_runtime_env(runtime_path),
            start_new_session=True,
            close_fds=True,
        )
    finally:
        log_handle.close()
    latest = load_json(state_path)
    if latest.get("supervisor_token") == supervisor_token:
        latest["supervisor_pid"] = int(process.pid)
        write_auto_stop_state(state_path, latest)
    return True


def schedule_auto_native_stop(
    *,
    session_id: str,
    repo: Path,
    receipt_id: str,
    registry: Path,
    codex: str,
    delay_seconds: float,
    state_path: Path,
    capture_path: Path | None = None,
    runtime_path: str | None = None,
    force_rearm: bool = False,
    replace_supervisor_token: str | None = None,
) -> bool:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = auto_stop_supervisor_lock_path(state_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            return _schedule_auto_native_stop_locked(
                session_id=session_id,
                repo=repo,
                receipt_id=receipt_id,
                registry=registry,
                codex=codex,
                delay_seconds=delay_seconds,
                state_path=state_path,
                capture_path=capture_path,
                runtime_path=runtime_path,
                force_rearm=force_rearm,
                replace_supervisor_token=replace_supervisor_token,
            )
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _rearm_auto_native_stop(
    *,
    session_id: str,
    repo: Path,
    receipt_id: str,
    registry: Path,
    codex: str,
    delay_seconds: float,
    state_path: Path,
    runtime_path: str | None,
    supervisor_token: str | None,
    supervisor_lock_held: bool,
) -> bool:
    kwargs = {
        "session_id": session_id,
        "repo": repo,
        "receipt_id": receipt_id,
        "registry": registry,
        "codex": codex,
        "delay_seconds": delay_seconds,
        "state_path": state_path,
        "runtime_path": runtime_path,
        "force_rearm": supervisor_token is not None,
        "replace_supervisor_token": supervisor_token,
    }
    if supervisor_lock_held:
        return _schedule_auto_native_stop_locked(**kwargs)
    return schedule_auto_native_stop(**kwargs)


def _canonical_ownership_fence(
    *, repo: Path, registry: Path, session_id: str
) -> dict[str, Any] | None:
    del repo  # Hot-path fence is registry-only; never launch Git/subprocess work here.
    registry_data = load_json(registry)
    ownership = target_guard.execution_ownership_record(
        registry_data, controller_id=session_id
    )
    if ownership is None:
        return None
    active_host, ownership_target, generation = (
        target_guard.validate_execution_ownership_record(ownership)
    )
    host_target = target_guard.target_record(
        registry_data, controller_id=session_id, host=active_host
    )
    if host_target is None:
        aliases = target_guard.host_sessions(
            registry_data, controller_id=session_id, host=active_host
        )
        if aliases or ownership_target != session_id:
            raise PermissionError(
                "canonical Controller ownership target is not current for its host"
            )
    else:
        status, current_target, _host_generation = target_guard.validate_target_record(
            host_target, host=active_host
        )
        if status != "active" or current_target != ownership_target:
            raise PermissionError(
                "canonical Controller ownership target is not current for its host"
            )
    return {
        "controller_id": session_id,
        "active_host": active_host,
        "execution_target_session_id": ownership_target,
        "generation": generation,
    }


def _canonical_ownership_fence_matches(
    expected: dict[str, Any] | None,
    *, repo: Path, registry: Path, session_id: str,
) -> bool:
    if expected is None:
        return True
    try:
        current = _canonical_ownership_fence(
            repo=repo, registry=registry, session_id=session_id
        )
    except (OSError, ValueError, PermissionError, subprocess.SubprocessError):
        return False
    return current == expected


def _validate_confirmed_web_reentry_receipt(
    *,
    attempt: dict[str, Any],
    session_id: str,
    registry: Path,
    ownership_fence: dict[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(ownership_fence, dict):
        raise PermissionError("canonical Controller execution ownership is missing")
    registry_data = load_json(registry)
    target = target_guard.target_record(
        registry_data, controller_id=session_id, host="web"
    )
    ownership = target_guard.execution_ownership_record(
        registry_data, controller_id=session_id
    )
    if target is None or ownership is None:
        raise PermissionError("canonical Web target or execution ownership is missing")
    if (
        target.get("provenance") == "host_attested_same_controller_recovery"
        and target.get("identity_proof") != "host_attested_origin"
    ):
        raise PermissionError(
            "legacy browser-tab Web identity record is not a trusted Host origin attestation"
        )
    status, expected_target, target_generation = target_guard.validate_target_record(
        target, host="web"
    )
    ownership_host, ownership_target, ownership_generation = (
        target_guard.validate_execution_ownership_record(ownership)
    )
    if (
        status != "active"
        or ownership_host != "web"
        or ownership_target != expected_target
        or ownership_fence.get("active_host") != "web"
        or ownership_fence.get("execution_target_session_id") != expected_target
        or ownership_fence.get("generation") != ownership_generation
        or attempt.get("execution_target_session_id") != expected_target
        or attempt.get("target_generation") != target_generation
        or attempt.get("ownership_generation") != ownership_generation
        or attempt.get("target_mode") != "explicit_current"
    ):
        raise PermissionError(
            "Web host target receipt does not match canonical target and ownership generations"
        )
    return {
        "execution_target_session_id": expected_target,
        "target_generation": target_generation,
        "ownership_generation": ownership_generation,
        "target_mode": "explicit_current",
    }


def _run_auto_native_stop_impl(
    *,
    session_id: str,
    repo: Path,
    receipt_id: str,
    registry: Path,
    codex: str,
    delay_seconds: float,
    state_path: Path,
    runtime_path: str | None = None,
    supervisor_token: str | None = None,
    supervisor_lock_held: bool = False,
) -> int:
    if delay_seconds > 0:
        time.sleep(delay_seconds)
    with _owned_supervisor_state(
        state_path, receipt_id=receipt_id, supervisor_token=supervisor_token
    ) as state:
        if state is None or state.get("receipt_id") != receipt_id:
            return 0
        latest = dict(state)
        latest.update({
            "state": "RESUME_PENDING",
            "pending_control_event": True,
            "started_at_unix_ms": int(time.time() * 1000),
            "controller_id": session_id,
            "runtime_path": runtime_path or DEFAULT_RUNTIME_PATH,
        })
        for stale_key in (
            "command",
            "execution_target_session_id",
            "target_generation",
            "target_mode",
        ):
            latest.pop(stale_key, None)
        write_auto_stop_state(state_path, latest)
    lifecycle_state = _load_lifecycle_state(session_id)
    lifecycle_state = lifecycle_state if isinstance(lifecycle_state, dict) else {}
    controller_host = resolve_controller_host(
        lifecycle_state, {}, load_json(registry), session_id
    )
    ownership_fence = _canonical_ownership_fence(
        repo=repo, registry=registry, session_id=session_id
    )
    if ownership_fence is not None and ownership_fence.get("active_host") != controller_host:
        raise PermissionError("resolved Controller host disagrees with canonical ownership")
    if controller_host == "web":
        # Read the current supervisor-owned inputs under lock, but never hold the
        # lock while Host/Web re-entry performs external work. Superseding a
        # stuck generation must stay possible.
        with _owned_supervisor_state(
            state_path, receipt_id=receipt_id, supervisor_token=supervisor_token
        ) as current:
            if current is None:
                return 0
            if lifecycle_state.get("pending_control_event") is not True:
                current.update({"state": "CONTINUATION_CLOSED", "pending_control_event": False})
                current.pop("failure_class", None)
                current.pop("error_code", None)
                current.pop("blocked_registry_sha256", None)
                current.pop("blocked_controller_fence", None)
                current.pop("blocked_since_unix_ms", None)
                write_auto_stop_state(state_path, current)
                return 0
            if lifecycle_state.get("requires_user") is True:
                current.update({
                    "state": "WAITING_USER", "pending_control_event": True,
                    "failure_class": "user_decision_required",
                })
                current.pop("error_code", None)
                write_auto_stop_state(state_path, current)
                return 0
            approval_id = str(current.get("approval_id") or "").strip() or None

        fingerprint = _wake_event_fingerprint(lifecycle_state)
        verifier = _registered_peer_attestation_verifier("web")
        registry_data_for_web = load_json(registry)
        current_web_record = target_guard.target_record(
            registry_data_for_web, controller_id=session_id, host="web"
        )
        use_registered_host = (
            isinstance(current_web_record, dict)
            and current_web_record.get("provenance")
            == "host_attested_same_controller_recovery"
            and current_web_record.get("identity_proof") == "host_attested_origin"
            and callable(verifier)
            and callable(getattr(verifier, "submit_reentry", None))
        )
        if use_registered_host:
            try:
                attempt = _execute_registered_web_host_reentry(
                    session_id=session_id,
                    repo=repo,
                    registry=registry,
                    lifecycle_state=lifecycle_state,
                    runtime_path=runtime_path,
                    ownership_fence=ownership_fence,
                    verifier=verifier,
                )
            except PeerHostTransientUnavailable as exc:
                attempt = {
                    "operation": "web_reentry",
                    "result": "DEFERRED",
                    "state": "WEB_REENTRY_PENDING",
                    "returncode": 78,
                    "failure_class": "web_reentry_unavailable",
                    "error_code": "WEB_HOST_TEMPORARILY_UNAVAILABLE",
                    "stderr_tail": str(exc),
                }
            except Exception as exc:
                attempt = {
                    "operation": "web_reentry",
                    "result": "FAILED",
                    "state": "WEB_REENTRY_IDENTITY_UNAVAILABLE",
                    "returncode": 78,
                    "failure_class": "web_reentry_identity_unavailable",
                    "error_code": "WEB_HOST_ATTESTATION_INVALID",
                    "stderr_tail": str(exc),
                }
        else:
            attempt = execute_web_reentry(
                controller_id=session_id, repo=repo, registry_path=registry,
                lease_path=DEFAULT_MANUAL_WEB_LEASES, lifecycle_state=lifecycle_state,
                approval_id=approval_id,
                origin_verifier=verifier,
            )
        if attempt.get("result") == "CONFIRMED":
            try:
                receipt = _validate_confirmed_web_reentry_receipt(
                    attempt=attempt,
                    session_id=session_id,
                    registry=registry,
                    ownership_fence=ownership_fence,
                )
            except (OSError, ValueError, PermissionError) as exc:
                attempt = {
                    "operation": "web_reentry",
                    "result": "DEFERRED",
                    "state": "WEB_REENTRY_TARGET_RECEIPT_MISMATCH",
                    "returncode": 78,
                    "failure_class": "web_reentry_identity_unavailable",
                    "error_code": "CONTROLLER_TARGET_RECEIPT_MISMATCH",
                    "stderr_tail": str(exc),
                    "execution_target_session_id": None,
                    "target_generation": None,
                    "ownership_generation": None,
                }
            else:
                attempt.update(receipt)

        # External work completed; only the still-current generation may commit
        # its result or schedule its successor.
        with _owned_supervisor_state(
            state_path, receipt_id=receipt_id, supervisor_token=supervisor_token
        ) as current:
            if current is None:
                return 0
            owned_lock_held = supervisor_token is not None
            if not _canonical_ownership_fence_matches(
                ownership_fence, repo=repo, registry=registry, session_id=session_id
            ):
                current.update({
                    "state": "WEB_REENTRY_SUPERSEDED_HOST_HANDOFF",
                    "pending_control_event": True,
                    "failure_class": "host_ownership_superseded",
                    "completed_at_unix_ms": int(time.time() * 1000),
                })
                write_auto_stop_state(state_path, current)
                return 0
            for evidence_key in (
                "execution_target_session_id",
                "target_generation",
                "ownership_generation",
                "target_mode",
                "delivery_authorization",
                "host_attested",
                "strong_web_identity_established",
            ):
                if evidence_key in attempt:
                    current[evidence_key] = attempt[evidence_key]
            if attempt.get("state") == "WEB_REENTRY_WAITING_LOCAL_APPROVAL":
                retry_count = int(current.get("approval_retry_count", 0) or 0) + 1
                current.update({
                    "state": "WEB_REENTRY_WAITING_LOCAL_APPROVAL",
                    "pending_control_event": True,
                    "failure_class": "local_approval_required",
                    "approval_retry_count": retry_count,
                    "completed_at_unix_ms": int(time.time() * 1000),
                })
                if isinstance(attempt.get("approval_id"), str) and attempt.get("approval_id"):
                    current["approval_id"] = attempt["approval_id"]
                if isinstance(attempt.get("approval_expires_at_unix"), int):
                    current["approval_expires_at_unix"] = attempt["approval_expires_at_unix"]
                write_auto_stop_state(state_path, current)
                expires_at = current.get("approval_expires_at_unix")
                if (not isinstance(expires_at, int) or expires_at > int(time.time())) and retry_count < 24:
                    _rearm_auto_native_stop(
                        session_id=session_id, repo=repo, receipt_id=receipt_id, registry=registry,
                        codex=codex, delay_seconds=5.0, state_path=state_path, runtime_path=runtime_path,
                        supervisor_token=supervisor_token,
                        supervisor_lock_held=owned_lock_held,
                    )
                    return 0
                current["state"] = "WAITING_USER"
                current["failure_class"] = "local_approval_required"
                write_auto_stop_state(state_path, current)
                return 0
            if attempt.get("state") == "WEB_REENTRY_DEFERRED_ACTIVE":
                current.update({
                    "state": "WEB_REENTRY_DEFERRED_ACTIVE",
                    "pending_control_event": True,
                    "failure_class": "web_host_active",
                    "completed_at_unix_ms": int(time.time() * 1000),
                })
                write_auto_stop_state(state_path, current)
                _rearm_auto_native_stop(
                    session_id=session_id, repo=repo, receipt_id=receipt_id, registry=registry,
                    codex=codex, delay_seconds=5.0, state_path=state_path, runtime_path=runtime_path,
                    supervisor_token=supervisor_token,
                    supervisor_lock_held=owned_lock_held,
                )
                return 0
            if attempt.get("result") == "CONFIRMED":
                previous_fingerprint = str(current.get("last_lifecycle_fingerprint") or "")
                unchanged = int(current.get("unchanged_continuation_count", 0) or 0)
                unchanged = unchanged + 1 if previous_fingerprint == fingerprint else 0
                continuation_count = int(current.get("continuation_count", 0) or 0) + 1
                now_ms = int(time.time() * 1000)
                current.update({
                    "state": "WAITING_FOR_CONTROLLER_PROGRESS",
                    "pending_control_event": True,
                    "continuation_count": continuation_count,
                    "unchanged_continuation_count": unchanged,
                    "last_lifecycle_fingerprint": fingerprint,
                    "completed_at_unix_ms": now_ms,
                    "returncode": 0,
                    "delivery_terminal_receipt_id": receipt_id,
                    "delivery_terminal_key": _lifecycle_delivery_key(lifecycle_state),
                    "delivery_terminal_outcome": "submit_confirmed",
                    "delivery_terminal_at_unix_ms": now_ms,
                })
                current.pop("failure_class", None)
                current.pop("error_code", None)
                current.pop("blocked_registry_sha256", None)
                current.pop("blocked_controller_fence", None)
                current.pop("blocked_since_unix_ms", None)
                try:
                    waiting_controller_fence = _controller_web_wait_fence(
                        registry=registry, controller_id=session_id
                    )
                except (OSError, ValueError, PermissionError):
                    waiting_controller_fence = None
                if isinstance(waiting_controller_fence, dict):
                    current["waiting_controller_fence"] = waiting_controller_fence
                else:
                    current.pop("waiting_controller_fence", None)
                current.pop("waiting_registry_sha256", None)
                current["waiting_since_unix_ms"] = now_ms
                write_auto_stop_state(state_path, current)
                return 0
            failure_class = str(attempt.get("failure_class") or "web_reentry_unavailable")
            retry_count = int(current.get("retry_count", 0) or 0)
            if failure_class == "web_reentry_unavailable":
                retry_count += 1
            current.update({
                "state": str(attempt.get("state") or "WEB_REENTRY_PENDING"),
                "pending_control_event": True,
                "completed_at_unix_ms": int(time.time() * 1000),
                "returncode": int(attempt.get("returncode", 78) or 78),
                "failure_class": failure_class,
                "error_code": str(attempt.get("error_code") or "WEB_REENTRY_UNAVAILABLE"),
                "stderr_tail": bounded_tail(str(attempt.get("stderr_tail", ""))),
                "retry_count": retry_count,
            })
            if (
                failure_class == "web_reentry_unavailable"
                and retry_count >= WEB_REENTRY_TRANSIENT_RETRY_LIMIT
            ):
                terminal_now_ms = int(time.time() * 1000)
                current.update({
                    "state": "WEB_REENTRY_RETRY_EXHAUSTED",
                    "failure_class": "web_reentry_retry_exhausted",
                    "error_code": "WEB_REENTRY_RETRY_EXHAUSTED",
                    "returncode": 78,
                    "delivery_terminal_receipt_id": receipt_id,
                    "delivery_terminal_key": _lifecycle_delivery_key(lifecycle_state),
                    "delivery_terminal_outcome": "retry_exhausted",
                    "delivery_terminal_at_unix_ms": terminal_now_ms,
                    "completed_at_unix_ms": terminal_now_ms,
                })
            if failure_class == "web_reentry_identity_unavailable":
                current.update({
                    "last_lifecycle_fingerprint": fingerprint,
                    "blocked_registry_sha256": _file_sha256(registry),
                })
            if failure_class in {
                "web_reentry_failed_before_dispatch",
                "web_reentry_result_unknown",
            }:
                current["last_lifecycle_fingerprint"] = fingerprint
                if failure_class == "web_reentry_result_unknown":
                    terminal_now_ms = int(time.time() * 1000)
                    current.update({
                        "delivery_terminal_receipt_id": receipt_id,
                        "delivery_terminal_key": _lifecycle_delivery_key(lifecycle_state),
                        "delivery_terminal_outcome": "result_unknown",
                        "delivery_terminal_at_unix_ms": terminal_now_ms,
                    })
                try:
                    blocked_controller_fence = _controller_web_wait_fence(
                        registry=registry, controller_id=session_id
                    )
                except (OSError, ValueError, PermissionError):
                    blocked_controller_fence = None
                if isinstance(blocked_controller_fence, dict):
                    current["blocked_controller_fence"] = blocked_controller_fence
                else:
                    current.pop("blocked_controller_fence", None)
                current["blocked_since_unix_ms"] = int(time.time() * 1000)
            else:
                current.pop("blocked_controller_fence", None)
                current.pop("blocked_since_unix_ms", None)
            write_auto_stop_state(state_path, current)
            if failure_class == "web_reentry_unavailable":
                if retry_count >= WEB_REENTRY_TRANSIENT_RETRY_LIMIT:
                    return 78
                retry_delay = min(60.0, float(2 ** min(max(retry_count - 1, 0), 5)))
                _rearm_auto_native_stop(
                    session_id=session_id, repo=repo, receipt_id=receipt_id, registry=registry,
                    codex=codex, delay_seconds=max(1.0, retry_delay), state_path=state_path,
                    runtime_path=runtime_path,
                    supervisor_token=supervisor_token,
                    supervisor_lock_held=owned_lock_held,
                )
                return 0
            return int(attempt.get("returncode", 78) or 78)
    # Validate ownership under the supervisor lock, then release it before the
    # potentially long external resume. A newer supervisor generation must be
    # able to supersede this worker while the external command is in flight.
    with _owned_supervisor_state(
        state_path, receipt_id=receipt_id, supervisor_token=supervisor_token
    ) as latest:
        if latest is None:
            return 0

    attempt = execute_native_resume(
        session_id=session_id, repo=repo, registry=registry, codex=codex,
        runtime_path=runtime_path,
        terminal_receipts=lifecycle_state.get("pending_terminal_receipts", []) if isinstance(lifecycle_state, dict) else [],
        next_action=str(lifecycle_state.get("next_action") or "").strip() or None if isinstance(lifecycle_state, dict) else None,
        supervisor_state_path=state_path,
        supervisor_receipt_id=receipt_id,
        supervisor_token=supervisor_token,
    )
    if (
        attempt.get("state") == "RESUME_TARGET_INCOMPATIBLE"
        and attempt.get("replacement_eligible") is True
    ):
        failed_target = str(attempt.get("execution_target_session_id") or "").strip()
        failed_generation = attempt.get("target_generation")
        failed_ownership_generation = (
            ownership_fence.get("generation") if isinstance(ownership_fence, dict) else None
        )
        if (
            failed_target
            and isinstance(failed_generation, int)
            and not isinstance(failed_generation, bool)
            and isinstance(failed_ownership_generation, int)
            and not isinstance(failed_ownership_generation, bool)
        ):
            attempt = recover_incompatible_native_target(
                session_id=session_id, repo=repo, registry=registry, codex=codex,
                failed_target_session_id=failed_target, expected_generation=failed_generation,
                expected_ownership_generation=failed_ownership_generation,
                runtime_path=runtime_path,
                terminal_receipts=lifecycle_state.get("pending_terminal_receipts", []) if isinstance(lifecycle_state, dict) else [],
                next_action=str(lifecycle_state.get("next_action") or "").strip() or None if isinstance(lifecycle_state, dict) else None,
                supervisor_state_path=state_path,
                supervisor_receipt_id=receipt_id,
                supervisor_token=supervisor_token,
            )

    replacement_target = str(
        attempt.get("replacement_execution_target_session_id") or ""
    ).strip()
    replacement_ownership_generation = attempt.get("ownership_generation")
    if (
        attempt.get("operation") == "native_target_recovery"
        and replacement_target
        and isinstance(replacement_ownership_generation, int)
        and not isinstance(replacement_ownership_generation, bool)
    ):
        ownership_fence = {
            "controller_id": session_id,
            "active_host": target_guard.DESKTOP_SESSION_HOST,
            "execution_target_session_id": replacement_target,
            "generation": replacement_ownership_generation,
        }

    if ownership_fence is not None:
        attempt["ownership_host"] = ownership_fence.get("active_host")
        attempt["ownership_execution_target_session_id"] = ownership_fence.get(
            "execution_target_session_id"
        )
        attempt["ownership_generation"] = ownership_fence.get("generation")

    attempt = confirm_host_observed_desktop_foreground(
        attempt=attempt,
        lifecycle_state=lifecycle_state,
        ownership_fence=ownership_fence,
        host_reload_required=desktop_host_reload_required(
            session_id=session_id,
            repo=repo,
            registry_path=registry,
        ),
    )

    # Revalidate both supervisor token and canonical host ownership before
    # committing any external result or rearming.
    with _owned_supervisor_state(
        state_path, receipt_id=receipt_id, supervisor_token=supervisor_token
    ) as latest:
        if latest is None:
            return 0
        if not _canonical_ownership_fence_matches(
            ownership_fence, repo=repo, registry=registry, session_id=session_id
        ):
            latest.update({
                "state": "RESUME_SUPERSEDED_HOST_HANDOFF",
                "pending_control_event": True,
                "failure_class": "host_ownership_superseded",
                "completed_at_unix_ms": int(time.time() * 1000),
            })
            write_auto_stop_state(state_path, latest)
            return 0
        for evidence_key in (
            "command",
            "execution_target_session_id",
            "target_generation",
            "target_mode",
            "host_reload_required",
            "activation_gate",
            "host_returncode",
        ):
            if evidence_key in attempt:
                latest[evidence_key] = attempt[evidence_key]
        if attempt["result"] == "CONFIRMED":
            latest.update({
                "state": "RESUME_CONFIRMED",
                "pending_control_event": True,
                "completed_at_unix_ms": int(time.time() * 1000),
                "returncode": attempt["returncode"],
                "stdout_tail": attempt.get("stdout_tail", ""),
                "stderr_tail": attempt.get("stderr_tail", ""),
            })
            latest.pop("error_code", None)
            latest.pop("failure_class", None)
            latest.pop("fallback_eligible", None)
        else:
            latest.update({
                "state": attempt["state"],
                "pending_control_event": True,
                "completed_at_unix_ms": int(time.time() * 1000),
                "returncode": attempt["returncode"],
                "stdout_tail": attempt.get("stdout_tail", ""),
                "stderr_tail": attempt.get("stderr_tail", ""),
            })
            for key in ("error_code", "failure_class", "fallback_eligible", "replacement_eligible"):
                if key in attempt:
                    latest[key] = attempt[key]
        write_auto_stop_state(state_path, latest)
        if attempt.get("state") == "RESUME_DEFERRED_ACTIVE_WRITER":
            latest["retry_count"] = int(latest.get("retry_count", 0) or 0) + 1
            write_auto_stop_state(state_path, latest)
            retry_delay = min(30.0, max(1.0, 2.0 ** min(latest["retry_count"], 4)))
            _rearm_auto_native_stop(
                session_id=session_id, repo=repo, receipt_id=receipt_id, registry=registry,
                codex=codex, delay_seconds=retry_delay, state_path=state_path, runtime_path=runtime_path,
                supervisor_token=supervisor_token,
                supervisor_lock_held=supervisor_token is not None,
            )
            return 0
        if str(attempt.get("failure_class") or "") in {"usage_limit_exceeded", "quota_exhausted"}:
            latest.update({
                "state": "RESUME_RETRY_BACKOFF",
                "pending_control_event": True,
                "retry_count": int(latest.get("retry_count", 0) or 0) + 1,
            })
            write_auto_stop_state(state_path, latest)
            _rearm_auto_native_stop(
                session_id=session_id, repo=repo, receipt_id=receipt_id, registry=registry,
                codex=codex, delay_seconds=300.0, state_path=state_path, runtime_path=runtime_path,
                supervisor_token=supervisor_token,
                supervisor_lock_held=supervisor_token is not None,
            )
            return 0

    if attempt.get("result") == "CONFIRMED":
        persist_confirmed_auto_native_wake(
            lifecycle_state=lifecycle_state,
            session_id=session_id,
            repo=repo,
            registry=registry,
            attempt=attempt,
            state_path=state_path,
            receipt_id=receipt_id,
            supervisor_token=supervisor_token,
        )

    stderr_tail = str(attempt.get("stderr_tail", ""))
    if stderr_tail:
        print(stderr_tail, file=sys.stderr, end="" if stderr_tail.endswith("\n") else "\n")
    if attempt.get("result") == "CONFIRMED":
        fresh_lifecycle = _load_lifecycle_state(session_id)
        with _owned_supervisor_state(
            state_path, receipt_id=receipt_id, supervisor_token=supervisor_token
        ) as current:
            if current is None or current.get("receipt_id") != receipt_id:
                return 0
            if not fresh_lifecycle:
                # No authoritative lifecycle state means there is nothing safe to re-arm from.
                # Keep the successful receipt, preserve pending evidence, and fail closed against spinning.
                current["state"] = "RESUME_CONFIRMED"
                current["pending_control_event"] = True
                current["lifecycle_rearm"] = "unavailable"
                write_auto_stop_state(state_path, current)
                return 0
            pending = fresh_lifecycle.get("pending_control_event") is True
            requires_user = fresh_lifecycle.get("requires_user") is True
            current["pending_control_event"] = pending
            if not pending:
                current["state"] = "CONTINUATION_CLOSED"
                current.pop("failure_class", None)
                write_auto_stop_state(state_path, current)
                return 0
            if requires_user:
                current["state"] = "WAITING_USER"
                current["failure_class"] = "user_decision_required"
                write_auto_stop_state(state_path, current)
                return 0
            fingerprint = _wake_event_fingerprint(fresh_lifecycle)
            previous_fingerprint = str(current.get("last_lifecycle_fingerprint") or "")
            unchanged = int(current.get("unchanged_continuation_count", 0) or 0)
            unchanged = unchanged + 1 if previous_fingerprint == fingerprint else 0
            continuation_count = int(current.get("continuation_count", 0) or 0) + 1
            current.update({
                "state": "RESUME_REARMED",
                "pending_control_event": True,
                "continuation_count": continuation_count,
                "unchanged_continuation_count": unchanged,
                "last_lifecycle_fingerprint": fingerprint,
            })
            if unchanged >= AUTO_CONTINUATION_STALL_LIMIT:
                current.update({
                    "state": "RESUME_STALLED_NO_PROGRESS",
                    "failure_class": "confirmed_resume_without_machine_progress",
                    "error_code": "WEB_LIFECYCLE_CONTINUATION_STALLED",
                })
                write_auto_stop_state(state_path, current)
                return 78
            current.pop("failure_class", None)
            current.pop("error_code", None)
            write_auto_stop_state(state_path, current)
            _rearm_auto_native_stop(
                session_id=session_id, repo=repo, receipt_id=receipt_id, registry=registry,
                codex=codex, delay_seconds=min(5.0, 1.0 + unchanged), state_path=state_path,
                runtime_path=runtime_path,
            supervisor_token=supervisor_token,
            supervisor_lock_held=supervisor_token is not None,
            )
            return 0
    return int(attempt["returncode"])


def run_auto_native_stop(
    *,
    session_id: str,
    repo: Path,
    receipt_id: str,
    registry: Path,
    codex: str,
    delay_seconds: float,
    state_path: Path,
    runtime_path: str | None = None,
    supervisor_token: str | None = None,
) -> int:
    if not supervisor_token:
        return _run_auto_native_stop_impl(
            session_id=session_id,
            repo=repo,
            receipt_id=receipt_id,
            registry=registry,
            codex=codex,
            delay_seconds=delay_seconds,
            state_path=state_path,
            runtime_path=runtime_path,
        )

    lock_path = auto_stop_supervisor_lock_path(state_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    def owns_current_generation() -> bool:
        with lock_path.open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                return _supervisor_token_is_current(
                    state_path,
                    receipt_id=receipt_id,
                    supervisor_token=supervisor_token,
                )
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    if not owns_current_generation():
        return 0
    if delay_seconds > 0:
        time.sleep(delay_seconds)
    if not owns_current_generation():
        return 0
    try:
        return _run_auto_native_stop_impl(
            session_id=session_id,
            repo=repo,
            receipt_id=receipt_id,
            registry=registry,
            codex=codex,
            delay_seconds=0,
            state_path=state_path,
            runtime_path=runtime_path,
            supervisor_token=supervisor_token,
            supervisor_lock_held=False,
        )
    finally:
        _release_supervisor_token(
            state_path,
            receipt_id=receipt_id,
            supervisor_token=supervisor_token,
        )


def zshenv_block() -> str:
    script = Path(__file__).resolve()
    python = Path("/Library/Frameworks/Python.framework/Versions/3.11/bin/python3")
    return f'''# >>> adaptive-delivery web lifecycle bridge >>>
_ad_web_parent=$(/bin/ps -p "$PPID" -o comm= 2>/dev/null)
_ad_web_session_id="${{ADAPTIVE_DELIVERY_WEB_SESSION_ID:-}}"
_ad_web_bridge_script="{script}"
_ad_web_bridge_python="{python}"
if [[ "$_ad_web_parent" == "{AI_BRIDGE_EXECUTABLE}" && -n "$_ad_web_session_id" ]]; then
  _ad_web_cwd="$PWD"
  _ad_web_command="$ZSH_EXECUTION_STRING"
  _ad_web_lifecycle_exit() {{
    local _ad_web_exit_code=$?
    trap - EXIT
    "$_ad_web_bridge_python" "$_ad_web_bridge_script" post-shell --cwd "$_ad_web_cwd" --command "$_ad_web_command" --exit-code "$_ad_web_exit_code" --web-session-id "$_ad_web_session_id"
    local _ad_web_bridge_exit_code=$?
    if [[ "$_ad_web_exit_code" -ne 0 ]]; then
      exit "$_ad_web_exit_code"
    fi
    exit "$_ad_web_bridge_exit_code"
  }}
  trap _ad_web_lifecycle_exit EXIT
fi
unset _ad_web_parent
# <<< adaptive-delivery web lifecycle bridge <<<'''


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Bridge AI-Bridge Web tool events into Adaptive Agent Runtime lifecycle hooks."
    )
    subparsers = parser.add_subparsers(dest="command_name", required=True)

    translate = subparsers.add_parser("translate-receipt")
    translate.add_argument("--session-id", required=True)
    translate.add_argument("--repo", required=True)
    translate.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    translate.add_argument("--web-session-id")

    bind_web = subparsers.add_parser("bind-web-session")
    bind_web.add_argument("--repo", required=True)
    bind_web.add_argument("--controller-id", required=True)
    bind_web.add_argument("--web-session-id", required=True)
    bind_web.add_argument("--registry", default=str(DEFAULT_REGISTRY))

    replace_web = subparsers.add_parser("replace-web-session")
    replace_web.add_argument("--repo", required=True)
    replace_web.add_argument("--controller-id", required=True)
    replace_web.add_argument("--web-session-id", required=True)
    replace_web.add_argument("--expected-generation", type=int, required=True)
    replace_web.add_argument("--expected-ownership-generation", type=int, required=True)
    replace_web.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    replace_web.add_argument("--lease-file", default=str(DEFAULT_MANUAL_WEB_LEASES))

    unbind_web = subparsers.add_parser("unbind-web-session")
    unbind_web.add_argument("--repo", required=True)
    unbind_web.add_argument("--controller-id", required=True)
    unbind_web.add_argument("--web-session-id", required=True)
    unbind_web.add_argument("--expected-generation", type=int, required=True)
    unbind_web.add_argument("--expected-ownership-generation", type=int, required=True)
    unbind_web.add_argument("--registry", default=str(DEFAULT_REGISTRY))

    authorize_manual = subparsers.add_parser("authorize-manual-web-session")
    authorize_manual.add_argument("--repo", required=True)
    authorize_manual.add_argument("--controller-id", required=True)
    authorize_manual.add_argument("--web-session-id", required=True)
    authorize_manual.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    authorize_manual.add_argument("--lease-file", default=str(DEFAULT_MANUAL_WEB_LEASES))
    authorize_manual.add_argument("--ttl-seconds", type=int, default=DEFAULT_MANUAL_WEB_LEASE_TTL_SECONDS)

    resolve_manual = subparsers.add_parser("resolve-manual-web-session")
    resolve_manual.add_argument("--cwd", required=True)
    resolve_manual.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    resolve_manual.add_argument("--lease-file", default=str(DEFAULT_MANUAL_WEB_LEASES))

    session_start = subparsers.add_parser("session-start")
    session_start.add_argument("--repo", required=True)
    session_start.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    session_start.add_argument("--web-session-id")
    session_start.add_argument("--host-identity-receipt-json")

    post_shell = subparsers.add_parser("post-shell")
    post_shell.add_argument("--cwd", required=True)
    post_shell.add_argument("--command", required=True)
    post_shell.add_argument("--exit-code", required=True, type=int)
    post_shell.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    post_shell.add_argument("--web-session-id")
    post_shell.add_argument("--capture-event")

    audit_once = subparsers.add_parser("audit-once")
    audit_once.add_argument("--session-id", required=True)
    audit_once.add_argument("--repo", required=True)
    audit_once.add_argument("--audit-log", default=str(DEFAULT_AUDIT_LOG))
    audit_once.add_argument("--cursor", required=True)
    audit_once.add_argument("--capture-events")
    audit_once.add_argument("--computer-lease")
    audit_once.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    audit_once.add_argument("--web-session-id")
    audit_once.add_argument("--auto-native-stop", action="store_true")
    audit_once.add_argument("--auto-stop-delay-seconds", type=float, default=5.0)
    audit_once.add_argument("--auto-stop-state")
    audit_once.add_argument("--capture-auto-stop")
    audit_once.add_argument("--codex", default="/opt/homebrew/bin/codex")
    audit_once.add_argument("--runtime-path", default=DEFAULT_RUNTIME_PATH)

    arm_computer = subparsers.add_parser("arm-computer")
    arm_computer.add_argument("--cwd", required=True)
    arm_computer.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    arm_computer.add_argument("--web-session-id")
    arm_computer.add_argument("--lease")
    arm_computer.add_argument("--ttl-seconds", type=int, default=90)
    arm_computer.add_argument("--uses", type=int, default=1)

    native_stop = subparsers.add_parser("native-stop")
    native_stop.add_argument("--session-id", required=True)
    native_stop.add_argument("--repo", required=True)
    native_stop.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    native_stop.add_argument("--codex", default="/opt/homebrew/bin/codex")
    native_stop.add_argument("--runtime-path", default=DEFAULT_RUNTIME_PATH)
    native_stop.add_argument("--dry-run", action="store_true")

    auto_stop = subparsers.add_parser("auto-native-stop")
    auto_stop.add_argument("--session-id", required=True)
    auto_stop.add_argument("--repo", required=True)
    auto_stop.add_argument("--receipt-id", required=True)
    auto_stop.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    auto_stop.add_argument("--codex", default="/opt/homebrew/bin/codex")
    auto_stop.add_argument("--runtime-path", default=DEFAULT_RUNTIME_PATH)
    auto_stop.add_argument("--delay-seconds", type=float, default=5.0)
    auto_stop.add_argument("--state")
    auto_stop.add_argument("--supervisor-token")

    subparsers.add_parser("print-zshenv-block")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command_name == "translate-receipt":
        repo = Path(args.repo).expanduser().resolve()
        registry_path = Path(args.registry).expanduser()
        try:
            registered = registered_controller_for_repo(repo, registry_path)
            if registered != args.session_id:
                raise PermissionError("translate-receipt controller does not match registered Controller")
            web_session_id = require_web_controller_session(
                controller_id=registered, web_session_id=args.web_session_id, registry_path=registry_path
            )
            receipt = json.load(sys.stdin)
        except PermissionError as exc:
            print(str(exc), file=sys.stderr)
            return 78
        except json.JSONDecodeError as exc:
            print(f"invalid receipt JSON: {exc}", file=sys.stderr)
            return 2
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        if not isinstance(receipt, dict):
            print("receipt must be a JSON object", file=sys.stderr)
            return 2
        event = translate_receipt(
            receipt, session_id=args.session_id, repo=repo, web_session_id=web_session_id
        )
        if event is not None:
            print(json.dumps(event, ensure_ascii=False))
        return 0

    if args.command_name == "bind-web-session":
        repo = canonical_root(args.repo)
        registry_path = Path(args.registry).expanduser()
        try:
            bind_web_session_to_controller(
                repo=repo, controller_id=args.controller_id, web_session_id=args.web_session_id, registry_path=registry_path
            )
        except PermissionError as exc:
            print(str(exc), file=sys.stderr)
            return 78
        except (OSError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(json.dumps({
            "controller_id": args.controller_id,
            "controller_session_id": args.controller_id,
            "web_session_id": args.web_session_id,
            "event_source": "web",
        }, ensure_ascii=False))
        return 0

    if args.command_name == "replace-web-session":
        repo = canonical_root(args.repo)
        registry_path = Path(args.registry).expanduser()
        lease_path = Path(args.lease_file).expanduser()
        try:
            receipt = replace_web_session(
                repo=repo, controller_id=args.controller_id, web_session_id=args.web_session_id,
                expected_generation=args.expected_generation,
                expected_ownership_generation=args.expected_ownership_generation,
                registry_path=registry_path, lease_path=lease_path,
            )
        except PermissionError as exc:
            print(str(exc), file=sys.stderr)
            return 78
        except (OSError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
        return 0

    if args.command_name == "unbind-web-session":
        repo = canonical_root(args.repo)
        registry_path = Path(args.registry).expanduser()
        try:
            receipt = unbind_web_session(
                repo=repo, controller_id=args.controller_id, web_session_id=args.web_session_id,
                expected_generation=args.expected_generation,
                expected_ownership_generation=args.expected_ownership_generation,
                registry_path=registry_path,
            )
        except PermissionError as exc:
            print(str(exc), file=sys.stderr)
            return 78
        except (OSError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
        return 0

    if args.command_name == "authorize-manual-web-session":
        repo = canonical_root(args.repo)
        registry_path = Path(args.registry).expanduser()
        lease_path = Path(args.lease_file).expanduser()
        try:
            record = authorize_manual_web_session(
                repo=repo,
                controller_id=args.controller_id,
                web_session_id=args.web_session_id,
                registry_path=registry_path,
                lease_path=lease_path,
                ttl_seconds=args.ttl_seconds,
            )
        except PermissionError as exc:
            print(str(exc), file=sys.stderr)
            return 78
        except (OSError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(json.dumps({
            **record,
            "lease_file": str(lease_path),
        }, ensure_ascii=False))
        return 0

    if args.command_name == "resolve-manual-web-session":
        value = resolve_manual_web_session(
            cwd=Path(args.cwd).expanduser(),
            registry_path=Path(args.registry).expanduser(),
            lease_path=Path(args.lease_file).expanduser(),
        )
        if value:
            print(value)
        return 0

    if args.command_name == "session-start":
        repo = canonical_root(args.repo)
        registry_path = Path(args.registry).expanduser()
        host_identity_receipt: dict[str, Any] | None = None
        if args.host_identity_receipt_json:
            try:
                parsed = json.loads(args.host_identity_receipt_json)
            except json.JSONDecodeError as exc:
                print(f"invalid Host identity receipt JSON: {exc}", file=sys.stderr)
                return 2
            if not isinstance(parsed, dict):
                print("Host identity receipt must be a JSON object", file=sys.stderr)
                return 2
            host_identity_receipt = parsed
        try:
            controller_id = registered_controller_for_repo(repo, registry_path)
            if controller_id is None:
                raise ValueError(f"no registered controller for {repo}")
            try:
                web_session_id = require_web_controller_session(
                    controller_id=controller_id,
                    web_session_id=args.web_session_id,
                    registry_path=registry_path,
                )
                recovery = {
                    "result": "ALREADY_VERIFIED",
                    "controller_id": controller_id,
                    "resume_lease_rotated": False,
                }
            except PermissionError:
                recovery = recover_same_controller_web_session(
                    repo=repo,
                    web_session_id=str(args.web_session_id or ""),
                    registry_path=registry_path,
                    host_identity_receipt=host_identity_receipt,
                )
                if recovery.get("result") not in {"RECOVERED", "ALREADY_VERIFIED"}:
                    diagnostic = {
                        "message": (
                            "verified Web Controller Session identity required; "
                            "project Controller ownership remains independent from current session authorization"
                        ),
                        "project_controller_state": recovery["identity"][
                            "project_controller_state"
                        ],
                        "session_binding_state": recovery["identity"][
                            "session_binding_state"
                        ],
                        "controller_actions_allowed": False,
                        "recovery": recovery.get("state")
                        or recovery["identity"]["session_binding_state"].get(
                            "recovery"
                        ),
                    }
                    print(
                        json.dumps(diagnostic, ensure_ascii=False, sort_keys=True),
                        file=sys.stderr,
                    )
                    return 78
                web_session_id = require_web_controller_session(
                    controller_id=controller_id,
                    web_session_id=args.web_session_id,
                    registry_path=registry_path,
                )
            payload = web_session_restore_payload(
                repo, registry_path, web_session_id=web_session_id
            )
        except PermissionError as exc:
            print(str(exc), file=sys.stderr)
            return 78
        except (OSError, ValueError, subprocess.CalledProcessError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        payload["controller_id"] = controller_id
        payload["controller_session_id"] = controller_id
        payload["web_session_id"] = web_session_id
        payload["event_source"] = "web"
        payload["session_recovery_result"] = recovery
        print(json.dumps(payload, ensure_ascii=False))
        return 0

    if args.command_name == "post-shell":
        repo = canonical_root(args.cwd)
        try:
            session_id = registered_controller_for_repo(
                repo, Path(args.registry).expanduser()
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        if session_id is None:
            return 0
        registry_path = Path(args.registry).expanduser()
        try:
            web_session_id = require_web_controller_session(
                controller_id=session_id,
                web_session_id=args.web_session_id,
                registry_path=registry_path,
            )
        except PermissionError as exc:
            print(str(exc), file=sys.stderr)
            return 78
        event = post_tool_event(
            session_id=session_id,
            repo=repo,
            command=args.command,
            exit_code=args.exit_code,
            web_session_id=web_session_id,
            execution_host="web",
        )
        if args.capture_event:
            Path(args.capture_event).write_text(
                json.dumps(event, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            return 0
        dispatch_outcome = dispatch_event_result(event)
        return complete_web_lifecycle_dispatch(
            dispatch_outcome=dispatch_outcome,
            session_id=session_id,
            repo=repo,
            registry=registry_path,
            codex="/opt/homebrew/bin/codex",
            receipt_prefix="post-shell",
        )

    if args.command_name == "audit-once":
        repo = Path(args.repo).expanduser().resolve()
        registry_path = Path(args.registry).expanduser()
        try:
            registered = registered_controller_for_repo(repo, registry_path)
            if registered is None or registered != args.session_id:
                raise PermissionError("audit-once controller does not match registered Controller")
            supplied_web_session_id = str(args.web_session_id or "").strip()
            if not supplied_web_session_id:
                raise PermissionError(
                    "audit-once requires explicit Web session identity; manual resume lease cannot prove caller origin"
                )
            web_session_id = require_web_controller_session(
                controller_id=registered, web_session_id=supplied_web_session_id, registry_path=registry_path
            )
        except PermissionError as exc:
            print(str(exc), file=sys.stderr)
            return 78
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        cursor_path = Path(args.cursor).expanduser()
        consumer_lock_path = cursor_path.with_suffix(cursor_path.suffix + ".consumer.lock")
        consumer_lock_path.parent.mkdir(parents=True, exist_ok=True)
        consumer_lock = consumer_lock_path.open("a+")
        try:
            fcntl.flock(consumer_lock.fileno(), fcntl.LOCK_EX)
            records, audit_inode = audit_records_from_cursor(
                Path(args.audit_log).expanduser(), cursor_path
            )
            receipts = [receipt for receipt, _ in records]
            lease_path = (
                Path(args.computer_lease).expanduser()
                if args.computer_lease
                else default_computer_lease_path(args.session_id)
            )
            for receipt, next_offset in records:
                status = _audit_receipt_status(cursor_path, receipt)
                if status == "handled":
                    _advance_audit_cursor(cursor_path, audit_inode, next_offset)
                    continue
                if status == "pending":
                    print(
                        f"web lifecycle receipt outcome unknown; reconcile before replay: {_audit_receipt_key(receipt)}",
                        file=sys.stderr,
                    )
                    return 3
                if status == "wake_pending":
                    expected_wake_fingerprint = _audit_receipt_wake_fingerprint(cursor_path, receipt)
                    try:
                        lifecycle_state = _load_lifecycle_state(args.session_id)
                        if lifecycle_state.get("pending_control_event") is not True:
                            print(
                                f"web lifecycle wake retry state missing or no longer pending for {_audit_receipt_key(receipt)}",
                                file=sys.stderr,
                            )
                            return 78
                        current_fingerprint = _wake_event_fingerprint(lifecycle_state)
                        if not expected_wake_fingerprint or current_fingerprint != expected_wake_fingerprint:
                            print(
                                f"web lifecycle wake retry generation mismatch for {_audit_receipt_key(receipt)}",
                                file=sys.stderr,
                            )
                            return 78
                        wake_receipt = dispatch_pending_lifecycle_wake(
                            lifecycle_state=lifecycle_state,
                            session_id=args.session_id,
                            repo=repo,
                            registry=Path(args.registry).expanduser(),
                            codex=args.codex,
                            runtime_path=args.runtime_path,
                        )
                        if not wake_receipt_confirmed(wake_receipt):
                            result = wake_receipt.get("result") if isinstance(wake_receipt, dict) else "MISSING_RECEIPT"
                            if args.auto_native_stop and wake_receipt_needs_auto_native_stop(wake_receipt):
                                state_path = Path(args.auto_stop_state).expanduser() if args.auto_stop_state else default_auto_stop_state_path(args.session_id)
                                schedule_auto_native_stop(
                                    session_id=args.session_id, repo=repo,
                                    receipt_id=str(receipt.get("receiptId") or _audit_receipt_key(receipt)),
                                    registry=Path(args.registry).expanduser(), codex=args.codex,
                                    delay_seconds=max(1.0, args.auto_stop_delay_seconds), state_path=state_path,
                                    capture_path=Path(args.capture_auto_stop).expanduser() if args.capture_auto_stop else None,
                                    runtime_path=args.runtime_path,
                                )
                            print(
                                f"web lifecycle wake not confirmed for {_audit_receipt_key(receipt)}: {result}",
                                file=sys.stderr,
                            )
                            return 78
                    except (OSError, ValueError, subprocess.SubprocessError) as exc:
                        print(f"web lifecycle handling failed for {_audit_receipt_key(receipt)}: {exc}", file=sys.stderr)
                        return 3
                    _set_audit_receipt_status(cursor_path, receipt, "handled")
                    _advance_audit_cursor(cursor_path, audit_inode, next_offset)
                    continue

                guard_event = successful_guard_event_from_receipt(
                    receipt, session_id=args.session_id, repo=repo, web_session_id=web_session_id
                )
                event = guard_event
                if event is None:
                    event = computer_event_from_receipt(
                        receipt, session_id=args.session_id, repo=repo, lease_path=lease_path,
                        web_session_id=web_session_id
                    )
                if event is None:
                    _advance_audit_cursor(cursor_path, audit_inode, next_offset)
                    continue
                _set_audit_receipt_status(cursor_path, receipt, "pending")
                try:
                    if args.capture_events:
                        append_captured_event(Path(args.capture_events), event)
                    else:
                        dispatch_code = dispatch_event(event)
                        if dispatch_code != 0:
                            print(
                                f"web lifecycle dispatch failed for {_audit_receipt_key(receipt)}: exit {dispatch_code}",
                                file=sys.stderr,
                            )
                            return dispatch_code
                        lifecycle_state = _load_lifecycle_state(args.session_id)
                        if lifecycle_state.get("pending_control_event") is True:
                            _set_audit_receipt_status(
                                cursor_path,
                                receipt,
                                "wake_pending",
                                wake_fingerprint=_wake_event_fingerprint(lifecycle_state),
                            )
                            wake_receipt = dispatch_pending_lifecycle_wake(
                                lifecycle_state=lifecycle_state,
                                session_id=args.session_id,
                                repo=repo,
                                registry=Path(args.registry).expanduser(),
                                codex=args.codex,
                                runtime_path=args.runtime_path,
                            )
                            if not wake_receipt_confirmed(wake_receipt):
                                result = wake_receipt.get("result") if isinstance(wake_receipt, dict) else "MISSING_RECEIPT"
                                if args.auto_native_stop and wake_receipt_needs_auto_native_stop(wake_receipt):
                                    state_path = Path(args.auto_stop_state).expanduser() if args.auto_stop_state else default_auto_stop_state_path(args.session_id)
                                    schedule_auto_native_stop(
                                        session_id=args.session_id, repo=repo,
                                        receipt_id=str(receipt.get("receiptId") or _audit_receipt_key(receipt)),
                                        registry=Path(args.registry).expanduser(), codex=args.codex,
                                        delay_seconds=max(1.0, args.auto_stop_delay_seconds), state_path=state_path,
                                        capture_path=Path(args.capture_auto_stop).expanduser() if args.capture_auto_stop else None,
                                        runtime_path=args.runtime_path,
                                    )
                                print(
                                    f"web lifecycle wake not confirmed for {_audit_receipt_key(receipt)}: {result}",
                                    file=sys.stderr,
                                )
                                return 78
                    if args.auto_native_stop and guard_event is not None:
                        receipt_id = str(receipt.get("receiptId") or "web-guard")
                        state_path = (
                            Path(args.auto_stop_state).expanduser()
                            if args.auto_stop_state
                            else default_auto_stop_state_path(args.session_id)
                        )
                        schedule_auto_native_stop(
                            session_id=args.session_id,
                            repo=repo,
                            receipt_id=receipt_id,
                            registry=Path(args.registry).expanduser(),
                            codex=args.codex,
                            delay_seconds=max(0.0, args.auto_stop_delay_seconds),
                            state_path=state_path,
                            capture_path=Path(args.capture_auto_stop).expanduser() if args.capture_auto_stop else None,
                            runtime_path=args.runtime_path,
                        )
                except (OSError, ValueError, subprocess.SubprocessError) as exc:
                    print(f"web lifecycle handling failed for {_audit_receipt_key(receipt)}: {exc}", file=sys.stderr)
                    return 3
                _set_audit_receipt_status(cursor_path, receipt, "handled")
                _advance_audit_cursor(cursor_path, audit_inode, next_offset)
            if receipts and args.auto_native_stop:
                lifecycle_state = refresh_rule_wake_state(session_id=args.session_id, repo=repo)
                state_path = (
                    Path(args.auto_stop_state).expanduser()
                    if args.auto_stop_state
                    else default_auto_stop_state_path(args.session_id)
                )
                schedule_guarded_rule_wake(
                    lifecycle_state=lifecycle_state,
                    session_id=args.session_id, repo=repo, registry=Path(args.registry).expanduser(),
                    codex=args.codex, delay_seconds=max(0.0, args.auto_stop_delay_seconds),
                    state_path=state_path,
                    capture_path=Path(args.capture_auto_stop).expanduser() if args.capture_auto_stop else None,
                    runtime_path=args.runtime_path,
                )
            return 0

        finally:
            try:
                fcntl.flock(consumer_lock.fileno(), fcntl.LOCK_UN)
            finally:
                consumer_lock.close()

    if args.command_name == "arm-computer":
        repo = canonical_root(args.cwd)
        registry_path = Path(args.registry).expanduser()
        try:
            session_id = registered_controller_for_repo(repo, registry_path)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        if session_id is None:
            print(f"no registered controller for {repo}", file=sys.stderr)
            return 2
        try:
            web_session_id = require_web_controller_session(
                controller_id=session_id, web_session_id=args.web_session_id, registry_path=registry_path
            )
        except PermissionError as exc:
            print(str(exc), file=sys.stderr)
            return 78
        if not (5 <= args.ttl_seconds <= 300):
            print("ttl-seconds must be between 5 and 300", file=sys.stderr)
            return 2
        if not (1 <= args.uses <= 8):
            print("uses must be between 1 and 8", file=sys.stderr)
            return 2
        lease_path = (
            Path(args.lease).expanduser()
            if args.lease
            else default_computer_lease_path(session_id)
        )
        value = write_computer_lease(
            lease_path=lease_path,
            session_id=session_id,
            web_session_id=web_session_id,
            repo=repo,
            ttl_seconds=args.ttl_seconds,
            uses=args.uses,
        )
        print(json.dumps(value, ensure_ascii=False))
        return 0

    if args.command_name == "native-stop":
        repo = canonical_root(args.repo)
        try:
            registered = registered_controller_for_repo(
                repo, Path(args.registry).expanduser()
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        if registered != args.session_id:
            print(
                f"session {args.session_id} is not the registered controller for {repo}",
                file=sys.stderr,
            )
            return 2
        try:
            target_receipt = resolve_native_resume_target(
                session_id=args.session_id,
                repo=repo,
                registry=Path(args.registry).expanduser(),
            )
        except (OSError, ValueError, PermissionError, subprocess.SubprocessError) as exc:
            print(f"Controller target guard rejected native-stop: {exc}", file=sys.stderr)
            return 78
        command = native_resume_command(
            codex=args.codex,
            session_id=str(target_receipt["execution_target_session_id"]),
            repo=repo,
        )
        if args.dry_run:
            print(json.dumps(command, ensure_ascii=False))
            return 0
        lifecycle_state = _load_lifecycle_state(args.session_id)
        wake_receipt = dispatch_pending_lifecycle_wake(
            lifecycle_state=lifecycle_state,
            session_id=args.session_id,
            repo=repo,
            registry=Path(args.registry).expanduser(),
            codex=args.codex,
            runtime_path=args.runtime_path,
        )
        if wake_receipt is None:
            print("web lifecycle wake supervisor produced no receipt", file=sys.stderr)
            return 78
        return 0 if wake_receipt_confirmed(wake_receipt) else 78

    if args.command_name == "auto-native-stop":
        repo = canonical_root(args.repo)
        state_path = (
            Path(args.state).expanduser()
            if args.state
            else default_auto_stop_state_path(args.session_id)
        )
        return run_auto_native_stop(
            session_id=args.session_id,
            repo=repo,
            receipt_id=args.receipt_id,
            registry=Path(args.registry).expanduser(),
            codex=args.codex,
            delay_seconds=max(0.0, args.delay_seconds),
            state_path=state_path,
            runtime_path=args.runtime_path,
            supervisor_token=args.supervisor_token,
        )

    if args.command_name == "print-zshenv-block":
        print(zshenv_block())
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
