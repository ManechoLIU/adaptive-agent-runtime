#!/usr/bin/env python3
"""Ephemeral runtime lease evidence for adaptive-delivery assignments."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

try:
    from project_state import adaptive_delivery_state_dir
except ModuleNotFoundError:
    from scripts.project_state import adaptive_delivery_state_dir

UTC = timezone.utc
EVENTS = {"assignment_started", "assignment_heartbeat", "assignment_progress", "assignment_terminal"}
TERMINAL_STATES = {"completed", "failed", "cancelled", "disconnected"}
OUTCOMES = {"success", "failed", "blocked", "cancelled", "recoverable_failure"}  # legacy read compatibility
TRANSPORT_OUTCOMES = {"completed", "failed", "cancelled", "blocked"}
DELIVERY_OUTCOMES = {"pass", "fail", "blocked", "unresolved"}
TRANSIENT_RETRY_CLASSES = {"rate_limit", "transport_error", "provider_5xx", "capacity", "lease_timeout"}
MAX_ATTEMPTS = 3
IDENTITY_FIELDS = ("assignment_id", "task_id", "agent_id", "provider", "session_id", "worktree")
EVIDENCE_FINGERPRINT_FIELDS = (
    "last_observed_head",
    "last_observed_status_sha256",
    "evidence_receipt_id",
    "artifact_fingerprint",
    "blocker_evidence_fingerprint",
)
GIT_EVIDENCE_FIELDS = ("last_observed_head", "last_observed_status_sha256")
MAX_PROGRESS_EVIDENCE_CHARS = 128
LINEAGE_CONTRACT_FIELDS = ("primary_goal", "success_criteria", "owned_scope", "strategy")
LINEAGE_WHITESPACE_RE = re.compile(r"[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+")
PASS_EVIDENCE_SCHEMES = {"test-log", "green-test", "receipt", "git", "file", "artifact"}
PASS_ARTIFACT_SCHEMES = {"git", "file", "artifact"}
RECONCILIATION_EVIDENCE_SCHEMES = {"receipt", "artifact"}

@dataclass(frozen=True)
class RuntimePolicy:
    heartbeat_ttl_minutes: int = 20
    progress_deadline_minutes: int = 30
    progress_grace_minutes: int = 15
    max_recoveries: int = 2

def _dt(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()

def runtime_state_path(repo: str | Path) -> Path:
    return adaptive_delivery_state_dir(repo) / "runtime-assignments.json"

def _normalized_contract_text(value: str) -> str:
    return LINEAGE_WHITESPACE_RE.sub(" ", str(value)).strip(" ")

def _traceable_locator(value: Any, schemes: set[str]) -> bool:
    if not isinstance(value, str):
        return False
    token = value.strip()
    if ":" not in token:
        return False
    scheme, locator = token.split(":", 1)
    return scheme in schemes and bool(locator.strip())

def _assignment_progress_deadline_minutes(receipt: dict[str, Any], policy: RuntimePolicy) -> int:
    raw = receipt.get("progress_deadline_minutes")
    if raw is None:
        return policy.progress_deadline_minutes
    if not isinstance(raw, int) or isinstance(raw, bool) or raw < 1 or raw > policy.progress_deadline_minutes:
        raise ValueError("progress_deadline_minutes must be a positive integer within the runtime policy bound")
    return raw

def _bounded_progress_scalar(value: Any) -> str | int | bool | None:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return value[:MAX_PROGRESS_EVIDENCE_CHARS]
    return None

def _bounded_progress_evidence(changed_fields: list[str], source: dict[str, Any]) -> dict[str, Any]:
    bounded_fields = [field for field in changed_fields if field in EVIDENCE_FINGERPRINT_FIELDS][:len(EVIDENCE_FINGERPRINT_FIELDS)]
    evidence: dict[str, Any] = {"changed_fields": bounded_fields}
    for field in bounded_fields:
        value = _bounded_progress_scalar(source.get(field))
        if value not in (None, ""):
            evidence[field] = value
    return evidence

def _project_progress_phase(*, event: str, changed_fields: list[str], current: str | None) -> str:
    if event == "assignment_started":
        return "STARTED"
    if event == "assignment_terminal":
        return "DELIVERY"
    if event == "assignment_progress" and any(field in GIT_EVIDENCE_FIELDS for field in changed_fields):
        return "WORKTREE_CHANGED"
    return current if current in {"STARTED", "WORKTREE_CHANGED", "DELIVERY"} else "STARTED"

def execution_lineage_id(*, task_id: str, primary_goal: str, success_criteria: list[str], owned_scope: list[str], strategy: str) -> str:
    canonical = json.dumps({
        "task_id": _normalized_contract_text(task_id),
        "primary_goal": _normalized_contract_text(primary_goal),
        "success_criteria": sorted(_normalized_contract_text(item) for item in success_criteria),
        "owned_scope": sorted(_normalized_contract_text(item) for item in owned_scope),
        "strategy": _normalized_contract_text(strategy),
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

def load_runtime_state(repo: str | Path) -> dict[str, Any]:
    path = runtime_state_path(repo)
    if not path.exists():
        return {"schema_version": 2, "leases": {}, "lineages": {}}
    return json.loads(path.read_text(encoding="utf-8"))

def save_runtime_state(repo: str | Path, state: dict[str, Any]) -> None:
    path = runtime_state_path(repo); path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload); handle.flush(); os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)

def apply_receipt(state: dict[str, Any], receipt: dict[str, Any], now: datetime | None = None, policy: RuntimePolicy | None = None) -> dict[str, Any]:
    policy = policy or RuntimePolicy(); now = now or datetime.now(UTC)
    event = receipt.get("event_type")
    if event not in EVENTS: raise ValueError(f"unsupported runtime event: {event}")
    missing = [f for f in IDENTITY_FIELDS if not receipt.get(f)]
    if missing: raise ValueError("runtime receipt missing identity: " + ", ".join(missing))
    issued = _dt(receipt.get("issued_at") or now)
    out = json.loads(json.dumps(state or {"schema_version": 2, "leases": {}, "lineages": {}})); out.setdefault("schema_version", 2); leases = out.setdefault("leases", {}); lineages = out.setdefault("lineages", {})
    aid = receipt["assignment_id"]; existing = leases.get(aid)
    attempt = int(receipt.get("attempt", 1)); lease_id = str(receipt.get("lease_id") or f"{aid}:attempt:{attempt}"); event_seq = int(receipt.get("event_seq", 1))
    if attempt < 1 or event_seq < 1: raise ValueError("attempt and event_seq must be positive")
    if existing:
        mismatches = [f for f in IDENTITY_FIELDS if existing.get(f) != receipt.get(f)]
        if mismatches: raise ValueError("runtime receipt identity mismatch: " + ", ".join(mismatches))
        current_attempt = int(existing.get("attempt", 1))
        if attempt < current_attempt: raise ValueError("stale runtime attempt")
        if attempt == current_attempt and existing.get("terminal_state"):
            raise ValueError("terminal attempt is immutable; create a new recovery attempt only when policy allows")
        if attempt == current_attempt and lease_id != existing.get("lease_id"): raise ValueError("runtime lease_id mismatch")
        if attempt == current_attempt and event_seq <= int(existing.get("last_event_seq", 0)): raise ValueError("runtime event_seq must increase")
    if event == "assignment_started":
        contract_version_raw = receipt.get("assignment_contract_version", 1)
        if not isinstance(contract_version_raw, int) or isinstance(contract_version_raw, bool) or contract_version_raw < 1:
            raise ValueError("assignment_contract_version must be a positive integer")
        contract_version = int(contract_version_raw)
        if contract_version >= 2:
            if "side_effect" not in receipt or not isinstance(receipt.get("side_effect"), bool):
                raise ValueError("runtime start receipt requires explicit side_effect contract")
            side_effect = receipt["side_effect"]
            idempotency_key_raw = receipt.get("idempotency_key")
            if idempotency_key_raw is not None and not isinstance(idempotency_key_raw, str):
                raise ValueError("idempotency_key must be a string when provided")
            idempotency_key = str(idempotency_key_raw or "").strip() or None
        else:
            # Legacy v1 receipts predate the side-effect contract. Keep an already-issued
            # initial receipt readable, but record the contract as unknown rather than
            # silently asserting that it has no external side effects.
            side_effect = receipt.get("side_effect") if isinstance(receipt.get("side_effect"), bool) else None
            idempotency_key = None
        missing_contract = [field for field in LINEAGE_CONTRACT_FIELDS if field not in receipt]
        if missing_contract:
            raise ValueError("runtime start receipt missing lineage contract: " + ", ".join(missing_contract))
        lineage_id = execution_lineage_id(
            task_id=receipt["task_id"], primary_goal=receipt["primary_goal"],
            success_criteria=receipt["success_criteria"], owned_scope=receipt["owned_scope"], strategy=receipt["strategy"],
        )
        if receipt.get("execution_lineage_id") not in (None, lineage_id):
            raise ValueError("runtime receipt execution lineage mismatch")
        if existing and existing.get("execution_lineage_id") not in (None, lineage_id):
            raise ValueError("runtime receipt execution lineage mismatch")
        if not existing and attempt != 1:
            raise ValueError("new Assignment must start at attempt 1")
        if existing:
            successful_terminal = (
                existing.get("terminal_state") == "completed"
                and (existing.get("delivery_outcome") == "pass" or existing.get("outcome") == "success")
            )
            if successful_terminal:
                raise ValueError("completed assignment cannot be recovered; create a new Assignment for new work")
            current_attempt = int(existing.get("attempt", 1))
            if attempt != current_attempt + 1:
                raise ValueError("recovery attempt must increment by exactly one")
            if int(existing.get("recovery_count", 0)) >= policy.max_recoveries:
                raise ValueError("recovery budget exhausted; strategy change requires a new execution lineage")
            existing_contract_version = int(existing.get("side_effect_contract_version", 1))
            if existing_contract_version != contract_version:
                raise ValueError("side-effect contract version drift requires a new Assignment")
            if existing_contract_version >= 2:
                existing_key = str(existing.get("idempotency_key") or "").strip() or None
                if existing.get("side_effect") != side_effect or existing_key != idempotency_key:
                    raise ValueError("side-effect contract drift requires a new Assignment")
                if bool(existing.get("side_effect")) and bool(existing.get("result_unknown")):
                    raise ValueError("unknown side effect requires reconciliation before recovery")
            elif bool(existing.get("result_unknown")):
                raise ValueError("legacy unknown side-effect result requires a new Assignment before recovery")
        lineage = lineages.get(lineage_id)
        if lineage is None and existing:
            legacy_recoveries = int(existing.get("recovery_count", 0))
            lineage = {
                "execution_lineage_id": lineage_id,
                "execution_count": legacy_recoveries + 1,
                "recovery_count": legacy_recoveries,
            }
        if lineage and int(lineage.get("recovery_count", 0)) >= policy.max_recoveries:
            raise ValueError("recovery budget exhausted; strategy change requires a new execution lineage")
        previous_recovery_count = int(lineage.get("recovery_count", 0)) if lineage else 0
        recovery_count = previous_recovery_count + 1 if lineage else 0
        lineages[lineage_id] = {
            "execution_lineage_id": lineage_id,
            "execution_count": int(lineage.get("execution_count", previous_recovery_count + 1)) + 1 if lineage else 1,
            "recovery_count": recovery_count,
        }
        deadline_minutes = _assignment_progress_deadline_minutes(receipt, policy)
        lease = {f: receipt[f] for f in IDENTITY_FIELDS}
        lease.update({
            "schema_version": 1,
            "execution_lineage_id": lineage_id,
            "attempt": attempt,
            "lease_id": lease_id,
            "last_event_seq": event_seq,
            "pid": receipt.get("pid"),
            "baseline_head": receipt.get("baseline_head"),
            "started_at": _iso(issued),
            "last_heartbeat_at": _iso(issued),
            "last_progress_at": _iso(issued),
            "last_observed_head": receipt.get("last_observed_head") or receipt.get("baseline_head"),
            "last_observed_status_sha256": receipt.get("last_observed_status_sha256"),
            "evidence_receipt_id": receipt.get("evidence_receipt_id"),
            "artifact_fingerprint": receipt.get("artifact_fingerprint"),
            "blocker_evidence_fingerprint": receipt.get("blocker_evidence_fingerprint"),
            "recovery_count": recovery_count,
            "side_effect_contract_version": contract_version,
            "side_effect": side_effect,
            "idempotency_key": idempotency_key,
            # A side-effecting attempt is unknown while it is in flight. Only a durable
            # terminal receipt may prove and clear the outcome. This keeps abrupt death
            # or terminal-receipt persistence failure from becoming an unsafe retry.
            "result_unknown": bool(side_effect) if contract_version >= 2 else True,
            "terminal_state": None,
            "terminal_at": None,
            "lease_expires_at": _iso(issued + timedelta(minutes=policy.heartbeat_ttl_minutes)),
            "progress_deadline_minutes": deadline_minutes,
            "progress_deadline_at": _iso(issued + timedelta(minutes=deadline_minutes)),
            "last_progress_phase": "STARTED",
            "runtime_receipt_id": receipt.get("receipt_id"),
        })
        start_changed = [field for field in EVIDENCE_FINGERPRINT_FIELDS if lease.get(field) not in (None, "")]
        lease["last_progress_evidence"] = _bounded_progress_evidence(start_changed, lease)
        leases[aid] = lease; return out
    if not existing: raise ValueError("runtime receipt has no started lease")
    lease = existing
    lease["last_event_seq"] = event_seq
    if event in {"assignment_heartbeat", "assignment_progress"}:
        lease["last_heartbeat_at"] = _iso(issued); lease["lease_expires_at"] = _iso(issued + timedelta(minutes=policy.heartbeat_ttl_minutes))
    if event == "assignment_progress":
        changed_fields: list[str] = []
        for field in EVIDENCE_FINGERPRINT_FIELDS:
            if field not in receipt:
                continue
            value = receipt.get(field)
            if value in (None, ""):
                continue
            if lease.get(field) != value:
                changed_fields.append(field)
                lease[field] = value
        if changed_fields:
            deadline_minutes = int(lease.get("progress_deadline_minutes") or policy.progress_deadline_minutes)
            lease["last_progress_at"] = _iso(issued)
            lease["progress_deadline_at"] = _iso(issued + timedelta(minutes=deadline_minutes))
            lease["last_progress_phase"] = _project_progress_phase(
                event="assignment_progress",
                changed_fields=changed_fields,
                current=lease.get("last_progress_phase"),
            )
            lease["last_progress_evidence"] = _bounded_progress_evidence(changed_fields, lease)
    if event == "assignment_terminal":
        existing_contract_version = int(existing.get("side_effect_contract_version", 1))
        if existing_contract_version >= 2:
            if "side_effect" not in receipt or not isinstance(receipt.get("side_effect"), bool):
                raise ValueError("terminal receipt requires side_effect contract")
            terminal_key_raw = receipt.get("idempotency_key")
            if terminal_key_raw is not None and not isinstance(terminal_key_raw, str):
                raise ValueError("idempotency_key must be a string when provided")
            terminal_key = str(terminal_key_raw or "").strip() or None
            existing_key = str(existing.get("idempotency_key") or "").strip() or None
            if receipt.get("side_effect") != existing.get("side_effect") or terminal_key != existing_key:
                raise ValueError("side-effect contract drift requires a new Assignment")
        terminal = receipt.get("terminal_state")
        if terminal not in TERMINAL_STATES: raise ValueError(f"invalid terminal state: {terminal}")
        summary = str(receipt.get("summary", "")).strip()
        if not summary: raise ValueError("terminal receipt requires structured outcome and summary")
        if not isinstance(receipt.get("evidence"), list) or not isinstance(receipt.get("artifacts"), list): raise ValueError("terminal receipt requires evidence and artifacts lists")
        if not str(receipt.get("next_action", "")).strip() or not str(receipt.get("retry_class", "")).strip(): raise ValueError("terminal receipt requires next_action and retry_class")
        transport_outcome = receipt.get("transport_outcome")
        delivery_outcome = receipt.get("delivery_outcome")
        legacy_outcome = receipt.get("outcome")
        if transport_outcome is None and delivery_outcome is None:
            if legacy_outcome not in OUTCOMES:
                raise ValueError("terminal receipt requires structured outcome and summary")
        else:
            if transport_outcome not in TRANSPORT_OUTCOMES or delivery_outcome not in DELIVERY_OUTCOMES:
                raise ValueError("terminal receipt requires valid transport_outcome and delivery_outcome")
            if terminal == "completed" and transport_outcome != "completed":
                raise ValueError("completed terminal state requires completed transport outcome")
            if terminal == "failed" and transport_outcome != "failed":
                raise ValueError("failed terminal state requires failed transport outcome")
            if delivery_outcome == "pass":
                if transport_outcome != "completed":
                    raise ValueError("delivery PASS requires completed transport")
                if not receipt["evidence"] or not receipt["artifacts"]:
                    raise ValueError("delivery PASS requires evidence and artifact")
                if not all(_traceable_locator(item, PASS_EVIDENCE_SCHEMES) for item in receipt["evidence"]) or not all(_traceable_locator(item, PASS_ARTIFACT_SCHEMES) for item in receipt["artifacts"]):
                    raise ValueError("delivery PASS requires traceable evidence and artifact")
        if existing_contract_version >= 2:
            if lease.get("side_effect"):
                if "result_unknown" not in receipt:
                    raise ValueError("side-effect terminal receipt requires explicit result_unknown")
                if not isinstance(receipt.get("result_unknown"), bool):
                    raise ValueError("result_unknown must be a boolean")
            elif "result_unknown" in receipt and not isinstance(receipt.get("result_unknown"), bool):
                raise ValueError("result_unknown must be a boolean")
            result_unknown = receipt.get("result_unknown", False)
            reconciliation_evidence = receipt.get("reconciliation_evidence", [])
            if lease.get("side_effect") and not result_unknown:
                if not isinstance(reconciliation_evidence, list) or not reconciliation_evidence:
                    raise ValueError("clearing side-effect result_unknown requires provider reconciliation evidence")
                stable_key = str(existing.get("idempotency_key") or "").strip() or None
                expected_provider = str(existing.get("provider") or "").strip()
                for item in reconciliation_evidence:
                    if not isinstance(item, dict):
                        raise ValueError("provider reconciliation evidence must be structured objects")
                    provider = str(item.get("provider") or "").strip()
                    resource = str(item.get("resource") or "").strip()
                    locator = item.get("locator")
                    evidence_key = item.get("idempotency_key")
                    if provider != expected_provider:
                        raise ValueError("provider reconciliation evidence provider mismatch")
                    if not resource:
                        raise ValueError("provider reconciliation evidence requires concrete resource identity")
                    if not _traceable_locator(locator, RECONCILIATION_EVIDENCE_SCHEMES):
                        raise ValueError("provider reconciliation evidence must use receipt/artifact locators")
                    if stable_key is not None and evidence_key != stable_key:
                        raise ValueError("provider reconciliation evidence must bind the exact idempotency key")
                    if stable_key is None and evidence_key not in (None, ""):
                        raise ValueError("provider reconciliation evidence cannot introduce an idempotency key")
            elif reconciliation_evidence and not isinstance(reconciliation_evidence, list):
                raise ValueError("reconciliation_evidence must be a list")
        else:
            if "result_unknown" in receipt and not isinstance(receipt.get("result_unknown"), bool):
                raise ValueError("result_unknown must be a boolean")
            if "result_unknown" in receipt:
                result_unknown = receipt["result_unknown"]
            elif transport_outcome is not None:
                result_unknown = transport_outcome != "completed" or delivery_outcome == "unresolved"
            else:
                result_unknown = legacy_outcome != "success"
        lease["result_unknown"] = result_unknown
        if existing_contract_version >= 2 and receipt.get("reconciliation_evidence"):
            lease["reconciliation_evidence"] = list(receipt["reconciliation_evidence"])
        lease["terminal_state"] = terminal; lease["terminal_at"] = _iso(issued); lease["summary"] = summary
        if transport_outcome is None and delivery_outcome is None:
            lease["outcome"] = legacy_outcome
        else:
            lease.pop("outcome", None)
            lease["transport_outcome"] = transport_outcome
            lease["delivery_outcome"] = delivery_outcome
        lease["evidence"] = receipt["evidence"]; lease["artifacts"] = receipt["artifacts"]; lease["next_action"] = receipt["next_action"]; lease["retry_class"] = receipt["retry_class"]
        lease["last_progress_phase"] = "DELIVERY"
    lease["runtime_receipt_id"] = receipt.get("receipt_id") or lease.get("runtime_receipt_id")
    return out

def evaluate_lease(lease: dict[str, Any] | None, now: datetime | None = None, policy: RuntimePolicy | None = None, process_probe: Callable[[int], bool] | None = None) -> dict[str, Any]:
    policy = policy or RuntimePolicy(); now = now or datetime.now(UTC)
    if not lease: return {"state": "unknown", "reason": "missing_runtime_lease"}
    recovery_count = int(lease.get("recovery_count", 0))
    terminal = lease.get("terminal_state")
    if terminal:
        if terminal != "completed" and recovery_count >= policy.max_recoveries:
            return {"state": "budget_exhausted", "reason": "recovery_budget_exhausted"}
        return {"state": "terminal", "reason": f"terminal:{terminal}"}
    pid = lease.get("pid")
    if pid is not None and process_probe is not None and not process_probe(int(pid)):
        if recovery_count >= policy.max_recoveries:
            return {"state": "budget_exhausted", "reason": "recovery_budget_exhausted"}
        return {"state": "unhealthy", "reason": "process_not_alive"}
    expires = _dt(lease["lease_expires_at"])
    if now > expires:
        if recovery_count >= policy.max_recoveries:
            return {"state": "budget_exhausted", "reason": "recovery_budget_exhausted"}
        return {"state": "unhealthy", "reason": "lease_expired"}
    deadline = _dt(lease["progress_deadline_at"])
    if now > deadline + timedelta(minutes=policy.progress_grace_minutes):
        if recovery_count >= policy.max_recoveries:
            return {"state": "budget_exhausted", "reason": "recovery_budget_exhausted"}
        return {"state": "unhealthy", "reason": "progress_stale_beyond_grace"}
    if now > deadline: return {"state": "progress_stale", "reason": "progress_deadline_exceeded"}
    return {"state": "healthy", "reason": "runtime_evidence_current"}


def retry_decision(
    retry_class: str,
    attempt: int,
    base_delay_seconds: int = 5,
    *,
    side_effect: bool = False,
    result_unknown: bool = False,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    stable_key = str(idempotency_key or "").strip() or None
    if side_effect and result_unknown:
        return {"retry": False, "reason": "unknown_side_effect_requires_reconciliation"}
    retry = retry_class in TRANSIENT_RETRY_CLASSES and attempt < MAX_ATTEMPTS
    if not retry:
        return {"retry": False, "reason": "non_retryable" if retry_class not in TRANSIENT_RETRY_CLASSES else "attempt_budget_exhausted"}
    # Deterministic envelope; caller adds random jitter within this range.
    delay = base_delay_seconds * (2 ** max(0, attempt - 1))
    decision = {"retry": True, "next_attempt": attempt + 1, "backoff_seconds": delay, "jitter_max_seconds": max(1, delay // 2)}
    if stable_key is not None:
        decision["idempotency_key"] = stable_key
    return decision

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply Adaptive Delivery runtime receipts to canonical Git state.")
    sub = parser.add_subparsers(dest="command", required=True)
    apply_parser = sub.add_parser("apply")
    apply_parser.add_argument("--repo", required=True)
    args = parser.parse_args(argv)
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("runtime receipt must be a JSON object")
        lock_path = adaptive_delivery_state_dir(args.repo) / "runtime-assignments.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            state = load_runtime_state(args.repo)
            updated = apply_receipt(state, payload)
            save_runtime_state(args.repo, updated)
        print(json.dumps({"allowed": True, "assignment_id": payload.get("assignment_id"), "attempt": payload.get("attempt", 1)}, sort_keys=True))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"assignment-runtime: blocked: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
