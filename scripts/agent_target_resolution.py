#!/usr/bin/env python3
from __future__ import annotations

from typing import Any

LOGICAL_AGENT_TARGET_RESOLUTION_SCHEMA = 1
LOGICAL_AGENT_TARGET_RESOLUTION_CONTRACT = "logical_agent_target_resolution_v1"
VERIFIED_EXECUTION_TARGET_CONTRACT = "verified_execution_target_v1"
LOGICAL_AGENT_TARGET_RESOLUTION_STATES = (
    "VERIFIED", "UNRESOLVED", "STALE", "CONFLICTED",
)
SUPPORTED_LOGICAL_AGENT_TYPES = (
    "controller",
    "agent",
    "reviewer",
    "runtime_repair_agent",
)
SUPPORTED_EXECUTION_HOSTS = ("web", "desktop_codex")
MAX_AGENT_ID_LENGTH = 256
MAX_SESSION_ID_LENGTH = 256


def _bounded_text(value: object, *, label: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    text = value.strip()
    if not text:
        raise ValueError(f"{label} is required")
    if len(text) > maximum:
        raise ValueError(f"{label} is too long")
    return text


def logical_agent_identity(*, agent_type: str, agent_id: str) -> dict[str, Any]:
    normalized_type = _bounded_text(agent_type, label="logical agent type", maximum=64)
    if normalized_type not in SUPPORTED_LOGICAL_AGENT_TYPES:
        raise ValueError(f"unsupported logical agent type: {normalized_type}")
    return {
        "schema_version": LOGICAL_AGENT_TARGET_RESOLUTION_SCHEMA,
        "agent_type": normalized_type,
        "agent_id": _bounded_text(
            agent_id, label="logical agent id", maximum=MAX_AGENT_ID_LENGTH
        ),
    }


def normalize_logical_agent_identity(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("logical agent identity must be an object")
    if value.get("schema_version") != LOGICAL_AGENT_TARGET_RESOLUTION_SCHEMA:
        raise ValueError("logical agent identity schema_version is unsupported")
    return logical_agent_identity(
        agent_type=value.get("agent_type"),
        agent_id=value.get("agent_id"),
    )


def verified_execution_target(
    *,
    logical_agent: object,
    host: str,
    execution_target_session_id: str,
    target_generation: int,
    ownership_generation: int,
    provenance: str,
    target_mode: str = "explicit_current",
) -> dict[str, Any]:
    identity = normalize_logical_agent_identity(logical_agent)
    normalized_host = _bounded_text(host, label="execution host", maximum=64)
    if normalized_host not in SUPPORTED_EXECUTION_HOSTS:
        raise ValueError(f"unsupported execution host: {normalized_host}")
    for value, label in (
        (target_generation, "target generation"),
        (ownership_generation, "ownership generation"),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{label} must be a positive integer")
    return {
        "schema_version": LOGICAL_AGENT_TARGET_RESOLUTION_SCHEMA,
        "contract": VERIFIED_EXECUTION_TARGET_CONTRACT,
        "state": "VERIFIED",
        "logical_agent_identity": identity,
        "host": normalized_host,
        "execution_target_session_id": _bounded_text(
            execution_target_session_id,
            label="execution target session id",
            maximum=MAX_SESSION_ID_LENGTH,
        ),
        "target_generation": target_generation,
        "ownership_generation": ownership_generation,
        "target_mode": _bounded_text(target_mode, label="target mode", maximum=64),
        "provenance": _bounded_text(provenance, label="target provenance", maximum=128),
    }


def normalize_verified_execution_target(
    value: object,
    *,
    expected_logical_agent: object | None = None,
    expected_host: str | None = None,
    expected_execution_target_session_id: str | None = None,
    expected_target_generation: int | None = None,
    expected_ownership_generation: int | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PermissionError("verified execution target must be an object")
    if value.get("schema_version") != LOGICAL_AGENT_TARGET_RESOLUTION_SCHEMA:
        raise PermissionError("verified execution target schema_version is unsupported")
    if value.get("contract") != VERIFIED_EXECUTION_TARGET_CONTRACT:
        raise PermissionError("verified execution target contract is unsupported")
    if value.get("state") != "VERIFIED":
        raise PermissionError("execution target is not VERIFIED")
    try:
        normalized = verified_execution_target(
            logical_agent=value.get("logical_agent_identity"),
            host=value.get("host"),
            execution_target_session_id=value.get("execution_target_session_id"),
            target_generation=value.get("target_generation"),
            ownership_generation=value.get("ownership_generation"),
            target_mode=value.get("target_mode"),
            provenance=value.get("provenance"),
        )
    except ValueError as exc:
        raise PermissionError(str(exc)) from exc
    if expected_logical_agent is not None:
        expected_identity = normalize_logical_agent_identity(expected_logical_agent)
        if normalized["logical_agent_identity"] != expected_identity:
            raise PermissionError("verified execution target belongs to a different logical Agent")
    if expected_host is not None and normalized["host"] != str(expected_host).strip():
        raise PermissionError("verified execution target host mismatch")
    if (
        expected_execution_target_session_id is not None
        and normalized["execution_target_session_id"]
        != str(expected_execution_target_session_id).strip()
    ):
        raise PermissionError("verified execution target session mismatch")
    if (
        expected_target_generation is not None
        and normalized["target_generation"] != expected_target_generation
    ):
        raise PermissionError("verified execution target target generation is stale")
    if (
        expected_ownership_generation is not None
        and normalized["ownership_generation"] != expected_ownership_generation
    ):
        raise PermissionError("verified execution target ownership generation is stale")
    return normalized


def assert_same_execution_target_fence(
    current: object,
    *,
    expected: object,
) -> dict[str, Any]:
    expected_target = normalize_verified_execution_target(expected)
    return normalize_verified_execution_target(
        current,
        expected_logical_agent=expected_target["logical_agent_identity"],
        expected_host=expected_target["host"],
        expected_execution_target_session_id=expected_target["execution_target_session_id"],
        expected_target_generation=expected_target["target_generation"],
        expected_ownership_generation=expected_target["ownership_generation"],
    )


def execution_target_resolution(
    *,
    logical_agent: object,
    state: str,
    reason: str,
    verified_target: object | None = None,
) -> dict[str, Any]:
    identity = normalize_logical_agent_identity(logical_agent)
    normalized_state = _bounded_text(state, label="target resolution state", maximum=32)
    if normalized_state not in LOGICAL_AGENT_TARGET_RESOLUTION_STATES:
        raise ValueError(f"unsupported target resolution state: {normalized_state}")
    normalized_reason = _bounded_text(reason, label="target resolution reason", maximum=256)
    payload: dict[str, Any] = {
        "schema_version": LOGICAL_AGENT_TARGET_RESOLUTION_SCHEMA,
        "contract": LOGICAL_AGENT_TARGET_RESOLUTION_CONTRACT,
        "state": normalized_state,
        "logical_agent_identity": identity,
        "reason": normalized_reason,
    }
    if normalized_state == "VERIFIED":
        if verified_target is None:
            raise ValueError("VERIFIED target resolution requires a verified execution target")
        payload["verified_execution_target"] = normalize_verified_execution_target(
            verified_target, expected_logical_agent=identity
        )
    elif verified_target is not None:
        raise ValueError("non-VERIFIED target resolution cannot carry a verified execution target")
    return payload


def normalize_execution_target_resolution(
    value: object,
    *,
    expected_logical_agent: object | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PermissionError("logical Agent target resolution must be an object")
    if value.get("schema_version") != LOGICAL_AGENT_TARGET_RESOLUTION_SCHEMA:
        raise PermissionError("logical Agent target resolution schema_version is unsupported")
    if value.get("contract") != LOGICAL_AGENT_TARGET_RESOLUTION_CONTRACT:
        raise PermissionError("logical Agent target resolution contract is unsupported")
    try:
        normalized = execution_target_resolution(
            logical_agent=value.get("logical_agent_identity"),
            state=value.get("state"),
            reason=value.get("reason"),
            verified_target=value.get("verified_execution_target"),
        )
    except ValueError as exc:
        raise PermissionError(str(exc)) from exc
    if expected_logical_agent is not None:
        expected = normalize_logical_agent_identity(expected_logical_agent)
        if normalized["logical_agent_identity"] != expected:
            raise PermissionError("target resolution belongs to a different logical Agent")
    return normalized


def require_verified_execution_target_from_resolution(
    value: object,
    *,
    expected_logical_agent: object | None = None,
) -> dict[str, Any]:
    resolution = normalize_execution_target_resolution(
        value, expected_logical_agent=expected_logical_agent
    )
    if resolution["state"] != "VERIFIED":
        raise PermissionError(
            f"{resolution['state']}: {resolution['reason']}"
        )
    return normalize_verified_execution_target(
        resolution["verified_execution_target"],
        expected_logical_agent=resolution["logical_agent_identity"],
    )

