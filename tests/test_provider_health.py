import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone

from scripts.assignment_runtime import runtime_state_path
from scripts.project_model_score import normalize_lease_sample
from scripts.provider_health import derive_route_health, main as provider_health_main, route_health_for_assignment

UTC = timezone.utc
T0 = datetime(2026, 9, 16, 10, 0, tzinfo=UTC)


def lease_at(minutes: int, **overrides):
    start = T0 + timedelta(minutes=minutes)
    terminal = start + timedelta(minutes=1)
    lease = {
        "assignment_id": f"A-{minutes}",
        "task_id": f"T-{minutes}",
        "provider": "grok-build",
        "model": "grok-4.6",
        "auth_mode": "oauth",
        "execution_transport": "external_process",
        "execution_role": "writer",
        "policy_class": "backend",
        "strategy": "engine=grok-build;model=grok-4.6;auth_mode=oauth;reasoning_effort=high",
        "started_at": start.isoformat(),
        "terminal_at": terminal.isoformat(),
        "terminal_state": "completed",
        "transport_outcome": "completed",
        "delivery_outcome": "pass",
        "evidence": ["test-log:ok"],
        "artifacts": [f"git:{minutes:06d}"],
        "result_unknown": False,
        "retry_class": "none",
        "recovery_count": 0,
    }
    lease.update(overrides)
    sample = normalize_lease_sample("SelfAlone", lease["assignment_id"], lease)
    assert sample is not None
    # Provider health can use these additional canonical fields when present.
    sample["provider_started"] = bool(lease.get("provider_started", True))
    sample["cleanup_confirmed"] = lease.get("cleanup_confirmed")
    return sample


def infra_failure(minutes: int, failure_class="provider_timeout", *, result_unknown=False):
    return lease_at(
        minutes,
        terminal_state="failed",
        transport_outcome="failed",
        delivery_outcome="unresolved",
        evidence=[],
        artifacts=[],
        failure_class=failure_class,
        outcome_code="PROVIDER_TIMEOUT",
        retry_class=failure_class,
        result_unknown=result_unknown,
        provider_started=True,
    )


