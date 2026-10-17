"""Regression tests for native same-Controller continuation handoff identity.

All scheduling is isolated behind temporary state/registry files and a mocked
``Popen``.  These tests must never contact a real Desktop Host.
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import web_lifecycle_bridge as bridge


class NativeContinuationHandoffTests(unittest.TestCase):
    controller_id = "controller-1"
    current_target = "desktop-current"

    def setUp(self) -> None:
        self.fence = {
            "execution_target_session_id": self.current_target,
            "target_generation": 4,
            "ownership_generation": 6,
        }
        self.lifecycle = {
            "pending_control_event": True,
            "requires_user": False,
            "controller_host": "desktop_codex",
            "wake_generation": 14,
            "active_turn_id": "turn-current",
            "triggers": ["rule_live_e2e_pending:rev-current", "VERIFY:VERIFY-1"],
            "snapshot": {
                "root": "/repo",
                "head": "head-current",
                "ledger_sha256": "1" * 64,
                "worktree_status_sha256": "2" * 64,
                "ready_ids": [],
                "runnable_ids": [],
                "candidate_revisions": [],
                "ledger_errors": [],
                "task_states": {"VERIFY-1": "VERIFY"},
                "assignment_liveness": {},
                "controller_actions": [
                    {
                        "id": "open_child:VERIFY-1",
                        "decision": "blocked",
                        "reason_code": "authorization",
                    }
                ],
                "controller_corrections": [],
                "rule_handshake": {
                    "state": "pending_live_e2e",
                    "blocking": True,
                    "installed_revision": "rev-current",
                    "loaded_revision": "rev-current",
                }
            },
            "rule_acceptance_yield": {
                "facts": {
                    "binding": {
                        "controller_session_id": self.controller_id,
                        "source_session_id": self.current_target,
                        "controller_target_generation": 4,
                        "controller_ownership_generation": 6,
                        "turn_id": "turn-current",
                    },
                    "projection": {
                        "root": "/repo",
                        "head": "head-current",
                        "ledger_sha256": "1" * 64,
                        "worktree_status_sha256": "2" * 64,
                        "task_states": {"VERIFY-1": "VERIFY"},
                        "assignment_liveness": {},
                        "controller_actions": [
                            {
                                "id": "open_child:VERIFY-1",
                                "decision": "blocked",
                                "reason_code": "authorization",
                            }
                        ],
                        "controller_corrections": [],
                    },
                    "debt": ["VERIFY:VERIFY-1"],
                    "revision": "rev-current",
                },
                "proposal": {
                    "event_id": "proposal-current",
                    "turn_id": "turn-current",
                    "machine_trace_sha256": "3" * 64,
                },
            },
            "host_turn_handoff": {
                "schema_version": 1,
                "state": "requested",
                "turn_id": "turn-current",
                "controller_id": self.controller_id,
                "execution_target_session_id": self.current_target,
                "target_generation": 4,
                "ownership_generation": 6,
                "wake_generation": 14,
                "debt_fingerprint": "b" * 64,
                "progress_fingerprint": "a" * 64,
            },
        }

    def handoff_receipt(
        self,
        lifecycle: dict | None = None,
        fence: dict | None = None,
        *,
        controller_id: str | None = None,
    ) -> str | None:
        helper = getattr(bridge, "_desktop_handoff_delivery_receipt", None)
        self.assertTrue(
            callable(helper),
            "web_lifecycle_bridge must expose _desktop_handoff_delivery_receipt",
        )
        return helper(
            lifecycle or self.lifecycle,
            controller_id or self.controller_id,
            fence or self.fence,
        )

    def make_runtime(self, root: Path) -> tuple[Path, Path, Path]:
        repo = root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
        registry = root / "controllers.json"
        registry.write_text(
            json.dumps(
                {
                    self.controller_id: str(repo.resolve()),
                    "__controller_targets__": {
                        self.controller_id: {
                            "desktop_codex": {
                                "status": "active",
                                "session_id": self.current_target,
                                "generation": 4,
                            }
                        }
                    },
                    "__controller_execution_ownership__": {
                        self.controller_id: {
                            "active_host": "desktop_codex",
                            "execution_target_session_id": self.current_target,
                            "generation": 6,
                            "provenance": "desktop_entry",
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        return repo, registry, root / "auto-stop.json"

    @staticmethod
    def old_web_unknown_receipt() -> dict:
        return {
            "schema_version": 1,
            "receipt_id": "web-unknown-old",
            "wake_id": "wake-old",
            "wake_nonce_sha256": "c" * 64,
            "continuation_payload_sha256": "d" * 64,
            "conversation_id": "web-old",
            "target_generation": 2,
            "ownership_generation": 4,
            "dispatch_attempted": True,
            "submit_confirmed": False,
            "retryable": False,
            "auto_retry_allowed": False,
            "result_class": "RESULT_UNKNOWN",
            "status": "result_unknown",
            "provenance": "browser_host_reentry_receipt_v1",
        }

    def legacy_stalled_state(self, repo: Path) -> dict:
        return {
            "receipt_id": "bootstrap:14",
            "session_id": self.controller_id,
            "repo": str(repo.resolve()),
            "state": "RESUME_STALLED_NO_PROGRESS",
            "pending_control_event": True,
            "execution_target_session_id": self.current_target,
            "target_generation": 4,
            "ownership_generation": 6,
            "last_lifecycle_fingerprint": "legacy-event",
            "terminal_delivery_coalesced_count": 4318,
            "original_reentry_receipt": self.old_web_unknown_receipt(),
        }

    def test_receipt_uses_stable_safe_yield_facts_and_excludes_turn_proposal_and_progress(self) -> None:
        receipt = self.handoff_receipt()
        self.assertIsInstance(receipt, str)
        self.assertTrue(receipt)
        self.assertNotEqual(receipt, "bootstrap:14")

        later_turn = copy.deepcopy(self.lifecycle)
        later_turn["active_turn_id"] = "turn-later"
        later_turn["host_turn_handoff"].update(
            {
                "state": "delegated",
                "turn_id": "turn-later",
                "recorded_at": "2099-01-01T00:00:00+00:00",
                "progress_fingerprint": "e" * 64,
            }
        )
        later_turn["rule_acceptance_yield"]["facts"]["binding"]["turn_id"] = "turn-later"
        later_turn["rule_acceptance_yield"]["proposal"] = {
            "event_id": "proposal-later",
            "turn_id": "turn-later",
            "machine_trace_sha256": "4" * 64,
        }
        self.assertEqual(self.handoff_receipt(later_turn), receipt)

        changed_progress_only = copy.deepcopy(self.lifecycle)
        changed_progress_only["host_turn_handoff"]["progress_fingerprint"] = "9" * 64
        self.assertEqual(self.handoff_receipt(changed_progress_only), receipt)

        changed_proposal_only = copy.deepcopy(self.lifecycle)
        changed_proposal_only["rule_acceptance_yield"]["proposal"] = {
            "event_id": "proposal-recomputed",
            "machine_trace_sha256": "8" * 64,
        }
        self.assertEqual(self.handoff_receipt(changed_proposal_only), receipt)

        mutations = {
            "projection_head": lambda state, fence: (
                state["snapshot"].update(head="head-next"),
                state["rule_acceptance_yield"]["facts"]["projection"].update(
                    head="head-next"
                ),
            ),
            "projection_ledger": lambda state, fence: (
                state["snapshot"].update(ledger_sha256="5" * 64),
                state["rule_acceptance_yield"]["facts"]["projection"].update(
                    ledger_sha256="5" * 64
                ),
            ),
            "debt": lambda state, fence: (
                state["snapshot"].update(task_states={"VERIFY-2": "VERIFY"}),
                state["rule_acceptance_yield"]["facts"].update(debt=["VERIFY:VERIFY-2"]),
                state["rule_acceptance_yield"]["facts"]["projection"].update(
                    task_states={"VERIFY-2": "VERIFY"}
                ),
                state["host_turn_handoff"].update(debt_fingerprint="f" * 64),
            ),
            "revision": lambda state, fence: (
                state["rule_acceptance_yield"]["facts"].update(revision="rev-next"),
                state["snapshot"]["rule_handshake"].update(
                    installed_revision="rev-next", loaded_revision="rev-next"
                ),
            ),
            "generation": lambda state, fence: (
                state.update(wake_generation=15),
                state["host_turn_handoff"].update(wake_generation=15),
            ),
            "fence": lambda state, fence: (
                state["host_turn_handoff"].update(
                    execution_target_session_id="desktop-next",
                    target_generation=5,
                    ownership_generation=7,
                ),
                state["rule_acceptance_yield"]["facts"]["binding"].update(
                    source_session_id="desktop-next",
                    controller_target_generation=5,
                    controller_ownership_generation=7,
                ),
                fence.update(
                    execution_target_session_id="desktop-next",
                    target_generation=5,
                    ownership_generation=7,
                ),
            ),
        }
        for case, mutate in mutations.items():
            with self.subTest(case=case):
                changed = copy.deepcopy(self.lifecycle)
                changed_fence = dict(self.fence)
                mutate(changed, changed_fence)
                self.assertNotEqual(
                    self.handoff_receipt(changed, changed_fence), receipt
                )

    def test_invalid_hash_current_turn_controller_or_fence_fails_closed(self) -> None:
        cases = {}

        def add(name, mutate_lifecycle=None, mutate_fence=None, controller_id=None):
            state = copy.deepcopy(self.lifecycle)
            fence = dict(self.fence)
            if mutate_lifecycle:
                mutate_lifecycle(state)
            if mutate_fence:
                mutate_fence(fence)
            cases[name] = (state, fence, controller_id or self.controller_id)

        add("short_progress", lambda state: state["host_turn_handoff"].update(progress_fingerprint="bad"))
        add("non_string_debt", lambda state: state["host_turn_handoff"].update(debt_fingerprint=7))
        add("missing_active_turn", lambda state: state.pop("active_turn_id"))
        add("wrong_turn", lambda state: state["host_turn_handoff"].update(turn_id="other-turn"))
        add("wrong_controller", controller_id="controller-2")
        add("untrusted_state", lambda state: state["host_turn_handoff"].update(state="reentry_confirmed"))
        add("handoff_generation_mismatch", lambda state: state["host_turn_handoff"].update(wake_generation=13))
        add("target_mismatch", lambda state: state["host_turn_handoff"].update(execution_target_session_id="desktop-other"))
        add("target_generation_mismatch", lambda state: state["host_turn_handoff"].update(target_generation=5))
        add("ownership_generation_mismatch", lambda state: state["host_turn_handoff"].update(ownership_generation=7))
        add("invalid_canonical_generation", mutate_fence=lambda fence: fence.update(target_generation=True))
        add("missing_safe_proof", lambda state: state.pop("rule_acceptance_yield"))
        add("missing_revision", lambda state: state["rule_acceptance_yield"]["facts"].pop("revision"))
        add(
            "proof_binding_target_mismatch",
            lambda state: state["rule_acceptance_yield"]["facts"]["binding"].update(
                source_session_id="desktop-other"
            ),
        )
        add(
            "proof_binding_turn_mismatch",
            lambda state: state["rule_acceptance_yield"]["facts"]["binding"].update(
                turn_id="other-turn"
            ),
        )

        for case, (state, fence, controller_id) in cases.items():
            with self.subTest(case=case):
                self.assertIsNone(
                    self.handoff_receipt(state, fence, controller_id=controller_id)
                )

    def test_legacy_stall_and_old_web_unknown_start_one_new_native_delivery_and_preserve_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, registry, state_path = self.make_runtime(Path(tmp))
            old_unknown = self.old_web_unknown_receipt()
            state_path.write_text(
                json.dumps(self.legacy_stalled_state(repo)), encoding="utf-8"
            )
            expected_receipt = self.handoff_receipt()

            with patch.object(
                bridge, "default_auto_stop_state_path", return_value=state_path
            ), patch.object(
                bridge.subprocess, "Popen", return_value=Mock(pid=9001)
            ) as popen, patch.object(bridge, "_pid_is_alive", return_value=True):
                first = bridge.ensure_continuation_supervisor(
                    lifecycle_state=self.lifecycle,
                    session_id=self.controller_id,
                    repo=repo,
                    registry=registry,
                    codex=None,
                    delay_seconds=1,
                )
                first_state = json.loads(state_path.read_text(encoding="utf-8"))

                later_turn = copy.deepcopy(self.lifecycle)
                later_turn["active_turn_id"] = "turn-later"
                later_turn["host_turn_handoff"].update(
                    state="delegated",
                    turn_id="turn-later",
                    recorded_at="later",
                    progress_fingerprint="e" * 64,
                )
                later_turn["rule_acceptance_yield"]["facts"]["binding"]["turn_id"] = "turn-later"
                later_turn["rule_acceptance_yield"]["proposal"] = {
                    "event_id": "proposal-later",
                    "turn_id": "turn-later",
                    "machine_trace_sha256": "4" * 64,
                }
                second = bridge.ensure_continuation_supervisor(
                    lifecycle_state=later_turn,
                    session_id=self.controller_id,
                    repo=repo,
                    registry=registry,
                    codex=None,
                    delay_seconds=1,
                )

            self.assertTrue(first)
            self.assertFalse(second)
            popen.assert_called_once()
            self.assertEqual(first_state["receipt_id"], expected_receipt)
            self.assertNotEqual(first_state["receipt_id"], "bootstrap:14")
            self.assertEqual(first_state["original_reentry_receipt"], old_unknown)

    def test_schedule_rejection_is_not_reported_as_started(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, registry, state_path = self.make_runtime(Path(tmp))
            state_path.write_text(
                json.dumps(self.legacy_stalled_state(repo)), encoding="utf-8"
            )
            with patch.object(
                bridge, "default_auto_stop_state_path", return_value=state_path
            ), patch.object(
                bridge, "schedule_auto_native_stop", return_value=False
            ) as schedule:
                started = bridge.ensure_continuation_supervisor(
                    lifecycle_state=self.lifecycle,
                    session_id=self.controller_id,
                    repo=repo,
                    registry=registry,
                    codex=None,
                    delay_seconds=1,
                )
            self.assertFalse(started)
            schedule.assert_called_once()

    def test_live_same_fence_supervisor_keeps_old_receipt_and_token_when_new_receipt_arrives(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, registry, state_path = self.make_runtime(Path(tmp))
            old_receipt = "handoff-old-receipt"
            old_token = "old-supervisor-token"
            state_path.write_text(
                json.dumps(
                    {
                        "receipt_id": old_receipt,
                        "supervisor_receipt_id": old_receipt,
                        "supervisor_token": old_token,
                        "supervisor_pid": 15406,
                        "session_id": self.controller_id,
                        "repo": str(repo.resolve()),
                        "state": "RESUME_PENDING",
                        "pending_control_event": True,
                        **self.fence,
                    }
                ),
                encoding="utf-8",
            )

            with patch.object(
                bridge, "_pid_is_alive", return_value=True
            ), patch.object(bridge.subprocess, "Popen", return_value=Mock(pid=9002)) as popen:
                scheduled = bridge.schedule_auto_native_stop(
                    session_id=self.controller_id,
                    repo=repo,
                    receipt_id="handoff-new-receipt",
                    registry=registry,
                    codex=None,
                    delay_seconds=1,
                    state_path=state_path,
                    expected_controller_fence=self.fence,
                )

            self.assertFalse(scheduled)
            popen.assert_not_called()
            preserved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(preserved["receipt_id"], old_receipt)
            self.assertEqual(preserved["supervisor_receipt_id"], old_receipt)
            self.assertEqual(preserved["supervisor_token"], old_token)

    def test_result_unknown_for_current_handoff_delivery_is_never_rescheduled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, registry, state_path = self.make_runtime(Path(tmp))
            receipt = self.handoff_receipt()
            state = self.legacy_stalled_state(repo)
            state.update(
                {
                    "receipt_id": receipt,
                    "state": "WEB_REENTRY_RESULT_UNKNOWN",
                    "delivery_terminal_receipt_id": receipt,
                    "delivery_terminal_key": receipt,
                    "delivery_terminal_outcome": "result_unknown",
                    "failure_class": "desktop_host_result_unknown",
                }
            )
            state_path.write_text(json.dumps(state), encoding="utf-8")

            with patch.object(
                bridge, "default_auto_stop_state_path", return_value=state_path
            ), patch.object(bridge, "schedule_auto_native_stop") as schedule:
                started = bridge.ensure_continuation_supervisor(
                    lifecycle_state=self.lifecycle,
                    session_id=self.controller_id,
                    repo=repo,
                    registry=registry,
                    codex=None,
                    delay_seconds=1,
                )

            self.assertFalse(started)
            schedule.assert_not_called()
            preserved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(preserved["receipt_id"], receipt)
            self.assertEqual(preserved["delivery_terminal_outcome"], "result_unknown")


if __name__ == "__main__":
    unittest.main()
