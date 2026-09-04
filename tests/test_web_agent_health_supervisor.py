import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
            event_source_probe=lambda: True,
        )

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
        from scripts.web_agent_execution import start_web_assignment
        from scripts.web_agent_health_supervisor import reconcile_web_agent_health_once
        start_web_assignment(
            repo=self.repo, registry_path=self.registry, controller_id="controller-1",
            conversation_id="child-stale", assignment=self.assignment(), now=T0,
            watchdog_launcher=lambda **_: {"launched": True},
            event_source_probe=lambda: True,
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
