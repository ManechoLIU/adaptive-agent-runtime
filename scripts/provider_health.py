"""Provider-generic external route health projection and circuit-breaker facts.

The projection is derived from canonical terminal Runtime evidence.  It does not
create a second scheduler or retry policy and it never changes provider/auth
routes on its own.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    from scripts.assignment_runtime import runtime_state_path
    from scripts.project_model_score import classify_attribution, load_project_samples
except ModuleNotFoundError:  # direct script execution from scripts/
    from assignment_runtime import runtime_state_path
    from project_model_score import classify_attribution, load_project_samples

UTC = timezone.utc
_RECENT_WINDOW = 5

_ROUTE_HEALTH_FAILURE_MARKERS = (
    "provider_timeout",
    "provider_start_timeout",
    "cli_launch_timeout",
    "first_output_timeout",
    "first_token_timeout",
    "generation_stalled",
    "heartbeat_timeout",
    "process_group_cleanup_failed",
    "review_process_stuck",
    "review_timeout",
    "provider_unavailable",
    "service_unavailable",
    "cli_unavailable",
    "transport_failure",
    "provider_exit",
    "auth_invalid",
    "authentication_failed",
    "credential_revoked",
    "quota",
    "usage_limit",
    "rate_limit",
    "insufficient_balance",
    "insufficient_credit",
    "runtime_unavailable",
    "bridge_unavailable",
    "host_unavailable",
)


def _dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _route_key(sample: dict[str, Any]) -> tuple[str, str, str, str]:
    identity = sample.get("identity") if isinstance(sample.get("identity"), dict) else {}
    return tuple(
        str(identity.get(field) or "unknown")
        for field in ("provider", "model", "auth_mode", "execution_transport")
    )


def _route_dict(key: tuple[str, str, str, str]) -> dict[str, str]:
    provider, model, auth_mode, execution_transport = key
    return {
        "provider": provider,
        "model": model,
        "auth_mode": auth_mode,
        "execution_transport": execution_transport,
    }


def _is_route_success(sample: dict[str, Any]) -> bool:
    return (
        str(sample.get("transport_outcome") or "").lower() == "completed"
        and not bool(sample.get("result_unknown"))
    )


def _is_health_failure(sample: dict[str, Any]) -> bool:
    if bool(sample.get("result_unknown")):
        return True
    attribution, _ = classify_attribution(sample)
    if attribution not in {"infrastructure", "external"}:
        return False
    failure = str(sample.get("failure_class") or "").lower()
    retry = str(sample.get("retry_class") or "").lower()
    outcome = str(sample.get("outcome_code") or "").lower()
    combined = " ".join((failure, retry, outcome))
    # Circuit-breaking is intentionally narrower than generic infrastructure
    # attribution. Local contract/packet validation and delivery-evidence
    # validation can fail while the provider route itself is perfectly healthy.
    return any(marker in combined for marker in _ROUTE_HEALTH_FAILURE_MARKERS)


def _failure_class(sample: dict[str, Any]) -> str:
    return str(sample.get("failure_class") or sample.get("retry_class") or classify_attribution(sample)[0])


def _consecutive_failures(recent: list[dict[str, Any]]) -> int:
    count = 0
    for sample in reversed(recent):
        if _is_health_failure(sample):
            count += 1
        else:
            break
    return count


def _would_open(recent: list[dict[str, Any]]) -> bool:
    failures = sum(1 for sample in recent if _is_health_failure(sample))
    consecutive = _consecutive_failures(recent)
    cleanup_uncertain = any(
        str(sample.get("failure_class") or "").lower() == "process_group_cleanup_failed"
        or sample.get("cleanup_confirmed") is False
        for sample in recent
        if _is_health_failure(sample)
    )
    return cleanup_uncertain or failures >= 3 or consecutive >= 2


def derive_route_health(
    samples: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    cooldown_seconds: int = 1800,
) -> list[dict[str, Any]]:
    if cooldown_seconds < 0:
        raise ValueError("cooldown_seconds must be >= 0")
    current = now or datetime.now(tz=UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    current = current.astimezone(UTC)

    grouped: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    for sample in samples:
        if not isinstance(sample, dict):
            continue
        terminal_at = _dt(sample.get("terminal_at"))
        if terminal_at is None:
            continue
        grouped.setdefault(_route_key(sample), []).append(sample)

    rows: list[dict[str, Any]] = []
    for key, route_samples in grouped.items():
        route_samples.sort(key=lambda item: (_dt(item.get("terminal_at")) or datetime.min.replace(tzinfo=UTC), str(item.get("assignment_id") or "")))
        recent = route_samples[-_RECENT_WINDOW:]

        # A successful real task after an already-open history and after the
        # cooldown acts as the allowed probe and resets the route to HEALTHY.
        recovered_by_probe = False
        if recent and _is_route_success(recent[-1]) and len(recent) > 1:
            prefix = recent[:-1]
            last_failure_times = [
                _dt(item.get("terminal_at")) for item in prefix if _is_health_failure(item)
            ]
            last_failure_times = [value for value in last_failure_times if value is not None]
            latest_success_at = _dt(recent[-1].get("terminal_at"))
            if _would_open(prefix) and last_failure_times and latest_success_at is not None:
                recovered_by_probe = latest_success_at >= max(last_failure_times) + timedelta(seconds=cooldown_seconds)

        effective_recent = [recent[-1]] if recovered_by_probe else recent
        recent_failures = sum(1 for sample in effective_recent if _is_health_failure(sample))
        consecutive = _consecutive_failures(effective_recent)
        result_unknown_blockers = sum(1 for sample in effective_recent if bool(sample.get("result_unknown")))
        cleanup_uncertain = any(
            (str(sample.get("failure_class") or "").lower() == "process_group_cleanup_failed" or sample.get("cleanup_confirmed") is False)
            for sample in effective_recent
            if _is_health_failure(sample)
        )

        failure_samples = [sample for sample in effective_recent if _is_health_failure(sample)]
        last_failure = failure_samples[-1] if failure_samples else None
        last_failure_at = _dt(last_failure.get("terminal_at")) if last_failure else None
        cooldown_until = last_failure_at + timedelta(seconds=cooldown_seconds) if last_failure_at else None

        state = "HEALTHY"
        opened = cleanup_uncertain or recent_failures >= 3 or consecutive >= 2
        if opened:
            if cooldown_until is not None and current >= cooldown_until:
                state = "PROBE_REQUIRED"
            else:
                state = "OPEN"
        elif recent_failures >= 2:
            state = "DEGRADED"

        block_reason = None
        dispatch_allowed = state in {"HEALTHY", "DEGRADED", "PROBE_REQUIRED"}
        if state == "OPEN":
            block_reason = "circuit_open"

        rows.append(
            {
                "route": _route_dict(key),
                "state": state,
                "eligible_attempts": len(effective_recent),
                "window_size": _RECENT_WINDOW,
                "recent_failures": recent_failures,
                "consecutive_failures": consecutive,
                "last_failure_class": _failure_class(last_failure) if last_failure else None,
                "opened_at": last_failure_at.isoformat() if opened and last_failure_at else None,
                "cooldown_until": cooldown_until.isoformat() if opened and cooldown_until else None,
                "probe_eligible": state == "PROBE_REQUIRED",
                "result_unknown_blockers": result_unknown_blockers,
                "dispatch_allowed": dispatch_allowed,
                "block_reason": block_reason,
                "recovered_by_probe": recovered_by_probe,
            }
        )

    rows.sort(key=lambda item: tuple(item["route"].values()))
    return rows


def _default_health_row(route: dict[str, Any]) -> dict[str, Any]:
    return {
        "route": {
            "provider": str(route.get("provider") or "unknown"),
            "model": str(route.get("model") or "unknown"),
            "auth_mode": str(route.get("auth_mode") or "unknown"),
            "execution_transport": str(route.get("execution_transport") or "external_process"),
        },
        "state": "HEALTHY",
        "eligible_attempts": 0,
        "window_size": _RECENT_WINDOW,
        "recent_failures": 0,
        "consecutive_failures": 0,
        "last_failure_class": None,
        "opened_at": None,
        "cooldown_until": None,
        "probe_eligible": False,
        "result_unknown_blockers": 0,
        "dispatch_allowed": True,
        "block_reason": None,
        "recovered_by_probe": False,
    }


def route_health_for_assignment(
    repo: Path | str,
    route: dict[str, Any],
    *,
    now: datetime | None = None,
    cooldown_seconds: int = 1800,
) -> dict[str, Any]:
    samples = load_project_samples(Path(repo), window_days=None, now=now)
    rows = derive_route_health(samples, now=now, cooldown_seconds=cooldown_seconds)
    expected = (
        str(route.get("provider") or "unknown"),
        str(route.get("model") or "unknown"),
        str(route.get("auth_mode") or "unknown"),
        str(route.get("execution_transport") or "external_process"),
    )
    for row in rows:
        current = row["route"]
        actual = (
            str(current.get("provider") or "unknown"),
            str(current.get("model") or "unknown"),
            str(current.get("auth_mode") or "unknown"),
            str(current.get("execution_transport") or "external_process"),
        )
        if actual == expected:
            return row
    return _default_health_row(route)


def _evidence_hash(samples: list[dict[str, Any]]) -> str:
    relevant = []
    for sample in samples:
        identity = sample.get("identity") if isinstance(sample.get("identity"), dict) else {}
        relevant.append({
            "assignment_id": sample.get("assignment_id"),
            "terminal_at": sample.get("terminal_at"),
            "provider": identity.get("provider"),
            "model": identity.get("model"),
            "auth_mode": identity.get("auth_mode"),
            "execution_transport": identity.get("execution_transport"),
            "terminal_state": sample.get("terminal_state"),
            "transport_outcome": sample.get("transport_outcome"),
            "delivery_outcome": sample.get("delivery_outcome"),
            "failure_class": sample.get("failure_class"),
            "outcome_code": sample.get("outcome_code"),
            "retry_class": sample.get("retry_class"),
            "result_unknown": sample.get("result_unknown"),
            "provider_started": sample.get("provider_started"),
            "cleanup_confirmed": sample.get("cleanup_confirmed"),
        })
    canonical = json.dumps(relevant, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_status_snapshot(
    repo: Path | str,
    *,
    now: datetime | None = None,
    cooldown_seconds: int = 1800,
) -> dict[str, Any]:
    repo_path = Path(repo).resolve()
    samples = load_project_samples(repo_path, window_days=None, now=now)
    current = now or datetime.now(tz=UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    current = current.astimezone(UTC)
    return {
        "schema_version": 1,
        "repo": str(repo_path),
        "generated_at": current.isoformat(),
        "input_evidence_hash": _evidence_hash(samples),
        "cooldown_seconds": cooldown_seconds,
        "routes": derive_route_health(samples, now=current, cooldown_seconds=cooldown_seconds),
    }


def _atomic_write(path: Path, text: str) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _parse_now(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = _dt(value)
    if parsed is None:
        raise ValueError("--now must be ISO-8601")
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Derive external provider route health from canonical Runtime evidence.")
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status")
    status.add_argument("--repo", required=True)
    status.add_argument("--cooldown-seconds", type=int, default=1800)
    status.add_argument("--write-cache", action="store_true")
    status.add_argument("--now", help=argparse.SUPPRESS)

    gate = sub.add_parser("gate")
    gate.add_argument("--repo", required=True)
    gate.add_argument("--provider", required=True)
    gate.add_argument("--model", required=True)
    gate.add_argument("--auth-mode", required=True)
    gate.add_argument("--execution-transport", default="external_process")
    gate.add_argument("--cooldown-seconds", type=int, default=1800)
    gate.add_argument("--now", help=argparse.SUPPRESS)

    args = parser.parse_args(argv)
    now = _parse_now(args.now)
    if args.command == "status":
        snapshot = build_status_snapshot(args.repo, now=now, cooldown_seconds=args.cooldown_seconds)
        rendered = json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if args.write_cache:
            cache = runtime_state_path(args.repo).parent / "provider-health" / "latest.json"
            _atomic_write(cache, rendered)
        print(rendered, end="")
        return 0

    route = {
        "provider": args.provider,
        "model": args.model,
        "auth_mode": args.auth_mode,
        "execution_transport": args.execution_transport,
    }
    row = route_health_for_assignment(args.repo, route, now=now, cooldown_seconds=args.cooldown_seconds)
    print(json.dumps(row, ensure_ascii=False, sort_keys=True))
    return 0 if row["dispatch_allowed"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
