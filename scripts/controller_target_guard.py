#!/usr/bin/env python3
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Sequence


DEFAULT_REGISTRY = Path.home() / ".codex" / "adaptive-delivery-controllers.json"
CONTROLLER_SESSIONS_KEY = "__controller_sessions__"
CONTROLLER_TARGETS_KEY = "__controller_targets__"
CONTROLLER_EXECUTION_OWNERSHIP_KEY = "__controller_execution_ownership__"
CONTROLLER_OUTBOUND_LEASES_KEY = "__controller_outbound_leases__"
CONTROLLER_OUTBOUND_LEASE_RECONCILIATIONS_KEY = "__controller_outbound_lease_reconciliations__"
MAX_OUTBOUND_LEASE_RECONCILIATIONS = 64
MAX_CONTROLLER_IDENTIFIER_LENGTH = 256
MAX_RECONCILIATION_TEXT_LENGTH = 1024
MAX_ACTIVE_OUTBOUND_LEASES_PER_HOST = 64
DESKTOP_SESSION_HOST = "desktop_codex"
SUPPORTED_HOSTS = (DESKTOP_SESSION_HOST, "web")
SUPPORTED_ACTIONS = ("native_resume", "message", "navigate")
CODEX_APP_TOOL_ACTIONS = {
    "mcp__codex_app__send_message_to_thread": "message",
    "mcp__codex_app__navigate_to_codex_page": "navigate",
    "mcp__codex_app__open_in_codex": "navigate",
}
CODEX_APP_OPTIONAL_TARGET_TOOLS = {"mcp__codex_app__open_in_codex"}


def registry_lock_path(registry_path: Path) -> Path:
    registry_path = registry_path.expanduser()
    return registry_path.with_suffix(registry_path.suffix + ".lock")


