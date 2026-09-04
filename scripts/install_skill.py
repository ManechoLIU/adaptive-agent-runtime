#!/usr/bin/env python3
"""Install one exact Adaptive Agent Runtime revision with a machine-readable manifest."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import tempfile
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

UTC = timezone.utc
PRODUCT_NAME = "Adaptive Agent Runtime"
SKILL_ID = "adaptive-agent-runtime"
PRODUCT_SLUG = "adaptive-agent-runtime"
LEGACY_SKILL_IDS = ("adaptive-delivery",)
DEFAULT_AI_BRIDGE_EXECUTABLE = Path("/Applications/AI-Bridge.app/Contents/MacOS/ai-bridge")
DEFAULT_CODEX_HOOKS = Path.home() / ".codex" / "hooks.json"
DEFAULT_ZSHENV = Path.home() / ".zshenv"
DEFAULT_CONTROLLER_REGISTRY = Path.home() / ".codex" / "adaptive-delivery-controllers.json"
DEFAULT_WEB_AGENT_EVENT_SOURCE = (
    Path.home() / ".codex" / "state" / "adaptive-delivery-web-agent-health" / "event-source.json"
)
DEFAULT_WEB_AGENT_HEALTH_PLIST = (
    Path.home() / "Library" / "LaunchAgents"
    / "com.openai.adaptive-agent-runtime.web-agent-health.plist"
)
WEB_AGENT_HEALTH_LABEL = "com.openai.adaptive-agent-runtime.web-agent-health"
DEFAULT_DESKTOP_CANARY = (
    Path.home() / ".codex" / "state" / "adaptive-delivery-desktop-canary.json"
)
WEB_BLOCK_START = "# >>> adaptive-delivery web lifecycle bridge >>>"
WEB_BLOCK_END = "# <<< adaptive-delivery web lifecycle bridge <<<"
MANIFEST_NAME = ".adaptive-delivery-install.json"
IMPACTS = {"none", "live_assignments"}
RUNTIME_RELEASE_REGRESSION_TESTS = (
    "tests.test_web_reentry_adapter.WebReentryContinuationRegressionTests."
    "test_transient_web_reentry_failure_rearms_existing_continuation_supervisor",
    "tests.test_web_collaboration_continuation.WebCollaborationContinuationRegressionTests."
    "test_regression_parent_already_yielded_then_writer_completed_wakes_same_controller_with_next_runnable",
    "tests.test_web_collaboration_continuation.WebCollaborationContinuationRegressionTests."
    "test_completed_reviewer_uses_same_terminal_continuation_path",
    "tests.test_web_collaboration_continuation.WebCollaborationContinuationRegressionTests."
    "test_stale_child_is_second_observed_by_existing_audit_and_wakes_same_controller",
    "tests.test_web_collaboration_continuation.WebCollaborationContinuationRegressionTests."
    "test_duplicate_terminal_observation_after_confirmed_continuation_does_not_wake_twice",
    "tests.test_governance.GovernanceTests."
    "test_project_wide_projection_web_active_verify_do_not_starve_mini_runnables",
    "tests.test_governance.GovernanceTests."
    "test_project_wide_projection_mini_active_does_not_starve_server_or_web",
    "tests.test_governance.GovernanceTests."
    "test_project_wide_fairness_requires_parallel_dispatch_when_capacity_exists",
    "tests.test_governance.GovernanceTests."
    "test_pending_dependency_closure_dynamically_enters_project_wide_runnable_projection",
    "tests.test_governance.GovernanceTests."
    "test_project_wide_fairness_rejects_more_active_dispatches_than_capacity",
    "tests.test_governance.GovernanceTests."
    "test_pending_parent_partial_dependency_creates_dynamic_runnable_slice",
    "tests.test_governance.GovernanceTests."
    "test_reviewer_terminal_recomputes_unrelated_project_runnable",
    "tests.test_governance.GovernanceTests."
    "test_stop_without_current_turn_control_loop_receipt_fails_closed_even_when_idle",
    "tests.test_governance.GovernanceTests."
    "test_active_writer_does_not_hide_immediate_controller_actions",
    "tests.test_governance.GovernanceTests."
    "test_control_loop_receipt_rejects_missing_or_reordered_control_steps",
    "tests.test_governance.GovernanceTests."
    "test_failed_control_cycle_generates_executable_controller_correction",
    "tests.test_governance.GovernanceTests."
    "test_unfinished_correction_prevents_control_cycle_closure",
    "tests.test_governance.GovernanceTests."
    "test_same_controller_deviation_fingerprint_escalates_on_recurrence",
    "tests.test_governance.GovernanceTests."
    "test_direct_cycle_persistence_cannot_fabricate_generic_correction_closure",
    "tests.test_web_agent_execution.WebAgentExecutionTests."
    "test_direct_start_web_assignment_is_rejected_even_with_forged_readiness_probe",
    "tests.test_web_agent_execution.WebAgentExecutionTests."
    "test_public_web_adapter_rejects_self_asserted_strong_attestation",
    "tests.test_web_agent_execution.WebAgentExecutionTests."
    "test_host_started_lease_uses_attested_machine_start_time_not_ingestion_time",
    "tests.test_web_agent_execution.WebAgentExecutionTests."
    "test_host_started_rejects_unattested_or_mismatched_machine_start_time",
    "tests.test_web_agent_execution.RuntimeOwnedWebRecoveryContractTests."
    "test_machine_event_source_public_status_cannot_accept_caller_verifier",
    "tests.test_web_agent_execution.RuntimeOwnedWebRecoveryContractTests."
    "test_forged_local_machine_event_receipt_cannot_enable_production_prepare",
    "tests.test_web_agent_execution.RuntimeOwnedWebRecoveryContractTests."
    "test_forged_safe_fallback_fields_without_canonical_prior_terminal_are_rejected",
    "tests.test_web_agent_execution.RuntimeOwnedWebRecoveryContractTests."
    "test_safe_fallback_policy_must_declare_selected_fallback_route_not_only_origin",
    "tests.test_web_agent_execution.RuntimeOwnedWebRecoveryContractTests."
    "test_canonical_kimi_terminal_with_declared_route_can_prepare_web_fallback",
    "tests.test_web_agent_execution.RuntimeOwnedWebRecoveryContractTests."
    "test_direct_internal_observation_chain_cannot_create_lease_without_attested_machine_event",
    "tests.test_web_agent_execution.RuntimeOwnedWebRecoveryContractTests."
    "test_route_policy_accepts_normal_whitespace_declaration",
    "tests.test_web_agent_execution.RuntimeOwnedWebRecoveryContractTests."
    "test_route_policy_rejects_prefixed_fields_comments_and_negative_examples",
    "tests.test_web_agent_execution.RuntimeOwnedWebRecoveryContractTests."
    "test_route_policy_accepts_active_rule_after_inactive_examples",
    "tests.test_web_agent_execution.RuntimeOwnedWebRecoveryContractTests."
    "test_route_policy_rejects_inline_comments_negation_hyphen_prefix_and_value_only_class_marker",
    "tests.test_web_agent_execution.RuntimeOwnedWebRecoveryContractTests."
    "test_route_policy_normalizes_unicode_dash_variants_before_authorization",
    "tests.test_web_agent_execution.RuntimeOwnedWebRecoveryContractTests."
    "test_route_policy_accepts_explicit_web_class_marker_before_fields",
    "tests.test_web_agent_execution.RuntimeOwnedWebRecoveryContractTests."
    "test_route_policy_rejects_tilde_fenced_route_examples",
    "tests.test_web_agent_execution.StructuredCollaborationTerminalTests."
    "test_public_structured_terminal_ingest_rejects_caller_supplied_observation",
    "tests.test_web_agent_execution.StructuredCollaborationTerminalTests."
    "test_internal_terminal_helper_cannot_accept_fabricated_observation_without_attested_path",
)
RUNTIME_RELEASE_NODE_REGRESSION_TESTS = (
    "heterogeneous frontend and backend tasks stay on Kimi and Grok canonical executors",
    "assignment-bound execute rejects CLI route mismatch before provider spawn",
    "assignment-bound safe fallback requires canonical prior terminal before provider spawn",
    "assignment-bound external start persists exact canonical route contract",
    "short assignment-bound execution reconciles final Git progress before terminal",
    "fresh legacy v1 assignment ACK cannot launch external provider",
)
RUNTIME_RELEASE_REQUIRED_FILES = (
    "scripts/web_agent_execution.py",
    "scripts/web_agent_events.py",
    "scripts/web_lifecycle_bridge.py",
    "scripts/web_reentry_adapter.py",
    "scripts/lifecycle_hook.py",
    "scripts/control_event_guard.py",
    "scripts/controller_state.py",
    "scripts/route_contract.py",
    "scripts/assignment_lease_guard.py",
    "scripts/run_external_agent.mjs",
    "tests/test_web_agent_execution.py",
    "tests/test_web_reentry_adapter.py",
    "tests/test_web_collaboration_continuation.py",
    "tests/test_governance.py",
    "tests/external-agent-routing.test.mjs",
)


def default_install_target(skills_root: str | Path | None = None) -> Path:
    root = Path(skills_root).expanduser() if skills_root is not None else Path.home() / ".agents" / "skills"
    current = root / SKILL_ID
    legacy = root / LEGACY_SKILL_IDS[0]
    if (current.exists() or current.is_symlink()) and (legacy.exists() or legacy.is_symlink()):
        raise ValueError(f"multiple skill installs detected: {current} and {legacy}; keep one canonical install")
    if current.exists() or current.is_symlink():
        return current
    if legacy.exists() or legacy.is_symlink():
        return legacy
    return current


def _ensure_single_skill_install(target: Path) -> None:
    if target.name not in {SKILL_ID, *LEGACY_SKILL_IDS}:
        return
    siblings = [target.parent / SKILL_ID, *(target.parent / value for value in LEGACY_SKILL_IDS)]
    existing = [path for path in siblings if path.exists() or path.is_symlink()]
    target_exists = target.exists() or target.is_symlink()
    if len(existing) > 1 or (existing and not target_exists):
        rendered = " and ".join(str(path) for path in existing)
        raise ValueError(
            f"multiple skill installs detected or would be created beside {rendered}; keep one canonical install"
        )



def _hooks_contain(path: Path, needle: str, *, skill_root: Path | None = None) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    hooks = data.get("hooks") if isinstance(data, dict) else None
    if not isinstance(hooks, dict):
        return False
    expected = (skill_root / "scripts" / needle).resolve() if skill_root is not None else None
    if expected is not None and not expected.is_file():
        return False
    for entries in hooks.values():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
                continue
            for handler in entry["hooks"]:
                if not isinstance(handler, dict):
                    continue
                command = handler.get("command")
                if not isinstance(command, str) or needle not in command:
                    continue
                if expected is None:
                    return True
                try:
                    tokens = shlex.split(command)
                except ValueError:
                    continue
                if len(tokens) >= 2 and Path(tokens[1]).expanduser().resolve() == expected:
                    return True
    return False


def _hook_event_contains(
    path: Path, event_name: str, needle: str, *, skill_root: Path | None = None
) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    hooks = data.get("hooks") if isinstance(data, dict) else None
    entries = hooks.get(event_name) if isinstance(hooks, dict) else None
    if not isinstance(entries, list):
        return False
    expected = (skill_root / "scripts" / needle).resolve() if skill_root is not None else None
    if expected is not None and not expected.is_file():
        return False
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
            continue
        for handler in entry["hooks"]:
            command = handler.get("command") if isinstance(handler, dict) else None
            if not isinstance(command, str) or needle not in command:
                continue
            if expected is None:
                return True
            try:
                tokens = shlex.split(command)
            except ValueError:
                continue
            if len(tokens) >= 2 and Path(tokens[1]).expanduser().resolve() == expected:
                return True
    return False


DESKTOP_CANARY_SEQUENCE = (
    "session_started",
    "pre_tool_allowed",
    "post_tool_observed",
    "receipt_latched",
    "same_turn_denied",
    "stop_observed",
    "next_turn_allowed",
    "subagent_stop_observed",
)
DESKTOP_CANARY_MAX_AGE_SECONDS = 24 * 60 * 60


def _valid_desktop_canary(
    path: Path, *, hooks_path: Path, skill_root: Path | None
) -> bool:
    if skill_root is None:
        return False
    lifecycle = skill_root / "scripts" / "lifecycle_hook.py"
    target_guard = skill_root / "scripts" / "controller_target_guard.py"
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
        hooks_sha256 = hashlib.sha256(hooks_path.read_bytes()).hexdigest()
        lifecycle_sha256 = hashlib.sha256(lifecycle.read_bytes()).hexdigest()
        target_guard_sha256 = hashlib.sha256(target_guard.read_bytes()).hexdigest()
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(receipt, dict):
        return False
    try:
        completed_at = datetime.fromisoformat(str(receipt.get("completed_at", "")))
        if completed_at.tzinfo is None:
            return False
        age_seconds = (datetime.now(UTC) - completed_at.astimezone(UTC)).total_seconds()
    except (TypeError, ValueError):
        return False
    if age_seconds < 0 or age_seconds > DESKTOP_CANARY_MAX_AGE_SECONDS:
        return False
    observations = receipt.get("observations")
    return (
        receipt.get("schema_version") == 3
        and receipt.get("status") == "passed"
        and isinstance(receipt.get("controller_session_id"), str)
        and bool(receipt.get("controller_session_id"))
        and isinstance(receipt.get("run_id"), str)
        and len(receipt.get("run_id")) >= 16
        and receipt.get("sequence_index") == len(DESKTOP_CANARY_SEQUENCE)
        and receipt.get("skill_root") == str(skill_root.resolve())
        and receipt.get("hooks_sha256") == hooks_sha256
        and receipt.get("lifecycle_sha256") == lifecycle_sha256
        and receipt.get("controller_target_guard_sha256") == target_guard_sha256
        and observations == list(DESKTOP_CANARY_SEQUENCE)
    )


def _zshenv_has_web_bridge(
    path: Path, *, skill_root: Path | None = None, ai_bridge_executable: Path | None = None
) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    start = text.find(WEB_BLOCK_START)
    end = text.find(WEB_BLOCK_END, start + len(WEB_BLOCK_START)) if start >= 0 else -1
    if start < 0 or end < 0:
        return False
    block = text[start:end + len(WEB_BLOCK_END)]
    command_line = next((line.strip() for line in block.splitlines() if " post-shell " in line and "web_lifecycle_bridge.py" in line), None)
    if command_line is None:
        return False
    command_text = command_line[:-7].rstrip() if command_line.endswith("|| true") else command_line
    try:
        tokens = shlex.split(command_text)
    except ValueError:
        return False
    if len(tokens) < 3 or tokens[2] != "post-shell":
        return False
    python_path = Path(tokens[0]).expanduser()
    script_path = Path(tokens[1]).expanduser()
    if not python_path.is_file() or not os.access(python_path, os.X_OK) or not script_path.is_file():
        return False
    if skill_root is not None:
        expected_script = (skill_root / "scripts" / "web_lifecycle_bridge.py").expanduser().resolve()
        if script_path.resolve() != expected_script:
            return False
    if ai_bridge_executable is not None:
        expected_assignment = f"_ad_web_bridge_executable={shlex.quote(str(ai_bridge_executable.expanduser().resolve()))}"
        if expected_assignment not in block:
            return False
    return True



def install_web_agent_health_service_plist(
    plist_file: str | Path,
    target: str | Path,
    *,
    python_executable: str | None = None,
    registry_path: str | Path = DEFAULT_CONTROLLER_REGISTRY,
) -> Path:
    path = Path(plist_file).expanduser().resolve(strict=False)
    target_path = Path(target).expanduser().resolve()
    script = (target_path / "scripts" / "web_agent_health_supervisor.py").resolve()
    if not script.is_file():
        raise ValueError("installed Web Agent health supervisor script is missing")
    python = str(Path(python_executable or sys.executable).expanduser().resolve())
    log_root = Path.home() / ".codex" / "state" / "adaptive-delivery-web-agent-health"
    log_root.mkdir(parents=True, exist_ok=True)
    payload = {
        "Label": WEB_AGENT_HEALTH_LABEL,
        "ProgramArguments": [
            python,
            str(script),
            "--registry",
            str(Path(registry_path).expanduser().resolve(strict=False)),
            "--poll-seconds",
            "15",
        ],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "StandardOutPath": str(log_root / "launchd.stdout.log"),
        "StandardErrorPath": str(log_root / "launchd.stderr.log"),
        "EnvironmentVariables": {
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            plistlib.dump(payload, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return path


def _health_service_plist_matches(
    path: Path, *, skill_root: Path | None,
) -> bool:
    if skill_root is None:
        return False
    expected = (skill_root / "scripts" / "web_agent_health_supervisor.py").resolve()
    if not expected.is_file():
        return False
    try:
        payload = plistlib.loads(path.read_bytes())
    except (OSError, ValueError, plistlib.InvalidFileException):
        return False
    args = payload.get("ProgramArguments") if isinstance(payload, dict) else None
    return (
        payload.get("Label") == WEB_AGENT_HEALTH_LABEL
        and payload.get("RunAtLoad") is True
        and payload.get("KeepAlive") is True
        and isinstance(args, list)
        and str(expected) in [str(item) for item in args]
    )



def _load_web_agent_health_service(plist_path: Path) -> dict[str, Any]:
    launchctl = Path("/bin/launchctl")
    if not launchctl.is_file():
        raise OSError("launchctl is unavailable; Runtime health service cannot be activated")
    domain = f"gui/{os.getuid()}"
    subprocess.run(
        [str(launchctl), "bootout", domain, str(plist_path)],
        capture_output=True, text=True, check=False,
    )
    bootstrap = subprocess.run(
        [str(launchctl), "bootstrap", domain, str(plist_path)],
        capture_output=True, text=True, check=False,
    )
    if bootstrap.returncode != 0:
        raise OSError(
            f"launchctl bootstrap failed: {(bootstrap.stderr or bootstrap.stdout).strip()}"
        )
    kick = subprocess.run(
        [str(launchctl), "kickstart", "-k", f"{domain}/{WEB_AGENT_HEALTH_LABEL}"],
        capture_output=True, text=True, check=False,
    )
    if kick.returncode != 0:
        raise OSError(
            f"launchctl kickstart failed: {(kick.stderr or kick.stdout).strip()}"
        )
    return {"state": "loaded", "domain": domain, "label": WEB_AGENT_HEALTH_LABEL}


def _unload_web_agent_health_service(plist_path: Path) -> None:
    launchctl = Path("/bin/launchctl")
    if not launchctl.is_file():
        return
    subprocess.run(
        [str(launchctl), "bootout", f"gui/{os.getuid()}", str(plist_path)],
        capture_output=True, text=True, check=False,
    )


def configure_runtime_services(
    target: str | Path,
    *,
    health_service_plist: str | Path = DEFAULT_WEB_AGENT_HEALTH_PLIST,
    registry_path: str | Path = DEFAULT_CONTROLLER_REGISTRY,
    python_executable: str | None = None,
    service_loader: Any | None = None,
) -> dict[str, Any]:
    target_path = Path(target).expanduser().resolve()
    plist_path = install_web_agent_health_service_plist(
        health_service_plist,
        target_path,
        python_executable=python_executable,
        registry_path=registry_path,
    )
    loader = service_loader or _load_web_agent_health_service
    result = loader(plist_path)
    if not isinstance(result, dict):
        result = {"state": "loaded"}
    return {
        **result,
        "configured": _health_service_plist_matches(
            plist_path, skill_root=target_path
        ),
        "plist": str(plist_path),
    }


def _machine_web_event_source_ready(path: Path) -> bool:
    try:
        from scripts.web_agent_events import machine_event_source_ready
    except ModuleNotFoundError:
        from web_agent_events import machine_event_source_ready
    return bool(machine_event_source_ready(path=path))


def detect_host_capabilities(
    *,
    codex_executable: str | Path | None = None,
    ai_bridge_executable: str | Path = DEFAULT_AI_BRIDGE_EXECUTABLE,
    hooks_file: str | Path = DEFAULT_CODEX_HOOKS,
    zshenv_file: str | Path = DEFAULT_ZSHENV,
    skill_root: str | Path | None = None,
    desktop_canary_file: str | Path = DEFAULT_DESKTOP_CANARY,
    health_service_plist: str | Path = DEFAULT_WEB_AGENT_HEALTH_PLIST,
    web_event_source_receipt: str | Path = DEFAULT_WEB_AGENT_EVENT_SOURCE,
) -> dict[str, dict[str, Any]]:
    codex_path = Path(codex_executable).expanduser() if codex_executable else None
    if codex_path is None:
        discovered = shutil.which("codex")
        codex_path = Path(discovered) if discovered else None
    bridge_path = Path(ai_bridge_executable).expanduser()
    hooks_path = Path(hooks_file).expanduser()
    zshenv_path = Path(zshenv_file).expanduser()
    desktop_canary_path = Path(desktop_canary_file).expanduser()
    skill_root_path = Path(skill_root).expanduser().resolve() if skill_root is not None else None
    health_service_path = Path(health_service_plist).expanduser().resolve(strict=False)
    health_service_configured = _health_service_plist_matches(
        health_service_path, skill_root=skill_root_path
    )
    web_event_source_path = Path(web_event_source_receipt).expanduser().resolve(strict=False)
    machine_event_source_ready = _machine_web_event_source_ready(web_event_source_path)

    codex_available = bool(codex_path and codex_path.is_file() and os.access(codex_path, os.X_OK))
    lifecycle_events = {
        "SessionStart",
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
        "SubagentStop",
        "Stop",
    }
    lifecycle_configured = all(
        _hook_event_contains(
            hooks_path, event_name, "lifecycle_hook.py", skill_root=skill_root_path
        )
        for event_name in lifecycle_events
    )
    scoring_configured = all(
        _hook_event_contains(
            hooks_path, event_name, "controller_scoring_hook.py", skill_root=skill_root_path
        )
        for event_name in {"UserPromptSubmit", "Stop"}
    )
    canary_valid = _valid_desktop_canary(
        desktop_canary_path, hooks_path=hooks_path, skill_root=skill_root_path
    )
    if not codex_available:
        desktop = {
            "status": "blocked", "adapter": "codex-native", "configured": False,
            "reason": "codex executable not detected",
        }
    elif lifecycle_configured and scoring_configured and canary_valid:
        desktop = {
            "status": "enabled", "adapter": "codex-native", "configured": True,
            "reason": "hooks configured and exact live canary receipt verified",
        }
    elif lifecycle_configured and scoring_configured:
        desktop = {
            "status": "degraded", "adapter": "codex-native", "configured": True,
            "reason": "hooks configured; exact live canary receipt is missing or stale",
        }
    else:
        desktop = {
            "status": "degraded", "adapter": "codex-native", "configured": False,
            "reason": "codex detected; lifecycle/scoring hooks are not fully configured",
        }

    bridge_available = bridge_path.is_file() and os.access(bridge_path, os.X_OK)
    bridge_configured = _zshenv_has_web_bridge(
        zshenv_path, skill_root=skill_root_path, ai_bridge_executable=bridge_path
    )
    if bridge_available and bridge_configured:
        web = {
            "status": "enabled", "adapter": "ai-bridge", "mode": "local_bridge",
            "configured": True, "reason": "AI-Bridge executable and shell lifecycle bridge detected",
        }
    elif bridge_available:
        web = {
            "status": "degraded", "adapter": "ai-bridge", "mode": "local_bridge",
            "configured": False, "reason": "AI-Bridge detected; shell lifecycle bridge is not configured",
        }
    else:
        web = {
            "status": "degraded", "adapter": "none", "mode": "pure_web_file",
            "configured": False, "reason": "AI-Bridge not detected; local repo/runtime access is unavailable",
        }
    return {
        "core": {"status": "enabled", "adapter": "adaptive-agent-runtime", "configured": True, "reason": "core governance is host-neutral"},
        "desktop_adapter": desktop,
        "web_local_adapter": web,
        "web_agent_execution": {
            "status": "host_limited",
            "adapter": "web-agent-execution",
            "configured": health_service_configured and machine_event_source_ready,
            "health_supervisor": "launchd_keepalive" if health_service_configured else "not_configured",
            "continuation": "existing_web_reentry_supervisor",
            "recovery_mode": "canonical_progress_health_supervisor",
            "host_terminal": "unavailable",
            "structured_terminal": (
                "chatgpt_subagent_machine_events" if machine_event_source_ready else "unavailable"
            ),
            "dispatch_interception": "unavailable_on_chatgpt_web",
            "reason": (
                "Runtime health scheduler is installed, but no trustworthy ChatGPT Web child machine event source is available; native Web child dispatch must fail closed"
                if health_service_configured and not machine_event_source_ready
                else (
                    "Runtime health scheduler and trustworthy Web child machine event source are ready; continuation reuses the existing Web reentry supervisor"
                    if health_service_configured and machine_event_source_ready
                    else "Web execution code is installed but the Runtime health supervisor service is not configured"
                )
            ),
        },
    }


def _read_hooks_config(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        value = {}
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid hooks JSON: {path}") from error
    if not isinstance(value, dict):
        raise ValueError("hooks config root must be an object")
    hooks = value.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("hooks must be an object")
    return value


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _remove_matching_handlers(entries: list[Any], needle: str) -> list[Any]:
    kept: list[Any] = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
            if needle not in str(entry):
                kept.append(entry)
            continue
        remaining = [handler for handler in entry["hooks"] if needle not in str(handler)]
        if remaining:
            preserved = dict(entry)
            preserved["hooks"] = remaining
            kept.append(preserved)
    return kept


def install_codex_hooks(
    hooks_file: str | Path,
    target: str | Path,
    *,
    python_executable: str | None = None,
) -> dict[str, Any]:
    path = Path(hooks_file).expanduser().resolve()
    target_path = Path(target).expanduser().resolve()
    config = _read_hooks_config(path)
    hooks = config["hooks"]
    python = python_executable or sys.executable
    lifecycle_command = f"{shlex.quote(str(python))} {shlex.quote(str(target_path / 'scripts' / 'lifecycle_hook.py'))}"
    scoring_command = f"{shlex.quote(str(python))} {shlex.quote(str(target_path / 'scripts' / 'controller_scoring_hook.py'))}"

    lifecycle_specs = {
        "SessionStart": ("startup|resume|clear|compact", "Loading Adaptive Agent Runtime controller state", True),
        "UserPromptSubmit": (None, "Opening Adaptive Agent Runtime control turn", False),
        "PreToolUse": ("*", "Enforcing Adaptive Agent Runtime turn boundary", False),
        "PostToolUse": ("*", "Checking Adaptive Agent Runtime lifecycle", True),
        "SubagentStop": (None, "Recording Adaptive Agent Runtime candidate", False),
        "Stop": (None, "Closing Adaptive Agent Runtime control event", False),
    }
    for event_name, (matcher, status, inject_context) in lifecycle_specs.items():
        entries = hooks.setdefault(event_name, [])
        if not isinstance(entries, list):
            raise ValueError(f"{event_name} hooks must be a list")
        entries[:] = _remove_matching_handlers(entries, "lifecycle_hook.py")
        handler: dict[str, Any] = {
            "type": "command", "command": lifecycle_command, "timeout": 5, "statusMessage": status,
        }
        if inject_context:
            handler["additionalContextLimit"] = 4096
        group: dict[str, Any] = {"hooks": [handler]}
        if matcher is not None:
            group["matcher"] = matcher
        entries.append(group)

    for event_name, inject_context in (("UserPromptSubmit", True), ("Stop", False)):
        entries = hooks.setdefault(event_name, [])
        if not isinstance(entries, list):
            raise ValueError(f"{event_name} hooks must be a list")
        entries[:] = _remove_matching_handlers(entries, "controller_scoring_hook.py")
        handler = {
            "type": "command", "command": scoring_command, "timeout": 5,
            "statusMessage": "Enforcing Adaptive Agent Runtime controller scoring model",
        }
        if inject_context:
            handler["additionalContextLimit"] = 0
        entries.append({"hooks": [handler]})

    _write_text_atomic(path, json.dumps(config, ensure_ascii=False, indent=2) + "\n")
    return config


def _web_zshenv_block(target: Path, bridge: Path, python_executable: str) -> str:
    script = target / "scripts" / "web_lifecycle_bridge.py"
    bridge_literal = shlex.quote(str(bridge))
    python_literal = shlex.quote(str(python_executable))
    script_literal = shlex.quote(str(script))
    return f"""{WEB_BLOCK_START}
