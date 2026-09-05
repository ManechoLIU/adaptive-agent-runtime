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
from scripts.web_agent_execution import prepare_web_assignment_dispatch, bind_web_assignment_dispatch

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
        self.policy = root / "AGENTS.md"
        self.policy.write_text(
            "general 默认 provider=chatgpt_web、model=gpt-5.6-sol、auth_mode=host。\n",
            encoding="utf-8",
        )
        self._machine_source_patcher = patch(
            "scripts.web_agent_execution._machine_event_source_context",
            return_value={
                "ready": True,
                "reason": "test_host_attested",
                "event_paths": [str(self.events.resolve())],
            },
        )
        self._machine_source_patcher.start()
        self.addCleanup(self._machine_source_patcher.stop)
        self._watchdog_launcher_patcher = patch(
            "scripts.web_agent_execution._default_watchdog_launcher",
            return_value={"launched": True, "pid": 4242, "log_path": "/tmp/test-watchdog.log"},
        )
        self._watchdog_launcher_patcher.start()
        self.addCleanup(self._watchdog_launcher_patcher.stop)
        self._continuation_supervisor_patcher = patch(
            "scripts.web_lifecycle_bridge.ensure_continuation_supervisor",
            return_value=False,
        )
        self._continuation_supervisor_patcher.start()
        self.addCleanup(self._continuation_supervisor_patcher.stop)

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
            "route": {
                "decision": "default",
                "policy_class": "general",
                "provider": "chatgpt_web",
                "model": "gpt-5.6-sol",
                "auth_mode": "host",
                "policy_source": {
                    "path": str(self.policy.resolve()),
                    "sha256": __import__("hashlib").sha256(self.policy.read_bytes()).hexdigest(),
                },
            },
        }
        if role == "reviewer":
            value["candidate_revision"] = self.head
        return value

    def start(self, *, role="writer"):
        assignment = self.assignment(role=role)
        task_name = f"{role}-start"
        prepared = prepare_web_assignment_dispatch(
            repo=self.repo,
            registry_path=self.registry,
            controller_id="controller-1",
            task_name=task_name,
            assignment=assignment,
            now=T0,
            health_probe=lambda: True,
        )
        call_id = f"spawn-{role}-start"
        self.events.write_text(
            chr(10).join([
                json.dumps({
                    "timestamp": T0.isoformat(),
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "namespace": "collaboration",
                        "name": "spawn_agent",
                        "call_id": call_id,
                        "arguments": json.dumps({
                            "task_name": task_name,
                            "agent_type": assignment["agent_type"],
                            "model": assignment["model"],
                        }),
                    },
                }),
                json.dumps({
                    "timestamp": (T0 + timedelta(seconds=1)).isoformat(),
                    "type": "event_msg",
                    "payload": {
                        "type": "item_completed",
                        "item": {
                            "type": "SubAgentActivity",
                            "kind": "started",
                            "id": call_id,
                            "agent_thread_id": "child-thread-1",
                            "agent_path": f"/root/{role}-child",
                        },
                    },
                }),
            ]) + chr(10),
            encoding="utf-8",
        )
        return bind_web_assignment_dispatch(
            repo=self.repo,
            registry_path=self.registry,
            controller_id="controller-1",
            dispatch_id=prepared["dispatch_id"],
            event_paths=[self.events],
            now=T0 + timedelta(seconds=1),
            health_probe=lambda: True,
            watchdog_launcher=lambda **_: {"launched": False},
        )

    def test_reconcile_binds_pending_session_owned_dispatch_without_controller_impersonation(self):
        assignment = self.assignment()
        prepared = prepare_web_assignment_dispatch(
            repo=self.repo,
            registry_path=self.registry,
            controller_id=None,
            delegator_session_id="ordinary-web-session-1",
            task_name="session-owned-start",
            assignment=assignment,
            now=T0,
            health_probe=lambda: True,
        )
        self.events.write_text(
            chr(10).join([
                json.dumps({
                    "timestamp": T0.isoformat(),
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "namespace": "collaboration",
                        "name": "spawn_agent",
                        "call_id": "spawn-session-owned",
                        "arguments": json.dumps({
                            "task_name": "session-owned-start",
                            "agent_type": assignment["agent_type"],
                            "model": assignment["model"],
                        }),
                    },
                }),
                json.dumps({
                    "timestamp": (T0 + timedelta(seconds=1)).isoformat(),
                    "type": "event_msg",
                    "payload": {
                        "type": "item_completed",
                        "item": {
                            "type": "SubAgentActivity",
                            "kind": "started",
                            "id": "spawn-session-owned",
                            "agent_thread_id": "session-owned-child",
                            "agent_path": "/root/session-owned-child",
                        },
                    },
                }),
            ]) + chr(10),
            encoding="utf-8",
        )

        result = web_bridge.reconcile_managed_web_assignments(
            repo=self.repo,
            controller_id="controller-1",
            registry=self.registry,
            event_paths=[self.events],
            now=T0 + timedelta(seconds=2),
            event_source_probe=lambda: True,
        )

        self.assertEqual(result["binding_errors"], [])
        self.assertEqual(len(result["bound_dispatches"]), 1)
        bound = result["bound_dispatches"][0]
        self.assertEqual(bound["dispatch_id"], prepared["dispatch_id"])
        self.assertEqual(bound["delegation_owner_kind"], "session")
        self.assertEqual(bound["delegation_owner_id"], "ordinary-web-session-1")
        self.assertIsNone(bound["controller_id"])
        lease = load_runtime_state(self.repo)["leases"]["A-WEB"]
        self.assertEqual(lease["delegation_owner_kind"], "session")
        self.assertEqual(lease["delegation_owner_id"], "ordinary-web-session-1")

    def test_reconcile_session_owned_terminal_does_not_wake_logical_controller(self):
        assignment = self.assignment()
        prepare_web_assignment_dispatch(
            repo=self.repo,
            registry_path=self.registry,
            controller_id=None,
            delegator_session_id="ordinary-web-session-1",
            task_name="session-owned-terminal",
            assignment=assignment,
            now=T0,
            health_probe=lambda: True,
        )
        self.events.write_text(
            chr(10).join([
                json.dumps({
                    "timestamp": T0.isoformat(),
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "namespace": "collaboration",
                        "name": "spawn_agent",
                        "call_id": "spawn-session-terminal",
                        "arguments": json.dumps({
                            "task_name": "session-owned-terminal",
                            "agent_type": assignment["agent_type"],
                            "model": assignment["model"],
                        }),
                    },
                }),
                json.dumps({
                    "timestamp": (T0 + timedelta(seconds=1)).isoformat(),
                    "type": "event_msg",
                    "payload": {
                        "type": "item_completed",
                        "item": {
                            "type": "SubAgentActivity",
                            "kind": "started",
                            "id": "spawn-session-terminal",
                            "agent_thread_id": "session-owned-terminal-child",
                            "agent_path": "/root/session-owned-terminal-child",
                        },
                    },
                }),
            ]) + chr(10),
            encoding="utf-8",
        )
        web_bridge.reconcile_managed_web_assignments(
            repo=self.repo, controller_id="controller-1", registry=self.registry,
            event_paths=[self.events], now=T0 + timedelta(seconds=2), event_source_probe=lambda: True,
        )
        self.events.write_text(json.dumps({
            "timestamp": (T0 + timedelta(minutes=2)).isoformat(),
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "item": {
                    "type": "SubAgentActivity",
                    "kind": "completed",
                    "id": "terminal-session-owned",
                    "agent_thread_id": "session-owned-terminal-child",
                    "agent_path": "/root/session-owned-terminal-child",
                },
            },
        }) + "\n", encoding="utf-8")
        controller_wakes = []
        result = web_bridge.reconcile_managed_web_assignments(
            repo=self.repo, controller_id="controller-1", registry=self.registry,
            event_paths=[self.events], now=T0 + timedelta(minutes=3),
            terminal_consumer=lambda **kwargs: controller_wakes.append(kwargs) or {"pending_control_event": False},
            event_source_probe=lambda: True,
        )

        self.assertEqual(controller_wakes, [])
        self.assertEqual(result["terminal_continuations"][0]["wake_state"], "session_parent")
        self.assertEqual(
            result["terminal_continuations"][0]["delegation_parent_session_id"],
            "ordinary-web-session-1",
        )
        self.assertIsNone(result["terminal_continuations"][0]["controller_id"])

        duplicate = web_bridge.reconcile_managed_web_assignments(
            repo=self.repo, controller_id="controller-1", registry=self.registry,
            event_paths=[self.events], now=T0 + timedelta(minutes=4),
            terminal_consumer=lambda **kwargs: controller_wakes.append(kwargs) or {"pending_control_event": False},
            event_source_probe=lambda: True,
        )
        self.assertEqual(controller_wakes, [])
        self.assertEqual(duplicate["terminal_continuations"], [])

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
