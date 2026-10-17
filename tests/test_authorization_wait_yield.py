"""Regression coverage for a CLOSED Controller cycle waiting on authorization."""
import copy
import unittest

from tests import test_rule_acceptance_yield as rule_tests


class AuthorizationWaitYieldTests(unittest.TestCase):
    def setUp(self):
        case = rule_tests.RuleAcceptanceYieldTests(
            methodName="test_pending_version_safe_receipt_yields_without_erasing_business_debt"
        )
        case.setUp()
        self.addCleanup(case.doCleanups)
        self.case = case
        self.case.snapshot["rule_handshake"] = {
            "state": "current",
            "blocking": False,
            "installed_revision": "candidate",
            "loaded_revision": "candidate",
        }

    def test_closed_authorization_only_cycle_waits_for_user_without_requeue(self):
        state = self.case.receipt()

        output, stopped = self.case.stop(state)

        self.assertIs(output.get("continue"), False)
        self.assertNotEqual(output.get("decision"), "block")
        self.assertTrue(stopped["requires_user"])
        self.assertTrue(stopped["pending_control_event"])
        self.assertIn("VERIFY:VERIFY-1", stopped["triggers"])
        self.assertNotIn("host_turn_handoff", stopped)

        repeated_output, repeated = self.case.stop(stopped)
        self.assertIs(repeated_output.get("continue"), False)
        self.assertTrue(repeated["requires_user"])
        self.assertTrue(repeated["pending_control_event"])
        self.assertIn("VERIFY:VERIFY-1", repeated["triggers"])
        self.assertNotIn("host_turn_handoff", repeated)

        with self.subTest(case="stale_requested_handoff"):
            stale = copy.deepcopy(state)
            stale["host_turn_handoff"] = {
                "state": "requested",
                "turn_id": "turn-current",
                "controller_id": "test-controller",
                "execution_target_session_id": "source",
                "target_generation": 4,
                "ownership_generation": 6,
                "debt_fingerprint": "d" * 64,
                "progress_fingerprint": "e" * 64,
            }
            expected_triggers = copy.deepcopy(stale["triggers"])

            stale_output, cleared = self.case.stop(stale)

            self.assertIs(stale_output.get("continue"), False)
            self.assertTrue(cleared["requires_user"])
            self.assertTrue(cleared["pending_control_event"])
            self.assertEqual(cleared["triggers"], expected_triggers)
            self.assertNotIn("host_turn_handoff", cleared)

    def test_new_executable_or_invalid_project_fact_blocks_authorization_wait(self):
        state = self.case.receipt()
        mutations = {
            "ready": lambda snapshot: snapshot.update(ready_ids=["READY-2"]),
            "runnable": lambda snapshot: snapshot.update(runnable_ids=["READY-2"]),
            "active": lambda snapshot: snapshot.update(
                assignment_liveness={"worker": {"state": "running"}}
            ),
            "candidate": lambda snapshot: snapshot.update(candidate_revisions=["candidate-2"]),
            "correction": lambda snapshot: snapshot.update(
                controller_corrections=[{"fingerprint": "correction-2"}]
            ),
            "ledger_error": lambda snapshot: snapshot.update(ledger_errors=["invalid ledger"]),
            "head": lambda snapshot: snapshot.update(head="changed-head"),
            "ledger": lambda snapshot: snapshot.update(ledger_sha256="changed-ledger"),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                snapshot = copy.deepcopy(self.case.snapshot)
                mutate(snapshot)
                output, _stopped = self.case.stop(copy.deepcopy(state), snapshot=snapshot)
                self.assertEqual(output.get("decision"), "block")
                self.assertIsNot(output.get("continue"), False)

    def test_missing_stale_or_noncurrent_proof_blocks_authorization_wait(self):
        state = self.case.receipt()
        state_cases = {
            "missing_proof": {**state, "rule_acceptance_yield": None},
            "invalid_proof": {
                **state,
                "rule_acceptance_yield": {
                    **state.get("rule_acceptance_yield", {}),
                    "proposal": None,
                },
            },
            "missing_receipt": {**state, "must_yield": False},
        }
        for name, malformed_facts in (("facts_none", None), ("facts_list", [])):
            changed = copy.deepcopy(state)
            changed["rule_acceptance_yield"]["facts"] = malformed_facts
            state_cases[name] = changed
        for name, changed in state_cases.items():
            with self.subTest(name=name):
                output, _stopped = self.case.stop(changed)
                self.assertEqual(output.get("decision"), "block")

        for name, handshake in {
            "loaded_mismatch": {
                "state": "current", "blocking": False,
                "installed_revision": "candidate", "loaded_revision": "other",
            },
            "blocking_current": {
                "state": "current", "blocking": True,
                "installed_revision": "candidate", "loaded_revision": "candidate",
            },
        }.items():
            with self.subTest(name=name):
                snapshot = copy.deepcopy(self.case.snapshot)
                snapshot["rule_handshake"] = handshake
                output, _stopped = self.case.stop(copy.deepcopy(state), snapshot=snapshot)
                self.assertEqual(output.get("decision"), "block")

        _tool_output, continued = rule_tests.hook.evaluate_event(
            {
                **self.case.event,
                "hook_event_name": "PreToolUse",
                "tool_use_id": "same-turn-later-tool",
                "tool_input": {"command": "true"},
            },
            snapshot=self.case.snapshot,
            prior_state=state,
        )
        _post_output, continued = rule_tests.hook.evaluate_event(
            {
                **self.case.event,
                "hook_event_name": "PostToolUse",
                "tool_use_id": "same-turn-later-tool",
                "tool_input": {"command": "true"},
                "tool_response": {"exit_code": 0},
            },
            snapshot=self.case.snapshot,
            prior_state=continued,
        )
        later_output, _stopped = self.case.stop(continued)
        self.assertEqual(later_output.get("decision"), "block")

    def test_empty_or_non_authorization_controller_action_blocks_wait(self):
        for name in ("empty", "wrong_reason", "wrong_decision"):
            with self.subTest(name=name):
                saved = copy.deepcopy(self.case.fixture.snapshot)

                def mutate(contract):
                    if name == "empty":
                        contract["controller_actions"] = []
                    elif name == "wrong_reason":
                        contract["controller_actions"][0]["reason_code"] = "external_blocker"
                    else:
                        contract["controller_actions"][0]["decision"] = "deferred"

                state = self.case.receipt(mutate)
                output, _stopped = self.case.stop(state)
                self.assertEqual(output.get("decision"), "block")
                self.case.fixture.snapshot = saved

    def test_pending_live_e2e_keeps_existing_supervisor_handoff(self):
        self.case.snapshot["rule_handshake"] = {
            "state": "pending_live_e2e",
            "blocking": True,
            "installed_revision": "candidate",
            "loaded_revision": "candidate",
        }
        state = self.case.receipt()

        output, stopped = self.case.stop(state)

        self.assertIs(output.get("continue"), False)
        self.assertEqual(stopped["host_turn_handoff"]["state"], "requested")
        self.assertFalse(stopped["requires_user"])


if __name__ == "__main__":
    unittest.main()
