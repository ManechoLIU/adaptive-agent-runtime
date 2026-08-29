import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts.assignment_runtime import (
    RuntimePolicy, apply_receipt, evaluate_lease, load_runtime_state, save_runtime_state, runtime_state_path,
)

UTC=timezone.utc
T0=datetime(2026,8,29,10,0,tzinfo=UTC)

def receipt(event, at=T0, **extra):
    base={"event_type":event,"assignment_id":"a1","task_id":"T1","agent_id":"grok-writer","provider":"grok","session_id":"s1","worktree":"/tmp/wt","issued_at":at.isoformat()}
    base.update(extra); return base

class RuntimeTests(unittest.TestCase):
    def test_defaults_are_20_30_15_minutes(self):
        p=RuntimePolicy(); self.assertEqual((p.heartbeat_ttl_minutes,p.progress_deadline_minutes,p.progress_grace_minutes),(20,30,15))
    def test_start_creates_healthy_provider_lease_without_pid(self):
        state=apply_receipt({},receipt("assignment_started"),now=T0)
        self.assertEqual(evaluate_lease(state["leases"]["a1"],now=T0)["state"],"healthy")
    def test_heartbeat_refreshes_lease_not_progress(self):
        state=apply_receipt({},receipt("assignment_started"),now=T0)
        state=apply_receipt(state,receipt("assignment_heartbeat",T0+timedelta(minutes=10),event_seq=2),now=T0+timedelta(minutes=10))
        lease=state["leases"]["a1"]
        self.assertEqual(lease["last_progress_at"],T0.isoformat())
        self.assertEqual(lease["lease_expires_at"],(T0+timedelta(minutes=30)).isoformat())
    def test_progress_refreshes_both_deadlines(self):
        state=apply_receipt({},receipt("assignment_started"),now=T0)
        t=T0+timedelta(minutes=10); state=apply_receipt(state,receipt("assignment_progress",t,last_observed_head="abc",event_seq=2),now=t)
        lease=state["leases"]["a1"]
        self.assertEqual(lease["lease_expires_at"],(t+timedelta(minutes=20)).isoformat()); self.assertEqual(lease["progress_deadline_at"],(t+timedelta(minutes=30)).isoformat())
    def test_progress_stale_then_unhealthy_after_grace_while_heartbeats_continue(self):
        state=apply_receipt({},receipt("assignment_started"),now=T0)
        for seq, minute in enumerate((15, 30), start=2):
            t=T0+timedelta(minutes=minute)
            state=apply_receipt(state,receipt("assignment_heartbeat",t,event_seq=seq),now=t)
        lease=state["leases"]["a1"]
        self.assertEqual(evaluate_lease(lease,now=T0+timedelta(minutes=31))["state"],"progress_stale")
        t=T0+timedelta(minutes=44)
        state=apply_receipt(state,receipt("assignment_heartbeat",t,event_seq=4),now=t)
        self.assertEqual(evaluate_lease(state["leases"]["a1"],now=T0+timedelta(minutes=46))["state"],"unhealthy")
    def test_lease_expiry_is_unhealthy(self):
        state=apply_receipt({},receipt("assignment_started"),now=T0)
        self.assertEqual(evaluate_lease(state["leases"]["a1"],now=T0+timedelta(minutes=21))["reason"],"lease_expired")
    def test_terminal_receipt_makes_terminal(self):
        state=apply_receipt({},receipt("assignment_started"),now=T0); t=T0+timedelta(minutes=2)
        state=apply_receipt(state,receipt("assignment_terminal",t,event_seq=2,terminal_state="completed",outcome="success",summary="done",evidence=["checkpoint"],artifacts=[],next_action="none",retry_class="none"),now=t)
        self.assertEqual(evaluate_lease(state["leases"]["a1"],now=t)["state"],"terminal")
    def test_identity_mismatch_fails_closed(self):
        state=apply_receipt({},receipt("assignment_started"),now=T0)
        with self.assertRaises(ValueError): apply_receipt(state,receipt("assignment_heartbeat",T0+timedelta(minutes=1),session_id="other"),now=T0+timedelta(minutes=1))
    def test_state_round_trip_under_git(self):
        import subprocess
        with tempfile.TemporaryDirectory() as d:
            root=Path(d) / "repo"
            root.mkdir()
            subprocess.run(["git", "-C", str(root), "init", "-b", "main"], check=True, capture_output=True)
            state=apply_receipt({},receipt("assignment_started"),now=T0)
            save_runtime_state(root,state)
            self.assertEqual(load_runtime_state(root)["leases"]["a1"]["session_id"],"s1")
            self.assertIn("adaptive-delivery", str(runtime_state_path(root)))

if __name__=="__main__": unittest.main()

