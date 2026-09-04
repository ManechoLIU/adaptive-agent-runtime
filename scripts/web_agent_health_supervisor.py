#!/usr/bin/env python3
"""KeepAlive scheduler for Runtime-managed Web Assignment health.

This service does not implement a second wake/reentry path. It writes a local readiness
heartbeat and periodically invokes the existing Web lifecycle reconciliation entrypoint,
which in turn hands terminal/health continuation to terminal_continuation and the existing
same-Controller Web reentry supervisor.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

try:
    from scripts import web_lifecycle_bridge
except ModuleNotFoundError:
    import web_lifecycle_bridge

UTC = timezone.utc
DEFAULT_REGISTRY = Path.home() / ".codex" / "adaptive-delivery-controllers.json"
DEFAULT_HEARTBEAT = (
    Path.home() / ".codex" / "state" / "adaptive-delivery-web-agent-health" / "heartbeat.json"
)
HEARTBEAT_MAX_AGE_SECONDS = 90.0


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
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


def write_health_heartbeat(
    *, path: str | Path = DEFAULT_HEARTBEAT, now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    payload = {
        "schema_version": 1,
        "state": "ready",
        "observed_at": _iso(now),
        "pid": os.getpid(),
    }
    _atomic_json(Path(path).expanduser(), payload)
    return payload


def health_supervisor_ready(
    *, path: str | Path = DEFAULT_HEARTBEAT, now: datetime | None = None,
    max_age_seconds: float = HEARTBEAT_MAX_AGE_SECONDS,
) -> bool:
    now = now or datetime.now(UTC)
    try:
        payload = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
        observed = datetime.fromisoformat(
            str(payload.get("observed_at") or "").replace("Z", "+00:00")
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=UTC)
    age = (now.astimezone(UTC) - observed.astimezone(UTC)).total_seconds()
    return payload.get("state") == "ready" and 0 <= age <= max_age_seconds


def reconcile_web_agent_health_once(
    *,
    repo: str | Path,
    registry_path: str | Path,
    controller_id: str,
    event_paths: Iterable[str | Path] | None = None,
    now: datetime | None = None,
    terminal_consumer: Callable[..., dict[str, Any]] | None = None,
    runtime_change_consumer: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    result = web_lifecycle_bridge.reconcile_managed_web_assignments(
        repo=Path(repo).expanduser().resolve(),
        controller_id=controller_id,
        registry=Path(registry_path).expanduser().resolve(),
        event_paths=None if event_paths is None else list(event_paths),
        now=now,
        terminal_consumer=terminal_consumer,
        runtime_change_consumer=runtime_change_consumer,
    )
    # Compatibility aliases for the health-service tests/callers; canonical semantics live
    # in web_lifecycle_bridge.reconcile_managed_web_assignments.
    health = []
    for item in result.get("health", []):
        if isinstance(item, dict):
            health.append({"controller_id": controller_id, **item})
    return {
        **result,
        "health": health,
        "terminal_handoffs": result.get("terminal_continuations", []),
    }


def reconcile_all_web_agent_health_once(
    *,
    registry_path: str | Path = DEFAULT_REGISTRY,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    registry = Path(registry_path).expanduser().resolve()
    try:
        data = json.loads(registry.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    results: list[dict[str, Any]] = []
    for controller_id, raw_repo in data.items():
        if (
            not isinstance(controller_id, str)
            or controller_id.startswith("__")
            or not isinstance(raw_repo, str)
            or not raw_repo.strip()
        ):
            continue
        repo = Path(raw_repo).expanduser().resolve()
        try:
            if (
                web_lifecycle_bridge._registered_controller_for_common_dir(repo, registry)
                != controller_id
            ):
                continue
            results.append(reconcile_web_agent_health_once(
                repo=repo,
                registry_path=registry,
                controller_id=controller_id,
                event_paths=None,
                now=now,
            ))
        except (OSError, ValueError, PermissionError, RuntimeError):
            # Canonical state remains durable; a later KeepAlive cycle retries.
            continue
    return results


def run_health_supervisor(
    *,
    registry_path: str | Path = DEFAULT_REGISTRY,
    poll_seconds: float = 15.0,
    heartbeat_path: str | Path = DEFAULT_HEARTBEAT,
) -> None:
    if poll_seconds < 1.0:
        raise ValueError("Web Agent health supervisor poll interval must be at least one second")
    while True:
        now = datetime.now(UTC)
        write_health_heartbeat(path=heartbeat_path, now=now)
        reconcile_all_web_agent_health_once(registry_path=registry_path, now=now)
        write_health_heartbeat(path=heartbeat_path, now=datetime.now(UTC))
        time.sleep(poll_seconds)


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(
        description="KeepAlive scheduler for Runtime-managed Web Assignment health"
    )
    parser.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    parser.add_argument("--heartbeat", default=str(DEFAULT_HEARTBEAT))
    parser.add_argument("--poll-seconds", type=float, default=15.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if args.once:
        now = datetime.now(UTC)
        write_health_heartbeat(path=args.heartbeat, now=now)
        print(json.dumps(
            reconcile_all_web_agent_health_once(registry_path=args.registry, now=now),
            ensure_ascii=False,
        ))
        return 0
    run_health_supervisor(
        registry_path=args.registry,
        poll_seconds=args.poll_seconds,
        heartbeat_path=args.heartbeat,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