@contextmanager
def locked_registry(registry_path: Path, *, exclusive: bool = False) -> Iterator[dict[str, Any]]:
    registry_path = registry_path.expanduser()
    lock_path = registry_lock_path(registry_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield load_json(registry_path)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _bounded_string(value: object, *, label: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{label} is required")
    if len(normalized) > maximum:
        raise ValueError(f"{label} is too long")
    return normalized


COLLABORATION_SPAWN_TOOLS = {"spawn_agent", "collaboration.spawn_agent", "mcp__collaboration__spawn_agent"}


def collaboration_spawn_contract(*, tool_name: object, tool_input: object) -> dict[str, str] | None:
    normalized_tool = str(tool_name or "").strip()
    if normalized_tool not in COLLABORATION_SPAWN_TOOLS:
        return None
    if not isinstance(tool_input, dict):
        raise ValueError(f"{normalized_tool} requires structured tool input")
    return {
        "task_name": _bounded_string(
            tool_input.get("task_name"), label="collaboration spawn task_name", maximum=256
        ),
        "model": _bounded_string(
            tool_input.get("model"), label="collaboration spawn model", maximum=128
        ),
        "agent_type": _bounded_string(
            tool_input.get("agent_type"), label="collaboration spawn agent_type", maximum=128
        ),
    }


def collaboration_spawn_task_name(*, tool_name: object, tool_input: object) -> str | None:
    """Compatibility parser for callers that only need to recognize the task name."""
    normalized_tool = str(tool_name or "").strip()
    if normalized_tool not in COLLABORATION_SPAWN_TOOLS:
        return None
    if not isinstance(tool_input, dict):
        raise ValueError(f"{normalized_tool} requires structured tool input")
    return _bounded_string(
        tool_input.get("task_name"), label="collaboration spawn task_name", maximum=256
    )


def codex_app_outbound_request(
    *, tool_name: object, tool_input: object
) -> tuple[str, str] | None:
    normalized_tool = str(tool_name or "").strip()
    action = CODEX_APP_TOOL_ACTIONS.get(normalized_tool)
    if action is None:
        return None
    if not isinstance(tool_input, dict):
        raise ValueError(f"{normalized_tool} requires structured tool input")
    if (
        normalized_tool in CODEX_APP_OPTIONAL_TARGET_TOOLS
        and "threadId" not in tool_input
        and "thread_id" not in tool_input
    ):
        return None
    target = tool_input.get("threadId", tool_input.get("thread_id"))
    target_session_id = str(target or "").strip()
    if not target_session_id:
        raise ValueError(f"{normalized_tool} requires an explicit thread target")
    return action, target_session_id


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _git_common_dir(repo: Path) -> Path:
    repo = repo.expanduser().resolve()
    completed = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--git-common-dir"],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        raise ValueError(f"cannot resolve Git common-dir for {repo}")
    common_dir = Path(completed.stdout.strip()).expanduser()
    if not common_dir.is_absolute():
        common_dir = repo / common_dir
    return common_dir.resolve()


def _matching_controller_ids_for_repo(
    repo: Path, registry: dict[str, Any]
) -> list[str]:
    repo = repo.expanduser().resolve()
    requested_common_dir = _git_common_dir(repo)
    matches: list[str] = []
    for controller_id, registered_repo in registry.items():
        if (
            not isinstance(controller_id, str)
            or controller_id.startswith("__")
            or not isinstance(registered_repo, str)
        ):
            continue
        try:
            if _git_common_dir(Path(registered_repo)) == requested_common_dir:
                matches.append(
                    _bounded_string(
                        controller_id,
                        label="controller id",
                        maximum=MAX_CONTROLLER_IDENTIFIER_LENGTH,
                    )
                )
        except ValueError:
            continue
    return sorted(set(matches))


def registered_controller_for_repo(
    repo: Path, registry_path: Path
) -> tuple[str, dict[str, Any]]:
    repo = repo.expanduser().resolve()
    registry_path = registry_path.expanduser()
    registry = load_json(registry_path)
    return unique_controller_id_for_repo_in_registry(repo, registry), registry


def unique_controller_id_for_repo_in_registry(
    repo: Path, registry: dict[str, Any]
) -> str:
    """Resolve exactly one logical Controller from an already locked registry."""
    matches = _matching_controller_ids_for_repo(repo, registry)
    if len(matches) != 1:
        raise PermissionError(
            f"expected exactly one registered Controller for {repo}, found {len(matches)}"
        )
    return matches[0]


def _project_controller_state_from_registry(
    repo: Path, registry: dict[str, Any]
) -> dict[str, Any]:
    matches = _matching_controller_ids_for_repo(repo, registry)
    if not matches:
        return {
            "project_controller": "ABSENT",
            "controller_id": None,
            "uniqueness": "NONE",
            "ownership": "NONE",
            "matching_controller_ids": [],
            "create_new_controller_allowed": True,
        }
    if len(matches) > 1:
        return {
            "project_controller": "CONFLICT",
            "controller_id": None,
            "uniqueness": "CONFLICT",
            "ownership": "CONFLICT",
            "matching_controller_ids": matches,
            "create_new_controller_allowed": False,
        }
    controller_id = matches[0]
    return {
        "project_controller": "EXISTING",
        "controller_id": controller_id,
        "uniqueness": "UNIQUE",
        "ownership": "ACTIVE",
        "matching_controller_ids": matches,
        "create_new_controller_allowed": False,
    }


def project_controller_state(
    *, repo: Path, registry_path: Path = DEFAULT_REGISTRY
) -> dict[str, Any]:
    repo = repo.expanduser().resolve()
    registry_path = registry_path.expanduser()
    with locked_registry(registry_path) as registry:
        state = _project_controller_state_from_registry(repo, registry)
    return {
        **state,
        "repo": str(repo),
        "registry": str(registry_path.resolve()),
        "registry_sha256": _registry_sha256(registry_path),
    }


def _session_owners(
    registry: dict[str, Any], *, session_id: str, host: str
) -> set[str]:
    owners: set[str] = set()
    if isinstance(registry.get(session_id), str):
        owners.add(session_id)
    for controller_id, registered_repo in registry.items():
        if (
            not isinstance(controller_id, str)
            or controller_id.startswith("__")
            or not isinstance(registered_repo, str)
        ):
            continue
        if session_id in host_sessions(
            registry, controller_id=controller_id, host=host
        ):
            owners.add(controller_id)
        record = target_record(registry, controller_id=controller_id, host=host)
        if record is not None:
            status, target, _generation = validate_target_record(record, host=host)
            if status == "active" and target == session_id:
                owners.add(controller_id)
    return owners


def controller_identity_capabilities() -> dict[str, Any]:
    """Machine-readable capability contract for the canonical Controller identity layer."""
    return {
        "schema_version": 1,
        "canonical_identity_cli": "controller_target_guard.py identity",
        "capabilities": [
            "controller_identity_projection",
            "same_controller_recovery",
            "web_session_binding",
            "target_generation_fence",
        ],
    }


def controller_identity_projection(
    *,
    repo: Path,
    host: str,
    source_session_id: str | None,
    registry_path: Path = DEFAULT_REGISTRY,
) -> dict[str, Any]:
    """Project durable Controller ownership separately from current session authorization."""
    host = str(host or "").strip()
    if host not in SUPPORTED_HOSTS:
        raise ValueError(f"unsupported controller host: {host}")
    repo = repo.expanduser().resolve()
    registry_path = registry_path.expanduser()
    supplied_session = str(source_session_id or "").strip()

    with locked_registry(registry_path) as registry:
        project = _project_controller_state_from_registry(repo, registry)
        project_controller = str(project.get("controller_id") or "").strip()

        if project["project_controller"] == "CONFLICT":
            binding = {
                "host": host,
                "session_id": supplied_session or None,
                "verification": "CONFLICT",
                "reason": "PROJECT_CONTROLLER_CONFLICT",
                "provenance": "controller_registry",
                "binding_mode": None,
                "target_generation": None,
                "controller_actions_allowed": False,
                "recovery": "CONFLICT_REQUIRES_MANUAL_RESOLUTION",
                "same_controller_recovery_allowed": False,
            }
        elif project["project_controller"] == "ABSENT":
            binding = {
                "host": host,
                "session_id": supplied_session or None,
                "verification": "UNVERIFIED",
                "reason": "NO_PROJECT_CONTROLLER",
                "provenance": "controller_registry",
                "binding_mode": None,
                "target_generation": None,
                "controller_actions_allowed": False,
                "recovery": "NEW_CONTROLLER_REQUIRED",
                "same_controller_recovery_allowed": False,
            }
        elif not supplied_session:
            binding = {
                "host": host,
                "session_id": None,
                "verification": "UNVERIFIED",
                "reason": "HOST_SESSION_ID_UNAVAILABLE",
                "provenance": "controller_registry",
                "binding_mode": None,
                "target_generation": None,
                "controller_actions_allowed": False,
                "recovery": "SAME_CONTROLLER_SESSION_RECOVERY_REQUIRED",
                "same_controller_recovery_allowed": True,
            }
        else:
            owners = _session_owners(
                registry, session_id=supplied_session, host=host
            )
            foreign_owners = {
                owner for owner in owners if owner != project_controller
            }
            if foreign_owners or len(owners) > 1:
                binding = {
                    "host": host,
                    "session_id": supplied_session,
                    "verification": "CONFLICT",
                    "reason": "SESSION_OWNERSHIP_CONFLICT",
                    "provenance": "controller_registry",
                    "binding_mode": None,
                    "target_generation": None,
                    "controller_actions_allowed": False,
                    "recovery": "CONFLICT_REQUIRES_MANUAL_RESOLUTION",
                    "same_controller_recovery_allowed": False,
                    "session_owner_controller_ids": sorted(owners),
                }
            else:
                aliases = host_sessions(
                    registry, controller_id=project_controller, host=host
                )
                record = target_record(
                    registry, controller_id=project_controller, host=host
                )
                if record is None:
                    if host == "web" and (supplied_session in aliases or supplied_session == project_controller):
                        verification = "STALE" if supplied_session in aliases else "UNVERIFIED"
                        reason = "EXPLICIT_CURRENT_TARGET_REQUIRED"
                        mode = "historical_alias" if supplied_session in aliases else None
                        generation = 0
                    elif not aliases and supplied_session == project_controller:
                        verification = "VERIFIED"
                        reason = "LEGACY_CANONICAL_SESSION"
                        mode = "legacy_canonical"
                        generation = 0
                    elif supplied_session in aliases:
                        verification = "STALE"
                        reason = "EXPLICIT_CURRENT_TARGET_REQUIRED"
                        mode = "historical_alias"
                        generation = 0
                    else:
                        verification = "UNVERIFIED"
                        reason = "SESSION_NOT_BOUND_TO_PROJECT_CONTROLLER"
                        mode = None
                        generation = None
                else:
                    status, target, generation = validate_target_record(
                        record, host=host
                    )
                    if status == "active" and target == supplied_session:
                        if (
                            host == "web"
                            and (
                                record.get("host_attested") is False
                                or (
                                    record.get("provenance")
                                    == "host_attested_same_controller_recovery"
                                    and record.get("identity_proof")
                                    != "host_attested_origin"
                                )
                            )
                        ):
                            verification = "UNVERIFIED"
                            reason = (
                                "MANUAL_BOOTSTRAP_NOT_HOST_ATTESTED"
                                if record.get("host_attested") is False
                                else "HOST_IDENTITY_UNAVAILABLE"
                            )
                            mode = str(
                                record.get("binding_mode") or "explicit_current"
                            )
                        else:
                            verification = "VERIFIED"
                            reason = "CURRENT_EXECUTION_TARGET"
                            mode = str(record.get("binding_mode") or "explicit_current")
                    elif supplied_session in aliases or supplied_session == project_controller:
                        verification = "STALE"
                        reason = (
                            "EXECUTION_TARGET_UNBOUND"
                            if status == "unbound"
                            else "SESSION_NOT_CURRENT_TARGET"
                        )
                        mode = "historical_alias"
                    else:
                        verification = "UNVERIFIED"
                        reason = "SESSION_NOT_BOUND_TO_PROJECT_CONTROLLER"
                        mode = None

                verified = verification == "VERIFIED"
                recoverable = verification in {"UNVERIFIED", "STALE"}
                current_record_for_session = (
                    isinstance(record, dict)
                    and str(record.get("status") or "") == "active"
                    and str(record.get("session_id") or "").strip() == supplied_session
                )
                target_provenance = (
                    str(record.get("provenance") or "controller_registry")
                    if current_record_for_session
                    else "controller_registry"
                )
                binding = {
                    "host": host,
                    "session_id": supplied_session,
                    "verification": verification,
                    "reason": reason,
                    "provenance": target_provenance,
                    "binding_mode": mode,
                    "target_generation": generation,
                    "controller_actions_allowed": verified,
                    "recovery": (
                        "NONE"
                        if verified
                        else "SAME_CONTROLLER_SESSION_RECOVERY_REQUIRED"
                        if recoverable
                        else "CONFLICT_REQUIRES_MANUAL_RESOLUTION"
                    ),
                    "same_controller_recovery_allowed": recoverable,
                }
                if current_record_for_session and "host_attested" in record:
                    binding["host_attested"] = bool(record.get("host_attested"))
                if (
                    isinstance(record, dict)
                    and verification == "VERIFIED"
                    and str(record.get("host_identity_receipt_sha256") or "").strip()
                ):
                    binding["host_identity_receipt_sha256"] = str(
                        record["host_identity_receipt_sha256"]
                    )

    project = {
        **project,
        "repo": str(repo),
        "registry": str(registry_path.resolve()),
        "registry_sha256": _registry_sha256(registry_path),
    }
    verification = str(binding.get("verification") or "").strip()
    if verification == "CONFLICT":
        identity_state = "CONFLICTED"
    elif verification == "VERIFIED":
        identity_state = "VERIFIED"
    elif (
        project.get("project_controller") == "EXISTING"
        and project.get("uniqueness") == "UNIQUE"
        and str(binding.get("reason") or "") in {
            "HOST_SESSION_ID_UNAVAILABLE",
            "HOST_IDENTITY_UNAVAILABLE",
        }
    ):
        identity_state = "DEGRADED"
    else:
        identity_state = "UNVERIFIED"

    return {
        "identity_state": identity_state,
        "project_controller_state": project,
        "session_binding_state": binding,
        "controller_actions_allowed": bool(
            binding.get("controller_actions_allowed")
        ),
        "same_controller_recovery_allowed": bool(
            binding.get("same_controller_recovery_allowed")
        ),
        "create_new_controller_allowed": bool(
            project.get("create_new_controller_allowed")
        ),
    }


def host_sessions(
    registry: dict[str, Any], *, controller_id: str, host: str
) -> list[str]:
    sessions = registry.get(CONTROLLER_SESSIONS_KEY)
    if sessions is None:
        return []
    if not isinstance(sessions, dict):
        raise ValueError("controller session registry is invalid")
    controller_sessions = sessions.get(controller_id)
    if controller_sessions is None:
        return []
    if not isinstance(controller_sessions, dict):
        raise ValueError("Controller session map is invalid")
    values = controller_sessions.get(host)
    if isinstance(values, str):
        values = [values]
    elif values is None:
        return []
    elif not isinstance(values, list):
        raise ValueError("Controller host session list is invalid")
    normalized: list[str] = []
    for value in values:
        if not isinstance(value, str):
            raise ValueError("Controller host session list has a non-string entry")
        if value.strip():
            normalized.append(_bounded_string(
                value, label="controller session id", maximum=MAX_CONTROLLER_IDENTIFIER_LENGTH
            ))
    return list(dict.fromkeys(normalized))


def target_record(
    registry: dict[str, Any], *, controller_id: str, host: str
) -> dict[str, Any] | None:
    targets = registry.get(CONTROLLER_TARGETS_KEY)
    if targets is None:
        return None
    if not isinstance(targets, dict):
        raise ValueError("controller target registry is invalid")
    controller_targets = targets.get(controller_id)
    if controller_targets is None:
        return None
    if not isinstance(controller_targets, dict):
        raise ValueError("Controller target map is invalid")
    record = controller_targets.get(host)
    if record is None:
        return None
    if not isinstance(record, dict):
        raise ValueError("Controller target record is invalid")
    return dict(record)


def execution_ownership_record(
    registry: dict[str, Any], *, controller_id: str
) -> dict[str, Any] | None:
    ownership = registry.get(CONTROLLER_EXECUTION_OWNERSHIP_KEY)
    if ownership is None:
        return None
    if not isinstance(ownership, dict):
        raise ValueError("controller execution ownership registry is invalid")
    record = ownership.get(controller_id)
    if record is None:
        return None
    if not isinstance(record, dict):
        raise ValueError("Controller execution ownership record is invalid")
    return dict(record)


def validate_execution_ownership_record(
    record: dict[str, Any]
) -> tuple[str, str, int]:
    active_host = str(record.get("active_host") or "").strip()
    if active_host not in SUPPORTED_HOSTS:
        raise PermissionError("Controller execution ownership active_host is invalid")
    target = _bounded_string(
        record.get("execution_target_session_id"),
        label="Controller execution ownership target session_id",
        maximum=MAX_CONTROLLER_IDENTIFIER_LENGTH,
    )
    generation = record.get("generation")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise PermissionError("Controller execution ownership generation is invalid")
    return active_host, target, generation


def resolve_execution_ownership(
    *, repo: Path, registry_path: Path = DEFAULT_REGISTRY
) -> dict[str, Any]:
    repo = repo.expanduser().resolve()
    registry_path = registry_path.expanduser()
    controller_id, registry = registered_controller_for_repo(repo, registry_path)
    record = execution_ownership_record(registry, controller_id=controller_id)
    if record is None:
        raise PermissionError("Controller execution ownership is not established")
    active_host, target, generation = validate_execution_ownership_record(record)
    host_target = target_record(registry, controller_id=controller_id, host=active_host)
    if host_target is None:
        aliases = host_sessions(registry, controller_id=controller_id, host=active_host)
        if aliases or target != controller_id:
            raise PermissionError("Controller execution ownership target is not current for its host")
    else:
        status, current_target, _host_generation = validate_target_record(
            host_target, host=active_host
        )
        if status != "active" or current_target != target:
            raise PermissionError("Controller execution ownership target is not current for its host")
    return {
        "result": "RESOLVED",
        "controller_id": controller_id,
        "controller_session_id": controller_id,
        "active_host": active_host,
        "host": active_host,
        "execution_target_session_id": target,
        "generation": generation,
        "target_mode": "canonical_host_ownership",
        "repo": str(repo),
        "registry": str(registry_path.resolve()),
        "registry_sha256": _registry_sha256(registry_path),
        **({"provenance": record.get("provenance")} if record.get("provenance") else {}),
    }


def _claim_controller_host_in_registry(
    registry: dict[str, Any],
    *,
    controller_id: str,
    requested_host: str,
    requested_target_session_id: str,
    expected_generation: int,
    provenance: str | None = None,
) -> dict[str, Any]:
    controller_id = _bounded_string(
        controller_id, label="controller id", maximum=MAX_CONTROLLER_IDENTIFIER_LENGTH
    )
    requested_host = str(requested_host or "").strip()
    if requested_host not in SUPPORTED_HOSTS:
        raise ValueError(f"unsupported controller host: {requested_host}")
    requested_target_session_id = _bounded_string(
        requested_target_session_id,
        label="requested target session id",
        maximum=MAX_CONTROLLER_IDENTIFIER_LENGTH,
    )
    if isinstance(expected_generation, bool) or not isinstance(expected_generation, int) or expected_generation < 0:
        raise ValueError("expected ownership generation must be a non-negative integer")
    provenance_value = str(provenance or "").strip() or None

    aliases = host_sessions(registry, controller_id=controller_id, host=requested_host)
    current_target_record = target_record(
        registry, controller_id=controller_id, host=requested_host
    )
    if current_target_record is None:
        if aliases:
            raise PermissionError(
                f"{requested_host} aliases exist without an explicit current target"
            )
        current_target = controller_id
    else:
        status, current_target, _host_generation = validate_target_record(
            current_target_record, host=requested_host
        )
        if status != "active" or current_target is None:
            raise PermissionError(f"{requested_host} execution target is not active")
        if current_target != controller_id and current_target not in aliases:
            raise PermissionError(
                f"{requested_host} execution target is not a bound Controller entry"
            )
    if current_target != requested_target_session_id:
        raise PermissionError(
            f"requested target {requested_target_session_id} is not the current "
            f"{requested_host} target {current_target}"
        )

    prior = execution_ownership_record(registry, controller_id=controller_id)
    if prior is None:
        current_generation = 0
    else:
        _prior_host, _prior_target, current_generation = validate_execution_ownership_record(prior)
    if current_generation != expected_generation:
        raise PermissionError(
            "Controller execution ownership generation changed; stale host claim refused"
        )

    ownership = registry.get(CONTROLLER_EXECUTION_OWNERSHIP_KEY)
    if ownership is None:
        ownership = {}
    if not isinstance(ownership, dict):
        raise ValueError("controller execution ownership registry is invalid")
    next_record: dict[str, Any] = {
        "active_host": requested_host,
        "execution_target_session_id": requested_target_session_id,
        "generation": current_generation + 1,
    }
    if provenance_value is not None:
        next_record["provenance"] = provenance_value
    ownership[controller_id] = next_record
    registry[CONTROLLER_EXECUTION_OWNERSHIP_KEY] = ownership
    return {
        "result": "CLAIMED",
        "controller_id": controller_id,
        "controller_session_id": controller_id,
        "active_host": requested_host,
        "host": requested_host,
        "execution_target_session_id": requested_target_session_id,
        "generation": current_generation + 1,
        "target_mode": "canonical_host_ownership",
        **({"provenance": provenance_value} if provenance_value else {}),
    }


def claim_controller_host(
    *,
    repo: Path,
    controller_id: str,
    requested_host: str,
    requested_target_session_id: str,
    expected_generation: int,
    registry_path: Path = DEFAULT_REGISTRY,
    provenance: str | None = None,
) -> dict[str, Any]:
    repo = repo.expanduser().resolve()
    registry_path = registry_path.expanduser()
    controller_id = _bounded_string(
        controller_id, label="controller id", maximum=MAX_CONTROLLER_IDENTIFIER_LENGTH
    )
    lock_path = registry_lock_path(registry_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            registry = load_json(registry_path)
            matches = _matching_controller_ids_for_repo(repo, registry)
            if matches != [controller_id]:
                raise PermissionError(
                    "Controller host claim requires the existing unique project Controller"
                )
            receipt = _claim_controller_host_in_registry(
                registry,
                controller_id=controller_id,
                requested_host=requested_host,
                requested_target_session_id=requested_target_session_id,
                expected_generation=expected_generation,
                provenance=provenance,
            )
            _write_registry(registry_path, registry)
            return {**receipt, "repo": str(repo)}
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def validate_target_record(
    record: dict[str, Any], *, host: str
) -> tuple[str, str | None, int]:
    status = record.get("status")
    generation = record.get("generation")
    if not isinstance(status, str) or status not in {"active", "unbound"}:
        raise PermissionError(f"{host} execution target status is invalid")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise PermissionError(f"{host} target generation is invalid")
    session_id = record.get("session_id")
    if status == "active":
        try:
            session_id = _bounded_string(
                session_id,
                label=f"{host} active execution target session_id",
                maximum=MAX_CONTROLLER_IDENTIFIER_LENGTH,
            )
        except ValueError as exc:
            raise PermissionError(str(exc)) from exc
        return status, session_id, generation
    if session_id is not None:
        raise PermissionError(f"{host} unbound execution target must not have session_id")
    return status, None, generation


def active_source_controller_id(
    registry: dict[str, Any], *, source_session_id: str, host: str
) -> str | None:
    source_session_id = source_session_id.strip()
    if not source_session_id:
        return None
    owners: set[str] = set()
    if isinstance(registry.get(source_session_id), str):
        owners.add(source_session_id)
    sessions = registry.get(CONTROLLER_SESSIONS_KEY)
    if isinstance(sessions, dict):
        for controller_id, controller_sessions in sessions.items():
            if not isinstance(controller_id, str) or not isinstance(controller_sessions, dict):
                continue
            values = controller_sessions.get(host)
            if isinstance(values, str):
                values = [values]
            if isinstance(values, list) and source_session_id in {
                value for value in values if isinstance(value, str)
            }:
                owners.add(controller_id)
    if len(owners) != 1:
        return None
    controller_id = next(iter(owners))
    if not isinstance(registry.get(controller_id), str):
        return None

    aliases = host_sessions(registry, controller_id=controller_id, host=host)
    record = target_record(registry, controller_id=controller_id, host=host)
    if record is None:
        # A legacy Controller with no aliases has only one possible source. Once
        # aliases exist, list membership cannot prove which entry is current.
        return controller_id if not aliases and source_session_id == controller_id else None
    status, target, _generation = validate_target_record(record, host=host)
    if status != "active":
        return None
    return controller_id if target and source_session_id == target else None


def _registry_sha256(registry_path: Path) -> str:
    try:
        return hashlib.sha256(registry_path.read_bytes()).hexdigest()
    except OSError:
        return ""


def resolve_execution_target(
    *, repo: Path, host: str, registry_path: Path = DEFAULT_REGISTRY
) -> dict[str, Any]:
    host = host.strip()
    if host not in SUPPORTED_HOSTS:
        raise ValueError(f"unsupported controller host: {host}")
    repo = repo.expanduser().resolve()
    registry_path = registry_path.expanduser()
    controller_id, registry = registered_controller_for_repo(repo, registry_path)
    aliases = host_sessions(registry, controller_id=controller_id, host=host)
    record = target_record(registry, controller_id=controller_id, host=host)

    if record is None:
        if aliases:
            raise PermissionError(
                f"{host} aliases exist without an explicit current target; replace or unbind before outbound work"
            )
        target = controller_id
        generation = 0
        target_mode = "legacy_canonical"
    else:
        status, target, generation = validate_target_record(record, host=host)
        if status == "unbound":
            raise PermissionError(f"{host} execution target is explicitly unbound")
        if target is None:
            raise PermissionError(f"{host} active execution target has no session_id")
        if target != controller_id and target not in aliases:
            raise PermissionError(f"{host} execution target is not a bound Controller entry")
        target_mode = "explicit_current"

    return {
        "result": "RESOLVED",
        "controller_id": controller_id,
        "controller_session_id": controller_id,
        "execution_target_session_id": target,
        "host": host,
        "generation": generation,
        "target_mode": target_mode,
        "repo": str(repo),
        "registry": str(registry_path.resolve()),
        "registry_sha256": _registry_sha256(registry_path),
    }


@contextmanager
def locked_execution_target(
    *, repo: Path, host: str, registry_path: Path = DEFAULT_REGISTRY
) -> Iterator[dict[str, Any]]:
    """Hold the registry read fence until an outbound operation has started."""
    registry_path = registry_path.expanduser()
    lock_path = registry_lock_path(registry_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_SH)
        try:
            yield resolve_execution_target(
                repo=repo,
                host=host,
                registry_path=registry_path,
            )
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def check_execution_target(
    *,
    repo: Path,
    host: str,
    action: str,
    target_session_id: str,
    registry_path: Path = DEFAULT_REGISTRY,
) -> dict[str, Any]:
    action = action.strip()
    if action not in SUPPORTED_ACTIONS:
        raise ValueError(f"unsupported outbound action: {action}")
    supplied = target_session_id.strip()
    if not supplied:
        raise ValueError("target-session-id is required")
    receipt = resolve_execution_target(repo=repo, host=host, registry_path=registry_path)
    expected = receipt["execution_target_session_id"]
    if supplied != expected:
        raise PermissionError(
            f"target {supplied} is not the current {host} target {expected}; refusing {action}"
        )
    return {**receipt, "result": "ALLOWED", "action": action}


def _outbound_leases_for_host(
    registry: dict[str, Any], *, controller_id: str, host: str
) -> dict[str, Any]:
    leases = registry.get(CONTROLLER_OUTBOUND_LEASES_KEY)
    if leases is None:
        return {}
    if not isinstance(leases, dict):
        raise ValueError("controller outbound lease registry is invalid")
    controller_leases = leases.get(controller_id)
    if controller_leases is None:
        return {}
    if not isinstance(controller_leases, dict):
        raise ValueError("Controller outbound lease map is invalid")
    host_leases = controller_leases.get(host)
    if host_leases is None:
        return {}
    if not isinstance(host_leases, dict):
        raise ValueError("Controller host outbound lease map is invalid")
    return dict(host_leases)


def _write_registry(registry_path: Path, registry: dict[str, Any]) -> None:
    temporary = registry_path.with_suffix(registry_path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(registry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(registry_path)


def acquire_outbound_lease(
    *,
    repo: Path,
    host: str,
    action: str,
    target_session_id: str,
    tool_use_id: str,
    source_session_id: str | None = None,
    registry_path: Path = DEFAULT_REGISTRY,
) -> dict[str, Any]:
    """Atomically bind one approved outbound dispatch to its exact target generation."""
    lease_id = _bounded_string(
        tool_use_id, label="tool_use_id", maximum=MAX_CONTROLLER_IDENTIFIER_LENGTH
    )
    target_session_id = _bounded_string(
        target_session_id, label="target session id", maximum=MAX_CONTROLLER_IDENTIFIER_LENGTH
    )
    if source_session_id is not None:
        source_session_id = _bounded_string(
            source_session_id, label="source session id", maximum=MAX_CONTROLLER_IDENTIFIER_LENGTH
        )
    registry_path = registry_path.expanduser()
    lock_path = registry_lock_path(registry_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            receipt = check_execution_target(
                repo=repo,
                host=host,
                action=action,
                target_session_id=target_session_id,
                registry_path=registry_path,
            )
            registry = load_json(registry_path)
            controller_id = str(receipt["controller_id"])
            if source_session_id is not None and active_source_controller_id(
                registry, source_session_id=source_session_id, host=host
            ) != controller_id:
                raise PermissionError("source session is not the current Controller target")
            existing = _outbound_leases_for_host(
                registry, controller_id=controller_id, host=host
            ).get(lease_id)
            expected = {
                "action": action,
                "target_session_id": receipt["execution_target_session_id"],
                "generation": receipt["generation"],
            }
            if existing is not None:
                raise PermissionError("tool_use_id already has an active outbound lease")
            if len(_outbound_leases_for_host(
                registry, controller_id=controller_id, host=host
            )) >= MAX_ACTIVE_OUTBOUND_LEASES_PER_HOST:
                raise PermissionError("active outbound lease limit reached for host")
            leases = registry.setdefault(CONTROLLER_OUTBOUND_LEASES_KEY, {})
            if not isinstance(leases, dict):
                raise ValueError("controller outbound lease registry is invalid")
            controller_leases = leases.setdefault(controller_id, {})
            if not isinstance(controller_leases, dict):
                raise ValueError("Controller outbound lease map is invalid")
            host_leases = controller_leases.setdefault(host, {})
            if not isinstance(host_leases, dict):
                raise ValueError("Controller host outbound lease map is invalid")
            host_leases[lease_id] = expected
            _write_registry(registry_path, registry)
            return {**receipt, "tool_use_id": lease_id, "lease": expected}
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def release_outbound_lease(
    *,
    repo: Path,
    host: str,
    tool_use_id: str,
    expected_action: str,
    expected_target_session_id: str,
    registry_path: Path = DEFAULT_REGISTRY,
) -> bool:
    """Release only the persisted lease matching a returned host tool use."""
    lease_id = tool_use_id.strip()
    if not lease_id:
        raise ValueError("tool_use_id is required to release an outbound Controller dispatch")
    action = expected_action.strip()
    target_session_id = expected_target_session_id.strip()
    if not action or not target_session_id:
        raise ValueError("outbound lease release requires action and target session id")
    registry_path = registry_path.expanduser()
    lock_path = registry_lock_path(registry_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            controller_id, registry = registered_controller_for_repo(repo, registry_path)
            host_leases = _outbound_leases_for_host(
                registry, controller_id=controller_id, host=host
            )
            if lease_id not in host_leases:
                return False
            lease = host_leases[lease_id]
            if not isinstance(lease, dict):
                raise ValueError("outbound lease record is invalid")
            generation = lease.get("generation")
            if (
                lease.get("action") != action
                or lease.get("target_session_id") != target_session_id
                or isinstance(generation, bool)
                or not isinstance(generation, int)
            ):
                raise PermissionError("returned outbound action does not match its active lease")
            current = check_execution_target(
                repo=repo,
                host=host,
                action=action,
                target_session_id=target_session_id,
                registry_path=registry_path,
            )
            if current.get("generation") != generation:
                raise PermissionError("returned outbound target generation does not match its active lease")
            leases = registry[CONTROLLER_OUTBOUND_LEASES_KEY]
            controller_leases = leases[controller_id]
            del controller_leases[host][lease_id]
            _write_registry(registry_path, registry)
            return True
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def reconcile_outbound_lease(
    *,
    controller_id: str,
    repo: Path,
    host: str,
    tool_use_id: str,
    action: str,
    target_session_id: str,
    generation: int,
    host_receipt_reference: str,
    reason: str,
    registry_path: Path = DEFAULT_REGISTRY,
) -> dict[str, Any]:
    """Manually reconcile a lease only after the host confirms terminal/interrupted state.

    The receipt reference and reason are operator assertions, not machine proof.
    """
    controller_id = _bounded_string(
        controller_id, label="controller id", maximum=MAX_CONTROLLER_IDENTIFIER_LENGTH
    )
    lease_id = _bounded_string(
        tool_use_id, label="tool_use_id", maximum=MAX_CONTROLLER_IDENTIFIER_LENGTH
    )
    action = _bounded_string(action, label="action", maximum=MAX_CONTROLLER_IDENTIFIER_LENGTH)
    target_session_id = _bounded_string(
        target_session_id, label="target session id", maximum=MAX_CONTROLLER_IDENTIFIER_LENGTH
    )
    receipt_reference = _bounded_string(
        host_receipt_reference, label="host receipt reference", maximum=MAX_RECONCILIATION_TEXT_LENGTH
    )
    reason = _bounded_string(
        reason, label="reason", maximum=MAX_RECONCILIATION_TEXT_LENGTH
    )
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise ValueError("generation must be a positive integer")
    registry_path = registry_path.expanduser()
    lock_path = registry_lock_path(registry_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            registered_controller_id, registry = registered_controller_for_repo(repo, registry_path)
            if registered_controller_id != controller_id:
                raise PermissionError("controller does not own this repository")
            current = check_execution_target(
                repo=repo,
                host=host,
                action=action,
                target_session_id=target_session_id,
                registry_path=registry_path,
            )
            if current.get("generation") != generation:
                raise PermissionError("current target generation does not match reconcile request")
            host_leases = _outbound_leases_for_host(
                registry, controller_id=controller_id, host=host
            )
            lease = host_leases.get(lease_id)
            expected = {
                "action": action,
                "target_session_id": target_session_id,
                "generation": generation,
            }
            if lease != expected:
                raise PermissionError("reconcile request does not match the active lease")
            audits = registry.get(CONTROLLER_OUTBOUND_LEASE_RECONCILIATIONS_KEY)
            if audits is None:
                audits = []
            if not isinstance(audits, list) or not all(isinstance(item, dict) for item in audits):
                raise ValueError("outbound lease reconciliation audit is invalid")
            audit = {
                "controller_id": controller_id,
                "host": host,
                "tool_use_id": lease_id,
                **expected,
                "host_receipt_reference": receipt_reference,
                "reason": reason,
            }
            leases = registry[CONTROLLER_OUTBOUND_LEASES_KEY]
            del leases[controller_id][host][lease_id]
            registry[CONTROLLER_OUTBOUND_LEASE_RECONCILIATIONS_KEY] = (audits + [audit])[
                -MAX_OUTBOUND_LEASE_RECONCILIATIONS:
            ]
            _write_registry(registry_path, registry)
            return {"result": "RECONCILED", **audit}
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def has_active_outbound_lease(
    *, repo: Path, host: str, registry_path: Path = DEFAULT_REGISTRY
) -> bool:
    controller_id, registry = registered_controller_for_repo(repo, registry_path)
    return bool(_outbound_leases_for_host(registry, controller_id=controller_id, host=host))


def require_no_active_outbound_lease(
    registry: dict[str, Any], *, controller_id: str, host: str
) -> None:
    active = _outbound_leases_for_host(
        registry, controller_id=controller_id, host=host
    )
    if active:
        raise PermissionError(
            f"cannot change {host} execution target while active outbound lease exists"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Resolve or validate the one current Controller execution target."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    resolve = subparsers.add_parser("resolve")
    check = subparsers.add_parser("check")
    reconcile = subparsers.add_parser("reconcile")
    identity = subparsers.add_parser("identity")
    subparsers.add_parser("capabilities")
    for command in (resolve, check, reconcile, identity):
        command.add_argument("--repo", required=True)
        command.add_argument("--host", choices=SUPPORTED_HOSTS, required=True)
        command.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    check.add_argument("--action", choices=SUPPORTED_ACTIONS, required=True)
    check.add_argument("--target-session-id", required=True)
    reconcile.add_argument("--controller-id", required=True)
    reconcile.add_argument("--tool-use-id", required=True)
    reconcile.add_argument("--action", choices=SUPPORTED_ACTIONS, required=True)
    reconcile.add_argument("--target-session-id", required=True)
    reconcile.add_argument("--generation", type=int, required=True)
    reconcile.add_argument("--host-receipt-reference", required=True)
    reconcile.add_argument("--reason", required=True)
    identity.add_argument("--session-id")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "capabilities":
            receipt = controller_identity_capabilities()
        elif args.command == "resolve":
            receipt = resolve_execution_target(
                repo=Path(args.repo),
                host=args.host,
                registry_path=Path(args.registry),
            )
        elif args.command == "identity":
            receipt = controller_identity_projection(
                repo=Path(args.repo),
                host=args.host,
                source_session_id=args.session_id,
                registry_path=Path(args.registry),
            )
        elif args.command == "check":
            receipt = check_execution_target(
                repo=Path(args.repo),
                host=args.host,
                action=args.action,
                target_session_id=args.target_session_id,
                registry_path=Path(args.registry),
            )
        else:
            receipt = reconcile_outbound_lease(
                controller_id=args.controller_id,
                repo=Path(args.repo),
                host=args.host,
                tool_use_id=args.tool_use_id,
                action=args.action,
                target_session_id=args.target_session_id,
                generation=args.generation,
                host_receipt_reference=args.host_receipt_reference,
                reason=args.reason,
                registry_path=Path(args.registry),
            )
    except (OSError, ValueError, PermissionError) as exc:
        print(str(exc), file=sys.stderr)
        return 78
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