class ReliableAttemptProtocolTests(unittest.TestCase):
    def test_stale_attempt_late_success_cannot_overwrite_current_attempt(self):
        started = receipt("assignment_started", attempt=1, lease_id="lease-1", event_seq=1)
        state = apply_receipt({}, started, now=T0)
        t = T0 + timedelta(minutes=1)
        state = apply_receipt(state, receipt("assignment_started", t, attempt=2, lease_id="lease-2", event_seq=1), now=t)
        with self.assertRaises(ValueError):
            apply_receipt(state, receipt("assignment_terminal", t, attempt=1, lease_id="lease-1", event_seq=2,
                                         terminal_state="completed", outcome="success", summary="late", evidence=["x"], artifacts=[], next_action="none", retry_class="none"), now=t)

    def test_out_of_order_event_sequence_is_rejected(self):
        state = apply_receipt({}, receipt("assignment_started", attempt=1, lease_id="lease-1", event_seq=2), now=T0)
        with self.assertRaises(ValueError):
            apply_receipt(state, receipt("assignment_heartbeat", T0 + timedelta(minutes=1), attempt=1, lease_id="lease-1", event_seq=1), now=T0 + timedelta(minutes=1))

    def test_terminal_requires_structured_outcome(self):
        state = apply_receipt({}, receipt("assignment_started", attempt=1, lease_id="lease-1", event_seq=1), now=T0)
        with self.assertRaises(ValueError):
            apply_receipt(state, receipt("assignment_terminal", T0 + timedelta(minutes=1), attempt=1, lease_id="lease-1", event_seq=2, terminal_state="completed"), now=T0 + timedelta(minutes=1))

    def test_retry_policy_only_retries_transient_failures_up_to_three_attempts(self):
        from scripts.assignment_runtime import retry_decision
        self.assertTrue(retry_decision("transport_error", attempt=1)["retry"])
        self.assertFalse(retry_decision("transport_error", attempt=3)["retry"])
        self.assertFalse(retry_decision("permission", attempt=1)["retry"])

