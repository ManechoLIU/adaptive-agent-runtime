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
            runtime_change_consumer=self._runtime_change, host_verifier=self._verify_host,
        )

    def _verify_host(self, attestation, event):
        return {
            "verified": True, "fresh": True, "replay": False,
            "observation_id": attestation["observation_id"],
        }

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
            runtime_change_consumer=self._runtime_change, host_verifier=self._verify_host,
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

    def test_connection_interrupted_is_not_authoritative_terminal(self):
        self.start()
        with self.assertRaisesRegex(ValueError, "not authoritative terminal"):
            self.event("interrupted", at=T0 + timedelta(minutes=1), summary="Connection interrupted")
        lease = load_runtime_state(self.repo)["leases"]["A-1"]
        self.assertIsNone(lease["terminal_state"])
        self.assertEqual(self.wakes, [])
    def test_host_attested_session_missing_is_not_authoritative_terminal(self):
        self.start()
        with self.assertRaisesRegex(ValueError, "not authoritative terminal"):
            self.event("missing", at=T0 + timedelta(minutes=1), summary="host reports execution missing")
        self.assertIsNone(load_runtime_state(self.repo)["leases"]["A-1"]["terminal_state"])
    def test_browser_tab_absence_is_not_strong_enough_to_claim_disconnect(self):
        self.start()
        with self.assertRaisesRegex(ValueError, "weak browser UI evidence"):
            apply_web_execution_event(
                repo=self.repo, registry_path=self.registry,
                event={
                    "controller_id": "controller-1", "conversation_id": "conv-1", "assignment_id": "A-1",
                    "state": "missing",
                    "attestation": self.attestation("missing", source="ai_bridge_browser_tab"),
                },
                now=T0 + timedelta(minutes=1), host_verifier=self._verify_host,
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
        from scripts.web_agent_execution import recover_web_assignment
        self.start()
        recover_web_assignment(
            repo=self.repo, registry_path=self.registry, controller_id="controller-1",
            assignment_id="A-1", conversation_id="conv-2", now=T0 + timedelta(minutes=46),
            watchdog_launcher=lambda **_: {"launched": False},
            lease_id_factory=lambda assignment_id, attempt: f"{assignment_id}:web:attempt:{attempt}",
        )
        with self.assertRaisesRegex(ValueError, "stale runtime attempt|execution identity"):
            self.event(
                "completed", at=T0 + timedelta(minutes=47), conversation="conv-1", attempt=1,
                delivery_outcome="unresolved", summary="late result", evidence=[], artifacts=[],
            )
    def test_terminal_attempt_cannot_be_revived_by_heartbeat(self):
        self.start()
        self.event(
            "completed", at=T0 + timedelta(minutes=1), delivery_outcome="unresolved",
            summary="completed", evidence=[], artifacts=[],
        )
        with self.assertRaisesRegex(ValueError, "terminal attempt is immutable"):
            self.event("heartbeat", at=T0 + timedelta(minutes=2))
    def test_recovery_must_increment_attempt_and_change_lease(self):
        self.start()
        with self.assertRaisesRegex(ValueError, "recovery attempt must increment"):
            apply_web_execution_event(
                repo=self.repo, registry_path=self.registry,
                event={
                    "controller_id": "controller-1", "conversation_id": "conv-3", "state": "started",
                    "assignment": self.assignment(attempt=3, lease_id="A-1:web:attempt:3"),
                    "attestation": self.attestation("running", conversation="conv-3"),
                }, now=T0 + timedelta(minutes=46), host_verifier=self._verify_host,
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
                }, now=T0 + timedelta(seconds=1), host_verifier=self._verify_host,
            )

    def test_reviewer_verdict_is_bound_to_candidate_revision(self):
        self.start(role="reviewer", candidate_revision=self.head, agent_id="web-reviewer-1")
        with self.assertRaisesRegex(ValueError, "reviewed_head does not match candidate revision"):
            self.event(
                "completed", at=T0 + timedelta(minutes=1), delivery_outcome="pass", summary="pass",
                evidence=["test-log:review"], artifacts=[f"git:{self.head}"],
                review_verdict={"reviewed_head": "0" * 40, "verdict": "PASS", "critical": [], "important": [], "minor": []},
            )

    def test_unknown_side_effect_timeout_cannot_auto_recover(self):
        from scripts.web_agent_execution import recover_web_assignment
        self.start(side_effect=True, idempotency_key="publish:stable")
        with self.assertRaisesRegex(ValueError, "unknown side effect requires reconciliation"):
            recover_web_assignment(
                repo=self.repo, registry_path=self.registry, controller_id="controller-1",
                assignment_id="A-1", conversation_id="conv-2", now=T0 + timedelta(minutes=46),
                watchdog_launcher=lambda **_: {"launched": False},
            )
    def test_normal_completion_persists_runtime_terminal_and_wakes_without_external_receipt(self):
        self.start()
        result = self.event(
            "completed", at=T0 + timedelta(minutes=1), delivery_outcome="unresolved",
            summary="Web agent completed", evidence=[], artifacts=[],
        )
        lease = load_runtime_state(self.repo)["leases"]["A-1"]
        self.assertEqual((lease["terminal_state"], lease["transport_outcome"]), ("completed", "completed"))
        self.assertNotIn("terminal_receipt", result)
        self.assertIn("runtime_continuation", result)
        self.assertEqual(len(self.runtime_wakes), 1)
    def test_web_recovery_budget_exhaustion_blocks_fourth_attempt(self):
        from scripts.web_agent_execution import recover_web_assignment
        self.start()
        recover = lambda conversation, at: recover_web_assignment(
            repo=self.repo, registry_path=self.registry, controller_id="controller-1",
            assignment_id="A-1", conversation_id=conversation, now=at,
            watchdog_launcher=lambda **_: {"launched": False},
            lease_id_factory=lambda assignment_id, attempt: f"{assignment_id}:web:attempt:{attempt}",
        )
        recover("conv-2", T0 + timedelta(minutes=46))
        recover("conv-3", T0 + timedelta(minutes=92))
        lease = load_runtime_state(self.repo)["leases"]["A-1"]
        self.assertEqual(evaluate_lease(lease, now=T0 + timedelta(minutes=138))["state"], "budget_exhausted")
        with self.assertRaisesRegex(ValueError, "recovery budget exhausted"):
            recover("conv-4", T0 + timedelta(minutes=138))
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
        self.assertEqual(lease["review_verdict"]["reviewed_head"], self.head)
        self.assertNotIn("terminal_receipt", result)
        self.assertEqual(result["runtime_state"], "terminal")
        self.assertEqual(len(self.runtime_wakes), 1)
    def test_recovery_cannot_supersede_healthy_active_attempt(self):
        self.start()
        with self.assertRaisesRegex(ValueError, "current attempt is still active"):
            apply_web_execution_event(
                repo=self.repo, registry_path=self.registry,
                event={
                    "controller_id": "controller-1", "conversation_id": "conv-2", "state": "started",
                    "assignment": self.assignment(attempt=2, lease_id="A-1:web:attempt:2"),
                    "attestation": self.attestation("running", conversation="conv-2"),
                }, now=T0 + timedelta(minutes=1), host_verifier=self._verify_host,
            )

    def test_non_start_event_must_match_current_attempt_and_lease_exactly(self):
        self.start()
        with self.assertRaisesRegex(ValueError, "current runtime attempt"):
            self.event("heartbeat", at=T0 + timedelta(minutes=1), attempt=2, lease_id="forged")
        lease = load_runtime_state(self.repo)["leases"]["A-1"]
        self.assertEqual(lease["attempt"], 1)
        self.assertEqual(lease["lease_id"], "A-1:web:attempt:1")

    def test_public_web_adapter_rejects_self_asserted_strong_attestation(self):
        with self.assertRaisesRegex(ValueError, "verified host provenance"):
            apply_web_execution_event(
                repo=self.repo, registry_path=self.registry,
                event={
                    "controller_id": "controller-1", "conversation_id": "conv-1", "state": "started",
                    "assignment": self.assignment(), "attestation": self.attestation("running"),
                }, now=T0,
            )

    def test_verified_host_observation_is_required_to_start_execution(self):
        result = apply_web_execution_event(
            repo=self.repo, registry_path=self.registry,
            event={
                "controller_id": "controller-1", "conversation_id": "conv-1", "state": "started",
                "assignment": self.assignment(), "attestation": self.attestation("running"),
            }, now=T0,
            host_verifier=self._verify_host,
        )
        self.assertEqual(result["runtime_state"], "healthy")

    def test_reviewer_candidate_must_be_resolved_immutable_commit(self):
        with self.assertRaisesRegex(ValueError, "immutable Git commit"):
            apply_web_execution_event(
                repo=self.repo, registry_path=self.registry,
                event={
                    "controller_id": "controller-1", "conversation_id": "conv-1", "state": "started",
                    "assignment": self.assignment(role="reviewer", candidate_revision="main", agent_id="web-reviewer-1"),
                    "attestation": self.attestation("running"),
                }, now=T0,
                host_verifier=self._verify_host,
            )

    def test_host_terminal_attestation_cannot_be_translated_into_progress(self):
        self.start()
        lease_before = load_runtime_state(self.repo)["leases"]["A-1"]
        with self.assertRaisesRegex(ValueError, "progress requires observed running state"):
            apply_web_execution_event(
                repo=self.repo, registry_path=self.registry,
                event={
                    "controller_id": "controller-1", "conversation_id": "conv-1",
                    "assignment_id": "A-1", "state": "progress",
                    "attestation": self.attestation("completed"),
                    "last_observed_head": self.head,
                    "artifact_fingerprint": "artifact:contradictory",
                }, now=T0 + timedelta(minutes=1), host_verifier=self._verify_host,
                runtime_change_consumer=self._runtime_change,
            )
        lease_after = load_runtime_state(self.repo)["leases"]["A-1"]
        self.assertEqual(lease_after["last_event_seq"], lease_before["last_event_seq"])