class ProviderHealthProjectionTests(unittest.TestCase):
    def one(self, samples, *, now=None, cooldown_seconds=1800):
        rows = derive_route_health(samples, now=now or T0 + timedelta(hours=2), cooldown_seconds=cooldown_seconds)
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_all_clean_attempts_are_healthy(self):
        row = self.one([lease_at(0), lease_at(5), lease_at(10)])
        self.assertEqual(row["state"], "HEALTHY")
        self.assertEqual(row["recent_failures"], 0)
        self.assertTrue(row["dispatch_allowed"])

    def test_two_failures_in_last_five_are_degraded(self):
        row = self.one([lease_at(0), infra_failure(5), lease_at(10), infra_failure(15), lease_at(20)], now=T0 + timedelta(minutes=25))
        self.assertEqual(row["state"], "DEGRADED")
        self.assertEqual(row["recent_failures"], 2)
        self.assertTrue(row["dispatch_allowed"])

    def test_three_failures_in_last_five_open_route(self):
        row = self.one([lease_at(0), infra_failure(5), lease_at(10), infra_failure(15), infra_failure(20)], now=T0 + timedelta(minutes=25))
        self.assertEqual(row["state"], "OPEN")
        self.assertEqual(row["recent_failures"], 3)
        self.assertFalse(row["dispatch_allowed"])

    def test_two_consecutive_provider_boundary_failures_open_route(self):
        row = self.one([lease_at(0), lease_at(5), infra_failure(10), infra_failure(15)], now=T0 + timedelta(minutes=17))
        self.assertEqual(row["state"], "OPEN")
        self.assertEqual(row["consecutive_failures"], 2)

    def test_cleanup_uncertainty_opens_immediately(self):
        sample = infra_failure(5, "process_group_cleanup_failed", result_unknown=True)
        sample["cleanup_confirmed"] = False
        row = self.one([lease_at(0), sample], now=T0 + timedelta(minutes=8))
        self.assertEqual(row["state"], "OPEN")
        self.assertEqual(row["result_unknown_blockers"], 1)
        self.assertFalse(row["dispatch_allowed"])

    def test_open_route_moves_to_probe_required_after_cooldown_but_not_healthy(self):
        samples = [lease_at(0), infra_failure(5), infra_failure(10)]
        row = self.one(samples, now=T0 + timedelta(minutes=50), cooldown_seconds=900)
        self.assertEqual(row["state"], "PROBE_REQUIRED")
        self.assertTrue(row["probe_eligible"])
        self.assertTrue(row["dispatch_allowed"])

    def test_semantic_model_failure_does_not_degrade_route_health(self):
        semantic_fail = lease_at(5, delivery_outcome="fail", evidence=["test-log:red"], artifacts=[])
        row = self.one([lease_at(0), semantic_fail, lease_at(10)])
        self.assertEqual(row["state"], "HEALTHY")
        self.assertEqual(row["recent_failures"], 0)

    def test_pre_provider_review_policy_rejections_do_not_degrade_route_health(self):
        rejected = [
            infra_failure(5, "review_policy_invalid"),
            infra_failure(10, "review_policy_invalid"),
            infra_failure(15, "review_policy_invalid"),
        ]
        for sample in rejected:
            sample["provider_started"] = False
            sample["outcome_code"] = "LOCAL_PRECHECK_FAILED"
        row = self.one([lease_at(0), *rejected], now=T0 + timedelta(minutes=20))
        self.assertEqual(row["state"], "HEALTHY")
        self.assertEqual(row["recent_failures"], 0)

    def test_delivery_receipt_validation_failures_do_not_open_provider_circuit(self):
        invalid_receipts = [
            infra_failure(5, "delivery_receipt_invalid"),
            infra_failure(10, "delivery_receipt_invalid"),
            infra_failure(15, "delivery_receipt_invalid"),
        ]
        for sample in invalid_receipts:
            sample["provider_started"] = True
            sample["outcome_code"] = "RESULT_PARSE_FAILED"
        row = self.one([lease_at(0), *invalid_receipts], now=T0 + timedelta(minutes=20))
        self.assertEqual(row["state"], "HEALTHY")
        self.assertEqual(row["recent_failures"], 0)


    def test_clean_real_attempt_after_prior_open_history_restores_healthy(self):
        row = self.one([lease_at(0), infra_failure(5), infra_failure(10), lease_at(40)], now=T0 + timedelta(minutes=45), cooldown_seconds=900)
        self.assertEqual(row["state"], "HEALTHY")
        self.assertEqual(row["consecutive_failures"], 0)

    def test_result_unknown_is_diagnostic_but_does_not_globally_block_unrelated_new_assignments(self):
        unknown = infra_failure(5, result_unknown=True)
        row = self.one([lease_at(0), unknown], now=T0 + timedelta(hours=2), cooldown_seconds=60)
        self.assertEqual(row["result_unknown_blockers"], 1)
        self.assertTrue(row["dispatch_allowed"])
        self.assertIsNone(row["block_reason"])

    def test_kimi_uses_same_generic_projection(self):
        kimi_ok = lease_at(0)
        kimi_ok["identity"].update({"provider": "kimi-code", "model": "kimi-k3", "auth_mode": "api"})
        kimi_fail = infra_failure(5)
        kimi_fail["identity"].update({"provider": "kimi-code", "model": "kimi-k3", "auth_mode": "api"})
        rows = derive_route_health([kimi_ok, kimi_fail], now=T0 + timedelta(minutes=10))
        self.assertEqual(rows[0]["route"]["model"], "kimi-k3")
        self.assertNotEqual(rows[0]["route"]["provider"], "grok-build")


if __name__ == "__main__":
    unittest.main()

class ProviderHealthCanonicalEvidenceTests(unittest.TestCase):
    def test_normalizer_preserves_provider_boundary_and_cleanup_evidence(self):
        start = T0
        lease = {
            "assignment_id": "cleanup",
            "task_id": "cleanup-task",
            "provider": "grok-build",
            "model": "grok-4.6",
            "auth_mode": "oauth",
            "execution_transport": "external_process",
            "execution_role": "writer",
            "policy_class": "backend",
            "strategy": "engine=grok-build;model=grok-4.6;auth_mode=oauth;reasoning_effort=high",
            "started_at": start.isoformat(),
            "terminal_at": (start + timedelta(minutes=1)).isoformat(),
            "terminal_state": "failed",
            "transport_outcome": "failed",
            "delivery_outcome": "unresolved",
            "failure_class": "process_group_cleanup_failed",
            "failure_details": {"provider_started": True, "cleanup_confirmed": False},
            "result_unknown": True,
            "evidence": [],
            "artifacts": [],
        }
        sample = normalize_lease_sample("SelfAlone", "cleanup", lease)
        self.assertIsNotNone(sample)
        self.assertTrue(sample["provider_started"])
        self.assertFalse(sample["cleanup_confirmed"])


