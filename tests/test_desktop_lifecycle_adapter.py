from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

import lifecycle_hook  # noqa: E402
import control_event_guard  # noqa: E402
import controller_target_guard  # noqa: E402


class DesktopLifecycleTurnGateTests(unittest.TestCase):
    def snapshot(self) -> dict[str, object]:
        return {
            "root": "/tmp/project",
            "head": "abc123",
            "ledger_sha256": "ledger-1",
            "worktree_status_sha256": "status-1",
            "ready_ids": [],
            "runnable_ids": [],
            "candidate_revisions": [],
            "ledger_errors": [],
            "assignment_liveness": {},
            "rule_handshake": {"state": "current", "blocking": False},
        }

    def successful_receipt(self, prior_state: dict[str, object]) -> dict[str, object]:
        _output, state = lifecycle_hook.evaluate_event(
            {
                "hook_event_name": "PostToolUse",
                "session_id": "controller-1",
                "turn_id": "turn-1",
                "tool_name": "Bash",
                "tool_use_id": "receipt-call",
                "tool_input": {
                    "command": (
                        f"{sys.executable} {SKILL_ROOT / 'scripts' / 'control_event_guard.py'} receipt.json "
                        "--ledger TASK_LEDGER.md --repo . "
                        "--controller-session controller-1"
                    )
                },
                "tool_response": {
                    "output": "control-event: allowed",
                    "exit_code": 0,
                },
            },
            snapshot=self.snapshot(),
            prior_state=prior_state,
        )
        return state

    def test_successful_receipt_is_invalidated_when_same_turn_continuation_executes(self) -> None:
        state = self.successful_receipt(
            {
                "pending_control_event": True,
                "triggers": ["READY:F1"],
                "active_turn_id": "turn-1",
            }
        )

        output, next_state = lifecycle_hook.evaluate_event(
            {
                "hook_event_name": "PreToolUse",
                "session_id": "controller-1",
                "turn_id": "turn-1",
                "tool_name": "apply_patch",
                "tool_use_id": "continue-after-receipt",
                "tool_input": {"command": "*** Begin Patch"},
            },
            snapshot=self.snapshot(),
            prior_state=state,
        )

        self.assertEqual(output, {})
        self.assertFalse(next_state["must_yield"])
        self.assertNotIn("receipt_turn_id", next_state)
        self.assertTrue(next_state["pending_control_event"])
        self.assertIn("post_receipt_action_started", next_state["triggers"])

        _post_output, after_action = lifecycle_hook.evaluate_event(
            {
                "hook_event_name": "PostToolUse",
                "session_id": "controller-1",
                "turn_id": "turn-1",
                "tool_name": "apply_patch",
                "tool_use_id": "continue-after-receipt",
                "tool_input": {"command": "*** Begin Patch"},
                "tool_response": {"output": "Done", "exit_code": 0},
            },
            snapshot=self.snapshot(),
            prior_state=next_state,
        )
        blocked, blocked_state = lifecycle_hook.evaluate_event(
            {
                "hook_event_name": "Stop",
                "session_id": "controller-1",
                "turn_id": "turn-1",
            },
            snapshot=self.snapshot(),
            prior_state=after_action,
        )
        self.assertEqual(blocked["decision"], "block")
        self.assertIn("control_event_guard.py", blocked["reason"])
        self.assertFalse(blocked_state["must_yield"])

    def test_status_query_does_not_clear_existing_controller_continuation(self) -> None:
        prior = {
            "pending_control_event": True,
            "triggers": ["RUNNABLE:MINI-READY", "next_action_pending"],
            "next_action": "integrate reviewed candidate",
            "requires_user": False,
            "active_turn_id": "turn-1",
            "must_yield": False,
        }
        _output, state = lifecycle_hook.evaluate_event(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "controller-1",
                "turn_id": "turn-status",
                "prompt": "进度怎么样？",
            },
            snapshot=self.snapshot(),
            prior_state=prior,
        )
        self.assertTrue(state["pending_control_event"])
        self.assertIn("RUNNABLE:MINI-READY", state["triggers"])
        self.assertIn("next_action_pending", state["triggers"])
        self.assertEqual(state["next_action"], "integrate reviewed candidate")
        self.assertFalse(state["requires_user"])


    def test_hard_yield_gate_rejects_declared_next_action_when_work_is_runnable(self) -> None:
        snapshot = {
            **self.snapshot(),
            "runnable_ids": ["MINI-READY"],
            "ready_ids": ["MINI-READY"],
            "control_loop_required": True,
        }
        output, state = lifecycle_hook.evaluate_event(
            {
                "hook_event_name": "Stop",
                "session_id": "controller-1",
                "turn_id": "turn-1",
                "last_assistant_message": "当前验证已经 PASS。下一步应该派发 MINI-READY。",
            },
            snapshot=snapshot,
            prior_state={
                "active_turn_id": "turn-1",
                "must_yield": True,
                "receipt_turn_id": "turn-1",
                "pending_control_event": False,
                "triggers": [],
                "snapshot": snapshot,
            },
        )
        self.assertEqual(output["decision"], "block")
        self.assertIn("不得一边声明下一步一边 Yield", output["reason"])
        self.assertFalse(state["must_yield"])
        self.assertTrue(state["pending_control_event"])
        self.assertIn("KNOWN_NEXT_ACTION_NOT_EXECUTED", state["triggers"])

    def test_hard_yield_gate_does_not_invent_work_from_status_only_message(self) -> None:
        snapshot = {
            **self.snapshot(),
            "control_loop_required": False,
        }
        output, state = lifecycle_hook.evaluate_event(
            {
                "hook_event_name": "Stop",
                "session_id": "controller-1",
                "turn_id": "turn-1",
                "last_assistant_message": "当前进度正常，暂无新的可执行工作。",
            },
            snapshot=snapshot,
            prior_state={
                "active_turn_id": "turn-1",
                "must_yield": False,
                "pending_control_event": False,
                "triggers": [],
                "snapshot": snapshot,
            },
        )
        self.assertEqual(output, {})
        self.assertFalse(state["pending_control_event"])

    def test_update_goal_blocked_is_denied_without_project_block_receipt(self) -> None:
        output, _state = lifecycle_hook.evaluate_event(
            {
                "hook_event_name": "PreToolUse",
                "session_id": "controller-1",
                "turn_id": "turn-1",
                "tool_name": "update_goal",
                "tool_use_id": "goal-block",
                "tool_input": {"status": "blocked"},
            },
            snapshot=self.snapshot(),
            prior_state={"active_turn_id": "turn-1"},
        )

        decision = output["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertIn("project_blocked", decision["permissionDecisionReason"])

    def test_project_block_receipt_authorizes_one_goal_block_in_the_same_turn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            receipt_path = Path(directory) / "receipt.json"
            receipt_path.write_text(
                json.dumps({"goal_rollover": {"status": "project_blocked"}}),
                encoding="utf-8",
            )
            command = (
                f"{sys.executable} scripts/control_event_guard.py {receipt_path} "
                "--ledger TASK_LEDGER.md --repo . "
                "--controller-session controller-1"
            )
            pre_output, pre_state = lifecycle_hook.evaluate_event(
                {
                    "hook_event_name": "PreToolUse",
                    "session_id": "controller-1",
                    "controller_session_id": "controller-1",
                    "turn_id": "turn-1",
                    "tool_name": "exec_command",
                    "tool_use_id": "receipt-call",
                    "tool_input": {"command": command},
                },
                snapshot=self.snapshot(),
                prior_state={
                    "pending_control_event": True,
                    "triggers": ["project_block_requested"],
                    "active_turn_id": "turn-1",
                },
            )
            self.assertEqual(pre_output, {})

            _post_output, receipt_state = lifecycle_hook.evaluate_event(
                {
                    "hook_event_name": "PostToolUse",
                    "session_id": "controller-1",
                    "controller_session_id": "controller-1",
                    "turn_id": "turn-1",
                    "tool_name": "exec_command",
                    "tool_use_id": "receipt-call",
                    "tool_input": {"command": command},
                    "tool_response": {
                        "output": "control-event: allowed",
                        "exit_code": 0,
                    },
                },
                snapshot=self.snapshot(),
                prior_state=pre_state,
            )

            allowed, allowed_state = lifecycle_hook.evaluate_event(
                {
                    "hook_event_name": "PreToolUse",
                    "session_id": "controller-1",
                    "turn_id": "turn-1",
                    "tool_name": "functions.update_goal",
                    "tool_use_id": "goal-block",
                    "tool_input": {"status": "blocked"},
                },
                snapshot=self.snapshot(),
                prior_state=receipt_state,
            )
            self.assertEqual(allowed, {})

            denied, _denied_state = lifecycle_hook.evaluate_event(
                {
                    "hook_event_name": "PreToolUse",
                    "session_id": "controller-1",
                    "turn_id": "turn-1",
                    "tool_name": "update_goal",
                    "tool_use_id": "goal-block-again",
                    "tool_input": {"status": "blocked"},
                },
                snapshot=self.snapshot(),
                prior_state=allowed_state,
            )
            self.assertEqual(
                denied["hookSpecificOutput"]["permissionDecision"], "deny"
            )

    def test_plain_shell_output_cannot_spoof_a_successful_receipt(self) -> None:
        _output, state = lifecycle_hook.evaluate_event(
            {
                "hook_event_name": "PostToolUse",
                "session_id": "controller-1",
                "controller_session_id": "controller-1",
                "turn_id": "turn-1",
                "tool_name": "Bash",
                "tool_use_id": "spoof",
                "tool_input": {
                    "command": (
                        "printf 'control-event: allowed' # control_event_guard.py "
                        "--controller-session controller-1"
                    )
                },
                "tool_response": {"output": "control-event: allowed", "exit_code": 0},
            },
            snapshot=self.snapshot(),
            prior_state={
                "pending_control_event": True,
                "triggers": ["READY:F1"],
                "active_turn_id": "turn-1",
            },
        )

        self.assertTrue(state["pending_control_event"])
        self.assertFalse(state["must_yield"])

    def test_same_named_script_outside_the_skill_cannot_spoof_a_receipt(self) -> None:
        _output, state = lifecycle_hook.evaluate_event(
            {
                "hook_event_name": "PostToolUse",
                "session_id": "controller-1",
                "controller_session_id": "controller-1",
                "turn_id": "turn-1",
                "tool_name": "Bash",
                "tool_use_id": "fake-script",
                "tool_input": {
                    "command": (
                        "python3 /tmp/fake-control_event_guard.py receipt.json "
                        "--ledger TASK_LEDGER.md --repo . "
                        "--controller-session controller-1"
                    )
                },
                "tool_response": {"output": "control-event: allowed", "exit_code": 0},
            },
            snapshot=self.snapshot(),
            prior_state={
                "pending_control_event": True,
                "triggers": ["READY:F1"],
                "active_turn_id": "turn-1",
            },
        )

        self.assertTrue(state["pending_control_event"])
        self.assertFalse(state["must_yield"])

    def test_untrusted_python_interpreter_cannot_spoof_a_receipt(self) -> None:
        guard = Path(lifecycle_hook.__file__).resolve().with_name(
            "control_event_guard.py"
        )
        _output, state = lifecycle_hook.evaluate_event(
            {
                "hook_event_name": "PostToolUse",
                "session_id": "controller-1",
                "controller_session_id": "controller-1",
                "turn_id": "turn-1",
                "tool_name": "Bash",
                "tool_use_id": "fake-python",
                "tool_input": {
                    "command": (
                        f"/tmp/python3 {guard} receipt.json "
                        "--ledger TASK_LEDGER.md --repo . "
                        "--controller-session controller-1"
                    )
                },
                "tool_response": {"output": "control-event: allowed", "exit_code": 0},
            },
            snapshot=self.snapshot(),
            prior_state={
                "pending_control_event": True,
                "triggers": ["READY:F1"],
                "active_turn_id": "turn-1",
            },
        )

        self.assertTrue(state["pending_control_event"])
        self.assertFalse(state["must_yield"])

    def test_locked_turn_without_turn_id_fails_closed(self) -> None:
        state = self.successful_receipt(
            {
                "pending_control_event": True,
                "triggers": ["READY:F1"],
                "active_turn_id": "turn-1",
            }
        )

        output, _next_state = lifecycle_hook.evaluate_event(
            {
                "hook_event_name": "PreToolUse",
                "session_id": "controller-1",
                "tool_name": "spawn_agent",
                "tool_use_id": "missing-turn",
                "tool_input": {"task_name": "late-agent"},
            },
            snapshot=self.snapshot(),
            prior_state=state,
        )

        self.assertEqual(
            output["hookSpecificOutput"]["permissionDecision"], "deny"
        )

    def test_control_receipt_preflight_is_serialized_against_other_tools(self) -> None:
        prior = {
            "pending_control_event": True,
            "triggers": ["READY:F1"],
            "active_turn_id": "turn-1",
            "inflight_tool_use_ids": ["parallel-tool"],
        }
        output, state = lifecycle_hook.evaluate_event(
            {
                "hook_event_name": "PreToolUse",
                "session_id": "controller-1",
                "turn_id": "turn-1",
                "tool_name": "Bash",
                "tool_use_id": "receipt-call",
                "tool_input": {
                    "command": (
                        f"{sys.executable} {SKILL_ROOT / 'scripts' / 'control_event_guard.py'} receipt.json "
                        "--ledger TASK_LEDGER.md --repo . "
                        "--controller-session controller-1"
                    )
                },
            },
            snapshot=self.snapshot(),
            prior_state=prior,
        )

        self.assertEqual(
            output["hookSpecificOutput"]["permissionDecision"], "deny"
        )
        self.assertNotIn("receipt-call", state["inflight_tool_use_ids"])

    def test_other_tools_are_denied_while_control_receipt_is_inflight(self) -> None:
        guard_event = {
            "hook_event_name": "PreToolUse",
            "session_id": "controller-1",
            "controller_session_id": "controller-1",
            "turn_id": "turn-1",
            "tool_name": "Bash",
            "tool_use_id": "receipt-call",
            "tool_input": {
                "command": (
                    f"{sys.executable} {SKILL_ROOT / 'scripts' / 'control_event_guard.py'} receipt.json "
                    "--ledger TASK_LEDGER.md --repo . "
                    "--controller-session controller-1"
                )
            },
        }
        allowed, state = lifecycle_hook.evaluate_event(
            guard_event,
            snapshot=self.snapshot(),
            prior_state={"active_turn_id": "turn-1"},
        )
        denied, _state = lifecycle_hook.evaluate_event(
            {
                "hook_event_name": "PreToolUse",
                "session_id": "controller-1",
                "turn_id": "turn-1",
                "tool_name": "apply_patch",
                "tool_use_id": "parallel-write",
                "tool_input": {"command": "*** Begin Patch"},
            },
            snapshot=self.snapshot(),
            prior_state=state,
        )

        self.assertEqual(allowed, {})
        self.assertEqual(
            denied["hookSpecificOutput"]["permissionDecision"], "deny"
        )

    def test_a_new_turn_clears_the_yield_latch(self) -> None:
        state = self.successful_receipt(
            {
                "pending_control_event": True,
                "triggers": ["READY:F1"],
                "active_turn_id": "turn-1",
            }
        )

        denied, still_locked = lifecycle_hook.evaluate_event(
            {
                "hook_event_name": "PreToolUse",
                "session_id": "controller-1",
                "turn_id": "turn-2",
                "tool_name": "apply_patch",
                "tool_use_id": "next-turn-write",
                "tool_input": {"command": "*** Begin Patch"},
            },
            snapshot=self.snapshot(),
            prior_state=state,
        )

        _prompt_output, prompted = lifecycle_hook.evaluate_event(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "controller-1",
                "turn_id": "turn-2",
            },
            snapshot=self.snapshot(),
            prior_state=still_locked,
        )
        output, next_state = lifecycle_hook.evaluate_event(
            {
                "hook_event_name": "PreToolUse",
                "session_id": "controller-1",
                "turn_id": "turn-2",
                "tool_name": "apply_patch",
                "tool_use_id": "next-turn-write",
                "tool_input": {"command": "*** Begin Patch"},
            },
            snapshot=self.snapshot(),
            prior_state=prompted,
        )

        self.assertEqual(
            denied["hookSpecificOutput"]["permissionDecision"], "deny"
        )
        self.assertEqual(output, {})
        self.assertFalse(next_state["must_yield"])
        self.assertEqual(next_state["active_turn_id"], "turn-2")

    def test_post_tool_use_records_a_bounded_machine_trace(self) -> None:
        _output, state = lifecycle_hook.evaluate_event(
            {
                "hook_event_name": "PostToolUse",
                "session_id": "controller-1",
                "turn_id": "turn-1",
                "tool_name": "apply_patch",
                "tool_use_id": "patch-1",
                "tool_input": {"command": "*** Begin Patch\nsecret-free\n*** End Patch"},
                "tool_response": {"output": "Done", "exit_code": 0},
            },
            snapshot=self.snapshot(),
            prior_state={"pending_control_event": False, "triggers": []},
        )

        self.assertEqual(state["active_turn_id"], "turn-1")
        self.assertEqual(len(state["tool_trace"]), 1)
        entry = state["tool_trace"][0]
        self.assertEqual(entry["tool_use_id"], "patch-1")
        self.assertEqual(entry["tool_name"], "apply_patch")

        self.assertEqual(len(entry["input_sha256"]), 64)
        self.assertNotIn("secret-free", str(entry))

    def test_machine_trace_overflow_blocks_a_registered_receipt(self) -> None:
        state = {"active_turn_id": "turn-1", "tool_trace": []}
        for index in range(lifecycle_hook.MAX_TOOL_TRACE_ENTRIES + 1):
            _output, state = lifecycle_hook.evaluate_event(
                {
                    "hook_event_name": "PostToolUse",
                    "session_id": "controller-1",
                    "turn_id": "turn-1",
                    "tool_name": "Bash",
                    "tool_use_id": f"call-{index}",
                    "tool_input": {"command": f"true # {index}"},
                    "tool_response": {"exit_code": 0},
                },
                snapshot=self.snapshot(),
                prior_state=state,
            )

        self.assertTrue(state["tool_trace_overflow"])
        with self.assertRaisesRegex(ValueError, "overflow"):
            control_event_guard.observed_machine_trace_from_state(state)

    def test_printing_the_trace_does_not_change_the_trace_it_printed(self) -> None:
        prior = {
            "active_turn_id": "turn-1",
            "must_yield": False,
            "tool_trace": [
                {
                    "turn_id": "turn-1",
                    "tool_use_id": "patch-1",
                    "tool_name": "apply_patch",
                    "input_sha256": "a" * 64,
                    "response_status": 0,
                }
            ],
        }
        expected = lifecycle_hook.machine_trace_projection(prior)

        _output, state = lifecycle_hook.evaluate_event(
            {
                "hook_event_name": "PostToolUse",
                "session_id": "controller-1",
                "turn_id": "turn-1",
                "tool_name": "Bash",
                "tool_use_id": "trace-read",
                "tool_input": {
                    "command": (
                        "python3 scripts/lifecycle_hook.py "
                        "--print-machine-trace controller-1"
                    )
                },
                "tool_response": {"output": "{}", "exit_code": 0},
            },
            snapshot=self.snapshot(),
            prior_state=prior,
        )

        self.assertEqual(lifecycle_hook.machine_trace_projection(state), expected)