class EvidenceDeltaAndRecoveryBudgetTests(unittest.TestCase):
    def test_no_delta_progress_refreshes_heartbeat_but_not_progress_deadline(self):
        start = receipt(
            "assignment_started",
            baseline_head="abc",
            last_observed_status_sha256="status-1",
            evidence_receipt_id="red-1",
            artifact_fingerprint="artifact-1",
            blocker_evidence_fingerprint="blocker-1",
        )
        state = apply_receipt({}, start, now=T0)
        t = T0 + timedelta(minutes=10)
        state = apply_receipt(
            state,
            receipt(
                "assignment_progress",
                t,
                event_seq=2,
                last_observed_head="abc",
                last_observed_status_sha256="status-1",
                evidence_receipt_id="red-1",
                artifact_fingerprint="artifact-1",
                blocker_evidence_fingerprint="blocker-1",
            ),
            now=t,
        )
        lease = state["leases"]["a1"]
        self.assertEqual(lease["last_progress_at"], T0.isoformat())
        self.assertEqual(lease["progress_deadline_at"], (T0 + timedelta(minutes=30)).isoformat())
        self.assertEqual(lease["lease_expires_at"], (t + timedelta(minutes=20)).isoformat())

    def test_each_changed_evidence_fingerprint_refreshes_progress(self):
        changes = {
            "last_observed_head": "def",
            "last_observed_status_sha256": "status-2",
            "evidence_receipt_id": "green-2",
            "artifact_fingerprint": "artifact-2",
            "blocker_evidence_fingerprint": "blocker-2",
        }
        for field, changed in changes.items():
            with self.subTest(field=field):
                state = apply_receipt(
                    {},
                    receipt(
                        "assignment_started",
                        baseline_head="abc",
                        last_observed_status_sha256="status-1",
                        evidence_receipt_id="red-1",
                        artifact_fingerprint="artifact-1",
                        blocker_evidence_fingerprint="blocker-1",
                    ),
                    now=T0,
                )
                t = T0 + timedelta(minutes=10)
                state = apply_receipt(
                    state,
                    receipt("assignment_progress", t, event_seq=2, **{field: changed}),
                    now=t,
                )
                lease = state["leases"]["a1"]
                self.assertEqual(lease["last_progress_at"], t.isoformat())
                self.assertEqual(lease["progress_deadline_at"], (t + timedelta(minutes=30)).isoformat())
                self.assertEqual(lease[field], changed)

    def test_recovery_count_persists_and_third_same_contract_failure_exhausts_budget(self):
        state = apply_receipt({}, receipt("assignment_started", attempt=1, lease_id="lease-1", event_seq=1), now=T0)
        self.assertEqual(state["leases"]["a1"]["recovery_count"], 0)
        t1 = T0 + timedelta(minutes=1)
        state = apply_receipt(state, receipt(
            "assignment_terminal", t1, attempt=1, lease_id="lease-1", event_seq=2,
            terminal_state="failed", outcome="recoverable_failure", summary="first failure",
            evidence=["red"], artifacts=[], next_action="retry", retry_class="transport_error",
        ), now=t1)
        t2 = T0 + timedelta(minutes=2)
        state = apply_receipt(state, receipt("assignment_started", t2, attempt=2, lease_id="lease-2", event_seq=1), now=t2)
        self.assertEqual(state["leases"]["a1"]["recovery_count"], 1)
        t3 = T0 + timedelta(minutes=3)
        state = apply_receipt(state, receipt(
            "assignment_terminal", t3, attempt=2, lease_id="lease-2", event_seq=2,
            terminal_state="failed", outcome="recoverable_failure", summary="second failure",
            evidence=["red"], artifacts=[], next_action="retry", retry_class="transport_error",
        ), now=t3)
        t4 = T0 + timedelta(minutes=4)
        state = apply_receipt(state, receipt("assignment_started", t4, attempt=3, lease_id="lease-3", event_seq=1), now=t4)
        lease = state["leases"]["a1"]
        self.assertEqual(lease["recovery_count"], 2)
        self.assertEqual(evaluate_lease(lease, now=t4)["state"], "healthy")
        t5 = T0 + timedelta(minutes=5)
        state = apply_receipt(state, receipt(
            "assignment_terminal", t5, attempt=3, lease_id="lease-3", event_seq=2,
            terminal_state="failed", outcome="recoverable_failure", summary="third failure",
            evidence=["red"], artifacts=[], next_action="change strategy", retry_class="transport_error",
        ), now=t5)
        decision = evaluate_lease(state["leases"]["a1"], now=t5)
        self.assertEqual(decision["state"], "budget_exhausted")
        self.assertEqual(decision["reason"], "recovery_budget_exhausted")

    def test_budget_exhausts_on_stale_progress_even_when_heartbeat_is_current(self):
        p = RuntimePolicy(progress_deadline_minutes=5, progress_grace_minutes=5, heartbeat_ttl_minutes=30, max_recoveries=2)
        state = apply_receipt({}, receipt("assignment_started", attempt=1, lease_id="l1", event_seq=1), now=T0, policy=p)
        state = apply_receipt(state, receipt("assignment_started", T0 + timedelta(minutes=1), attempt=2, lease_id="l2", event_seq=1), now=T0 + timedelta(minutes=1), policy=p)
        state = apply_receipt(state, receipt("assignment_started", T0 + timedelta(minutes=2), attempt=3, lease_id="l3", event_seq=1), now=T0 + timedelta(minutes=2), policy=p)
        heartbeat = T0 + timedelta(minutes=11)
        state = apply_receipt(state, receipt("assignment_heartbeat", heartbeat, attempt=3, lease_id="l3", event_seq=2), now=heartbeat, policy=p)
        decision = evaluate_lease(state["leases"]["a1"], now=T0 + timedelta(minutes=13), policy=p)
        self.assertEqual(decision["state"], "budget_exhausted")
        self.assertEqual(decision["reason"], "recovery_budget_exhausted")

class RecoveryBudgetLaunchGateTests(unittest.TestCase):
    def test_fourth_same_assignment_attempt_is_rejected_after_two_recoveries(self):
        state = apply_receipt({}, receipt("assignment_started", attempt=1, lease_id="l1", event_seq=1), now=T0)
        for attempt, minute in ((1, 1), (2, 3), (3, 5)):
            if attempt > 1:
                state = apply_receipt(
                    state,
                    receipt("assignment_started", T0 + timedelta(minutes=minute - 1), attempt=attempt, lease_id=f"l{attempt}", event_seq=1),
                    now=T0 + timedelta(minutes=minute - 1),
                )
            state = apply_receipt(
                state,
                receipt(
                    "assignment_terminal", T0 + timedelta(minutes=minute), attempt=attempt, lease_id=f"l{attempt}", event_seq=2,
                    terminal_state="failed", outcome="recoverable_failure", summary="failed",
                    evidence=["checkpoint"], artifacts=[], next_action="change strategy", retry_class="transport_error",
                ),
                now=T0 + timedelta(minutes=minute),
            )
        self.assertEqual(evaluate_lease(state["leases"]["a1"], now=T0 + timedelta(minutes=5))["state"], "budget_exhausted")
        with self.assertRaisesRegex(ValueError, "recovery budget exhausted.*new Assignment"):
            apply_receipt(
                state,
                receipt("assignment_started", T0 + timedelta(minutes=6), attempt=4, lease_id="l4", event_seq=1),
                now=T0 + timedelta(minutes=6),
            )

    def test_budget_exhausted_lineage_does_not_block_a_new_assignment(self):
        old = apply_receipt({}, receipt("assignment_started", attempt=1, lease_id="l1", event_seq=1), now=T0)
        old["leases"]["a1"]["recovery_count"] = 2
        old["leases"]["a1"]["terminal_state"] = "failed"
        new_receipt = receipt("assignment_started", assignment_id="a2", task_id="T1", attempt=1, lease_id="a2-l1", event_seq=1)
        state = apply_receipt(old, new_receipt, now=T0 + timedelta(minutes=1))
        self.assertEqual(state["leases"]["a2"]["recovery_count"], 0)

