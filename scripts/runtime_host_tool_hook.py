#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Sequence

try:
    import controller_target_guard as target_guard
    import web_lifecycle_bridge as web_bridge
    import lifecycle_hook as lifecycle
    import control_event_guard
except ModuleNotFoundError:
    from scripts import controller_target_guard as target_guard
    from scripts import web_lifecycle_bridge as web_bridge
    from scripts import lifecycle_hook as lifecycle
    from scripts import control_event_guard


HOOK_PROTOCOL = "runtime_host_tool_hook_v1"
MAX_FRAME_BYTES = 64 * 1024
IO_TIMEOUT_SECONDS = 5.0
MAX_REQUEST_ID_BYTES = 256
MAX_SNAPSHOT_BYTES = 64 * 1024
MAX_EVIDENCE_BYTES = 64 * 1024
MAX_LEDGER_BYTES = 128 * 1024
INTENT_KEYS = {
    "schema_version", "provenance", "request_digest_profile", "normalized_request_utf8",
}
PRE_REQUEST_KEYS = {
    "request_id", "protocol", "op", "bridge_call_id", "host_tool_execution_id",
    "pre_receipt", "tool_intent",
}
TERMINAL_REQUEST_KEYS = {
    "request_id", "protocol", "op", "bridge_call_id", "host_tool_execution_id",
    "terminal_receipt", "tool_intent", "backend_http_status", "response_body_sha256",
    "backend_response",
}


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _sha256_hex(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _bounded_text(value: object, *, label: str, maximum: int = 256) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    if not value or value != value.strip() or len(value.encode("utf-8")) > maximum:
        raise ValueError(f"{label} is missing, padded, or too long")
    return value


def _safe_request_id(value: object) -> str:
    """Accept only bounded printable Unicode scalar values for response correlation."""
    request_id = _bounded_text(
        value, label="Runtime Host hook request_id", maximum=MAX_REQUEST_ID_BYTES
    )
    try:
        request_id.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise ValueError("Runtime Host hook request_id is not valid UTF-8 scalar text") from exc
    if any(ord(character) < 0x20 or 0x7F <= ord(character) <= 0x9F for character in request_id):
        raise ValueError("Runtime Host hook request_id contains unsafe control characters")
    return request_id


def _safe_request_id_or_none(value: object) -> str | None:
    try:
        return _safe_request_id(value)
    except (ValueError, UnicodeError):
        return None


def _safe_error_text(error: BaseException | object) -> str:
    """Return a bounded printable-ASCII error without reflecting malformed text."""
    text = str(error).replace("\n", " ").replace("\r", " ")
    return "".join(character if 0x20 <= ord(character) <= 0x7E else "?" for character in text)[:2048]


def _safe_response_bytes(response: dict[str, Any]) -> bytes:
    """Serialize protocol responses as ASCII so malformed request text cannot poison a listener."""
    return json.dumps(
        response, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("ascii") + b"\n"


def _remaining_seconds(deadline_monotonic: float | None) -> float | None:
    if deadline_monotonic is None:
        return None
    remaining = deadline_monotonic - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Runtime Host hook request exceeded deadline")
    return remaining


def _read_trusted_regular_file(
    path: Path, *, label: str, maximum: int,
    deadline_monotonic: float | None = None,
) -> bytes:
    """Read a private regular file through one O_NOFOLLOW descriptor and bounded bytes."""
    _remaining_seconds(deadline_monotonic)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except PermissionError:
        raise
    except OSError as exc:
        raise PermissionError(f"{label} is unavailable") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise PermissionError(f"{label} must be an owned regular file")
        if hasattr(os, "geteuid") and before.st_uid != os.geteuid():
            raise PermissionError(f"{label} must be an owned regular file")
        if stat.S_IMODE(before.st_mode) & 0o022:
            raise PermissionError(f"{label} permissions must not allow group or other writes")
        if before.st_size > maximum:
            raise PermissionError(f"{label} exceeds byte limit")
        data = bytearray()
        while len(data) <= maximum:
            _remaining_seconds(deadline_monotonic)
            chunk = os.read(descriptor, min(8192, maximum + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > maximum:
            raise PermissionError(f"{label} exceeds byte limit")
        after = os.fstat(descriptor)
        _remaining_seconds(deadline_monotonic)
        if (before.st_dev, before.st_ino, before.st_size) != (after.st_dev, after.st_ino, after.st_size):
            raise PermissionError(f"{label} changed while being read")
        return bytes(data)
    except PermissionError:
        raise
    except OSError as exc:
        raise PermissionError(f"{label} is unavailable") from exc
    finally:
        os.close(descriptor)


def _sha256_field(value: object, *, label: str) -> str:
    text = _bounded_text(value, label=label, maximum=64).lower()
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise ValueError(f"{label} must be a SHA-256 digest")
    return text


def _strict_json(text: str, *, label: str) -> Any:
    def reject_constant(value: str) -> None:
        raise ValueError(f"{label} contains unsupported numeric value {value}")

    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"{label} contains duplicate key {key}")
            result[key] = value
        return result

    try:
        return json.loads(text, object_pairs_hook=object_pairs, parse_constant=reject_constant)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} is not strict JSON") from exc


def _runtime_context(
    receipt: dict[str, Any], *, registry_path: Path, lifecycle_path: Path | None,
    deadline_monotonic: float | None = None,
) -> dict[str, Any]:
    conversation_id = _bounded_text(
        receipt.get("conversation_id"), label="Host receipt conversation id"
    )
    registry_path = Path(registry_path).expanduser()
    with target_guard.locked_registry(
        registry_path, deadline_monotonic=deadline_monotonic
    ) as registry:
        controller_id = target_guard.active_source_controller_id(
            registry, source_session_id=conversation_id, host="web"
        )
        if not controller_id:
            raise PermissionError("Host receipt conversation is not the current Web target")
        registered_repo = registry.get(controller_id)
        if not isinstance(registered_repo, str) or not registered_repo.strip():
            raise PermissionError("Host receipt Controller repository is unavailable")
        repo = Path(registered_repo).expanduser().resolve()
        if target_guard.unique_controller_id_for_repo_in_registry(repo, registry) != controller_id:
            raise PermissionError("Host receipt does not belong to the unique project Controller")
        target = target_guard.target_record(registry, controller_id=controller_id, host="web")
        if target is None:
            raise PermissionError("current Web target is unavailable")
        target_status, target_session, target_generation = target_guard.validate_target_record(
            target, host="web"
        )
        ownership = target_guard.execution_ownership_record(registry, controller_id=controller_id)
        if ownership is None:
            raise PermissionError("current Web execution ownership is unavailable")
        ownership_host, ownership_session, ownership_generation = (
            target_guard.validate_execution_ownership_record(ownership)
        )
        if (
            target_status != "active" or target_session != conversation_id
            or ownership_host != "web" or ownership_session != conversation_id
        ):
            raise PermissionError("Host receipt target or ownership is stale")
        resolved_lifecycle = (
            Path(lifecycle_path).expanduser()
            if lifecycle_path is not None else lifecycle.state_path(controller_id)
        )
        _remaining_seconds(deadline_monotonic)
        lifecycle_state = lifecycle.load_json(resolved_lifecycle)
        _remaining_seconds(deadline_monotonic)
        lease = lifecycle_state.get("web_turn_lease")
        if not isinstance(lease, dict) or lease.get("contract") != "runtime_web_turn_lease_v1" or lease.get("status") != "active":
            raise PermissionError("active Runtime Web turn lease is unavailable")
        if (
            lease.get("execution_target_session_id") != conversation_id
            or lease.get("target_generation") != target_generation
            or lease.get("ownership_generation") != ownership_generation
            or lifecycle_state.get("active_turn_id") != lease.get("turn_id")
        ):
            raise PermissionError("Runtime Web turn lease is stale")
        identity = target_guard.agent_target.logical_agent_identity(
            agent_type="controller", agent_id=controller_id
        )
        verified_target = target_guard.agent_target.verified_execution_target(
            logical_agent=identity, host="web",
            execution_target_session_id=conversation_id,
            target_generation=target_generation,
            ownership_generation=ownership_generation,
            provenance="runtime_host_tool_receipt_v1",
        )
        verified_turn = target_guard.agent_target.verified_execution_turn(
            verified_target=verified_target,
            runtime_invocation_id=lease.get("runtime_invocation_id"),
            provenance="runtime_host_tool_receipt_v1",
        )
        if verified_turn["turn_id"] != lease.get("turn_id"):
            raise PermissionError("Runtime Web turn identity is invalid")
    ledger = repo / "TASK_LEDGER.md"
    _read_trusted_regular_file(
        ledger, label="canonical TASK_LEDGER.md", maximum=MAX_LEDGER_BYTES,
        deadline_monotonic=deadline_monotonic,
    )
    return {
        "controller_id": controller_id, "repo": repo, "ledger": ledger.resolve(),
        "target_generation": target_generation,
        "ownership_generation": ownership_generation,
        "verified_turn": verified_turn, "lifecycle_path": resolved_lifecycle,
    }


def _verified_intent(
    value: object, *, receipt: dict[str, Any], context: dict[str, Any],
    deadline_monotonic: float | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != INTENT_KEYS:
        raise PermissionError("Runtime Host tool intent envelope is invalid")
    if (
        value.get("schema_version") != 1
        or value.get("provenance") != "runtime_host_tool_intent_v1"
        or value.get("request_digest_profile") != "lab_run_command_digest_v1"
    ):
        raise PermissionError("Runtime Host tool intent provenance is invalid")
    raw = value.get("normalized_request_utf8")
    if not isinstance(raw, str) or not raw or len(raw.encode("utf-8")) > MAX_FRAME_BYTES:
        raise PermissionError("Runtime Host normalized request is missing or too large")
    if _sha256_hex(raw.encode("utf-8")) != _sha256_field(
        receipt.get("normalized_request_sha256"), label="Host normalized request digest"
    ):
        raise PermissionError("Runtime Host normalized request digest mismatch")
    parsed = _strict_json(raw, label="Runtime Host normalized request")
    if not isinstance(parsed, dict) or set(parsed) != {"arguments", "tool_name"}:
        raise PermissionError("Runtime Host normalized request schema is invalid")
    if _canonical_json_bytes(parsed).decode("utf-8") != raw:
        raise PermissionError("Runtime Host normalized request is not canonical")
    if parsed.get("tool_name") != "run_command":
        raise PermissionError("Runtime Host normalized request is not run_command")
    arguments = parsed.get("arguments")
    if not isinstance(arguments, dict) or set(arguments) not in (
        {"command", "cwd"}, {"command", "cwd", "timeout"},
    ):
        raise PermissionError("Runtime Host run_command arguments are not exact")
    cwd = Path(_bounded_text(arguments.get("cwd"), label="run_command cwd", maximum=4096)).expanduser()
    if not cwd.is_absolute() or cwd.resolve() != context["repo"]:
        raise PermissionError("Runtime Host run_command cwd does not match canonical repository")
    timeout = arguments.get("timeout")
    if timeout is not None and (
        isinstance(timeout, bool) or not isinstance(timeout, int) or timeout < 1 or timeout > 3600
    ):
        raise PermissionError("Runtime Host run_command timeout is invalid")
    command = _bounded_text(arguments.get("command"), label="run_command command", maximum=16384)
    if any(character in command for character in "\n\r;&|><#$`()"):
        raise PermissionError("Runtime Host control guard command contains shell syntax")
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise PermissionError("Runtime Host control guard argv is invalid") from exc
    if len(argv) != 9:
        raise PermissionError("Runtime Host control guard argv is not exact")
    expected_python = Path(sys.executable).resolve()
    declared_python = Path(argv[0]).expanduser()
    if not declared_python.is_absolute() or declared_python.resolve() != expected_python:
        raise PermissionError("Runtime Host control guard Python is not the Runtime interpreter")
    expected_guard = Path(__file__).resolve().with_name("control_event_guard.py")
    declared_guard = Path(argv[1]).expanduser()
    if not declared_guard.is_absolute() or declared_guard.resolve() != expected_guard:
        raise PermissionError("Runtime Host control guard executable is not canonical")
    snapshot_path = Path(argv[2]).expanduser()
    if not snapshot_path.is_absolute():
        raise PermissionError("Runtime Host control snapshot path must be absolute")
    expected_tail = [
        "--ledger", str(context["ledger"]), "--repo", str(context["repo"]),
        "--controller-session", context["controller_id"],
    ]
    if argv[3:] != expected_tail:
        raise PermissionError("Runtime Host control guard ledger/repo/Controller argv is not canonical")
    snapshot_raw = _read_trusted_regular_file(
        snapshot_path, label="Runtime Host control snapshot", maximum=MAX_SNAPSHOT_BYTES,
        deadline_monotonic=deadline_monotonic,
    )
    snapshot = _strict_json(snapshot_raw.decode("utf-8"), label="Runtime Host control snapshot")
    if not isinstance(snapshot, dict):
        raise PermissionError("Runtime Host control snapshot is invalid")
    event_contract = snapshot.get("event_contract")
    event_id = (
        _bounded_text(event_contract.get("event_id"), label="control event id")
        if isinstance(event_contract, dict) else ""
    )
    return {
        "command": command, "cwd": str(context["repo"]), "snapshot_path": snapshot_path,
        "snapshot": snapshot, "snapshot_raw": snapshot_raw, "event_id": event_id,
    }


def _verify_host_result(
    result: object, *, phase: str, receipt: dict[str, Any], context: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise PermissionError("registered Host verifier returned no tool receipt result")
    receipt_sha256 = _sha256_hex(_canonical_json_bytes(receipt))
    if (
        result.get("ok") is not True
        or result.get("protocol") != "runtime_host_verifier_cli_v2"
        or result.get("operation") != f"verify_tool_{phase}"
        or result.get("receipt_sha256") != receipt_sha256
        or result.get("receipt_id") != receipt.get(f"{phase}_receipt_id")
        or result.get("capability_id") != receipt.get("capability_id")
        or result.get("host_tool_execution_id") != receipt.get("host_tool_execution_id")
    ):
        raise PermissionError("registered Host verifier returned a mismatched tool receipt")
    current = result.get("current_entry")
    if not isinstance(current, dict):
        raise PermissionError("registered Host verifier returned no current entry")
    for name in (
        "conversation_id", "browser_target_id", "top_frame_id", "loader_id",
        "generation_anchor_sha256", "secure_origin",
    ):
        if current.get(name) != receipt.get(name):
            raise PermissionError(f"registered Host verifier current entry {name} mismatch")
    if (
        current.get("target_generation") != context["target_generation"]
        or current.get("ownership_generation") != context["ownership_generation"]
        or current.get("runtime_invocation_id") != context["verified_turn"]["runtime_invocation_id"]
    ):
        raise PermissionError("registered Host verifier current entry Runtime fence mismatch")
    return result


def _verified_backend(
    request: dict[str, Any], *, receipt: dict[str, Any], intent: dict[str, Any],
) -> None:
    if request.get("backend_http_status") != receipt.get("http_status") or receipt.get("http_status") != 200:
        raise PermissionError("Runtime Host backend HTTP status does not match receipt")
    response_digest = _sha256_field(
        receipt.get("response_body_sha256"), label="Host backend response digest"
    )
    if _sha256_field(request.get("response_body_sha256"), label="Bridge backend response digest") != response_digest:
        raise PermissionError("Bridge backend response digest does not match Host receipt")
    envelope = request.get("backend_response")
    if not isinstance(envelope, dict) or set(envelope) != {
        "schema_version", "provenance", "response_body_utf8",
    }:
        raise PermissionError("Runtime Host backend response envelope is invalid")
    if envelope.get("schema_version") != 1 or envelope.get("provenance") != "runtime_host_backend_response_v1":
        raise PermissionError("Runtime Host backend response provenance is invalid")
    raw = envelope.get("response_body_utf8")
    if not isinstance(raw, str) or not raw or len(raw.encode("utf-8")) > MAX_FRAME_BYTES:
        raise PermissionError("Runtime Host backend response is missing or too large")
    if _sha256_hex(raw.encode("utf-8")) != response_digest:
        raise PermissionError("Runtime Host backend response bytes do not match Host digest")
    value = _strict_json(raw, label="Runtime Host backend response")
    if not isinstance(value, dict) or set(value) != {"jsonrpc", "id", "result"}:
        raise PermissionError("Runtime Host backend response is not one successful JSON-RPC result")
    if value.get("jsonrpc") != "2.0" or value.get("id") != receipt.get("bridge_call_id"):
        raise PermissionError("Runtime Host backend JSON-RPC identity is invalid")
    result = value.get("result")
    if not isinstance(result, dict) or result.get("isError") is not False:
        raise PermissionError("Runtime Host backend result is an MCP error")
    structured = result.get("structuredContent")
    if not isinstance(structured, dict):
        raise PermissionError("Runtime Host backend structuredContent is missing")
    exit_code = structured.get("exitCode")
    if (
        structured.get("toolName") != "run_command"
        or structured.get("success") is not True
        or isinstance(exit_code, bool) or not isinstance(exit_code, int) or exit_code != 0
        or structured.get("timedOut") is not False
        or structured.get("command") != intent["command"]
        or structured.get("cwd") != intent["cwd"]
    ):
        raise PermissionError("Runtime Host backend structured result is not exact success")


def _guard_evidence(
    *, context: dict[str, Any], intent: dict[str, Any], terminal: dict[str, Any],
    pending: dict[str, Any], deadline_monotonic: float | None = None,
) -> tuple[dict[str, Any], str]:
    snapshot = intent["snapshot"]
    _remaining_seconds(deadline_monotonic)
    ledger_raw = _read_trusted_regular_file(
        context["ledger"], label="canonical TASK_LEDGER.md", maximum=MAX_LEDGER_BYTES,
        deadline_monotonic=deadline_monotonic,
    )
    ledger_sha = _sha256_hex(ledger_raw)
    try:
        remaining = _remaining_seconds(deadline_monotonic)
        head = subprocess.run(
            ["git", "-C", str(context["repo"]), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True, timeout=remaining,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise PermissionError("Runtime Host repository revision is unavailable") from exc
    if snapshot.get("ledger_sha256") not in (None, ledger_sha):
        raise PermissionError("Runtime Host control snapshot ledger drifted")
    if snapshot.get("head") not in (None, head):
        raise PermissionError("Runtime Host control snapshot revision drifted")
    evidence_path = control_event_guard.controller_cycle_evidence_path(
        context["repo"], intent["event_id"]
    )
    _remaining_seconds(deadline_monotonic)
    evidence_raw = _read_trusted_regular_file(
        evidence_path, label="controller guard evidence", maximum=MAX_EVIDENCE_BYTES,
        deadline_monotonic=deadline_monotonic,
    )
    evidence = _strict_json(evidence_raw.decode("utf-8"), label="controller guard evidence")
    snapshot_sha = _sha256_hex(intent["snapshot_raw"])
    if (
        not isinstance(evidence, dict)
        or evidence.get("record_kind") != "controller_cycle_evidence"
        or evidence.get("evidence_id") != intent["event_id"]
        or evidence.get("cycle_id") != intent["event_id"]
        or evidence.get("controller_id") != context["controller_id"]
        or str(evidence.get("terminal_status") or "").upper() != "CLOSED"
        or evidence.get("snapshot_sha256") != snapshot_sha
        or evidence.get("ledger_sha256") != ledger_sha
        or evidence.get("main_revision") != head
        or evidence.get("validation_errors") != []
    ):
        raise PermissionError("controller guard evidence does not match current Runtime facts")
    tuple_value = pending.get("tuple")
    if not isinstance(tuple_value, dict):
        raise PermissionError("Runtime Host pending receipt tuple is invalid")
    guard = {
        **tuple_value,
        "terminal_receipt_sha256": pending["terminal_receipt_sha256"],
        "terminal_status": "CLOSED",
        "guard_evidence_id": intent["event_id"],
        "guard_evidence_file_sha256": _sha256_hex(evidence_raw),
    }
    return guard, _sha256_hex(evidence_raw)


def handle_request(
    request: object, *, registry_path: Path = target_guard.DEFAULT_REGISTRY,
    lifecycle_path: Path | None = None, verifier: Callable[..., Any] | None = None,
    verifier_config_path: Path | None = None, deadline_monotonic: float | None = None,
) -> dict[str, Any]:
    _remaining_seconds(deadline_monotonic)
    if not isinstance(request, dict):
        raise ValueError("Runtime Host hook request must be an object")
    request_id = _safe_request_id(request.get("request_id"))
    if request.get("protocol") != HOOK_PROTOCOL:
        raise PermissionError("Runtime Host hook protocol is unsupported")
    op = request.get("op")
    expected_keys = PRE_REQUEST_KEYS if op == "pre" else TERMINAL_REQUEST_KEYS if op == "terminal" else set()
    if not expected_keys or set(request) != expected_keys:
        raise PermissionError("Runtime Host hook request schema is invalid")
    receipt_name = "pre_receipt" if op == "pre" else "terminal_receipt"
    receipt = request.get(receipt_name)
    if not isinstance(receipt, dict):
        raise PermissionError(f"Runtime Host {receipt_name} is invalid")
    bridge_call_id = _bounded_text(request.get("bridge_call_id"), label="Bridge call id")
    execution_id = _bounded_text(
        request.get("host_tool_execution_id"), label="Host tool execution id"
    )
    if receipt.get("bridge_call_id") != bridge_call_id or receipt.get("host_tool_execution_id") != execution_id:
        raise PermissionError("Runtime Host hook transport tuple does not match Host receipt")
    context = _runtime_context(
        receipt, registry_path=Path(registry_path), lifecycle_path=lifecycle_path,
        deadline_monotonic=deadline_monotonic,
    )
    _remaining_seconds(deadline_monotonic)
    registered = verifier or web_bridge._external_peer_attestation_verifier(
        "web", config_path=verifier_config_path,
        deadline_monotonic=deadline_monotonic,
    )
    method = getattr(registered, f"verify_tool_{op}", None)
    if not callable(method):
        raise PermissionError("registered Host verifier v2 tool capability is unavailable")
    kwargs: dict[str, Any] = {
        "receipt": receipt,
        "expected_target_generation": context["target_generation"],
        "expected_ownership_generation": context["ownership_generation"],
    }
    if op == "terminal":
        kwargs["expected_pre_chain"] = {
            "pre_receipt_id": receipt.get("pre_receipt_id"),
            "pre_receipt_sha256": receipt.get("pre_receipt_sha256"),
            "capability_id": receipt.get("capability_id"),
            "host_tool_execution_id": execution_id,
        }
    verified = _verify_host_result(
        method(**kwargs, deadline_monotonic=deadline_monotonic),
        phase=op, receipt=receipt, context=context
    )
    if op == "terminal" and verified.get("pre_chain") != {
        "pre_receipt_id": receipt.get("pre_receipt_id"),
        "pre_receipt_sha256": receipt.get("pre_receipt_sha256"),
    }:
        raise PermissionError("registered Host terminal pre chain is mismatched")
    intent = _verified_intent(
        request.get("tool_intent"), receipt=receipt, context=context,
        deadline_monotonic=deadline_monotonic,
    )
    _remaining_seconds(deadline_monotonic)
    if op == "pre":
        prepared = target_guard.prepare_host_tool_execution(
            repo=context["repo"], controller_id=context["controller_id"],
            verified_turn=context["verified_turn"], host_pre_receipt=receipt,
            snapshot_path=intent["snapshot_path"], lifecycle_path=context["lifecycle_path"],
            registry_path=Path(registry_path), deadline_monotonic=deadline_monotonic,
        )
        event = {
            "hook_event_name": "PreToolUse", "session_id": context["controller_id"],
            "controller_session_id": context["controller_id"],
            "source_session_id": context["verified_turn"]["execution_target_session_id"],
            "web_session_id": context["verified_turn"]["execution_target_session_id"],
            "controller_host": "web", "execution_host": "web", "event_source": "web",
            "verified_execution_turn": context["verified_turn"],
            "turn_id": context["verified_turn"]["turn_id"], "cwd": str(context["repo"]),
            "tool_name": "run_command", "tool_use_id": execution_id,
            "tool_input": {"command": intent["command"], "cwd": intent["cwd"]},
        }
        lifecycle_output, _state = lifecycle.process_verified_web_event(
            event, registry_path=Path(registry_path), lifecycle_path=context["lifecycle_path"],
            deadline_monotonic=deadline_monotonic,
        )
        decision = lifecycle_output.get("hookSpecificOutput", {}).get("permissionDecision")
        if decision == "deny" or lifecycle_output.get("decision") == "block":
            raise PermissionError("Runtime lifecycle rejected Host tool pre event")
        return {
            "protocol": HOOK_PROTOCOL, "op": "pre", "decision": "ALLOW",
            "bridge_call_id": bridge_call_id, "host_tool_execution_id": execution_id,
            "receipt_record_sha256": prepared["receipt_record_sha256"],
        }
    _verified_backend(request, receipt=receipt, intent=intent)
    _remaining_seconds(deadline_monotonic)
    pending = target_guard.terminalize_host_tool_execution(
        repo=context["repo"], controller_id=context["controller_id"],
        verified_turn=context["verified_turn"], host_terminal_receipt=receipt,
        snapshot_path=intent["snapshot_path"], lifecycle_path=context["lifecycle_path"],
        registry_path=Path(registry_path), deadline_monotonic=deadline_monotonic,
    )
    guard, evidence_file_sha256 = _guard_evidence(
        context=context, intent=intent, terminal=receipt, pending=pending,
        deadline_monotonic=deadline_monotonic,
    )
    closed = lifecycle.process_verified_host_tool_terminal(
        repo=context["repo"], controller_id=context["controller_id"],
        verified_turn=context["verified_turn"], host_terminal_receipt=receipt,
        guard_evidence=guard, guard_evidence_id=intent["event_id"],
        guard_evidence_file_sha256=evidence_file_sha256,
        snapshot_path=intent["snapshot_path"], command=intent["command"],
        lifecycle_path=context["lifecycle_path"], registry_path=Path(registry_path),
        deadline_monotonic=deadline_monotonic,
    )
    return {
        "protocol": HOOK_PROTOCOL, "op": "terminal", "state": "CLOSED",
        "bridge_call_id": bridge_call_id, "host_tool_execution_id": execution_id,
        "receipt_record_sha256": closed["receipt_record_sha256"],
    }


def _socket_parent(socket_path: Path) -> Path:
    if not socket_path.is_absolute():
        raise PermissionError("Runtime Host hook socket path must be absolute")
    parent = socket_path.parent
    metadata = parent.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise PermissionError("Runtime Host hook socket parent must be a real directory")
    if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
        raise PermissionError("Runtime Host hook socket parent owner mismatch")
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        raise PermissionError("Runtime Host hook socket parent permissions must be 0700")
    if socket_path.exists() or socket_path.is_symlink():
        raise PermissionError("Runtime Host hook refuses a pre-existing socket path")
    return parent


def _read_frame(connection: socket.socket, *, deadline_monotonic: float | None = None) -> bytes:
    deadline = deadline_monotonic if deadline_monotonic is not None else time.monotonic() + IO_TIMEOUT_SECONDS
    data = bytearray()
    while len(data) <= MAX_FRAME_BYTES:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Runtime Host hook request exceeded I/O deadline")
        connection.settimeout(remaining)
        chunk = connection.recv(min(4096, MAX_FRAME_BYTES + 1 - len(data)))
        if not chunk:
            break
        data.extend(chunk)
        newline = data.find(b"\n")
        if newline >= 0:
            if newline != len(data) - 1:
                raise ValueError("Runtime Host hook accepts exactly one NDJSON request")
            return bytes(data[:newline])
    if len(data) > MAX_FRAME_BYTES:
        raise ValueError("Runtime Host hook request exceeds 64 KiB")
    raise ValueError("Runtime Host hook request is missing newline framing")


def serve_unix_socket(
    *, socket_path: Path, registry_path: Path = target_guard.DEFAULT_REGISTRY,
    lifecycle_path: Path | None = None, verifier: Callable[..., Any] | None = None,
    verifier_config_path: Path | None = None,
    stop_event: threading.Event | None = None,
) -> None:
    socket_path = Path(socket_path).expanduser()
    _socket_parent(socket_path)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    created_identity: tuple[int, int] | None = None
    local_stop = stop_event or threading.Event()
    try:
        listener.bind(str(socket_path))
        os.chmod(socket_path, 0o600)
        metadata = socket_path.lstat()
        created_identity = (metadata.st_dev, metadata.st_ino)
        listener.listen(8)
        listener.settimeout(0.2)
        while not local_stop.is_set():
            try:
                connection, _ = listener.accept()
            except socket.timeout:
                continue
            with connection:
                request_id: str | None = None
                try:
                    deadline_monotonic = time.monotonic() + IO_TIMEOUT_SECONDS
                    frame = _read_frame(connection, deadline_monotonic=deadline_monotonic)
                    request = _strict_json(frame.decode("utf-8"), label="Runtime Host hook request")
                    if isinstance(request, dict):
                        request_id = _safe_request_id_or_none(request.get("request_id"))
                    result = handle_request(
                        request, registry_path=registry_path, lifecycle_path=lifecycle_path,
                        verifier=verifier, verifier_config_path=verifier_config_path,
                        deadline_monotonic=deadline_monotonic,
                    )
                    response = {"request_id": request_id, "ok": True, "result": result}
                except Exception as exc:
                    response = {
                        "request_id": request_id, "ok": False,
                        "error": _safe_error_text(exc),
                    }
                try:
                    encoded = _safe_response_bytes(response)
                    if len(encoded) > MAX_FRAME_BYTES:
                        encoded = _safe_response_bytes({
                        "request_id": request_id, "ok": False,
                        "error": "Runtime Host hook response exceeds limit",
                        })
                except Exception:
                    encoded = b'{"error":"Runtime Host hook response serialization failed","ok":false,"request_id":null}\n'
                try:
                    connection.settimeout(_remaining_seconds(deadline_monotonic))
                    connection.sendall(encoded)
                except Exception:
                    pass
    finally:
        listener.close()
        if created_identity is not None:
            try:
                metadata = socket_path.lstat()
                if (
                    stat.S_ISSOCK(metadata.st_mode)
                    and (metadata.st_dev, metadata.st_ino) == created_identity
                ):
                    socket_path.unlink()
            except FileNotFoundError:
                pass


def _stdio_once() -> int:
    request_id: str | None = None
    try:
        request = _strict_json(sys.stdin.read(MAX_FRAME_BYTES + 1).rstrip("\n"), label="Runtime Host hook request")
        if isinstance(request, dict):
            request_id = _safe_request_id_or_none(request.get("request_id"))
        result = handle_request(request)
        response = {"request_id": request_id, "ok": True, "result": result}
    except Exception as exc:
        response = {"request_id": request_id, "ok": False, "error": _safe_error_text(exc)}
    sys.stdout.buffer.write(_safe_response_bytes(response))
    return 0 if response["ok"] else 78


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Consume verified Host tool receipts")
    parser.add_argument("--serve-unix-socket")
    parser.add_argument("--stdio-once", action="store_true")
    args = parser.parse_args(argv)
    if bool(args.serve_unix_socket) == bool(args.stdio_once):
        parser.error("choose exactly one hook transport")
    if args.stdio_once:
        return _stdio_once()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_args: stop.set())
    signal.signal(signal.SIGINT, lambda *_args: stop.set())
    serve_unix_socket(socket_path=Path(args.serve_unix_socket), stop_event=stop)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