class DesktopOutboundLeaseHookTests(unittest.TestCase):
    def snapshot(self, root: Path) -> dict[str, object]:
        return {
            "root": str(root), "head": "abc123", "ledger_sha256": "ledger-1",
            "worktree_status_sha256": "status-1", "ready_ids": [], "runnable_ids": [],
            "candidate_revisions": [], "ledger_errors": [], "assignment_liveness": {},
            "rule_handshake": {"state": "current", "blocking": False},
        }

    def make_repo(self, root: Path) -> Path:
        repo = root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
        return repo

    def invoke_hook(self, event: dict[str, object]) -> tuple[int, str]:
        output = StringIO()
        with patch.object(sys, "stdin", StringIO(json.dumps(event))), redirect_stdout(output):
            code = lifecycle_hook.run_hook()
        return code, output.getvalue()

    def test_managed_controller_rejects_unbounded_dev_commands_before_state_write(self) -> None:
        commands = (
            ("Bash", "pnpm --filter @selfalone/server dev"),
            ("exec_command", "DATABASE_URL=postgres://localhost/selfalone pnpm --filter @selfalone/server exec tsx watch src/index.ts"),
            ("functions.exec_command", "pnpm dev"),
            ("functions.shell_command", "pnpm dev"),
            ("commandExecution", "pnpm dev"),
            ("exec_command", "cd apps/server && pnpm dev"),
            ("exec_command", "bash -lc 'pnpm --filter @selfalone/server dev'"),
            ("exec_command", "bash -lc 'pnpm --filter @selfalone/server dev' --help"),
            ("exec_command", "npm run dev"),
            ("exec_command", "pnpm test -- --watch"),
            ("exec_command", "tsx watch src/index.ts --once"),
            ("exec_command", "pnpm dev -- --once"),
            ("exec_command", "pnpm simulator"),
            ("exec_command", "npx vite"),
            ("exec_command", "yarn dlx vite"),
            ("exec_command", "bunx vite"),
            ("exec_command", "python3 -m http.server"),
            ("exec_command", "env -i pnpm dev"),
            ("exec_command", "env --unset HOME pnpm dev"),
            ("exec_command", "env -S 'pnpm dev'"),
            ("exec_command", "env -P /opt/homebrew/bin pnpm dev"),
            ("exec_command", "arch -arm64 pnpm dev"),
            ("exec_command", "caffeinate pnpm dev"),
            ("exec_command", "command pnpm dev"),
            ("exec_command", "command pnpm dev -v"),
            ("exec_command", "command -- pnpm dev -v"),
            ("exec_command", "nohup pnpm --filter @selfalone/server dev"),
            ("exec_command", "nice pnpm dev"),
            ("exec_command", "corepack pnpm dev"),
            ("exec_command", "bash -xec 'pnpm dev'"),
            ("exec_command", "npx --yes vite"),
            ("exec_command", "pnpm exec -- vite"),
            ("exec_command", "yarn dlx --quiet vite"),
            ("exec_command", "pnpm dlx --silent vite"),
            ("exec_command", "npm exec -c 'pnpm dev'"),
            ("exec_command", "npm exec --call 'pnpm dev'"),
            ("exec_command", "npm exec --call='pnpm dev'"),
            ("exec_command", "npm exec --script-shell sh -- vite"),
            ("exec_command", "npx --call='pnpm dev'"),
            ("exec_command", "exec -a controller pnpm dev"),
            ("exec_command", "time pnpm dev"),
            ("exec_command", "npm --prefix apps/server run dev"),
            ("exec_command", "npm --prefix=apps/server run dev"),
            ("exec_command", "pnpm -C apps/server dev"),
            ("exec_command", "pnpm --dir apps/server dev"),
            ("exec_command", "pnpm run dev:server"),
            ("exec_command", "npm run start:dev"),
            ("exec_command", "pnpm watch:server"),
            ("exec_command", "next start"),
            ("exec_command", "webpack serve"),
            ("exec_command", "flask run"),
            ("exec_command", "make dev"),
            ("exec_command", "echo \"$(pnpm dev)\""),
            ("exec_command", "printf \"%s\" \"$(vite)\""),
            ("exec_command", "rg \"$(pnpm dev)\" README.md"),
            ("exec_command", "echo $(( $(pnpm dev) + 1 ))"),
            ("exec_command", "echo `pnpm dev`"),
            ("exec_command", "cat <(pnpm dev)"),
            ("exec_command", "rg x <(vite)"),
            ("exec_command", "cat =(pnpm dev)"),
            ("exec_command", "(pnpm dev)"),
            ("exec_command", "find . -exec pnpm dev \\;"),
            ("exec_command", "find . -exec echo ok {} + -exec pnpm dev {} +"),
            ("exec_command", "find . -exec echo ok {} + -execdir vite {} +"),
            ("exec_command", "pnpm preview"),
            ("exec_command", "npm run preview"),
            ("exec_command", "npm run server"),
            ("exec_command", "python manage.py runserver"),
            ("exec_command", "pnpm dev -v"),
            ("exec_command", "vite"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            old_registry, old_state_root = lifecycle_hook.REGISTRY_PATH, lifecycle_hook.STATE_ROOT
            lifecycle_hook.REGISTRY_PATH, lifecycle_hook.STATE_ROOT = (
                root / "controllers.json",
                root / "state",
            )
            original_state = {
                "active_turn_id": "turn-1",
                "tool_trace": [],
                "inflight_tool_use_ids": [],
                "pending_control_event": True,
            }
            lifecycle_hook.write_json(
                lifecycle_hook.state_path("controller-1"), original_state
            )
            try:
                for index, (tool_name, command) in enumerate(commands, start=1):
                    with self.subTest(tool_name=tool_name, command=command), patch.object(
                        lifecycle_hook, "registered_controller_id", return_value="controller-1"
                    ), patch.object(
                        lifecycle_hook, "registered_root", return_value=repo
                    ), patch.object(
                        lifecycle_hook, "project_snapshot", return_value=self.snapshot(repo)
                    ), patch.object(
                        lifecycle_hook, "controller_event_is_managed", return_value=True
                    ), patch.object(
                        lifecycle_hook.target_guard, "locked_registry"
                    ) as locked_registry, patch.object(
                        lifecycle_hook.target_guard,
                        "active_source_controller_id",
                        return_value="controller-1",
                    ), patch.object(
                        lifecycle_hook, "registry_controller_root_matches", return_value=True
                    ), patch.object(
                        lifecycle_hook,
                        "persist_event_state",
                        return_value=({}, original_state),
                    ) as persist:
                        locked_registry.return_value.__enter__.return_value = {}
                        code, output = self.invoke_hook({
                            "hook_event_name": "PreToolUse",
                            "session_id": "desktop-current",
                            "turn_id": "turn-1",
                            "tool_name": tool_name,
                            "tool_use_id": f"foreground-{index}",
                            "tool_input": {"command": command},
                            "cwd": str(repo),
                        })
                    self.assertEqual(code, 0)
                    self.assertTrue(output, "unbounded command was allowed")
                    denial = json.loads(output)
                    self.assertEqual(
                        denial["hookSpecificOutput"]["permissionDecision"], "deny"
                    )
                    self.assertIn(
                        "persistent terminal",
                        denial["hookSpecificOutput"]["permissionDecisionReason"],
                    )
                    self.assertEqual(
                        lifecycle_hook.load_json(
                            lifecycle_hook.state_path("controller-1")
                        ),
                        original_state,
                    )
                    persist.assert_not_called()
            finally:
                lifecycle_hook.REGISTRY_PATH, lifecycle_hook.STATE_ROOT = (
                    old_registry,
                    old_state_root,
                )

    def test_foreground_command_gate_allows_bounded_work_and_skips_unmanaged_sessions(self) -> None:
        allowed_inputs = (
            {"command": "pnpm --filter @selfalone/server dev", "yield_time_ms": 1000},
            {"cmd": "timeout 5s pnpm --filter @selfalone/server dev"},
            {"cmd": "timeout --signal=KILL 5s pnpm dev"},
            {"cmd": "gtimeout --foreground 5s pnpm dev"},
            {"cmd": "timeout -- 5s pnpm dev"},
            {"command": "pnpm --filter @selfalone/server test"},
            {"command": "pnpm build"},
            {"command": "pnpm typecheck"},
            {"command": "vite --help"},
            {"command": "arch -arm64 vite --help"},
            {"command": "npm view vite version"},
            {"command": "rg 'pnpm dev' README.md"},
            {"command": "echo '$(pnpm dev)'"},
            {"command": "printf '%s' '`pnpm dev`'"},
            {"command": "pnpm test:server"},
            {"command": "npm run build:server"},
            {"command": "pnpm typecheck:server"},
            {"command": "pnpm smoke:server"},
            {"command": "items=(pnpm dev)"},
            {"command": "echo $((vite + 1))"},
            {"command": "echo $(( (vite) + 1 ))"},
            {"command": "pnpm build-storybook"},
            {"command": "npm run build:storybook"},
            {"command": "pnpm --help"},
            {"command": "tsx --version"},
            {"command": "tsx scripts/smoke.ts"},
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            for index, tool_input in enumerate(allowed_inputs, start=1):
                with self.subTest(tool_input=tool_input), patch.object(
                    lifecycle_hook, "registered_controller_id", return_value="controller-1"
                ), patch.object(
                    lifecycle_hook, "registered_root", return_value=repo
                ), patch.object(
                    lifecycle_hook, "project_snapshot", return_value=self.snapshot(repo)
                ), patch.object(
                    lifecycle_hook, "controller_event_is_managed", return_value=True
                ), patch.object(
                    lifecycle_hook.target_guard, "locked_registry"
                ) as locked_registry, patch.object(
                    lifecycle_hook.target_guard,
                    "active_source_controller_id",
                    return_value="controller-1",
                ), patch.object(
                    lifecycle_hook, "registry_controller_root_matches", return_value=True
                ), patch.object(
                    lifecycle_hook, "persist_event_state", return_value=({}, {})
                ) as persist:
                    locked_registry.return_value.__enter__.return_value = {}
                    code, output = self.invoke_hook({
                        "hook_event_name": "PreToolUse",
                        "session_id": "desktop-current",
                        "turn_id": "turn-1",
                        "tool_name": "exec_command",
                        "tool_use_id": f"allowed-{index}",
                        "tool_input": tool_input,
                        "cwd": str(repo),
                    })
                self.assertEqual(code, 0)
                self.assertEqual(output, "")
                persist.assert_called_once()

            with patch.object(
                lifecycle_hook, "registered_controller_id", return_value=None
            ), patch.object(
                lifecycle_hook,
                "persist_event_state",
                side_effect=AssertionError("unmanaged Writer/Reviewer must stay outside Controller gate"),
            ):
                code, output = self.invoke_hook({
                    "hook_event_name": "PreToolUse",
                    "session_id": "writer-session",
                    "turn_id": "turn-1",
                    "tool_name": "exec_command",
                    "tool_use_id": "writer-dev",
                    "tool_input": {"command": "pnpm --filter @selfalone/server dev"},
                    "cwd": str(repo),
                })
        self.assertEqual(code, 0)
        self.assertEqual(output, "")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            with patch.object(
                lifecycle_hook, "registered_controller_id", return_value="controller-1"
            ), patch.object(
                lifecycle_hook, "registered_root", return_value=repo
            ), patch.object(
                lifecycle_hook, "project_snapshot", return_value=self.snapshot(repo)
            ), patch.object(
                lifecycle_hook, "controller_event_is_managed", return_value=True
            ), patch.object(
                lifecycle_hook.target_guard, "locked_registry"
            ) as locked_registry, patch.object(
                lifecycle_hook.target_guard,
                "active_source_controller_id",
                return_value=None,
            ), patch.object(
                lifecycle_hook,
                "persist_event_state",
                side_effect=AssertionError("non-current alias must not enter Controller state"),
            ):
                locked_registry.return_value.__enter__.return_value = {}
                code, output = self.invoke_hook({
                    "hook_event_name": "PreToolUse",
                    "session_id": "desktop-old",
                    "turn_id": "turn-1",
                    "tool_name": "exec_command",
                    "tool_use_id": "old-alias-dev",
                    "tool_input": {"command": "pnpm dev"},
                    "cwd": str(repo),
                })
        self.assertEqual(code, 0)
        self.assertEqual(output, "")

    def test_non_controller_session_spawn_still_runs_runtime_dispatch_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            event = {
                "hook_event_name": "PreToolUse",
                "session_id": "ordinary-web-session-1",
                "turn_id": "turn-1",
                "tool_name": "collaboration.spawn_agent",
                "tool_input": {
                    "task_name": "ordinary-session-child",
                    "model": "gpt-5.6-sol",
                    "agent_type": "default",
                },
                "cwd": str(repo),
            }
            with patch.object(lifecycle_hook, "project_snapshot", return_value=self.snapshot(repo)):
                code, output = self.invoke_hook(event)

        self.assertEqual(code, 0)
        denial = json.loads(output)
        self.assertEqual(denial["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("Web Agent dispatch gate rejected collaboration.spawn_agent", denial["hookSpecificOutput"]["permissionDecisionReason"])

    def test_non_controller_session_spawn_binds_dispatch_gate_to_source_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            event = {
                "hook_event_name": "PreToolUse",
                "session_id": "ordinary-web-session-1",
                "turn_id": "turn-1",
                "tool_name": "collaboration.spawn_agent",
                "tool_input": {
                    "task_name": "ordinary-session-child",
                    "model": "gpt-5.6-sol",
                    "agent_type": "default",
                },
                "cwd": str(repo),
            }
            with patch.object(lifecycle_hook, "project_snapshot", return_value=self.snapshot(repo)), patch(
                "scripts.web_agent_execution.require_prepared_web_dispatch", return_value={}
            ) as gate:
                code, output = self.invoke_hook(event)

        self.assertEqual(code, 0)
        self.assertEqual(output, "")
        gate.assert_called_once_with(
            repo=repo.resolve(),
            controller_id=None,
            delegator_session_id="ordinary-web-session-1",
            task_name="ordinary-session-child",
            expected_model="gpt-5.6-sol",
            expected_agent_type="default",
        )

    def test_managed_outbound_pre_tool_holds_lease_until_matching_post_tool_use(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {"controller-1": {"desktop_codex": ["desktop-current"]}},
                "__controller_targets__": {"controller-1": {"desktop_codex": {
                    "status": "active", "session_id": "desktop-current", "generation": 4,
                }}},
            }), encoding="utf-8")
            old_registry, old_state_root = lifecycle_hook.REGISTRY_PATH, lifecycle_hook.STATE_ROOT
            lifecycle_hook.REGISTRY_PATH, lifecycle_hook.STATE_ROOT = registry, root / "state"
            try:
                with patch.object(lifecycle_hook, "registered_controller_id", return_value="controller-1"), patch.object(lifecycle_hook, "registered_root", return_value=repo), patch.object(lifecycle_hook, "project_snapshot", return_value=self.snapshot(repo)), patch.object(lifecycle_hook, "controller_event_is_managed", return_value=True):
                    code, output = self.invoke_hook({
                        "hook_event_name": "PreToolUse", "session_id": "desktop-current", "turn_id": "turn-1",
                        "tool_name": "mcp__codex_app__send_message_to_thread", "tool_use_id": "message-1",
                        "tool_input": {"threadId": "desktop-current", "prompt": "continue"},
                    })
                    self.assertEqual(code, 0)
                    self.assertEqual(output, "")
                    self.assertTrue(controller_target_guard.has_active_outbound_lease(repo=repo, host="desktop_codex", registry_path=registry))
                    code, output = self.invoke_hook({
                        "hook_event_name": "PostToolUse", "session_id": "desktop-current", "turn_id": "turn-1",
                        "tool_name": "mcp__codex_app__send_message_to_thread", "tool_use_id": "message-1",
                        "tool_input": {"threadId": "desktop-current", "prompt": "continue"}, "tool_response": {"isError": False},
                    })
                    self.assertFalse(controller_target_guard.has_active_outbound_lease(repo=repo, host="desktop_codex", registry_path=registry))
            finally:
                lifecycle_hook.REGISTRY_PATH, lifecycle_hook.STATE_ROOT = old_registry, old_state_root

        self.assertEqual(code, 0)
        self.assertFalse(output)

    def test_post_tool_collision_does_not_release_a_different_outbound_target_lease(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {"controller-1": {"desktop_codex": ["desktop-current"]}},
                "__controller_targets__": {"controller-1": {"desktop_codex": {
                    "status": "active", "session_id": "desktop-current", "generation": 4,
                }}},
            }), encoding="utf-8")
            old_registry, old_state_root = lifecycle_hook.REGISTRY_PATH, lifecycle_hook.STATE_ROOT
            lifecycle_hook.REGISTRY_PATH, lifecycle_hook.STATE_ROOT = registry, root / "state"
            try:
                with patch.object(lifecycle_hook, "registered_controller_id", return_value="controller-1"), patch.object(lifecycle_hook, "registered_root", return_value=repo), patch.object(lifecycle_hook, "project_snapshot", return_value=self.snapshot(repo)), patch.object(lifecycle_hook, "controller_event_is_managed", return_value=True):
                    self.invoke_hook({
                        "hook_event_name": "PreToolUse", "session_id": "desktop-current", "turn_id": "turn-1",
                        "tool_name": "mcp__codex_app__send_message_to_thread", "tool_use_id": "message-1",
                        "tool_input": {"threadId": "desktop-current", "prompt": "continue"},
                    })
                    code, output = self.invoke_hook({
                        "hook_event_name": "PostToolUse", "session_id": "desktop-current", "turn_id": "turn-1",
                        "tool_name": "mcp__codex_app__send_message_to_thread", "tool_use_id": "message-1",
                        "tool_input": {"threadId": "desktop-other", "prompt": "collision"},
                        "tool_response": {"isError": False},
                    })
                    self.assertTrue(controller_target_guard.has_active_outbound_lease(repo=repo, host="desktop_codex", registry_path=registry))
            finally:
                lifecycle_hook.REGISTRY_PATH, lifecycle_hook.STATE_ROOT = old_registry, old_state_root

        self.assertEqual(code, 0)
        self.assertFalse(output)

    def test_post_receipt_outbound_continuation_holds_lease_until_post_tool(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {"controller-1": {"desktop_codex": ["desktop-current"]}},
                "__controller_targets__": {"controller-1": {"desktop_codex": {
                    "status": "active", "session_id": "desktop-current", "generation": 4,
                }}},
            }), encoding="utf-8")
            old_registry, old_state_root = lifecycle_hook.REGISTRY_PATH, lifecycle_hook.STATE_ROOT
            lifecycle_hook.REGISTRY_PATH, lifecycle_hook.STATE_ROOT = registry, root / "state"
            lifecycle_hook.write_json(lifecycle_hook.state_path("controller-1"), {
                "active_turn_id": "turn-1", "must_yield": True, "receipt_turn_id": "turn-1",
            })
            try:
                with patch.object(lifecycle_hook, "registered_controller_id", return_value="controller-1"), patch.object(lifecycle_hook, "registered_root", return_value=repo), patch.object(lifecycle_hook, "project_snapshot", return_value=self.snapshot(repo)), patch.object(lifecycle_hook, "controller_event_is_managed", return_value=True):
                    code, output = self.invoke_hook({
                        "hook_event_name": "PreToolUse", "session_id": "desktop-current", "turn_id": "turn-1",
                        "tool_name": "mcp__codex_app__send_message_to_thread", "tool_use_id": "continue-1",
                        "tool_input": {"threadId": "desktop-current", "prompt": "continue mandatory successor"},
                    })
                    self.assertTrue(
                        controller_target_guard.has_active_outbound_lease(
                            repo=repo, host="desktop_codex", registry_path=registry
                        )
                    )
                    post_code, post_output = self.invoke_hook({
                        "hook_event_name": "PostToolUse", "session_id": "desktop-current", "turn_id": "turn-1",
                        "tool_name": "mcp__codex_app__send_message_to_thread", "tool_use_id": "continue-1",
                        "tool_input": {"threadId": "desktop-current", "prompt": "continue mandatory successor"},
                        "tool_response": {"isError": False},
                    })
                    self.assertFalse(
                        controller_target_guard.has_active_outbound_lease(
                            repo=repo, host="desktop_codex", registry_path=registry
                        )
                    )
            finally:
                lifecycle_hook.REGISTRY_PATH, lifecycle_hook.STATE_ROOT = old_registry, old_state_root

        self.assertEqual(code, 0)
        self.assertFalse(output)
        self.assertEqual(post_code, 0)
        self.assertIn("post_receipt_action_started", post_output)
        self.assertIn("control_event_guard.py", post_output)


    def test_outbound_pre_tool_without_tool_use_id_fails_closed_without_a_lease(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {"controller-1": {"desktop_codex": ["desktop-current"]}},
                "__controller_targets__": {"controller-1": {"desktop_codex": {
                    "status": "active", "session_id": "desktop-current", "generation": 4,
                }}},
            }), encoding="utf-8")
            old_registry, old_state_root = lifecycle_hook.REGISTRY_PATH, lifecycle_hook.STATE_ROOT
            lifecycle_hook.REGISTRY_PATH, lifecycle_hook.STATE_ROOT = registry, root / "state"
            try:
                with patch.object(lifecycle_hook, "registered_controller_id", return_value="controller-1"), patch.object(lifecycle_hook, "registered_root", return_value=repo), patch.object(lifecycle_hook, "project_snapshot", return_value=self.snapshot(repo)), patch.object(lifecycle_hook, "controller_event_is_managed", return_value=True):
                    code, output = self.invoke_hook({
                        "hook_event_name": "PreToolUse", "session_id": "desktop-current", "turn_id": "turn-1",
                        "tool_name": "mcp__codex_app__navigate_to_codex_page",
                        "tool_input": {"threadId": "desktop-current"},
                    })
                    self.assertFalse(controller_target_guard.has_active_outbound_lease(repo=repo, host="desktop_codex", registry_path=registry))
            finally:
                lifecycle_hook.REGISTRY_PATH, lifecycle_hook.STATE_ROOT = old_registry, old_state_root

        self.assertEqual(code, 0)
        denial = json.loads(output)
        self.assertEqual(denial["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("tool_use_id", denial["hookSpecificOutput"]["permissionDecisionReason"])

    def test_outbound_pre_tool_rechecks_its_source_before_leasing_the_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {"controller-1": {
                    "desktop_codex": ["desktop-old", "desktop-current"]
                }},
                "__controller_targets__": {"controller-1": {"desktop_codex": {
                    "status": "active", "session_id": "desktop-current", "generation": 4,
                }}},
            }), encoding="utf-8")
            old_registry, old_state_root = lifecycle_hook.REGISTRY_PATH, lifecycle_hook.STATE_ROOT
            lifecycle_hook.REGISTRY_PATH, lifecycle_hook.STATE_ROOT = registry, root / "state"
            try:
                with patch.object(lifecycle_hook, "registered_controller_id", return_value="controller-1"), patch.object(
                    lifecycle_hook, "registered_root", return_value=repo
                ), patch.object(lifecycle_hook, "project_snapshot", return_value=self.snapshot(repo)), patch.object(
                    lifecycle_hook, "controller_event_is_managed", return_value=True
                ):
                    code, output = self.invoke_hook({
                        "hook_event_name": "PreToolUse", "session_id": "desktop-old", "turn_id": "turn-1",
                        "tool_name": "mcp__codex_app__send_message_to_thread", "tool_use_id": "message-1",
                        "tool_input": {"threadId": "desktop-current", "prompt": "continue"},
                    })
                    self.assertFalse(controller_target_guard.has_active_outbound_lease(
                        repo=repo, host="desktop_codex", registry_path=registry
                    ))
            finally:
                lifecycle_hook.REGISTRY_PATH, lifecycle_hook.STATE_ROOT = old_registry, old_state_root

        self.assertEqual(code, 0)
        denial = json.loads(output)
        self.assertEqual(denial["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("source session", denial["hookSpecificOutput"]["permissionDecisionReason"])

    def test_hook_source_rejects_malformed_current_target_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp) / "controllers.json"
            registry.write_text(json.dumps({
                "controller-old": "/tmp/project",
                "__controller_targets__": {"controller-old": {"desktop_codex": []}},
            }), encoding="utf-8")
            old_registry = lifecycle_hook.REGISTRY_PATH
            lifecycle_hook.REGISTRY_PATH = registry
            try:
                with self.assertRaisesRegex(ValueError, "target"):
                    lifecycle_hook.registered_controller_id("controller-old")
            finally:
                lifecycle_hook.REGISTRY_PATH = old_registry

    def test_hook_source_rejects_malformed_session_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp) / "controllers.json"
            registry.write_text(json.dumps({
                "controller-old": "/tmp/project",
                "__controller_sessions__": {"controller-old": {"desktop_codex": {}}},
            }), encoding="utf-8")
            old_registry = lifecycle_hook.REGISTRY_PATH
            lifecycle_hook.REGISTRY_PATH = registry
            try:
                with self.assertRaisesRegex(ValueError, "session"):
                    lifecycle_hook.registered_controller_id("controller-old")
            finally:
                lifecycle_hook.REGISTRY_PATH = old_registry

    def test_cas_generation_rejects_boolean_metadata(self) -> None:
        with self.assertRaisesRegex(ValueError, "generation"):
            lifecycle_hook._desktop_target_generation({"generation": True})

    def test_ordinary_managed_hook_holds_source_fence_through_state_persist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_targets__": {"controller-1": {"desktop_codex": {
                    "status": "active", "session_id": "controller-1", "generation": 1,
                }}},
            }), encoding="utf-8")
            entered, release, replaced = threading.Event(), threading.Event(), threading.Event()
            old_registry = lifecycle_hook.REGISTRY_PATH
            lifecycle_hook.REGISTRY_PATH = registry
            try:
                def persist(_path, _event, _snapshot):
                    self.assertEqual(_event["controller_target_generation"], 1)
                    entered.set()
                    self.assertTrue(release.wait(1))
                    return {}, {}

                with patch.object(lifecycle_hook, "project_snapshot", return_value=self.snapshot(repo)), patch.object(
                    lifecycle_hook, "controller_event_is_managed", return_value=True
                ), patch.object(lifecycle_hook, "persist_event_state", side_effect=persist):
                    hook_thread = threading.Thread(target=self.invoke_hook, args=({
                        "hook_event_name": "SessionStart", "session_id": "controller-1", "cwd": str(repo),
                    },))
                    hook_thread.start()
                    self.assertTrue(entered.wait(1))
                    def replace():
                        lifecycle_hook.replace_desktop_session(
                            controller_id="controller-1", desktop_session_id="desktop-next",
                            repo=repo, expected_generation=1,
                        )
                        replaced.set()
                    replace_thread = threading.Thread(target=replace)
                    replace_thread.start()
                    time.sleep(0.05)
                    self.assertFalse(replaced.is_set())
                    release.set()
                    hook_thread.join(1)
                    replace_thread.join(1)
            finally:
                lifecycle_hook.REGISTRY_PATH = old_registry

        self.assertTrue(replaced.is_set())

    def test_shared_fence_skips_an_event_after_its_controller_repo_is_rebound(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            replacement_root = root / "replacement-root"
            replacement_root.mkdir()
            replacement = self.make_repo(replacement_root)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_targets__": {"controller-1": {"desktop_codex": {
                    "status": "active", "session_id": "controller-1", "generation": 1,
                }}},
            }), encoding="utf-8")
            old_registry = lifecycle_hook.REGISTRY_PATH
            lifecycle_hook.REGISTRY_PATH = registry
            try:
                def mutate_registered_root(*_args):
                    saved = lifecycle_hook.load_json(registry)
                    saved["controller-1"] = str(replacement.resolve())
                    lifecycle_hook.write_json(registry, saved)
                    return True

                with patch.object(lifecycle_hook, "project_snapshot", return_value=self.snapshot(repo)), patch.object(
                    lifecycle_hook, "controller_event_is_managed", side_effect=mutate_registered_root
                ), patch.object(lifecycle_hook, "persist_event_state", side_effect=AssertionError):
                    code, output = self.invoke_hook({
                        "hook_event_name": "SessionStart", "session_id": "controller-1", "cwd": str(repo),
                    })
            finally:
                lifecycle_hook.REGISTRY_PATH = old_registry

        self.assertEqual(code, 0)
        self.assertEqual(output, "")

    def test_register_controller_refuses_rebinding_a_session_to_a_different_repository(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            replacement_root = root / "replacement-root"
            replacement_root.mkdir()
            replacement = self.make_repo(replacement_root)
            registry = root / "controllers.json"
            old_registry = lifecycle_hook.REGISTRY_PATH
            lifecycle_hook.REGISTRY_PATH = registry
            try:
                lifecycle_hook.register_controller("controller-1", repo)
                with self.assertRaisesRegex(ValueError, "different repository"):
                    lifecycle_hook.register_controller("controller-1", replacement)
                saved = lifecycle_hook.load_json(registry)
            finally:
                lifecycle_hook.REGISTRY_PATH = old_registry

        self.assertEqual(saved["controller-1"], str(repo.resolve()))

    def test_register_controller_allows_a_same_repository_surface_update(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "tests@example.com"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Tests"], check=True)
            (repo / "README.md").write_text("fixture\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "fixture"], check=True)
            surface = root / "controller-surface"
            subprocess.run(["git", "-C", str(repo), "worktree", "add", "-qb", "controller", str(surface)], check=True)
            registry = root / "controllers.json"
            old_registry = lifecycle_hook.REGISTRY_PATH
            lifecycle_hook.REGISTRY_PATH = registry
            try:
                lifecycle_hook.register_controller("controller-1", repo)
                lifecycle_hook.register_controller("controller-1", surface)
                saved = lifecycle_hook.load_json(registry)
            finally:
                lifecycle_hook.REGISTRY_PATH = old_registry

        self.assertEqual(saved["controller-1"], str(repo.resolve()))
        self.assertEqual(saved["__controller_surfaces__"]["controller-1"], str(surface.resolve()))

    def test_matching_post_tool_keeps_its_lease_until_lifecycle_state_persists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {"controller-1": {"desktop_codex": ["desktop-current"]}},
                "__controller_targets__": {"controller-1": {"desktop_codex": {
                    "status": "active", "session_id": "desktop-current", "generation": 4,
                }}},
            }), encoding="utf-8")
            old_registry, old_state_root = lifecycle_hook.REGISTRY_PATH, lifecycle_hook.STATE_ROOT
            lifecycle_hook.REGISTRY_PATH, lifecycle_hook.STATE_ROOT = registry, root / "state"
            try:
                event = {
                    "session_id": "desktop-current", "turn_id": "turn-1",
                    "tool_name": "mcp__codex_app__send_message_to_thread", "tool_use_id": "message-1",
                    "tool_input": {"threadId": "desktop-current", "prompt": "continue"},
                }
                with patch.object(lifecycle_hook, "registered_controller_id", return_value="controller-1"), patch.object(
                    lifecycle_hook, "registered_root", return_value=repo
                ), patch.object(lifecycle_hook, "project_snapshot", return_value=self.snapshot(repo)), patch.object(
                    lifecycle_hook, "controller_event_is_managed", return_value=True
                ):
                    self.invoke_hook({"hook_event_name": "PreToolUse", **event})

                    def persist(_path, _event, _snapshot):
                        self.assertTrue(controller_target_guard.has_active_outbound_lease(
                            repo=repo, host="desktop_codex", registry_path=registry
                        ))
                        return {}, {}

                    with patch.object(lifecycle_hook, "persist_event_state", side_effect=persist):
                        code, output = self.invoke_hook({"hook_event_name": "PostToolUse", **event})
                    self.assertFalse(controller_target_guard.has_active_outbound_lease(
                        repo=repo, host="desktop_codex", registry_path=registry
                    ))
            finally:
                lifecycle_hook.REGISTRY_PATH, lifecycle_hook.STATE_ROOT = old_registry, old_state_root

        self.assertEqual(code, 0)
        self.assertEqual(output, "")

    def test_unmanaged_hook_silently_skips_without_entering_the_state_fence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({"controller-1": "/tmp/project"}), encoding="utf-8")
            old_registry = lifecycle_hook.REGISTRY_PATH
            lifecycle_hook.REGISTRY_PATH = registry
            try:
                with patch.object(lifecycle_hook, "persist_event_state", side_effect=AssertionError):
                    code, output = self.invoke_hook({
                        "hook_event_name": "SessionStart", "session_id": "unmanaged-session", "cwd": str(root),
                    })
            finally:
                lifecycle_hook.REGISTRY_PATH = old_registry

        self.assertEqual(code, 0)
        self.assertEqual(output, "")