class RuntimeOwnedWebRecoveryContractTests(unittest.TestCase):
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
        self.registry = root / "controllers.json"
        self.registry.write_text(json.dumps({"controller-1": str(self.repo.resolve())}), encoding="utf-8")
        self.wakes = []
        self.launched = []

    def tearDown(self):
        self.tmp.cleanup()

    def assignment(self, **extra):
        value = {
            "assignment_id": "A-RUNTIME",
            "task_id": "T-RUNTIME",
            "agent_id": "web-writer-runtime",
            "provider": "chatgpt_web",
            "worktree": str(self.repo),
            "primary_goal": "finish bounded runtime task",
            "success_criteria": ["tests pass"],
            "owned_scope": ["README.md"],
            "strategy": "chatgpt-web:gpt-5.6-sol",
            "assignment_contract_version": 2,
            "side_effect": False,
            "progress_deadline_minutes": 10,
            "role": "writer",
        }
        value.update(extra)
        return value

    def _launch(self, **kwargs):
        self.launched.append(kwargs)
        return {"launched": True, "pid": 999}

    def _wake(self, **kwargs):
        self.wakes.append(kwargs)
        return {"controller_id": "controller-1", "pending_control_event": True, "wake_result": {"result": "CONFIRMED"}}

    def test_dispatch_start_needs_no_host_generation_attestation(self):
        from scripts.web_agent_execution import start_web_assignment
        result = start_web_assignment(
            repo=self.repo, registry_path=self.registry, controller_id="controller-1",
            conversation_id="conv-runtime-1", assignment=self.assignment(), now=T0,
            watchdog_launcher=self._launch,
        )
        lease = load_runtime_state(self.repo)["leases"]["A-RUNTIME"]
        self.assertEqual(result["runtime_state"], "healthy")
        self.assertEqual(lease["session_id"], "conv-runtime-1")
        self.assertEqual(lease["health_mode"], "progress_watchdog")
        self.assertIsNone(lease.get("host_attestation_id"))
        self.assertEqual(len(self.launched), 1)

    def test_watchdog_observes_git_progress_without_any_host_event(self):
        from scripts.web_agent_execution import start_web_assignment, watch_web_assignment_once
        start_web_assignment(
            repo=self.repo, registry_path=self.registry, controller_id="controller-1",
            conversation_id="conv-runtime-1", assignment=self.assignment(), now=T0,
            watchdog_launcher=self._launch,
        )
        before = load_runtime_state(self.repo)["leases"]["A-RUNTIME"]["progress_deadline_at"]
        (self.repo / "README.md").write_text("runtime changed\n", encoding="utf-8")
        result = watch_web_assignment_once(
            repo=self.repo, registry_path=self.registry, assignment_id="A-RUNTIME",
            expected_attempt=1, expected_lease_id="A-RUNTIME:web:attempt:1", now=T0 + timedelta(minutes=8),
            runtime_change_consumer=self._wake,
        )
        lease = load_runtime_state(self.repo)["leases"]["A-RUNTIME"]
        self.assertTrue(result["progress_observed"])
        self.assertGreater(lease["progress_deadline_at"], before)
        self.assertEqual(lease["last_progress_at"], (T0 + timedelta(minutes=8)).isoformat())
        self.assertEqual(self.wakes, [])

    def test_watchdog_no_progress_becomes_unhealthy_and_wakes_same_controller(self):
        from scripts.web_agent_execution import start_web_assignment, watch_web_assignment_once
        start_web_assignment(
            repo=self.repo, registry_path=self.registry, controller_id="controller-1",
            conversation_id="conv-runtime-1", assignment=self.assignment(), now=T0,
            watchdog_launcher=self._launch,
        )
        result = watch_web_assignment_once(
            repo=self.repo, registry_path=self.registry, assignment_id="A-RUNTIME",
            expected_attempt=1, expected_lease_id="A-RUNTIME:web:attempt:1",
            now=T0 + timedelta(minutes=26), runtime_change_consumer=self._wake,
        )
        self.assertEqual(result["runtime_state"], "unhealthy")
        self.assertEqual(result["reason"], "progress_stale_beyond_grace")
        self.assertTrue(result["auto_recovery_eligible"])
        self.assertEqual(len(self.wakes), 1)
        self.assertEqual(self.wakes[0]["repo"], self.repo.resolve())

    def test_progress_watchdog_mode_does_not_use_pid_death_as_terminal_or_progress(self):
        from scripts.web_agent_execution import start_web_assignment
        start_web_assignment(
            repo=self.repo, registry_path=self.registry, controller_id="controller-1",
            conversation_id="conv-runtime-1", assignment=self.assignment(), now=T0,
            watchdog_launcher=self._launch,
        )
        lease = load_runtime_state(self.repo)["leases"]["A-RUNTIME"]
        lease["pid"] = 12345
        decision = evaluate_lease(
            lease, now=T0 + timedelta(minutes=5), process_probe=lambda _pid: False
        )
        self.assertEqual(decision, {"state": "healthy", "reason": "runtime_evidence_current"})

    def test_recovery_derives_next_attempt_and_new_lease_from_unhealthy_canonical_lease(self):
        from scripts.web_agent_execution import start_web_assignment, recover_web_assignment
        start_web_assignment(
            repo=self.repo, registry_path=self.registry, controller_id="controller-1",
            conversation_id="conv-runtime-1", assignment=self.assignment(), now=T0,
            watchdog_launcher=self._launch,
        )
        result = recover_web_assignment(
            repo=self.repo, registry_path=self.registry, controller_id="controller-1",
            assignment_id="A-RUNTIME", conversation_id="conv-runtime-2", now=T0 + timedelta(minutes=26),
            watchdog_launcher=self._launch,
            lease_id_factory=lambda assignment_id, attempt: f"{assignment_id}:web:auto:{attempt}",
        )
        lease = load_runtime_state(self.repo)["leases"]["A-RUNTIME"]
        self.assertEqual(result["attempt"], 2)
        self.assertEqual(lease["attempt"], 2)
        self.assertEqual(lease["session_id"], "conv-runtime-2")
        self.assertEqual(lease["lease_id"], "A-RUNTIME:web:auto:2")
        self.assertEqual(lease["recovery_count"], 1)

    def test_unknown_side_effect_timeout_fails_closed_even_with_stable_key(self):
        from scripts.web_agent_execution import start_web_assignment, recover_web_assignment
        start_web_assignment(
            repo=self.repo, registry_path=self.registry, controller_id="controller-1",
            conversation_id="conv-runtime-1",
            assignment=self.assignment(side_effect=True, idempotency_key="publish:42"), now=T0,
            watchdog_launcher=self._launch,
        )
        with self.assertRaisesRegex(ValueError, "unknown side effect"):
            recover_web_assignment(
                repo=self.repo, registry_path=self.registry, controller_id="controller-1",
                assignment_id="A-RUNTIME", conversation_id="conv-runtime-2", now=T0 + timedelta(minutes=26),
                watchdog_launcher=self._launch,
            )
        lease = load_runtime_state(self.repo)["leases"]["A-RUNTIME"]
        self.assertEqual(lease["attempt"], 1)
        self.assertTrue(lease["result_unknown"])

    def test_watchdog_retries_same_controller_wake_after_transient_dispatch_failure(self):
        from scripts.web_agent_execution import start_web_assignment, watch_web_assignment
        now = datetime.now(UTC)
        start_web_assignment(
            repo=self.repo, registry_path=self.registry, controller_id="controller-1",
            conversation_id="conv-runtime-1", assignment=self.assignment(), now=now - timedelta(minutes=26),
            watchdog_launcher=self._launch,
        )
        calls = []

        def flaky_wake(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise RuntimeError("transient wake transport failure")
            return {"controller_id": "controller-1", "pending_control_event": True, "wake_result": {"result": "CONFIRMED"}}

        result = watch_web_assignment(
            repo=self.repo, registry_path=self.registry, assignment_id="A-RUNTIME",
            expected_attempt=1, expected_lease_id="A-RUNTIME:web:attempt:1",
            poll_seconds=0.1, runtime_change_consumer=flaky_wake,
        )
        self.assertEqual(result["runtime_state"], "unhealthy")
        self.assertNotIn("wake_error", result)
        self.assertEqual(len(calls), 2)
        lease = load_runtime_state(self.repo)["leases"]["A-RUNTIME"]
        self.assertEqual(lease["attempt"], 1)
        self.assertIsNone(lease["terminal_state"])

    def test_connection_interrupted_text_is_not_a_runtime_terminal(self):
        from scripts.web_agent_execution import start_web_assignment, apply_web_execution_event
        start_web_assignment(
            repo=self.repo, registry_path=self.registry, controller_id="controller-1",
            conversation_id="conv-runtime-1", assignment=self.assignment(), now=T0,
            watchdog_launcher=self._launch,
        )
        with self.assertRaisesRegex(ValueError, "not authoritative terminal"):
            apply_web_execution_event(
                repo=self.repo, registry_path=self.registry,
                event={
                    "controller_id": "controller-1", "conversation_id": "conv-runtime-1",
                    "assignment_id": "A-RUNTIME", "state": "interrupted", "summary": "Connection interrupted",
                    "attestation": {
                        "kind": "web_execution_state", "source": "chatgpt_host_event",
                        "observation_id": "obs-interrupted", "conversation_id": "conv-runtime-1", "state": "interrupted",
                    },
                }, now=T0 + timedelta(minutes=1), host_verifier=lambda att, event: {
                    "verified": True, "fresh": True, "replay": False, "observation_id": att["observation_id"]
                },
            )
        self.assertIsNone(load_runtime_state(self.repo)["leases"]["A-RUNTIME"]["terminal_state"])


if __name__ == "__main__":
    unittest.main()
