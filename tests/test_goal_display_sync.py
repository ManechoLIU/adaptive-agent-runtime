import unittest
import copy
import os
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from scripts import goal_display_sync
from scripts import lifecycle_hook
from scripts import web_lifecycle_bridge


class GoalDisplaySyncTests(unittest.TestCase):
    def start_rebind(self) -> dict[str, object]:
        receipt = goal_display_sync.start_target_goal_rebind(
            None,
            {
                "ledger_sha256": "ledger-rebind",
                "objective": "M1-F5-B / OUTLINE GENERATION AND EDITING",
                "project_name": "SelfAlone",
            },
            controller_id="controller-1",
            source_session_id="desktop-next",
            host="desktop_codex",
            target_generation=5,
            turn_id="turn-rebind",
            host_capabilities={
                "create_goal", "set_thread_title", "get_goal", "list_threads"
            },
        )
        self.assertIsNotNone(receipt)
        return receipt  # type: ignore[return-value]

    def rebind_event(
        self, kind: str, tool_use_id: str, tool_input: dict[str, object]
    ) -> dict[str, object]:
        return {
            "tool_name": kind,
            "tool_use_id": tool_use_id,
            "tool_input": tool_input,
            "turn_id": "turn-rebind",
            "controller_session_id": "controller-1",
            "source_session_id": "desktop-next",
            "controller_host": "desktop_codex",
            "controller_target_generation": 5,
        }

    def test_target_rebind_restores_missing_goal_before_controller_work(self) -> None:
        receipt = self.start_rebind()
        self.assertEqual(receipt["status"], "pending_existing_goal_readback")

        readback = self.rebind_event("get_goal", "goal-existing", {})
        receipt = self.complete_step(
            receipt, readback, response={"goal": None, "isError": False}
        )
        self.assertEqual(receipt["status"], "pending_create_goal")

        create = self.rebind_event(
            "create_goal",
            "goal-create",
            {"objective": "M1-F5-B / OUTLINE GENERATION AND EDITING"},
        )
        receipt = self.complete_step(receipt, create)
        self.assertEqual(receipt["status"], "pending_thread_title")

    def test_target_rebind_preserves_matching_active_goal_without_recreating_it(self) -> None:
        receipt = self.start_rebind()
        readback = self.rebind_event("get_goal", "goal-existing", {})
        receipt = self.complete_step(
            receipt,
            readback,
            response={
                "goal": {
                    "objective": "M1-F5-B / OUTLINE GENERATION AND EDITING",
                    "status": "active",
                },
                "isError": False,
            },
        )
        self.assertEqual(receipt["status"], "pending_thread_title")

    def test_lifecycle_hydrates_target_rebind_and_blocks_business_tool_first(self) -> None:
        event = {
            **self.rebind_event("exec_command", "business-1", {"cmd": "git status"}),
            "hook_event_name": "PreToolUse",
            "session_id": "controller-1",
            "goal_rebind_contract": {
                "ledger_sha256": "ledger-rebind",
                "objective": "M1-F5-B / OUTLINE GENERATION AND EDITING",
                "project_name": "SelfAlone",
                "controller_id": "controller-1",
                "execution_target_session_id": "desktop-next",
                "host": "desktop_codex",
                "target_generation": 5,
            },
        }
        output, state = lifecycle_hook.evaluate_event(
            event,
            snapshot={"control_loop_required": False, "rule_handshake": {}},
            prior_state={},
        )

        self.assertEqual(
            output["hookSpecificOutput"]["permissionDecision"], "deny"
        )
        self.assertIn("get_goal", output["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertEqual(
            state["goal_display_sync"]["status"], "pending_existing_goal_readback"
        )
        self.assertTrue(state["pending_control_event"])

    def test_completed_target_rebind_cannot_overwrite_a_later_goal_rollover(self) -> None:
        completed_rebind = self.start_rebind()
        completed_rebind["status"] = "completed"
        later_rollover = goal_display_sync.start_goal_display_sync(
            None,
            self.contract(),
            controller_id="controller-1",
            source_session_id="desktop-next",
            host="desktop_codex",
            target_generation=5,
            turn_id="turn-rollover",
            host_capabilities={
                "update_goal", "create_goal", "set_thread_title", "get_goal", "list_threads"
            },
        )
        self.assertIsNotNone(later_rollover)
        contract = {
            "ledger_sha256": "ledger-rebind",
            "objective": "M1-F5-B / OUTLINE GENERATION AND EDITING",
            "project_name": "SelfAlone",
            "controller_id": "controller-1",
            "execution_target_session_id": "desktop-next",
            "host": "desktop_codex",
            "target_generation": 5,
        }
        event = {
            **self.rebind_event("get_goal", "goal-new", {}),
            "hook_event_name": "PreToolUse",
            "session_id": "controller-1",
            "goal_rebind_contract": contract,
        }

        output, state = lifecycle_hook.evaluate_event(
            event,
            snapshot={"control_loop_required": False, "rule_handshake": {}},
            prior_state={
                "goal_display_sync": later_rollover,
                "goal_rebind_completed_fingerprints": [completed_rebind["fingerprint"]],
                "pending_control_event": True,
            },
        )

        self.assertEqual(state["goal_display_sync"]["fingerprint"], later_rollover["fingerprint"])
        self.assertEqual(output, {})
        self.assertNotIn("inflight", state["goal_display_sync"])

    def contract(self, *, status: str = "rolled") -> dict[str, object]:
        return {
            "status": status,
            "ledger_sha256": "ledger-abc",
            "closed_goal_id": "M1-F4",
            "current_goal_id": "M1-F5-B",
            "current_goal_display": "M1-F5-B 大纲闭环",
            "project_name": "SelfAlone",
            "project_recomputed": True,
        }

    def event(self, kind: str, tool_use_id: str, tool_input: dict[str, object]) -> dict[str, object]:
        return {
            "tool_name": kind,
            "tool_use_id": tool_use_id,
            "tool_input": tool_input,
            "turn_id": "turn-rollover",
            "controller_session_id": "controller-1",
            "source_session_id": "desktop-current",
            "controller_host": "desktop_codex",
            "controller_target_generation": 4,
        }

    def start(self, contract: dict[str, object] | None = None) -> dict[str, object]:
        receipt = goal_display_sync.start_goal_display_sync(
            None,
            contract or self.contract(),
            controller_id="controller-1",
            source_session_id="desktop-current",
            host="desktop_codex",
            target_generation=4,
            turn_id="turn-rollover",
            host_capabilities={
                "update_goal", "create_goal", "set_thread_title", "get_goal", "list_threads"
            },
        )
        self.assertIsNotNone(receipt)
        return receipt  # type: ignore[return-value]

    def complete_step(
        self,
        receipt: dict[str, object],
        event: dict[str, object],
        *, success: bool = True, response: dict[str, object] | None = None,
    ) -> dict[str, object]:
        authorized, denial = goal_display_sync.authorize_goal_display_sync_tool(receipt, event)
        self.assertIsNone(denial)
        response = response or {"isError": not success, "result": "ok" if success else "failed"}
        return goal_display_sync.observe_goal_display_sync_result(
            authorized,
            {**event, "tool_response": response},
        )

    def test_rolled_happy_path_records_exact_host_sequence_and_binding(self) -> None:
        receipt = self.start()
        update = self.event("update_goal", "update-1", {"status": "complete"})
        receipt = self.complete_step(receipt, update)
        self.assertEqual(receipt["status"], "pending_create_goal")

        create = self.event(
            "create_goal", "create-1", {"objective": "M1-F5-B 大纲闭环"}
        )
        receipt = self.complete_step(receipt, create)
        self.assertEqual(receipt["status"], "pending_thread_title")

        title = self.event(
            "mcp__codex_app__set_thread_title",
            "title-1",
            {"title": "SelfAlone 总控｜M1-F5-B 大纲闭环"},
        )
        receipt = self.complete_step(receipt, title)

        self.assertEqual(receipt["status"], "pending_goal_readback")
        receipt = self.complete_step(
            receipt,
            self.event("get_goal", "goal-read-1", {}),
            response={"objective": "M1-F5-B 大纲闭环", "status": "active"},
        )
        self.assertEqual(receipt["status"], "pending_title_readback")
        receipt = self.complete_step(
            receipt,
            self.event("mcp__codex_app__list_threads", "title-read-1", {"limit": 10}),
            response={"threads": [{
                "threadId": "desktop-current",
                "title": "SelfAlone 总控｜M1-F5-B 大纲闭环",
            }]},
        )

        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(receipt["controller_id"], "controller-1")
        self.assertEqual(receipt["execution_target_session_id"], "desktop-current")
        self.assertEqual(receipt["target_generation"], 4)
        self.assertEqual(receipt["ledger_sha256"], "ledger-abc")
        self.assertEqual(receipt["closed_goal_id"], "M1-F4")
        self.assertEqual(receipt["current_goal_id"], "M1-F5-B")
        self.assertEqual([item["step"] for item in receipt["steps"]], [
            "update_goal_complete", "create_goal", "set_thread_title",
            "get_goal_readback", "thread_title_readback",
        ])

    def test_create_goal_is_rejected_until_old_goal_completion_succeeds(self) -> None:
        receipt = self.start()
        _receipt, denial = goal_display_sync.authorize_goal_display_sync_tool(
            receipt,
            self.event("create_goal", "create-early", {"objective": "M1-F5-B 大纲闭环"}),
        )
        self.assertIn("update_goal", denial or "")
        self.assertEqual(receipt["status"], "pending_update_goal")

    def test_title_failure_recovers_without_recreating_goal(self) -> None:
        receipt = self.start()
        receipt = self.complete_step(
            receipt, self.event("update_goal", "update-1", {"status": "complete"})
        )
        receipt = self.complete_step(
            receipt,
            self.event("create_goal", "create-1", {"objective": "M1-F5-B 大纲闭环"}),
        )
        failed_title = self.event(
            "set_thread_title", "title-fail", {"title": "SelfAlone 总控｜M1-F5-B 大纲闭环"}
        )
        receipt = self.complete_step(receipt, failed_title, success=False)
        self.assertEqual(receipt["status"], "pending_thread_title")
        self.assertEqual(len(receipt["steps"]), 2)

        _receipt, denial = goal_display_sync.authorize_goal_display_sync_tool(
            receipt,
            self.event("create_goal", "create-again", {"objective": "M1-F5-B 大纲闭环"}),
        )
        self.assertIn("set_thread_title", denial or "")
        receipt = self.complete_step(
            receipt,
            self.event(
                "set_thread_title", "title-retry", {"title": "SelfAlone 总控｜M1-F5-B 大纲闭环"}
            ),
        )
        self.assertEqual(receipt["status"], "pending_goal_readback")
        receipt = self.complete_step(
            receipt,
            self.event("get_goal", "goal-read", {}),
            response={"objective": "M1-F5-B 大纲闭环"},
        )
        receipt = self.complete_step(
            receipt,
            self.event("list_threads", "title-read", {}),
            response={"threads": [{
                "threadId": "desktop-current",
                "title": "SelfAlone 总控｜M1-F5-B 大纲闭环",
            }]},
        )
        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(sum(step["step"] == "create_goal" for step in receipt["steps"]), 1)

    def test_duplicate_rollover_reuses_completed_receipt(self) -> None:
        receipt = self.start()
        for event in (
            self.event("update_goal", "update-1", {"status": "complete"}),
            self.event("create_goal", "create-1", {"objective": "M1-F5-B 大纲闭环"}),
            self.event("set_thread_title", "title-1", {"title": "SelfAlone 总控｜M1-F5-B 大纲闭环"}),
        ):
            receipt = self.complete_step(receipt, event)
        receipt = self.complete_step(
            receipt,
            self.event("get_goal", "goal-read", {}),
            response={"objective": "M1-F5-B 大纲闭环"},
        )
        receipt = self.complete_step(
            receipt,
            self.event("list_threads", "title-read", {}),
            response={"threads": [{
                "threadId": "desktop-current",
                "title": "SelfAlone 总控｜M1-F5-B 大纲闭环",
            }]},
        )
        duplicate = goal_display_sync.start_goal_display_sync(
            receipt,
            self.contract(),
            controller_id="controller-1",
            source_session_id="desktop-next",
            host="desktop_codex",
            target_generation=5,
            turn_id="turn-retry",
            host_capabilities={"update_goal", "create_goal", "set_thread_title", "get_goal", "list_threads"},
        )
        self.assertIs(duplicate, receipt)
        self.assertEqual(len(duplicate["steps"]), 5)

    def test_unavailable_host_tool_marks_receipt_degraded(self) -> None:
        receipt = self.start()
        update = self.event("update_goal", "update-missing", {"status": "complete"})
        receipt = self.complete_step(
            receipt,
            update,
            response={"isError": True, "error": "Tool update_goal is unavailable"},
        )
        self.assertEqual(receipt["status"], "degraded")
        self.assertEqual(receipt["reason"], "HOST_GOAL_DISPLAY_CAPABILITY_UNAVAILABLE")

    def test_host_readback_mismatch_retries_only_the_failed_read(self) -> None:
        receipt = self.start()
        for event in (
            self.event("update_goal", "update-1", {"status": "complete"}),
            self.event("create_goal", "create-1", {"objective": "M1-F5-B 大纲闭环"}),
            self.event("set_thread_title", "title-1", {"title": "SelfAlone 总控｜M1-F5-B 大纲闭环"}),
        ):
            receipt = self.complete_step(receipt, event)
        receipt = self.complete_step(
            receipt,
            self.event("get_goal", "goal-read-wrong", {}),
            response={"objective": "M1-F4 old goal"},
        )
        self.assertEqual(receipt["status"], "pending_goal_readback")
        self.assertEqual(len(receipt["steps"]), 3)
        _receipt, denial = goal_display_sync.authorize_goal_display_sync_tool(
            receipt,
            self.event("create_goal", "create-again", {"objective": "M1-F5-B 大纲闭环"}),
        )
        self.assertIn("get_goal", denial or "")

    def test_explicit_host_goal_envelope_readback_and_target_fences(self) -> None:
        receipt = self.start()
        receipt["status"] = "pending_goal_readback"
        objective = receipt["objective"]
        base_goal = {"threadId": "desktop-current", "objective": objective, "status": "active",
                     "tokensUsed": 9385, "timeUsedSeconds": 196, "createdAt": 1791983350}
        valid = self.complete_step(
            receipt, self.event("get_goal", "explicit-goal", {}),
            response={"goal": base_goal, "remainingTokens": None, "completionBudgetReport": None})
        self.assertEqual(valid["status"], "pending_title_readback")
        for response in [
            {"goal": {**base_goal, "threadId": "another-thread"}},
            {"goal": {**base_goal, "objective": "another objective"}},
            {"goal": {**base_goal, "status": "complete"}},
            {"goal": None}, {"goal": None, "objective": objective},
            {"arbitrary": {"goal": base_goal}}, {"goal": "malformed"}, {"remainingTokens": None},
        ]:
            with self.subTest(response=response):
                denied = self.complete_step(receipt, self.event("get_goal", "bad-goal", {}), response=response)
                if response.get("goal", "not-null") is None:
                    self.assertEqual(denied["status"], "pending_create_goal")
                    self.assertEqual(denied["goal_recovery_count"], 1)
                else:
                    self.assertEqual(denied["status"], "pending_goal_readback")
                    self.assertEqual(denied["last_failure"]["reason"], "HOST_READBACK_MISMATCH")

    def test_explicit_goal_absence_has_one_persistent_recovery_then_bounded_stop(self) -> None:
        receipt = self.start()
        receipt["status"] = "pending_goal_readback"
        receipt["steps"] = [{"step": "create_goal", "tool_use_id": "old-create", "turn_id": "old-turn"}]
        first = self.complete_step(receipt, self.event("get_goal", "missing-1", {}), response={"goal": None})
        self.assertEqual(first["status"], "pending_create_goal")
        self.assertEqual(first["goal_recovery_count"], 1)
        self.assertEqual(first["steps"], receipt["steps"])
        resumed = goal_display_sync.start_goal_display_sync(
            first, self.contract(), controller_id="controller-1", source_session_id="desktop-current",
            host="desktop_codex", target_generation=4, turn_id="new-turn", host_capabilities=None)
        self.assertIs(resumed, first)
        self.assertEqual(resumed["goal_recovery_count"], 1)
        _, denial = goal_display_sync.authorize_goal_display_sync_tool(
            first, self.event("create_goal", "wrong", {"objective": "wrong"}))
        self.assertIsNotNone(denial)
        first = self.complete_step(first, self.event("create_goal", "recreated", {"objective": first["objective"]}))
        first = self.complete_step(first, self.event("set_thread_title", "retitled", {"title": first["thread_title"]}))
        restored = self.complete_step(first, self.event("get_goal", "restored", {}), response={
            "goal": {"objective": first["objective"], "status": "active", "threadId": "desktop-current"}})
        restored = self.complete_step(restored, self.event("list_threads", "title-restored", {}), response={
            "threads": [{"id": "desktop-current", "title": first["thread_title"]}]})
        self.assertEqual(restored["status"], "completed")
        self.assertEqual(restored["goal_recovery_count"], 1)
        later = {**self.event("get_goal", "missing-2", {}), "turn_id": "later-turn"}
        second = self.complete_step(first, later, response={"goal": None})
        self.assertEqual(second["status"], "degraded")
        self.assertEqual(second["reason"], "HOST_GOAL_NOT_PERSISTENT")
        self.assertEqual(second["goal_recovery_count"], 1)
        state = {"goal_display_sync": second, "pending_control_event": True}
        for _ in range(3):
            self.assertNotEqual(lifecycle_hook._pending_stop_output(state, later, reason="blocked").get("decision"), "block")
        self.assertTrue(state["pending_control_event"])
        self.assertIsNone(goal_display_sync.authorize_goal_display_sync_tool(second, self.event("get_goal", "safe", {}))[1])
        self.assertIsNotNone(goal_display_sync.authorize_goal_display_sync_tool(second, self.event("send_message_to_thread", "dispatch", {}))[1])
        self.assertIsNotNone(goal_display_sync.authorize_goal_display_sync_tool(second, self.event("create_goal", "write", {"objective": second["objective"]}))[1])
        self.assertIsNone(goal_display_sync.external_block_reason({"status": "degraded", "reason": "HOST_GOAL_NOT_PERSISTENT"}))

    def test_wrong_binding_or_unmatched_absence_cannot_arm_recovery(self) -> None:
        receipt = self.start()
        receipt["status"] = "pending_goal_readback"
        event = self.event("get_goal", "query", {})
        inflight, denial = goal_display_sync.authorize_goal_display_sync_tool(receipt, event)
        self.assertIsNone(denial)
        for change in [{"tool_use_id": "wrong"}, {"controller_target_generation": 99}]:
            observed = goal_display_sync.observe_goal_display_sync_result(inflight, {**event, **change, "tool_response": {"goal": None}})
            self.assertIs(observed, inflight)
            self.assertNotIn("goal_recovery_count", observed)

    def test_unknown_sync_status_fences_mutations_and_reads_do_not_advance(self) -> None:
        receipt = self.start()
        receipt["status"] = "unexpected_status"
        for kind, args in [("exec_command", {"cmd": "touch unauthorized"}),
                           ("send_message_to_thread", {"threadId": "worker", "prompt": "dispatch"}),
                           ("create_goal", {"objective": receipt["objective"]})]:
            unchanged, denial = goal_display_sync.authorize_goal_display_sync_tool(receipt, self.event(kind, "unknown", args))
            self.assertIs(unchanged, receipt)
            self.assertIsNotNone(denial)
        unchanged, denial = goal_display_sync.authorize_goal_display_sync_tool(receipt, self.event("get_goal", "safe", {}))
        self.assertIsNone(denial)
        self.assertIs(unchanged, receipt)
        self.assertNotIn("inflight", unchanged)

    def reconcile_goal_mcp_fixture(self, *, item_changes=None, event_changes=None, closed=True, final_readback=False, wait="own"):
        receipt = self.start()
        receipt["status"] = "pending_title_readback" if final_readback else "pending_thread_title"
        tool = "list_threads" if final_readback else "set_thread_title"
        args = {"limit": 1} if final_readback else {"threadId": "desktop-current", "title": receipt["thread_title"]}
        receipt["sync_kind"] = "target_goal_rebind"
        receipt, denial = goal_display_sync.authorize_goal_display_sync_tool(
            receipt, self.event(tool, "old-title-tool", args))
        self.assertIsNone(denial)
        state = {"goal_display_sync": receipt, "active_turn_id": "new-turn", "pending_control_event": True,
                 "inflight_tool_use_ids": [], "inflight_tool_records": {}, "must_yield": False,
                 "tool_trace": [{"turn_id": "new-turn", "tool_use_id": "current-evidence"}]}
        if final_readback:
            state.update({"requires_user": True, "user_halt": wait == "user_halt",
                          "next_action": "foreign human decision" if wait == "foreign" else lifecycle_hook._goal_sync_wait_action(receipt)})
        item = {"type": "McpToolCall", "id": "old-title-tool", "server": "codex_app", "tool": tool,
                "arguments": args, "status": "completed", "result": {"isError": False,
                "content": [{"type": "text", "text": json.dumps({"threadId": "desktop-current", "title": receipt["thread_title"]})}]}}
        if final_readback:
            item["result"]["content"][0]["text"] = json.dumps({"threads": [{"id": "desktop-current", "title": receipt["thread_title"]}]})
        item.update(item_changes or {})
        with tempfile.TemporaryDirectory() as tmp:
            sessions = Path(tmp) / "sessions"
            sessions.mkdir()
            transcript = sessions / "rollout.jsonl"
            rows = [
                {"type": "session_meta", "payload": {"id": "desktop-current"}},
                {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-rollover"}},
                {"type": "event_msg", "payload": {"type": "item_completed", "turn_id": "turn-rollover", "item": item}},
            ]
            if closed:
                rows.append({"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn-rollover"}})
            rows.append({"type": "event_msg", "payload": {"type": "task_started", "turn_id": "new-turn"}})
            transcript.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
            event = {**self.event("get_goal", "current-query", {}), "hook_event_name": "PreToolUse",
                     "turn_id": "new-turn", "transcript_path": str(transcript)}
            event.update(event_changes or {})
            with patch.dict(os.environ, {"CODEX_HOME": tmp}):
                observed = lifecycle_hook._reconcile_desktop_rollout(copy.deepcopy(state), event, {})
        return state, observed

    def test_goal_local_old_mcp_terminal_recovers_without_current_turn_receipt(self) -> None:
        before, observed = self.reconcile_goal_mcp_fixture()
        sync = observed["goal_display_sync"]
        self.assertEqual(sync["status"], "pending_goal_readback")
        self.assertNotIn("inflight", sync)
        self.assertEqual(sync["last_rollout_terminal"]["source"], "codex_rollout_item_completed")
        self.assertEqual(sync["last_rollout_terminal"]["turn_id"], "turn-rollover")
        for key in ["active_turn_id", "inflight_tool_use_ids", "inflight_tool_records", "must_yield", "tool_trace", "pending_control_event"]:
            self.assertEqual(observed[key], before[key])

    def test_recovered_final_goal_readback_records_completion_and_preserves_foreign_wait(self) -> None:
        for wait in ["own", "foreign", "user_halt"]:
            before, observed = self.reconcile_goal_mcp_fixture(final_readback=True, wait=wait)
            sync = observed["goal_display_sync"]
            self.assertEqual(sync["status"], "completed")
            self.assertIn(sync["fingerprint"], observed["goal_rebind_completed_fingerprints"])
            self.assertEqual(observed["active_turn_id"], before["active_turn_id"])
            self.assertFalse(observed["must_yield"])
            self.assertEqual(observed["tool_trace"], before["tool_trace"])
            if wait == "own":
                self.assertFalse(observed["requires_user"])
                self.assertEqual(observed["next_action"], "")
            else:
                self.assertTrue(observed["requires_user"])
                self.assertEqual(observed["next_action"], before["next_action"])
            if wait == "user_halt":
                self.assertTrue(observed["user_halt"])

    def test_goal_mcp_recovery_fences_untrusted_or_nonterminal_evidence(self) -> None:
        for changes in [
            {"id": "other-id"}, {"tool": "get_goal"}, {"server": "other-server"},
            {"status": "in_progress"}, {"arguments": {"title": "wrong"}}, {"result": None},
            {"result": {}}, {"result": {"isError": False, "content": []}},
            {"result": {"content": [{"type": "text", "text": "ok"}]}},
        ]:
            with self.subTest(changes=changes):
                before, observed = self.reconcile_goal_mcp_fixture(item_changes=changes)
                self.assertEqual(observed["goal_display_sync"], before["goal_display_sync"])
        for changes in [{"controller_target_generation": 99}, {"source_session_id": "other-source"}]:
            before, observed = self.reconcile_goal_mcp_fixture(event_changes=changes)
            self.assertEqual(observed["goal_display_sync"], before["goal_display_sync"])
        before, observed = self.reconcile_goal_mcp_fixture(closed=False)
        self.assertEqual(observed["goal_display_sync"], before["goal_display_sync"])

    def test_goal_mcp_error_receipt_is_consumed_without_success(self) -> None:
        _, observed = self.reconcile_goal_mcp_fixture(item_changes={"status": "failed", "result": {
            "isError": True, "content": [{"type": "text", "text": "host operation failed"}]}})
        sync = observed["goal_display_sync"]
        self.assertEqual(sync["status"], "pending_thread_title")
        self.assertNotIn("inflight", sync)
        self.assertTrue(sync["last_failure"]["explicit_host_error"])
        self.assertFalse(observed["must_yield"])

    def test_matching_paused_goal_stops_without_recreating_or_authorizing_work(self) -> None:
        receipt = self.start()
        receipt["status"] = "pending_goal_readback"
        paused = self.complete_step(receipt, self.event("get_goal", "paused-read", {}), response={
            "goal": {"threadId": "desktop-current", "objective": receipt["objective"], "status": "paused"}})
        self.assertEqual(paused["status"], "degraded")
        self.assertEqual(goal_display_sync.external_block_reason(paused), "HOST_GOAL_PAUSED")
        state = {"goal_display_sync": paused, "pending_control_event": True, "must_yield": False}
        stop = lifecycle_hook._pending_stop_output(state, {}, reason="pending control receipt")
        self.assertFalse(stop["continue"])
        self.assertTrue(state["requires_user"])
        for tool, args in [("create_goal", {"objective": receipt["objective"]}), ("exec_command", {"cmd": "touch demo"})]:
            _, denial = goal_display_sync.authorize_goal_display_sync_tool(paused, self.event(tool, "mutation", args))
            self.assertIsNotNone(denial)
        for response in [{"goal": None}, {}, {"isError": True}, {"objective": receipt["objective"]},
                         {"goal": {"objective": receipt["objective"], "status": "active", "threadId": "foreign"}},
                         {"goal": {"objective": "foreign", "status": "active"}}]:
            observed = self.complete_step(paused, self.event("get_goal", "paused-probe", {}), response=response)
            self.assertEqual(observed["status"], "degraded")
            self.assertEqual(observed["reason"], "HOST_GOAL_PAUSED")
            self.assertEqual(goal_display_sync.external_block_reason(observed), "HOST_GOAL_PAUSED")
            self.assertNotIn("goal_recovery_count", observed)
            _, denial = goal_display_sync.authorize_goal_display_sync_tool(observed,
                self.event("create_goal", "forbidden-create", {"objective": receipt["objective"]}))
            self.assertIsNotNone(denial)
        restored = self.complete_step(paused, self.event("get_goal", "resumed-read", {}), response={
            "goal": {"threadId": "desktop-current", "objective": receipt["objective"], "status": "active"}})
        self.assertEqual(restored["status"], "pending_title_readback")
        self.assertNotIn("goal_recovery_count", restored)
        self.assertNotIn("reason", restored)

    def test_unrelated_paused_goal_cannot_authorize_stop(self) -> None:
        for changes in [{"threadId": "other-thread"}, {"objective": "another goal"}]:
            receipt = self.start()
            receipt["status"] = "pending_goal_readback"
            response = {"goal": {"threadId": "desktop-current", "objective": receipt["objective"], "status": "paused", **changes}}
            observed = self.complete_step(receipt, self.event("get_goal", "unrelated-pause", {}), response=response)
            self.assertIsNone(goal_display_sync.external_block_reason(observed))
            self.assertEqual(observed["status"], "pending_goal_readback")

    def persistent_absence(self) -> dict[str, object]:
        receipt = self.start()
        receipt.update({"status": "pending_goal_readback", "goal_recovery_count": 1})
        return self.complete_step(receipt, self.event("get_goal", "gone", {}), response={"goal": None})

    def test_external_goal_stop_suspends_canonical_continuation_without_user_halt(self) -> None:
        state = {"goal_display_sync": self.persistent_absence(), "pending_control_event": True,
                 "requires_user": False, "active_turn_id": "turn-rollover"}
        result = lifecycle_hook._pending_stop_output(state, {}, reason="blocked")
        self.assertFalse(result["continue"])
        self.assertTrue(state["requires_user"])
        self.assertFalse(state.get("user_halt", False))
        self.assertIn(state["goal_display_sync"]["fingerprint"], state["next_action"])
        with patch.object(lifecycle_hook, "load_json", return_value=state), patch.object(
                web_lifecycle_bridge, "resolve_controller_host", return_value="desktop_codex"):
            health = web_lifecycle_bridge.controller_continuation_projection(
                repo=Path("/nonexistent/read-only-fixture"), controller_id="controller-1",
                registry=Path("/nonexistent/registry"))
        self.assertFalse(health["should_continue"])
        self.assertIn("pending_control_event", health["debt_ids"])

    def test_degraded_probe_recovers_and_completion_clears_only_own_wait(self) -> None:
        receipt = self.persistent_absence()
        for wait in ["own", "foreign", "user_halt"]:
            state = {"goal_display_sync": receipt, "pending_control_event": True,
                     "active_turn_id": "turn-rollover"}
            if wait == "foreign":
                state.update({"requires_user": True, "next_action": "another human decision"})
            lifecycle_hook._pending_stop_output(state, {}, reason="blocked")
            waiting_action = state["next_action"]
            probe = {**self.event("get_goal", "restored-probe", {}), "hook_event_name": "PreToolUse"}
            _, state = lifecycle_hook.evaluate_event(probe, snapshot={"control_loop_required": False}, prior_state=state)
            self.assertIn("inflight", state["goal_display_sync"])
            _, state = lifecycle_hook.evaluate_event({**probe, "hook_event_name": "PostToolUse", "tool_response": {
                "goal": {"threadId": "desktop-current", "objective": receipt["objective"], "status": "active"}}},
                snapshot={"control_loop_required": False}, prior_state=state)
            restored = state["goal_display_sync"]
            self.assertEqual(restored["status"], "pending_title_readback")
            self.assertEqual(restored["goal_recovery_count"], 1)
            state["goal_display_sync"] = restored
            event = {**self.event("list_threads", "restored-title", {}), "hook_event_name": "PreToolUse"}
            _, state = lifecycle_hook.evaluate_event(event, snapshot={"control_loop_required": False}, prior_state=state)
            if wait == "user_halt":
                state.update({"user_halt": True, "requires_user": True})
            _, state = lifecycle_hook.evaluate_event({**event, "hook_event_name": "PostToolUse",
                "tool_response": {"threads": [{"id": "desktop-current", "title": receipt["thread_title"]}]}},
                snapshot={"control_loop_required": False}, prior_state=state)
            if wait == "own":
                self.assertEqual(state["goal_display_sync"]["status"], "completed")
                self.assertFalse(state["requires_user"])
                self.assertEqual(state["next_action"], "")
            else:
                self.assertTrue(state["requires_user"])
                self.assertEqual(state["next_action"], waiting_action)
            if wait == "user_halt":
                self.assertTrue(state["user_halt"])

    def test_degraded_null_probes_keep_budget_and_evidence_bounded(self) -> None:
        receipt = self.persistent_absence()
        for i in range(5):
            receipt = self.complete_step(receipt, self.event("get_goal", f"null-{i}", {}), response={"goal": None})
        self.assertEqual(receipt["status"], "degraded")
        self.assertEqual(receipt["goal_recovery_count"], 1)
        self.assertEqual(len(receipt["goal_absence_observations"]), 2)
        self.assertEqual(goal_display_sync.external_block_reason(receipt), "HOST_GOAL_NOT_PERSISTENT")

    def test_initial_explicit_goal_precedes_outer_legacy_fields(self) -> None:
        receipt = self.start_rebind()
        for goal in [None, {"objective": receipt["objective"], "status": "active", "threadId": "other-thread"}]:
            with self.subTest(goal=goal):
                observed = self.complete_step(
                    receipt, self.rebind_event("get_goal", "wrapped-goal", {}),
                    response={"objective": receipt["objective"], "status": "active",
                              "structuredContent": {"goal": goal}})
                self.assertEqual(observed["status"], "pending_create_goal" if goal is None else "pending_existing_goal_readback")
                self.assertNotEqual(observed["status"], "pending_thread_title")

    def test_initial_goal_envelope_wrong_thread_is_not_adopted(self) -> None:
        receipt = self.start_rebind()
        denied = self.complete_step(
            receipt, self.rebind_event("get_goal", "wrong-thread", {}),
            response={"goal": {"objective": receipt["objective"], "status": "active", "threadId": "other-thread"}})
        self.assertEqual(denied["status"], "pending_existing_goal_readback")
        self.assertEqual(denied["last_failure"]["reason"], "HOST_READBACK_MISMATCH")

    def test_host_readback_rejects_unrelated_objective_and_split_thread_match(self) -> None:
        receipt = self.start()
        for event in (
            self.event("update_goal", "update-1", {"status": "complete"}),
            self.event("create_goal", "create-1", {"objective": "M1-F5-B 大纲闭环"}),
            self.event("set_thread_title", "title-1", {"title": "SelfAlone 总控｜M1-F5-B 大纲闭环"}),
        ):
            receipt = self.complete_step(receipt, event)
        receipt = self.complete_step(
            receipt,
            self.event("get_goal", "goal-read-unrelated", {}),
            response={
                "objective": "M1-F4 old goal",
                "diagnostic": "expected M1-F5-B 大纲闭环",
            },
        )
        self.assertEqual(receipt["status"], "pending_goal_readback")

        receipt = self.complete_step(
            receipt,
            self.event("get_goal", "goal-read-valid", {}),
            response={"structuredContent": {"objective": "M1-F5-B 大纲闭环"}},
        )
        self.assertEqual(receipt["status"], "pending_title_readback")
        receipt = self.complete_step(
            receipt,
            self.event("list_threads", "title-read-split", {}),
            response={"threads": [
                {"threadId": "desktop-current", "title": "old title"},
                {"threadId": "another-thread", "title": "SelfAlone 总控｜M1-F5-B 大纲闭环"},
            ]},
        )
        self.assertEqual(receipt["status"], "pending_title_readback")
        self.assertEqual(len(receipt["steps"]), 4)

    def test_project_terminal_states_do_not_create_a_display_sync(self) -> None:
        for status in ("project_complete", "project_blocked"):
            with self.subTest(status=status):
                self.assertIsNone(goal_display_sync.start_goal_display_sync(
                    None,
                    self.contract(status=status),
                    controller_id="controller-1",
                    source_session_id="desktop-current",
                    host="desktop_codex",
                    target_generation=4,
                    turn_id="turn-terminal",
                    host_capabilities={"update_goal", "create_goal", "set_thread_title", "get_goal", "list_threads"},
                ))

    def test_missing_host_capability_is_degraded_and_exact_target_change_is_fenced(self) -> None:
        degraded = goal_display_sync.start_goal_display_sync(
            None,
            self.contract(),
            controller_id="controller-1",
            source_session_id="web-current",
            host="web",
            target_generation=7,
            turn_id="turn-web",
            host_capabilities=set(),
        )
        self.assertEqual(degraded["status"], "degraded")
        self.assertEqual(degraded["reason"], "HOST_GOAL_DISPLAY_CAPABILITY_UNAVAILABLE")

        receipt = self.start()
        changed = self.event("update_goal", "update-1", {"status": "complete"})
        changed["controller_target_generation"] = 5
        _receipt, denial = goal_display_sync.authorize_goal_display_sync_tool(receipt, changed)
        self.assertIn("target generation", denial or "")

    def test_pending_sync_allows_recovery_reads_without_advancing_receipt(self) -> None:
        receipt = self.start()
        for kind, args in [("get_goal", {}), ("request_user_input", {}),
                           ("exec_command", {"cmd": "cat TASK_LEDGER.md"}),
                           ("Bash", {"command": "cat PROJECT_STATUS.md"})]:
            with self.subTest(kind=kind, args=args):
                unchanged, denial = goal_display_sync.authorize_goal_display_sync_tool(
                    receipt, self.event(kind, "read-1", args))
                self.assertIsNone(denial)
                self.assertIs(unchanged, receipt)
                self.assertNotIn("inflight", unchanged)

    def test_pending_sync_denies_shell_writes_and_dispatch(self) -> None:
        for command in ["cat TASK_LEDGER.md > stolen", "cat TASK_LEDGER.md; touch x",
                        "python3 scripts/controller_target_guard.py bind --root .",
                        "cat $(touch x)TASK_LEDGER.md"]:
            _, denial = goal_display_sync.authorize_goal_display_sync_tool(
                self.start(), self.event("exec_command", "write-1", {"cmd": command}))
            self.assertIsNotNone(denial)

    def test_external_goal_failure_can_end_turn_without_clearing_debt(self) -> None:
        receipt = self.start()
        receipt.update({"status": "degraded", "reason": "HOST_GOAL_CONFLICT"})
        state = {"goal_display_sync": receipt, "pending_control_event": True,
                 "active_turn_id": "turn-rollover"}
        output = lifecycle_hook._pending_stop_output(
            state, {"turn_id": "turn-rollover"}, reason="blocked")
        self.assertNotEqual(output.get("decision"), "block")
        self.assertTrue(state["pending_control_event"])
        self.assertEqual(state["goal_display_sync"]["status"], "degraded")

    def test_degraded_sync_keeps_reads_open_but_dispatch_fenced(self) -> None:
        receipt = self.start()
        receipt.update({"status": "degraded", "reason": "HOST_GOAL_CONFLICT"})
        _, denial = goal_display_sync.authorize_goal_display_sync_tool(
            receipt, self.event("get_goal", "read", {}))
        self.assertIsNone(denial)
        _, denial = goal_display_sync.authorize_goal_display_sync_tool(
            receipt, self.event("send_message_to_thread", "dispatch", {}))
        self.assertIsNotNone(denial)

    def test_unknown_or_inflight_blocker_cannot_end_turn(self) -> None:
        for reason, inflight in [("invented", []), ("HOST_GOAL_CONFLICT", ["live"] )]:
            state = {"goal_display_sync": {"status": "degraded", "reason": reason},
                     "inflight_tool_use_ids": inflight}
            result = lifecycle_hook._pending_stop_output(state, {}, reason="blocked")
            self.assertEqual(result.get("decision"), "block")

    def test_degraded_sync_with_own_inflight_cannot_end_turn(self) -> None:
        state = {"goal_display_sync": {"status": "degraded", "reason": "HOST_GOAL_CONFLICT",
                                      "inflight": {"tool_use_id": "sync-live"}}}
        result = lifecycle_hook._pending_stop_output(state, {}, reason="blocked")
        self.assertEqual(result.get("decision"), "block")

    def test_observed_host_error_can_report_and_stop_without_clearing_sync(self) -> None:
        for response in [{"isError": True, "result": "connection failed"}, {"exit_code": 1}]:
            receipt = self.complete_step(
                self.start(), self.event("update_goal", "host-failed", {"status": "complete"}),
                response=response)
            self.assertEqual(receipt["status"], "pending_update_goal")
            state = {"goal_display_sync": receipt, "pending_control_event": True}
            result = lifecycle_hook._pending_stop_output(state, {}, reason="blocked")
            self.assertNotEqual(result.get("decision"), "block")
            self.assertTrue(state["pending_control_event"])
            _, denial = goal_display_sync.authorize_goal_display_sync_tool(
                receipt, self.event("send_message_to_thread", "dispatch", {}))
            self.assertIsNotNone(denial)

    def test_readback_mismatch_is_not_external_failure_stop_permission(self) -> None:
        receipt = self.start_rebind()
        receipt = self.complete_step(
            receipt, self.rebind_event("get_goal", "malformed", {}), response={"result": "ok"})
        state = {"goal_display_sync": receipt}
        self.assertEqual(lifecycle_hook._pending_stop_output(state, {}, reason="blocked").get("decision"), "block")

    def test_identity_registry_override_and_missing_default_are_not_read_only(self) -> None:
        base = "python3 scripts/controller_target_guard.py identity --repo . --host desktop_codex"
        for suffix in [" --registry /tmp/new/new.json", " --registry=/tmp/new.json", " --reg /tmp/new.json"]:
            self.assertFalse(goal_display_sync.is_recovery_read(self.event("exec_command", "id", {"cmd": base + suffix})))
        with tempfile.TemporaryDirectory() as tmp, patch.object(goal_display_sync.Path, "home", return_value=Path(tmp)):
            event = self.event("exec_command", "id", {"cmd": base})
            self.assertFalse(goal_display_sync.is_recovery_read(event))
            registry = Path(tmp) / ".codex" / "adaptive-delivery-controllers.json"
            registry.parent.mkdir()
            registry.write_text("{}")
            self.assertFalse(goal_display_sync.is_recovery_read(event))
            registry.with_suffix(".json.lock").write_text("")
            self.assertTrue(goal_display_sync.is_recovery_read(event))
            receipt = self.start()
            unchanged, denial = goal_display_sync.authorize_goal_display_sync_tool(receipt, event)
            self.assertIsNone(denial)
            self.assertIs(unchanged, receipt)
            self.assertNotIn("inflight", unchanged)

    def test_generation_drift_permits_reads_without_mutation_authority(self) -> None:
        receipt = self.start()
        read = self.event("get_goal", "query", {})
        read["controller_target_generation"] = 99
        unchanged, denial = goal_display_sync.authorize_goal_display_sync_tool(receipt, read)
        self.assertIsNone(denial)
        self.assertIs(unchanged, receipt)
        self.assertNotIn("inflight", unchanged)
        write = {**read, "tool_name": "update_goal", "tool_input": {"status": "complete"}}
        _, denial = goal_display_sync.authorize_goal_display_sync_tool(receipt, write)
        self.assertIn("generation", denial)

    def test_unrelated_parallel_task_does_not_mutate_receipt(self) -> None:
        receipt = self.start()
        unrelated = self.event("send_message_to_thread", "message-1", {"threadId": "writer-1"})
        unchanged, denial = goal_display_sync.authorize_goal_display_sync_tool(receipt, unrelated)
        self.assertIs(unchanged, receipt)
        self.assertIn("update_goal", denial or "")
        self.assertEqual(receipt["steps"], [])

    def test_control_guard_proposal_derives_exact_display_from_hashed_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "SelfAlone"
            root.mkdir()
            ledger = root / "TASK_LEDGER.md"
            ledger.write_text("- 当前 Goal：M1-F5-B 大纲闭环\n", encoding="utf-8")
            snapshot = {
                "ledger_sha256": lifecycle_hook.sha256_bytes(ledger.read_bytes()),
                "goal_rollover": {
                    "status": "rolled",
                    "closed_goal_id": "M1-F4",
                    "current_goal_id": "M1-F5-B",
                    "project_recomputed": True,
                },
            }
            receipt = root / "receipt.json"
            receipt.write_text(json.dumps(snapshot), encoding="utf-8")
            command = (
                f"{sys.executable} {Path(lifecycle_hook.__file__).with_name('control_event_guard.py')} "
                f"{receipt} --ledger {ledger} --controller-session controller-1"
            )
            proposal = lifecycle_hook._control_guard_proposal(command, cwd=root)

        self.assertEqual(proposal["goal_rollover"]["current_goal_display"], "M1-F5-B 大纲闭环")
        self.assertEqual(proposal["goal_rollover"]["project_name"], "SelfAlone")
        self.assertEqual(proposal["goal_rollover"]["ledger_sha256"], snapshot["ledger_sha256"])

    def test_lifecycle_state_enforces_and_advances_pending_display_sync(self) -> None:
        receipt = self.start()
        snapshot = {"control_loop_required": False, "rule_handshake": {}}
        wrong = self.event(
            "create_goal", "create-early", {"objective": "M1-F5-B 大纲闭环"}
        )
        output, unchanged = lifecycle_hook.evaluate_event(
            {**wrong, "hook_event_name": "PreToolUse", "session_id": "controller-1"},
            snapshot=snapshot,
            prior_state={"goal_display_sync": receipt, "pending_control_event": True},
        )
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(unchanged["goal_display_sync"]["status"], "pending_update_goal")

        update = self.event("update_goal", "update-1", {"status": "complete"})
        _output, pre_state = lifecycle_hook.evaluate_event(
            {**update, "hook_event_name": "PreToolUse", "session_id": "controller-1"},
            snapshot=snapshot,
            prior_state={"goal_display_sync": receipt, "pending_control_event": True},
        )
        self.assertEqual(pre_state["goal_display_sync"]["inflight"]["tool_use_id"], "update-1")
        _output, post_state = lifecycle_hook.evaluate_event(
            {
                **update,
                "hook_event_name": "PostToolUse",
                "session_id": "controller-1",
                "tool_response": {"isError": False, "result": "completed"},
            },
            snapshot=snapshot,
            prior_state=pre_state,
        )
        self.assertEqual(post_state["goal_display_sync"]["status"], "pending_create_goal")
        self.assertTrue(post_state["pending_control_event"])

    def test_successful_rolled_control_receipt_activates_display_sync_debt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "SelfAlone"
            root.mkdir()
            ledger = root / "TASK_LEDGER.md"
            ledger.write_text("- 当前 Goal：M1-F5-B 大纲闭环\n", encoding="utf-8")
            control_snapshot = {
                "root": str(root),
                "ledger_sha256": lifecycle_hook.sha256_bytes(ledger.read_bytes()),
                "goal_rollover": {
                    "status": "rolled",
                    "closed_goal_id": "M1-F4",
                    "current_goal_id": "M1-F5-B",
                    "project_recomputed": True,
                },
            }
            receipt_path = root / "receipt.json"
            receipt_path.write_text(json.dumps(control_snapshot), encoding="utf-8")
            command = (
                f"{sys.executable} {Path(lifecycle_hook.__file__).with_name('control_event_guard.py')} "
                f"{receipt_path} --ledger {ledger} --controller-session controller-1"
            )
            base_event = {
                "session_id": "controller-1",
                "source_session_id": "desktop-current",
                "controller_session_id": "controller-1",
                "controller_host": "desktop_codex",
                "controller_target_generation": 4,
                "turn_id": "turn-rollover",
                "tool_name": "exec_command",
                "tool_use_id": "guard-1",
                "tool_input": {"command": command},
                "cwd": str(root),
            }
            project_snapshot = {
                "root": str(root),
                "control_loop_required": False,
                "candidate_revisions": [],
                "ready_ids": [],
                "runnable_ids": [],
                "rule_handshake": {},
            }
            _output, pre_state = lifecycle_hook.evaluate_event(
                {**base_event, "hook_event_name": "PreToolUse"},
                snapshot=project_snapshot,
                prior_state={"pending_control_event": True},
            )
            _output, post_state = lifecycle_hook.evaluate_event(
                {
                    **base_event,
                    "hook_event_name": "PostToolUse",
                    "tool_response": {"exit_code": 0, "output": "control-event: allowed"},
                },
                snapshot=project_snapshot,
                prior_state=pre_state,
            )

        sync = post_state["goal_display_sync"]
        self.assertEqual(sync["status"], "pending_update_goal")
        self.assertEqual(sync["target_generation"], 4)
        self.assertTrue(post_state["pending_control_event"])
        self.assertIn("goal_display_sync:", " ".join(post_state["triggers"]))

    def test_rollover_snapshot_change_before_post_degrades_without_host_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "SelfAlone"
            root.mkdir()
            ledger = root / "TASK_LEDGER.md"
            ledger.write_text("- 当前 Goal：M1-F5-B 大纲闭环\n", encoding="utf-8")
            receipt_path = root / "receipt.json"
            receipt_path.write_text(json.dumps({
                "root": str(root),
                "ledger_sha256": lifecycle_hook.sha256_bytes(ledger.read_bytes()),
                "goal_rollover": {
                    "status": "rolled", "closed_goal_id": "M1-F4",
                    "current_goal_id": "M1-F5-B", "project_recomputed": True,
                },
            }), encoding="utf-8")
            command = (
                f"{sys.executable} {Path(lifecycle_hook.__file__).with_name('control_event_guard.py')} "
                f"{receipt_path} --ledger {ledger} --controller-session controller-1"
            )
            event = {
                "session_id": "controller-1", "source_session_id": "desktop-current",
                "controller_session_id": "controller-1", "controller_host": "desktop_codex",
                "controller_target_generation": 4, "turn_id": "turn-rollover",
                "tool_name": "exec_command", "tool_use_id": "guard-1",
                "tool_input": {"command": command}, "cwd": str(root),
            }
            project_snapshot = {
                "root": str(root), "control_loop_required": False,
                "candidate_revisions": [], "ready_ids": [], "runnable_ids": [],
                "rule_handshake": {},
            }
            _output, pre_state = lifecycle_hook.evaluate_event(
                {**event, "hook_event_name": "PreToolUse"},
                snapshot=project_snapshot,
                prior_state={"pending_control_event": True},
            )
            receipt_path.write_text("{}", encoding="utf-8")
            _output, post_state = lifecycle_hook.evaluate_event(
                {**event, "hook_event_name": "PostToolUse", "tool_response": {
                    "exit_code": 0, "output": "control-event: allowed"
                }},
                snapshot=project_snapshot,
                prior_state=pre_state,
            )
        self.assertEqual(post_state["goal_display_sync"]["status"], "degraded")
        self.assertEqual(
            post_state["goal_display_sync"]["reason"],
            "GOAL_DISPLAY_CONTRACT_UNAVAILABLE",
        )


if __name__ == "__main__":
    unittest.main()
