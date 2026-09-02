from __future__ import annotations

import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

import lifecycle_hook  # noqa: E402
import control_event_guard  # noqa: E402


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
                        "python3 scripts/control_event_guard.py receipt.json "
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

    def test_successful_receipt_denies_another_tool_in_the_same_turn(self) -> None:
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
                "tool_use_id": "late-write",
                "tool_input": {"command": "*** Begin Patch"},
            },
            snapshot=self.snapshot(),
            prior_state=state,
        )

        decision = output["hookSpecificOutput"]
        self.assertEqual(decision["hookEventName"], "PreToolUse")
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertIn("同一回合", decision["permissionDecisionReason"])
        self.assertTrue(next_state["must_yield"])

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
                        "python3 scripts/control_event_guard.py receipt.json "
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
                    "python3 scripts/control_event_guard.py receipt.json "
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


class DesktopLifecycleCanaryTests(unittest.TestCase):
    def test_live_observations_are_required_before_canary_passes(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            skill_root = root / "adaptive-delivery"
            (skill_root / "scripts").mkdir(parents=True)
            lifecycle = skill_root / "scripts" / "lifecycle_hook.py"
            lifecycle.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
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

    def test_canary_does_not_combine_events_from_other_sessions_or_wrong_order(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            skill_root = root / "adaptive-delivery"
            (skill_root / "scripts").mkdir(parents=True)
            (skill_root / "scripts" / "lifecycle_hook.py").write_text(
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
