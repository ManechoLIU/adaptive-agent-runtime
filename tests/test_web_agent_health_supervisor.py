import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from scripts.assignment_runtime import load_runtime_state

UTC = timezone.utc
T0 = datetime(2026, 9, 4, 2, 0, tzinfo=UTC)


class WebAgentHealthSupervisorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.repo = root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)], check=True)
        (self.repo / "README.md").write_text("runtime\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "README.md"], check=True)
        subprocess.run([
            "git", "-C", str(self.repo), "-c", "user.name=Test",
            "-c", "user.email=test@example.com", "commit", "-qm", "init"
        ], check=True)
        self.head = subprocess.check_output(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True
        ).strip()
        self.registry = root / "controllers.json"
        self.registry.write_text(
            json.dumps({"controller-1": str(self.repo.resolve())}), encoding="utf-8"
        )
        self.events = root / "controller-1.jsonl"
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

    def tearDown(self):
        self.tmp.cleanup()

    def assignment(self, **extra):
        value = {
            "assignment_id": "A-WEB",
            "task_id": "T-WEB",
            "agent_id": "web-writer",
            "provider": "chatgpt_web",
            "model": "gpt-5.6-sol",
            "agent_type": "default",
            "worktree": str(self.repo),
            "primary_goal": "finish bounded Web task",
            "success_criteria": ["candidate produced"],
            "owned_scope": ["README.md"],
            "strategy": "chatgpt-web:gpt-5.6-sol",
            "assignment_contract_version": 2,
            "side_effect": False,
            "progress_deadline_minutes": 10,
            "role": "writer",
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
        value.update(extra)
        return value

    def write_spawn_and_terminal(
        self, *, task_name="writer-task", child="child-1", kind="completed",
        call_id="spawn-1", started_at=T0 + timedelta(seconds=2),
    ):
        records = [
            {
                "timestamp": T0.isoformat(),
                "type": "response_item",
                "payload": {
                    "type": "function_call", "namespace": "collaboration", "name": "spawn_agent",
                    "call_id": call_id,
                    "arguments": json.dumps({
                        "task_name": task_name, "agent_type": "default",
                        "model": "gpt-5.6-sol", "fork_turns": "none"
                    }),
                },
            },
            {
                "timestamp": started_at.isoformat(),
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "SubAgentActivity", "kind": "started", "id": call_id,
                        "agent_thread_id": child, "agent_path": f"/root/{task_name}",
                    },
                },
            },
            {
                "timestamp": (started_at + timedelta(minutes=1)).isoformat(),
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "SubAgentActivity", "kind": kind, "id": f"terminal-{call_id}",
                        "agent_thread_id": child, "agent_path": f"/root/{task_name}",
                    },
                },
            },
        ]
        self.events.write_text(
            "\n".join(json.dumps(item) for item in records) + "\n", encoding="utf-8"
        )

    def prepare(self, *, assignment=None, task_name="writer-task"):
        from scripts.web_agent_execution import prepare_web_assignment_dispatch
        return prepare_web_assignment_dispatch(
            repo=self.repo, registry_path=self.registry, controller_id="controller-1",
            task_name=task_name, assignment=assignment or self.assignment(), now=T0 + timedelta(seconds=1),
            health_probe=lambda: True,
        )

    def test_model_end_turn_with_pending_controller_debt_arms_existing_same_controller_supervisor(self):
        from scripts import web_lifecycle_bridge
        from scripts.web_agent_health_supervisor import reconcile_web_agent_health_once

        projection = {
            "controller_id": "controller-1",
            "controller_host": "web",
            "should_continue": True,
            "requires_user": False,
            "debt_ids": ["known_next_action:abc"],
            "lifecycle_state": {
                "pending_control_event": True,
                "requires_user": False,
                "controller_host": "web",
                "wake_generation": 7,
                "triggers": ["KNOWN_NEXT_ACTION_NOT_EXECUTED"],
            },
        }
        with (
            patch.object(
                web_lifecycle_bridge,
                "controller_continuation_projection",
                return_value=projection,
                create=True,
            ) as projected,
            patch.object(
                web_lifecycle_bridge,
                "ensure_continuation_supervisor",
                return_value=True,
            ) as ensure,
        ):
            result = reconcile_web_agent_health_once(
                repo=self.repo,
                registry_path=self.registry,
                controller_id="controller-1",
                event_paths=[],
                now=T0 + timedelta(minutes=3),
                event_source_probe=lambda: True,
            )

        projected.assert_called_once()
        ensure.assert_called_once()
        self.assertEqual(result["controller_continuation"]["controller_id"], "controller-1")
        self.assertTrue(result["controller_continuation"]["should_continue"])
        self.assertTrue(result["controller_continuation"]["supervisor_armed"])

    def test_observation_only_or_legitimate_block_does_not_create_continuation(self):
        from scripts import web_lifecycle_bridge
        from scripts.web_agent_health_supervisor import reconcile_web_agent_health_once

        for label, projection in {
            "observation_only": {
                "controller_id": "controller-1",
                "controller_host": "web",
                "should_continue": False,
                "requires_user": False,
                "debt_ids": [],
                "lifecycle_state": {
                    "pending_control_event": False,
                    "requires_user": False,
                    "controller_host": "web",
                    "observation_query_only": True,
                },
            },
            "blocked": {
                "controller_id": "controller-1",
                "controller_host": "web",
                "should_continue": False,
                "requires_user": True,
                "debt_ids": [],
                "lifecycle_state": {
                    "pending_control_event": True,
                    "requires_user": True,
                    "controller_host": "web",
                },
            },
        }.items():
            with self.subTest(label=label):
                with (
                    patch.object(
                        web_lifecycle_bridge,
                        "controller_continuation_projection",
                        return_value=projection,
                        create=True,
                    ),
                    patch.object(
                        web_lifecycle_bridge,
                        "ensure_continuation_supervisor",
                        return_value=True,
                    ) as ensure,
                ):
                    result = reconcile_web_agent_health_once(
                        repo=self.repo,
                        registry_path=self.registry,
                        controller_id="controller-1",
                        event_paths=[],
                        now=T0 + timedelta(minutes=3),
                        event_source_probe=lambda: True,
                    )
                ensure.assert_not_called()
                self.assertFalse(result["controller_continuation"]["should_continue"])
                self.assertFalse(result["controller_continuation"]["supervisor_armed"])

    def test_canonical_runnable_reopens_continuation_without_user_message(self):
        from scripts import control_event_guard, lifecycle_hook, web_lifecycle_bridge

        state_file = Path(self.tmp.name) / "controller-state.json"
        state_file.write_text(json.dumps({
            "pending_control_event": False,
            "requires_user": False,
            "controller_host": "web",
            "wake_generation": 4,
            "triggers": [],
        }), encoding="utf-8")
        ledger = self.repo / "TASK_LEDGER.md"
        ledger.write_text("# ledger\n", encoding="utf-8")

        with (
            patch.object(lifecycle_hook, "state_path", return_value=state_file),
            patch.object(
                control_event_guard,
                "project_wide_dispatch_projection",
                return_value={
                    "derived_runnable_ids": {"MINI-READY"},
                    "work_in_flight": {},
                    "task_states": {"MINI-READY": "READY"},
                },
            ),
            patch.object(control_event_guard, "unmerged_worktree_candidates", return_value={}),
            patch.object(control_event_guard, "open_controller_corrections", return_value=[]),
            patch.object(control_event_guard, "canonical_controller_action_projection", return_value={}),
        ):
            projection = web_lifecycle_bridge.controller_continuation_projection(
                repo=self.repo,
                controller_id="controller-1",
                registry=self.registry,
            )

        persisted = json.loads(state_file.read_text(encoding="utf-8"))
        self.assertTrue(projection["should_continue"])
        self.assertEqual(projection["runnable_ids"], ["MINI-READY"])
        self.assertEqual(projection["runnable_count"], 1)
        self.assertEqual(projection["active_assignment_count"], 0)
        self.assertIn("runnable:MINI-READY", projection["debt_ids"])
        self.assertTrue(persisted["pending_control_event"])
        self.assertFalse(persisted["requires_user"])
        self.assertEqual(persisted["wake_generation"], 5)

    def test_no_canonical_work_does_not_reopen_after_observation_only_turn(self):
        from scripts import control_event_guard, lifecycle_hook, web_lifecycle_bridge

        state_file = Path(self.tmp.name) / "controller-state-idle.json"
        state_file.write_text(json.dumps({
            "pending_control_event": False,
            "requires_user": False,
            "controller_host": "web",
            "wake_generation": 4,
            "observation_query_only": True,
            "triggers": [],
        }), encoding="utf-8")
        ledger = self.repo / "TASK_LEDGER.md"
        ledger.write_text("# ledger\n", encoding="utf-8")

        with (
            patch.object(lifecycle_hook, "state_path", return_value=state_file),
            patch.object(
                control_event_guard,
                "project_wide_dispatch_projection",
                return_value={
                    "derived_runnable_ids": set(),
                    "work_in_flight": {},
                    "task_states": {},
                },
            ),
            patch.object(control_event_guard, "unmerged_worktree_candidates", return_value={}),
            patch.object(control_event_guard, "open_controller_corrections", return_value=[]),
            patch.object(control_event_guard, "canonical_controller_action_projection", return_value={}),
        ):
            projection = web_lifecycle_bridge.controller_continuation_projection(
                repo=self.repo,
                controller_id="controller-1",
                registry=self.registry,
            )

        persisted = json.loads(state_file.read_text(encoding="utf-8"))
        self.assertFalse(projection["should_continue"])
        self.assertEqual(projection["debt_ids"], [])
        self.assertFalse(persisted["pending_control_event"])
        self.assertTrue(persisted["observation_query_only"])

    def test_health_tick_with_runnable_and_no_child_event_arms_same_controller_without_user_message(self):
        from scripts import control_event_guard, lifecycle_hook, web_lifecycle_bridge
        from scripts.web_agent_health_supervisor import reconcile_web_agent_health_once

        state_file = Path(self.tmp.name) / "controller-health-tick.json"
        state_file.write_text(json.dumps({
            "pending_control_event": False,
            "requires_user": False,
            "controller_host": "web",
            "wake_generation": 11,
            "triggers": [],
        }), encoding="utf-8")
        ledger = self.repo / "TASK_LEDGER.md"
        ledger.write_text("# ledger\n", encoding="utf-8")

        with (
            patch.object(lifecycle_hook, "state_path", return_value=state_file),
            patch.object(
                control_event_guard,
                "project_wide_dispatch_projection",
                return_value={
                    "derived_runnable_ids": {"SERVER-READY"},
                    "work_in_flight": {},
                    "task_states": {"SERVER-READY": "READY"},
                },
            ),
            patch.object(control_event_guard, "unmerged_worktree_candidates", return_value={}),
            patch.object(control_event_guard, "open_controller_corrections", return_value=[]),
            patch.object(control_event_guard, "canonical_controller_action_projection", return_value={}),
            patch.object(web_lifecycle_bridge, "ensure_continuation_supervisor", return_value=True) as ensure,
        ):
            result = reconcile_web_agent_health_once(
                repo=self.repo,
                registry_path=self.registry,
                controller_id="controller-1",
                event_paths=[],
                now=T0 + timedelta(minutes=3),
                event_source_probe=lambda: True,
            )

        ensure.assert_called_once()
        call = ensure.call_args.kwargs
        self.assertEqual(call["session_id"], "controller-1")
        self.assertTrue(call["lifecycle_state"]["pending_control_event"])
        self.assertEqual(result["controller_continuation"]["runnable_ids"], ["SERVER-READY"])
        self.assertTrue(result["controller_continuation"]["supervisor_armed"])

    def test_completed_writer_becomes_canonical_terminal_and_handoffs_same_controller(self):
        from scripts.web_agent_health_supervisor import reconcile_web_agent_health_once
        self.prepare()
        self.write_spawn_and_terminal()
        calls = []
        def consume(**kwargs):
            calls.append(kwargs)
            return {
                "controller_id": "controller-1", "pending_control_event": True,
                "wake_result": {"result": "DEFERRED"}, "supervisor_armed": True,
            }
        result = reconcile_web_agent_health_once(
            repo=self.repo, registry_path=self.registry, controller_id="controller-1",
            event_paths=[self.events], now=T0 + timedelta(minutes=2),
            terminal_consumer=consume,
            event_source_probe=lambda: True,
        )
        lease = load_runtime_state(self.repo)["leases"]["A-WEB"]
        self.assertEqual(lease["terminal_state"], "completed")
        self.assertEqual(lease["session_id"], "child-1")
        self.assertEqual(result["controller_id"], "controller-1")
        self.assertEqual(result["terminal_handoffs"][0]["controller_id"], "controller-1")
        self.assertEqual(len(calls), 1)

    def test_completed_reviewer_uses_same_terminal_continuation_path(self):
        from scripts.web_agent_health_supervisor import reconcile_web_agent_health_once
        assignment = self.assignment(
            role="reviewer", agent_id="web-reviewer", candidate_revision=self.head,
        )
        self.prepare(assignment=assignment, task_name="review-task")
        self.write_spawn_and_terminal(task_name="review-task", child="review-child")
        calls = []
        result = reconcile_web_agent_health_once(
            repo=self.repo, registry_path=self.registry, controller_id="controller-1",
            event_paths=[self.events], now=T0 + timedelta(minutes=2),
            event_source_probe=lambda: True,
            terminal_consumer=lambda **kwargs: calls.append(kwargs) or {
                "controller_id": "controller-1", "pending_control_event": True,
                "wake_result": {"result": "CONFIRMED"}, "supervisor_armed": False,
            },
        )
        lease = load_runtime_state(self.repo)["leases"]["A-WEB"]
        self.assertEqual(lease["execution_role"], "reviewer")
        self.assertEqual(lease["candidate_revision"], self.head)
        self.assertEqual(lease["terminal_state"], "completed")
        self.assertEqual(result["terminal_handoffs"][0]["controller_id"], "controller-1")
        self.assertEqual(len(calls), 1)

    def test_failed_cancelled_and_disconnected_all_handoff_same_controller(self):
        from scripts.web_agent_health_supervisor import reconcile_web_agent_health_once

        expectations = {
            "failed": "failed",
            "cancelled": "cancelled",
            "disconnected": "disconnected",
        }
        for index, (kind, expected_terminal) in enumerate(expectations.items(), start=1):
            with self.subTest(kind=kind):
                # Each subcase needs a distinct Assignment/session so immutable terminal state
                # from the previous case cannot hide a routing/continuation defect.
                assignment = self.assignment(
                    assignment_id=f"A-WEB-{index}",
                    task_id=f"T-WEB-{index}",
                    agent_id=f"web-writer-{index}",
                )
                task_name = f"writer-task-{index}"
                self.prepare(assignment=assignment, task_name=task_name)
                child = f"child-{index}"
                self.write_spawn_and_terminal(
                    task_name=task_name,
                    child=child,
                    kind=kind,
                    call_id=f"spawn-{index}",
                    started_at=T0 + timedelta(seconds=2 + index),
                )
                calls = []
                result = reconcile_web_agent_health_once(
                    repo=self.repo,
                    registry_path=self.registry,
                    controller_id="controller-1",
                    event_paths=[self.events],
                    now=T0 + timedelta(minutes=2),
                    terminal_consumer=lambda **kwargs: calls.append(kwargs) or {
                        "controller_id": "controller-1",
                        "pending_control_event": True,
                        "wake_result": {"result": "CONFIRMED"},
                        "supervisor_armed": False,
                    },
                    event_source_probe=lambda: True,
                )
                lease = load_runtime_state(self.repo)["leases"][assignment["assignment_id"]]
                self.assertEqual(lease["terminal_state"], expected_terminal)
                self.assertEqual(result["terminal_handoffs"][0]["controller_id"], "controller-1")
                self.assertEqual(len(calls), 1)

    def test_stale_lease_only_handoffs_to_existing_runtime_continuation(self):
        from scripts.web_agent_execution import bind_web_assignment_dispatch
        from scripts.web_agent_health_supervisor import reconcile_web_agent_health_once
        prepared = self.prepare(task_name="stale-writer")
        call_id = "spawn-stale-writer"
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
                            "task_name": "stale-writer",
                            "agent_type": "default",
                            "model": "gpt-5.6-sol",
                        }),
                    },
                }),
                json.dumps({
                    "timestamp": (T0 + timedelta(seconds=2)).isoformat(),
                    "type": "event_msg",
                    "payload": {
                        "type": "item_completed",
                        "item": {
                            "type": "SubAgentActivity",
                            "kind": "started",
                            "id": call_id,
                            "agent_thread_id": "child-stale",
                            "agent_path": "/root/stale-writer",
                        },
                    },
                }),
            ]) + chr(10),
            encoding="utf-8",
        )
        bind_web_assignment_dispatch(
            repo=self.repo,
            registry_path=self.registry,
            controller_id="controller-1",
            dispatch_id=prepared["dispatch_id"],
            event_paths=[self.events],
            now=T0 + timedelta(seconds=2),
            health_probe=lambda: True,
            watchdog_launcher=lambda **_: {"launched": False},
        )
        wakes = []
        result = reconcile_web_agent_health_once(
            repo=self.repo, registry_path=self.registry, controller_id="controller-1",
            event_paths=[], now=T0 + timedelta(minutes=26),
            event_source_probe=lambda: True,
            runtime_change_consumer=lambda **kwargs: wakes.append(kwargs) or {
                "controller_id": "controller-1", "pending_control_event": True,
                "wake_result": {"result": "DEFERRED"}, "supervisor_armed": True,
            },
        )
        self.assertEqual(result["health"][0]["runtime_state"], "unhealthy")
        self.assertEqual(result["health"][0]["controller_id"], "controller-1")
        self.assertEqual(len(wakes), 1)

    def test_duplicate_terminal_observation_does_not_repeat_continuation_handoff(self):
        from scripts.web_agent_health_supervisor import reconcile_web_agent_health_once
        self.prepare()
        self.write_spawn_and_terminal()
        calls = []
        consume = lambda **kwargs: calls.append(kwargs) or {
            "controller_id": "controller-1", "pending_control_event": True,
            "wake_result": {"result": "DEFERRED"}, "supervisor_armed": True,
        }
        first = reconcile_web_agent_health_once(
            repo=self.repo, registry_path=self.registry, controller_id="controller-1",
            event_paths=[self.events], now=T0 + timedelta(minutes=2), terminal_consumer=consume,
            event_source_probe=lambda: True,
        )
        second = reconcile_web_agent_health_once(
            repo=self.repo, registry_path=self.registry, controller_id="controller-1",
            event_paths=[self.events], now=T0 + timedelta(minutes=3), terminal_consumer=consume,
            event_source_probe=lambda: True,
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(first["terminal_handoffs"]), 1)
        self.assertEqual(second["terminal_handoffs"], [])
        self.assertEqual(load_runtime_state(self.repo)["leases"]["A-WEB"]["attempt"], 1)


if __name__ == "__main__":
    unittest.main()
