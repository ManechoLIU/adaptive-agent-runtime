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


def registered_controller_for_repo(
    repo: Path, registry_path: Path
) -> tuple[str, dict[str, Any]]:
    repo = repo.expanduser().resolve()
    registry_path = registry_path.expanduser()
    registry = load_json(registry_path)
    requested_common_dir = _git_common_dir(repo)
    matches: list[str] = []
    for controller_id, registered_repo in registry.items():
        if not isinstance(controller_id, str) or not isinstance(registered_repo, str):
            continue
        try:
            if _git_common_dir(Path(registered_repo)) == requested_common_dir:
                matches.append(controller_id)
        except ValueError:
            continue
    if len(matches) != 1:
        raise PermissionError(
            f"expected exactly one registered Controller for {repo}, found {len(matches)}"
        )
    return _bounded_string(
        matches[0], label="controller id", maximum=MAX_CONTROLLER_IDENTIFIER_LENGTH
    ), registry


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
    for command in (resolve, check, reconcile):
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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "resolve":
            receipt = resolve_execution_target(
                repo=Path(args.repo),
                host=args.host,
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
