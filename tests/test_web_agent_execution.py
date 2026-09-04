import json
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
                    "attestation": self.attestation("running", conversation="conv-direct"),
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

    def test_proven_kimi_frontend_failure_can_prepare_web_fallback_when_machine_source_ready(self):
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
                failure_evidence="receipt:kimi/terminal-provider-unavailable",
                prior_attempt_terminal=True,
                result_unknown=False,
            ),
        )
        prepared = prepare_web_assignment_dispatch(
            repo=self.repo,
            registry_path=self.registry,
            controller_id="controller-1",
            task_name="runtime-proven-web-fallback",
            assignment=assignment,
            now=T0,
            health_probe=lambda: True,
            event_source_probe=lambda: True,
        )
        self.assertEqual(prepared["assignment"]["route"]["decision"], "safe_fallback")
        self.assertEqual(prepared["assignment"]["route"]["fallback_from"]["provider"], "kimi-code")

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
        self.registry.write_text(json.dumps({"controller-1": str(self.repo.resolve())}), encoding="utf-8")
        self.policy = root / "AGENTS.md"
        self.policy.write_text(
            "general 默认 provider=chatgpt_web、model=gpt-5.6-sol、auth_mode=host。\n",
            encoding="utf-8",
        )

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
                    "sha256": __import__("hashlib").sha256(self.policy.read_bytes()).hexdigest(),
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

    def observation(self, kind="completed", **extra):
        value = {
            "source": "collaboration_session_event",
            "kind": kind,
            "conversation_id": "child-thread-1",
            "observation_id": f"terminal-{kind}-1",
            "timestamp": (T0 + timedelta(minutes=2)).isoformat(),
        }
        value.update(extra)
        return value

    def test_public_structured_terminal_ingest_rejects_caller_supplied_observation(self):
        from scripts.web_agent_execution import ingest_structured_subagent_terminal
        self.start()
        with self.assertRaisesRegex(PermissionError, "direct structured Web terminal ingest"):
            ingest_structured_subagent_terminal(
                repo=self.repo,
                assignment_id="A-COLLAB",
                observation=self.observation(),
                now=T0 + timedelta(minutes=2),
            )
        self.assertIsNone(load_runtime_state(self.repo)["leases"]["A-COLLAB"]["terminal_state"])

    def test_structured_completed_closes_canonical_attempt_and_writes_durable_terminal_receipt(self):
        from scripts.web_agent_execution import _ingest_verified_structured_subagent_terminal as ingest_structured_subagent_terminal
        self.start()
        result = ingest_structured_subagent_terminal(
            repo=self.repo,
            assignment_id="A-COLLAB",
            observation=self.observation(),
            now=T0 + timedelta(minutes=2),
        )
        lease = load_runtime_state(self.repo)["leases"]["A-COLLAB"]
        self.assertEqual(lease["terminal_state"], "completed")
        self.assertEqual(lease["transport_outcome"], "completed")
        self.assertEqual(lease["delivery_outcome"], "unresolved")
        receipt = json.loads(Path(result["terminal_receipt"]).read_text(encoding="utf-8"))
        self.assertEqual(receipt["event_type"], "external_agent_terminal")
        self.assertEqual(receipt["assignment_id"], "A-COLLAB")
        self.assertEqual(receipt["session_id"], "child-thread-1")
        self.assertEqual(receipt["attempt"], 1)
        self.assertEqual(receipt["lease_id"], lease["lease_id"])
        self.assertEqual(receipt["machine_terminal_observation_id"], "terminal-completed-1")
        self.assertEqual(receipt["model"], "gpt-5.6-sol")
        self.assertEqual(receipt["agent_type"], "default")

    def test_duplicate_structured_terminal_reuses_same_receipt_without_reopening_attempt(self):
        from scripts.web_agent_execution import _ingest_verified_structured_subagent_terminal as ingest_structured_subagent_terminal
        self.start()
        first = ingest_structured_subagent_terminal(
            repo=self.repo, assignment_id="A-COLLAB",
            observation=self.observation(), now=T0 + timedelta(minutes=2),
        )
        second = ingest_structured_subagent_terminal(
            repo=self.repo, assignment_id="A-COLLAB",
            observation=self.observation(observation_id="terminal-refresh-2"),
            now=T0 + timedelta(minutes=3),
        )
        lease = load_runtime_state(self.repo)["leases"]["A-COLLAB"]
        self.assertEqual(first["terminal_receipt"], second["terminal_receipt"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(lease["attempt"], 1)
        self.assertEqual(lease["terminal_state"], "completed")

    def test_structured_terminal_must_match_machine_observed_child_session(self):
        from scripts.web_agent_execution import _ingest_verified_structured_subagent_terminal as ingest_structured_subagent_terminal
        self.start()
        with self.assertRaisesRegex(ValueError, "session"):
            ingest_structured_subagent_terminal(
                repo=self.repo, assignment_id="A-COLLAB",
                observation=self.observation(conversation_id="other-child"),
                now=T0 + timedelta(minutes=2),
            )
        self.assertIsNone(load_runtime_state(self.repo)["leases"]["A-COLLAB"]["terminal_state"])

    def test_structured_disconnected_is_terminal_but_ui_interruption_text_is_not_used(self):
        from scripts.web_agent_execution import _ingest_verified_structured_subagent_terminal as ingest_structured_subagent_terminal
        self.start()
        result = ingest_structured_subagent_terminal(
            repo=self.repo, assignment_id="A-COLLAB",
            observation=self.observation(kind="disconnected"),
            now=T0 + timedelta(minutes=2),
        )
        lease = load_runtime_state(self.repo)["leases"]["A-COLLAB"]
        self.assertEqual(lease["terminal_state"], "disconnected")
        self.assertEqual(lease["transport_outcome"], "failed")
        self.assertIn("terminal_receipt", result)
