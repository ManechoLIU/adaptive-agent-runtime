#!/usr/bin/env python3
"""Install one exact Adaptive Agent Runtime revision with a machine-readable manifest."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import plistlib
import re
import shutil
import subprocess
import tempfile
import shlex
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

try:
    import controller_target_guard as target_guard
except ModuleNotFoundError:
    from scripts import controller_target_guard as target_guard

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
DEFAULT_CONTROLLER_RUNTIME_HEARTBEAT = (
    Path.home() / ".codex" / "state" / "adaptive-delivery-web-agent-health" / "heartbeat.json"
)
CONTROLLER_RUNTIME_SUPERVISOR_CONTRACT = "host_neutral_controller_runtime_v1"
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
    "tests.test_assignment_runtime.ExternalFailureEvidencePersistenceTests."
    "test_terminal_persists_external_failure_class_retry_safety_and_details",
    "tests.test_assignment_runtime.ReviewerRuntimeContractTests."
    "test_reviewer_terminal_persists_structured_review_status",
    "tests.test_assignment_runtime.ReviewerRuntimeContractTests."
    "test_reviewer_terminal_rejects_review_status_that_conflicts_with_delivery",
    "tests.test_assignment_runtime.ReviewerRuntimeContractTests."
    "test_reviewer_infra_status_requires_unresolved_delivery_and_no_verdict",
    "tests.test_reviewer_supervisor.ReviewerSupervisorRoutingTests."
    "test_web_controller_review_does_not_launch_codex_directly",
    "tests.test_reviewer_supervisor.ReviewerSupervisorWebHandoffTests."
    "test_web_review_emits_canonical_dispatch_request_without_codex",
    "tests.test_reviewer_supervisor.ReviewerSupervisorWebHandoffTests."
    "test_web_review_finalizes_only_from_canonical_runtime_reviewer_lease",
    "tests.test_web_reentry_adapter.WebReentryContinuationRegressionTests."
    "test_transient_web_reentry_failure_rearms_existing_continuation_supervisor",
    "tests.test_web_lifecycle_bridge.WebAutoStopSupervisorCoalescingTests."
    "test_same_receipt_live_supervisor_is_coalesced",
    "tests.test_web_lifecycle_bridge.WebAutoStopSupervisorCoalescingTests."
    "test_current_token_web_rearm_hands_off_with_force_rearm_proof",
    "tests.test_web_lifecycle_bridge.WebAutoStopSupervisorCoalescingTests."
    "test_stale_supervisor_token_exits_without_running_impl",
    "tests.test_web_lifecycle_bridge.WebAutoStopSupervisorCoalescingTests."
    "test_stale_supervisor_cannot_native_wake_after_supersession",
    "tests.test_web_lifecycle_bridge.WebAutoStopSupervisorCoalescingTests."
    "test_execute_native_resume_stale_supervisor_token_blocks_process_launch",
    "tests.test_web_lifecycle_bridge.WebHostNativeWakeIsolationTests."
    "test_stale_web_host_with_only_desktop_current_target_resumes_same_controller_desktop",
    "tests.test_controller_target_guard.ControllerTargetGuardTests."
    "test_claim_controller_host_desktop_after_web_increments_one_cross_host_generation",
    "tests.test_web_lifecycle_bridge.WebLocalReentryIntegrationTests."
    "test_direct_wake_rejects_confirmed_web_result_after_desktop_handoff",
    "tests.test_web_lifecycle_bridge.WebLocalReentryIntegrationTests."
    "test_desktop_result_cannot_persist_or_rearm_after_web_handoff",
    "tests.test_web_lifecycle_bridge.WebLifecycleBridgeTests."
    "test_dispatch_event_result_treats_decision_block_as_logical_yield_rejection",
    "tests.test_web_lifecycle_bridge.WebLifecycleBridgeTests."
    "test_production_bridge_has_no_trusted_web_attestation_verifier",
    "tests.test_web_lifecycle_bridge.WebLifecycleBridgeTests."
    "test_registered_web_verifier_timeout_covers_product_host_request_budget",
    "tests.test_web_lifecycle_bridge.WebLifecycleBridgeTests."
    "test_registered_web_verifier_classifies_frame_tree_timeout_as_transient",
    "tests.test_web_lifecycle_bridge.WebLifecycleBridgeTests."
    "test_registered_web_verifier_classifies_exact_target_ambiguous_as_transient",
    "tests.test_agent_target_resolution.LogicalAgentTargetResolutionTests."
    "test_identity_contract_directly_supports_controller_agent_reviewer_and_runtime_repair_agent",
    "tests.test_agent_target_resolution.LogicalAgentTargetResolutionTests."
    "test_verified_execution_target_is_generic_and_carries_double_generation_fence",
    "tests.test_agent_target_resolution.LogicalAgentTargetResolutionTests."
    "test_verified_execution_target_rejects_wrong_logical_agent_and_stale_fences",
    "tests.test_agent_target_resolution.LogicalAgentTargetResolutionTests."
    "test_resolution_status_model_is_generic_and_requires_verified_target_only_for_verified_state",
    "tests.test_agent_target_resolution.LogicalAgentExecutionTurnTests."
    "test_verified_execution_turn_is_generic_and_stable_across_target_generation_rotation",
    "tests.test_agent_target_resolution.LogicalAgentExecutionTurnTests."
    "test_verified_execution_turn_rejects_wrong_agent_target_or_invocation",
    "tests.test_governance.WebMachineTurnLifecycleTests."
    "test_new_machine_web_turn_resets_old_trace_overflow",
    "tests.test_governance.WebMachineTurnLifecycleTests."
    "test_same_machine_web_turn_does_not_reset_existing_trace_or_overflow",
    "tests.test_governance.WebMachineTurnLifecycleTests."
    "test_new_machine_web_turn_with_inflight_tool_fails_closed_without_hiding_old_trace",
    "tests.test_governance.WebMachineTurnLifecycleTests."
    "test_unverified_web_session_start_cannot_rotate_turn",
    "tests.test_governance.WebMachineTurnLifecycleTests."
    "test_web_post_tool_cannot_start_next_turn_without_session_boundary",
    "tests.test_governance.UnboundWebPostToolIsolationTests."
    "test_unverified_web_post_tool_without_turn_id_cannot_mutate_active_turn",
    "tests.test_governance.WebMachineTurnLifecycleTests."
    "test_direct_host_turn_cannot_abandon_active_runtime_fallback_lease",
    "tests.test_governance.WebMachineTurnLifecycleTests."
    "test_verified_web_event_rejects_stale_target_and_ownership_fences",
    "tests.test_governance.WebMachineTurnLifecycleTests."
    "test_verified_web_event_rejects_historical_web_target",
    "tests.test_governance.WebMachineTurnLifecycleTests."
    "test_multiple_web_turns_under_limit_do_not_accumulate_overflow_but_single_turn_still_does",
    "tests.test_controller_target_guard.ControllerTargetGuardTests."
    "test_verified_logical_agent_target_projects_controller_and_defers_other_agent_ownership",
    "tests.test_web_lifecycle_bridge.WebLifecycleBridgeTests."
    "test_registered_web_verifier_exposes_pinned_current_entry_discovery",
    "tests.test_web_lifecycle_bridge.WebCurrentEntryDiscoveryTests."
    "test_generic_current_entry_discovery_accepts_runtime_repair_agent_verified_target",
    "tests.test_web_lifecycle_bridge.WebCurrentEntryDiscoveryTests."
    "test_session_start_auto_discovers_machine_current_entry_and_allows_controller_actions",
    "tests.test_web_lifecycle_bridge.WebCurrentEntryDiscoveryTests."
    "test_session_start_without_host_current_entry_fails_closed",
    "tests.test_web_lifecycle_bridge.WebCurrentEntryDiscoveryTests."
    "test_session_start_caller_claim_of_real_canonical_conversation_is_not_current_entry_proof",
    "tests.test_web_lifecycle_bridge.WebCurrentEntryDiscoveryTests."
    "test_session_start_same_conversation_different_browser_target_fails_closed",
    "tests.test_web_lifecycle_bridge.WebCurrentEntryDiscoveryTests."
    "test_session_start_caller_claim_cannot_override_different_machine_current_entry",
    "tests.test_web_lifecycle_bridge.WebCurrentEntryDiscoveryTests."
    "test_session_start_historical_alias_discovered_by_host_is_not_restored_as_current",
    "tests.test_web_lifecycle_bridge.WebCurrentEntryDiscoveryTests."
    "test_session_start_active_tab_drift_cannot_change_discovered_invocation_identity",
    "tests.test_web_lifecycle_bridge.WebCurrentEntryDiscoveryTests."
    "test_session_start_stale_current_entry_ownership_generation_fails_closed",
    "tests.test_web_lifecycle_bridge.WebCurrentEntryDiscoveryTests."
    "test_session_start_stale_current_entry_generation_fails_closed",
    "tests.test_web_lifecycle_bridge.WebCurrentEntryDiscoveryTests."
    "test_session_start_machine_current_successor_rotates_same_controller_only",
    "tests.test_web_lifecycle_bridge.WebMachineInvocationTurnBridgeTests."
    "test_current_entry_machine_invocation_builds_generic_verified_execution_turn",
    "tests.test_web_lifecycle_bridge.WebMachineInvocationTurnBridgeTests."
    "test_same_host_invocation_has_stable_turn_id_and_next_invocation_changes_it",
    "tests.test_web_lifecycle_bridge.WebMachineInvocationTurnBridgeTests."
    "test_session_start_same_machine_invocation_preserves_trace_and_next_invocation_resets",
    "tests.test_web_lifecycle_bridge.WebMachineInvocationTurnBridgeTests."
    "test_session_start_without_host_invocation_id_recovers_legacy_overflow_with_runtime_lease",
    "tests.test_web_lifecycle_bridge.WebMachineInvocationTurnBridgeTests."
    "test_caller_turn_id_cannot_override_host_machine_turn",
    "tests.test_web_lifecycle_bridge.WebMachineInvocationTurnBridgeTests."
    "test_current_entry_rejects_oversized_runtime_invocation_id",
    "tests.test_governance.RuntimeWebTurnLeaseTests."
    "test_legacy_overflow_migrates_once_only_without_inflight",
    "tests.test_governance.RuntimeWebTurnLeaseTests."
    "test_legacy_overflow_with_inflight_cannot_migrate",
    "tests.test_governance.RuntimeWebTurnLeaseTests."
    "test_active_lease_repeated_session_start_is_idempotent",
    "tests.test_governance.RuntimeWebTurnLeaseTests."
    "test_active_lease_cannot_rotate_without_machine_end",
    "tests.test_governance.RuntimeWebTurnLeaseTests."
    "test_ended_lease_allows_next_generation_and_clears_overflow",
    "tests.test_governance.RuntimeWebTurnLeaseTests."
    "test_forged_ended_status_without_machine_end_evidence_cannot_rotate",
    "tests.test_governance.RuntimeWebTurnLeaseTests."
    "test_stale_watcher_cannot_end_newer_lease",
    "tests.test_governance.RuntimeWebTurnMachineTraceAcceptanceTests."
    "test_recovered_web_turn_produces_machine_trace_and_clean_closed_cycle_evidence",
    "tests.test_web_lifecycle_bridge.RuntimeWebTurnEdgeWatcherTests."
    "test_host_unavailable_marks_lease_ended_but_preserves_overflow_until_next_session_start",
    "tests.test_web_lifecycle_bridge.RuntimeWebTurnEdgeWatcherTests."
    "test_active_host_probe_does_not_end_current_runtime_turn",
    "tests.test_web_lifecycle_bridge.RuntimeWebTurnEdgeWatcherTests."
    "test_post_shell_without_host_turn_token_reuses_active_runtime_lease",
    "tests.test_web_lifecycle_bridge.RuntimeWebTurnEdgeWatcherTests."
    "test_host_invocation_token_does_not_orphan_active_fallback_lease",
    "tests.test_web_lifecycle_bridge.RuntimeWebTurnEdgeWatcherTests."
    "test_direct_host_turn_after_ended_fallback_never_leaves_stale_lease",
    "tests.test_web_lifecycle_bridge.RuntimeWebTurnEdgeWatcherTests."
    "test_verified_same_controller_successor_rotates_active_fallback_lease_without_watcher_end",
    "tests.test_web_lifecycle_bridge.RuntimeWebTurnEdgeWatcherTests."
    "test_verified_same_controller_successor_with_inflight_fallback_tool_fails_closed",
    "tests.test_web_lifecycle_bridge.RuntimeWebTurnEdgeWatcherTests."
    "test_successor_rechecks_inflight_under_registry_fence_before_target_rotation",
    "tests.test_web_lifecycle_bridge.RuntimeWebTurnEndClassificationTests."
    "test_only_explicit_generation_end_errors_count_as_turn_end",
    "tests.test_web_lifecycle_bridge.RuntimeWebTurnEndClassificationTests."
    "test_generic_web_host_generation_end_markers_are_explicit_terminal_edges",
    "tests.test_web_lifecycle_bridge.RuntimeWebTurnEndClassificationTests."
    "test_missed_generation_end_edge_never_false_resets_next_active_generation",
    "tests.test_web_lifecycle_bridge.RuntimeWebTurnStaleFenceWatcherTests."
    "test_foreign_current_entry_does_not_end_or_clear_current_lease",
    "tests.test_web_lifecycle_bridge.RuntimeWebTurnStaleFenceWatcherTests."
    "test_target_generation_change_does_not_end_current_lease",
    "tests.test_web_lifecycle_bridge.RuntimeWebTurnWatcherSpawnTests."
    "test_detached_web_turn_watcher_does_not_use_unreaped_popen",
    "tests.test_install_skill.InstallCapabilityTests."
    "test_identity_capability_report_exposes_runtime_current_entry_host_contract",
    "tests.test_web_lifecycle_bridge.WebLifecycleBridgeTests."
    "test_registered_web_verifier_loads_pinned_external_runtime_host_cli",
    "tests.test_web_lifecycle_bridge.WebLifecycleBridgeTests."
    "test_registered_web_verifier_exposes_pinned_host_submit_adapter",
    "tests.test_web_lifecycle_bridge.WebLifecycleBridgeTests."
    "test_registered_web_verifier_rechecks_bundle_before_each_execution",
    "tests.test_web_lifecycle_bridge.WebHostNativeWakeIsolationTests."
    "test_registered_external_web_host_submit_adapter_is_used_without_caller_injection",
    "tests.test_web_lifecycle_bridge.WebLocalReentryIntegrationTests."
    "test_detached_supervisor_uses_registered_host_submit_adapter_for_strong_web_target",
    "tests.test_web_lifecycle_bridge.WebLocalReentryIntegrationTests."
    "test_strong_host_confirmed_submit_waits_without_rearm",
    "tests.test_web_lifecycle_bridge.WebContinuationSupervisorBootstrapTests."
    "test_retry_exhausted_rearms_after_host_delivery_fingerprint_change",
    "tests.test_web_lifecycle_bridge.WebContinuationSupervisorBootstrapTests."
    "test_retry_exhausted_rearms_after_controller_fence_change_same_host_fingerprint",
    "tests.test_web_lifecycle_bridge.WebContinuationSupervisorBootstrapTests."
    "test_ensure_supervisor_uses_new_receipt_after_controller_fence_change",
    "tests.test_web_lifecycle_bridge.WebLocalReentryIntegrationTests."
    "test_retry_exhausted_persists_host_delivery_fingerprint",
    "tests.test_web_lifecycle_bridge.WebContinuationSupervisorBootstrapTests."
    "test_confirmed_or_result_unknown_never_rearm_for_host_fingerprint_change",
    "tests.test_web_lifecycle_bridge.WebContinuationSupervisorBootstrapTests."
    "test_terminal_rule_delivery_blocks_bootstrap_across_fingerprint_changes",
    "tests.test_web_lifecycle_bridge.WebContinuationSupervisorBootstrapTests."
    "test_non_rule_delivery_key_uses_wake_generation_with_current_rule_snapshot",
    "tests.test_web_lifecycle_bridge.WebLocalReentryIntegrationTests."
    "test_transient_web_reentry_retry_budget_exhausts_without_rearm",
    "tests.test_web_lifecycle_bridge.WebAutoStopSupervisorCoalescingTests."
    "test_same_terminal_receipt_cannot_be_rescheduled",
    "tests.test_web_lifecycle_bridge.WebLocalReentryIntegrationTests."
    "test_detached_supervisor_retries_transient_registered_host_attestation_failure",
    "tests.test_web_lifecycle_bridge.WebLifecycleBridgeTests."
    "test_malformed_registered_web_verifier_config_fails_closed_without_manual_fallback",
    "tests.test_web_lifecycle_bridge.WebLifecycleBridgeTests."
    "test_browser_tab_receipt_cannot_recover_an_unverified_web_session",
    "tests.test_web_lifecycle_bridge.WebLifecycleBridgeTests."
    "test_replace_web_session_bootstrap_rotates_target_and_manual_lease_without_host_attestation",
    "tests.test_web_lifecycle_bridge.WebLifecycleBridgeTests."
    "test_manual_web_mutations_cannot_downgrade_host_attested_current_target",
    "tests.test_web_lifecycle_bridge.WebLifecycleBridgeTests."
    "test_same_controller_web_recovery_cannot_replace_different_host_attested_current_target",
    "tests.test_web_lifecycle_bridge.StrongWebSuccessorHandoffTests."
    "test_authorize_web_successor_records_only_fenced_fresh_session",
    "tests.test_web_lifecycle_bridge.StrongWebSuccessorHandoffTests."
    "test_authorize_web_successor_cli_does_not_rotate_target",
    "tests.test_web_lifecycle_bridge.StrongWebSuccessorHandoffTests."
    "test_authorized_strong_web_successor_rotates_target_and_ownership_once",
    "tests.test_web_lifecycle_bridge.StrongWebSuccessorHandoffTests."
    "test_strong_web_successor_expired_authorization_does_not_call_verifier",
    "tests.test_web_lifecycle_bridge.StrongWebSuccessorHandoffTests."
    "test_strong_web_successor_rechecks_target_generation_after_attestation",
    "tests.test_web_lifecycle_bridge.WebLifecycleBridgeTests."
    "test_legacy_quarantined_target_keeps_manual_replacement_exit",
    "tests.test_web_lifecycle_bridge.WebLifecycleBridgeTests."
    "test_replace_web_session_rejects_unapproved_session_and_stale_generation",
    "tests.test_web_lifecycle_bridge.WebLifecycleBridgeTests."
    "test_replace_same_web_target_is_idempotent_and_unbind_tombstones_without_losing_alias_history",
    "tests.test_web_lifecycle_bridge.WebLifecycleBridgeTests."
    "test_same_controller_web_recovery_does_not_rotate_manual_resume_lease",
    "tests.test_web_lifecycle_bridge.WebLifecycleBridgeTests."
    "test_session_start_verified_target_does_not_rotate_manual_resume_lease",
    "tests.test_web_lifecycle_bridge.WebLifecycleBridgeTests."
    "test_historical_alias_cannot_recover_even_with_trusted_verifier",
    "tests.test_web_lifecycle_bridge.WebLifecycleBridgeTests."
    "test_unbound_chat_cannot_recover_even_with_trusted_verifier",
    "tests.test_web_lifecycle_bridge.WebLifecycleBridgeTests."
    "test_zshenv_exit_bridge_executes_and_preserves_exit_precedence",
    "tests.test_web_lifecycle_bridge.WebLifecycleComputerLeaseTests."
    "test_audit_once_never_uses_manual_resume_lease_as_caller_identity",
    "tests.test_install_skill.InstallCapabilityTests."
    "test_installer_web_bridge_preserves_shell_and_lifecycle_exit_precedence",
    "tests.test_install_skill.ProjectContextHookInstallationTests."
    "test_runtime_hooks_keep_trust_stable_legacy_indices",
    "tests.test_install_skill.ProjectContextHookInstallationTests."
    "test_shifted_runtime_hook_groups_migrate_back_without_moving_user_groups",
    "tests.test_install_skill.HostAdapterInstallationTests."
    "test_configure_host_adapters_can_update_codex_hooks_without_touching_ai_bridge",
    "tests.test_install_skill.HostAdapterInstallationTests."
    "test_install_cli_skip_ai_bridge_never_rolls_back_concurrent_zshenv_update",
    "tests.test_install_skill.WebAgentHealthServiceInstallationTests."
    "test_runtime_service_retires_legacy_per_controller_web_audit_after_new_service_load",
    "tests.test_install_skill.WebAgentHealthServiceInstallationTests."
    "test_runtime_service_load_failure_preserves_legacy_web_audit",
    "tests.test_web_reentry_adapter.WebReentryAdapterTests."
    "test_resolve_reentry_session_strong_host_target_does_not_require_manual_lease",
    "tests.test_web_reentry_adapter.WebReentryAdapterTests."
    "test_resolve_reentry_session_strong_host_target_requires_matching_web_ownership",
    "tests.test_web_reentry_adapter.WebReentryAdapterTests."
    "test_reentry_without_canonical_web_ownership_never_calls_browser",
    "tests.test_web_reentry_adapter.WebReentryAdapterTests."
    "test_reentry_without_explicit_canonical_web_target_never_calls_browser",
    "tests.test_web_reentry_adapter.WebReentryAdapterTests."
    "test_reentry_without_registered_host_origin_verifier_never_calls_browser",
    "tests.test_web_reentry_adapter.WebReentryAdapterTests."
    "test_legacy_browser_attested_target_is_quarantined_before_browser_use",
    "tests.test_web_reentry_adapter.WebReentryAdapterTests."
    "test_submit_holds_registry_fence_against_target_rotation",
    "tests.test_web_reentry_adapter.WebReentryAdapterTests."
    "test_target_generation_change_before_submit_never_types",
    "tests.test_web_lifecycle_bridge.WebLocalReentryIntegrationTests."
    "test_web_supervisor_rejects_confirmed_receipt_for_noncanonical_target",
    "tests.test_web_lifecycle_bridge.WebLocalReentryIntegrationTests."
    "test_builtin_web_reentry_without_registered_origin_verifier_never_calls_browser_adapter",
    "tests.test_web_reentry_adapter.ManualFencedWebReentryTests."
    "test_missing_host_verifier_allows_only_manual_fenced_exact_current_target",
    "tests.test_web_reentry_adapter.ManualFencedWebReentryTests."
    "test_manual_fenced_reentry_rejects_generation_or_lease_mismatch_before_browser",
    "tests.test_web_reentry_adapter.ManualFencedWebReentryTests."
    "test_invalid_registered_host_verifier_never_falls_back_to_manual_fenced_delivery",
    "tests.test_web_reentry_adapter.ManualFencedWebReentryTests."
    "test_manual_fenced_submit_holds_registry_and_lease_fences",
    "tests.test_web_lifecycle_bridge.WebLocalReentryIntegrationTests."
    "test_manual_fenced_direct_wake_uses_builtin_adapter_without_peer_verifier",
    "tests.test_web_lifecycle_bridge.WebLocalReentryIntegrationTests."
    "test_manual_fenced_supervisor_persists_unverified_delivery_evidence",
    "tests.test_web_reentry_adapter.ManualFencedWebReentryTests."
    "test_explicit_bridge_verifier_rejection_never_falls_back_when_module_verifier_missing",
    "tests.test_web_lifecycle_bridge.WebLocalReentryIntegrationTests."
    "test_manual_fenced_direct_wake_passes_bridge_verifier_into_builtin_adapter",
    "tests.test_web_lifecycle_bridge.WebLocalReentryIntegrationTests."
    "test_manual_fenced_supervisor_passes_bridge_verifier_into_builtin_adapter",
    "tests.test_web_lifecycle_bridge.WebLocalReentryIntegrationTests."
    "test_manual_fenced_confirmed_waits_for_progress_without_resubmit",
    "tests.test_web_lifecycle_bridge.WebLocalReentryIntegrationTests."
    "test_waiting_for_controller_progress_does_not_bootstrap_until_machine_facts_change",
    "tests.test_web_lifecycle_bridge.WebContinuationSupervisorBootstrapTests."
    "test_identity_blocked_same_event_and_registry_are_not_bootstrapped_again",
    "tests.test_web_lifecycle_bridge.WebContinuationSupervisorBootstrapTests."
    "test_identity_blocked_event_retries_after_registry_changes",
    "tests.test_web_lifecycle_bridge.WebContinuationSupervisorBootstrapTests."
    "test_nonretryable_web_failure_same_event_and_fence_stays_quiet",
    "tests.test_web_lifecycle_bridge.WebLocalReentryIntegrationTests."
    "test_registered_host_nonretryable_failure_persists_quiet_fence",
    "tests.test_web_lifecycle_bridge.WebLocalReentryIntegrationTests."
    "test_registered_host_result_unknown_persists_quiet_fence",
    "tests.test_web_lifecycle_bridge.WebLocalReentryIntegrationTests."
    "test_confirmed_web_reentry_clears_stale_nonretryable_block_evidence",
    "tests.test_web_lifecycle_bridge.WebHostNativeWakeIsolationTests."
    "test_registered_current_web_adapter_is_fenced_and_host_attested",
    "tests.test_web_lifecycle_bridge.WebHostNativeWakeIsolationTests."
    "test_current_web_adapter_is_not_called_when_pre_delivery_attestation_rejects",
    "tests.test_web_lifecycle_bridge.WebHostNativeWakeIsolationTests."
    "test_current_web_adapter_receipt_must_correlate_origin_call_receipt",
    "tests.test_web_lifecycle_bridge.WebHostNativeWakeIsolationTests."
    "test_current_web_adapter_is_not_called_for_malformed_origin_attestation",
    "tests.test_web_lifecycle_bridge.WebHostNativeWakeIsolationTests."
    "test_registered_current_web_adapter_without_ownership_is_never_called",
    "tests.test_web_reentry_adapter.AiBridgeMcpDiscoveryTests."
    "test_discovery_selects_only_live_loopback_endpoint_and_accepts_url_prefix",
    "tests.test_rule_handshake.RuleHandshakeTests."
    "test_critical_live_runtime_update_requires_real_e2e_after_ack_and_ledger_sync",
    "tests.test_rule_handshake.RuleHandshakeTests."
    "test_forged_live_e2e_acceptance_without_machine_evidence_stays_blocking",
    "tests.test_rule_handshake.RuleHandshakeTests."
    "test_real_confirmed_wake_followed_by_closed_cycle_can_finalize_live_e2e",
    "tests.test_rule_handshake.RuleHandshakeTests."
    "test_failed_live_e2e_does_not_freeze_invalid_wake_snapshot",
    "tests.test_rule_handshake.RuleHandshakeTests."
    "test_live_e2e_debt_survives_later_nonimpacting_install_until_accepted",
    "tests.test_rule_handshake.RuleHandshakeTests."
    "test_live_e2e_rejects_confirmed_wake_that_predates_rule_ack",
    "tests.test_rule_handshake.RuleHandshakeTests."
    "test_live_e2e_rejects_stale_ownership_generation_before_acceptance",
    "tests.test_rule_handshake.RuleHandshakeTests."
    "test_fake_project_chat_cannot_ack_by_claiming_logical_controller_id",
    "tests.test_rule_handshake.RuleHandshakeTests."
    "test_current_web_target_ack_records_exact_source_and_generations",
    "tests.test_rule_handshake.RuleHandshakeTests."
    "test_fake_project_chat_cannot_accept_or_defer_live_e2e",
    "tests.test_rule_handshake.RuleHandshakeTests."
    "test_ack_revalidates_source_fence_immediately_before_persist",
    "tests.test_rule_handshake.RuleHandshakeTests."
    "test_defer_revalidates_source_fence_immediately_before_persist",
    "tests.test_rule_handshake.RuleHandshakeTests."
    "test_accept_revalidates_source_fence_before_freezing_evidence",
    "tests.test_governance.ControllerActionSourcePromptTests."
    "test_rule_ack_prompt_carries_logical_controller_and_actual_execution_source",
    "tests.test_governance.ControllerActionSourcePromptTests."
    "test_live_e2e_accept_prompt_carries_actual_execution_source",
    "tests.test_governance.ControllerActionSourcePromptTests."
    "test_web_bridge_event_uses_actual_web_conversation_as_controller_action_source",
    "tests.test_web_lifecycle_bridge.WebReentryDebounceTests."
    "test_web_confirmed_wake_is_not_debounced_after_same_target_ownership_reclaim",
    "tests.test_web_lifecycle_bridge.WebContinuationSupervisorBootstrapTests."
    "test_dead_or_untracked_active_supervisor_requires_bootstrap",
    "tests.test_web_lifecycle_bridge.WebContinuationSupervisorBootstrapTests."
    "test_live_active_supervisor_does_not_need_duplicate_bootstrap",
    "tests.test_web_agent_health_supervisor.WebAgentHealthSupervisorTests."
    "test_global_health_cycle_refreshes_and_schedules_immediate_rule_update_for_registered_controller",
    "tests.test_web_agent_health_supervisor.WebAgentHealthSupervisorTests."
    "test_global_health_cycle_does_not_schedule_rule_update_without_explicit_current_target",
    "tests.test_web_lifecycle_bridge.WebLifecycleAuditTests."
    "test_rule_wake_target_resolution_fails_closed_instead_of_falling_back_to_logical_controller",
    "tests.test_web_lifecycle_bridge.WebLifecycleAuditTests."
    "test_audit_once_rule_update_uses_guarded_scheduler_and_never_direct_scheduler",
    "tests.test_web_lifecycle_bridge.WebLifecycleAuditTests."
    "test_rule_wake_rejects_explicit_target_without_canonical_execution_ownership",
    "tests.test_web_lifecycle_bridge.WebLifecycleAuditTests."
    "test_rule_wake_rejects_legacy_recovery_target_without_trusted_host_origin_proof",
    "tests.test_web_lifecycle_bridge.WebLifecycleAuditTests."
    "test_auto_native_stop_confirms_host_observed_canonical_target_already_foreground",
    "tests.test_web_lifecycle_bridge.WebLifecycleAuditTests."
    "test_mocked_active_writer_without_host_observation_still_rearms",
    "tests.test_web_lifecycle_bridge.WebLifecycleAuditTests."
    "test_serialized_active_writer_claim_cannot_confirm_already_foreground",
    "tests.test_web_lifecycle_bridge.WebLifecycleAuditTests."
    "test_execute_native_resume_marks_host_observed_active_writer_process_locally",
    "tests.test_web_lifecycle_bridge.WebLifecycleAuditTests."
    "test_auto_native_stop_yields_external_wait_when_desktop_host_reload_is_required",
    "tests.test_web_lifecycle_bridge.WebLifecycleAuditTests."
    "test_desktop_host_reload_gate_requires_exact_armed_zero_sequence_canary",
    "tests.test_web_lifecycle_bridge.WebLifecycleAuditTests."
    "test_desktop_codex_resolution_prefers_the_app_bundled_runtime",
    "tests.test_web_lifecycle_bridge.WebLifecycleAuditTests."
    "test_desktop_codex_resolution_rejects_an_invalid_explicit_override",
    "tests.test_web_lifecycle_bridge.WebLifecycleAuditTests."
    "test_rule_wake_uses_desktop_host_adapter_without_resolving_cli",
    "tests.test_web_lifecycle_bridge.WebLifecycleAuditTests."
    "test_codex_app_server_turn_uses_official_protocol_and_waits_for_completion",
    "tests.test_web_lifecycle_bridge.WebLifecycleAuditTests."
    "test_codex_app_server_active_writer_fails_before_turn_submit",
    "tests.test_web_lifecycle_bridge.WebLifecycleAuditTests."
    "test_codex_app_server_turn_start_response_timeout_is_result_unknown",
    "tests.test_web_lifecycle_bridge.WebLifecycleAuditTests."
    "test_codex_app_server_eof_after_turn_start_confirmation_is_result_unknown",
    "tests.test_web_lifecycle_bridge.WebLifecycleAuditTests."
    "test_desktop_host_resume_uses_app_server_under_target_and_ownership_fence",
    "tests.test_web_lifecycle_bridge.WebLifecycleAuditTests."
    "test_desktop_host_resume_missing_app_server_fails_closed_without_cli_fallback",
    "tests.test_web_lifecycle_bridge.WebLifecycleAuditTests."
    "test_desktop_host_resume_web_ownership_never_starts_app_server",
    "tests.test_web_lifecycle_bridge.WebLifecycleAuditTests."
    "test_execute_native_resume_without_explicit_cli_uses_desktop_host_adapter",
    "tests.test_web_lifecycle_bridge.WebLifecycleAuditTests."
    "test_rule_wake_does_not_require_desktop_runtime_for_web_target",
    "tests.test_web_lifecycle_bridge.WebAutoStopSupervisorCoalescingTests."
    "test_host_neutral_supervisor_omits_missing_desktop_codex_argument",
    "tests.test_web_agent_health_supervisor.WebAgentHealthSupervisorTests."
    "test_rule_wake_defers_host_runtime_resolution_until_target_is_known",
    "tests.test_web_agent_health_supervisor.WebAgentHealthSupervisorTests."
    "test_global_health_cycle_isolates_one_repo_git_failure",
    "tests.test_web_agent_health_supervisor.WebAgentHealthSupervisorTests."
    "test_health_tick_with_runnable_and_no_child_event_arms_same_controller_without_user_message",
    "tests.test_web_agent_health_supervisor.WebAgentHealthSupervisorTests."
    "test_no_canonical_work_does_not_reopen_after_observation_only_turn",
    "tests.test_web_agent_health_supervisor.WebAgentHealthSupervisorTests."
    "test_health_tick_reopens_persisted_non_user_next_action_without_stop_callback",
    "tests.test_terminal_continuation.TerminalContinuationTests."
    "test_terminal_receipt_persists_before_desktop_runtime_is_needed",
    "tests.test_terminal_continuation.PendingTerminalReconcileTests."
    "test_reconcile_pending_discovers_canonical_receipts_without_receipt_cli_argument",
    "tests.test_terminal_continuation.PendingTerminalReconcileTests."
    "test_reconcile_pending_is_idempotent_and_does_not_mutate_lifecycle_or_dispatch_wake",
    "tests.test_terminal_continuation.PendingTerminalReconcileTests."
    "test_reconcile_pending_fails_closed_when_canonical_ownership_is_missing_or_mismatched",
    "tests.test_terminal_continuation.PendingTerminalReconcileTests."
    "test_reconcile_pending_cli_has_no_receipt_argument_and_never_self_spawns",
    "tests.test_terminal_continuation.PendingTerminalReconcileTests."
    "test_reconcile_pending_classifies_legacy_assignment_without_weakening_current_lease_checks",
    "tests.test_terminal_continuation.PendingTerminalReconcileTests."
    "test_reconcile_pending_rejects_lifecycle_change_before_publish",
    "tests.test_terminal_continuation.PendingTerminalReconcileTests."
    "test_reconcile_pending_accepts_independent_desktop_target_and_ownership_generations",
    "tests.test_terminal_continuation.PendingTerminalReconcileTests."
    "test_reconcile_pending_hashes_same_bytes_it_parses",
    "tests.test_terminal_continuation.PendingTerminalReconcileTests."
    "test_reconcile_pending_holds_lifecycle_and_registry_fences_through_audit",
    "tests.test_terminal_continuation.PendingTerminalReconcileTests."
    "test_reconcile_pending_holds_runtime_assignment_fence_through_audit",
    "tests.test_terminal_continuation.PendingTerminalReconcileTests."
    "test_reconcile_pending_fingerprint_is_order_independent",
    "tests.test_terminal_continuation.PendingTerminalReconcileTests."
    "test_atomic_audit_writer_handles_concurrent_publication",
    "tests.test_terminal_continuation.ManualControlCycleReconcileTests."
    "test_manual_fenced_control_cycle_reconcile_rejects_untrusted_ai_bridge_receipt",
    "tests.test_terminal_continuation.ManualControlCycleReconcileTests."
    "test_manual_fenced_control_cycle_reconcile_requires_unexpired_matching_manual_lease",
    "tests.test_terminal_continuation.ManualControlCycleReconcileTests."
    "test_immutable_cycle_evidence_rejects_forged_snapshot_hash",
    "tests.test_terminal_continuation.ManualControlCycleReconcileTests."
    "test_immutable_cycle_evidence_requires_terminal_debt_event_type",
    "tests.test_terminal_continuation.ManualControlCycleReconcileTests."
    "test_manual_reconcile_requires_target_lineage_membership",
    "tests.test_terminal_continuation.ManualControlCycleReconcileTests."
    "test_manual_reconcile_rejects_untrusted_ai_bridge_even_with_later_receipt",
    "tests.test_terminal_continuation.ManualControlCycleReconcileTests."
    "test_reconcile_control_cycle_cli_accepts_no_receipt_or_web_session_identity_argument",
    "tests.test_terminal_continuation.ManualControlCycleReconcileTests."
    "test_manual_reconcile_rejects_untrusted_ai_bridge_before_cycle_evidence",
    "tests.test_terminal_continuation.ManualControlCycleReconcileTests."
    "test_manual_reconcile_rejects_receipt_from_before_current_target_rotation",
    "tests.test_terminal_continuation.ManualControlCycleReconcileTests."
    "test_manual_reconcile_never_establishes_idempotence_from_untrusted_ai_bridge",
    "tests.test_terminal_continuation.ManualControlCycleReconcileTests."
    "test_manual_reconcile_rejects_untrusted_ai_bridge_before_event_type",
    "tests.test_terminal_continuation.ManualControlCycleReconcileTests."
    "test_manual_reconcile_closes_only_terminal_debt_and_preserves_current_nonterminal_triggers",
    "tests.test_web_collaboration_continuation.WebCollaborationContinuationRegressionTests."
    "test_regression_parent_already_yielded_then_writer_completed_wakes_same_controller_with_next_runnable",
    "tests.test_web_collaboration_continuation.WebCollaborationContinuationRegressionTests."
    "test_completed_reviewer_uses_same_terminal_continuation_path",
    "tests.test_web_collaboration_continuation.WebCollaborationContinuationRegressionTests."
    "test_stale_child_is_second_observed_by_existing_audit_and_wakes_same_controller",
    "tests.test_web_collaboration_continuation.WebCollaborationContinuationRegressionTests."
    "test_duplicate_terminal_observation_after_confirmed_continuation_does_not_wake_twice",
    "tests.test_governance.GovernanceTests."
    "test_runtime_terminal_active_row_does_not_consume_dispatch_capacity",
    "tests.test_governance.GovernanceTests."
    "test_runnable_hard_defer_requires_machine_evidence_and_checkpoint",
    "tests.test_governance.GovernanceTests."
    "test_local_hard_defer_still_fills_other_nonconflicting_capacity",
    "tests.test_governance.GovernanceTests."
    "test_identity_degraded_cannot_authorize_stop_while_project_runnable_exists",
    "tests.test_governance.GovernanceTests."
    "test_pending_live_e2e_allows_safe_control_cycle_but_no_new_assignment",
    "tests.test_web_agent_health_supervisor.WebAgentHealthSupervisorTests."
    "test_canonical_runnable_reopens_continuation_without_user_message",
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
    "test_control_loop_stop_rejection_reopens_pending_event_even_if_prior_state_was_closed",
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
    "tests.test_governance.GovernanceTests."
    "test_reviewer_pass_integration_has_mandatory_verify_converge_recompute_successors",
    "tests.test_governance.GovernanceTests."
    "test_known_next_action_enters_canonical_controller_action_projection",
    "tests.test_governance.GovernanceTests."
    "test_continuation_debt_blocks_control_loop_receipt_until_every_action_resolved",
    "tests.test_governance.GovernanceTests."
    "test_durable_terminal_receipt_enters_debt_once_and_disappears_after_consumption",
    "tests.test_governance.GovernanceTests."
    "test_hard_blocked_or_deferred_actions_clear_continuation_debt_and_allow_yield",
    "tests.test_governance.GovernanceTests."
    "test_continuation_debt_fingerprint_escalates_through_existing_recurrence_rules",
    "tests.test_governance.GovernanceTests."
    "test_event_scope_guard_allows_project_wide_dispatch_across_business_lines",
    "tests.test_governance.GovernanceTests."
    "test_event_scope_guard_rejects_cross_task_work_without_project_wide_dispatch_proof",
    "tests.test_governance.GovernanceTests."
    "test_candidate_inventory_batches_ancestry_for_multiple_worktrees",
    "tests.test_desktop_lifecycle_adapter.DesktopLifecycleTurnGateTests."
    "test_successful_receipt_is_invalidated_when_same_turn_continuation_executes",
    "tests.test_desktop_lifecycle_adapter.DesktopLifecycleTurnGateTests."
    "test_status_query_does_not_clear_existing_controller_continuation",
    "tests.test_desktop_lifecycle_adapter.DesktopLifecycleTurnGateTests."
    "test_hard_yield_gate_rejects_declared_next_action_when_work_is_runnable",
    "tests.test_desktop_lifecycle_adapter.DesktopLifecycleTurnGateTests."
    "test_hard_yield_gate_does_not_invent_work_from_status_only_message",
    "tests.test_desktop_lifecycle_adapter.DesktopOutboundLeaseHookTests."
    "test_managed_controller_rejects_unbounded_dev_commands_before_state_write",
    "tests.test_desktop_lifecycle_adapter.DesktopOutboundLeaseHookTests."
    "test_foreground_command_gate_allows_bounded_work_and_skips_unmanaged_sessions",
    "tests.test_desktop_lifecycle_adapter.DesktopOutboundLeaseHookTests."
    "test_controller_without_explicit_surface_uses_its_registered_canonical_repo",
    "tests.test_desktop_lifecycle_adapter.DesktopOutboundLeaseHookTests."
    "test_explicit_controller_surface_rejects_another_checkout",
    "tests.test_desktop_lifecycle_adapter.DesktopOutboundLeaseHookTests."
    "test_run_hook_reuses_one_project_snapshot_for_management_fence",
    "tests.test_desktop_lifecycle_adapter.DesktopOutboundLeaseHookTests."
    "test_current_desktop_user_prompt_persists_confirmed_native_wake",
    "tests.test_goal_display_sync.GoalDisplaySyncTests."
    "test_rolled_happy_path_records_exact_host_sequence_and_binding",
    "tests.test_goal_display_sync.GoalDisplaySyncTests."
    "test_successful_rolled_control_receipt_activates_display_sync_debt",
    "tests.test_goal_display_sync.GoalDisplaySyncTests."
    "test_title_failure_recovers_without_recreating_goal",
    "tests.test_goal_display_sync.GoalDisplaySyncTests."
    "test_host_readback_mismatch_retries_only_the_failed_read",
    "tests.test_goal_display_sync.GoalDisplaySyncTests."
    "test_host_readback_rejects_unrelated_objective_and_split_thread_match",
    "tests.test_goal_display_sync.GoalDisplaySyncTests."
    "test_duplicate_rollover_reuses_completed_receipt",
    "tests.test_goal_display_sync.GoalDisplaySyncTests."
    "test_unavailable_host_tool_marks_receipt_degraded",
    "tests.test_goal_display_sync.GoalDisplaySyncTests."
    "test_missing_host_capability_is_degraded_and_exact_target_change_is_fenced",
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
    "test_route_policy_uses_positive_prefix_grammar_not_negative_phrase_allowlist",
    "tests.test_web_agent_execution.RuntimeOwnedWebRecoveryContractTests."
    "test_route_policy_full_line_grammar_rejects_intervening_and_trailing_semantics",
    "tests.test_web_agent_execution.RuntimeOwnedWebRecoveryContractTests."
    "test_route_policy_requires_canonical_class_then_single_directive_order",
    "tests.test_web_agent_execution.RuntimeOwnedWebRecoveryContractTests."
    "test_route_policy_requires_class_marker_as_first_nonspace_token",
    "tests.test_web_agent_execution.RuntimeOwnedWebRecoveryContractTests."
    "test_route_policy_accepts_explicit_web_class_marker_before_fields",
    "tests.test_web_agent_execution.RuntimeOwnedWebRecoveryContractTests."
    "test_route_policy_rejects_tilde_fenced_route_examples",
    "tests.test_web_agent_execution.StructuredCollaborationTerminalTests."
    "test_public_structured_terminal_ingest_rejects_caller_supplied_observation",
    "tests.test_web_agent_execution.StructuredCollaborationTerminalTests."
    "test_internal_terminal_helper_cannot_accept_fabricated_observation_without_attested_path",
    "tests.test_controller_scoring_hook.ControllerScoringEvaluationTransactionTests."
    "test_current_re_evaluation_does_not_inject_or_accept_historical_score_as_new_result",
    "tests.test_controller_scoring_hook.ControllerScoringEvaluationTransactionTests."
    "test_computed_current_score_requires_exact_transaction_metadata_and_persists_it",
    "tests.test_controller_scoring_hook.ControllerScoringEvaluationTransactionTests."
    "test_historical_total_relabelled_computed_without_fresh_dimension_vector_is_blocked",
    "tests.test_controller_scoring_hook.ControllerScoringEvaluationTransactionTests."
    "test_runtime_rejects_performance_total_that_does_not_match_dimension_vector",
    "tests.test_controller_scoring_hook.ControllerScoringEvaluationTransactionTests."
    "test_fact_change_before_stop_forces_same_flow_re_evaluation_refresh",
    "tests.test_evaluation_transaction.EvaluationTransactionTests."
    "test_historical_72_8_cannot_satisfy_re_evaluate_current_capability",
    "tests.test_evaluation_transaction.EvaluationTransactionTests."
    "test_read_as_computed_violation_starts_same_flow_correction_with_new_evidence",
    "tests.test_project_context_guard.ProjectContextGuardTests."
    "test_new_session_project_governance_question_requires_initialized_current_rules",
    "tests.test_project_context_guard.ProjectContextGuardTests."
    "test_existing_scoring_model_request_must_resolve_real_current_definition",
    "tests.test_project_context_guard.ProjectContextGuardTests."
    "test_source_change_before_stop_fails_closed_and_refreshes_for_same_turn_correction",
    "tests.test_project_context_guard.ProjectContextGuardTests."
    "test_not_found_unknown_token_does_not_authorize_fabricated_definitive_mechanism",
    "tests.test_project_context_guard.ProjectContextGuardTests."
    "test_runtime_state_creation_after_prompt_invalidates_fact_receipt_before_stop",
    "tests.test_project_context_guard.ProjectContextGuardTests."
    "test_nested_correction_refresh_preserves_full_applicable_agents_scope_chain",
    "tests.test_controller_target_guard.ControllerTargetGuardTests."
    "test_identity_projection_marks_unique_controller_with_missing_host_session_as_degraded",
    "tests.test_controller_target_guard.ControllerTargetGuardTests."
    "test_identity_capability_contract_exposes_canonical_projection_and_cli",
    "tests.test_controller_target_guard.ControllerTargetGuardTests."
    "test_identity_projection_keeps_unique_project_controller_when_session_id_unavailable",
    "tests.test_controller_target_guard.ControllerTargetGuardTests."
    "test_identity_projection_verifies_current_desktop_target_without_changing_controller_id",
    "tests.test_controller_target_guard.ControllerTargetGuardTests."
    "test_identity_projection_marks_old_target_stale_but_keeps_project_ownership",
    "tests.test_controller_target_guard.ControllerTargetGuardTests."
    "test_identity_projection_reports_project_controller_conflict_without_silent_selection",
    "tests.test_project_context_guard.ProjectContextGuardTests."
    "test_missing_required_identity_capability_reports_contract_drift_without_revoking_controller",
    "tests.test_project_context_guard.ProjectContextGuardTests."
    "test_contract_drift_does_not_upgrade_foreign_unverified_session_to_degraded",
    "tests.test_project_context_guard.ProjectContextGuardTests."
    "test_project_context_separates_unique_controller_from_unverified_web_session",
    "tests.test_project_context_guard.ProjectContextGuardTests."
    "test_project_context_reports_verified_bound_web_session_without_changing_ownership",
    "tests.test_web_lifecycle_bridge.WebLifecycleBridgeTests."
    "test_session_start_without_host_session_id_reports_existing_controller_not_new_controller",
    "tests.test_web_lifecycle_bridge.WebLifecycleBridgeTests."
    "test_session_start_host_attested_recovery_restores_pending_control_loop_same_controller",
    "tests.test_web_lifecycle_bridge.WebLifecycleBridgeTests."
    "test_same_controller_web_recovery_verifier_exception_degrades_without_revoking_controller",
    "tests.test_web_lifecycle_bridge.WebLifecycleBridgeTests."
    "test_same_controller_web_recovery_rejects_attestation_if_target_generation_changes_before_lock",
    "tests.test_web_lifecycle_bridge.WebLifecycleBridgeTests."
    "test_same_controller_web_recovery_is_idempotent_after_user_reconfirms_ownership",
    "tests.test_web_lifecycle_bridge.WebLifecycleBridgeTests."
    "test_web_recovery_preserves_desktop_target_and_only_advances_web_generation",
    "tests.test_web_lifecycle_bridge.WebAutoStopSupervisorCoalescingTests."
    "test_replacement_can_supersede_while_old_supervisor_waits_in_web_reentry",
    "tests.test_web_lifecycle_bridge.WebAutoStopSupervisorCoalescingTests."
    "test_replacement_can_supersede_while_old_supervisor_waits_in_native_resume",
    "tests.test_web_lifecycle_bridge.WebAutoStopSupervisorCoalescingTests."
    "test_superseded_supervisor_cannot_start_native_recovery_bootstrap_after_resume",
    "tests.test_web_lifecycle_bridge.WebAutoStopSupervisorCoalescingTests."
    "test_superseded_supervisor_cannot_start_recovery_bootstrap_between_ownership_check_and_launch",
    "tests.test_web_lifecycle_bridge.WebAutoStopSupervisorCoalescingTests."
    "test_superseded_supervisor_cannot_replace_native_target_after_recovery_bootstrap",
    "tests.test_web_lifecycle_bridge.WebAutoStopSupervisorCoalescingTests."
    "test_superseded_supervisor_cannot_launch_recovery_resume_after_target_replacement",
    "tests.test_web_agent_events.WebAgentMachineEventSourceTests."
    "test_caller_created_file_inside_codex_session_root_cannot_self_attest",
    "tests.test_web_agent_events.WebAgentMachineEventSourceTests."
    "test_health_supervisor_publishes_diagnostic_source_without_authorizing_it",
    "tests.test_controller_target_guard.ControllerTargetGuardTests."
    "test_host_tool_preparation_persists_full_tuple_and_redacts_receipt_material",
    "tests.test_controller_target_guard.ControllerTargetGuardTests."
    "test_host_tool_preparation_rejects_nonce_receipt_and_execution_replays_after_reload",
    "tests.test_runtime_host_tool_hook.RuntimeHostToolHookTests."
    "test_verified_pre_is_prepared_and_dispatches_same_execution_id",
    "tests.test_runtime_host_tool_hook.RuntimeHostToolHookTests."
    "test_pre_requires_verifier_v2_tool_capability",
    "tests.test_runtime_host_tool_hook.RuntimeHostToolHookTests."
    "test_registered_v2_verifier_cli_is_used_across_the_real_process_boundary",
    "tests.test_runtime_host_tool_hook.RuntimeHostToolHookTests."
    "test_terminal_requires_structured_success_and_closes_after_lifecycle_commit",
    "tests.test_runtime_host_tool_hook.RuntimeHostToolHookTests."
    "test_terminal_without_pre_and_generation_rotation_fail_closed",
    "tests.test_runtime_host_tool_hook.RuntimeHostToolHookTests."
    "test_real_unix_server_correlates_request_and_owns_socket_mode",
    "tests.test_runtime_host_tool_hook.RuntimeHostToolHookTests."
    "test_unix_server_rejects_unsafe_paths_and_bad_frames",
    "tests.test_controller_target_guard.ControllerTargetGuardTests."
    "test_host_tool_terminal_retry_is_exact_and_direct_close_is_disabled",
    "tests.test_governance.DurableHostToolReceiptPersistenceTests."
    "test_lifecycle_commit_fsync_failure_keeps_registry_pending_and_exact_retry_is_single_trace",
    "tests.test_governance.DurableHostToolReceiptPersistenceTests."
    "test_verified_terminal_times_out_hung_snapshot_git_without_closed_or_trace",
    "tests.test_governance.GovernanceTests."
    "test_web_stdout_marker_never_closes_without_private_terminal_commit",
    "tests.test_web_lifecycle_bridge.WebLifecycleBridgeTests."
    "test_loaded_verifier_rejects_writable_members_parents_and_replaced_path",
    "tests.test_web_lifecycle_bridge.ControllerWakeSupervisorTests."
    "test_audit_wake_retry_rejects_same_id_receipt_shape_replacement",
)
RUNTIME_RELEASE_NODE_REGRESSION_TESTS = (
    "heterogeneous frontend and backend tasks stay on Kimi and Grok canonical executors",
    "assignment-bound execute rejects CLI route mismatch before provider spawn",
    "assignment-bound safe fallback requires canonical prior terminal before provider spawn",
    "assignment-bound external start persists exact canonical route contract",
    "short assignment-bound execution reconciles final Git progress before terminal",
    "fresh legacy v1 assignment ACK cannot launch external provider",
    "Grok execution transports prompts through a private prompt file and removes it",
    "oversized Grok reviewer prompt fails before provider spawn with sharding evidence",
    "Grok launch deadline classifies cli_launch_timeout and terminates an unconfirmed process group",
    "Grok post-launch child error preserves provider boundary evidence",
    "Grok first-output deadline starts after launch confirmation",
    "Grok first-output timeout terminates a silent provider attempt",
    "Grok generation stall timeout terminates after structured output stops",
    "Grok normal leader exit reaps surviving process-group descendants before success",
    "Grok absolute deadline kills the entire provider process group",
    "Grok side-effect timeout crosses provider boundary as result_unknown and disables retry",
    "Grok stall timeout persists structured canonical terminal classification",
    "Grok failed attempt uses 0600 prompt file and removes it",
    "oversized non-reviewer Grok prompt fails before spawn without sharding",
    "Grok stderr and assignment heartbeat do not satisfy first stdout progress",
    "Grok unstructured stdout does not satisfy structured first-output progress",
    "Grok 1.0.13 streaming text output satisfies first-output progress then stalls",
    "Grok 1.0.13 streaming thought tool-call and tool-update events count as model progress",
    "Grok 1.0.13 bare type and metadata events cannot spoof model progress",
    "Grok malformed stdout after one structured event does not prevent generation stall",
    "Grok structured metadata stdout does not satisfy model first-output progress",
    "Grok metadata after agent activity does not prevent generation stall",
    "Grok empty tool-call updates do not count as provider progress",
    "Grok misleading type or event fields do not count as ACP model progress",
    "ordinary Grok provider exit and invalid delivery persist durable failure classification",
    "Grok delivery validator binds synthesis evidence to exact assigned shard receipts",
    "Grok delivery validator requires explicit reviewer phase",
    "Grok reviewer shard cannot finalize and synthesis binds exact candidate head",
    "Grok reviewer requires explicit phase and immutable candidate commit",
    "Grok synthesis validates canonical same-candidate shard receipts",
    "Grok cleanup uncertainty preserves cleanup failure class while remaining result unknown",
    "Grok side-effect provider uncertainty elevates a known timeout to result_unknown",
    "Grok cleanup uncertainty is result unknown and not retry safe",
    "cleanup uncertainty is fail closed and result unknown",
    "Grok payload or data wrappers cannot spoof ACP model progress",
    "Grok prompt preparation cleans a temp directory when prompt write fails",
    "Grok prompt write plus cleanup failure is fail closed",
    "cleanup failure preserves prior Grok provider exit evidence",
    "Grok pure-packet Reviewer uses no tools, no planning, structured verdict, and sufficient turn budget",
    "Grok Reviewer structured PASS and FAIL are validated independently from process exit",
    "Grok Reviewer max turns without verdict is REVIEW_MAX_TURNS, never PASS",
    "Grok Reviewer timeout with residual process group is REVIEW_PROCESS_STUCK",
    "Grok work_type=review accepts only structured PASS into canonical acceptance",
    "Grok work_type=review valid FAIL is terminal findings and is never retried into PASS",
    "Grok work_type=review malformed verdict persists REVIEW_OUTPUT_INVALID and no retry",
    "Grok Reviewer stall after stdout closes reaps TERM-resistant relay descendants",
    "ordinary Grok parent SIGTERM also performs bounded process-group cleanup",
    "Grok work_type=review retries transient pre-output failure only once then accepts PASS",
    "Grok Reviewer waits for stdio close before classifying final verdict",
    "Grok 1.0.13 json-schema envelope validates structuredOutput as Reviewer verdict",
    "Grok json-schema envelope rejects conflicting text and structuredOutput",
    "Grok Reviewer returns validated verdict after bounded cleanup even if CLI does not exit",
    "run_external_agent direct execution survives symlinked filesystem path",
)
RUNTIME_HOST_TOOL_HOOK_BUNDLE_FILES = (
    "scripts/agent_target_resolution.py",
    "scripts/assignment_runtime.py",
    "scripts/control_event_guard.py",
    "scripts/controller_health.py",
    "scripts/controller_self_check.py",
    "scripts/controller_state.py",
    "scripts/controller_target_guard.py",
    "scripts/event_scope_guard.py",
    "scripts/goal_display_sync.py",
    "scripts/ledger_consistency_guard.py",
    "scripts/lifecycle_hook.py",
    "scripts/lint_governance.py",
    "scripts/preblock_guard.py",
    "scripts/project_context_guard.py",
    "scripts/project_state.py",
    "scripts/reviewer_supervisor.py",
    "scripts/route_contract.py",
    "scripts/rule_handshake.py",
    "scripts/runtime_host_tool_hook.py",
    "scripts/terminal_continuation.py",
    "scripts/web_agent_events.py",
    "scripts/web_agent_execution.py",
    "scripts/web_agent_health_supervisor.py",
    "scripts/web_lifecycle_bridge.py",
    "scripts/web_reentry_adapter.py",
)
RUNTIME_RELEASE_REQUIRED_FILES = (
    "scripts/controller_runtime_supervisor.py",
    "scripts/web_agent_execution.py",
    "scripts/web_agent_events.py",
    "scripts/web_agent_health_supervisor.py",
    "scripts/web_lifecycle_bridge.py",
    "scripts/web_reentry_adapter.py",
    "scripts/lifecycle_hook.py",
    "scripts/goal_display_sync.py",
    "scripts/ledger_consistency_guard.py",
    "scripts/control_event_guard.py",
    "scripts/event_scope_guard.py",
    "scripts/controller_state.py",
    "scripts/controller_health.py",
    "scripts/controller_self_check.py",
    "scripts/controller_target_guard.py",
    "scripts/agent_target_resolution.py",
    "scripts/controller_scoring_guard.py",
    "scripts/controller_scoring_hook.py",
    "scripts/project_context_guard.py",
    "scripts/project_state.py",
    "scripts/rule_handshake.py",
    "scripts/evaluation_transaction.py",
    "scripts/route_contract.py",
    "scripts/reviewer_supervisor.py",
    "scripts/assignment_lease_guard.py",
    "scripts/assignment_runtime.py",
    "scripts/lint_governance.py",
    "scripts/preblock_guard.py",
    "scripts/run_external_agent.mjs",
    "scripts/runtime_host_tool_hook.py",
    "scripts/terminal_continuation.py",
    "tests/test_web_agent_execution.py",
    "tests/test_reviewer_supervisor.py",
    "tests/test_web_reentry_adapter.py",
    "tests/test_web_collaboration_continuation.py",
    "tests/test_governance.py",
    "tests/test_desktop_lifecycle_adapter.py",
    "tests/test_goal_display_sync.py",
    "tests/test_controller_scoring_hook.py",
    "tests/test_project_context_guard.py",
    "tests/test_rule_handshake.py",
    "tests/test_controller_target_guard.py",
    "tests/test_agent_target_resolution.py",
    "tests/test_web_lifecycle_bridge.py",
    "tests/test_web_agent_health_supervisor.py",
    "tests/test_terminal_continuation.py",
    "tests/test_web_agent_events.py",
    "tests/test_evaluation_transaction.py",
    "tests/test_assignment_runtime.py",
    "tests/test_runtime_host_tool_hook.py",
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
    "same_turn_continuation_invalidated_receipt",
    "stop_observed",
    "post_stop_receipt_latched",
    "post_stop_continuation_invalidated_receipt",
)
DESKTOP_CANARY_MAX_AGE_SECONDS = 24 * 60 * 60


def _desktop_canary_registry_fence_current(
    receipt: dict[str, Any], *, registry_path: Path
) -> bool:
    controller_id = str(receipt.get("controller_id") or "").strip()
    target_session_id = str(receipt.get("execution_target_session_id") or "").strip()
    repo_value = str(receipt.get("canonical_repo") or "").strip()
    registry_value = str(receipt.get("controller_registry_path") or "").strip()
    if not controller_id or not target_session_id or not repo_value or not registry_value:
        return False
    expected_registry_path = registry_path.expanduser().resolve()
    try:
        repo = Path(repo_value).expanduser().resolve()
        if Path(registry_value).expanduser().resolve() != expected_registry_path:
            return False
    except OSError:
        return False
    try:
        with target_guard.locked_registry(expected_registry_path) as registry:
            registered_controller = target_guard.unique_controller_id_for_repo_in_registry(
                repo, registry
            )
            if registered_controller != controller_id:
                return False
            target = target_guard.target_record(
                registry, controller_id=controller_id, host="desktop_codex"
            )
            owner = target_guard.execution_ownership_record(
                registry, controller_id=controller_id
            )
            if target is None or owner is None:
                return False
            target_status, registered_target, target_generation = (
                target_guard.validate_target_record(target, host="desktop_codex")
            )
            ownership_host, ownership_target, ownership_generation = (
                target_guard.validate_execution_ownership_record(owner)
            )
    except (OSError, ValueError, PermissionError):
        return False
    return (
        target_status == "active"
        and registered_target == target_session_id
        and target_generation == receipt.get("target_generation")
        and ownership_host == "desktop_codex"
        and ownership_target == target_session_id
        and ownership_generation == receipt.get("ownership_generation")
    )


def _valid_desktop_canary(
    path: Path,
    *,
    hooks_path: Path,
    skill_root: Path | None,
    controller_registry_path: Path,
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
    target_generation = receipt.get("target_generation")
    ownership_generation = receipt.get("ownership_generation")
    return (
        receipt.get("schema_version") == 6
        and receipt.get("status") == "passed"
        and isinstance(receipt.get("controller_id"), str)
        and bool(receipt.get("controller_id"))
        and isinstance(receipt.get("controller_session_id"), str)
        and bool(receipt.get("controller_session_id"))
        and receipt.get("controller_session_id") == receipt.get("controller_id")
        and isinstance(receipt.get("execution_target_session_id"), str)
        and bool(receipt.get("execution_target_session_id"))
        and isinstance(target_generation, int)
        and not isinstance(target_generation, bool)
        and target_generation > 0
        and isinstance(ownership_generation, int)
        and not isinstance(ownership_generation, bool)
        and ownership_generation > 0
        and isinstance(receipt.get("run_id"), str)
        and len(receipt.get("run_id")) >= 16
        and receipt.get("sequence_index") == len(DESKTOP_CANARY_SEQUENCE)
        and receipt.get("skill_root") == str(skill_root.resolve())
        and receipt.get("hooks_sha256") == hooks_sha256
        and receipt.get("lifecycle_sha256") == lifecycle_sha256
        and receipt.get("controller_target_guard_sha256") == target_guard_sha256
        and observations == list(DESKTOP_CANARY_SEQUENCE)
        and _desktop_canary_registry_fence_current(
            receipt, registry_path=controller_registry_path
        )
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
    registry_path: str | Path = DEFAULT_CONTROLLER_REGISTRY,
) -> Path:
    path = Path(plist_file).expanduser().resolve(strict=False)
    target_path = Path(target).expanduser().resolve()
    script = (target_path / "scripts" / "controller_runtime_supervisor.py").resolve()
    if not script.is_file() or not os.access(script, os.X_OK):
        raise ValueError("installed Controller Runtime supervisor script is missing")
    log_root = Path.home() / ".codex" / "state" / "adaptive-delivery-web-agent-health"
    log_root.mkdir(parents=True, exist_ok=True)
    payload = {
        "Label": WEB_AGENT_HEALTH_LABEL,
        "ProgramArguments": [
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
    expected = (skill_root / "scripts" / "controller_runtime_supervisor.py").resolve()
    if not expected.is_file():
        return False
    try:
        payload = plistlib.loads(path.read_bytes())
    except (OSError, ValueError, plistlib.InvalidFileException):
        return False
    args = payload.get("ProgramArguments") if isinstance(payload, dict) else None
    if (
        not isinstance(args, list)
        or len(args) != 5
        or not all(isinstance(item, str) and item for item in args)
    ):
        return False
    program = payload.get("Program")
    if program is not None and (not isinstance(program, str) or not program):
        return False
    try:
        poll_seconds = float(args[4])
    except ValueError:
        return False
    return (
        payload.get("Label") == WEB_AGENT_HEALTH_LABEL
        and payload.get("RunAtLoad") is True
        and payload.get("KeepAlive") is True
        and os.access(expected, os.X_OK)
        and Path(args[0]).expanduser().resolve(strict=False) == expected
        and (
            program is None
            or Path(program).expanduser().resolve(strict=False) == expected
        )
        and args[1] == "--registry"
        and Path(args[2]).expanduser().is_absolute()
        and args[3] == "--poll-seconds"
        and math.isfinite(poll_seconds)
        and poll_seconds >= 1.0
    )


def _runtime_supervisor_heartbeat_ready(
    path: Path, *, max_age_seconds: float = 90.0
) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        observed = datetime.fromisoformat(
            str(payload.get("observed_at") or "").replace("Z", "+00:00")
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=UTC)
    age = (datetime.now(UTC) - observed.astimezone(UTC)).total_seconds()
    return (
        payload.get("state") == "ready"
        and payload.get("supervisor_contract")
        == CONTROLLER_RUNTIME_SUPERVISOR_CONTRACT
        and 0 <= age <= max_age_seconds
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


LEGACY_WEB_LIFECYCLE_PLIST_GLOBS = (
    "ai.openai.adaptive-delivery.web-lifecycle.*.plist",
    "com.openai.adaptive-delivery.web-lifecycle.*.plist",
)


def _unload_legacy_web_lifecycle_service(plist_path: Path) -> None:
    plist_path = plist_path.expanduser().resolve()
    launchctl = Path("/bin/launchctl")
    if launchctl.is_file():
        subprocess.run(
            [str(launchctl), "bootout", f"gui/{os.getuid()}", str(plist_path)],
            capture_output=True, text=True, check=False,
        )
    plist_path.unlink(missing_ok=True)


def _retire_legacy_web_lifecycle_services(
    launchagents_dir: Path, *, unloader: Any | None = None
) -> list[str]:
    retire = unloader or _unload_legacy_web_lifecycle_service
    candidates: list[Path] = []
    for pattern in LEGACY_WEB_LIFECYCLE_PLIST_GLOBS:
        candidates.extend(launchagents_dir.glob(pattern))
    retired: list[str] = []
    for path in sorted({item.expanduser().resolve() for item in candidates}, key=str):
        retire(path)
        retired.append(str(path))
    return retired


def configure_runtime_services(
    target: str | Path,
    *,
    health_service_plist: str | Path = DEFAULT_WEB_AGENT_HEALTH_PLIST,
    registry_path: str | Path = DEFAULT_CONTROLLER_REGISTRY,
    service_loader: Any | None = None,
    legacy_service_unloader: Any | None = None,
) -> dict[str, Any]:
    target_path = Path(target).expanduser().resolve()
    plist_path = install_web_agent_health_service_plist(
        health_service_plist,
        target_path,
        registry_path=registry_path,
    )
    loader = service_loader or _load_web_agent_health_service
    result = loader(plist_path)
    if not isinstance(result, dict):
        result = {"state": "loaded"}
    retired_legacy_services = _retire_legacy_web_lifecycle_services(
        plist_path.parent, unloader=legacy_service_unloader
    )
    return {
        **result,
        "configured": _health_service_plist_matches(
            plist_path, skill_root=target_path
        ),
        "plist": str(plist_path),
        "retired_legacy_services": retired_legacy_services,
    }


def _machine_web_event_source_ready(path: Path) -> bool:
    try:
        from scripts.web_agent_events import machine_event_source_ready
    except ModuleNotFoundError:
        from web_agent_events import machine_event_source_ready
    return bool(machine_event_source_ready(path=path))


def _read_trusted_runtime_bundle_file(
    path: Path, *, root: Path, maximum: int = 4 * 1024 * 1024
) -> tuple[bytes, str]:
    root = Path(os.path.abspath(os.fspath(root)))
    path = Path(os.path.abspath(os.fspath(path)))
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise PermissionError("Runtime Host hook bundle member escapes the installed root") from exc
    current = path.parent
    while True:
        metadata = current.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise PermissionError("Runtime Host hook bundle parent must be a real directory")
        if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
            raise PermissionError("Runtime Host hook bundle parent owner mismatch")
        if metadata.st_mode & 0o022:
            raise PermissionError("Runtime Host hook bundle parent is group or other writable")
        if current == root:
            break
        if current.parent == current:
            raise PermissionError("Runtime Host hook bundle root is not an ancestor")
        current = current.parent
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if not isinstance(nofollow, int) or nofollow == 0:
        raise PermissionError("Runtime Host hook bundle requires O_NOFOLLOW support")
    descriptor = os.open(path, os.O_RDONLY | nofollow)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise PermissionError("Runtime Host hook bundle member must be a regular file")
        if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
            raise PermissionError("Runtime Host hook bundle member owner mismatch")
        if metadata.st_mode & 0o022:
            raise PermissionError("Runtime Host hook bundle member is group or other writable")
        if metadata.st_size < 0 or metadata.st_size > maximum:
            raise PermissionError("Runtime Host hook bundle member exceeds size limit")
        content = bytearray()
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, min(65536, maximum - len(content) + 1))
            if not chunk:
                break
            content.extend(chunk)
            if len(content) > maximum:
                raise PermissionError("Runtime Host hook bundle member exceeds size limit")
            digest.update(chunk)
        return bytes(content), digest.hexdigest()
    finally:
        os.close(descriptor)


def _trusted_runtime_host_tool_bundle(
    skill_root: Path,
    *,
    expected_files: dict[str, str] | None = None,
    expected_revision: str | None = None,
) -> tuple[str, dict[str, str], dict[str, bytes]]:
    root = Path(os.path.abspath(os.fspath(skill_root)))
    files = expected_files
    revision = str(expected_revision or "").strip()
    if files is None:
        manifest_bytes, _ = _read_trusted_runtime_bundle_file(
            root / MANIFEST_NAME, root=root, maximum=2 * 1024 * 1024
        )
        manifest = json.loads(manifest_bytes.decode("utf-8"))
        files = manifest.get("files") if isinstance(manifest, dict) else None
        revision = str(manifest.get("revision") or "").strip() if isinstance(manifest, dict) else ""
    if not isinstance(files, dict):
        raise PermissionError("Runtime Host hook bundle hash manifest is unavailable")
    if len(revision) != 40 or any(character not in "0123456789abcdef" for character in revision.lower()):
        raise PermissionError("Runtime Host hook bundle Runtime revision is invalid")
    bundle_hashes: dict[str, str] = {}
    bundle_contents: dict[str, bytes] = {}
    for relative in RUNTIME_HOST_TOOL_HOOK_BUNDLE_FILES:
        expected = files.get(relative)
        if (
            not isinstance(expected, str)
            or len(expected) != 64
            or any(character not in "0123456789abcdef" for character in expected.lower())
        ):
            raise PermissionError(f"Runtime Host hook bundle hash is missing: {relative}")
        content, actual = _read_trusted_runtime_bundle_file(root / relative, root=root)
        if actual != expected.lower():
            raise PermissionError(f"Runtime Host hook bundle hash mismatch: {relative}")
        bundle_hashes[relative] = actual
        bundle_contents[relative] = content
    return revision.lower(), bundle_hashes, bundle_contents


def _installed_controller_identity_capability(
    skill_root: Path | None,
    *,
    expected_files: dict[str, str] | None = None,
    expected_revision: str | None = None,
    reported_root: Path | None = None,
) -> dict[str, Any]:
    script = (
        skill_root / "scripts" / "controller_target_guard.py"
        if skill_root is not None
        else Path(__file__).resolve().parent / "controller_target_guard.py"
    )
    bridge = (
        skill_root / "scripts" / "web_lifecycle_bridge.py"
        if skill_root is not None
        else Path(__file__).resolve().parent / "web_lifecycle_bridge.py"
    )
    tool_hook = (
        skill_root / "scripts" / "runtime_host_tool_hook.py"
        if skill_root is not None
        else Path(__file__).resolve().parent / "runtime_host_tool_hook.py"
    )
    try:
        bridge_text = bridge.read_text(encoding="utf-8") if bridge.is_file() else ""
    except OSError:
        bridge_text = ""
    runtime_current_entry_supported = (
        "def discover_current_web_entry_for_logical_agent(" in bridge_text
        and "logical_agent_identity" in bridge_text
        and '"discover_current_entry"' in bridge_text
        and "runtime_host_current_entry_v1" in bridge_text
    )
    runtime_web_turn_supported = (
        "def verified_web_execution_turn_from_current_entry(" in bridge_text
        and "verified_execution_turn" in bridge_text
    )
    runtime_web_turn_edge_supported = (
        "def runtime_web_turn_for_session_start(" in bridge_text
        and "def watch_runtime_web_turn_end(" in bridge_text
        and "runtime_web_turn_lease_v1" in bridge_text
        and "host_current_entry_unavailable" in bridge_text
    )
    bundle_revision: str | None = None
    bundle_hashes: dict[str, str] | None = None
    bundle_contents: dict[str, bytes] = {}
    if skill_root is not None:
        try:
            bundle_revision, bundle_hashes, bundle_contents = _trusted_runtime_host_tool_bundle(
                skill_root,
                expected_files=expected_files,
                expected_revision=expected_revision,
            )
        except (OSError, UnicodeError, ValueError, PermissionError, json.JSONDecodeError):
            bundle_revision = None
            bundle_hashes = None
            bundle_contents = {}
    try:
        tool_hook_text = bundle_contents.get(
            "scripts/runtime_host_tool_hook.py", b""
        ).decode("utf-8")
        trusted_bridge_text = bundle_contents.get(
            "scripts/web_lifecycle_bridge.py", b""
        ).decode("utf-8")
    except UnicodeError:
        tool_hook_text = ""
        trusted_bridge_text = ""
    runtime_host_tool_hook_supported = (
        runtime_current_entry_supported
        and bundle_hashes is not None
        and "runtime_host_verifier_cli_v2" in trusted_bridge_text
        and "verify_tool_pre" in trusted_bridge_text
        and "verify_tool_terminal" in trusted_bridge_text
        and "runtime_host_tool_hook_v1" in tool_hook_text
        and "def handle_request(" in tool_hook_text
        and "def serve_unix_socket(" in tool_hook_text
    )
    output_root = Path(reported_root or skill_root or Path(__file__).resolve().parent.parent)
    tool_hook_output = output_root / "scripts" / "runtime_host_tool_hook.py"
    tool_hook_sha256 = (
        bundle_hashes.get("scripts/runtime_host_tool_hook.py")
        if runtime_host_tool_hook_supported and bundle_hashes is not None
        else None
    )
    tool_hook_bundle_sha256 = (
        {str((output_root / relative).resolve()): digest for relative, digest in bundle_hashes.items()}
        if runtime_host_tool_hook_supported and bundle_hashes is not None
        else None
    )
    host_tool_contract = {
        "host_verifier_protocol": (
            "runtime_host_verifier_cli_v2"
            if runtime_host_tool_hook_supported
            else "runtime_host_verifier_cli_v1" if runtime_current_entry_supported else None
        ),
        "tool_hook_protocol": (
            "runtime_host_tool_hook_v1" if runtime_host_tool_hook_supported else None
        ),
        "tool_hook_path": str(tool_hook_output.resolve()) if runtime_host_tool_hook_supported else None,
        "tool_hook_sha256": tool_hook_sha256,
        "tool_hook_runtime_revision": bundle_revision if runtime_host_tool_hook_supported else None,
        "tool_hook_bundle_sha256": tool_hook_bundle_sha256,
    }
    if not script.is_file():
        return {
            "status": "degraded",
            "configured": False,
            "state": "RUNTIME_CONTRACT_DRIFT",
            "reason": "canonical Controller identity guard is missing",
            "capabilities": [],
            "canonical_identity_cli": "controller_target_guard.py identity",
            "strong_web_binding_available": False,
            "host_attestation": "unavailable",
            "runtime_current_entry_discovery_supported": runtime_current_entry_supported,
            "current_entry_discovery_contract": "runtime_host_current_entry_v1" if runtime_current_entry_supported else None,
            "current_entry_host_operation": "discover_current_entry",
            "host_current_entry_required": runtime_current_entry_supported,
            "runtime_web_turn_identity_supported": runtime_web_turn_supported,
            "verified_execution_turn_contract": "verified_execution_turn_v1" if runtime_web_turn_supported else None,
            "host_current_entry_turn_field_optional": "runtime_invocation_id" if runtime_web_turn_supported else None,
            "runtime_web_turn_edge_fallback_supported": runtime_web_turn_edge_supported,
            "host_schema_change_required_for_trace_rotation": False,
            "machine_turn_end_required_for_trace_rotation": runtime_web_turn_supported,
            **host_tool_contract,
        }
    completed = subprocess.run(
        [sys.executable, str(script), "capabilities"],
        check=False, capture_output=True, text=True, timeout=5,
    )
    if completed.returncode != 0:
        return {
            "status": "degraded",
            "configured": False,
            "state": "RUNTIME_CONTRACT_DRIFT",
            "reason": "canonical Controller identity capability probe failed",
            "capabilities": [],
            "canonical_identity_cli": "controller_target_guard.py identity",
            "strong_web_binding_available": False,
            "host_attestation": "unavailable",
            "runtime_current_entry_discovery_supported": runtime_current_entry_supported,
            "current_entry_discovery_contract": "runtime_host_current_entry_v1" if runtime_current_entry_supported else None,
            "current_entry_host_operation": "discover_current_entry",
            "host_current_entry_required": runtime_current_entry_supported,
            "runtime_web_turn_identity_supported": runtime_web_turn_supported,
            "verified_execution_turn_contract": "verified_execution_turn_v1" if runtime_web_turn_supported else None,
            "host_current_entry_turn_field_optional": "runtime_invocation_id" if runtime_web_turn_supported else None,
            "runtime_web_turn_edge_fallback_supported": runtime_web_turn_edge_supported,
            "host_schema_change_required_for_trace_rotation": False,
            "machine_turn_end_required_for_trace_rotation": runtime_web_turn_supported,
            **host_tool_contract,
        }
    try:
        contract = json.loads(completed.stdout)
    except json.JSONDecodeError:
        contract = {}
    capabilities = contract.get("capabilities") if isinstance(contract, dict) else None
    canonical_cli = contract.get("canonical_identity_cli") if isinstance(contract, dict) else None
    logical_target_contract = (
        contract.get("logical_agent_target_resolution_contract") if isinstance(contract, dict) else None
    )
    verified_target_contract = (
        contract.get("verified_execution_target_contract") if isinstance(contract, dict) else None
    )
    supported_logical_agent_types = (
        contract.get("supported_logical_agent_types") if isinstance(contract, dict) else None
    )
    logical_target_states = (
        contract.get("logical_agent_target_resolution_states") if isinstance(contract, dict) else None
    )
    if not isinstance(capabilities, list) or not all(isinstance(item, str) for item in capabilities):
        capabilities = []
    required = {
        "controller_identity_projection",
        "same_controller_recovery",
        "web_session_binding",
        "target_generation_fence",
        "logical_agent_target_resolution",
        "verified_execution_target_fence",
    }
    missing = sorted(required - set(capabilities))
    expected_agent_types = {"controller", "agent", "reviewer", "runtime_repair_agent"}
    expected_resolution_states = {"VERIFIED", "UNRESOLVED", "STALE", "CONFLICTED"}
    current = (
        not missing
        and canonical_cli == "controller_target_guard.py identity"
        and logical_target_contract == "logical_agent_target_resolution_v1"
        and verified_target_contract == "verified_execution_target_v1"
        and isinstance(supported_logical_agent_types, list)
        and expected_agent_types.issubset(set(supported_logical_agent_types))
        and isinstance(logical_target_states, list)
        and expected_resolution_states.issubset(set(logical_target_states))
    )
    return {
        "status": "enabled" if current else "degraded",
        "configured": current,
        "state": "CURRENT" if current else "RUNTIME_CONTRACT_DRIFT",
        "reason": (
            "canonical Controller identity capability contract verified"
            if current else "installed Controller identity capability contract is incomplete"
        ),
        "capabilities": sorted(set(capabilities)),
        "missing_capabilities": missing,
        "canonical_identity_cli": canonical_cli or "controller_target_guard.py identity",
        "logical_agent_target_resolution_contract": logical_target_contract,
        "verified_execution_target_contract": verified_target_contract,
        "supported_logical_agent_types": (
            sorted(set(supported_logical_agent_types))
            if isinstance(supported_logical_agent_types, list)
            else []
        ),
        "logical_agent_target_resolution_states": (
            sorted(set(logical_target_states))
            if isinstance(logical_target_states, list)
            else []
        ),
        "ownership_resolver_scope": str(contract.get("ownership_resolver_scope") or "controller_registry_only"),
        "automatic_problem_attribution": "post_migration_enhancement",
        "strong_web_binding_available": False,
        "host_attestation": (
            "external_current_entry_required"
            if runtime_current_entry_supported
            else "unavailable"
        ),
        "runtime_current_entry_discovery_supported": runtime_current_entry_supported,
        "current_entry_discovery_contract": "runtime_host_current_entry_v1" if runtime_current_entry_supported else None,
        "current_entry_host_operation": "discover_current_entry",
        "host_current_entry_required": runtime_current_entry_supported,
        "runtime_web_turn_identity_supported": runtime_web_turn_supported,
        "verified_execution_turn_contract": "verified_execution_turn_v1" if runtime_web_turn_supported else None,
        "host_current_entry_turn_field_optional": "runtime_invocation_id" if runtime_web_turn_supported else None,
        "runtime_web_turn_edge_fallback_supported": runtime_web_turn_edge_supported,
        "host_schema_change_required_for_trace_rotation": False,
        "machine_turn_end_required_for_trace_rotation": runtime_web_turn_supported,
        **host_tool_contract,
    }


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
    runtime_supervisor_heartbeat: str | Path = DEFAULT_CONTROLLER_RUNTIME_HEARTBEAT,
    controller_registry: str | Path = DEFAULT_CONTROLLER_REGISTRY,
) -> dict[str, dict[str, Any]]:
    codex_path = Path(codex_executable).expanduser() if codex_executable else None
    if codex_path is None:
        discovered = shutil.which("codex")
        codex_path = Path(discovered) if discovered else None
    bridge_path = Path(ai_bridge_executable).expanduser()
    hooks_path = Path(hooks_file).expanduser()
    zshenv_path = Path(zshenv_file).expanduser()
    desktop_canary_path = Path(desktop_canary_file).expanduser()
    controller_registry_path = Path(controller_registry).expanduser().resolve()
    skill_root_path = Path(skill_root).expanduser().resolve() if skill_root is not None else None
    health_service_path = Path(health_service_plist).expanduser().resolve(strict=False)
    health_service_configured = _health_service_plist_matches(
        health_service_path, skill_root=skill_root_path
    )
    runtime_supervisor_ready = _runtime_supervisor_heartbeat_ready(
        Path(runtime_supervisor_heartbeat).expanduser().resolve(strict=False)
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
    project_context_script = (
        skill_root_path / "scripts" / "project_context_guard.py"
        if skill_root_path is not None else None
    )
    project_context_configured = bool(
        project_context_script
        and project_context_script.is_file()
        and all(
            _hook_event_contains(
                hooks_path, event_name, "project_context_guard.py", skill_root=skill_root_path
            )
            for event_name in {"SessionStart", "UserPromptSubmit", "Stop"}
        )
    )
    canary_valid = _valid_desktop_canary(
        desktop_canary_path,
        hooks_path=hooks_path,
        skill_root=skill_root_path,
        controller_registry_path=controller_registry_path,
    )
    if not codex_available:
        desktop = {
            "status": "blocked", "adapter": "codex-native", "configured": False,
            "reason": "codex executable not detected",
        }
    elif lifecycle_configured and scoring_configured and project_context_configured and canary_valid:
        desktop = {
            "status": "enabled", "adapter": "codex-native", "configured": True,
            "reason": "hooks configured and exact live canary receipt verified",
        }
    elif lifecycle_configured and scoring_configured and project_context_configured:
        desktop = {
            "status": "degraded", "adapter": "codex-native", "configured": True,
            "reason": "hooks configured; exact live canary receipt is missing or stale",
        }
    else:
        desktop = {
            "status": "degraded", "adapter": "codex-native", "configured": False,
            "reason": "codex detected; lifecycle/scoring/project-context hooks are not fully configured",
        }
    desktop["background_continuation"] = (
        "ready"
        if health_service_configured and runtime_supervisor_ready
        else "configured_unverified"
        if health_service_configured
        else "not_configured"
    )
    desktop["background_continuation_ready"] = bool(
        health_service_configured and runtime_supervisor_ready
    )
    desktop["continuation_independent_of_ai_bridge"] = health_service_configured
    goal_display_sync_ready = bool(
        lifecycle_configured
        and skill_root_path is not None
        and (skill_root_path / "scripts" / "goal_display_sync.py").is_file()
    )
    desktop["goal_display_sync"] = (
        "configured_unverified"
        if goal_display_sync_ready
        else "degraded_runtime_hook_unavailable"
    )

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
    web["goal_display_sync"] = "degraded_host_capability_unavailable"
    return {
        "core": {"status": "enabled", "adapter": "adaptive-agent-runtime", "configured": True, "reason": "core governance is host-neutral"},
        "controller_identity": _installed_controller_identity_capability(skill_root_path),
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


def _upsert_hook_handler_in_place(
    entries: list[Any],
    *,
    needle: str,
    handler: dict[str, Any],
    matcher: str | None,
) -> None:
    """Replace a Runtime handler without moving its trust-key group index."""
    found = False
    updated: list[Any] = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
            if needle not in str(entry):
                updated.append(entry)
            continue
        prior_handlers = entry["hooks"]
        remaining: list[Any] = []
        matched_here = False
        for prior_handler in prior_handlers:
            if needle in str(prior_handler):
                matched_here = True
                if not found:
                    remaining.append(dict(handler))
                    found = True
                continue
            remaining.append(prior_handler)
        if not remaining:
            continue
        preserved = dict(entry)
        preserved["hooks"] = remaining
        if matched_here and len(remaining) == 1 and len(prior_handlers) == 1:
            if matcher is None:
                preserved.pop("matcher", None)
            else:
                preserved["matcher"] = matcher
        updated.append(preserved)
    if not found:
        group: dict[str, Any] = {"hooks": [dict(handler)]}
        if matcher is not None:
            group["matcher"] = matcher
        updated.append(group)
    entries[:] = updated


def _runtime_hook_group_role(entry: Any) -> str | None:
    if not isinstance(entry, dict):
        return None
    handlers = entry.get("hooks")
    if not isinstance(handlers, list) or len(handlers) != 1:
        return None
    text = str(handlers[0])
    for role in (
        "lifecycle_hook.py",
        "controller_scoring_hook.py",
        "project_context_guard.py",
    ):
        if role in text:
            return role
    return None


def _stabilize_runtime_hook_group_indices(entries: list[Any]) -> None:
    """Restore the legacy trusted Runtime slots while leaving user slots fixed."""
    slots: list[int] = []
    groups: list[Any] = []
    priority = {
        "lifecycle_hook.py": 0,
        "controller_scoring_hook.py": 1,
        "project_context_guard.py": 2,
    }
    for index, entry in enumerate(entries):
        if _runtime_hook_group_role(entry) is None:
            continue
        slots.append(index)
        groups.append(entry)
    groups.sort(key=lambda entry: priority[_runtime_hook_group_role(entry)])
    for index, entry in zip(slots, groups):
        entries[index] = entry


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
    project_context_command = f"{shlex.quote(str(python))} {shlex.quote(str(target_path / 'scripts' / 'project_context_guard.py'))}"

    project_context_specs = {
        "SessionStart": ("startup|resume|clear|compact", True),
        "UserPromptSubmit": (None, True),
        "Stop": (None, False),
    }
    for event_name, (matcher, inject_context) in project_context_specs.items():
        entries = hooks.setdefault(event_name, [])
        if not isinstance(entries, list):
            raise ValueError(f"{event_name} hooks must be a list")
        handler: dict[str, Any] = {
            "type": "command",
            "command": project_context_command,
            "timeout": 5,
            "statusMessage": "Loading Adaptive Agent Runtime current project facts",
        }
        if inject_context:
            handler["additionalContextLimit"] = 0
        _upsert_hook_handler_in_place(
            entries,
            needle="project_context_guard.py",
            handler=handler,
            matcher=matcher,
        )

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
        handler: dict[str, Any] = {
            "type": "command", "command": lifecycle_command, "timeout": 20, "statusMessage": status,
        }
        if inject_context:
            handler["additionalContextLimit"] = 4096
        _upsert_hook_handler_in_place(
            entries,
            needle="lifecycle_hook.py",
            handler=handler,
            matcher=matcher,
        )

    for event_name, inject_context in (("UserPromptSubmit", True), ("Stop", False)):
        entries = hooks.setdefault(event_name, [])
        if not isinstance(entries, list):
            raise ValueError(f"{event_name} hooks must be a list")
        handler = {
            "type": "command", "command": scoring_command, "timeout": 5,
            "statusMessage": "Enforcing Adaptive Agent Runtime controller scoring model",
        }
        if inject_context:
            handler["additionalContextLimit"] = 0
        _upsert_hook_handler_in_place(
            entries,
            needle="controller_scoring_hook.py",
            handler=handler,
            matcher=None,
        )

    for entries in hooks.values():
        if isinstance(entries, list):
            _stabilize_runtime_hook_group_indices(entries)

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
    configure_ai_bridge: bool = True,
    controller_registry: str | Path = DEFAULT_CONTROLLER_REGISTRY,
) -> dict[str, dict[str, Any]]:
    target_path = Path(target).expanduser().resolve()
    codex_path = Path(codex_executable).expanduser() if codex_executable else None
    if codex_path is None:
        discovered = shutil.which("codex")
        codex_path = Path(discovered) if discovered else None
    if codex_path is not None and codex_path.is_file() and os.access(codex_path, os.X_OK):
        install_codex_hooks(hooks_file, target_path, python_executable=python_executable)
    bridge = Path(ai_bridge_executable).expanduser()
    if configure_ai_bridge and bridge.is_file() and os.access(bridge, os.X_OK):
        install_ai_bridge_zshenv(zshenv_file, target_path, bridge, python_executable=python_executable)
    return detect_host_capabilities(
        codex_executable=codex_path,
        ai_bridge_executable=bridge,
        hooks_file=hooks_file,
        zshenv_file=zshenv_file,
        skill_root=target_path,
        controller_registry=controller_registry,
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


def verify_canonical_release_source(source: str | Path) -> dict[str, Any]:
    """Prove that a formal Runtime release is the clean, published canonical main."""
    source_path = Path(source).expanduser().resolve()
    if _git(source_path, "status", "--porcelain=v1"):
        raise ValueError("canonical Runtime release source must be completely clean")

    branch = _git(source_path, "branch", "--show-current")
    if branch != "main":
        raise ValueError(
            f"formal Runtime installation requires the canonical main branch; found {branch or 'detached HEAD'}"
        )

    revision = _git(source_path, "rev-parse", "HEAD")
    main_revision = _git(source_path, "rev-parse", "refs/heads/main")
    if revision != main_revision:
        raise ValueError("canonical Runtime release HEAD must equal refs/heads/main")

    try:
        upstream_ref = _git(source_path, "rev-parse", "--symbolic-full-name", "@{upstream}")
    except subprocess.CalledProcessError as error:
        raise ValueError("canonical Runtime main must track origin/main") from error
    if upstream_ref != "refs/remotes/origin/main":
        raise ValueError(
            "canonical Runtime main must track origin/main; "
            f"found {upstream_ref or 'no upstream'}"
        )

    upstream_revision = _git(source_path, "rev-parse", upstream_ref)
    remote_result = subprocess.run(
        ["git", "-C", str(source_path), "ls-remote", "--exit-code", "origin", "refs/heads/main"],
        check=True,
        capture_output=True,
        text=True,
    )
    remote_fields = remote_result.stdout.strip().split()
    if len(remote_fields) != 2 or remote_fields[1] != "refs/heads/main":
        raise ValueError("canonical Runtime origin/main remote proof is malformed")
    remote_revision = remote_fields[0]
    if revision != upstream_revision or revision != remote_revision:
        raise ValueError(
            "canonical Runtime main must equal published upstream before formal installation: "
            f"HEAD={revision}, origin/main={upstream_revision}, remote={remote_revision}"
        )

    return {
        "status": "verified",
        "branch": branch,
        "upstream": "origin/main",
        "remote_ref": "refs/heads/main",
        "revision": revision,
        "upstream_revision": upstream_revision,
        "remote_revision": remote_revision,
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
    controller_registry: str | Path = DEFAULT_CONTROLLER_REGISTRY,
    canonical_release_source: dict[str, Any] | None = None,
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
    if canonical_release_source is not None:
        fresh_release_source = verify_canonical_release_source(source_path)
        if fresh_release_source != canonical_release_source or revision != fresh_release_source["revision"]:
            raise ValueError("canonical Runtime release source changed before installation")
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
        staged_capabilities = detect_host_capabilities(
            skill_root=target_path, controller_registry=controller_registry
        )
        staged_capabilities["controller_identity"] = _installed_controller_identity_capability(
            stage,
            expected_files=hashes,
            expected_revision=revision,
            reported_root=target_path,
        )
        manifest: dict[str, Any] = {
            "schema_version": 2 if canonical_release_source is not None else 1,
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
            "canonical_release_source": canonical_release_source,
            "summary": summary.strip(),
            "impact": impact,
            "stop_condition": stop_condition.strip(),
            "changed_files": _changed_files(source_path, prior_revision, revision, tracked),
            "capabilities": staged_capabilities,
            "files": hashes,
        }
        _write_json_atomic(stage / MANIFEST_NAME, manifest)
        for relative, expected in hashes.items():
            if _sha256(stage / relative) != expected:
                raise ValueError(f"staged file hash mismatch: {relative}")
        if canonical_release_source is not None:
            final_release_source = verify_canonical_release_source(source_path)
            if final_release_source != canonical_release_source:
                raise ValueError("canonical Runtime release source changed before promotion")
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
    target: Path,
    hooks: Path,
    zshenv: Path,
    health_service_plist: Path | None = None,
    *,
    include_zshenv: bool = True,
) -> list[Path]:
    paths = [
        target.parent / f".{target.name}.install.lock",
        hooks.parent / f".{hooks.name}.adaptive-agent-runtime.lock",
    ]
    if include_zshenv:
        paths.append(zshenv.parent / f".{zshenv.name}.adaptive-agent-runtime.lock")
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
    parser.add_argument("--no-configure-ai-bridge", action="store_true")
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
        include_zshenv=(
            not args.no_configure_host_adapters and not args.no_configure_ai_bridge
        ),
    )
    try:
        try:
            install_locks = _acquire_install_resource_locks(lock_paths)
        except BlockingIOError:
            print(f"adaptive-agent-runtime-install: blocked: another installer is active for shared install resources: {target_path}")
            return 1
        try:
            canonical_release_source = verify_canonical_release_source(args.source)
        except (OSError, ValueError, subprocess.CalledProcessError) as error:
            print(f"adaptive-agent-runtime-install: blocked: {error}")
            return 1
        if args.no_configure_host_adapters and args.no_configure_runtime_services:
            try:
                manifest = install_skill(
                    args.source, target_path, summary=args.summary, impact=args.impact,
                    stop_condition=args.stop_condition, previous_revision=args.previous_revision,
                    controller_registry=args.controller_registry,
                    canonical_release_source=canonical_release_source,
                )
            except (OSError, ValueError, subprocess.CalledProcessError) as error:
                print(f"adaptive-agent-runtime-install: blocked: {error}")
                return 1
            print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
            return 0
        return _run_install_transaction(
            args, target_path, hooks_path, zshenv_path, health_service_path,
            canonical_release_source=canonical_release_source,
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
    *,
    canonical_release_source: dict[str, Any],
) -> int:
    with tempfile.TemporaryDirectory(prefix="adaptive-agent-runtime-install-rollback-") as backup_dir:
        backup_root = Path(backup_dir)
        snapshots = [_snapshot_path(target_path, backup_root, "target")]
        if not args.no_configure_host_adapters:
            snapshots.append(_snapshot_path(hooks_path, backup_root, "hooks"))
            if not args.no_configure_ai_bridge:
                snapshots.append(_snapshot_path(zshenv_path, backup_root, "zshenv"))
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
                controller_registry=args.controller_registry,
                canonical_release_source=canonical_release_source,
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
                    configure_ai_bridge=not args.no_configure_ai_bridge,
                    controller_registry=args.controller_registry,
                )
            manifest["capabilities"] = detect_host_capabilities(
                codex_executable=args.codex,
                ai_bridge_executable=args.ai_bridge,
                hooks_file=hooks_path,
                zshenv_file=zshenv_path,
                skill_root=target_path,
                health_service_plist=health_service_path,
                controller_registry=args.controller_registry,
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