class RuntimeAttemptSequenceTests(unittest.TestCase):
    def test_new_assignment_must_start_at_attempt_one(self):
        with self.assertRaisesRegex(ValueError, "new Assignment must start at attempt 1"):
            apply_receipt({}, receipt("assignment_started", attempt=99, lease_id="l99", event_seq=1), now=T0)

    def test_recovery_attempt_must_increment_by_exactly_one(self):
        state = apply_receipt({}, receipt("assignment_started", attempt=1, lease_id="l1", event_seq=1), now=T0)
        with self.assertRaisesRegex(ValueError, "recovery attempt must increment by exactly one"):
            apply_receipt(
                state,
                receipt("assignment_started", T0 + timedelta(minutes=1), attempt=3, lease_id="l3", event_seq=1),
                now=T0 + timedelta(minutes=1),
            )

class RuntimeCliCommonStateTests(unittest.TestCase):
    def test_apply_cli_shares_lineage_and_rejects_attempt_four_across_worktrees(self):
        import json
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            repo = base / "repo"
            wt = base / "wt"
            repo.mkdir()
            subprocess.run(["git", "-C", str(repo), "init", "-b", "main"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
            (repo / "README.md").write_text("x\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "worktree", "add", str(wt), "-b", "worker"], check=True, capture_output=True)
            script = Path(__file__).resolve().parents[1] / "scripts" / "assignment_runtime.py"

            def apply(repo_arg: Path, payload: dict) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    [sys.executable, str(script), "apply", "--repo", str(repo_arg)],
                    input=json.dumps(payload), text=True, capture_output=True,
                )

            def event(event_type: str, attempt: int, seq: int, **extra):
                payload = receipt(
                    event_type,
                    assignment_id="shared-a1",
                    task_id="T1",
                    agent_id="writer",
                    provider="grok-build",
                    session_id="s1",
                    worktree=str(wt),
                    attempt=attempt,
                    lease_id=f"shared-a1:attempt:{attempt}",
                    event_seq=seq,
                    **extra,
                )
                return payload

            for attempt in (1, 2, 3):
                started = apply(repo if attempt == 1 else wt, event("assignment_started", attempt, 1))
                self.assertEqual(started.returncode, 0, started.stderr + started.stdout)
                failed = apply(
                    wt,
                    event(
                        "assignment_terminal", attempt, 2,
                        terminal_state="failed", outcome="recoverable_failure", summary="failed",
                        evidence=["checkpoint"], artifacts=[], next_action="retry", retry_class="transport_error",
                    ),
                )
                self.assertEqual(failed.returncode, 0, failed.stderr + failed.stdout)

            before = load_runtime_state(repo)
            blocked = apply(wt, event("assignment_started", 4, 1))
            self.assertNotEqual(blocked.returncode, 0)
            self.assertIn("recovery budget exhausted", blocked.stdout + blocked.stderr)
            self.assertEqual(load_runtime_state(wt), before)

            new_assignment = event("assignment_started", 1, 1)
            new_assignment.update({"assignment_id": "shared-a2", "lease_id": "shared-a2:attempt:1"})
            allowed = apply(wt, new_assignment)
            self.assertEqual(allowed.returncode, 0, allowed.stderr + allowed.stdout)
            self.assertIn("shared-a2", load_runtime_state(repo)["leases"])

class WorktreeRuntimePathTests(unittest.TestCase):
    def test_root_and_linked_worktree_share_runtime_state_path(self):
        import subprocess
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "repo"
            wt = Path(d) / "wt"
            root.mkdir()
            subprocess.run(["git", "-C", str(root), "init", "-b", "main"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
            (root / "seed").write_text("seed\n")
            subprocess.run(["git", "-C", str(root), "add", "seed"], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-m", "seed"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(root), "worktree", "add", "-b", "worker", str(wt)], check=True, capture_output=True)
            self.assertEqual(runtime_state_path(root), runtime_state_path(wt))
            self.assertEqual(runtime_state_path(root), (root / ".git" / "adaptive-delivery" / "runtime-assignments.json").resolve())
            state = apply_receipt({}, receipt("assignment_started"), now=T0)
            save_runtime_state(wt, state)
            self.assertEqual(load_runtime_state(root)["leases"]["a1"]["session_id"], "s1")