class DesktopLifecycleCanaryTests(unittest.TestCase):
    def test_live_observations_are_required_before_canary_passes(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            skill_root = root / "adaptive-delivery"
            (skill_root / "scripts").mkdir(parents=True)
            lifecycle = skill_root / "scripts" / "lifecycle_hook.py"
            lifecycle.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            (skill_root / "scripts" / "controller_target_guard.py").write_text(
                "#!/usr/bin/env python3\n", encoding="utf-8"
            )
            hooks = root / "hooks.json"
            hooks.write_text('{"hooks": {"PreToolUse": []}}\n', encoding="utf-8")
            canary = root / "desktop-canary.json"

            lifecycle_hook.arm_desktop_canary(
                "c1",
                canary_path=canary,
                hooks_path=hooks,
                skill_root=skill_root,
            )
            sequence = [
                (
                    {"hook_event_name": "SessionStart", "session_id": "c1", "turn_id": "t1"},
                    {},
                    {"active_turn_id": "t1", "must_yield": False},
                ),
                (
                    {"hook_event_name": "PreToolUse", "session_id": "c1", "turn_id": "t1"},
                    {},
                    {"active_turn_id": "t1", "must_yield": False},
                ),
                (
                    {"hook_event_name": "PostToolUse", "session_id": "c1", "turn_id": "t1"},
                    {},
                    {"active_turn_id": "t1", "must_yield": False},
                ),
                (
                    {"hook_event_name": "PostToolUse", "session_id": "c1", "turn_id": "t1"},
                    {},
                    {"active_turn_id": "t1", "must_yield": True, "receipt_turn_id": "t1"},
                ),
                (
                    {"hook_event_name": "PreToolUse", "session_id": "c1", "turn_id": "t1"},
                    {
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "permissionDecision": "deny",
                        }
                    },
                    {"active_turn_id": "t1", "must_yield": True, "receipt_turn_id": "t1"},
                ),
                (
                    {"hook_event_name": "Stop", "session_id": "c1", "turn_id": "t1"},
                    {},
                    {"active_turn_id": "t1", "must_yield": True, "receipt_turn_id": "t1"},
                ),
                (
                    {"hook_event_name": "PreToolUse", "session_id": "c1", "turn_id": "t2"},
                    {},
                    {"active_turn_id": "t2", "must_yield": False},
                ),
            ]
            for event, output, state in sequence:
                receipt = lifecycle_hook.record_desktop_canary_observation(
                    event,
                    output,
                    state,
                    canary_path=canary,
                    hooks_path=hooks,
                    skill_root=skill_root,
                )

            self.assertEqual(receipt["status"], "pending")
            self.assertNotIn("subagent_stop_observed", receipt["observations"])

            receipt = lifecycle_hook.record_desktop_canary_observation(
                {
                    "hook_event_name": "SubagentStop",
                    "session_id": "c1",
                    "turn_id": "t2",
                },
                {},
                {"active_turn_id": "t2", "must_yield": False},
                canary_path=canary,
                hooks_path=hooks,
                skill_root=skill_root,
            )

            self.assertEqual(receipt["status"], "passed")
            self.assertEqual(receipt["controller_session_id"], "c1")
            self.assertEqual(receipt["sequence_index"], 8)
            self.assertEqual(receipt["skill_root"], str(skill_root.resolve()))
            self.assertEqual(len(receipt["hooks_sha256"]), 64)
            self.assertEqual(len(receipt["lifecycle_sha256"]), 64)
            self.assertEqual(len(receipt["controller_target_guard_sha256"]), 64)

    def test_canary_does_not_combine_events_from_other_sessions_or_wrong_order(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            skill_root = root / "adaptive-delivery"
            (skill_root / "scripts").mkdir(parents=True)
            (skill_root / "scripts" / "lifecycle_hook.py").write_text(
                "#!/usr/bin/env python3\n", encoding="utf-8"
            )
            (skill_root / "scripts" / "controller_target_guard.py").write_text(
                "#!/usr/bin/env python3\n", encoding="utf-8"
            )
            hooks = root / "hooks.json"
            hooks.write_text('{"hooks": {"PreToolUse": []}}\n', encoding="utf-8")
            canary = root / "desktop-canary.json"
            lifecycle_hook.arm_desktop_canary(
                "c1", canary_path=canary, hooks_path=hooks, skill_root=skill_root
            )

            wrong_session = lifecycle_hook.record_desktop_canary_observation(
                {"hook_event_name": "SessionStart", "session_id": "c2", "turn_id": "t1"},
                {},
                {"active_turn_id": "t1"},
                canary_path=canary,
                hooks_path=hooks,
                skill_root=skill_root,
            )
            wrong_order = lifecycle_hook.record_desktop_canary_observation(
                {"hook_event_name": "SubagentStop", "session_id": "c1", "turn_id": "t1"},
                {},
                {"active_turn_id": "t1"},
                canary_path=canary,
                hooks_path=hooks,
                skill_root=skill_root,
            )

            self.assertEqual(wrong_session["sequence_index"], 0)
            self.assertEqual(wrong_order["sequence_index"], 0)
            self.assertEqual(wrong_order["observations"], [])


class MachineTraceReceiptTests(unittest.TestCase):
    def receipt(self) -> dict[str, object]:
        return {
            "event_contract": {
                "event_id": "event-1",
                "event_type": "dispatch",
                "primary_task": "F1",
                "candidate_revision": "rev-1",
                "allowed_actions": ["ledger_sync"],
                "allowed_files": [],
                "terminal_receipt": "closed",
            },
            "event_actions": [
                {
                    "action": "ledger_sync",
                    "primary_task": "F1",
                    "candidate_revision": "rev-1",
                    "files": [],
                    "required_to_close_current_state": True,
                }
            ],
            "terminal_receipt_issued": True,
            "ledger_sha256": "ledger-1",
            "available_slots": 0,
            "ready_packages": [],
            "new_assignments": [],
        }

    def test_guard_requires_the_exact_machine_trace_for_registered_controller(self) -> None:
        expected = lifecycle_hook.machine_trace_projection(
            {
                "active_turn_id": "turn-1",
                "tool_trace": [
                    {
                        "turn_id": "turn-1",
                        "tool_use_id": "patch-1",
                        "tool_name": "apply_patch",
                        "input_sha256": "a" * 64,
                        "response_status": 0,
                    },
                    {
                        "turn_id": "turn-1",
                        "tool_use_id": "agent-1",
                        "tool_name": "spawn_agent",
                        "input_sha256": "b" * 64,
                        "response_status": 0,
                    },
                ],
            }
        )
        receipt = self.receipt()
        receipt["machine_trace"] = expected

        self.assertEqual(
            control_event_guard.validate_snapshot(
                receipt, expected_machine_trace=expected
            ),
            [],
        )

        receipt["machine_trace"] = {
            **expected,
            "tool_use_ids": ["patch-1"],
        }
        errors = control_event_guard.validate_snapshot(
            receipt, expected_machine_trace=expected
        )
        self.assertTrue(any("machine_trace" in error for error in errors))

    def test_guard_loads_machine_trace_from_the_registered_controller_state(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            state_root = Path(d)
            lifecycle_hook.write_json(
                state_root / lifecycle_hook.state_path("controller-1").name,
                {
                    "active_turn_id": "turn-9",
                    "tool_trace": [
                        {
                            "turn_id": "turn-9",
                            "tool_use_id": "tool-9",
                            "tool_name": "Bash",
                            "input_sha256": "c" * 64,
                            "response_status": 0,
                        }
                    ],
                },
            )

            projection = control_event_guard.observed_machine_trace(
                "controller-1", state_root=state_root
            )

        self.assertEqual(projection["turn_id"], "turn-9")
        self.assertEqual(projection["tool_use_ids"], ["tool-9"])

    def test_registered_controller_cannot_omit_machine_trace_flag(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            registry = root / "controllers.json"
            registry.write_text(
                '{"controller-1": "' + str(root) + '"}\n', encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "--controller-session"):
                control_event_guard.resolve_controller_trace_session(
                    root, None, registry_path=registry
                )
            self.assertEqual(
                control_event_guard.resolve_controller_trace_session(
                    root, "controller-1", registry_path=registry
                ),
                "controller-1",
            )

    def test_controller_can_print_the_exact_machine_trace_for_its_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            previous = lifecycle_hook.STATE_ROOT
            lifecycle_hook.STATE_ROOT = Path(d)
            try:
                lifecycle_hook.write_json(
                    lifecycle_hook.state_path("controller-1"),
                    {
                        "active_turn_id": "turn-print",
                        "tool_trace": [
                            {
                                "turn_id": "turn-print",
                                "tool_use_id": "tool-print",
                                "tool_name": "Bash",
                                "input_sha256": "d" * 64,
                                "response_status": 0,
                            }
                        ],
                    },
                )
                output = StringIO()
                with redirect_stdout(output):
                    code = lifecycle_hook.main(
                        ["--print-machine-trace", "controller-1"]
                    )
            finally:
                lifecycle_hook.STATE_ROOT = previous

        self.assertEqual(code, 0)
        self.assertIn('"turn_id": "turn-print"', output.getvalue())
        self.assertIn('"tool-print"', output.getvalue())


if __name__ == "__main__":
    unittest.main()
