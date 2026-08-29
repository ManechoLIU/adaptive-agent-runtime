import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts.assignment_runtime import (
    RuntimePolicy, apply_receipt, evaluate_lease, load_runtime_state, save_runtime_state,
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
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); (root/".git").mkdir(); state=apply_receipt({},receipt("assignment_started"),now=T0)
            save_runtime_state(root,state); self.assertEqual(load_runtime_state(root)["leases"]["a1"]["session_id"],"s1")

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
