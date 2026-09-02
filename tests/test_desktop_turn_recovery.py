from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import lifecycle_hook


class DesktopTurnRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sessions = self.root / "sessions"
        self.sessions.mkdir()
        self.transcript = self.sessions / "rollout-source.jsonl"
        self.snapshot = {
            "root": str(self.root), "head": "abc", "ledger_sha256": "ledger",
            "worktree_status_sha256": "clean", "ready_ids": [], "runnable_ids": [],
            "candidate_revisions": [], "ledger_errors": [], "assignment_liveness": {},
            "rule_handshake": {"state": "current", "blocking": False},
        }
        self.prior = {
            "active_turn_id": "old", "must_yield": False,
            "pending_control_event": True, "triggers": ["ledger_changed"],
            "tool_trace_overflow": True,
            "tool_trace": [{"turn_id": "old", "tool_use_id": "old-call"}],
            "inflight_tool_use_ids": ["unfinished"],
            "pending_terminal_receipts": ["/tmp/result.json"],
            "next_action": "review existing candidate", "requires_user": False,
            "snapshot": self.snapshot,
        }
        self.write_transcript()
        self.addCleanup(patch.stopall)
        patch.dict("os.environ", {"CODEX_HOME": str(self.root)}).start()

    def write_transcript(self, session="source", turn="new", terminal=False):
        rows = [
            {"type": "session_meta", "payload": {"id": session}},
            {"type": "event_msg", "payload": {"type": "turn_aborted", "turn_id": "old"}},
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": turn}},
        ]
        if terminal:
            rows.append({"type": "event_msg", "payload": {"type": "task_complete", "turn_id": turn}})
        self.transcript.write_text("".join(json.dumps(row) + "\n" for row in rows))

    def event(self, name="PreToolUse", turn="new"):
        return {
            "hook_event_name": name, "session_id": "logical",
            "controller_session_id": "logical", "source_session_id": "source",
            "controller_host": "desktop_codex", "turn_id": turn,
            "transcript_path": str(self.transcript), "cwd": str(self.root),
            "tool_name": "Bash", "tool_use_id": "new-call",
            "tool_input": {"command": "git status --short"},
        }

    def test_delegated_turn_uses_host_start_without_user_prompt(self):
        output, state = lifecycle_hook.evaluate_event(
            self.event(), snapshot=self.snapshot, prior_state=self.prior,
        )
        self.assertEqual(output, {})
        self.assertEqual(state["active_turn_id"], "new")
        self.assertFalse(state["tool_trace_overflow"])
        self.assertEqual(state["tool_trace"], [])
        self.assertEqual(state["inflight_tool_use_ids"], ["new-call"])
        self.assertTrue(state["pending_control_event"])
        self.assertEqual(state["pending_terminal_receipts"], ["/tmp/result.json"])

    def test_turn_transition_archives_old_trace_before_replacing_it(self):
        path = self.root / "state.json"
        lifecycle_hook.write_json(path, self.prior)
        output, state = lifecycle_hook.persist_event_state(path, self.event(), self.snapshot)
        self.assertEqual(output, {})
        self.assertEqual(state["active_turn_id"], "new")
        archive = path.with_suffix(".turns.jsonl")
        records = [json.loads(line) for line in archive.read_text().splitlines()]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["tool_trace"], self.prior["tool_trace"])
        self.assertEqual(records[0]["inflight_tool_use_ids"], ["unfinished"])
        self.assertTrue(records[0]["tool_trace_overflow"])
        self.assertNotIn("command", archive.read_text())

    def test_unproven_or_foreign_turn_cannot_unlock(self):
        for case in ("missing", "foreign", "stale", "terminal", "outside", "web", "nested"):
            with self.subTest(case=case):
                self.write_transcript()
                event = self.event()
                if case == "missing":
                    event.pop("transcript_path")
                elif case == "foreign":
                    self.write_transcript(session="another-session")
                elif case == "stale":
                    self.write_transcript(turn="later")
                elif case == "terminal":
                    self.write_transcript(terminal=True)
                elif case == "outside":
                    outside = self.root / "fake.jsonl"
                    outside.write_bytes(self.transcript.read_bytes())
                    event["transcript_path"] = str(outside)
                elif case == "web":
                    event["controller_host"] = "web"
                else:
                    self.write_transcript(turn="old")
                    with self.transcript.open("a") as stream:
                        stream.write(json.dumps({"type": "response_item", "payload": {
                            "type": "message", "text": json.dumps({"type": "task_started", "turn_id": "new"}),
                        }}) + "\n")
                prior = {**self.prior, "must_yield": True}
                output, state = lifecycle_hook.evaluate_event(event, snapshot=self.snapshot, prior_state=prior)
                self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")
                self.assertEqual(state["active_turn_id"], "old")
                self.assertTrue(state["must_yield"])
                self.assertEqual(state["tool_trace"], prior["tool_trace"])

    def test_overflow_stop_ends_loop_without_closing_pending_work(self):
        for _ in range(3):
            output, state = lifecycle_hook.evaluate_event(
                self.event("Stop", turn="old"), snapshot=self.snapshot, prior_state=self.prior,
            )
            self.assertIs(output.get("continue"), False)
            self.assertNotEqual(output.get("decision"), "block")
            self.assertTrue(state["pending_control_event"])
            self.assertTrue(state["tool_trace_overflow"])
            self.assertFalse(state["must_yield"])
            self.assertEqual(state["tool_trace"], self.prior["tool_trace"])
            self.assertNotIn("goal_block_authorization", state)
            self.assertEqual(state["adapter_fault"]["code"], "tool_trace_overflow")
            self.prior = state

    def test_unresolved_inflight_at_stop_is_not_an_infinite_receipt_retry(self):
        prior = {**self.prior, "tool_trace_overflow": False}
        output, state = lifecycle_hook.evaluate_event(
            self.event("Stop", turn="old"), snapshot=self.snapshot, prior_state=prior,
        )
        self.assertIs(output.get("continue"), False)
        self.assertEqual(state["inflight_tool_use_ids"], ["unfinished"])
        self.assertTrue(state["pending_control_event"])

    def test_unverified_turn_stop_preserves_old_state_and_ends_loop(self):
        event = self.event("Stop")
        event.pop("transcript_path")
        prior = {**self.prior, "tool_trace_overflow": False, "inflight_tool_use_ids": []}
        output, state = lifecycle_hook.evaluate_event(event, snapshot=self.snapshot, prior_state=prior)
        self.assertIs(output.get("continue"), False)
        self.assertEqual(state["active_turn_id"], "old")
        self.assertEqual(state["adapter_fault"]["code"], "unverified_turn_boundary")

    def test_archive_failure_denies_transition_without_losing_old_evidence(self):
        path = self.root / "state.json"
        lifecycle_hook.write_json(path, self.prior)
        path.with_suffix(".turns.jsonl").mkdir()
        output, state = lifecycle_hook.persist_event_state(path, self.event(), self.snapshot)
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(state["active_turn_id"], "old")
        self.assertEqual(lifecycle_hook.load_json(path), self.prior)

    def test_late_tool_result_cannot_pollute_or_unlock_current_turn(self):
        prior = {**self.prior, "active_turn_id": "new", "must_yield": True,
                 "tool_trace_overflow": False, "tool_trace": [], "inflight_tool_use_ids": []}
        event = self.event("PostToolUse", turn="old")
        event["tool_response"] = {"exit_code": 0}
        _output, state = lifecycle_hook.evaluate_event(event, snapshot=self.snapshot, prior_state=prior)
        self.assertEqual(state["tool_trace"], [])
        self.assertTrue(state["must_yield"])
        self.assertEqual(state["active_turn_id"], "new")

    def test_trace_overflow_blocks_new_tool_without_erasing_trace(self):
        output, state = lifecycle_hook.evaluate_event(
            self.event(turn="old"), snapshot=self.snapshot, prior_state=self.prior,
        )
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(state["tool_trace"], self.prior["tool_trace"])
        self.assertEqual(state["inflight_tool_use_ids"], ["unfinished"])

    def test_valid_delegated_turn_unlocks_prior_receipt_once(self):
        prior = {**self.prior, "must_yield": True, "receipt_turn_id": "old"}
        output, state = lifecycle_hook.evaluate_event(self.event(), snapshot=self.snapshot, prior_state=prior)
        self.assertEqual(output, {})
        self.assertFalse(state["must_yield"])
        self.assertNotIn("receipt_turn_id", state)
        state["must_yield"] = True
        output, state = lifecycle_hook.evaluate_event(self.event(), snapshot=self.snapshot, prior_state=state)
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertTrue(state["must_yield"])

    def test_normal_pending_stop_does_not_gain_a_retry_count_escape(self):
        prior = {**self.prior, "tool_trace_overflow": False, "inflight_tool_use_ids": []}
        for _ in range(3):
            output, prior = lifecycle_hook.evaluate_event(
                self.event("Stop", turn="old"), snapshot=self.snapshot, prior_state=prior,
            )
            self.assertEqual(output.get("decision"), "block")
            self.assertTrue(prior["pending_control_event"])


if __name__ == "__main__":
    unittest.main()