def raw_runtime_lease(minutes: int, **overrides):
    start = T0 + timedelta(minutes=minutes)
    lease = {
        "assignment_id": f"R-{minutes}",
        "task_id": f"RT-{minutes}",
        "provider": "grok-build",
        "model": "grok-4.6",
        "auth_mode": "oauth",
        "execution_transport": "external_process",
        "execution_role": "writer",
        "policy_class": "backend",
        "strategy": "engine=grok-build;model=grok-4.6;auth_mode=oauth;reasoning_effort=high",
        "started_at": start.isoformat(),
        "terminal_at": (start + timedelta(minutes=1)).isoformat(),
        "terminal_state": "completed",
        "transport_outcome": "completed",
        "delivery_outcome": "pass",
        "evidence": ["test-log:ok"],
        "artifacts": [f"git:{minutes:06d}"],
        "result_unknown": False,
        "retry_class": "none",
        "recovery_count": 0,
    }
    lease.update(overrides)
    return lease


class ProviderHealthCliTests(unittest.TestCase):
    def make_repo(self, leases):
        temp = tempfile.TemporaryDirectory()
        repo = Path(temp.name) / "demo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        state_path = runtime_state_path(repo)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps({"schema_version": 1, "leases": leases, "lineages": {}}), encoding="utf-8")
        return temp, repo

    def test_route_health_for_assignment_recomputes_exact_route_from_canonical_state(self):
        fail = dict(
            terminal_state="failed",
            transport_outcome="failed",
            delivery_outcome="unresolved",
            failure_class="provider_timeout",
            outcome_code="PROVIDER_TIMEOUT",
            retry_class="provider_timeout",
            evidence=[],
            artifacts=[],
        )
        temp, repo = self.make_repo({
            "a": raw_runtime_lease(0),
            "b": raw_runtime_lease(5, **fail),
            "c": raw_runtime_lease(10, **fail),
        })
        self.addCleanup(temp.cleanup)
        row = route_health_for_assignment(
            repo,
            {"provider": "grok-build", "model": "grok-4.6", "auth_mode": "oauth", "execution_transport": "external_process"},
            now=T0 + timedelta(minutes=12),
        )
        self.assertEqual(row["state"], "OPEN")
        self.assertFalse(row["dispatch_allowed"])

    def test_status_cli_can_write_reproducible_cache_snapshot(self):
        temp, repo = self.make_repo({"a": raw_runtime_lease(0)})
        self.addCleanup(temp.cleanup)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            rc = provider_health_main(["status", "--repo", str(repo), "--write-cache", "--now", (T0 + timedelta(minutes=5)).isoformat()])
        self.assertEqual(rc, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(len(payload["input_evidence_hash"]), 64)
        cache = runtime_state_path(repo).parent / "provider-health" / "latest.json"
        self.assertTrue(cache.exists())
        cached = json.loads(cache.read_text(encoding="utf-8"))
        self.assertEqual(cached["input_evidence_hash"], payload["input_evidence_hash"])

    def test_gate_cli_blocks_open_route_before_provider_spawn(self):
        fail = dict(
            terminal_state="failed", transport_outcome="failed", delivery_outcome="unresolved",
            failure_class="provider_timeout", outcome_code="PROVIDER_TIMEOUT", retry_class="provider_timeout",
            evidence=[], artifacts=[],
        )
        temp, repo = self.make_repo({"a": raw_runtime_lease(0, **fail), "b": raw_runtime_lease(5, **fail)})
        self.addCleanup(temp.cleanup)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            rc = provider_health_main([
                "gate", "--repo", str(repo), "--provider", "grok-build", "--model", "grok-4.6",
                "--auth-mode", "oauth", "--execution-transport", "external_process",
                "--now", (T0 + timedelta(minutes=7)).isoformat(),
            ])
        self.assertEqual(rc, 3)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["state"], "OPEN")
        self.assertEqual(payload["block_reason"], "circuit_open")

    def test_gate_cli_allows_healthy_and_probe_required_routes_without_changing_provider(self):
        temp, repo = self.make_repo({"a": raw_runtime_lease(0)})
        self.addCleanup(temp.cleanup)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            rc = provider_health_main([
                "gate", "--repo", str(repo), "--provider", "grok-build", "--model", "grok-4.6",
                "--auth-mode", "oauth", "--execution-transport", "external_process",
                "--now", (T0 + timedelta(minutes=5)).isoformat(),
            ])
        self.assertEqual(rc, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["route"]["provider"], "grok-build")
        self.assertTrue(payload["dispatch_allowed"])

class ProviderHealthProbeCompatibilityTests(unittest.TestCase):
    def test_result_unknown_history_does_not_prevent_new_route_probe_after_cooldown(self):
        samples = [lease_at(0), infra_failure(5, result_unknown=True), infra_failure(10)]
        row = derive_route_health(samples, now=T0 + timedelta(minutes=50), cooldown_seconds=900)[0]
        self.assertEqual(row["state"], "PROBE_REQUIRED")
        self.assertTrue(row["probe_eligible"])
        self.assertTrue(row["dispatch_allowed"])
        self.assertEqual(row["result_unknown_blockers"], 1)
