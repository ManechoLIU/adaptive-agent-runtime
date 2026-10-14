import copy
import sys
import json
import tempfile
from unittest.mock import patch
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import lifecycle_hook as hook


class DesktopBoundedHandoffTests(unittest.TestCase):
    def setUp(self):
        self.event = {"hook_event_name": "Stop", "controller_host": "desktop_codex",
                      "controller_session_id": "logical", "session_id": "logical",
                      "source_session_id": "current", "turn_id": "turn",
                      "controller_target_generation": 4, "controller_ownership_generation": 6}
        self.state = {"active_turn_id": "turn", "controller_host": "desktop_codex",
                      "source_session_id": "current", "pending_control_event": True,
                      "triggers": ["CORRECTION:fp", "OPEN_CHILD:R28-A"],
                      "pending_terminal_receipts": ["/tmp/result.json"], "requires_user": False,
                      "snapshot": {"head": "abc", "ledger_sha256": "ledger", "worktree_status_sha256": "clean"}}

    def stop(self, state):
        return hook._pending_stop_output(state, self.event, reason="L4 pending")

    def test_two_no_progress_stops_request_same_controller_handoff_without_clearing_debt(self):
        state = copy.deepcopy(self.state)
        self.assertEqual(self.stop(state)["decision"], "block")
        output = self.stop(state)
        self.assertIs(output.get("continue"), False)
        self.assertEqual(state["host_turn_handoff"]["state"], "requested")
        self.assertEqual(state["host_turn_handoff"]["controller_id"], "logical")
        self.assertEqual(state["host_turn_handoff"]["execution_target_session_id"], "current")
        self.assertEqual(state["host_turn_handoff"]["debt_fingerprint"], hook.continuation_debt_fingerprint(state))
        self.assertTrue(state["pending_control_event"])
        self.assertEqual(state["triggers"], self.state["triggers"])
        self.assertEqual(state["pending_terminal_receipts"], self.state["pending_terminal_receipts"])
        self.assertFalse(state.get("must_yield", False))
        self.assertNotIn("goal_block_authorization", state)

    def test_real_fact_progress_resets_stop_budget(self):
        state = copy.deepcopy(self.state)
        self.stop(state)
        state["snapshot"]["ledger_sha256"] = "changed"
        self.assertEqual(self.stop(state)["decision"], "block")
        self.assertIs(self.stop(state).get("continue"), False)

    def test_running_tools_and_unverified_fences_do_not_request_handoff(self):
        for case in ["inflight", "receipt", "wrong_source", "target_missing", "ownership_missing"]:
            with self.subTest(case=case):
                state = copy.deepcopy(self.state)
                event = dict(self.event)
                if case == "inflight": state["inflight_tool_use_ids"] = ["running"]
                elif case == "receipt": state["control_receipt_inflight"] = "guard"
                elif case == "wrong_source": event["source_session_id"] = "other"
                elif case == "target_missing": event.pop("controller_target_generation")
                elif case == "ownership_missing": event.pop("controller_ownership_generation")
                for _ in range(3):
                    result = hook._pending_stop_output(state, event, reason="pending")
                self.assertEqual(result["decision"], "block")
                self.assertNotIn("host_turn_handoff", state)

    def test_fence_change_resets_stop_budget(self):
        state = copy.deepcopy(self.state)
        self.stop(state)
        self.event["controller_target_generation"] = 5
        self.event["controller_ownership_generation"] = 7
        self.assertEqual(self.stop(state)["decision"], "block")
        self.assertIs(self.stop(state).get("continue"), False)

    def test_unavailable_supervisor_is_external_wait_not_delegated(self):
        state = copy.deepcopy(self.state)
        self.stop(state)
        self.stop(state)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text(json.dumps(state))
            result = hook.arm_host_turn_handoff(
                path=path, lifecycle_state=state, controller_id="logical", repo=Path(tmp),
                registry=Path(tmp) / "registry.json", supervisor_ensurer=lambda **kwargs: False,
                supervisor_manager_probe=lambda **kwargs: False)
            saved = json.loads(path.read_text())
            self.assertEqual(result["state"], "external_blocked")
            self.assertTrue(saved["pending_control_event"])
            self.assertTrue(saved["requires_user"])
            self.assertIs(self.stop(saved).get("continue"), False)
            self.assertFalse(saved.get("must_yield", False))

    def test_verified_existing_manager_is_distinct_from_external_wait(self):
        state = copy.deepcopy(self.state)
        self.stop(state)
        self.stop(state)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text(json.dumps(state))
            result = hook.arm_host_turn_handoff(
                path=path, lifecycle_state=state, controller_id="logical", repo=Path(tmp),
                registry=Path(tmp) / "registry.json", supervisor_ensurer=lambda **kwargs: False,
                supervisor_manager_probe=lambda **kwargs: True)
            self.assertEqual(result["state"], "delegated")
            self.assertEqual(result["delivery_state"], "existing_supervisor_verified")

    def test_default_manager_probe_rejects_canonical_unknown_failure_class(self):
        from scripts import web_lifecycle_bridge as bridge
        state = copy.deepcopy(self.state)
        self.stop(state)
        self.stop(state)
        manager = {"state": "RESUME_PENDING", "receipt_id": "receipt",
                   "supervisor_receipt_id": "receipt", "supervisor_token": "token", "supervisor_pid": 123,
                   "delivery_controller_fence": {"execution_target_session_id": "current",
                                                 "target_generation": 4, "ownership_generation": 6},
                   "last_lifecycle_fingerprint": bridge._wake_event_fingerprint(state)}
        with patch.object(bridge, "load_json", return_value=manager), patch.object(bridge, "_pid_is_alive", return_value=True):
            self.assertTrue(hook._existing_same_controller_supervisor(lifecycle_state=state, controller_id="logical"))
            manager["failure_class"] = "web_reentry_result_unknown"
            self.assertTrue(bridge._result_unknown_terminal_state(manager))
            self.assertFalse(hook._existing_same_controller_supervisor(lifecycle_state=state, controller_id="logical"))


if __name__ == "__main__":
    unittest.main()
