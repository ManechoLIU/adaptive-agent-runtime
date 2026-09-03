import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts.assignment_runtime import RuntimePolicy, evaluate_lease, load_runtime_state
from scripts.web_agent_execution import apply_web_execution_event


UTC = timezone.utc
T0 = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


class WebAgentExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.repo = root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)], check=True)
        (self.repo / "README.md").write_text("runtime\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "README.md"], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "init"],
            check=True,
        )
        self.head = subprocess.check_output(["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True).strip()
        self.registry = root / "controllers.json"
        self.registry.write_text(json.dumps({"controller-1": str(self.repo.resolve())}), encoding="utf-8")
        self.wakes = []
        self.runtime_wakes = []

    def tearDown(self):
        self.tmp.cleanup()

    def attestation(self, state, *, source="chatgpt_host_event", conversation="conv-1"):
        return {
            "kind": "web_execution_state",
            "source": source,
            "observation_id": f"obs:{state}:{conversation}",
            "conversation_id": conversation,
            "state": state,
        }

    def assignment(self, **extra):
        value = {
            "assignment_id": "A-1",
            "task_id": "T-1",
            "agent_id": "web-writer-1",
            "provider": "chatgpt_web",
            "worktree": str(self.repo),
            "primary_goal": "finish bounded runtime task",
            "success_criteria": ["tests pass"],
            "owned_scope": ["README.md"],
            "strategy": "chatgpt-web:gpt-5.6-sol",
            "assignment_contract_version": 2,
            "side_effect": False,
            "attempt": 1,
            "lease_id": "A-1:web:attempt:1",
            "role": "writer",
        }
        value.update(extra)
        return value

    def start(self, **assignment_extra):
        return apply_web_execution_event(
            repo=self.repo,
            registry_path=self.registry,
            event={
                "controller_id": "controller-1",
                "conversation_id": "conv-1",
                "state": "started",
                "assignment": self.assignment(**assignment_extra),
                "attestation": self.attestation("running"),
            },
            now=T0,
            continuation_consumer=self._consume, runtime_change_consumer=self._runtime_change,
        )

    def _runtime_change(self, **kwargs):
        self.runtime_wakes.append(kwargs)
        return {"controller_id": "controller-1", "pending_control_event": True, "wake_result": {"result": "CONFIRMED"}}

    def _consume(self, **kwargs):
        self.wakes.append(kwargs)
        return {"controller_id": "controller-1", "wake_result": {"result": "CONFIRMED"}}

    def event(self, state, *, at, conversation="conv-1", **extra):
        payload = {
            "controller_id": "controller-1",
            "conversation_id": conversation,
            "assignment_id": "A-1",
            "state": state,
            "attestation": self.attestation(state if state != "heartbeat" else "running", conversation=conversation),
        }
        payload.update(extra)
        return apply_web_execution_event(
            repo=self.repo, registry_path=self.registry, event=payload, now=at,
            continuation_consumer=self._consume, runtime_change_consumer=self._runtime_change,
        )

    def test_start_enters_canonical_runtime_and_is_healthy(self):
        result = self.start()
        lease = load_runtime_state(self.repo)["leases"]["A-1"]
        self.assertEqual(result["runtime_state"], "healthy")
        self.assertEqual(lease["session_id"], "conv-1")
        self.assertEqual(lease["execution_transport"], "web")
        self.assertEqual(lease["exclusive_execution_key"], "task:T-1")
        self.assertNotEqual(lease["session_id"], "controller-1")

    def test_heartbeat_proves_liveness_but_not_progress(self):
        self.start()
        before = load_runtime_state(self.repo)["leases"]["A-1"]["last_progress_at"]
        self.event("heartbeat", at=T0 + timedelta(minutes=10))
        lease = load_runtime_state(self.repo)["leases"]["A-1"]
        self.assertEqual(lease["last_progress_at"], before)
        self.assertEqual(evaluate_lease(lease, now=T0 + timedelta(minutes=10))["state"], "healthy")

    def test_connection_interrupted_becomes_terminal_and_wakes_same_controller(self):
        self.start()
        result = self.event("interrupted", at=T0 + timedelta(minutes=1), summary="Connection interrupted")
        lease = load_runtime_state(self.repo)["leases"]["A-1"]
        self.assertEqual(lease["terminal_state"], "disconnected")
        self.assertEqual(lease["retry_class"], "transport_error")
        self.assertEqual(result["controller_id"], "controller-1")
        self.assertEqual(len(self.wakes), 1)
        receipt = json.loads(Path(self.wakes[0]["receipt_path"]).read_text(encoding="utf-8"))
        self.assertEqual(receipt["event_type"], "external_agent_terminal")
        self.assertEqual(receipt["session_id"], "conv-1")

    def test_host_attested_session_missing_is_disconnected(self):
        self.start()
        self.event("missing", at=T0 + timedelta(minutes=1), summary="host reports execution missing")
        self.assertEqual(load_runtime_state(self.repo)["leases"]["A-1"]["terminal_state"], "disconnected")

    def test_browser_tab_absence_is_not_strong_enough_to_claim_disconnect(self):
        self.start()
        with self.assertRaisesRegex(ValueError, "host-attested terminal evidence"):
            apply_web_execution_event(
                repo=self.repo, registry_path=self.registry,
                event={
                    "controller_id": "controller-1", "conversation_id": "conv-1", "assignment_id": "A-1",
                    "state": "missing",
                    "attestation": self.attestation("missing", source="ai_bridge_browser_tab"),
                },
                now=T0 + timedelta(minutes=1), continuation_consumer=self._consume,
            )

    def test_progress_stale_remains_watchdog_owned_and_bounded(self):
        self.start(progress_deadline_minutes=10)
        self.event("heartbeat", at=T0 + timedelta(minutes=12))
        lease = load_runtime_state(self.repo)["leases"]["A-1"]
        self.assertEqual(evaluate_lease(lease, now=T0 + timedelta(minutes=12))["state"], "progress_stale")
        self.event("heartbeat", at=T0 + timedelta(minutes=26))
        lease = load_runtime_state(self.repo)["leases"]["A-1"]
        self.assertEqual(evaluate_lease(lease, now=T0 + timedelta(minutes=26))["reason"], "progress_stale_beyond_grace")
        self.assertEqual(len(self.runtime_wakes), 2)
        self.assertTrue(all(call["repo"] == self.repo.resolve() for call in self.runtime_wakes))

    def test_late_old_attempt_cannot_overwrite_recovery_attempt(self):
        self.start()
        self.event("interrupted", at=T0 + timedelta(minutes=1), summary="lost")
        apply_web_execution_event(
            repo=self.repo, registry_path=self.registry,
            event={
                "controller_id": "controller-1", "conversation_id": "conv-2", "state": "started",
                "assignment": self.assignment(attempt=2, lease_id="A-1:web:attempt:2"),
                "attestation": self.attestation("running", conversation="conv-2"),
            },
            now=T0 + timedelta(minutes=2), continuation_consumer=self._consume,
        )
        with self.assertRaisesRegex(ValueError, "stale runtime attempt|execution identity"):
            self.event(
                "completed", at=T0 + timedelta(minutes=3), conversation="conv-1", attempt=1,
                delivery_outcome="unresolved", summary="late result", evidence=[], artifacts=[],
            )

    def test_terminal_attempt_cannot_be_revived_by_heartbeat(self):
        self.start()
        self.event("interrupted", at=T0 + timedelta(minutes=1), summary="lost")
        with self.assertRaisesRegex(ValueError, "terminal attempt is immutable"):
            self.event("heartbeat", at=T0 + timedelta(minutes=2))

    def test_recovery_must_increment_attempt_and_change_lease(self):
        self.start()
        self.event("interrupted", at=T0 + timedelta(minutes=1), summary="lost")
        with self.assertRaisesRegex(ValueError, "recovery attempt must increment"):
            apply_web_execution_event(
                repo=self.repo, registry_path=self.registry,
                event={
                    "controller_id": "controller-1", "conversation_id": "conv-2", "state": "started",
                    "assignment": self.assignment(attempt=3, lease_id="A-1:web:attempt:3"),
                    "attestation": self.attestation("running", conversation="conv-2"),
                }, now=T0 + timedelta(minutes=2), continuation_consumer=self._consume,
            )

    def test_active_execution_blocks_duplicate_assignment_even_across_transport(self):
        self.start()
        with self.assertRaisesRegex(ValueError, "exclusive execution already active"):
            apply_web_execution_event(
                repo=self.repo, registry_path=self.registry,
                event={
                    "controller_id": "controller-1", "conversation_id": "conv-2", "state": "started",
                    "assignment": self.assignment(assignment_id="A-2", lease_id="A-2:web:attempt:1"),
                    "attestation": self.attestation("running", conversation="conv-2"),
                }, now=T0 + timedelta(seconds=1), continuation_consumer=self._consume,
            )

    def test_reviewer_verdict_is_bound_to_candidate_revision(self):
        self.start(role="reviewer", candidate_revision=self.head, agent_id="web-reviewer-1")
        with self.assertRaisesRegex(ValueError, "reviewed_head does not match candidate revision"):
            self.event(
                "completed", at=T0 + timedelta(minutes=1), delivery_outcome="pass", summary="pass",
                evidence=["test-log:review"], artifacts=[f"git:{self.head}"],
                review_verdict={"reviewed_head": "0" * 40, "verdict": "PASS", "critical": [], "important": [], "minor": []},
            )

    def test_unknown_side_effect_disconnect_cannot_auto_recover(self):
        self.start(side_effect=True, idempotency_key=None)
        self.event("interrupted", at=T0 + timedelta(minutes=1), summary="unknown publish outcome")
        with self.assertRaisesRegex(ValueError, "unknown side effect requires reconciliation"):
            apply_web_execution_event(
                repo=self.repo, registry_path=self.registry,
                event={
                    "controller_id": "controller-1", "conversation_id": "conv-2", "state": "started",
                    "assignment": self.assignment(side_effect=True, idempotency_key=None, attempt=2, lease_id="A-1:web:attempt:2"),
                    "attestation": self.attestation("running", conversation="conv-2"),
                }, now=T0 + timedelta(minutes=2), continuation_consumer=self._consume,
            )

    def test_normal_completion_creates_terminal_receipt_and_continuation(self):
        self.start()
        result = self.event(
            "completed", at=T0 + timedelta(minutes=1), delivery_outcome="unresolved",
            summary="Web agent completed", evidence=[], artifacts=[],
        )
        lease = load_runtime_state(self.repo)["leases"]["A-1"]
        self.assertEqual((lease["terminal_state"], lease["transport_outcome"]), ("completed", "completed"))
        self.assertTrue(Path(result["terminal_receipt"]).is_file())
        self.assertEqual(len(self.wakes), 1)

    def test_web_recovery_budget_exhaustion_blocks_fourth_attempt(self):
        self.start()
        self.event("interrupted", at=T0 + timedelta(minutes=1), summary="lost-1")
        for attempt, minute in ((2, 2), (3, 4)):
            conversation = f"conv-{attempt}"
            apply_web_execution_event(
                repo=self.repo, registry_path=self.registry,
                event={
                    "controller_id": "controller-1", "conversation_id": conversation, "state": "started",
                    "assignment": self.assignment(attempt=attempt, lease_id=f"A-1:web:attempt:{attempt}"),
                    "attestation": self.attestation("running", conversation=conversation),
                }, now=T0 + timedelta(minutes=minute), continuation_consumer=self._consume,
            )
            apply_web_execution_event(
                repo=self.repo, registry_path=self.registry,
                event={
                    "controller_id": "controller-1", "conversation_id": conversation, "assignment_id": "A-1",
                    "state": "interrupted", "summary": f"lost-{attempt}",
                    "attestation": self.attestation("interrupted", conversation=conversation),
                }, now=T0 + timedelta(minutes=minute + 1), continuation_consumer=self._consume,
            )
        lease = load_runtime_state(self.repo)["leases"]["A-1"]
        self.assertEqual(evaluate_lease(lease, now=T0 + timedelta(minutes=6))["state"], "budget_exhausted")
        with self.assertRaisesRegex(ValueError, "recovery budget exhausted"):
            apply_web_execution_event(
                repo=self.repo, registry_path=self.registry,
                event={
                    "controller_id": "controller-1", "conversation_id": "conv-4", "state": "started",
                    "assignment": self.assignment(attempt=4, lease_id="A-1:web:attempt:4"),
                    "attestation": self.attestation("running", conversation="conv-4"),
                }, now=T0 + timedelta(minutes=7), continuation_consumer=self._consume,
            )

    def test_reviewer_pass_accepts_only_exact_candidate_revision(self):
        self.start(role="reviewer", candidate_revision=self.head, agent_id="web-reviewer-1")
        result = self.event(
            "completed", at=T0 + timedelta(minutes=1), delivery_outcome="pass", summary="review pass",
            evidence=["test-log:web-review-pass"], artifacts=[f"git:{self.head}"],
            review_verdict={"reviewed_head": self.head, "verdict": "PASS", "critical": [], "important": [], "minor": []},
        )
        lease = load_runtime_state(self.repo)["leases"]["A-1"]
        self.assertEqual(lease["delivery_outcome"], "pass")
        self.assertEqual(lease["candidate_revision"], self.head)
        self.assertEqual(result["runtime_state"], "terminal")


if __name__ == "__main__":
    unittest.main()
