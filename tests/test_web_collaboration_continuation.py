from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from scripts.assignment_runtime import load_runtime_state
from scripts import terminal_continuation
from scripts import web_lifecycle_bridge as web_bridge
from scripts.web_agent_execution import start_web_assignment

UTC = timezone.utc
T0 = datetime(2026, 9, 4, 2, 0, tzinfo=UTC)


class WebCollaborationContinuationRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.repo = root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)], check=True)
        (self.repo / "README.md").write_text("runtime\n", encoding="utf-8")
        (self.repo / "TASK_LEDGER.md").write_text(
            """# Ledger

- 当前 Goal：T-WEB complete writer
- 下一可见检查点：T-NEXT dispatch
- 当前阻塞：none
- 规则版本：test

| ID | 状态 | 负责人 | 下一步 |
|---|---|---|---|
| T-WEB | ACTIVE | web-writer | verify child result |
| T-NEXT | READY | 待分配 | dispatch |
""",
            encoding="utf-8",
        )
        subprocess.run(["git", "-C", str(self.repo), "add", "README.md", "TASK_LEDGER.md"], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "init"],
            check=True,
        )
        self.head = subprocess.check_output(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True
        ).strip()
        self.registry = root / "controllers.json"
        self.registry.write_text(
            json.dumps({"controller-1": str(self.repo.resolve())}), encoding="utf-8"
        )
        self.events = root / "controller.jsonl"
        self.lifecycle_state = root / "lifecycle-controller-1.json"

    def tearDown(self):
        self.tmp.cleanup()

    def assignment(self, *, role="writer"):
        value = {
            "assignment_id": "A-WEB",
            "task_id": "T-WEB",
            "agent_id": "web-reviewer" if role == "reviewer" else "web-writer",
            "provider": "chatgpt_web",
            "model": "gpt-5.6-sol",
            "agent_type": "default",
            "worktree": str(self.repo),
            "primary_goal": "finish bounded child task",
            "success_criteria": ["child reaches terminal"],
            "owned_scope": ["README.md"],
            "strategy": "collaboration:gpt-5.6-sol",
            "assignment_contract_version": 2,
            "side_effect": False,
            "progress_deadline_minutes": 10,
            "role": role,
        }
        if role == "reviewer":
            value["candidate_revision"] = self.head
        return value

    def start(self, *, role="writer"):
        return start_web_assignment(
            repo=self.repo,
            registry_path=self.registry,
            controller_id="controller-1",
            conversation_id="child-thread-1",
            assignment=self.assignment(role=role),
            now=T0,
            watchdog_launcher=lambda **_: {"launched": True, "pid": 101},
        )

    def terminal_event(self, kind="completed", observation_id="terminal-1"):
        self.events.write_text(
            json.dumps({
                "timestamp": (T0 + timedelta(minutes=2)).isoformat(),
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "SubAgentActivity",
                        "kind": kind,
                        "id": observation_id,
                        "agent_thread_id": "child-thread-1",
                        "agent_path": "/root/web-child",
                    },
                },
            }) + "\n",
            encoding="utf-8",
        )

    def test_regression_parent_already_yielded_then_writer_completed_wakes_same_controller_with_next_runnable(self):
        self.start()
        self.terminal_event("completed")
        self.lifecycle_state.write_text(json.dumps({
            "session_id": "controller-1",
            "source_session_id": "controller-1",
            "controller_host": "web",
            "must_yield": True,
            "pending_control_event": False,
            "triggers": [],
            "wake_generation": 7,
            "snapshot": {},
        }), encoding="utf-8")
        dispatched = []

        def consume(**kwargs):
            return terminal_continuation.consume_terminal_receipt(
                **kwargs,
                wake_dispatcher=lambda **wake_kwargs: dispatched.append(wake_kwargs) or {
                    "result": "CONFIRMED",
                    "controller_id": "controller-1",
                },
            )

        scripts_dir = str(Path(__file__).resolve().parents[1] / "scripts")
        inserted = scripts_dir not in sys.path
        if inserted:
            sys.path.insert(0, scripts_dir)
        try:
            with patch.object(
                terminal_continuation.lifecycle, "state_path", return_value=self.lifecycle_state
            ):
                result = web_bridge.reconcile_managed_web_assignments(
                    repo=self.repo,
                    controller_id="controller-1",
                    registry=self.registry,
                    event_paths=[self.events],
                    now=T0 + timedelta(minutes=3),
                    terminal_consumer=consume,
                )
        finally:
            if inserted:
                sys.path.remove(scripts_dir)

        lease = load_runtime_state(self.repo)["leases"]["A-WEB"]
        self.assertEqual(lease["terminal_state"], "completed")
        self.assertEqual(result["terminal_continuations"][0]["wake_state"], "confirmed")
        self.assertEqual(len(dispatched), 1)
        self.assertEqual(dispatched[0]["session_id"], "controller-1")
        wake_state = dispatched[0]["lifecycle_state"]
        self.assertIn("agent_session_terminal:T-WEB", wake_state["triggers"])
        self.assertIn("READY:T-NEXT", wake_state["triggers"])
        self.assertTrue(wake_state["pending_control_event"])

    def test_completed_reviewer_uses_same_terminal_continuation_path(self):
        self.start(role="reviewer")
        self.terminal_event("completed")
        calls = []
        result = web_bridge.reconcile_managed_web_assignments(
            repo=self.repo,
            controller_id="controller-1",
            registry=self.registry,
            event_paths=[self.events],
            now=T0 + timedelta(minutes=3),
            terminal_consumer=lambda **kwargs: calls.append(kwargs) or {
                "controller_id": "controller-1",
                "pending_control_event": True,
                "wake_result": {"result": "CONFIRMED"},
            },
        )
        lease = load_runtime_state(self.repo)["leases"]["A-WEB"]
        self.assertEqual(lease["execution_role"], "reviewer")
        self.assertEqual(lease["candidate_revision"], self.head)
        self.assertEqual(lease["terminal_state"], "completed")
        self.assertEqual(result["terminal_continuations"][0]["controller_id"], "controller-1")
        self.assertEqual(len(calls), 1)

    def test_stale_child_is_second_observed_by_existing_audit_and_wakes_same_controller(self):
        self.start()
        wakes = []
        result = web_bridge.reconcile_managed_web_assignments(
            repo=self.repo,
            controller_id="controller-1",
            registry=self.registry,
            event_paths=[],
            now=T0 + timedelta(minutes=26),
            runtime_change_consumer=lambda **kwargs: wakes.append(kwargs) or {
                "controller_id": "controller-1",
                "pending_control_event": True,
                "wake_result": {"result": "CONFIRMED"},
            },
        )
        self.assertEqual(result["health"][0]["runtime_state"], "unhealthy")
        self.assertEqual(result["health"][0]["wake_state"], "confirmed")
        self.assertEqual(len(wakes), 1)
        self.assertEqual(wakes[0]["repo"], self.repo.resolve())

    def test_duplicate_terminal_observation_after_confirmed_continuation_does_not_wake_twice(self):
        self.start()
        self.terminal_event("completed")
        calls = []

        def consume(**kwargs):
            calls.append(kwargs)
            return {
                "controller_id": "controller-1",
                "pending_control_event": True,
                "wake_result": {"result": "CONFIRMED"},
            }

        first = web_bridge.reconcile_managed_web_assignments(
            repo=self.repo, controller_id="controller-1", registry=self.registry,
            event_paths=[self.events], now=T0 + timedelta(minutes=3),
            terminal_consumer=consume,
        )
        self.terminal_event("completed", observation_id="terminal-refresh")
        second = web_bridge.reconcile_managed_web_assignments(
            repo=self.repo, controller_id="controller-1", registry=self.registry,
            event_paths=[self.events], now=T0 + timedelta(minutes=4),
            terminal_consumer=consume,
        )
        self.assertEqual(first["terminal_continuations"][0]["wake_state"], "confirmed")
        self.assertEqual(second["terminal_continuations"], [])
        self.assertEqual(len(calls), 1)
        self.assertEqual(load_runtime_state(self.repo)["leases"]["A-WEB"]["attempt"], 1)


if __name__ == "__main__":
    unittest.main()