_ad_web_bridge_executable={bridge_literal}
_ad_web_parent=$(/bin/ps -p \"$PPID\" -o comm= 2>/dev/null)
_ad_web_session_id=\"${{ADAPTIVE_DELIVERY_WEB_SESSION_ID:-}}\"
if [[ \"$_ad_web_parent\" == \"$_ad_web_bridge_executable\" && -z \"$_ad_web_session_id\" ]]; then
  _ad_web_session_id=$({python_literal} {script_literal} resolve-manual-web-session --cwd \"$PWD\" 2>/dev/null)
fi
if [[ \"$_ad_web_parent\" == \"$_ad_web_bridge_executable\" && -n \"$_ad_web_session_id\" ]]; then
  _ad_web_cwd=\"$PWD\"
  _ad_web_command=\"$ZSH_EXECUTION_STRING\"
  _ad_web_lifecycle_exit() {{
    local _ad_web_exit_code=$?
    trap - EXIT
    {python_literal} {script_literal} post-shell --cwd \"$_ad_web_cwd\" --command \"$_ad_web_command\" --exit-code \"$_ad_web_exit_code\" --web-session-id \"$_ad_web_session_id\"
    local _ad_web_bridge_exit_code=$?
    if [[ \"$_ad_web_exit_code\" -ne 0 ]]; then
      exit \"$_ad_web_exit_code\"
    fi
    exit \"$_ad_web_bridge_exit_code\"
  }}
  trap _ad_web_lifecycle_exit EXIT
fi
unset _ad_web_parent _ad_web_bridge_executable
{WEB_BLOCK_END}"""

def install_ai_bridge_zshenv(
    zshenv_file: str | Path,
    target: str | Path,
    ai_bridge_executable: str | Path = DEFAULT_AI_BRIDGE_EXECUTABLE,
    *,
    python_executable: str | None = None,
) -> None:
    path = Path(zshenv_file).expanduser().resolve()
    target_path = Path(target).expanduser().resolve()
    bridge = Path(ai_bridge_executable).expanduser().resolve()
    python = python_executable or sys.executable
    try:
        current = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        current = ""
    start = current.find(WEB_BLOCK_START)
    if start >= 0:
        end = current.find(WEB_BLOCK_END, start)
        if end < 0:
            raise ValueError("existing web lifecycle bridge block is unterminated")
        end += len(WEB_BLOCK_END)
        current = current[:start].rstrip() + "\n" + current[end:].lstrip("\n")
    block = _web_zshenv_block(target_path, bridge, python)
    prefix = current.rstrip()
    text = (prefix + "\n\n" if prefix else "") + block + "\n"
    _write_text_atomic(path, text)


def configure_host_adapters(
    target: str | Path,
    *,
    codex_executable: str | Path | None = None,
    ai_bridge_executable: str | Path = DEFAULT_AI_BRIDGE_EXECUTABLE,
    hooks_file: str | Path = DEFAULT_CODEX_HOOKS,
    zshenv_file: str | Path = DEFAULT_ZSHENV,
    python_executable: str | None = None,
) -> dict[str, dict[str, Any]]:
    target_path = Path(target).expanduser().resolve()
    codex_path = Path(codex_executable).expanduser() if codex_executable else None
    if codex_path is None:
        discovered = shutil.which("codex")
        codex_path = Path(discovered) if discovered else None
    if codex_path is not None and codex_path.is_file() and os.access(codex_path, os.X_OK):
        install_codex_hooks(hooks_file, target_path, python_executable=python_executable)
    bridge = Path(ai_bridge_executable).expanduser()
    if bridge.is_file() and os.access(bridge, os.X_OK):
        install_ai_bridge_zshenv(zshenv_file, target_path, bridge, python_executable=python_executable)
    return detect_host_capabilities(
        codex_executable=codex_path,
        ai_bridge_executable=bridge,
        hooks_file=hooks_file,
        zshenv_file=zshenv_file,
        skill_root=target_path,
    )


def _git(source: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(source), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_manifest(path: Path) -> dict[str, Any]:
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


def _verify_upgrade_lineage(source: Path, previous_revision: str | None, revision: str) -> dict[str, Any]:
    """Fail closed when an installed Runtime revision is not in the candidate's Git ancestry."""
    if not previous_revision:
        return {"status": "fresh_install", "previous_revision": None, "revision": revision}

    exists = subprocess.run(
        ["git", "-C", str(source), "cat-file", "-e", f"{previous_revision}^{{commit}}"],
        capture_output=True,
    )
    if exists.returncode != 0:
        raise ValueError(
            "installed previous revision is absent from candidate source history: "
            f"{previous_revision}; integrate/adopt the installed Runtime lineage before upgrading"
        )

    ancestry = subprocess.run(
        ["git", "-C", str(source), "merge-base", "--is-ancestor", previous_revision, revision],
        capture_output=True,
    )
    if ancestry.returncode != 0:
        raise ValueError(
            "candidate Runtime revision does not descend from the installed revision: "
            f"{previous_revision} -> {revision}; nonlinear upgrade is blocked"
        )
    return {
        "status": "linear",
        "previous_revision": previous_revision,
        "revision": revision,
    }


def _changed_files(source: Path, previous_revision: str | None, revision: str, tracked: list[str]) -> list[str]:
    if not previous_revision:
        return tracked
    exists = subprocess.run(
        ["git", "-C", str(source), "cat-file", "-e", f"{previous_revision}^{{commit}}"],
        capture_output=True,
    )
    if exists.returncode != 0:
        return tracked
    output = _git(source, "diff", "--name-only", f"{previous_revision}..{revision}")
    return sorted(line for line in output.splitlines() if line.strip())



def _revision_tree_entries(source: Path, revision: str) -> list[tuple[str, str, str, str]]:
    raw = subprocess.run(
        ["git", "-C", str(source), "ls-tree", "-rz", "--full-tree", revision],
        check=True,
        capture_output=True,
    ).stdout
    entries: list[tuple[str, str, str, str]] = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        meta, raw_path = record.split(b"\t", 1)
        mode, kind, object_id = meta.decode("ascii").split(" ", 2)
        path = raw_path.decode("utf-8")
        entries.append((mode, kind, object_id, path))
    return entries


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _materialize_revision(source: Path, revision: str, destination: Path) -> list[str]:
    entries = _revision_tree_entries(source, revision)
    tracked: list[str] = []
    for mode, kind, object_id, relative in entries:
        if kind != "blob":
            raise ValueError(f"unsupported tracked object in install revision: {relative} ({kind})")
        tracked.append(relative)
        dst = destination / relative
        if dst.exists() or dst.is_symlink():
            _remove_path(dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        content = subprocess.run(
            ["git", "-C", str(source), "cat-file", "blob", object_id],
            check=True,
            capture_output=True,
        ).stdout
        if mode == "120000":
            os.symlink(content.decode("utf-8"), dst)
        else:
            dst.write_bytes(content)
            dst.chmod(0o755 if mode == "100755" else 0o644)
    return sorted(tracked)


def _verify_runtime_release_regressions(
    source: Path,
    revision: str,
    *,
    required: bool = False,
) -> dict[str, Any]:
    """Run immutable cross-language Runtime regressions before installation."""
    tracked = {entry[3] for entry in _revision_tree_entries(source, revision)}
    # A genuinely pre-Web/minimal package may remain not-applicable. Once the installed
    # Runtime has Web execution, however, removing the marker is a downgrade attempt and
    # the required-file gate must fail closed instead of disabling itself.
    if "scripts/web_agent_execution.py" not in tracked and not required:
        return {"status": "not_applicable", "tests": [], "node_tests": []}

    missing = sorted(path for path in RUNTIME_RELEASE_REQUIRED_FILES if path not in tracked)
    if missing:
        raise ValueError(
            "Runtime release regression gate is incomplete; required files are missing: "
            + ", ".join(missing)
        )

    with tempfile.TemporaryDirectory(prefix="adaptive-agent-runtime-release-gate-") as tmp:
        checkout = Path(tmp) / "candidate"
        checkout.mkdir()
        _materialize_revision(source, revision, checkout)
        python_result = subprocess.run(
            [sys.executable, "-m", "unittest", "-v", *RUNTIME_RELEASE_REGRESSION_TESTS],
            cwd=checkout,
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        if python_result.returncode != 0:
            bounded = (
                python_result.stderr
                or python_result.stdout
                or "release regression test failed"
            ).strip()
            if len(bounded) > 6000:
                bounded = bounded[-6000:]
            raise ValueError("Runtime release regression gate failed:" + chr(10) + bounded)

        node = shutil.which("node")
        if not node:
            raise ValueError(
                "Runtime release regression gate failed: node executable is required "
                "for external-agent routing regression"
            )
        pattern = "|".join(re.escape(name) for name in RUNTIME_RELEASE_NODE_REGRESSION_TESTS)
        node_result = subprocess.run(
            [
                node,
                "--test",
                "--test-name-pattern=" + pattern,
                "tests/external-agent-routing.test.mjs",
            ],
            cwd=checkout,
            capture_output=True,
            text=True,
            env={**os.environ, "NODE_NO_WARNINGS": "1"},
        )
        if node_result.returncode != 0:
            bounded = (
                node_result.stderr
                or node_result.stdout
                or "external-agent routing regression failed"
            ).strip()
            if len(bounded) > 6000:
                bounded = bounded[-6000:]
            raise ValueError("Runtime release regression gate failed:" + chr(10) + bounded)

    return {
        "status": "passed",
        "tests": list(RUNTIME_RELEASE_REGRESSION_TESTS),
        "node_tests": list(RUNTIME_RELEASE_NODE_REGRESSION_TESTS),
    }


def _promote_staged_install(stage: Path, target: Path) -> None:
    backup = target.parent / f".{target.name}.backup-{next(tempfile._get_candidate_names())}"
    had_target = target.exists() or target.is_symlink()
    if had_target:
        os.replace(target, backup)
    try:
        os.replace(stage, target)
    except Exception:
        if had_target and (backup.exists() or backup.is_symlink()):
            os.replace(backup, target)
        raise
    if had_target and (backup.exists() or backup.is_symlink()):
        _remove_path(backup)


def install_skill(
    source: str | Path,
    target: str | Path,
    *,
    summary: str,
    impact: str,
    stop_condition: str,
    previous_revision: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    source_path = Path(source).expanduser().resolve()
    target_path = Path(target).expanduser().resolve()
    _ensure_single_skill_install(target_path)
    if impact not in IMPACTS:
        raise ValueError("impact must be none or live_assignments")
    if not summary.strip() or not stop_condition.strip():
        raise ValueError("summary and stop_condition are required")
    if source_path == target_path or source_path in target_path.parents:
        raise ValueError("target must be outside the source repository")
    if _git(source_path, "status", "--porcelain=v1", "--untracked-files=no"):
        raise ValueError("source repository must be tracked-clean before installation")

    revision = _git(source_path, "rev-parse", "HEAD")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    prior_manifest = _read_manifest(target_path / MANIFEST_NAME)
    prior_files = prior_manifest.get("files", {}) if isinstance(prior_manifest.get("files"), dict) else {}
    installed_revision = str(prior_manifest.get("revision", "")).strip() or None
    explicit_previous_revision = str(previous_revision or "").strip() or None
    if (
        installed_revision
        and explicit_previous_revision
        and explicit_previous_revision != installed_revision
    ):
        raise ValueError(
            "previous revision override does not match the installed manifest revision: "
            f"{explicit_previous_revision} != {installed_revision}"
        )
    prior_revision = installed_revision or explicit_previous_revision
    upgrade_lineage = _verify_upgrade_lineage(source_path, prior_revision, revision)
    installed_web_runtime = (
        "scripts/web_agent_execution.py" in prior_files
        or (target_path / "scripts" / "web_agent_execution.py").is_file()
    )
    release_regressions = _verify_runtime_release_regressions(
        source_path,
        revision,
        required=installed_web_runtime,
    )

    stage = Path(tempfile.mkdtemp(prefix=f".{target_path.name}.stage-", dir=target_path.parent))
    try:
        # The installed skill directory is machine-owned. Build it solely from the frozen
        # revision so incomplete/legacy manifests cannot preserve stale executable files.
        tracked = _materialize_revision(source_path, revision, stage)
        hashes = {relative: _sha256(stage / relative) for relative in tracked}
        installed_at = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
        manifest: dict[str, Any] = {
            "schema_version": 1,
            "product_name": PRODUCT_NAME,
            "skill_id": SKILL_ID,
            "product_slug": PRODUCT_SLUG,
            "legacy_skill_ids": list(LEGACY_SKILL_IDS),
            "revision": revision,
            "previous_revision": prior_revision,
            "upgrade_lineage": upgrade_lineage,
            "release_regressions": release_regressions,
            "installed_at": installed_at,
            "source_root": str(source_path),
            "summary": summary.strip(),
            "impact": impact,
            "stop_condition": stop_condition.strip(),
            "changed_files": _changed_files(source_path, prior_revision, revision, tracked),
            "capabilities": detect_host_capabilities(skill_root=target_path),
            "files": hashes,
        }
        _write_json_atomic(stage / MANIFEST_NAME, manifest)
        for relative, expected in hashes.items():
            if _sha256(stage / relative) != expected:
                raise ValueError(f"staged file hash mismatch: {relative}")
        _promote_staged_install(stage, target_path)
    finally:
        if stage.exists():
            shutil.rmtree(stage)

    for relative, expected in hashes.items():
        if _sha256(target_path / relative) != expected:
            raise ValueError(f"installed file hash mismatch: {relative}")
    return manifest



def _snapshot_path(path: Path, backup_root: Path, label: str) -> dict[str, Any]:
    path = path.expanduser().resolve(strict=False)
    exists = path.exists() or path.is_symlink()
    state: dict[str, Any] = {"exists": exists, "path": str(path), "kind": "missing", "backup": None}
    if not exists:
        return state
    backup = backup_root / label
    if path.is_symlink():
        state.update({"kind": "symlink", "target": os.readlink(path)})
    elif path.is_dir():
        shutil.copytree(path, backup, symlinks=True)
        state.update({"kind": "dir", "backup": str(backup)})
    else:
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup, follow_symlinks=False)
        state.update({"kind": "file", "backup": str(backup)})
    return state


def _restore_snapshot(state: dict[str, Any]) -> None:
    path = Path(str(state["path"]))
    if path.exists() or path.is_symlink():
        _remove_path(path)
    if not state.get("exists"):
        return
    kind = state.get("kind")
    path.parent.mkdir(parents=True, exist_ok=True)
    if kind == "symlink":
        os.symlink(str(state["target"]), path)
    elif kind == "dir":
        shutil.copytree(Path(str(state["backup"])), path, symlinks=True)
    elif kind == "file":
        shutil.copy2(Path(str(state["backup"])), path, follow_symlinks=False)
    else:
        raise ValueError(f"unsupported snapshot kind: {kind}")


def _rollback_install_transaction(states: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    for state in reversed(states):
        try:
            _restore_snapshot(state)
        except (OSError, ValueError) as exc:
            errors.append(f"{state.get('path')}: {exc}")
    return errors


def _install_resource_lock_paths(
    target: Path, hooks: Path, zshenv: Path, health_service_plist: Path | None = None
) -> list[Path]:
    paths = [
        target.parent / f".{target.name}.install.lock",
        hooks.parent / f".{hooks.name}.adaptive-agent-runtime.lock",
        zshenv.parent / f".{zshenv.name}.adaptive-agent-runtime.lock",
    ]
    if health_service_plist is not None:
        paths.append(
            health_service_plist.parent
            / f".{health_service_plist.name}.adaptive-agent-runtime.lock"
        )
    return sorted(set(path.resolve(strict=False) for path in paths), key=lambda item: str(item))


def _acquire_install_resource_locks(paths: list[Path]):
    handles = []
    try:
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = path.open("a+")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                handle.close()
                raise
            handles.append(handle)
        return handles
    except Exception:
        for handle in reversed(handles):
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
        raise


def _release_install_resource_locks(handles) -> None:
    for handle in reversed(handles):
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install an exact Adaptive Agent Runtime revision with manifest and host-adapter evidence.")
    parser.add_argument("--source", required=True)
    parser.add_argument("--target")
    parser.add_argument("--summary", required=True)
    parser.add_argument("--impact", required=True, choices=sorted(IMPACTS))
    parser.add_argument("--stop-condition", required=True)
    parser.add_argument("--previous-revision")
    parser.add_argument("--no-configure-host-adapters", action="store_true")
    parser.add_argument("--codex")
    parser.add_argument("--ai-bridge", default=str(DEFAULT_AI_BRIDGE_EXECUTABLE))
    parser.add_argument("--hooks-file", default=str(DEFAULT_CODEX_HOOKS))
    parser.add_argument("--zshenv-file", default=str(DEFAULT_ZSHENV))
    parser.add_argument("--health-service-plist", default=str(DEFAULT_WEB_AGENT_HEALTH_PLIST))
    parser.add_argument("--controller-registry", default=str(DEFAULT_CONTROLLER_REGISTRY))
    parser.add_argument("--no-configure-runtime-services", action="store_true")
    args = parser.parse_args(argv)

    try:
        selected_target = Path(args.target).expanduser() if args.target else default_install_target()
    except ValueError as error:
        print(f"adaptive-agent-runtime-install: blocked: {error}")
        return 1
    target_path = selected_target.resolve(strict=False)
    hooks_path = Path(args.hooks_file).expanduser().resolve(strict=False)
    zshenv_path = Path(args.zshenv_file).expanduser().resolve(strict=False)
    health_service_path = Path(args.health_service_plist).expanduser().resolve(strict=False)
    lock_paths = _install_resource_lock_paths(
        target_path,
        hooks_path,
        zshenv_path,
        None if args.no_configure_runtime_services else health_service_path,
    )
    try:
        try:
            install_locks = _acquire_install_resource_locks(lock_paths)
        except BlockingIOError:
            print(f"adaptive-agent-runtime-install: blocked: another installer is active for shared install resources: {target_path}")
            return 1
        if args.no_configure_host_adapters and args.no_configure_runtime_services:
            try:
                manifest = install_skill(
                    args.source, target_path, summary=args.summary, impact=args.impact,
                    stop_condition=args.stop_condition, previous_revision=args.previous_revision,
                )
            except (OSError, ValueError, subprocess.CalledProcessError) as error:
                print(f"adaptive-agent-runtime-install: blocked: {error}")
                return 1
            print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
            return 0
        return _run_install_transaction(
            args, target_path, hooks_path, zshenv_path, health_service_path
        )
    finally:
        if 'install_locks' in locals():
            _release_install_resource_locks(install_locks)


def _run_install_transaction(
    args,
    target_path: Path,
    hooks_path: Path,
    zshenv_path: Path,
    health_service_path: Path,
) -> int:
    with tempfile.TemporaryDirectory(prefix="adaptive-agent-runtime-install-rollback-") as backup_dir:
        backup_root = Path(backup_dir)
        snapshots = [_snapshot_path(target_path, backup_root, "target")]
        if not args.no_configure_host_adapters:
            snapshots.extend([
                _snapshot_path(hooks_path, backup_root, "hooks"),
                _snapshot_path(zshenv_path, backup_root, "zshenv"),
            ])
        prior_health_snapshot = None
        if not args.no_configure_runtime_services:
            prior_health_snapshot = _snapshot_path(
                health_service_path, backup_root, "web-agent-health-plist"
            )
            snapshots.append(prior_health_snapshot)
        service_loaded = False
        try:
            manifest = install_skill(
                args.source, target_path, summary=args.summary, impact=args.impact,
                stop_condition=args.stop_condition, previous_revision=args.previous_revision,
            )
            if not args.no_configure_runtime_services:
                service = configure_runtime_services(
                    target_path,
                    health_service_plist=health_service_path,
                    registry_path=args.controller_registry,
                )
                service_loaded = service.get("configured") is True
            if not args.no_configure_host_adapters:
                configure_host_adapters(
                    target_path,
                    codex_executable=args.codex,
                    ai_bridge_executable=args.ai_bridge,
                    hooks_file=hooks_path,
                    zshenv_file=zshenv_path,
                )
            manifest["capabilities"] = detect_host_capabilities(
                codex_executable=args.codex,
                ai_bridge_executable=args.ai_bridge,
                hooks_file=hooks_path,
                zshenv_file=zshenv_path,
                skill_root=target_path,
                health_service_plist=health_service_path,
            )
            _write_json_atomic(target_path / MANIFEST_NAME, manifest)
        except (OSError, ValueError, json.JSONDecodeError, subprocess.CalledProcessError) as error:
            if service_loaded:
                _unload_web_agent_health_service(health_service_path)
            rollback_errors = _rollback_install_transaction(snapshots)
            if (
                isinstance(prior_health_snapshot, dict)
                and prior_health_snapshot.get("exists")
                and health_service_path.is_file()
            ):
                try:
                    _load_web_agent_health_service(health_service_path)
                except OSError as reload_error:
                    rollback_errors.append(f"health service reload: {reload_error}")
            suffix = f"; rollback errors: {'; '.join(rollback_errors)}" if rollback_errors else ""
            print(f"adaptive-agent-runtime-install: blocked and rolled back: {error}{suffix}")
            return 1

    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
