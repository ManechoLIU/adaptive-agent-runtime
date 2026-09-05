import json
import sys
from io import StringIO
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from scripts.assignment_runtime import RuntimePolicy, evaluate_lease, load_runtime_state
from scripts.web_agent_execution import (
    _apply_verified_web_execution_event,
    apply_web_execution_event,
)


UTC = timezone.utc
T0 = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


def verified_web_execution_event(*, host_verifier, **kwargs):
    with patch(
        "scripts.web_agent_execution._trusted_host_execution_verifier",
        return_value=host_verifier,
    ):
        return _apply_verified_web_execution_event(**kwargs)


def bind_recovery_attempt(
    *, repo, registry, controller_id, assignment, conversation_id, task_name, at,
    observed_model="gpt-5.6-sol", observed_agent_type="default",
):
    from scripts.web_agent_execution import prepare_web_assignment_dispatch, bind_web_assignment_dispatch
    prepared_at = at - timedelta(seconds=1)
    event_path = Path(registry).parent / f"{task_name}-{conversation_id}.jsonl"
    trusted = {
        "ready": True,
        "reason": "test_host_attested",
        "event_paths": [str(event_path.resolve())],
    }
    with patch("scripts.web_agent_execution._machine_event_source_context", return_value=trusted):
        prepared = prepare_web_assignment_dispatch(
            repo=repo, registry_path=registry, controller_id=controller_id,
            task_name=task_name, assignment=assignment, now=prepared_at,
            health_probe=lambda: True,
        )
        call_id = f"spawn-{task_name}-{conversation_id}"
        records = [
            {
                "timestamp": prepared_at.isoformat(),
                "type": "response_item",
                "payload": {
                    "type": "function_call", "namespace": "collaboration", "name": "spawn_agent",
                    "call_id": call_id,
                    "arguments": json.dumps({"task_name": task_name, "agent_type": observed_agent_type, "model": observed_model}),
                },
            },
            {
                "timestamp": at.isoformat(),
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "SubAgentActivity", "kind": "started", "id": call_id,
                        "agent_thread_id": conversation_id, "agent_path": f"/root/{task_name}",
                    },
                },
            },
        ]
        event_path.write_text(chr(10).join(json.dumps(item) for item in records) + chr(10), encoding="utf-8")
        return bind_web_assignment_dispatch(
            repo=repo, registry_path=registry, controller_id=controller_id,
            dispatch_id=prepared["dispatch_id"], event_paths=[event_path], now=at,
            health_probe=lambda: True,
            watchdog_launcher=lambda **_: {"launched": False, "reason": "test_drives_health_explicitly"},
        )



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
        self.policy = root / "AGENTS.md"
        self.policy.write_text(
            "general 默认 provider=chatgpt_web、model=gpt-5.6-sol、auth_mode=host。\n",
            encoding="utf-8",
        )
        self.wakes = []
        self.runtime_wakes = []

    def tearDown(self):
        self.tmp.cleanup()

    def attestation(
        self, state, *, source="chatgpt_host_event", conversation="conv-1", observed_at=None
    ):
        value = {
            "kind": "web_execution_state",
            "source": source,
            "observation_id": f"obs:{state}:{conversation}",
            "conversation_id": conversation,
            "state": state,
        }
        if observed_at is not None:
            value["observed_at"] = observed_at.isoformat()
        return value

    def assignment(self, **extra):
        value = {
            "assignment_id": "A-1",
            "task_id": "T-1",
            "agent_id": "web-writer-1",
            "provider": "chatgpt_web",
            "model": "gpt-5.6-sol",
            "agent_type": "default",
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

    def start(self, **assignment_extra):
        from scripts.web_agent_execution import prepare_web_assignment_dispatch
        assignment = self.assignment(**assignment_extra)
        trusted = {
            "ready": True,
            "reason": "test_host_attested",
            "event_paths": [str((Path(self.tmp.name) / "host-events.jsonl").resolve())],
        }
        with patch("scripts.web_agent_execution._machine_event_source_context", return_value=trusted):
            prepared = prepare_web_assignment_dispatch(
                repo=self.repo,
                registry_path=self.registry,
                controller_id="controller-1",
                task_name=f"host-start-{assignment['assignment_id']}",
                assignment=assignment,
                now=T0 - timedelta(seconds=1),
                health_probe=lambda: True,
            )
        return verified_web_execution_event(
            repo=self.repo,
            registry_path=self.registry,
            event={
                "controller_id": "controller-1",
                "conversation_id": "conv-1",
                "state": "started",
                "dispatch_id": prepared["dispatch_id"],
                "assignment": assignment,
                "model": assignment["model"],
                "agent_type": assignment["agent_type"],
                "attestation": self.attestation("running", observed_at=T0),
            },
            now=T0,
            runtime_change_consumer=self._runtime_change, host_verifier=self._verify_host,
        )

    def _verify_host(self, attestation, event):
        result = {
            "verified": True, "fresh": True, "replay": False,
            "observation_id": attestation["observation_id"],
        }
        if attestation.get("observed_at"):
            result["observed_at"] = attestation["observed_at"]
        return result

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
        return verified_web_execution_event(
            repo=self.repo, registry_path=self.registry, event=payload, now=at,
            runtime_change_consumer=self._runtime_change, host_verifier=self._verify_host,
        )

    def test_direct_start_web_assignment_is_rejected_even_with_forged_readiness_probe(self):
        from scripts.web_agent_execution import start_web_assignment
        with self.assertRaisesRegex(PermissionError, "prepared dispatch|direct Web start|bind"):
            start_web_assignment(
                repo=self.repo,
                registry_path=self.registry,
                controller_id="controller-1",
                conversation_id="conv-direct",
                assignment=self.assignment(),
                now=T0,
                watchdog_launcher=lambda **_: {"launched": False},
                health_probe=lambda: True,
                event_source_probe=lambda: True,
            )
        self.assertNotIn("A-1", load_runtime_state(self.repo).get("leases", {}))

    def test_verified_host_started_event_still_requires_prepared_dispatch_ticket(self):
        with self.assertRaisesRegex(PermissionError, "prepared dispatch|dispatch ticket"):
            verified_web_execution_event(
                repo=self.repo,
                registry_path=self.registry,
                event={
                    "controller_id": "controller-1",
                    "conversation_id": "conv-direct",
                    "state": "started",
                    "assignment": self.assignment(),
                    "attestation": self.attestation(
                        "running", conversation="conv-direct", observed_at=T0
                    ),
                },
                now=T0,
                runtime_change_consumer=self._runtime_change,
                host_verifier=self._verify_host,
            )
        self.assertNotIn("A-1", load_runtime_state(self.repo).get("leases", {}))

    def test_start_enters_canonical_runtime_and_is_healthy(self):
        result = self.start()
        lease = load_runtime_state(self.repo)["leases"]["A-1"]
        self.assertEqual(result["runtime_state"], "healthy")
        self.assertEqual(lease["session_id"], "conv-1")
        self.assertEqual(lease["execution_transport"], "web")
        self.assertEqual(lease["exclusive_execution_key"], "task:T-1")
        self.assertEqual(
            lease["exclusive_execution_keys"],
            ["task:T-1", f"worktree:{self.repo.resolve()}"],
        )
        self.assertNotEqual(lease["session_id"], "controller-1")

    def test_host_started_lease_uses_attested_machine_start_time_not_ingestion_time(self):
        from scripts.web_agent_execution import prepare_web_assignment_dispatch
        assignment = self.assignment(assignment_id="A-HOST-TIME", lease_id="A-HOST-TIME:web:attempt:1")
        trusted = {
            "ready": True,
            "reason": "test_host_attested",
            "event_paths": [str((Path(self.tmp.name) / "host-events-time.jsonl").resolve())],
        }
        prepared_at = T0 - timedelta(seconds=5)
        machine_started_at = T0 - timedelta(seconds=2)
        ingestion_at = T0 + timedelta(minutes=7)
        with patch("scripts.web_agent_execution._machine_event_source_context", return_value=trusted):
            prepared = prepare_web_assignment_dispatch(
                repo=self.repo,
                registry_path=self.registry,
                controller_id="controller-1",
                task_name="host-machine-start-time",
                assignment=assignment,
                now=prepared_at,
                health_probe=lambda: True,
            )
        verified_web_execution_event(
            repo=self.repo,
            registry_path=self.registry,
            event={
                "controller_id": "controller-1",
                "conversation_id": "conv-host-time",
                "state": "started",
                "dispatch_id": prepared["dispatch_id"],
                "assignment": assignment,
                "model": assignment["model"],
                "agent_type": assignment["agent_type"],
                "attestation": self.attestation(
                    "running",
                    conversation="conv-host-time",
                    observed_at=machine_started_at,
                ),
            },
            now=ingestion_at,
            runtime_change_consumer=self._runtime_change,
            host_verifier=self._verify_host,
        )
        lease = load_runtime_state(self.repo)["leases"]["A-HOST-TIME"]
        self.assertEqual(lease["started_at"], machine_started_at.isoformat())
        self.assertNotEqual(lease["started_at"], ingestion_at.isoformat())

    def test_host_started_rejects_unattested_or_mismatched_machine_start_time(self):
        from scripts.web_agent_execution import prepare_web_assignment_dispatch
        assignment = self.assignment(assignment_id="A-HOST-TIME-BAD", lease_id="A-HOST-TIME-BAD:web:attempt:1")
        trusted = {
            "ready": True,
            "reason": "test_host_attested",
            "event_paths": [str((Path(self.tmp.name) / "host-events-time-bad.jsonl").resolve())],
        }
        with patch("scripts.web_agent_execution._machine_event_source_context", return_value=trusted):
            prepared = prepare_web_assignment_dispatch(
                repo=self.repo,
                registry_path=self.registry,
                controller_id="controller-1",
                task_name="host-machine-start-time-bad",
                assignment=assignment,
                now=T0 - timedelta(seconds=5),
                health_probe=lambda: True,
            )
        event = {
            "controller_id": "controller-1",
            "conversation_id": "conv-host-time-bad",
            "state": "started",
            "dispatch_id": prepared["dispatch_id"],
            "assignment": assignment,
            "model": assignment["model"],
            "agent_type": assignment["agent_type"],
            "attestation": self.attestation(
                "running",
                conversation="conv-host-time-bad",
                observed_at=T0 - timedelta(seconds=2),
            ),
        }
        def mismatched_verifier(attestation, _event):
            return {
                "verified": True,
                "fresh": True,
                "replay": False,
                "observation_id": attestation["observation_id"],
                "observed_at": T0.isoformat(),
            }
        with self.assertRaisesRegex(ValueError, "started timestamp mismatch"):
            verified_web_execution_event(
                repo=self.repo,
                registry_path=self.registry,
                event=event,
                now=T0,
                runtime_change_consumer=self._runtime_change,
                host_verifier=mismatched_verifier,
            )
        self.assertNotIn("A-HOST-TIME-BAD", load_runtime_state(self.repo).get("leases", {}))

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
            verified_web_execution_event(
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
        bind_recovery_attempt(
            repo=self.repo, registry=self.registry, controller_id="controller-1",
            assignment=self.assignment(), conversation_id="conv-2", task_name="recover-a1-attempt-2",
            at=T0 + timedelta(minutes=46),
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
    def test_recovery_attempt_is_runtime_derived_and_caller_cannot_skip_attempts(self):
        self.start()
        result = bind_recovery_attempt(
            repo=self.repo,
            registry=self.registry,
            controller_id="controller-1",
            assignment=self.assignment(attempt=3, lease_id="caller-forged-attempt-3"),
            conversation_id="conv-3",
            task_name="recover-runtime-derived-attempt",
            at=T0 + timedelta(minutes=46),
        )
        lease = load_runtime_state(self.repo)["leases"]["A-1"]
        self.assertEqual(result["attempt"], 2)
        self.assertEqual(lease["attempt"], 2)
        self.assertNotEqual(lease["lease_id"], "caller-forged-attempt-3")
    def test_active_execution_blocks_duplicate_assignment_even_across_transport(self):
        self.start()
        with self.assertRaisesRegex(ValueError, "exclusive execution already active"):
            bind_recovery_attempt(
                repo=self.repo,
                registry=self.registry,
                controller_id="controller-1",
                assignment=self.assignment(assignment_id="A-2", lease_id="A-2:web:attempt:1"),
                conversation_id="conv-2",
                task_name="duplicate-task-execution",
                at=T0 + timedelta(seconds=1),
            )

    def test_active_writer_blocks_different_task_in_same_worktree(self):
        self.start()
        with self.assertRaisesRegex(ValueError, "worktree:"):
            bind_recovery_attempt(
                repo=self.repo,
                registry=self.registry,
                controller_id="controller-1",
                assignment=self.assignment(
                    assignment_id="A-2", task_id="T-2", agent_id="web-writer-2",
                    lease_id="A-2:web:attempt:1",
                ),
                conversation_id="conv-2",
                task_name="same-worktree-different-task",
                at=T0 + timedelta(seconds=1),
            )

    def test_reviewer_does_not_claim_writer_worktree_exclusive_key(self):
        self.start(role="reviewer", candidate_revision=self.head, agent_id="web-reviewer-1")
        lease = load_runtime_state(self.repo)["leases"]["A-1"]
        self.assertEqual(lease["exclusive_execution_keys"], ["task:T-1"])

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
        with patch(
            "scripts.web_agent_execution._machine_event_source_context",
            return_value={"ready": True, "reason": "test_host_attested", "event_paths": [str(self.repo / "host-events.jsonl")]},
        ), self.assertRaisesRegex(ValueError, "unknown side effect requires reconciliation"):
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
        recover = lambda conversation, task_name, at: bind_recovery_attempt(
            repo=self.repo, registry=self.registry, controller_id="controller-1",
            assignment=self.assignment(), conversation_id=conversation, task_name=task_name, at=at,
        )
        recover("conv-2", "recover-budget-2", T0 + timedelta(minutes=46))
        recover("conv-3", "recover-budget-3", T0 + timedelta(minutes=92))
        lease = load_runtime_state(self.repo)["leases"]["A-1"]
        self.assertEqual(evaluate_lease(lease, now=T0 + timedelta(minutes=138))["state"], "budget_exhausted")
        with self.assertRaisesRegex(ValueError, "recovery budget exhausted"):
            recover("conv-4", "recover-budget-4", T0 + timedelta(minutes=138))
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
        with self.assertRaisesRegex(ValueError, "runtime_not_unhealthy|still active|not allowed"):
            bind_recovery_attempt(
                repo=self.repo,
                registry=self.registry,
                controller_id="controller-1",
                assignment=self.assignment(),
                conversation_id="conv-2",
                task_name="healthy-attempt-cannot-recover",
                at=T0 + timedelta(minutes=1),
            )

    def test_non_start_event_must_match_current_attempt_and_lease_exactly(self):
        self.start()
        with self.assertRaisesRegex(ValueError, "current runtime attempt"):
            self.event("heartbeat", at=T0 + timedelta(minutes=1), attempt=2, lease_id="forged")
        lease = load_runtime_state(self.repo)["leases"]["A-1"]
        self.assertEqual(lease["attempt"], 1)
        self.assertEqual(lease["lease_id"], "A-1:web:attempt:1")

    def test_public_web_adapter_rejects_self_asserted_strong_attestation(self):
        import inspect
        self.assertNotIn("host_verifier", inspect.signature(apply_web_execution_event).parameters)
        with self.assertRaises(TypeError):
            apply_web_execution_event(
                repo=self.repo,
                registry_path=self.registry,
                event={},
                host_verifier=self._verify_host,
            )
        with self.assertRaisesRegex(PermissionError, "direct Web Host event ingest is disabled"):
            apply_web_execution_event(
                repo=self.repo, registry_path=self.registry,
                event={
                    "controller_id": "controller-1", "conversation_id": "conv-1", "state": "started",
                    "assignment": self.assignment(), "attestation": self.attestation("running"),
                }, now=T0,
            )
        self.assertNotIn("A-1", load_runtime_state(self.repo).get("leases", {}))


    def test_verified_host_observation_is_required_to_start_execution(self):
        result = self.start()
        self.assertEqual(result["runtime_state"], "healthy")
        ticket = result["dispatch_id"]
        self.assertTrue(ticket)

    def test_reviewer_candidate_must_be_resolved_immutable_commit(self):
        with self.assertRaisesRegex(ValueError, "immutable Git commit"):
            self.start(
                role="reviewer",
                candidate_revision="main",
                agent_id="web-reviewer-1",
            )

    def test_host_terminal_attestation_cannot_be_translated_into_progress(self):
        self.start()
        lease_before = load_runtime_state(self.repo)["leases"]["A-1"]
        with self.assertRaisesRegex(ValueError, "progress requires observed running state"):
            verified_web_execution_event(
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
        self.policy = root / "AGENTS.md"
        self.policy.write_text(
            "general 默认 provider=chatgpt_web、model=gpt-5.6-sol、auth_mode=host。\n"
            "前端默认 provider=kimi-code、model=kimi-k3、auth_mode=api。\n"
            "前端fallback provider=chatgpt_web、model=gpt-5.6-sol、auth_mode=host。\n"
            "后端默认 provider=grok-build、model=grok-4.6、auth_mode=oauth。\n",
            encoding="utf-8",
        )
        self.wakes = []
        self.launched = []
        self._machine_source_patcher = patch(
            "scripts.web_agent_execution._machine_event_source_context",
            return_value={
                "ready": True,
                "reason": "test_host_attested",
                "event_paths": [str((root / "trusted-events.jsonl").resolve())],
            },
        )
        self._machine_source_patcher.start()
        self.addCleanup(self._machine_source_patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def seed_prior_external_terminal(
        self,
        *,
        assignment_id="A-KIMI-PRIOR",
        task_id="T-RUNTIME",
        provider="kimi-code",
        model="kimi-k3",
        auth_mode="api",
        evidence=None,
        result_unknown=False,
        terminal_state="failed",
        delivery_outcome="unresolved",
    ):
        from scripts.assignment_runtime import apply_runtime_receipt
        evidence = list(evidence if evidence is not None else ["receipt:kimi/terminal-provider-unavailable"])
        started = {
            "event_type": "assignment_started",
            "assignment_id": assignment_id,
            "task_id": task_id,
            "agent_id": "kimi-prior",
            "provider": provider,
            "model": model,
            "agent_type": "external-kimi",
            "session_id": "kimi-prior-session",
            "worktree": str(self.repo),
            "issued_at": (T0 - timedelta(minutes=2)).isoformat(),
            "attempt": 1,
            "lease_id": f"{assignment_id}:attempt:1",
            "event_seq": 1,
            "receipt_id": f"{assignment_id}:1:1",
            "assignment_contract_version": 2,
            "side_effect": False,
            "primary_goal": "attempt preferred external route",
            "success_criteria": ["produce bounded result"],
            "owned_scope": ["apps/web/src/runtime.ts"],
            "strategy": "external-preferred",
            "auth_mode": auth_mode,
            "policy_class": "frontend",
            "route_decision": "default",
            "route_contract": {
                "decision": "default",
                "policy_class": "frontend",
                "provider": provider,
                "model": model,
                "auth_mode": auth_mode,
                "policy_source": {
                    "path": str(self.policy.resolve()),
                    "sha256": __import__("hashlib").sha256(self.policy.read_bytes()).hexdigest(),
                },
            },
        }
        apply_runtime_receipt(self.repo, started, now=T0 - timedelta(minutes=2))
        terminal = {
            "event_type": "assignment_terminal",
            "assignment_id": assignment_id,
            "task_id": task_id,
            "agent_id": "kimi-prior",
            "provider": provider,
            "model": model,
            "agent_type": "external-kimi",
            "session_id": "kimi-prior-session",
            "worktree": str(self.repo),
            "issued_at": (T0 - timedelta(minutes=1)).isoformat(),
            "attempt": 1,
            "lease_id": started["lease_id"],
            "event_seq": 2,
            "receipt_id": f"{assignment_id}:1:2",
            "terminal_state": terminal_state,
            "transport_outcome": "failed" if terminal_state == "failed" else "cancelled",
            "delivery_outcome": delivery_outcome,
            "summary": "preferred provider failed before safe fallback",
            "evidence": evidence,
            "artifacts": [],
            "next_action": "use declared safe fallback",
            "retry_class": "provider_exit",
            "side_effect": False,
            "result_unknown": result_unknown,
        }
        apply_runtime_receipt(self.repo, terminal, now=T0 - timedelta(minutes=1))
        return load_runtime_state(self.repo)["leases"][assignment_id]

    def assignment(self, **extra):
        value = {
            "assignment_id": "A-RUNTIME",
            "task_id": "T-RUNTIME",
            "agent_id": "web-writer-runtime",
            "provider": "chatgpt_web",
            "model": "gpt-5.6-sol",
            "agent_type": "default",
            "worktree": str(self.repo),
            "primary_goal": "finish bounded runtime task",
            "success_criteria": ["tests pass"],
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

    def _launch(self, **kwargs):
        self.launched.append(kwargs)
        return {"launched": True, "pid": 999}

    def _wake(self, **kwargs):
        self.wakes.append(kwargs)
        return {"controller_id": "controller-1", "pending_control_event": True, "wake_result": {"result": "CONFIRMED"}}

    def route(self, *, decision="default", policy_class="general", provider="chatgpt_web",
              model="gpt-5.6-sol", auth_mode="host", **extra):
        policy = Path(self.tmp.name) / "AGENTS.md"
        if not policy.exists():
            policy.write_text(
                "前端默认 provider=kimi-code、model=kimi-k3、auth_mode=api。\n"
                "后端默认 provider=grok-build、model=grok-4.6、auth_mode=oauth。\n"
                "general 默认 provider=chatgpt_web、model=gpt-5.6-sol、auth_mode=host。\n",
                encoding="utf-8",
            )
        import hashlib
        value = {
            "decision": decision,
            "policy_class": policy_class,
            "provider": provider,
            "model": model,
            "auth_mode": auth_mode,
            "policy_source": {
                "path": str(policy.resolve()),
                "sha256": hashlib.sha256(policy.read_bytes()).hexdigest(),
            },
        }
        value.update(extra)
        return value

    def test_prepare_web_dispatch_requires_formal_route_contract(self):
        from scripts.web_agent_execution import prepare_web_assignment_dispatch
        with self.assertRaisesRegex((ValueError, PermissionError), "route"):
            prepare_web_assignment_dispatch(
                repo=self.repo, registry_path=self.registry, controller_id="controller-1",
                task_name="runtime-route-missing", assignment=self.assignment(route=None), now=T0,
                health_probe=lambda: True,
                event_source_probe=lambda: True,
            )

    def test_frontend_default_kimi_route_cannot_prepare_chatgpt_web_assignment(self):
        from scripts.web_agent_execution import prepare_web_assignment_dispatch
        assignment = self.assignment(
            owned_scope=["apps/web/src/runtime.ts"],
            route=self.route(
                policy_class="frontend", provider="kimi-code", model="kimi-k3", auth_mode="api"
            ),
            auth_mode="host",
        )
        with self.assertRaisesRegex((ValueError, PermissionError), "provider|model|route"):
            prepare_web_assignment_dispatch(
                repo=self.repo, registry_path=self.registry, controller_id="controller-1",
                task_name="runtime-frontend-route-bypass", assignment=assignment, now=T0,
                health_probe=lambda: True,
                event_source_probe=lambda: True,
            )

    def test_heterogeneous_kimi_and_grok_routes_cannot_be_executed_by_web_transport(self):
        from scripts.control_event_guard import delegated_route_contract_errors
        from scripts.web_agent_execution import prepare_web_assignment_dispatch

        frontend_route = self.route(
            policy_class="frontend", provider="kimi-code", model="kimi-k3", auth_mode="api"
        )
        backend_route = self.route(
            policy_class="backend", provider="grok-build", model="grok-4.6", auth_mode="oauth"
        )
        self.assertEqual(
            delegated_route_contract_errors(
                "WEB-KIMI", ["apps/web/src/runtime.ts"], frontend_route
            ),
            [],
        )
        self.assertEqual(
            delegated_route_contract_errors(
                "SERVER-GROK", ["apps/server/src/runtime.ts"], backend_route
            ),
            [],
        )

        frontend = self.assignment(
            assignment_id="A-KIMI", task_id="WEB-KIMI", agent_id="kimi-writer",
            provider="kimi-code", model="kimi-k3", agent_type="external-kimi",
            owned_scope=["apps/web/src/runtime.ts"], strategy="kimi-code:kimi-k3",
            route=frontend_route,
        )
        backend = self.assignment(
            assignment_id="A-GROK", task_id="SERVER-GROK", agent_id="grok-writer",
            provider="grok-build", model="grok-4.6", agent_type="external-grok",
            owned_scope=["apps/server/src/runtime.ts"], strategy="grok-build:grok-4.6",
            route=backend_route,
        )
        for task_name, assignment in (("frontend-kimi", frontend), ("backend-grok", backend)):
            with self.assertRaisesRegex(PermissionError, "Web execution.*provider|Web transport|chatgpt_web"):
                prepare_web_assignment_dispatch(
                    repo=self.repo, registry_path=self.registry, controller_id="controller-1",
                    task_name=task_name, assignment=assignment, now=T0,
                    health_probe=lambda: True, event_source_probe=lambda: True,
                )

    def test_web_safe_fallback_requires_terminal_failure_proof(self):
        from scripts.web_agent_execution import prepare_web_assignment_dispatch
        assignment = self.assignment(
            owned_scope=["apps/web/src/runtime.ts"],
            auth_mode="host",
            route=self.route(
                decision="safe_fallback",
                policy_class="frontend",
                provider="chatgpt_web",
                model="gpt-5.6-sol",
                auth_mode="host",
                fallback_from={"provider": "kimi-code", "model": "kimi-k3", "auth_mode": "api"},
            ),
        )
        with self.assertRaisesRegex((ValueError, PermissionError), "fallback|terminal|failure|result"):
            prepare_web_assignment_dispatch(
                repo=self.repo, registry_path=self.registry, controller_id="controller-1",
                task_name="runtime-unproven-fallback", assignment=assignment, now=T0,
                health_probe=lambda: True,
                event_source_probe=lambda: True,
            )

    def test_forged_safe_fallback_fields_without_canonical_prior_terminal_are_rejected(self):
        from scripts.web_agent_execution import prepare_web_assignment_dispatch
        assignment = self.assignment(
            owned_scope=["apps/web/src/runtime.ts"],
            route=self.route(
                decision="safe_fallback",
                policy_class="frontend",
                provider="chatgpt_web",
                model="gpt-5.6-sol",
                auth_mode="host",
                fallback_from={"provider": "kimi-code", "model": "kimi-k3", "auth_mode": "api"},
                prior_assignment_id="A-KIMI-PRIOR",
                failure_evidence="receipt:kimi/terminal-provider-unavailable",
                prior_attempt_terminal=True,
                result_unknown=False,
            ),
        )
        with self.assertRaisesRegex((ValueError, PermissionError), "canonical|prior.*assignment|terminal"):
            prepare_web_assignment_dispatch(
                repo=self.repo, registry_path=self.registry, controller_id="controller-1",
                task_name="runtime-forged-fallback", assignment=assignment, now=T0,
                health_probe=lambda: True,
            )

    def test_safe_fallback_requires_traceable_evidence_from_canonical_prior_lease(self):
        from scripts.web_agent_execution import prepare_web_assignment_dispatch
        self.seed_prior_external_terminal(evidence=[])
        assignment = self.assignment(
            owned_scope=["apps/web/src/runtime.ts"],
            route=self.route(
                decision="safe_fallback",
                policy_class="frontend",
                provider="chatgpt_web",
                model="gpt-5.6-sol",
                auth_mode="host",
                fallback_from={"provider": "kimi-code", "model": "kimi-k3", "auth_mode": "api"},
                prior_assignment_id="A-KIMI-PRIOR",
                failure_evidence="receipt:kimi/terminal-provider-unavailable",
            ),
        )
        with self.assertRaisesRegex((ValueError, PermissionError), "evidence|canonical"):
            prepare_web_assignment_dispatch(
                repo=self.repo, registry_path=self.registry, controller_id="controller-1",
                task_name="runtime-fallback-missing-evidence", assignment=assignment, now=T0,
                health_probe=lambda: True,
            )

    def test_safe_fallback_policy_must_declare_selected_fallback_route_not_only_origin(self):
        from scripts.web_agent_execution import prepare_web_assignment_dispatch
        self.seed_prior_external_terminal()
        self.policy.write_text(
            "前端默认 provider=kimi-code、model=kimi-k3、auth_mode=api。" + chr(10),
            encoding="utf-8",
        )
        assignment = self.assignment(
            owned_scope=["apps/web/src/runtime.ts"],
            route=self.route(
                decision="safe_fallback",
                policy_class="frontend",
                provider="chatgpt_web",
                model="gpt-5.6-sol",
                auth_mode="host",
                fallback_from={"provider": "kimi-code", "model": "kimi-k3", "auth_mode": "api"},
                prior_assignment_id="A-KIMI-PRIOR",
                failure_evidence="receipt:kimi/terminal-provider-unavailable",
            ),
        )
        with self.assertRaisesRegex((ValueError, PermissionError), "policy|declared"):
            prepare_web_assignment_dispatch(
                repo=self.repo, registry_path=self.registry, controller_id="controller-1",
                task_name="runtime-undeclared-fallback-route", assignment=assignment, now=T0,
                health_probe=lambda: True,
            )

    def test_canonical_kimi_terminal_with_declared_route_can_prepare_web_fallback(self):
        from scripts.web_agent_execution import prepare_web_assignment_dispatch
        self.seed_prior_external_terminal()
        assignment = self.assignment(
            owned_scope=["apps/web/src/runtime.ts"],
            route=self.route(
                decision="safe_fallback",
                policy_class="frontend",
                provider="chatgpt_web",
                model="gpt-5.6-sol",
                auth_mode="host",
                fallback_from={"provider": "kimi-code", "model": "kimi-k3", "auth_mode": "api"},
                prior_assignment_id="A-KIMI-PRIOR",
                failure_evidence="receipt:kimi/terminal-provider-unavailable",
            ),
        )
        prepared = prepare_web_assignment_dispatch(
            repo=self.repo, registry_path=self.registry, controller_id="controller-1",
            task_name="runtime-proven-web-fallback", assignment=assignment, now=T0,
            health_probe=lambda: True,
        )
        self.assertEqual(prepared["assignment"]["route"]["decision"], "safe_fallback")
        self.assertEqual(prepared["assignment"]["route"]["prior_assignment_id"], "A-KIMI-PRIOR")


    def test_forged_local_machine_event_receipt_cannot_enable_production_prepare(self):
        from datetime import datetime, timezone
        from scripts.web_agent_events import DEFAULT_MACHINE_EVENT_SOURCE_RECEIPT
        from scripts.web_agent_execution import prepare_web_assignment_dispatch

        receipt = Path(DEFAULT_MACHINE_EVENT_SOURCE_RECEIPT).expanduser()
        original = receipt.read_bytes() if receipt.exists() else None
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text(json.dumps({
            "schema_version": 1,
            "state": "ready",
            "source": "chatgpt_subagent_machine_events",
            "events": ["started", "completed", "failed", "interrupted", "cancelled", "disconnected"],
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }), encoding="utf-8")
        try:
            with patch(
                "scripts.web_agent_execution._machine_event_source_context",
                return_value={"ready": False, "reason": "trusted_machine_event_source_verifier_unavailable"},
            ), self.assertRaisesRegex(RuntimeError, "trusted.*machine.*event|machine.*event source"):
                prepare_web_assignment_dispatch(
                    repo=self.repo,
                    registry_path=self.registry,
                    controller_id="controller-1",
                    task_name="forged-local-source",
                    assignment=self.assignment(),
                    now=T0,
                    health_probe=lambda: True,
                )
        finally:
            if original is None:
                receipt.unlink(missing_ok=True)
            else:
                receipt.write_bytes(original)

    def test_machine_event_source_public_status_cannot_accept_caller_verifier(self):
        import inspect
        from scripts.web_agent_events import machine_event_source_status
        self.assertNotIn("verifier", inspect.signature(machine_event_source_status).parameters)
        with self.assertRaises(TypeError):
            machine_event_source_status(verifier=lambda *_: {"verified": True})
        receipt = Path(self.tmp.name) / "forged-event-source.json"
        receipt.write_text(json.dumps({
            "schema_version": 1,
            "state": "ready",
            "source": "chatgpt_subagent_machine_events",
            "events": ["started", "completed", "failed", "interrupted", "cancelled", "disconnected"],
            "observed_at": T0.isoformat(),
        }), encoding="utf-8")
        status = machine_event_source_status(path=receipt, now=T0)
        self.assertFalse(status["ready"])
        self.assertEqual(status["reason"], "trusted_machine_event_source_verifier_unavailable")

    def test_production_prepare_fails_closed_without_machine_web_event_source(self):
        from scripts.web_agent_execution import prepare_web_assignment_dispatch
        with patch(
            "scripts.web_agent_execution._machine_event_source_context",
            return_value={"ready": False, "reason": "trusted_machine_event_source_verifier_unavailable"},
        ), self.assertRaisesRegex(RuntimeError, "machine.*event source|event source"):
            prepare_web_assignment_dispatch(
                repo=self.repo,
                registry_path=self.registry,
                controller_id="controller-1",
                task_name="runtime-no-real-web-events",
                assignment=self.assignment(),
                now=T0,
                health_probe=lambda: True,
            )

    def test_direct_internal_observation_chain_cannot_create_lease_without_attested_machine_event(self):
        from scripts.web_agent_execution import (
            prepare_web_assignment_dispatch,
            _persist_observed_dispatch,
            _start_bound_web_assignment,
        )
        assignment = self.assignment()
        prepared = prepare_web_assignment_dispatch(
            repo=self.repo,
            registry_path=self.registry,
            controller_id="controller-1",
            task_name="forged-internal-start",
            assignment=assignment,
            now=T0,
            health_probe=lambda: True,
        )
        trusted_path = Path(self.tmp.name) / "trusted-events.jsonl"
        _persist_observed_dispatch(
            repo=self.repo,
            dispatch_id=prepared["dispatch_id"],
            controller_id="controller-1",
            assignment_id=assignment["assignment_id"],
            observed={
                "source": "collaboration_session_event",
                "source_path": str(trusted_path),
                "conversation_id": "forged-child",
                "observation_id": "forged-start",
                "model": assignment["model"],
                "agent_type": assignment["agent_type"],
            },
            now=T0 + timedelta(seconds=1),
        )
        with self.assertRaisesRegex(
            (ValueError, PermissionError),
            "attested|structured machine started|observation",
        ):
            _start_bound_web_assignment(
                repo=self.repo,
                registry_path=self.registry,
                controller_id="controller-1",
                conversation_id="forged-child",
                assignment=assignment,
                dispatch_id=prepared["dispatch_id"],
                now=T0 + timedelta(seconds=1),
                health_probe=lambda: True,
                watchdog_launcher=lambda **_: {"launched": False},
            )
        self.assertNotIn(
            assignment["assignment_id"],
            load_runtime_state(self.repo).get("leases", {}),
        )

    def test_route_policy_accepts_normal_whitespace_declaration(self):
        from scripts.route_contract import route_policy_errors
        policy = Path(self.tmp.name) / "spaced-policy.md"
        policy.write_text(
            "frontend provider = chatgpt_web, model = gpt-5.6-sol, auth_mode = host."
            + chr(10),
            encoding="utf-8",
        )
        route = {
            "decision": "default",
            "policy_class": "frontend",
            "provider": "chatgpt_web",
            "model": "gpt-5.6-sol",
            "auth_mode": "host",
            "policy_source": {
                "path": str(policy.resolve()),
                "sha256": __import__("hashlib").sha256(policy.read_bytes()).hexdigest(),
            },
        }
        self.assertEqual(route_policy_errors("T-SPACED", route), [])

    def test_route_policy_rejects_prefixed_fields_comments_and_negative_examples(self):
        from scripts.route_contract import route_policy_errors
        policy = Path(self.tmp.name) / "inactive-policy.md"
        fence = chr(96) * 3
        policy.write_text(
            "# frontend provider=chatgpt_web, model=gpt-5.6-sol, auth_mode=host."
            + chr(10)
            + "frontend disallowed_provider=chatgpt_web, model=gpt-5.6-sol, auth_mode=host."
            + chr(10)
            + "frontend 禁止 provider=chatgpt_web, model=gpt-5.6-sol, auth_mode=host."
            + chr(10)
            + fence
            + chr(10)
            + "frontend provider=chatgpt_web, model=gpt-5.6-sol, auth_mode=host."
            + chr(10)
            + fence
            + chr(10),
            encoding="utf-8",
        )
        route = {
            "decision": "default",
            "policy_class": "frontend",
            "provider": "chatgpt_web",
            "model": "gpt-5.6-sol",
            "auth_mode": "host",
            "policy_source": {
                "path": str(policy.resolve()),
                "sha256": __import__("hashlib").sha256(policy.read_bytes()).hexdigest(),
            },
        }
        self.assertEqual(
            route_policy_errors("T-INACTIVE", route),
            ["T-INACTIVE route is not declared by policy source"],
        )

    def test_route_policy_rejects_inline_comments_negation_hyphen_prefix_and_value_only_class_marker(self):
        from scripts.route_contract import route_policy_errors

        bad_lines = [
            "provider=chatgpt_web, model=gpt-5.6-sol, auth_mode=host.",
            "frontend provider=chatgpt_web, model=gpt-5.6-sol, auth_mode=host. # historical only",
            "frontend must not use provider=chatgpt_web, model=gpt-5.6-sol, auth_mode=host.",
            "frontend must-not use provider=chatgpt_web, model=gpt-5.6-sol, auth_mode=host.",
            "frontend must_not use provider=chatgpt_web, model=gpt-5.6-sol, auth_mode=host.",
            "frontend should-not use provider=chatgpt_web, model=gpt-5.6-sol, auth_mode=host.",
            "frontend may-not use provider=chatgpt_web, model=gpt-5.6-sol, auth_mode=host.",
            "frontend never use provider=chatgpt_web, model=gpt-5.6-sol, auth_mode=host.",
            "frontend backup-provider=chatgpt_web, model=gpt-5.6-sol, auth_mode=host.",
            "frontend must－not use provider=chatgpt_web, model=gpt-5.6-sol, auth_mode=host.",
            "frontend must−not use provider=chatgpt_web, model=gpt-5.6-sol, auth_mode=host.",
            "frontend backup－provider=chatgpt_web, model=gpt-5.6-sol, auth_mode=host.",
            "frontend backup–provider=chatgpt_web, model=gpt-5.6-sol, auth_mode=host.",
            "not frontend: provider=chatgpt_web, model=gpt-5.6-sol, auth_mode=host.",
            "frontend excluded provider=chatgpt_web, model=gpt-5.6-sol, auth_mode=host.",
            "frontend exclude route provider=chatgpt_web, model=gpt-5.6-sol, auth_mode=host.",
            "frontend no route provider=chatgpt_web, model=gpt-5.6-sol, auth_mode=host.",
        ]
        route = {
            "decision": "default",
            "policy_class": "frontend",
            "provider": "chatgpt_web",
            "model": "gpt-5.6-sol",
            "auth_mode": "host",
        }
        for index, line in enumerate(bad_lines):
            with self.subTest(line=line):
                policy = Path(self.tmp.name) / f"inactive-policy-{index}.md"
                policy.write_text(line + chr(10), encoding="utf-8")
                candidate = dict(route)
                candidate["policy_source"] = {
                    "path": str(policy.resolve()),
                    "sha256": __import__("hashlib").sha256(policy.read_bytes()).hexdigest(),
                }
                self.assertEqual(
                    route_policy_errors(f"T-INACTIVE-{index}", candidate),
                    [f"T-INACTIVE-{index} route is not declared by policy source"],
                )

    def test_route_policy_uses_positive_prefix_grammar_not_negative_phrase_allowlist(self):
        from scripts.route_contract import route_policy_errors

        documents = [
            "not frontend: provider=chatgpt_web, model=gpt-5.6-sol, auth_mode=host.",
            "frontend excluded provider=chatgpt_web, model=gpt-5.6-sol, auth_mode=host.",
            "frontend exclude route provider=chatgpt_web, model=gpt-5.6-sol, auth_mode=host.",
            "frontend no route provider=chatgpt_web, model=gpt-5.6-sol, auth_mode=host.",
            "frontend unknown-directive provider=chatgpt_web, model=gpt-5.6-sol, auth_mode=host.",
        ]
        route = {
            "decision": "default",
            "policy_class": "frontend",
            "provider": "chatgpt_web",
            "model": "gpt-5.6-sol",
            "auth_mode": "host",
        }
        for index, line in enumerate(documents):
            with self.subTest(line=line):
                policy = Path(self.tmp.name) / f"negative-prefix-{index}.md"
                policy.write_text(line + chr(10), encoding="utf-8")
                candidate = dict(route)
                candidate["policy_source"] = {
                    "path": str(policy.resolve()),
                    "sha256": __import__("hashlib").sha256(policy.read_bytes()).hexdigest(),
                }
                self.assertEqual(
                    route_policy_errors(f"T-PREFIX-{index}", candidate),
                    [f"T-PREFIX-{index} route is not declared by policy source"],
                )

        for index, line in enumerate(
            [
                "frontend provider=chatgpt_web, model=gpt-5.6-sol, auth_mode=host.",
                "frontend default provider=chatgpt_web, model=gpt-5.6-sol, auth_mode=host.",
                "frontend fallback route provider=chatgpt_web, model=gpt-5.6-sol, auth_mode=host.",
                "前端默认 provider=chatgpt_web、model=gpt-5.6-sol、auth_mode=host。",
            ]
        ):
            with self.subTest(active=line):
                policy = Path(self.tmp.name) / f"positive-prefix-{index}.md"
                policy.write_text(line + chr(10), encoding="utf-8")
                candidate = dict(route)
                candidate["policy_source"] = {
                    "path": str(policy.resolve()),
                    "sha256": __import__("hashlib").sha256(policy.read_bytes()).hexdigest(),
                }
                self.assertEqual(route_policy_errors(f"T-POSITIVE-{index}", candidate), [])

    def test_route_policy_requires_canonical_class_then_single_directive_order(self):
        from scripts.route_contract import route_policy_errors

        bad_lines = [
            "fallback default frontend route route provider=chatgpt_web, model=gpt-5.6-sol, auth_mode=host.",
            "default frontend provider=chatgpt_web, model=gpt-5.6-sol, auth_mode=host.",
            "frontend default fallback provider=chatgpt_web, model=gpt-5.6-sol, auth_mode=host.",
            "frontend route route provider=chatgpt_web, model=gpt-5.6-sol, auth_mode=host.",
            "frontend fallback default provider=chatgpt_web, model=gpt-5.6-sol, auth_mode=host.",
        ]
        route = {
            "decision": "default",
            "policy_class": "frontend",
            "provider": "chatgpt_web",
            "model": "gpt-5.6-sol",
            "auth_mode": "host",
        }
        for index, line in enumerate(bad_lines):
            with self.subTest(line=line):
                policy = Path(self.tmp.name) / f"canonical-prefix-bad-{index}.md"
                policy.write_text(line + chr(10), encoding="utf-8")
                candidate = dict(route)
                candidate["policy_source"] = {
                    "path": str(policy.resolve()),
                    "sha256": __import__("hashlib").sha256(policy.read_bytes()).hexdigest(),
                }
                self.assertEqual(
                    route_policy_errors(f"T-CANON-{index}", candidate),
                    [f"T-CANON-{index} route is not declared by policy source"],
                )

        good_lines = [
            "frontend provider=chatgpt_web, model=gpt-5.6-sol, auth_mode=host.",
            "frontend default provider=chatgpt_web, model=gpt-5.6-sol, auth_mode=host.",
            "frontend default route provider=chatgpt_web, model=gpt-5.6-sol, auth_mode=host.",
            "frontend fallback provider=chatgpt_web, model=gpt-5.6-sol, auth_mode=host.",
            "frontend fallback route provider=chatgpt_web, model=gpt-5.6-sol, auth_mode=host.",
            "前端默认 provider=chatgpt_web、model=gpt-5.6-sol、auth_mode=host。",
        ]
        for index, line in enumerate(good_lines):
            with self.subTest(active=line):
                policy = Path(self.tmp.name) / f"canonical-prefix-good-{index}.md"
                policy.write_text(line + chr(10), encoding="utf-8")
                candidate = dict(route)
                candidate["policy_source"] = {
                    "path": str(policy.resolve()),
                    "sha256": __import__("hashlib").sha256(policy.read_bytes()).hexdigest(),
                }
                self.assertEqual(route_policy_errors(f"T-CANON-POS-{index}", candidate), [])

    def test_route_policy_requires_class_marker_as_first_nonspace_token(self):
        from scripts.route_contract import route_policy_errors

        bad_lines = [
            "- frontend default provider=chatgpt_web, model=gpt-5.6-sol, auth_mode=host.",
            ": frontend default provider=chatgpt_web, model=gpt-5.6-sol, auth_mode=host.",
            "* frontend default provider=chatgpt_web, model=gpt-5.6-sol, auth_mode=host.",
            "1. frontend default provider=chatgpt_web, model=gpt-5.6-sol, auth_mode=host.",
            "• frontend default provider=chatgpt_web, model=gpt-5.6-sol, auth_mode=host.",
            "frontend - default provider=chatgpt_web, model=gpt-5.6-sol, auth_mode=host.",
            "frontend/default provider=chatgpt_web, model=gpt-5.6-sol, auth_mode=host.",
            "frontend (default) provider=chatgpt_web, model=gpt-5.6-sol, auth_mode=host.",
            "frontend default: provider=chatgpt_web, model=gpt-5.6-sol, auth_mode=host.",
            "frontend:: provider=chatgpt_web, model=gpt-5.6-sol, auth_mode=host.",
            "frontend：：默认 provider=chatgpt_web、model=gpt-5.6-sol、auth_mode=host。",
            "前端：：默认 provider=chatgpt_web、model=gpt-5.6-sol、auth_mode=host。",
            "frontend provider=chatgpt:web, model=gpt-5.6-sol, auth_mode=host.",
            "frontend provider=chatgpt_web, model=gpt:5.6-sol, auth_mode=host.",
            "frontend provider=chatgpt_web, model=gpt-5.6-sol, auth_mode=ho:st.",
            "frontend:default: provider=chatgpt_web, model=gpt-5.6-sol, auth_mode=host.",
            "frontend : : default provider=chatgpt_web, model=gpt-5.6-sol, auth_mode=host.",
            "frontend default ： provider=chatgpt_web, model=gpt-5.6-sol, auth_mode=host.",
        ]
        route = {
            "decision": "default",
            "policy_class": "frontend",
            "provider": "chatgpt_web",
            "model": "gpt-5.6-sol",
            "auth_mode": "host",
        }
        for index, line in enumerate(bad_lines):
            with self.subTest(line=line):
                policy = Path(self.tmp.name) / f"leading-punctuation-{index}.md"
                policy.write_text(line + chr(10), encoding="utf-8")
                candidate = dict(route)
                candidate["policy_source"] = {
                    "path": str(policy.resolve()),
                    "sha256": __import__("hashlib").sha256(policy.read_bytes()).hexdigest(),
                }
                self.assertEqual(
                    route_policy_errors(f"T-LEAD-{index}", candidate),
                    [f"T-LEAD-{index} route is not declared by policy source"],
                )

        good_lines = [
            "frontend: default provider=chatgpt_web, model=gpt-5.6-sol, auth_mode=host.",
            "前端：默认 provider=chatgpt_web、model=gpt-5.6-sol、auth_mode=host。",
            "frontend:default provider=chatgpt_web, model=gpt-5.6-sol, auth_mode=host.",
            "frontend: provider=chatgpt_web, model=gpt-5.6-sol, auth_mode=host.",
            "前端默认 provider=chatgpt_web、model=gpt-5.6-sol、auth_mode=host。",
        ]
        for index, line in enumerate(good_lines):
            with self.subTest(active=line):
                policy = Path(self.tmp.name) / f"leading-punctuation-good-{index}.md"
                policy.write_text(line + chr(10), encoding="utf-8")
                candidate = dict(route)
                candidate["policy_source"] = {
                    "path": str(policy.resolve()),
                    "sha256": __import__("hashlib").sha256(policy.read_bytes()).hexdigest(),
                }
                self.assertEqual(route_policy_errors(f"T-LEAD-POS-{index}", candidate), [])

    def test_route_policy_full_line_grammar_rejects_intervening_and_trailing_semantics(self):
        from scripts.route_contract import route_policy_errors

        bad_lines = [
            "frontend provider=chatgpt_web not model=gpt-5.6-sol auth_mode=host.",
            "frontend provider=chatgpt_web model=gpt-5.6-sol auth_mode=host excluded",
            "frontend provider=chatgpt_web arbitrary model=gpt-5.6-sol auth_mode=host.",
            "frontend provider=chatgpt_web model=gpt-5.6-sol unexpected auth_mode=host.",
            "frontend provider=chatgpt_web model=gpt-5.6-sol auth_mode=host unknown-directive",
        ]
        route = {
            "decision": "default",
            "policy_class": "frontend",
            "provider": "chatgpt_web",
            "model": "gpt-5.6-sol",
            "auth_mode": "host",
        }
        for index, line in enumerate(bad_lines):
            with self.subTest(line=line):
                policy = Path(self.tmp.name) / f"full-line-negative-{index}.md"
                policy.write_text(line + chr(10), encoding="utf-8")
                candidate = dict(route)
                candidate["policy_source"] = {
                    "path": str(policy.resolve()),
                    "sha256": __import__("hashlib").sha256(policy.read_bytes()).hexdigest(),
                }
                self.assertEqual(
                    route_policy_errors(f"T-FULL-{index}", candidate),
                    [f"T-FULL-{index} route is not declared by policy source"],
                )

        good_lines = [
            "frontend provider=chatgpt_web model=gpt-5.6-sol auth_mode=host.",
            "frontend default provider=chatgpt_web, model=gpt-5.6-sol, auth_mode=host.",
            "前端默认 provider=chatgpt_web、model=gpt-5.6-sol、auth_mode=host。",
        ]
        for index, line in enumerate(good_lines):
            with self.subTest(active=line):
                policy = Path(self.tmp.name) / f"full-line-positive-{index}.md"
                policy.write_text(line + chr(10), encoding="utf-8")
                candidate = dict(route)
                candidate["policy_source"] = {
                    "path": str(policy.resolve()),
                    "sha256": __import__("hashlib").sha256(policy.read_bytes()).hexdigest(),
                }
                self.assertEqual(route_policy_errors(f"T-FULL-POS-{index}", candidate), [])

    def test_route_policy_normalizes_unicode_dash_variants_before_authorization(self):
        from scripts.route_contract import UNICODE_DASH_EQUIVALENTS, route_policy_errors

        route = {
            "decision": "default",
            "policy_class": "frontend",
            "provider": "chatgpt_web",
            "model": "gpt-5.6-sol",
            "auth_mode": "host",
        }
        for index, dash in enumerate(UNICODE_DASH_EQUIVALENTS):
            for variant, line in (
                ("negative", f"frontend must{dash}not use provider=chatgpt_web, model=gpt-5.6-sol, auth_mode=host."),
                ("prefixed", f"frontend backup{dash}provider=chatgpt_web, model=gpt-5.6-sol, auth_mode=host."),
            ):
                with self.subTest(dash=ord(dash), variant=variant):
                    policy = Path(self.tmp.name) / f"unicode-dash-{index}-{variant}.md"
                    policy.write_text(line + chr(10), encoding="utf-8")
                    candidate = dict(route)
                    candidate["policy_source"] = {
                        "path": str(policy.resolve()),
                        "sha256": __import__("hashlib").sha256(policy.read_bytes()).hexdigest(),
                    }
                    self.assertEqual(
                        route_policy_errors(f"T-DASH-{index}-{variant}", candidate),
                        [f"T-DASH-{index}-{variant} route is not declared by policy source"],
                    )

    def test_route_policy_rejects_tilde_fenced_route_examples(self):
        from scripts.route_contract import route_policy_errors

        fence = "~" * 3
        inline = chr(96)
        bad_documents = [
            fence
            + "text"
            + chr(10)
            + "frontend provider=chatgpt_web, model=gpt-5.6-sol, auth_mode=host."
            + chr(10)
            + fence
            + chr(10),
            "<!--"
            + chr(10)
            + "frontend provider=chatgpt_web, model=gpt-5.6-sol, auth_mode=host."
            + chr(10)
            + "-->"
            + chr(10),
            "    frontend provider=chatgpt_web, model=gpt-5.6-sol, auth_mode=host."
            + chr(10),
            "frontend "
            + inline
            + "provider=chatgpt_web, model=gpt-5.6-sol, auth_mode=host"
            + inline
            + chr(10),
        ]
        route = {
            "decision": "default",
            "policy_class": "frontend",
            "provider": "chatgpt_web",
            "model": "gpt-5.6-sol",
            "auth_mode": "host",
        }
        for index, text in enumerate(bad_documents):
            with self.subTest(index=index):
                policy = Path(self.tmp.name) / f"code-example-{index}.md"
                policy.write_text(text, encoding="utf-8")
                candidate = dict(route)
                candidate["policy_source"] = {
                    "path": str(policy.resolve()),
                    "sha256": __import__("hashlib").sha256(policy.read_bytes()).hexdigest(),
                }
                self.assertEqual(
                    route_policy_errors(f"T-CODE-{index}", candidate),
                    [f"T-CODE-{index} route is not declared by policy source"],
                )

    def test_route_policy_accepts_explicit_web_class_marker_before_fields(self):
        from scripts.route_contract import route_policy_errors
        policy = Path(self.tmp.name) / "web-class-policy.md"
        policy.write_text(
            "Web 默认 provider = chatgpt_web, model = gpt-5.6-sol, auth_mode = host."
            + chr(10),
            encoding="utf-8",
        )
        route = {
            "decision": "default",
            "policy_class": "frontend",
            "provider": "chatgpt_web",
            "model": "gpt-5.6-sol",
            "auth_mode": "host",
            "policy_source": {
                "path": str(policy.resolve()),
                "sha256": __import__("hashlib").sha256(policy.read_bytes()).hexdigest(),
            },
        }
        self.assertEqual(route_policy_errors("T-WEB-CLASS", route), [])

    def test_route_policy_accepts_active_rule_after_inactive_examples(self):
        from scripts.route_contract import route_policy_errors
        policy = Path(self.tmp.name) / "active-policy.md"
        policy.write_text(
            "frontend disallowed_provider=chatgpt_web, model=gpt-5.6-sol, auth_mode=host."
            + chr(10)
            + "frontend 默认 provider = chatgpt_web, model = gpt-5.6-sol, auth_mode = host."
            + chr(10),
            encoding="utf-8",
        )
        route = {
            "decision": "default",
            "policy_class": "frontend",
            "provider": "chatgpt_web",
            "model": "gpt-5.6-sol",
            "auth_mode": "host",
            "policy_source": {
                "path": str(policy.resolve()),
                "sha256": __import__("hashlib").sha256(policy.read_bytes()).hexdigest(),
            },
        }
        self.assertEqual(route_policy_errors("T-ACTIVE", route), [])

    def test_dispatch_start_needs_prepared_verified_observation(self):
        from scripts.web_agent_execution import start_web_assignment
        with self.assertRaisesRegex(PermissionError, "direct Web start"):
            start_web_assignment(
                repo=self.repo, registry_path=self.registry, controller_id="controller-1",
                conversation_id="conv-runtime-1", assignment=self.assignment(), now=T0,
                watchdog_launcher=self._launch,
            )

    def test_runtime_records_exact_web_agent_model_and_type(self):
        result = bind_recovery_attempt(
            repo=self.repo,
            registry=self.registry,
            controller_id="controller-1",
            assignment=self.assignment(),
            conversation_id="conv-runtime-1",
            task_name="runtime-model-type",
            at=T0,
        )
        lease = load_runtime_state(self.repo)["leases"]["A-RUNTIME"]
        self.assertEqual(result["conversation_id"], "conv-runtime-1")
        self.assertEqual(lease["model"], "gpt-5.6-sol")
        self.assertEqual(lease["agent_type"], "default")

    def test_prepared_dispatch_rejects_spawn_model_or_agent_type_mismatch_before_start(self):
        from scripts.web_agent_execution import prepare_web_assignment_dispatch, require_prepared_web_dispatch
        prepared = prepare_web_assignment_dispatch(
            repo=self.repo,
            registry_path=self.registry,
            controller_id="controller-1",
            task_name="runtime-pretool-match",
            assignment=self.assignment(),
            now=T0,
            health_probe=lambda: True,
            event_source_probe=lambda: True,
        )
        self.assertEqual(prepared["task_name"], "runtime-pretool-match")
        with self.assertRaisesRegex(PermissionError, "model"):
            require_prepared_web_dispatch(
                repo=self.repo,
                controller_id="controller-1",
                task_name="runtime-pretool-match",
                expected_model="gpt-5.6-luna",
                expected_agent_type="default",
                health_probe=lambda: True,
                event_source_probe=lambda: True,
            )
        with self.assertRaisesRegex(PermissionError, "agent_type"):
            require_prepared_web_dispatch(
                repo=self.repo,
                controller_id="controller-1",
                task_name="runtime-pretool-match",
                expected_model="gpt-5.6-sol",
                expected_agent_type="reviewer",
                health_probe=lambda: True,
                event_source_probe=lambda: True,
            )

    def test_machine_observed_spawn_must_match_prepared_model_and_agent_type(self):
        with self.assertRaisesRegex(PermissionError, "model does not match"):
            bind_recovery_attempt(
                repo=self.repo, registry=self.registry, controller_id="controller-1",
                assignment=self.assignment(), conversation_id="conv-runtime-mismatch",
                task_name="runtime-mismatch", at=T0 + timedelta(seconds=2),
                observed_model="gpt-5.6-luna",
            )

    def test_watchdog_observes_git_progress_without_any_host_event(self):
        from scripts.web_agent_execution import watch_web_assignment_once
        bind_recovery_attempt(
            repo=self.repo, registry=self.registry, controller_id="controller-1",
            assignment=self.assignment(), conversation_id="conv-runtime-1",
            task_name="runtime-watch-progress", at=T0,
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
        from scripts.web_agent_execution import watch_web_assignment_once
        bind_recovery_attempt(
            repo=self.repo, registry=self.registry, controller_id="controller-1",
            assignment=self.assignment(), conversation_id="conv-runtime-1",
            task_name="runtime-watch-stale", at=T0,
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
        bind_recovery_attempt(
            repo=self.repo, registry=self.registry, controller_id="controller-1",
            assignment=self.assignment(), conversation_id="conv-runtime-1",
            task_name="runtime-pid-not-terminal", at=T0,
        )
        lease = load_runtime_state(self.repo)["leases"]["A-RUNTIME"]
        lease["pid"] = 12345
        decision = evaluate_lease(
            lease, now=T0 + timedelta(minutes=5), process_probe=lambda _pid: False
        )
        self.assertEqual(decision, {"state": "healthy", "reason": "runtime_evidence_current"})

    def test_recovery_derives_next_attempt_and_new_lease_from_unhealthy_canonical_lease(self):
        bind_recovery_attempt(
            repo=self.repo, registry=self.registry, controller_id="controller-1",
            assignment=self.assignment(), conversation_id="conv-runtime-1",
            task_name="runtime-recovery-base", at=T0,
        )
        result = bind_recovery_attempt(
            repo=self.repo, registry=self.registry, controller_id="controller-1",
            assignment=self.assignment(), conversation_id="conv-runtime-2",
            task_name="runtime-recover-2", at=T0 + timedelta(minutes=26),
        )
        lease = load_runtime_state(self.repo)["leases"]["A-RUNTIME"]
        self.assertEqual(result["attempt"], 2)
        self.assertEqual(lease["attempt"], 2)
        self.assertEqual(lease["session_id"], "conv-runtime-2")
        self.assertNotEqual(lease["lease_id"], "A-RUNTIME:web:attempt:1")
        self.assertEqual(lease["recovery_count"], 1)
        self.assertEqual(lease["route_decision"], "default")
        self.assertEqual(lease["policy_class"], "general")
        self.assertEqual(lease["route_contract"], self.assignment()["route"])

    def test_unknown_side_effect_timeout_fails_closed_even_with_stable_key(self):
        from scripts.web_agent_execution import recover_web_assignment
        bind_recovery_attempt(
            repo=self.repo, registry=self.registry, controller_id="controller-1",
            assignment=self.assignment(side_effect=True, idempotency_key="publish:42"),
            conversation_id="conv-runtime-1", task_name="runtime-side-effect-base", at=T0,
        )
        with self.assertRaisesRegex(ValueError, "unknown side effect"):
            recover_web_assignment(
                repo=self.repo, registry_path=self.registry, controller_id="controller-1",
                assignment_id="A-RUNTIME", conversation_id="conv-runtime-2",
                now=T0 + timedelta(minutes=26), watchdog_launcher=self._launch,
            )
        lease = load_runtime_state(self.repo)["leases"]["A-RUNTIME"]
        self.assertEqual(lease["attempt"], 1)
        self.assertTrue(lease["result_unknown"])

    def test_message_delivery_timeout_retries_same_controller_wake_boundedly(self):
        from scripts.web_agent_execution import watch_web_assignment
        now = datetime.now(UTC)
        bind_recovery_attempt(
            repo=self.repo, registry=self.registry, controller_id="controller-1",
            assignment=self.assignment(), conversation_id="conv-runtime-1",
            task_name="runtime-message-timeout", at=now - timedelta(minutes=26),
        )
        calls = []
        def flaky_wake(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise RuntimeError("Message delivery timed out")
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
        bind_recovery_attempt(
            repo=self.repo, registry=self.registry, controller_id="controller-1",
            assignment=self.assignment(), conversation_id="conv-runtime-1",
            task_name="runtime-ui-interruption", at=T0,
        )
        with self.assertRaisesRegex(ValueError, "not authoritative terminal"):
            verified_web_execution_event(
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



class StructuredCollaborationTerminalTests(unittest.TestCase):
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
        self.registry.write_text(
            json.dumps({"controller-1": str(self.repo.resolve())}),
            encoding="utf-8",
        )
        self.policy = root / "AGENTS.md"
        self.policy.write_text(
            "general 默认 provider=chatgpt_web、model=gpt-5.6-sol、auth_mode=host。\n",
            encoding="utf-8",
        )
        self.terminal_events = root / "terminal-events.jsonl"
        self._machine_source_patcher = patch(
            "scripts.web_agent_execution._machine_event_source_context",
            return_value={
                "ready": True,
                "reason": "test_host_attested",
                "event_paths": [str(self.terminal_events.resolve())],
            },
        )
        self._machine_source_patcher.start()
        self.addCleanup(self._machine_source_patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def assignment(self, **extra):
        value = {
            "assignment_id": "A-COLLAB",
            "task_id": "T-COLLAB",
            "agent_id": "writer-collab",
            "provider": "chatgpt_web",
            "model": "gpt-5.6-sol",
            "agent_type": "default",
            "worktree": str(self.repo),
            "primary_goal": "finish child task",
            "success_criteria": ["child completes"],
            "owned_scope": ["README.md"],
            "strategy": "collaboration:gpt-5.6-sol",
            "assignment_contract_version": 2,
            "side_effect": False,
            "role": "writer",
            "route": {
                "decision": "default",
                "policy_class": "general",
                "provider": "chatgpt_web",
                "model": "gpt-5.6-sol",
                "auth_mode": "host",
                "policy_source": {
                    "path": str(self.policy.resolve()),
                    "sha256": __import__("hashlib").sha256(
                        self.policy.read_bytes()
                    ).hexdigest(),
                },
            },
        }
        value.update(extra)
        return value

    def start(self, **extra):
        return bind_recovery_attempt(
            repo=self.repo,
            registry=self.registry,
            controller_id="controller-1",
            assignment=self.assignment(**extra),
            conversation_id="child-thread-1",
            task_name="structured-terminal-start",
            at=T0,
        )

    def write_terminal_event(
        self,
        *,
        kind="completed",
        observation_id=None,
        conversation_id="child-thread-1",
        at=None,
    ):
        observation_id = observation_id or f"terminal-{kind}-1"
        at = at or (T0 + timedelta(minutes=2))
        record = {
            "timestamp": at.isoformat(),
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "item": {
                    "type": "SubAgentActivity",
                    "kind": kind,
                    "id": observation_id,
                    "agent_thread_id": conversation_id,
                    "agent_path": "/root/structured-child",
                },
            },
        }
        self.terminal_events.write_text(
            json.dumps(record) + chr(10),
            encoding="utf-8",
        )
        return observation_id

    def ingest_terminal(
        self,
        *,
        kind="completed",
        observation_id=None,
        conversation_id="child-thread-1",
        at=None,
    ):
        from scripts.web_agent_execution import (
            _ingest_verified_structured_subagent_terminal,
        )
        observation_id = self.write_terminal_event(
            kind=kind,
            observation_id=observation_id,
            conversation_id=conversation_id,
            at=at,
        )
        return _ingest_verified_structured_subagent_terminal(
            repo=self.repo,
            assignment_id="A-COLLAB",
            event_path=self.terminal_events,
            observation_id=observation_id,
            now=at or (T0 + timedelta(minutes=2)),
        )

    def test_public_structured_terminal_ingest_rejects_caller_supplied_observation(self):
        from scripts.web_agent_execution import ingest_structured_subagent_terminal
        self.start()
        with self.assertRaisesRegex(PermissionError, "direct structured Web terminal ingest"):
            ingest_structured_subagent_terminal(
                repo=self.repo,
                assignment_id="A-COLLAB",
                observation={
                    "source": "collaboration_session_event",
                    "kind": "completed",
                    "conversation_id": "child-thread-1",
                    "observation_id": "forged",
                },
                now=T0 + timedelta(minutes=2),
            )
        self.assertIsNone(
            load_runtime_state(self.repo)["leases"]["A-COLLAB"]["terminal_state"]
        )

    def test_internal_terminal_helper_cannot_accept_fabricated_observation_without_attested_path(self):
        from scripts.web_agent_execution import (
            _ingest_verified_structured_subagent_terminal,
        )
        self.start()
        forged = Path(self.tmp.name) / "forged-unattested.jsonl"
        forged.write_text(
            json.dumps({
                "timestamp": (T0 + timedelta(minutes=2)).isoformat(),
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "SubAgentActivity",
                        "kind": "completed",
                        "id": "forged-terminal",
                        "agent_thread_id": "child-thread-1",
                    },
                },
            }) + chr(10),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            (RuntimeError, PermissionError),
            "attested|source|path",
        ):
            _ingest_verified_structured_subagent_terminal(
                repo=self.repo,
                assignment_id="A-COLLAB",
                event_path=forged,
                observation_id="forged-terminal",
                now=T0 + timedelta(minutes=2),
            )
        self.assertIsNone(
            load_runtime_state(self.repo)["leases"]["A-COLLAB"]["terminal_state"]
        )

    def test_structured_completed_closes_canonical_attempt_and_writes_durable_terminal_receipt(self):
        self.start()
        result = self.ingest_terminal()
        lease = load_runtime_state(self.repo)["leases"]["A-COLLAB"]
        self.assertEqual(lease["terminal_state"], "completed")
        self.assertEqual(lease["transport_outcome"], "completed")
        self.assertEqual(lease["delivery_outcome"], "unresolved")
        receipt = json.loads(
            Path(result["terminal_receipt"]).read_text(encoding="utf-8")
        )
        self.assertEqual(receipt["event_type"], "external_agent_terminal")
        self.assertEqual(receipt["assignment_id"], "A-COLLAB")
        self.assertEqual(receipt["session_id"], "child-thread-1")
        self.assertEqual(receipt["attempt"], 1)
        self.assertEqual(receipt["lease_id"], lease["lease_id"])
        self.assertEqual(
            receipt["machine_terminal_observation_id"],
            "terminal-completed-1",
        )
        self.assertEqual(receipt["model"], "gpt-5.6-sol")
        self.assertEqual(receipt["agent_type"], "default")

    def test_duplicate_structured_terminal_reuses_same_receipt_without_reopening_attempt(self):
        self.start()
        first = self.ingest_terminal(observation_id="terminal-completed-1")
        second = self.ingest_terminal(
            observation_id="terminal-completed-1",
            at=T0 + timedelta(minutes=3),
        )
        lease = load_runtime_state(self.repo)["leases"]["A-COLLAB"]
        self.assertEqual(first["terminal_receipt"], second["terminal_receipt"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(lease["attempt"], 1)
        self.assertEqual(lease["terminal_state"], "completed")

    def test_structured_terminal_must_match_machine_observed_child_session(self):
        self.start()
        with self.assertRaisesRegex(PermissionError, "Host-attested machine observation"):
            self.ingest_terminal(conversation_id="other-child")
        self.assertIsNone(
            load_runtime_state(self.repo)["leases"]["A-COLLAB"]["terminal_state"]
        )

    def test_structured_disconnected_is_terminal_but_ui_interruption_text_is_not_used(self):
        self.start()
        result = self.ingest_terminal(kind="disconnected")
        lease = load_runtime_state(self.repo)["leases"]["A-COLLAB"]
        self.assertEqual(lease["terminal_state"], "disconnected")
        self.assertEqual(lease["transport_outcome"], "failed")
        self.assertIn("terminal_receipt", result)

class WebSessionDelegationAuthorityTests(WebAgentExecutionTests):
    def test_cli_prepare_forwards_session_delegation_owner(self):
        from scripts import web_agent_execution as execution
        event = {
            "delegator_session_id": "ordinary-web-session-1",
            "task_name": "ordinary-cli-child",
            "assignment": {"assignment_id": "A-CLI"},
        }
        with patch.object(execution, "prepare_web_assignment_dispatch", return_value={"dispatch_id": "D-CLI"}) as prepare, \
             patch.object(sys, "stdin", StringIO(json.dumps(event))), \
             patch.object(sys, "stdout", new_callable=StringIO):
            code = execution.main(["prepare", "--repo", str(self.repo), "--registry", str(self.registry)])
        self.assertEqual(code, 0)
        prepare.assert_called_once_with(
            repo=str(self.repo),
            registry_path=str(self.registry),
            controller_id="",
            delegator_session_id="ordinary-web-session-1",
            task_name="ordinary-cli-child",
            assignment={"assignment_id": "A-CLI"},
        )

    def test_cli_recover_forwards_session_delegation_owner(self):
        from scripts import web_agent_execution as execution
        event = {
            "delegator_session_id": "ordinary-web-session-1",
            "assignment_id": "A-CLI",
            "conversation_id": "child-conversation-1",
        }
        with patch.object(execution, "recover_web_assignment", return_value={"assignment_id": "A-CLI"}) as recover, \
             patch.object(sys, "stdin", StringIO(json.dumps(event))), \
             patch.object(sys, "stdout", new_callable=StringIO):
            code = execution.main(["recover", "--repo", str(self.repo), "--registry", str(self.registry)])
        self.assertEqual(code, 0)
        recover.assert_called_once_with(
            repo=str(self.repo),
            registry_path=str(self.registry),
            controller_id="",
            delegator_session_id="ordinary-web-session-1",
            assignment_id="A-CLI",
            conversation_id="child-conversation-1",
        )

    def test_cli_bind_forwards_session_delegation_owner(self):
        from scripts import web_agent_execution as execution
        event = {
            "delegator_session_id": "ordinary-web-session-1",
            "dispatch_id": "D-CLI",
            "event_paths": ["/tmp/web-events.jsonl"],
        }
        with patch.object(execution, "bind_web_assignment_dispatch", return_value={"dispatch_id": "D-CLI"}) as bind, \
             patch.object(sys, "stdin", StringIO(json.dumps(event))), \
             patch.object(sys, "stdout", new_callable=StringIO):
            code = execution.main(["bind", "--repo", str(self.repo), "--registry", str(self.registry)])
        self.assertEqual(code, 0)
        bind.assert_called_once_with(
            repo=str(self.repo),
            registry_path=str(self.registry),
            controller_id="",
            delegator_session_id="ordinary-web-session-1",
            dispatch_id="D-CLI",
            event_paths=["/tmp/web-events.jsonl"],
        )

    def test_non_controller_web_session_can_prepare_canonical_child_dispatch(self):
        from scripts.web_agent_execution import prepare_web_assignment_dispatch
        trusted = {
            "ready": True,
            "reason": "test_host_attested",
            "event_paths": [str((Path(self.tmp.name) / "session-host-events.jsonl").resolve())],
        }
        with patch("scripts.web_agent_execution._machine_event_source_context", return_value=trusted):
            prepared = prepare_web_assignment_dispatch(
                repo=self.repo,
                registry_path=self.registry,
                controller_id=None,
                delegator_session_id="ordinary-web-session-1",
                task_name="ordinary-session-child",
                assignment=self.assignment(assignment_id="A-SESSION", task_id="T-SESSION", lease_id="A-SESSION:web:attempt:1"),
                now=T0,
                health_probe=lambda: True,
            )
        self.assertEqual(prepared["delegation_owner_kind"], "session")
        self.assertEqual(prepared["delegation_owner_id"], "ordinary-web-session-1")
        self.assertIsNone(prepared.get("controller_id"))

    def test_non_controller_session_can_bind_machine_observed_child_without_controller_identity(self):
        from scripts.web_agent_execution import prepare_web_assignment_dispatch, bind_web_assignment_dispatch
        event_path = Path(self.tmp.name) / "ordinary-session-bind.jsonl"
        trusted = {"ready": True, "reason": "test_host_attested", "event_paths": [str(event_path.resolve())]}
        with patch("scripts.web_agent_execution._machine_event_source_context", return_value=trusted):
            prepared = prepare_web_assignment_dispatch(
                repo=self.repo, registry_path=self.registry, controller_id=None,
                delegator_session_id="ordinary-web-session-1", task_name="ordinary-bind",
                assignment=self.assignment(), now=T0, health_probe=lambda: True,
            )
            event_path.write_text(chr(10).join([
                json.dumps({"timestamp": T0.isoformat(), "type": "response_item", "payload": {
                    "type": "function_call", "namespace": "collaboration", "name": "spawn_agent",
                    "call_id": "spawn-ordinary", "arguments": json.dumps({"task_name": "ordinary-bind", "agent_type": "default", "model": "gpt-5.6-sol"})}}),
                json.dumps({"timestamp": (T0 + timedelta(seconds=1)).isoformat(), "type": "event_msg", "payload": {
                    "type": "item_completed", "item": {"type": "SubAgentActivity", "kind": "started",
                    "id": "spawn-ordinary", "agent_thread_id": "ordinary-child-1", "agent_path": "/ordinary/child"}}})
            ]) + chr(10), encoding="utf-8")
            result = bind_web_assignment_dispatch(
                repo=self.repo, registry_path=self.registry, controller_id=None,
                delegator_session_id="ordinary-web-session-1", dispatch_id=prepared["dispatch_id"],
                event_paths=[event_path], now=T0 + timedelta(seconds=1), health_probe=lambda: True,
                watchdog_launcher=lambda **_: {"launched": False},
            )
        self.assertEqual(result["delegation_owner_kind"], "session")
        self.assertEqual(result["delegation_owner_id"], "ordinary-web-session-1")
        lease = load_runtime_state(self.repo)["leases"]["A-1"]
        self.assertEqual(lease["delegation_owner_kind"], "session")
        self.assertEqual(lease["delegation_owner_id"], "ordinary-web-session-1")

    def test_session_delegation_spawn_requires_same_parent_session(self):
        from scripts.web_agent_execution import prepare_web_assignment_dispatch, require_prepared_web_dispatch
        trusted = {
            "ready": True,
            "reason": "test_host_attested",
            "event_paths": [str((Path(self.tmp.name) / "session-host-events-2.jsonl").resolve())],
        }
        with patch("scripts.web_agent_execution._machine_event_source_context", return_value=trusted):
            prepare_web_assignment_dispatch(
                repo=self.repo,
                registry_path=self.registry,
                controller_id=None,
                delegator_session_id="ordinary-web-session-1",
                task_name="ordinary-session-child-2",
                assignment=self.assignment(assignment_id="A-SESSION-2", task_id="T-SESSION-2", lease_id="A-SESSION-2:web:attempt:1"),
                now=T0,
                health_probe=lambda: True,
            )
        with patch("scripts.web_agent_execution._machine_event_source_context", return_value=trusted):
            with self.assertRaisesRegex(PermissionError, "delegation owner"):
                require_prepared_web_dispatch(
                    repo=self.repo,
                    controller_id=None,
                    delegator_session_id="ordinary-web-session-2",
                    task_name="ordinary-session-child-2",
                    expected_model="gpt-5.6-sol",
                    expected_agent_type="default",
                    health_probe=lambda: True,
                    event_source_probe=lambda: True,
                )
            allowed = require_prepared_web_dispatch(
                repo=self.repo,
                controller_id=None,
                delegator_session_id="ordinary-web-session-1",
                task_name="ordinary-session-child-2",
                expected_model="gpt-5.6-sol",
                expected_agent_type="default",
                health_probe=lambda: True,
                event_source_probe=lambda: True,
            )
        self.assertEqual(allowed["delegation_owner_id"], "ordinary-web-session-1")

    def test_session_owned_unhealthy_watch_returns_to_parent_without_controller_continuation(self):
        from scripts.web_agent_execution import prepare_web_assignment_dispatch, bind_web_assignment_dispatch, watch_web_assignment_once
        event_path = Path(self.tmp.name) / "ordinary-session-unhealthy.jsonl"
        trusted = {"ready": True, "reason": "test_host_attested", "event_paths": [str(event_path.resolve())]}
        assignment = self.assignment(progress_deadline_minutes=1)
        with patch("scripts.web_agent_execution._machine_event_source_context", return_value=trusted):
            prepared = prepare_web_assignment_dispatch(
                repo=self.repo, registry_path=self.registry, controller_id=None,
                delegator_session_id="ordinary-web-session-1", task_name="ordinary-unhealthy",
                assignment=assignment, now=T0, health_probe=lambda: True,
            )
            event_path.write_text(chr(10).join([
                json.dumps({"timestamp": T0.isoformat(), "type": "response_item", "payload": {
                    "type": "function_call", "namespace": "collaboration", "name": "spawn_agent",
                    "call_id": "spawn-unhealthy", "arguments": json.dumps({"task_name": "ordinary-unhealthy", "agent_type": "default", "model": "gpt-5.6-sol"})}}),
                json.dumps({"timestamp": (T0 + timedelta(seconds=1)).isoformat(), "type": "event_msg", "payload": {
                    "type": "item_completed", "item": {"type": "SubAgentActivity", "kind": "started",
                    "id": "spawn-unhealthy", "agent_thread_id": "ordinary-child-unhealthy", "agent_path": "/ordinary/unhealthy"}}})
            ]) + chr(10), encoding="utf-8")
            bound = bind_web_assignment_dispatch(
                repo=self.repo, registry_path=self.registry, controller_id=None,
                delegator_session_id="ordinary-web-session-1", dispatch_id=prepared["dispatch_id"],
                event_paths=[event_path], now=T0 + timedelta(seconds=1), health_probe=lambda: True,
                watchdog_launcher=lambda **_: {"launched": False},
            )
        continuation_calls = []
        watched = watch_web_assignment_once(
            repo=self.repo, registry_path=self.registry, assignment_id="A-1",
            expected_attempt=bound["attempt"], expected_lease_id=bound["lease_id"],
            now=T0 + timedelta(minutes=46),
            runtime_change_consumer=lambda **kwargs: continuation_calls.append(kwargs) or {"unexpected": True},
        )
        self.assertIn(watched["runtime_state"], {"unhealthy", "budget_exhausted"})
        self.assertEqual(continuation_calls, [])
        self.assertEqual(watched["delegation_parent_session_id"], "ordinary-web-session-1")
        self.assertNotIn("runtime_continuation", watched)

    def test_session_owned_terminal_returns_to_parent_without_controller_continuation(self):
        from scripts.web_agent_execution import (
            prepare_web_assignment_dispatch, bind_web_assignment_dispatch,
            _ingest_verified_structured_subagent_terminal, watch_web_assignment_once,
        )
        event_path = Path(self.tmp.name) / "ordinary-session-terminal.jsonl"
        trusted = {"ready": True, "reason": "test_host_attested", "event_paths": [str(event_path.resolve())]}
        assignment = self.assignment()
        with patch("scripts.web_agent_execution._machine_event_source_context", return_value=trusted):
            prepared = prepare_web_assignment_dispatch(
                repo=self.repo, registry_path=self.registry, controller_id=None,
                delegator_session_id="ordinary-web-session-1", task_name="ordinary-terminal",
                assignment=assignment, now=T0, health_probe=lambda: True,
            )
            started = {"timestamp": T0.isoformat(), "type": "response_item", "payload": {
                "type": "function_call", "namespace": "collaboration", "name": "spawn_agent",
                "call_id": "spawn-terminal", "arguments": json.dumps({"task_name": "ordinary-terminal", "agent_type": "default", "model": "gpt-5.6-sol"})}}
            child_started = {"timestamp": (T0 + timedelta(seconds=1)).isoformat(), "type": "event_msg", "payload": {
                "type": "item_completed", "item": {"type": "SubAgentActivity", "kind": "started",
                "id": "spawn-terminal", "agent_thread_id": "ordinary-child-terminal", "agent_path": "/ordinary/terminal"}}}
            event_path.write_text(json.dumps(started) + chr(10) + json.dumps(child_started) + chr(10), encoding="utf-8")
            bound = bind_web_assignment_dispatch(
                repo=self.repo, registry_path=self.registry, controller_id=None,
                delegator_session_id="ordinary-web-session-1", dispatch_id=prepared["dispatch_id"],
                event_paths=[event_path], now=T0 + timedelta(seconds=1), health_probe=lambda: True,
                watchdog_launcher=lambda **_: {"launched": False},
            )
            terminal = {"timestamp": (T0 + timedelta(minutes=2)).isoformat(), "type": "event_msg", "payload": {
                "type": "item_completed", "item": {"type": "SubAgentActivity", "kind": "completed",
                "id": "terminal-ordinary", "agent_thread_id": "ordinary-child-terminal", "agent_path": "/ordinary/terminal"}}}
            with event_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(terminal) + chr(10))
            terminal_result = _ingest_verified_structured_subagent_terminal(
                repo=self.repo, assignment_id="A-1", event_path=event_path,
                observation_id="terminal-ordinary", now=T0 + timedelta(minutes=2),
            )
        receipt = json.loads(Path(terminal_result["terminal_receipt"]).read_text(encoding="utf-8"))
        self.assertEqual(receipt["delegation_owner_kind"], "session")
        self.assertEqual(receipt["delegation_owner_id"], "ordinary-web-session-1")
        continuation_calls = []
        watched = watch_web_assignment_once(
            repo=self.repo, registry_path=self.registry, assignment_id="A-1",
            expected_attempt=bound["attempt"], expected_lease_id=bound["lease_id"],
            now=T0 + timedelta(minutes=3),
            runtime_change_consumer=lambda **kwargs: continuation_calls.append(kwargs) or {"unexpected": True},
        )
        self.assertEqual(continuation_calls, [])
        self.assertEqual(watched["delegation_parent_session_id"], "ordinary-web-session-1")
        self.assertNotIn("runtime_continuation", watched)

    def test_session_owned_recovery_keeps_same_delegation_lineage(self):
        from scripts.web_agent_execution import prepare_web_assignment_dispatch, bind_web_assignment_dispatch
        event_path = Path(self.tmp.name) / "ordinary-session-recovery.jsonl"
        trusted = {"ready": True, "reason": "test_host_attested", "event_paths": [str(event_path.resolve())]}
        assignment = self.assignment()

        def write_started(task_name, call_id, child_id, at):
            records = [
                {"timestamp": at.isoformat(), "type": "response_item", "payload": {
                    "type": "function_call", "namespace": "collaboration", "name": "spawn_agent",
                    "call_id": call_id, "arguments": json.dumps({"task_name": task_name, "agent_type": "default", "model": "gpt-5.6-sol"})}},
                {"timestamp": (at + timedelta(seconds=1)).isoformat(), "type": "event_msg", "payload": {
                    "type": "item_completed", "item": {"type": "SubAgentActivity", "kind": "started",
                    "id": call_id, "agent_thread_id": child_id, "agent_path": f"/ordinary/{child_id}"}}},
            ]
            event_path.write_text(chr(10).join(json.dumps(record) for record in records) + chr(10), encoding="utf-8")

        with patch("scripts.web_agent_execution._machine_event_source_context", return_value=trusted):
            first = prepare_web_assignment_dispatch(
                repo=self.repo, registry_path=self.registry, controller_id=None,
                delegator_session_id="ordinary-web-session-1", task_name="ordinary-recovery-1",
                assignment=assignment, now=T0, health_probe=lambda: True,
            )
            write_started("ordinary-recovery-1", "spawn-recovery-1", "ordinary-child-r1", T0)
            bind_web_assignment_dispatch(
                repo=self.repo, registry_path=self.registry, controller_id=None,
                delegator_session_id="ordinary-web-session-1", dispatch_id=first["dispatch_id"],
                event_paths=[event_path], now=T0 + timedelta(seconds=1), health_probe=lambda: True,
                watchdog_launcher=lambda **_: {"launched": False},
            )
            second_at = T0 + timedelta(minutes=46)
            second = prepare_web_assignment_dispatch(
                repo=self.repo, registry_path=self.registry, controller_id=None,
                delegator_session_id="ordinary-web-session-1", task_name="ordinary-recovery-2",
                assignment=assignment, now=second_at, health_probe=lambda: True,
            )
            write_started("ordinary-recovery-2", "spawn-recovery-2", "ordinary-child-r2", second_at)
            recovered = bind_web_assignment_dispatch(
                repo=self.repo, registry_path=self.registry, controller_id=None,
                delegator_session_id="ordinary-web-session-1", dispatch_id=second["dispatch_id"],
                event_paths=[event_path], now=second_at + timedelta(seconds=1), health_probe=lambda: True,
                event_source_probe=lambda: True, watchdog_launcher=lambda **_: {"launched": False},
            )
        lease = load_runtime_state(self.repo)["leases"]["A-1"]
        self.assertEqual(recovered["attempt"], 2)
        self.assertEqual(lease["delegation_owner_kind"], "session")
        self.assertEqual(lease["delegation_owner_id"], "ordinary-web-session-1")
        self.assertEqual(recovered["delegation_owner_id"], "ordinary-web-session-1")
