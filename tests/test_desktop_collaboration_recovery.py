from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import lifecycle_hook as hook


class DesktopCollaborationRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sessions = self.root / "sessions"
        self.sessions.mkdir()
        self.path = self.sessions / "rollout-source.jsonl"
        self.env = patch.dict(os.environ, {"CODEX_HOME": str(self.root)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.arguments = {"target": "reviewer", "message": "check candidate"}
        self.snapshot = {"root": str(self.root), "head": "abc", "ledger_sha256": "ledger",
                         "worktree_status_sha256": "clean", "ready_ids": [], "runnable_ids": [],
                         "candidate_revisions": [], "ledger_errors": [], "assignment_liveness": {},
                         "rule_handshake": {"state": "current", "blocking": False}}
        self.state = {"active_turn_id": "turn", "controller_host": "desktop_codex",
                      "pending_control_event": True, "triggers": ["correction:open"],
                      "pending_terminal_receipts": ["/tmp/terminal.json"],
                      "inflight_tool_use_ids": ["message", "running"],
                      "inflight_tool_records": {
                          "message": {"turn_id": "turn", "tool_name": "collaborationsend_message",
                                      "input_sha256": hook._json_sha256(self.arguments)},
                          "running": {"turn_id": "turn", "tool_name": "Bash"}}}
        self.event = {"hook_event_name": "PreToolUse", "controller_host": "desktop_codex",
                      "session_id": "logical", "controller_session_id": "logical",
                      "source_session_id": "source", "turn_id": "turn",
                      "transcript_path": str(self.path)}

    def rows(self):
        metadata = {"turn_id": "turn"}
        return [
            {"type": "session_meta", "payload": {"id": "source"}},
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn"}},
            {"type": "response_item", "payload": {"type": "function_call", "call_id": "message",
                "namespace": "collaboration", "name": "send_message",
                "arguments": json.dumps(self.arguments),
                "internal_chat_message_metadata_passthrough": metadata}},
            {"type": "event_msg", "payload": {"type": "item_completed", "turn_id": "turn",
                "thread_id": "source", "item": {"type": "SubAgentActivity", "id": "message",
                "kind": "interacted", "agent_thread_id": "reviewer"}}},
            {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "message",
                "output": "", "internal_chat_message_metadata_passthrough": metadata}},
        ]

    def recover(self, rows=None, state=None, event=None):
        self.path.write_text("".join(json.dumps(row) + "\n" for row in (rows or self.rows())))
        return hook._reconcile_desktop_rollout(
            copy.deepcopy(state or self.state), event or self.event, self.snapshot)

    def test_real_call_activity_and_empty_return_recover_only_matching_inflight(self):
        result = self.recover()
        self.assertEqual(result["inflight_tool_use_ids"], ["running"])
        self.assertNotIn("message", result["inflight_tool_records"])
        self.assertTrue(result["pending_control_event"])
        self.assertIn("correction:open", result["triggers"])
        self.assertEqual(result["pending_terminal_receipts"], ["/tmp/terminal.json"])
        self.assertFalse(result.get("must_yield", False))
        self.assertEqual(result["tool_trace"][-1]["tool_use_id"], "message")
        self.assertEqual(result["tool_trace"][-1]["input_sha256"], hook._json_sha256(self.arguments))
        # Re-reading a genuine terminal cannot duplicate the trace.
        again = self.recover(state=result)
        self.assertEqual(again["tool_trace"], result["tool_trace"])

    def test_incomplete_or_conflicting_terminal_keeps_lock(self):
        for case in ["call_missing", "activity_missing", "return_missing", "hash", "namespace",
                     "tool", "turn", "return_turn", "return_id", "activity_id", "activity_kind",
                     "duplicate_call", "duplicate_return", "duplicate_activity", "return_before_activity",
                     "output_missing", "metadata_missing", "activity_source", "source", "boundary",
                     "call_before_start", "activity_before_call"]:
            with self.subTest(case=case):
                rows = self.rows()
                if case in {"call_missing", "activity_missing", "return_missing"}:
                    rows.pop({"call_missing": 2, "activity_missing": 3, "return_missing": 4}[case])
                elif case == "hash":
                    rows[2]["payload"]["arguments"] = json.dumps({"target": "other", "message": "different"})
                elif case == "namespace": rows[2]["payload"]["namespace"] = "functions"
                elif case == "tool": rows[2]["payload"]["name"] = "exec"
                elif case == "turn": rows[2]["payload"]["internal_chat_message_metadata_passthrough"] = {"turn_id": "old"}
                elif case == "return_turn": rows[4]["payload"]["internal_chat_message_metadata_passthrough"] = {"turn_id": "old"}
                elif case == "return_id": rows[4]["payload"]["call_id"] = "other"
                elif case == "activity_id": rows[3]["payload"]["item"]["id"] = "other"
                elif case == "activity_kind": rows[3]["payload"]["item"]["kind"] = "started"
                elif case == "duplicate_call": rows.insert(3, copy.deepcopy(rows[2]))
                elif case == "duplicate_return": rows.append(copy.deepcopy(rows[4]))
                elif case == "duplicate_activity": rows.insert(4, copy.deepcopy(rows[3]))
                elif case == "return_before_activity": rows[3], rows[4] = rows[4], rows[3]
                elif case == "output_missing": rows[4]["payload"].pop("output")
                elif case == "metadata_missing": rows[4]["payload"].pop("internal_chat_message_metadata_passthrough")
                elif case == "activity_source": rows[3]["payload"]["thread_id"] = "other"
                elif case == "source": rows[0]["payload"]["id"] = "other"
                elif case == "boundary": rows.insert(4, {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "other"}})
                elif case == "call_before_start": rows[1], rows[2] = rows[2], rows[1]
                elif case == "activity_before_call": rows[2], rows[3] = rows[3], rows[2]
                result = self.recover(rows)
                self.assertIn("message", result["inflight_tool_use_ids"])

    def test_saved_tool_name_and_turn_must_match(self):
        for field, value in [("tool_name", "Bash"), ("turn_id", "old")]:
            with self.subTest(field=field):
                state = copy.deepcopy(self.state)
                state["inflight_tool_records"]["message"][field] = value
                self.assertIn("message", self.recover(state=state)["inflight_tool_use_ids"])

    def test_pretool_recovers_completed_message_before_serial_guard_fence(self):
        self.path.write_text("".join(json.dumps(row) + "\n" for row in self.rows()))
        prior = copy.deepcopy(self.state)
        prior["inflight_tool_use_ids"] = ["message"]
        prior["inflight_tool_records"].pop("running")
        event = {**self.event, "tool_name": "Bash", "tool_use_id": "new-guard",
                 "tool_input": {"command": f"{sys.executable} {Path(hook.__file__).with_name('control_event_guard.py')} "
                 f"{self.root / 'receipt.json'} --ledger {self.root / 'TASK_LEDGER.md'} "
                 f"--repo {self.root} --controller-session logical"}}
        output, result = hook.evaluate_event(event, snapshot=self.snapshot, prior_state=prior)
        self.assertEqual(output, {})
        self.assertEqual(result["inflight_tool_use_ids"], ["new-guard"])
        self.assertEqual(result["control_receipt_inflight"], "new-guard")
        self.assertTrue(result["pending_control_event"])
        self.assertFalse(result.get("must_yield", False))

    def test_other_running_tool_still_blocks_serial_guard(self):
        self.path.write_text("".join(json.dumps(row) + "\n" for row in self.rows()))
        event = {**self.event, "tool_name": "Bash", "tool_use_id": "new-guard",
                 "tool_input": {"command": f"{sys.executable} {Path(hook.__file__).with_name('control_event_guard.py')} "
                 f"{self.root / 'receipt.json'} --ledger {self.root / 'TASK_LEDGER.md'} "
                 f"--repo {self.root} --controller-session logical"}}
        output, result = hook.evaluate_event(event, snapshot=self.snapshot, prior_state=copy.deepcopy(self.state))
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(result["inflight_tool_use_ids"], ["running"])

    def test_closed_turn_recovery_preserves_business_debt(self):
        rows = self.rows() + [{"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn"}}]
        self.path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        event = {**self.event, "hook_event_name": "Stop"}
        result = hook._reconcile_desktop_rollout(copy.deepcopy(self.state), event, self.snapshot, closed_turn=True)
        self.assertEqual(result["inflight_tool_use_ids"], ["running"])
        self.assertTrue(result["pending_control_event"])
        self.assertFalse(result.get("must_yield", False))


if __name__ == "__main__":
    unittest.main()
