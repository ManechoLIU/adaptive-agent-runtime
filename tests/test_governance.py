from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(name, SKILL_ROOT / relative_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {relative_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


init_project = load_module("init_project", "scripts/init_project.py")
lint_governance = load_module("lint_governance", "scripts/lint_governance.py")
preblock_guard = load_module("preblock_guard", "scripts/preblock_guard.py")
control_event_guard = load_module(
    "control_event_guard", "scripts/control_event_guard.py"
)
event_scope_guard = load_module("event_scope_guard", "scripts/event_scope_guard.py")
assignment_lease_guard = load_module(
    "assignment_lease_guard", "scripts/assignment_lease_guard.py"
)
ledger_consistency_guard = load_module(
    "ledger_consistency_guard", "scripts/ledger_consistency_guard.py"
)
lifecycle_hook = load_module("lifecycle_hook", "scripts/lifecycle_hook.py")


class GovernanceTests(unittest.TestCase):
    def complete_event_receipt(self) -> dict[str, object]:
        return {
            "event_contract": {
                "event_id": "control-event-1",
                "event_type": "dispatch",
                "primary_task": "CONTROL-WAVE-1",
                "candidate_revision": "ledger-abc123",
                "allowed_actions": ["ledger_sync"],
                "allowed_files": [],
                "terminal_receipt": "control event synchronized",
            },
            "event_actions": [
                {
                    "action": "ledger_sync",
                    "primary_task": "CONTROL-WAVE-1",
                    "candidate_revision": "ledger-abc123",
                    "files": [],
                    "required_to_close_current_state": True,
                }
            ],
            "terminal_receipt_issued": True,
        }

    def test_lifecycle_hook_surfaces_ready_work_after_tool_use(self) -> None:
        snapshot = {
            "head": "abc123",
            "ledger_sha256": "ledger-1",
            "worktree_status_sha256": "status-1",
            "ready_ids": ["WEB-READY", "MINI-READY"],
        }

        output, next_state = lifecycle_hook.evaluate_event(
            {
                "hook_event_name": "PostToolUse",
                "session_id": "session-1",
                "turn_id": "turn-1",
                "tool_name": "Bash",
                "tool_input": {"command": "git status --short"},
                "tool_response": {"output": ""},
            },
            snapshot=snapshot,
            prior_state=None,
        )

        self.assertEqual(
            output["hookSpecificOutput"]["hookEventName"], "PostToolUse"
        )
        self.assertIn("WEB-READY", output["hookSpecificOutput"]["additionalContext"])
        self.assertIn("MINI-READY", output["hookSpecificOutput"]["additionalContext"])
        self.assertTrue(next_state["pending_control_event"])

    def test_project_snapshot_joins_runtime_liveness_for_active_ledger_task(self) -> None:
        import json
        import subprocess
        from datetime import datetime, timedelta, timezone
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            subprocess.run(["git", "-C", str(root), "init", "-b", "main"], check=True, capture_output=True)
            (root/".git"/"adaptive-delivery").mkdir(parents=True)
            (root/"TASK_LEDGER.md").write_text("| ID | 状态 | 负责人 | 下一步 |\n|---|---|---|---|\n| `T1` | `ACTIVE` | grok | y |\n",encoding="utf-8")
            now=datetime.now(timezone.utc)
            lease={"assignment_id":"a1","task_id":"T1","agent_id":"grok","provider":"grok","session_id":"s1","worktree":"/tmp/wt","lease_expires_at":(now-timedelta(minutes=1)).isoformat(),"progress_deadline_at":(now+timedelta(minutes=10)).isoformat(),"terminal_state":None}
            (root/".git"/"adaptive-delivery"/"runtime-assignments.json").write_text(json.dumps({"schema_version":1,"leases":{"a1":lease}}))
            def fake_git(_root,*args):
                if args==("rev-parse","--show-toplevel"): return str(root)
                if args==("branch","--show-current"): return "main"
                if args==("status","--porcelain=v1","--untracked-files=no"): return ""
                if args==("rev-parse","HEAD"): return "abc"
                raise AssertionError(args)
            with patch.object(lifecycle_hook,"run_git",side_effect=fake_git), patch("control_event_guard.unmerged_worktree_candidates",return_value={}):
                snap=lifecycle_hook.project_snapshot(root)
            self.assertEqual(snap["assignment_liveness"]["T1"]["state"],"unhealthy")
            self.assertEqual(snap["assignment_liveness"]["T1"]["reason"],"lease_expired")

    def test_project_snapshot_exposes_five_state_task_projection(self) -> None:
        import subprocess
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            subprocess.run(["git", "-C", str(root), "init", "-b", "main"], check=True, capture_output=True)
            (root / ".git" / "adaptive-delivery").mkdir(parents=True)
            (root / "TASK_LEDGER.md").write_text(
                """# Ledger

- 当前 Goal：`T3` active work
- 下一可见检查点：`T3` checkpoint
- 当前阻塞：none
- 规则版本：test

| ID | 状态 | 负责人 | 下一步 |
|---|---|---|---|
| `T1` | `PENDING` | 待分配 | wait |
| `T2` | `READY` | 待分配 | dispatch |
| `T3` | `ACTIVE` | Agent A | execute |
| `T4` | `RECOVERING` | Agent B Assignment T4-RECOVERY-01 delivered ACK | 恢复动作：继续 checkpoint 测试 PASS |
| `T5` | `VERIFY` | reviewer | review |
| `T6` | `BLOCKED` | controller | external |
| `T7` | `DONE` | controller | none |
| `T8` | `SUPERSEDED` | controller | none |
""",
                encoding="utf-8",
            )
            def fake_git(_root, *args):
                if args == ("rev-parse", "--show-toplevel"): return str(root)
                if args == ("branch", "--show-current"): return "main"
                if args == ("status", "--porcelain=v1", "--untracked-files=no"): return ""
                if args == ("rev-parse", "HEAD"): return "abc"
                raise AssertionError(args)
            with patch.object(lifecycle_hook, "run_git", side_effect=fake_git), patch(
                "control_event_guard.unmerged_worktree_candidates", return_value={}
            ):
                snap = lifecycle_hook.project_snapshot(root)
            projection = snap["task_projection"]
            self.assertEqual(projection["T1"]["main_state"], "READY")
            self.assertFalse(projection["T1"]["dispatchable"])
            self.assertEqual(projection["T2"]["main_state"], "READY")
            self.assertTrue(projection["T2"]["dispatchable"])
            self.assertEqual((projection["T3"]["main_state"], projection["T3"]["health"]), ("ACTIVE", "recovering"))
            self.assertEqual((projection["T4"]["main_state"], projection["T4"]["health"]), ("ACTIVE", "recovering"))
            self.assertEqual(projection["T5"]["main_state"], "VERIFY")
            self.assertEqual(projection["T6"]["main_state"], "BLOCKED")
            self.assertEqual((projection["T7"]["main_state"], projection["T7"]["closure_reason"]), ("CLOSED", "done"))
            self.assertEqual((projection["T8"]["main_state"], projection["T8"]["closure_reason"]), ("CLOSED", "superseded"))

    def test_lifecycle_rule_update_is_persistent_and_exact_until_ack_and_ledger_sync(self) -> None:
        base = {
            "head": "abc", "ledger_sha256": "l1", "worktree_status_sha256": "s1",
            "ready_ids": [], "candidate_revisions": [], "ledger_errors": [], "assignment_liveness": {},
        }
        pending = {**base, "rule_handshake": {
            "state": "pending_ack", "blocking": True, "installed_revision": "rev-new",
            "summary": "canonical runtime", "impact": "live_assignments",
            "stop_condition": "ACK and ledger sync", "changed_files": ["scripts/assignment_runtime.py"],
        }}
        first, state = lifecycle_hook.evaluate_event(
            {"hook_event_name": "SessionStart", "session_id": "controller-1"}, snapshot=pending, prior_state=None
        )
        self.assertIn("rule_update_pending:rev-new", state["triggers"])
        context = first["hookSpecificOutput"]["additionalContext"]
        self.assertIn("rev-new", context)
        self.assertIn("canonical runtime", context)
        self.assertIn("rule_handshake.py ack", context)

        receipt_event = {
            "hook_event_name": "PostToolUse", "session_id": "controller-1",
            "tool_input": {"command": "python3 scripts/control_event_guard.py snapshot --ledger TASK_LEDGER.md"},
            "tool_response": {"exit_code": 0, "output": "control-event: allowed"},
        }
        _, still_pending = lifecycle_hook.evaluate_event(receipt_event, snapshot=pending, prior_state=state)
        self.assertTrue(still_pending["pending_control_event"])
        self.assertIn("rule_update_pending:rev-new", still_pending["triggers"])

        stale = {**base, "ledger_sha256": "l2", "rule_handshake": {
            **pending["rule_handshake"], "state": "ledger_stale", "loaded_revision": "rev-new",
        }}
        _, stale_state = lifecycle_hook.evaluate_event(
            {"hook_event_name": "PostToolUse", "session_id": "controller-1", "tool_input": {}, "tool_response": {}},
            snapshot=stale, prior_state=still_pending,
        )
        self.assertNotIn("rule_update_pending:rev-new", stale_state["triggers"])
        self.assertIn("rule_ledger_stale:rev-new", stale_state["triggers"])

        current = {**base, "ledger_sha256": "l3", "rule_handshake": {
            **pending["rule_handshake"], "state": "current", "blocking": False, "loaded_revision": "rev-new",
        }}
        _, current_state = lifecycle_hook.evaluate_event(
            {"hook_event_name": "PostToolUse", "session_id": "controller-1", "tool_input": {}, "tool_response": {}},
            snapshot=current, prior_state=stale_state,
        )
        self.assertFalse(any(t.startswith("rule_update_pending:") or t.startswith("rule_ledger_stale:") for t in current_state["triggers"]))

    def test_lifecycle_rule_install_integrity_error_is_blocking(self) -> None:
        snapshot = {
            "head": "abc", "ledger_sha256": "l", "worktree_status_sha256": "s",
            "ready_ids": [], "candidate_revisions": [], "ledger_errors": [], "assignment_liveness": {},
            "rule_handshake": {"state": "integrity_error", "blocking": True, "installed_revision": "rev-bad", "errors": ["hash mismatch"]},
        }
        triggers = lifecycle_hook.lifecycle_triggers(snapshot, None)
        self.assertIn("rule_install_integrity_error:rev-bad", triggers)

    def test_lifecycle_hook_surfaces_unhealthy_active_runtime_without_git_change(self) -> None:
        snapshot = {
            "head": "abc123", "ledger_sha256": "ledger-1", "worktree_status_sha256": "status-1",
            "ready_ids": [], "candidate_revisions": [], "ledger_errors": [],
            "assignment_liveness": {"F1": {"ledger_state": "ACTIVE", "state": "unhealthy", "reason": "lease_expired"}},
            "stale_active_ids": ["F1"], "progress_stale_ids": [], "terminal_active_ids": [],
        }
        prior={"snapshot":dict(snapshot),"pending_control_event":False,"triggers":[],"stop_continuations":0}
        output, state=lifecycle_hook.evaluate_event({"hook_event_name":"PostToolUse","session_id":"s","tool_input":{},"tool_response":{}},snapshot=snapshot,prior_state=prior)
        self.assertTrue(state["pending_control_event"]); self.assertIn("active_lease_expired:F1", state["triggers"]); self.assertIn("active_lease_expired:F1", str(output))

    def test_control_event_guard_blocks_canonical_pending_rule_handshake(self) -> None:
        from unittest.mock import patch
        with patch("scripts.rule_handshake.evaluate_rule_handshake", return_value={
            "state": "pending_ack", "blocking": True, "installed_revision": "rev-new"
        }):
            errors = control_event_guard.canonical_rule_handshake_errors(Path("."), Path("TASK_LEDGER.md"))
        self.assertIn("rule handshake pending_ack for installed revision rev-new", errors)

    def test_control_event_guard_blocks_stale_active_runtime(self) -> None:
        snapshot=self.complete_event_receipt(); snapshot.update({"ledger_sha256":"x","available_slots":0,"ready_packages":[],"assignment_liveness":{"F1":{"ledger_state":"ACTIVE","state":"unhealthy","reason":"lease_expired"}}})
        errors=control_event_guard.validate_snapshot(snapshot)
        self.assertIn("ACTIVE runtime unhealthy: F1 (lease_expired)", errors)

    def test_control_event_guard_requires_every_work_in_flight_runtime(self) -> None:
        snapshot = self.complete_event_receipt()
        snapshot.update(
            {
                "ledger_sha256": "x",
                "available_slots": 0,
                "ready_packages": [],
                "assignment_liveness": {
                    "F1": {
                        "ledger_state": "ACTIVE",
                        "state": "healthy",
                        "reason": "lease_current",
                    }
                },
            }
        )

        errors = control_event_guard.validate_snapshot(
            snapshot,
            ledger_work_in_flight={"F1": "ACTIVE", "F2": "RECOVERING"},
        )

        self.assertIn("assignment_liveness omitted RECOVERING task: F2", errors)

    def test_control_event_guard_accepts_exact_work_in_flight_runtime(self) -> None:
        snapshot = self.complete_event_receipt()
        snapshot.update(
            {
                "ledger_sha256": "x",
                "available_slots": 0,
                "ready_packages": [],
                "assignment_liveness": {
                    "F1": {
                        "ledger_state": "ACTIVE",
                        "state": "healthy",
                        "reason": "lease_current",
                    },
                    "F2": {
                        "ledger_state": "RECOVERING",
                        "state": "healthy",
                        "reason": "lease_current",
                    },
                },
            }
        )

        self.assertEqual(
            control_event_guard.validate_snapshot(
                snapshot,
                ledger_work_in_flight={"F1": "ACTIVE", "F2": "RECOVERING"},
            ),
            [],
        )
    def test_runtime_reconciliation_keeps_checkpoint_governance_lightweight(self) -> None:
        long_task = (SKILL_ROOT / "references" / "long-task-governance.md").read_text(encoding="utf-8")
        delivery = (SKILL_ROOT / "references" / "agent-delivery-contract.md").read_text(encoding="utf-8")
        for phrase in (
            "普通短任务不强制 checkpoint",
            "不新增总控人工必填字段",
            "不新增第二套状态机",
            "最近已验收 checkpoint",
        ):
            self.assertIn(phrase, long_task)
        self.assertIn("checkpoint 只作为恢复锚点", delivery)

    def test_lifecycle_clears_stale_recovery_trigger_after_task_returns_ready(self) -> None:
        prior_snapshot = {
            "head": "abc", "ledger_sha256": "ledger-old", "worktree_status_sha256": "status",
            "ready_ids": [], "candidate_revisions": [], "ledger_errors": [],
            "assignment_liveness": {
                "F1": {"ledger_state": "RECOVERING", "state": "terminal", "reason": "terminal:failed"}
            },
        }
        prior_state = {
            "snapshot": prior_snapshot, "pending_control_event": True,
            "triggers": ["recovery_stalled:F1"], "stop_continuations": 0,
        }
        current_snapshot = {
            "head": "abc", "ledger_sha256": "ledger-new", "worktree_status_sha256": "status",
            "ready_ids": ["F1"], "candidate_revisions": [], "ledger_errors": [],
            "assignment_liveness": {},
        }
        _, state = lifecycle_hook.evaluate_event(
            {"hook_event_name": "PostToolUse", "session_id": "s", "tool_input": {}, "tool_response": {}},
            snapshot=current_snapshot, prior_state=prior_state,
        )
        self.assertNotIn("recovery_stalled:F1", state["triggers"])
        self.assertIn("READY:F1", state["triggers"])

    def test_lifecycle_current_snapshot_drops_ready_trigger_after_task_blocks(self) -> None:
        prior_snapshot = {
            "head": "abc", "ledger_sha256": "ledger-old", "worktree_status_sha256": "status",
            "ready_ids": ["F1"], "candidate_revisions": [], "ledger_errors": [], "assignment_liveness": {},
        }
        prior_state = {
            "snapshot": prior_snapshot, "pending_control_event": True,
            "triggers": ["READY:F1"], "stop_continuations": 0,
        }
        current_snapshot = {
            "head": "abc", "ledger_sha256": "ledger-new", "worktree_status_sha256": "status",
            "ready_ids": [], "candidate_revisions": [], "ledger_errors": [],
            "assignment_liveness": {"F1": {"ledger_state": "BLOCKED", "state": "terminal", "reason": "external blocker"}},
        }
        output, state = lifecycle_hook.evaluate_event(
            {"hook_event_name": "PostToolUse", "session_id": "s", "tool_input": {}, "tool_response": {}},
            snapshot=current_snapshot, prior_state=prior_state,
        )
        self.assertNotIn("READY:F1", state["triggers"])
        self.assertNotIn("READY 派发", str(output))

    def test_lifecycle_current_snapshot_drops_terminal_trigger_after_task_blocks(self) -> None:
        prior_snapshot = {
            "head": "abc", "ledger_sha256": "ledger-old", "worktree_status_sha256": "status",
            "ready_ids": [], "candidate_revisions": [], "ledger_errors": [],
            "assignment_liveness": {"F1": {"ledger_state": "ACTIVE", "state": "terminal", "reason": "terminal:failed"}},
        }
        prior_state = {
            "snapshot": prior_snapshot, "pending_control_event": True,
            "triggers": ["agent_session_terminal:F1"], "stop_continuations": 0,
        }
        current_snapshot = {
            "head": "abc", "ledger_sha256": "ledger-new", "worktree_status_sha256": "status",
            "ready_ids": [], "candidate_revisions": [], "ledger_errors": [],
            "assignment_liveness": {"F1": {"ledger_state": "BLOCKED", "state": "terminal", "reason": "external blocker"}},
        }
        _, state = lifecycle_hook.evaluate_event(
            {"hook_event_name": "PostToolUse", "session_id": "s", "tool_input": {}, "tool_response": {}},
            snapshot=current_snapshot, prior_state=prior_state,
        )
        self.assertNotIn("agent_session_terminal:F1", state["triggers"])

    def test_lifecycle_keeps_terminal_trigger_while_task_is_still_active(self) -> None:
        snapshot = {
            "head": "abc", "ledger_sha256": "ledger", "worktree_status_sha256": "status",
            "ready_ids": [], "candidate_revisions": [], "ledger_errors": [],
            "assignment_liveness": {
                "F1": {"ledger_state": "ACTIVE", "state": "terminal", "reason": "terminal:failed"}
            },
        }
        triggers = lifecycle_hook.lifecycle_triggers(snapshot, None)
        self.assertIn("agent_session_terminal:F1", triggers)

    def test_lifecycle_surfaces_recovery_budget_exhaustion_as_control_trigger(self) -> None:
        snapshot = {
            "head": "abc", "ledger_sha256": "ledger", "worktree_status_sha256": "status",
            "ready_ids": [], "candidate_revisions": [], "ledger_errors": [],
            "assignment_liveness": {
                "F1": {"ledger_state": "ACTIVE", "state": "budget_exhausted", "reason": "recovery_budget_exhausted"}
            },
        }
        triggers = lifecycle_hook.lifecycle_triggers(snapshot, None)
        self.assertIn("recovery_budget_exhausted:F1", triggers)

    def test_lifecycle_hook_surfaces_invalid_ledger_at_session_start(self) -> None:
        output, next_state = lifecycle_hook.evaluate_event(
            {
                "hook_event_name": "SessionStart",
                "session_id": "session-1",
                "turn_id": "turn-1",
            },
            snapshot={
                "head": "abc123",
                "ledger_sha256": "ledger-1",
                "worktree_status_sha256": "status-1",
                "ready_ids": [],
                "candidate_revisions": [],
                "ledger_errors": [
                    "M2-F2 next action references undeclared task ID M2-F2-DEV-WEREAD-QA-01"
                ],
            },
            prior_state=None,
        )

        context = output["hookSpecificOutput"]["additionalContext"]
        self.assertIn("LEDGER_INVALID", context)
        self.assertTrue(next_state["pending_control_event"])

    def test_lifecycle_hook_surfaces_unmerged_candidate(self) -> None:
        snapshot = {
            "head": "abc123",
            "ledger_sha256": "ledger-1",
            "worktree_status_sha256": "status-1",
            "ready_ids": [],
            "candidate_revisions": ["candidate-123"],
        }

        output, next_state = lifecycle_hook.evaluate_event(
            {
                "hook_event_name": "PostToolUse",
                "session_id": "session-1",
                "turn_id": "turn-1",
                "tool_name": "Bash",
                "tool_input": {"command": "git status --short"},
                "tool_response": {"output": ""},
            },
            snapshot=snapshot,
            prior_state=None,
        )

        self.assertIn("candidate-123", output["hookSpecificOutput"]["additionalContext"])
        self.assertTrue(next_state["pending_control_event"])

    def test_lifecycle_hook_stop_continues_until_ready_is_dispatched(self) -> None:
        output, next_state = lifecycle_hook.evaluate_event(
            {
                "hook_event_name": "Stop",
                "session_id": "session-1",
                "turn_id": "turn-1",
                "stop_hook_active": False,
            },
            snapshot={
                "head": "abc123",
                "ledger_sha256": "ledger-1",
                "worktree_status_sha256": "status-1",
                "ready_ids": ["WEB-READY"],
            },
            prior_state={
                "pending_control_event": True,
                "triggers": ["READY:WEB-READY"],
            },
        )

        self.assertEqual(output["decision"], "block")
        self.assertIn("WEB-READY", output["reason"])
        self.assertTrue(next_state["pending_control_event"])

    def test_lifecycle_hook_repeated_stop_with_ready_stays_blocked_until_dispatch_or_blocked(self) -> None:
        snapshot = {
            "head": "abc123",
            "ledger_sha256": "ledger-1",
            "worktree_status_sha256": "status-1",
            "ready_ids": ["SERVER-GATE"],
            "candidate_revisions": [],
            "rule_handshake": {"state": "current", "blocking": False, "installed_revision": "rev-current"},
        }
        prior = {
            "pending_control_event": True,
            "triggers": ["READY:SERVER-GATE", "ledger_changed"],
            "stop_continuations": 1,
            "snapshot": snapshot,
        }

        output, next_state = lifecycle_hook.evaluate_event(
            {
                "hook_event_name": "Stop",
                "session_id": "session-1",
                "turn_id": "turn-2",
                "stop_hook_active": False,
            },
            snapshot=snapshot,
            prior_state=prior,
        )

        self.assertEqual(output["decision"], "block")
        self.assertIn("SERVER-GATE", output["reason"])
        self.assertIn("BLOCKED", output["reason"])
        self.assertTrue(next_state["pending_control_event"])

    def test_lifecycle_hook_second_stop_without_progress_fails_closed(self) -> None:
        snapshot = {
            "head": "abc123",
            "ledger_sha256": "ledger-1",
            "worktree_status_sha256": "status-1",
            "ready_ids": [],
            "candidate_revisions": ["candidate-123"],
        }
        first_output, first_state = lifecycle_hook.evaluate_event(
            {
                "hook_event_name": "Stop",
                "session_id": "session-1",
                "turn_id": "turn-1",
            },
            snapshot=snapshot,
            prior_state={
                "pending_control_event": True,
                "triggers": ["CANDIDATE:candidate-123"],
            },
        )
        self.assertEqual(first_output["decision"], "block")

        second_output, second_state = lifecycle_hook.evaluate_event(
            {
                "hook_event_name": "Stop",
                "session_id": "session-1",
                "turn_id": "turn-1",
            },
            snapshot=snapshot,
            prior_state=first_state,
        )

        self.assertFalse(second_output["continue"])
        self.assertIn("failed closed", second_output["stopReason"])
        self.assertEqual(second_state["stop_continuations"], 2)

    def test_lifecycle_hook_snapshot_progress_resets_stop_continuation(self) -> None:
        output, next_state = lifecycle_hook.evaluate_event(
            {
                "hook_event_name": "PostToolUse",
                "session_id": "session-1",
                "turn_id": "turn-1",
                "tool_name": "Bash",
                "tool_input": {"command": "git status --short"},
                "tool_response": {"output": ""},
            },
            snapshot={
                "head": "abc123",
                "ledger_sha256": "ledger-2",
                "worktree_status_sha256": "status-1",
                "ready_ids": ["WEB-READY"],
                "candidate_revisions": [],
            },
            prior_state={
                "pending_control_event": True,
                "triggers": ["READY:WEB-READY"],
                "stop_continuations": 1,
                "snapshot": {
                    "head": "abc123",
                    "ledger_sha256": "ledger-1",
                    "worktree_status_sha256": "status-1",
                    "ready_ids": ["WEB-READY"],
                    "candidate_revisions": [],
                },
            },
        )

        self.assertEqual(output["hookSpecificOutput"]["hookEventName"], "PostToolUse")
        self.assertEqual(next_state["stop_continuations"], 0)

    def test_lifecycle_hook_successful_guard_receipt_clears_pending_event(self) -> None:
        output, next_state = lifecycle_hook.evaluate_event(
            {
                "hook_event_name": "PostToolUse",
                "session_id": "session-1",
                "turn_id": "turn-1",
                "tool_name": "Bash",
                "tool_input": {
                    "command": "python3 scripts/control_event_guard.py receipt.json --ledger TASK_LEDGER.md"
                },
                "tool_response": {"output": "control-event: allowed", "exit_code": 0},
            },
            snapshot={
                "head": "abc123",
                "ledger_sha256": "ledger-2",
                "worktree_status_sha256": "status-2",
                "ready_ids": [],
            },
            prior_state={
                "pending_control_event": True,
                "triggers": ["ledger_changed"],
            },
        )

        self.assertEqual(output, {})
        self.assertFalse(next_state["pending_control_event"])
        self.assertEqual(next_state["triggers"], [])

    def test_lifecycle_hook_does_not_retrigger_unchanged_candidate_after_receipt(self) -> None:
        snapshot = {
            "head": "abc123",
            "ledger_sha256": "ledger-2",
            "worktree_status_sha256": "status-2",
            "ready_ids": [],
            "candidate_revisions": ["candidate-123"],
            "ledger_errors": [],
            "assignment_liveness": {},
        }
        receipt_output, receipt_state = lifecycle_hook.evaluate_event(
            {
                "hook_event_name": "PostToolUse",
                "session_id": "session-1",
                "turn_id": "turn-1",
                "tool_name": "Bash",
                "tool_input": {
                    "command": (
                        "python3 scripts/control_event_guard.py receipt.json "
                        "--ledger TASK_LEDGER.md --repo ."
                    )
                },
                "tool_response": {"output": "control-event: allowed", "exit_code": 0},
            },
            snapshot=snapshot,
            prior_state={
                "snapshot": dict(snapshot),
                "pending_control_event": True,
                "triggers": ["CANDIDATE:candidate-123"],
            },
        )
        self.assertEqual(receipt_output, {})
        self.assertFalse(receipt_state["pending_control_event"])

        next_output, next_state = lifecycle_hook.evaluate_event(
            {
                "hook_event_name": "PostToolUse",
                "session_id": "session-1",
                "turn_id": "turn-1",
                "tool_name": "Bash",
                "tool_input": {"command": "git status --short"},
                "tool_response": {"output": "", "exit_code": 0},
            },
            snapshot=snapshot,
            prior_state=receipt_state,
        )

        self.assertEqual(next_output, {})
        self.assertFalse(next_state["pending_control_event"])
        self.assertEqual(next_state["triggers"], [])

    def test_lifecycle_hook_ignores_projects_without_a_canonical_ledger(self) -> None:
        output, next_state = lifecycle_hook.evaluate_event(
            {
                "hook_event_name": "Stop",
                "session_id": "session-1",
                "turn_id": "turn-1",
            },
            snapshot=None,
            prior_state={"pending_control_event": True},
        )

        self.assertEqual(output, {})
        self.assertEqual(next_state, {})

    def test_event_scope_guard_allows_only_causally_required_same_candidate_action(self) -> None:
        contract = {
            "event_id": "event-1",
            "event_type": "candidate integration",
            "primary_task": "F1",
            "candidate_revision": "abc123",
            "allowed_actions": ["review", "integrate", "main_regression", "ledger_sync"],
            "allowed_files": ["app/a.ts", "TASK_LEDGER.md"],
            "terminal_receipt": "main regression and ledger sync",
        }
        proposed = {
            "action": "main_regression",
            "primary_task": "F1",
            "candidate_revision": "abc123",
            "files": [],
            "required_to_close_current_state": True,
        }

        self.assertEqual(
            event_scope_guard.classify_append(contract, proposed),
            ("SAME_EVENT", []),
        )

    def test_event_scope_guard_queues_unrelated_or_future_work(self) -> None:
        contract = {
            "event_id": "event-1",
            "event_type": "candidate integration",
            "primary_task": "F1",
            "candidate_revision": "abc123",
            "allowed_actions": ["review", "integrate", "ledger_sync"],
            "allowed_files": ["app/a.ts", "TASK_LEDGER.md"],
            "terminal_receipt": "integration verdict",
        }
        proposed = {
            "action": "implement",
            "primary_task": "F2",
            "candidate_revision": "def456",
            "files": ["app/b.ts"],
            "required_to_close_current_state": False,
            "starts_new_implementation": True,
            "waits_for_future_input": True,
        }

        decision, reasons = event_scope_guard.classify_append(contract, proposed)

        self.assertEqual(decision, "QUEUE_NEXT_EVENT")
        self.assertIn("different primary task", reasons)
        self.assertIn("new implementation belongs to a new event", reasons)
        self.assertIn("future input must trigger a new event", reasons)

    def test_assignment_lease_rejects_writes_before_complete_ack(self) -> None:
        errors = assignment_lease_guard.validate_assignment(
            {
                "assignment_id": "F1-writer-1",
                "agent_id": "/root/writer",
                "state": "RESERVED",
                "observed_modified_files": ["app/a.ts"],
            }
        )

        self.assertIn(
            "RESERVED assignment cannot modify files before delivered ACK", errors
        )

    def test_assignment_lease_allows_active_writer_with_complete_ack(self) -> None:
        errors = assignment_lease_guard.validate_assignment(
            {
                "assignment_id": "F1-writer-1",
                "agent_id": "/root/writer",
                "state": "ACTIVE",
                "primary_goal": "close F1 safely",
                "success_criteria": ["targeted test green"],
                "owned_scope": ["app/a.ts"],
                "forbidden_scope": [],
                "parallelizable": True,
                "role": "writer",
                "observed_modified_files": ["app/a.ts"],
                "ack": {
                    "repository_root": "/repo",
                    "branch": "codex/f1",
                    "head": "abc123",
                    "status": "clean at ACK",
                    "owned_files": ["app/a.ts"],
                    "first_red": "test_f1 fails",
                    "stop_condition": "candidate commit and receipt",
                },
            }
        )

        self.assertEqual(errors, [])

    def test_assignment_contract_requires_one_goal_and_parallel_decision(self) -> None:
        assignment = {
            "assignment_id": "F1-writer-1", "task_id": "F1", "agent_id": "/root/writer",
            "state": "ACKED", "observed_modified_files": [],
            "ack": {"repository_root": "/repo", "branch": "codex/f1", "head": "abc123",
                    "status": "clean at ACK", "owned_files": ["app/a.ts"],
                    "first_red": "test_f1 fails", "stop_condition": "candidate commit and receipt"},
        }
        errors = assignment_lease_guard.validate_assignment(assignment)
        self.assertIn("primary_goal is required", errors)
        self.assertIn("success_criteria must contain unique non-empty items", errors)
        self.assertIn("owned_scope must contain unique non-empty items", errors)
        self.assertIn("forbidden_scope must be a list", errors)
        self.assertIn("parallelizable must be true or false", errors)

    def test_assignment_contract_accepts_single_goal_and_parallel_reason(self) -> None:
        assignment = {
            "assignment_id": "F1-writer-1", "task_id": "F1", "agent_id": "/root/writer",
            "state": "ACKED", "primary_goal": "close F1 migration guard",
            "success_criteria": ["targeted test green", "scope-only diff"],
            "owned_scope": ["app/a.ts"], "forbidden_scope": ["app/b.ts"],
            "parallelizable": False, "dependency_reason": "shares migration contract with F0",
            "observed_modified_files": [],
            "ack": {"repository_root": "/repo", "branch": "codex/f1", "head": "abc123",
                    "status": "clean at ACK", "owned_files": ["app/a.ts"],
                    "first_red": "test_f1 fails", "stop_condition": "candidate commit and receipt"},
        }
        self.assertEqual(assignment_lease_guard.validate_assignment(assignment), [])

    def test_runtime_aware_active_requires_current_matching_lease(self) -> None:
        assignment = {
            "assignment_id": "F1-writer-1", "task_id": "F1", "agent_id": "/root/writer",
            "state": "ACTIVE", "worktree": "/tmp/wt", "observed_modified_files": [],
            "primary_goal": "close F1 safely", "success_criteria": ["targeted test green"],
            "owned_scope": ["app/a.ts"], "forbidden_scope": [], "parallelizable": True,
            "ack": {"repository_root": "/repo", "branch": "codex/f1", "head": "abc123",
                    "status": "clean at ACK", "owned_files": ["app/a.ts"],
                    "first_red": "test_f1 fails", "stop_condition": "candidate commit and receipt"},
        }
        errors = assignment_lease_guard.validate_assignment(assignment, runtime_state={"schema_version": 1, "leases": {}})
        self.assertIn("ACTIVE requires current runtime lease", errors)

    def test_runtime_aware_active_accepts_healthy_matching_lease(self) -> None:
        from datetime import datetime, timedelta, timezone
        now=datetime(2026,8,29,10,0,tzinfo=timezone.utc)
        assignment = {
            "assignment_id": "F1-writer-1", "task_id": "F1", "agent_id": "/root/writer",
            "state": "ACTIVE", "worktree": "/tmp/wt", "observed_modified_files": [],
            "primary_goal": "close F1 safely", "success_criteria": ["targeted test green"],
            "owned_scope": ["app/a.ts"], "forbidden_scope": [], "parallelizable": True,
            "ack": {"repository_root": "/repo", "branch": "codex/f1", "head": "abc123",
                    "status": "clean at ACK", "owned_files": ["app/a.ts"],
                    "first_red": "test_f1 fails", "stop_condition": "candidate commit and receipt"},
        }
        lease={"assignment_id":"F1-writer-1","task_id":"F1","agent_id":"/root/writer",
               "provider":"grok","session_id":"s1","worktree":"/tmp/wt",
               "lease_expires_at":(now+timedelta(minutes=20)).isoformat(),
               "progress_deadline_at":(now+timedelta(minutes=30)).isoformat(),"terminal_state":None}
        errors=assignment_lease_guard.validate_assignment(assignment, runtime_state={"schema_version":1,"leases":{"F1-writer-1":lease}}, now=now)
        self.assertEqual(errors, [])

    def test_assignment_lease_exact_launch_expectations_fail_closed(self) -> None:
        assignment = {
            "assignment_id": "F1-writer-1", "task_id": "F1", "agent_id": "writer",
            "state": "ACKED", "primary_goal": "close F1",
            "success_criteria": ["green"], "owned_scope": ["app/a.ts"],
            "forbidden_scope": [], "parallelizable": True, "observed_modified_files": [],
            "ack": {
                "repository_root": "/repo", "branch": "codex/f1", "head": "abc123",
                "status": "clean", "owned_files": ["app/a.ts"], "first_red": "red",
                "stop_condition": "candidate",
            },
        }
        errors = assignment_lease_guard.validate_assignment(
            assignment,
            expected_assignment_id="F1-writer-2",
            expected_task_id="F2",
            expected_agent_id="reviewer",
            expected_repository_root="/other",
            expected_branch="main",
            expected_head="def456",
        )
        self.assertIn("assignment_id does not match launch contract", errors)
        self.assertIn("task_id does not match launch contract", errors)
        self.assertIn("agent_id does not match launch contract", errors)
        self.assertIn("ack.repository_root does not match launch repository", errors)
        self.assertIn("ack.branch does not match launch branch", errors)
        self.assertIn("ack.head does not match launch revision", errors)

    def test_assignment_lease_exact_launch_expectations_accept_matching_ack(self) -> None:
        assignment = {
            "assignment_id": "F1-writer-1", "task_id": "F1", "agent_id": "writer",
            "state": "ACKED", "primary_goal": "close F1",
            "success_criteria": ["green"], "owned_scope": ["app/a.ts"],
            "forbidden_scope": [], "parallelizable": True, "observed_modified_files": [],
            "ack": {
                "repository_root": "/repo", "branch": "codex/f1", "head": "abc123",
                "status": "clean", "owned_files": ["app/a.ts"], "first_red": "red",
                "stop_condition": "candidate",
            },
        }
        self.assertEqual(assignment_lease_guard.validate_assignment(
            assignment,
            expected_assignment_id="F1-writer-1", expected_task_id="F1", expected_agent_id="writer",
            expected_repository_root="/repo", expected_branch="codex/f1", expected_head="abc123",
        ), [])

    def test_assignment_lease_reuse_requires_release_and_new_ack(self) -> None:
        errors = assignment_lease_guard.validate_assignment(
            {
                "assignment_id": "F2-writer-2",
                "agent_id": "/root/reused",
                "state": "ACTIVE",
                "observed_modified_files": [],
                "previous_assignment": {
                    "state": "ACTIVE",
                    "files_released": False,
                    "worktree_released": False,
                },
            }
        )

        self.assertTrue(any("complete delivered ACK" in error for error in errors))
        self.assertIn(
            "reused agent requires previous assignment to be FROZEN or TERMINAL",
            errors,
        )

    def test_control_event_guard_cli_rejects_invalid_ledger_contract(self) -> None:
        import json

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = root / "TASK_LEDGER.md"
            ledger.write_text(
                """# Ledger

- 当前 Goal：`M2-F2` 完成微信读书内部闭环
- 下一可见检查点：`M2-F2` 形成真实端证据
- 当前阻塞：无
- 规则版本：abc123

| ID | 状态 / 负责人 | 证据 / 下一步 |
| --- | --- | --- |
| M2-F2 | `ACTIVE` / 项目总控 | 下一步派发 `M2-F2-DEV-WEREAD-QA-01` 后继续 |
""",
                encoding="utf-8",
            )
            snapshot = self.complete_event_receipt()
            snapshot.update(
                {
                    "ledger_sha256": control_event_guard.ledger_sha256(ledger),
                    "available_slots": 0,
                    "ready_packages": [],
                    "required_reviews": [],
                }
            )
            receipt = root / "receipt.json"
            receipt.write_text(json.dumps(snapshot), encoding="utf-8")

            result = control_event_guard.main(
                [str(receipt), "--ledger", str(ledger)]
            )

            self.assertEqual(result, 1)

    def test_control_event_guard_requires_goal_rollover_on_goal_close(self) -> None:
        snapshot = {
            **self.complete_event_receipt(),
            "ledger_sha256": "abc123",
            "available_slots": 0,
            "ready_packages": [],
            "event_contract": {
                "event_id": "close-goal-1",
                "event_type": "Goal closure",
                "primary_task": "M2-F6",
                "candidate_revision": "ledger-abc123",
                "allowed_actions": ["ledger_sync"],
                "allowed_files": ["TASK_LEDGER.md"],
                "terminal_receipt": "M2-F6 Goal closed",
            },
        }

        errors = control_event_guard.validate_snapshot(
            snapshot,
            ledger_ready_ids=set(),
            ledger_open_ids={"M2-F6", "M1-F4-B"},
            ledger_goal_ids={"M2-F6"},
        )

        self.assertTrue(any("goal_rollover" in error for error in errors))

    def test_control_event_guard_accepts_goal_rollover_to_new_open_goal(self) -> None:
        snapshot = {
            **self.complete_event_receipt(),
            "ledger_sha256": "abc123",
            "available_slots": 0,
            "ready_packages": [],
            "event_contract": {
                "event_id": "close-goal-2",
                "event_type": "里程碑收口",
                "primary_task": "M2-F6",
                "candidate_revision": "ledger-abc123",
                "allowed_actions": ["ledger_sync"],
                "allowed_files": ["TASK_LEDGER.md"],
                "terminal_receipt": "M2-F6 Goal 已完成并关闭",
            },
            "event_actions": [{
                "action": "ledger_sync",
                "primary_task": "M2-F6",
                "candidate_revision": "ledger-abc123",
                "files": ["TASK_LEDGER.md"],
                "required_to_close_current_state": True,
            }],
            "goal_rollover": {
                "status": "rolled",
                "closed_goal_id": "M2-F6",
                "current_goal_id": "M1-F4-B",
                "project_recomputed": True,
            },
        }

        errors = control_event_guard.validate_snapshot(
            snapshot,
            ledger_ready_ids=set(),
            ledger_open_ids={"M2-F6", "M1-F4-B"},
            ledger_goal_ids={"M1-F4-B"},
        )

        self.assertEqual(errors, [])

    def test_control_event_guard_rejects_unproven_project_block_on_goal_close(self) -> None:
        snapshot = {
            **self.complete_event_receipt(),
            "ledger_sha256": "abc123",
            "available_slots": 0,
            "ready_packages": [],
            "event_contract": {
                "event_id": "close-goal-blocked",
                "event_type": "Goal closure",
                "primary_task": "M2-F6",
                "candidate_revision": "ledger-abc123",
                "allowed_actions": ["ledger_sync"],
                "allowed_files": ["TASK_LEDGER.md"],
                "terminal_receipt": "M2-F6 Goal closed",
            },
            "event_actions": [{
                "action": "ledger_sync",
                "primary_task": "M2-F6",
                "candidate_revision": "ledger-abc123",
                "files": ["TASK_LEDGER.md"],
                "required_to_close_current_state": True,
            }],
            "goal_rollover": {
                "status": "project_blocked",
                "closed_goal_id": "M2-F6",
                "project_recomputed": True,
                "blocked_scan": {
                    "project_scope_scan": True,
                    "ledger_revision": "abc123",
                    "open_packages": [{
                        "id": "M1-F4-B",
                        "state": "READY",
                        "can_progress": True,
                        "reason": "still executable",
                        "external_condition_id": "",
                    }],
                    "live_tasks": 0,
                    "pending_candidates": 0,
                    "controller_actions": 0,
                },
            },
        }

        errors = control_event_guard.validate_snapshot(
            snapshot,
            ledger_ready_ids=set(),
            ledger_open_ids={"M1-F4-B"},
            ledger_goal_ids=set(),
        )

        self.assertTrue(any("goal_rollover blocked scan" in error for error in errors))
        self.assertTrue(any("can still make progress" in error for error in errors))

    def test_control_event_guard_requires_every_ready_decision(self) -> None:
        snapshot = {
            **self.complete_event_receipt(),
            "ledger_sha256": "abc123",
            "available_slots": 1,
            "ready_packages": [
                {
                    "id": "F1",
                    "decision": "active",
                    "task_id": "task-1",
                    "delivered_ack": True,
                }
            ],
        }

        errors = control_event_guard.validate_snapshot(
            snapshot, ledger_ready_ids={"F1", "F2"}
        )

        self.assertIn("control event omitted READY packages: F2", errors)

    def test_control_event_guard_checks_reviewer_and_rule_acks(self) -> None:
        snapshot = {
            **self.complete_event_receipt(),
            "ledger_sha256": "abc123",
            "available_slots": 0,
            "ready_packages": [],
            "required_reviews": [
                {"id": "R1", "task_id": "review-1", "delivered_ack": False}
            ],
            "rule_update": {
                "revision": "def456",
                "affected_tasks": ["writer-1", "writer-2"],
                "acknowledged_tasks": ["writer-1"],
            },
        }

        errors = control_event_guard.validate_snapshot(
            snapshot,
            ledger_ready_ids=set(),
            required_review_ids={"R1"},
            expected_rule_revision="def456",
            affected_task_ids={"writer-1", "writer-2"},
        )

        self.assertIn("required review R1 requires delivered_ack=true", errors)
        self.assertIn("rule update missing loaded ACK: writer-2", errors)

    def test_control_event_guard_requires_tdd_causal_evidence_for_risk_scoped_review(self) -> None:
        snapshot = {
            **self.complete_event_receipt(), "ledger_sha256": "abc123", "available_slots": 0,
            "ready_packages": [],
            "required_reviews": [{"id": "R1", "task_id": "review-1", "delivered_ack": True, "tdd_required": True}],
        }
        errors = control_event_guard.validate_snapshot(snapshot, ledger_ready_ids=set(), required_review_ids={"R1"})
        self.assertIn("required review R1 requires red_evidence", errors)
        self.assertIn("required review R1 requires candidate_revision", errors)
        self.assertIn("required review R1 requires green_evidence", errors)
        self.assertIn("required review R1 requires red_green_same_case=true", errors)
        self.assertIn("required review R1 requires reviewer_counterexample", errors)
        self.assertIn("required review R1 requires verdict PASS or FAIL", errors)

    def test_control_event_guard_accepts_complete_tdd_causal_review(self) -> None:
        snapshot = {
            **self.complete_event_receipt(), "ledger_sha256": "abc123", "available_slots": 0,
            "ready_packages": [],
            "required_reviews": [{
                "id": "R1", "task_id": "review-1", "delivered_ack": True, "tdd_required": True,
                "red_evidence": "pre-fix test X fails with expected assertion", "candidate_revision": "deadbeef",
                "green_evidence": "candidate test X passes", "red_green_same_case": True,
                "reviewer_counterexample": "boundary X+1 remains rejected", "verdict": "PASS",
            }],
        }
        self.assertEqual(control_event_guard.validate_snapshot(snapshot, ledger_ready_ids=set(), required_review_ids={"R1"}), [])

    def test_review_pass_cannot_close_while_candidate_is_still_only_in_review(self) -> None:
        snapshot = {
            **self.complete_event_receipt(), "ledger_sha256": "abc", "available_slots": 0, "ready_packages": [],
            "required_reviews": [{"id": "R1", "task_id": "review-1", "delivered_ack": True,
                                  "candidate_revision": "candidate-1", "verdict": "PASS"}],
            "candidate_packages": [{"revision": "candidate-1", "worktree": "/repo/wt",
                                     "task_id": "F1", "integration_flow": "server-main",
                                     "decision": "review", "review_task_id": "review-1", "delivered_ack": True}],
            "new_assignments": [],
        }
        errors = control_event_guard.validate_snapshot(
            snapshot, ledger_ready_ids=set(), expected_candidates={"/repo/wt": "candidate-1"}
        )
        self.assertIn("review PASS for candidate-1 requires completed integration or ordered integration queue", errors)

    def test_review_fail_requires_same_candidate_rework_disposition(self) -> None:
        snapshot = {
            **self.complete_event_receipt(), "ledger_sha256": "abc", "available_slots": 0, "ready_packages": [],
            "required_reviews": [{"id": "R1", "task_id": "review-1", "delivered_ack": True,
                                  "candidate_revision": "candidate-1", "verdict": "FAIL"}],
            "candidate_packages": [{"revision": "candidate-1", "worktree": "/repo/wt",
                                     "task_id": "F1", "integration_flow": "server-main",
                                     "decision": "review", "review_task_id": "review-1", "delivered_ack": True}],
            "new_assignments": [],
        }
        errors = control_event_guard.validate_snapshot(
            snapshot, ledger_ready_ids=set(), expected_candidates={"/repo/wt": "candidate-1"}
        )
        self.assertIn("review FAIL for candidate-1 requires rework disposition", errors)

    def test_completed_integration_requires_exact_main_revision_and_regression_evidence(self) -> None:
        base_candidate = {
            "revision": "candidate-1", "worktree": "/repo/wt", "task_id": "F1",
            "integration_flow": "server-main", "decision": "integrate",
            "controller_event_id": "event-1", "integrated_this_event": True,
        }
        for missing_field in ("main_revision", "regression_evidence"):
            candidate = {**base_candidate, "main_revision": "main-2", "regression_evidence": "tests green"}
            candidate.pop(missing_field)
            snapshot = {
                **self.complete_event_receipt(), "ledger_sha256": "abc", "available_slots": 0, "ready_packages": [],
                "required_reviews": [{"id": "R1", "task_id": "review-1", "delivered_ack": True,
                                      "candidate_revision": "candidate-1", "verdict": "PASS"}],
                "candidate_packages": [candidate], "new_assignments": [],
            }
            errors = control_event_guard.validate_snapshot(
                snapshot, ledger_ready_ids=set(), expected_candidates={}, expected_main_revision="main-2"
            )
            self.assertIn(f"candidate-1 integrate requires {missing_field}", errors)

    def test_integrated_candidate_revisions_are_derived_from_git_ancestry(self) -> None:
        from types import SimpleNamespace
        from unittest.mock import patch
        snapshot = {"candidate_packages": [
            {"revision": "good", "decision": "integrate"},
            {"revision": "bad", "decision": "integrate"},
            {"revision": "queued", "decision": "queued"},
        ]}
        def fake_git(_root, *args, **kwargs):
            self.assertEqual(args[:2], ("merge-base", "--is-ancestor"))
            return SimpleNamespace(returncode=0 if args[2] == "good" else 1)
        with patch.object(control_event_guard, "run_git", side_effect=fake_git):
            revisions = control_event_guard.integrated_candidate_revisions(Path("/repo"), snapshot, "main-2")
        self.assertEqual(revisions, {"good"})

    def test_completed_integration_requires_candidate_to_be_ancestor_of_current_main(self) -> None:
        snapshot = {
            **self.complete_event_receipt(), "ledger_sha256": "abc", "available_slots": 0, "ready_packages": [],
            "required_reviews": [{"id": "R1", "task_id": "review-1", "delivered_ack": True,
                                  "candidate_revision": "candidate-1", "verdict": "PASS"}],
            "candidate_packages": [{
                "revision": "candidate-1", "worktree": "/repo/wt", "task_id": "F1",
                "integration_flow": "server-main", "decision": "integrate",
                "controller_event_id": "event-1", "integrated_this_event": True,
                "main_revision": "main-2", "regression_evidence": "current-main regression PASS",
            }],
            "new_assignments": [],
        }
        errors = control_event_guard.validate_snapshot(
            snapshot, ledger_ready_ids=set(), expected_candidates={}, expected_main_revision="main-2",
            expected_integrated_revisions=set(),
        )
        self.assertIn("candidate-1 integrate requires candidate revision to be an ancestor of current main", errors)

    def test_review_pass_accepts_completed_integration_with_current_main_regression(self) -> None:
        snapshot = {
            **self.complete_event_receipt(), "ledger_sha256": "abc", "available_slots": 0, "ready_packages": [],
            "required_reviews": [{"id": "R1", "task_id": "review-1", "delivered_ack": True,
                                  "candidate_revision": "candidate-1", "verdict": "PASS"}],
            "candidate_packages": [{
                "revision": "candidate-1", "worktree": "/repo/wt", "task_id": "F1",
                "integration_flow": "server-main", "decision": "integrate",
                "controller_event_id": "event-1", "integrated_this_event": True,
                "main_revision": "main-2", "regression_evidence": "current-main targeted 16/16 PASS",
            }],
            "new_assignments": [],
        }
        self.assertEqual(control_event_guard.validate_snapshot(
            snapshot, ledger_ready_ids=set(), expected_candidates={}, expected_main_revision="main-2",
            expected_integrated_revisions={"candidate-1"},
        ), [])

    def test_review_pass_accepts_ordered_integration_queue(self) -> None:
        snapshot = {
            **self.complete_event_receipt(), "ledger_sha256": "abc", "available_slots": 0, "ready_packages": [],
            "required_reviews": [{"id": "R1", "task_id": "review-1", "delivered_ack": True,
                                  "candidate_revision": "candidate-1", "verdict": "PASS"}],
            "candidate_packages": [{
                "revision": "candidate-1", "worktree": "/repo/wt", "task_id": "F1",
                "integration_flow": "server-main", "decision": "queued",
                "reason_code": "ordered_integration", "next_checkpoint": "after preceding candidate integrates",
            }],
            "new_assignments": [],
        }
        self.assertEqual(control_event_guard.validate_snapshot(
            snapshot, ledger_ready_ids=set(), expected_candidates={"/repo/wt": "candidate-1"}
        ), [])

    def test_review_fail_accepts_acknowledged_rework(self) -> None:
        snapshot = {
            **self.complete_event_receipt(), "ledger_sha256": "abc", "available_slots": 0, "ready_packages": [],
            "required_reviews": [{"id": "R1", "task_id": "review-1", "delivered_ack": True,
                                  "candidate_revision": "candidate-1", "verdict": "FAIL"}],
            "candidate_packages": [{
                "revision": "candidate-1", "worktree": "/repo/wt", "task_id": "F1",
                "integration_flow": "server-main", "decision": "rework",
                "writer_task_id": "F1-REWORK", "delivered_ack": True,
            }],
            "new_assignments": [],
        }
        self.assertEqual(control_event_guard.validate_snapshot(
            snapshot, ledger_ready_ids=set(), expected_candidates={"/repo/wt": "candidate-1"}
        ), [])

    def test_control_event_guard_allows_complete_event(self) -> None:
        snapshot = {
            **self.complete_event_receipt(),
            "ledger_sha256": "abc123",
            "available_slots": 1,
            "ready_packages": [
                {
                    "id": "F1",
                    "decision": "active",
                    "task_id": "task-1",
                    "delivered_ack": True,
                },
                {
                    "id": "F2",
                    "decision": "deferred",
                    "reason": "shares the same output directory as F1",
                    "reason_code": "file_conflict",
                },
            ],
            "required_reviews": [
                {"id": "R1", "task_id": "review-1", "delivered_ack": True}
            ],
            "rule_update": {
                "revision": "def456",
                "affected_tasks": ["writer-1"],
                "acknowledged_tasks": ["writer-1"],
            },
        }

        self.assertEqual(
            control_event_guard.validate_snapshot(
                snapshot,
                ledger_ready_ids={"F1", "F2"},
                expected_ledger_sha256="abc123",
                required_review_ids={"R1"},
                expected_rule_revision="def456",
                affected_task_ids={"writer-1"},
            ),
            [],
        )

    def test_control_event_guard_rejects_stale_ledger_and_omitted_expectations(self) -> None:
        snapshot = {
            **self.complete_event_receipt(),
            "ledger_sha256": "stale",
            "available_slots": 0,
            "ready_packages": [],
        }

        errors = control_event_guard.validate_snapshot(
            snapshot,
            ledger_ready_ids=set(),
            expected_ledger_sha256="current",
            required_review_ids={"UX-EARLY"},
            expected_rule_revision="rule-2",
            affected_task_ids={"writer-1"},
        )

        self.assertIn("ledger_sha256 does not match the current ledger", errors)
        self.assertIn("control event omitted required reviews: UX-EARLY", errors)
        self.assertIn("control event omitted the declared rule update", errors)

    def test_control_event_guard_rejects_multi_chain_event_journal(self) -> None:
        snapshot = {
            "ledger_sha256": "abc123",
            "available_slots": 0,
            "ready_packages": [],
            "event_contract": {
                "event_id": "event-1",
                "event_type": "candidate integration",
                "primary_task": "F1",
                "candidate_revision": "abc123",
                "allowed_actions": ["review", "integrate", "ledger_sync"],
                "allowed_files": ["app/a.ts", "TASK_LEDGER.md"],
                "terminal_receipt": "F1 integrated and ledger synchronized",
            },
            "event_actions": [
                {
                    "action": "integrate",
                    "primary_task": "F1",
                    "candidate_revision": "abc123",
                    "files": ["app/a.ts"],
                    "required_to_close_current_state": True,
                },
                {
                    "action": "integrate",
                    "primary_task": "F2",
                    "candidate_revision": "def456",
                    "files": ["app/b.ts"],
                    "required_to_close_current_state": True,
                },
            ],
            "terminal_receipt_issued": True,
        }

        errors = control_event_guard.validate_snapshot(
            snapshot, ledger_ready_ids=set()
        )

        self.assertTrue(any("event_actions[1]" in error for error in errors))
        self.assertTrue(any("different primary task" in error for error in errors))

    def test_control_event_guard_rejects_vague_deferral_with_idle_slot(self) -> None:
        snapshot = {
            "ledger_sha256": "abc123",
            "available_slots": 2,
            "ready_packages": [
                {
                    "id": "F1",
                    "decision": "deferred",
                    "reason": "handle in the next event",
                }
            ],
            "event_contract": {
                "event_id": "event-1",
                "event_type": "dispatch",
                "primary_task": "F1",
                "candidate_revision": "ledger-abc123",
                "allowed_actions": ["dispatch", "ledger_sync"],
                "allowed_files": ["TASK_LEDGER.md"],
                "terminal_receipt": "all READY packages have exact decisions",
            },
            "event_actions": [
                {
                    "action": "ledger_sync",
                    "primary_task": "F1",
                    "candidate_revision": "ledger-abc123",
                    "files": ["TASK_LEDGER.md"],
                    "required_to_close_current_state": True,
                }
            ],
            "terminal_receipt_issued": True,
        }

        errors = control_event_guard.validate_snapshot(
            snapshot, ledger_ready_ids={"F1"}
        )

        self.assertTrue(any("reason_code" in error for error in errors))
        self.assertTrue(any("idle dispatch capacity" in error for error in errors))

    def test_control_event_guard_absorbed_candidate_releases_flow_wip(self) -> None:
        snapshot = {
            **self.complete_event_receipt(),
            "ledger_sha256": "abc123",
            "available_slots": 1,
            "ready_packages": [],
            "candidate_packages": [{
                "revision": "candidate-1",
                "worktree": "/repo/worktree-1",
                "task_id": "WEB-1",
                "integration_flow": "web-main",
                "decision": "absorbed",
                "absorbing_revision": "main-2",
                "retention_reason": "retain QA evidence",
            }],
            "new_assignments": [{"task_id": "WEB-2", "integration_flow": "web-main"}],
        }

        self.assertEqual(
            control_event_guard.validate_snapshot(
                snapshot,
                ledger_ready_ids=set(),
                expected_candidates={"/repo/worktree-1": "candidate-1"},
            ),
            [],
        )

    def test_control_event_guard_parked_candidate_requires_recovery_metadata(self) -> None:
        snapshot = {
            **self.complete_event_receipt(),
            "ledger_sha256": "abc123",
            "available_slots": 0,
            "ready_packages": [],
            "candidate_packages": [{
                "revision": "candidate-1",
                "worktree": "/repo/worktree-1",
                "task_id": "SERVER-1",
                "integration_flow": "server-main",
                "decision": "parked",
            }],
            "new_assignments": [],
        }

        errors = control_event_guard.validate_snapshot(
            snapshot,
            ledger_ready_ids=set(),
            expected_candidates={"/repo/worktree-1": "candidate-1"},
        )

        self.assertTrue(any("parked requires reason_code" in error for error in errors))
        self.assertTrue(any("parked requires wake_condition" in error for error in errors))
        self.assertTrue(any("parked requires retention_reason" in error for error in errors))

    def test_candidate_inventory_excludes_absorbed_retained_worktree(self) -> None:
        import subprocess

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            worktree = Path(directory) / "candidate"
            state_dir = Path(directory) / "state"
            root.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
            (root / "base.txt").write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "add", "base.txt"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "worktree", "add", "-b", "candidate", str(worktree)], cwd=root, check=True, capture_output=True)
            (worktree / "candidate.txt").write_text("candidate\n", encoding="utf-8")
            subprocess.run(["git", "add", "candidate.txt"], cwd=worktree, check=True)
            subprocess.run(["git", "commit", "-m", "candidate"], cwd=worktree, check=True, capture_output=True)
            revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=worktree, check=True, capture_output=True, text=True).stdout.strip()
            main_revision = subprocess.run(["git", "rev-parse", "main"], cwd=root, check=True, capture_output=True, text=True).stdout.strip()

            control_event_guard.record_candidate_lifecycle(
                root,
                [{
                    "revision": revision,
                    "worktree": str(worktree),
                    "decision": "absorbed",
                    "absorbing_revision": main_revision,
                    "retention_reason": "keep real-device QA evidence",
                }],
                state_dir=state_dir,
            )

            self.assertEqual(
                control_event_guard.unmerged_worktree_candidates(root, state_dir=state_dir),
                {},
            )
            inventory = control_event_guard.worktree_candidate_inventory(root, state_dir=state_dir)
            self.assertEqual(inventory["retained"][str(worktree.resolve())]["state"], "absorbed")

    def test_candidate_inventory_reactivates_when_retained_worktree_head_changes(self) -> None:
        import subprocess

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            worktree = Path(directory) / "candidate"
            state_dir = Path(directory) / "state"
            root.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
            (root / "base.txt").write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "add", "base.txt"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "worktree", "add", "-b", "candidate", str(worktree)], cwd=root, check=True, capture_output=True)
            (worktree / "candidate.txt").write_text("v1\n", encoding="utf-8")
            subprocess.run(["git", "add", "candidate.txt"], cwd=worktree, check=True)
            subprocess.run(["git", "commit", "-m", "v1"], cwd=worktree, check=True, capture_output=True)
            old_revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=worktree, check=True, capture_output=True, text=True).stdout.strip()
            main_revision = subprocess.run(["git", "rev-parse", "main"], cwd=root, check=True, capture_output=True, text=True).stdout.strip()
            control_event_guard.record_candidate_lifecycle(root, [{
                "revision": old_revision,
                "worktree": str(worktree),
                "decision": "absorbed",
                "absorbing_revision": main_revision,
                "retention_reason": "keep QA evidence",
            }], state_dir=state_dir)
            (worktree / "candidate.txt").write_text("v2\n", encoding="utf-8")
            subprocess.run(["git", "add", "candidate.txt"], cwd=worktree, check=True)
            subprocess.run(["git", "commit", "-m", "v2"], cwd=worktree, check=True, capture_output=True)
            new_revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=worktree, check=True, capture_output=True, text=True).stdout.strip()

            self.assertEqual(
                control_event_guard.unmerged_worktree_candidates(root, state_dir=state_dir),
                {str(worktree.resolve()): new_revision},
            )

    def test_control_event_guard_cli_persists_retained_candidate_state(self) -> None:
        import json
        import subprocess

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            worktree = Path(directory) / "candidate"
            state_dir = Path(directory) / "state"
            root.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
            ledger = root / "TASK_LEDGER.md"
            ledger.write_text((SKILL_ROOT / "assets" / "templates" / "TASK_LEDGER.md").read_text(encoding="utf-8"), encoding="utf-8")
            subprocess.run(["git", "add", "TASK_LEDGER.md"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "worktree", "add", "-b", "candidate", str(worktree)], cwd=root, check=True, capture_output=True)
            (worktree / "candidate.txt").write_text("candidate\n", encoding="utf-8")
            subprocess.run(["git", "add", "candidate.txt"], cwd=worktree, check=True)
            subprocess.run(["git", "commit", "-m", "candidate"], cwd=worktree, check=True, capture_output=True)
            revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=worktree, check=True, capture_output=True, text=True).stdout.strip()
            main_revision = subprocess.run(["git", "rev-parse", "main"], cwd=root, check=True, capture_output=True, text=True).stdout.strip()
            snapshot = {
                **self.complete_event_receipt(),
                "ledger_sha256": control_event_guard.ledger_sha256(ledger),
                "available_slots": 1,
                "ready_packages": [{"id": "INIT-01", "decision": "active", "task_id": "INIT-01-WRITER", "delivered_ack": True}],
                "candidate_packages": [{
                    "revision": revision,
                    "worktree": str(worktree.resolve()),
                    "task_id": "OLD-CANDIDATE",
                    "integration_flow": "mini-main",
                    "decision": "absorbed",
                    "absorbing_revision": main_revision,
                    "retention_reason": "retain QA evidence",
                }],
                "new_assignments": [],
            }
            receipt = root / "receipt.json"
            receipt.write_text(json.dumps(snapshot), encoding="utf-8")
            previous = control_event_guard.CANDIDATE_STATE_ROOT
            control_event_guard.CANDIDATE_STATE_ROOT = state_dir
            try:
                result = control_event_guard.main([str(receipt), "--ledger", str(ledger), "--repo", str(root)])
                self.assertEqual(result, 0)
                self.assertEqual(control_event_guard.unmerged_worktree_candidates(root, state_dir=state_dir), {})
            finally:
                control_event_guard.CANDIDATE_STATE_ROOT = previous

    def test_control_event_guard_requires_every_live_candidate_decision(self) -> None:
        snapshot = {
            **self.complete_event_receipt(),
            "ledger_sha256": "abc123",
            "available_slots": 1,
            "ready_packages": [],
            "candidate_packages": [],
            "new_assignments": [],
        }

        errors = control_event_guard.validate_snapshot(
            snapshot,
            ledger_ready_ids=set(),
            expected_candidates={"/repo/worktree-1": "candidate-1"},
        )

        self.assertIn("control event omitted unmerged candidates: candidate-1", errors)

    def test_control_event_guard_blocks_same_flow_writer_behind_candidate(self) -> None:
        snapshot = {
            **self.complete_event_receipt(),
            "ledger_sha256": "abc123",
            "available_slots": 2,
            "ready_packages": [],
            "candidate_packages": [
                {
                    "revision": "candidate-1",
                    "worktree": "/repo/worktree-1",
                    "task_id": "WEB-1",
                    "integration_flow": "web-main",
                    "decision": "review",
                    "review_task_id": "review-web-1",
                    "delivered_ack": True,
                }
            ],
            "new_assignments": [
                {"task_id": "WEB-2", "integration_flow": "web-main"}
            ],
        }

        errors = control_event_guard.validate_snapshot(
            snapshot,
            ledger_ready_ids=set(),
            expected_candidates={"/repo/worktree-1": "candidate-1"},
        )

        self.assertTrue(any("WEB-2 cannot start" in error for error in errors))

    def test_control_event_guard_accepts_parallel_flow_behind_candidate(self) -> None:
        snapshot = {
            **self.complete_event_receipt(),
            "ledger_sha256": "abc123",
            "available_slots": 2,
            "ready_packages": [],
            "candidate_packages": [
                {
                    "revision": "candidate-1",
                    "worktree": "/repo/worktree-1",
                    "task_id": "WEB-1",
                    "integration_flow": "web-main",
                    "decision": "integrate",
                    "controller_event_id": "integrate-web-1",
                    "integrated_this_event": True,
                    "main_revision": "main-after-web-1",
                    "regression_evidence": "current-main targeted regression PASS",
                }
            ],
            "new_assignments": [
                {"task_id": "MINI-1", "integration_flow": "mini-main"}
            ],
        }

        self.assertEqual(
            control_event_guard.validate_snapshot(
                snapshot,
                ledger_ready_ids=set(),
                expected_candidates={"/repo/worktree-1": "candidate-1"},
            ),
            [],
        )

    def test_control_event_guard_requires_exact_checkpoint_for_queued_candidate(self) -> None:
        snapshot = {
            **self.complete_event_receipt(),
            "ledger_sha256": "abc123",
            "available_slots": 0,
            "ready_packages": [],
            "candidate_packages": [
                {
                    "revision": "candidate-1",
                    "worktree": "/repo/worktree-1",
                    "task_id": "SERVER-1",
                    "integration_flow": "server-main",
                    "decision": "queued",
                    "reason_code": "capacity",
                }
            ],
            "new_assignments": [],
        }

        errors = control_event_guard.validate_snapshot(
            snapshot,
            ledger_ready_ids=set(),
            expected_candidates={"/repo/worktree-1": "candidate-1"},
        )

        self.assertIn("candidate-1 queued requires next_checkpoint", errors)

    def test_preblock_guard_rejects_ready_work_outside_current_goal(self) -> None:
        snapshot = {
            "project_scope_scan": True,
            "ledger_revision": "abc123",
            "open_packages": [
                {
                    "id": "CURRENT-GOAL",
                    "state": "BLOCKED",
                    "can_progress": False,
                    "reason": "GUI unavailable",
                    "external_condition_id": "gui-session",
                },
                {
                    "id": "P2-OTHER-END",
                    "state": "READY",
                    "can_progress": True,
                    "reason": "independent package",
                    "external_condition_id": "",
                },
            ],
            "live_tasks": 0,
            "pending_candidates": 0,
            "controller_actions": 0,
        }

        errors = preblock_guard.validate_snapshot(
            snapshot, ledger_package_ids={"CURRENT-GOAL", "P2-OTHER-END"}
        )

        self.assertTrue(any("P2-OTHER-END can still make progress" in e for e in errors))
        self.assertTrue(any("P2-OTHER-END is still READY" in e for e in errors))

    def test_preblock_guard_rejects_active_work_and_controller_actions(self) -> None:
        snapshot = {
            "project_scope_scan": True,
            "ledger_revision": "abc123",
            "open_packages": [
                {
                    "id": "F1",
                    "state": "ACTIVE",
                    "can_progress": False,
                    "reason": "writer still running",
                    "external_condition_id": "writer-result",
                }
            ],
            "live_tasks": 1,
            "pending_candidates": 0,
            "controller_actions": 1,
        }

        errors = preblock_guard.validate_snapshot(snapshot, ledger_package_ids={"F1"})

        self.assertTrue(any("F1 is still ACTIVE" in e for e in errors))
        self.assertTrue(any("live_tasks must be zero" in e for e in errors))
        self.assertTrue(any("controller_actions must be zero" in e for e in errors))

    def test_preblock_guard_allows_single_shared_external_blocker(self) -> None:
        snapshot = {
            "project_scope_scan": True,
            "ledger_revision": "abc123",
            "open_packages": [
                {
                    "id": "F1",
                    "state": "BLOCKED",
                    "can_progress": False,
                    "reason": "waiting for the same production credential",
                    "external_condition_id": "production-credential",
                },
                {
                    "id": "F2",
                    "state": "BLOCKED",
                    "can_progress": False,
                    "reason": "waiting for the same production credential",
                    "external_condition_id": "production-credential",
                },
            ],
            "live_tasks": 0,
            "pending_candidates": 0,
            "controller_actions": 0,
        }

        self.assertEqual(
            preblock_guard.validate_snapshot(snapshot, ledger_package_ids={"F1", "F2"}),
            [],
        )

    def test_preblock_guard_rejects_omitted_open_ledger_package(self) -> None:
        snapshot = {
            "project_scope_scan": True,
            "ledger_revision": "abc123",
            "open_packages": [
                {
                    "id": "F1",
                    "state": "BLOCKED",
                    "can_progress": False,
                    "reason": "waiting for credential",
                    "external_condition_id": "credential",
                }
            ],
            "live_tasks": 0,
            "pending_candidates": 0,
            "controller_actions": 0,
        }

        errors = preblock_guard.validate_snapshot(
            snapshot, ledger_package_ids={"F1", "P2-READY"}
        )

        self.assertIn("scan omitted ledger packages: P2-READY", errors)

    def test_durable_profile_creates_nine_project_documents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = init_project.initialize_project(root, profile="durable")

            self.assertEqual(
                set(report.created),
                {
                    "AGENTS.md",
                    "TASK_LEDGER.md",
                    "MEMORY.md",
                    "WIKI_INDEX.md",
                    "SKILL.md",
                    "SPEC.md",
                    "DESIGN.md",
                    "TECHNICAL.md",
                    "EVOLUTION.md",
                },
            )
            errors, warnings = lint_governance.lint_project(root, strict=True)
            self.assertEqual(errors, [])
            self.assertEqual(warnings, [])

    def test_durable_profile_initializes_queryable_knowledge_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            report = init_project.initialize_project(root, profile="durable")

            self.assertEqual(
                set(report.created_directories),
                {
                    "raw_sources",
                    "wiki",
                    "logs",
                    "logs/ingestion",
                },
            )
            self.assertTrue((root / "raw_sources").is_dir())
            self.assertTrue((root / "wiki").is_dir())
            self.assertTrue((root / "logs" / "ingestion").is_dir())

    def test_existing_legacy_ledger_prevents_second_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "PROJECT_STATUS.md").write_text("# Existing\n", encoding="utf-8")

            report = init_project.initialize_project(root, profile="collaborative")

            self.assertFalse((root / "TASK_LEDGER.md").exists())
            self.assertIn(
                "TASK_LEDGER.md (using existing PROJECT_STATUS.md)", report.skipped
            )

    def test_linter_rejects_two_ledgers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "TASK_LEDGER.md").write_text("# New\n", encoding="utf-8")
            (root / "PROJECT_STATUS.md").write_text("# Old\n", encoding="utf-8")

            errors, _ = lint_governance.lint_project(root)

            self.assertTrue(any("both TASK_LEDGER" in error for error in errors))

    def test_linter_accepts_parallel_active_rows_when_pointer_matches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "TASK_LEDGER.md").write_text(
                """# Ledger

- 当前活动项：F1、F2

| ID | 状态 | 负责人 | 文件或范围 | 下一步动作 |
| --- | --- | --- | --- | --- |
| F1 | `ACTIVE` | Agent A | app/auth/** | 完成后进入 F3 |
| F2 | `ACTIVE` | Agent B | app/reader/** | 完成后进入 F4 |
""",
                encoding="utf-8",
            )

            errors, _ = lint_governance.lint_project(root)

            self.assertEqual(errors, [])

    def test_linter_rejects_activity_pointer_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "TASK_LEDGER.md").write_text(
                """# Ledger

- 当前活动项：F1

| ID | 状态 |
| --- | --- |
| F1 | `ACTIVE` |
| F2 | `ACTIVE` |
""",
                encoding="utf-8",
            )

            errors, _ = lint_governance.lint_project(root)

            self.assertTrue(any("current activity pointer does not match" in error for error in errors))

    def test_linter_allows_omitting_derived_activity_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "TASK_LEDGER.md").write_text(
                """# Ledger

| ID | 状态 / 负责人 |
| --- | --- |
| F1 | `ACTIVE` / Agent A |
""",
                encoding="utf-8",
            )

            errors, warnings = lint_governance.lint_project(root)

            self.assertEqual(errors, [])
            self.assertFalse(any("current activity" in warning for warning in warnings))

    def test_ledger_consistency_uses_task_table_as_canonical_state(self) -> None:
        text = """# Ledger

- 当前 Goal：`F1` 完成真实登录闭环
- 下一可见检查点：`F1` 真实浏览器登录恢复通过
- 当前阻塞：无
- 规则版本：abc123

| ID | 状态 / 负责人 | 证据 / 下一步 |
| --- | --- | --- |
| F1 | `ACTIVE` / Agent A | 完成登录恢复 Case |
| F2 | `RECOVERING` / Agent B Assignment F2-RECOVERY-01 | delivered ACK；修复 RED 后在 checkpoint 复审 |
"""

        self.assertEqual(ledger_consistency_guard.validate_ledger(text), [])

    def test_ledger_consistency_rejects_implicit_next_task_not_in_task_table(self) -> None:
        text = """# Ledger

- 当前 Goal：`M2-F2` 完成微信读书内部闭环
- 下一可见检查点：`M2-F2` 形成真实端证据
- 当前阻塞：无
- 规则版本：abc123

| ID | 状态 / 负责人 | 证据 / 下一步 |
| --- | --- | --- |
| M2-F2 | `ACTIVE` / 项目总控 | 下一步派发 `M2-F2-DEV-WEREAD-QA-01` 后继续 |
"""

        errors = ledger_consistency_guard.validate_ledger(text)

        self.assertTrue(
            any("undeclared task ID M2-F2-DEV-WEREAD-QA-01" in error for error in errors)
        )

    def test_ledger_consistency_allows_assignment_id_without_extra_task_row(self) -> None:
        text = """# Ledger

- 当前 Goal：`M1-F4-C-SERVER-GATE` 完成后端门禁
- 下一可见检查点：`M1-F4-C-SERVER-GATE` 的 Assignment `M1-F4-C-SERVER-GATE-B-01` 返回候选
- 当前阻塞：无
- 规则版本：abc123

| ID | 状态 / 负责人 | 证据 / 下一步 |
| --- | --- | --- |
| M1-F4-C-SERVER-GATE | `ACTIVE` / Agent B | 当前 Assignment `M1-F4-C-SERVER-GATE-B-01` delivered ACK；等待候选 |
"""

        self.assertEqual(ledger_consistency_guard.validate_ledger(text), [])

    def test_ledger_consistency_assignment_exemption_requires_declared_parent(self) -> None:
        text = """# Ledger

- 当前 Goal：`M1-F4-C-SERVER-GATE` 完成后端门禁
- 下一可见检查点：`M1-F4-C-SERVER-GATE` 等待 Assignment `M9-UNKNOWN-B-01`
- 当前阻塞：无
- 规则版本：abc123

| ID | 状态 / 负责人 | 证据 / 下一步 |
| --- | --- | --- |
| M1-F4-C-SERVER-GATE | `ACTIVE` / Agent B | 等待外部执行 |
"""

        errors = ledger_consistency_guard.validate_ledger(text)
        self.assertTrue(any("undeclared task ID M9-UNKNOWN-B-01" in error for error in errors))

    def test_ledger_consistency_rejects_implicit_task_in_visible_checkpoint(self) -> None:
        text = """# Ledger

- 当前 Goal：`M2-F2` 完成微信读书内部闭环
- 下一可见检查点：`M2-F2` 的 `M2-F2-DEV-WEREAD-QA-01` 先形成候选
- 当前阻塞：无
- 规则版本：abc123

| ID | 状态 / 负责人 | 证据 / 下一步 |
| --- | --- | --- |
| M2-F2 | `ACTIVE` / 项目总控 | 完成当前父任务 |
"""

        errors = ledger_consistency_guard.validate_ledger(text)

        self.assertTrue(
            any("checkpoint references undeclared task ID M2-F2-DEV-WEREAD-QA-01" in error for error in errors)
        )

    def test_ledger_consistency_does_not_treat_crypto_algorithm_as_task_id(self) -> None:
        text = """# Ledger

- 当前 Goal：`M1-F3A-A` 完成加密闭环
- 下一可见检查点：`M1-F3A-A` 完成验证
- 当前阻塞：无
- 规则版本：abc123

| ID | 状态 / 负责人 | 证据 / 下一步 |
| --- | --- | --- |
| M1-F3A-A | `ACTIVE` / Agent A | 使用 AES-256-GCM 完成加密验证 |
"""

        errors = ledger_consistency_guard.validate_ledger(text)

        self.assertFalse(any("AES-256-GCM" in error for error in errors))

    def test_ledger_consistency_recovering_requires_real_execution_binding(self) -> None:
        text = """# Ledger

- 当前 Goal：`M1-F4-B` 修复后端闭环
- 下一可见检查点：`M1-F4-B` 恢复执行
- 当前阻塞：无
- 规则版本：abc123

| ID | 状态 / 负责人 | 证据 / 下一步 |
| --- | --- | --- |
| M1-F4-B | `RECOVERING` / 项目总控 | 后续只在形成新的可验证执行路由后再恢复 |
"""

        errors = ledger_consistency_guard.validate_ledger(text)

        self.assertIn(
            "M1-F4-B RECOVERING requires a delivered assignment ACK or a verifiable recovery action",
            errors,
        )

    def test_ledger_consistency_allows_recovering_with_delivered_assignment_ack(self) -> None:
        text = """# Ledger

- 当前 Goal：`M2-F2` 完成微信读书内部闭环
- 下一可见检查点：`M2-F2` 候选进入代码门
- 当前阻塞：无
- 规则版本：abc123

| ID | 状态 / 负责人 | 固定边界与当前证据 | 依赖、阻塞与下一步 |
| --- | --- | --- | --- |
| M2-F2 | `RECOVERING` / 外部 Kimi `M2-F2-DEV-WEREAD-QA-01 / kimi-k3 / api / high` | Writer 已完整 delivered ACK，lease guard PASS | 恢复动作：只实现 develop-only port，候选先过非作者代码门 |
"""

        self.assertEqual(ledger_consistency_guard.validate_ledger(text), [])

    def test_ledger_consistency_rejects_stale_pointer_and_unmapped_goal(self) -> None:
        text = """# Ledger

- 当前 Goal：旧目标
- 当前活动项：F1
- 下一可见检查点：稍后看看
- 当前阻塞：无
- 规则版本：abc123

| ID | 状态 / 负责人 | 证据 / 下一步 |
| --- | --- | --- |
| F1 | `ACTIVE` / Agent A | 完成登录恢复 Case |
| F2 | `RECOVERING` / 待分配 | 等待 |
"""

        errors = ledger_consistency_guard.validate_ledger(text)

        self.assertIn(
            "current activity pointer does not match ACTIVE/RECOVERING task rows",
            errors,
        )
        self.assertIn("current Goal must reference at least one open task ID", errors)
        self.assertIn("next visible checkpoint must reference at least one open task ID", errors)
        self.assertIn("F2 RECOVERING requires a unique owner", errors)

    def test_ledger_consistency_does_not_match_task_id_prefixes(self) -> None:
        text = """# Ledger

- 当前 Goal：`F10` 另一个任务
- 下一可见检查点：`F10` 稍后
- 当前阻塞：无
- 规则版本：abc123

| ID | 状态 / 负责人 | 证据 / 下一步 |
| --- | --- | --- |
| F1 | `READY` / 主 Agent | 执行 F1 |
"""

        errors = ledger_consistency_guard.validate_ledger(text)

        self.assertIn("current Goal must reference at least one open task ID", errors)

    def test_ledger_consistency_rejects_duplicated_runtime_capacity_pointer(self) -> None:
        text = """# Ledger

- 当前 Goal：`F1` 完成登录闭环
- 下一可见检查点：`F1` 真实浏览器通过
- 当前阻塞：无
- 规则版本：abc123
- 容量 / READY：1/4，三个空槽

| ID | 状态 / 负责人 | 证据 / 下一步 |
| --- | --- | --- |
| F1 | `ACTIVE` / Agent A | 完成登录恢复 Case |
"""

        errors = ledger_consistency_guard.validate_ledger(text)

        self.assertTrue(any("runtime capacity" in error for error in errors))

    def test_task_parser_ignores_status_words_in_evidence_and_non_task_tables(self) -> None:
        text = """# Ledger

| ID | 状态 / owner | 证据 / 下一步 |
| --- | --- | --- |
| F1 | `VERIFY` | 旧收据写 READY，但本项仍待复验 |

| 证据 | 结论 |
| --- | --- |
| screenshot | READY FOR EARLY |
"""

        self.assertEqual(lint_governance.task_rows(text), [("F1", "VERIFY")])

    def test_task_parser_normalizes_code_span_id_with_summary_label(self) -> None:
        text = """| 功能组 | 汇总状态 |
| --- | --- |
| `M2` 微信小程序 | `READY` |
"""

        self.assertEqual(lint_governance.task_rows(text), [("M2", "READY")])

    def test_task_record_prefers_next_step_over_evidence_column(self) -> None:
        text = """| ID | 状态 / owner | 固定边界与当前证据 | 依赖、阻塞与下一步 |
| --- | --- | --- | --- |
| F1 | `RECOVERING` / Agent A | 旧候选失败 | 修复后在 checkpoint 复审 |
"""

        self.assertEqual(
            lint_governance.task_records(text)[0]["next_action"],
            "修复后在 checkpoint 复审",
        )

    def test_legacy_project_status_template_passes_strict_lint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = (
                SKILL_ROOT / "assets" / "templates" / "PROJECT_STATUS.md"
            ).read_text(encoding="utf-8")
            (root / "PROJECT_STATUS.md").write_text(template, encoding="utf-8")

            errors, warnings = lint_governance.lint_project(root, strict=True)

            self.assertEqual(errors, [])
            self.assertEqual(warnings, [])

    def test_linter_rejects_duplicate_task_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "TASK_LEDGER.md").write_text(
                """# Ledger

- 当前活动项：无
- 协调下一动作：选择 F1

| ID | 状态 |
| --- | --- |
| F1 | `READY` |

| ID | 状态 |
| --- | --- |
| F1 | `DONE` |
""",
                encoding="utf-8",
            )

            errors, _ = lint_governance.lint_project(root, strict=True)

            self.assertTrue(any("repeats task IDs" in error for error in errors))

    def test_linter_warns_before_strict_duplicate_id_migration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "TASK_LEDGER.md").write_text(
                """# Ledger

- 当前活动项：无

| ID | 状态 |
| --- | --- |
| F1 | `READY` |

| ID | 状态 |
| --- | --- |
| F1 | `DONE` |
""",
                encoding="utf-8",
            )

            errors, warnings = lint_governance.lint_project(root)

            self.assertEqual(errors, [])
            self.assertTrue(any("repeats task IDs" in warning for warning in warnings))


if __name__ == "__main__":
    unittest.main()
