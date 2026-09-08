import unittest
import json
import sys
import tempfile
from pathlib import Path

from scripts import goal_display_sync
from scripts import lifecycle_hook


class GoalDisplaySyncTests(unittest.TestCase):
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
