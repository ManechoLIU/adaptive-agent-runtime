import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from scripts.assignment_runtime import runtime_state_path
from scripts.project_model_score import load_project_samples, normalize_lease_sample, parse_reasoning_effort

UTC = timezone.utc
NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)


def terminal_lease(**overrides):
    lease = {
        "assignment_id": "A-1",
        "task_id": "T-1",
        "provider": "grok-build",
        "model": "grok-4.6",
        "auth_mode": "oauth",
        "execution_transport": "external_process",
        "execution_role": "writer",
        "policy_class": "backend",
        "strategy": "engine=grok-build;model=grok-4.6;auth_mode=oauth;reasoning_effort=high",
        "started_at": "2026-09-16T10:00:00+00:00",
        "terminal_at": "2026-09-16T10:12:00+00:00",
        "terminal_state": "completed",
        "transport_outcome": "completed",
        "delivery_outcome": "pass",
        "evidence": ["test-log:focused"],
        "artifacts": ["git:abc123"],
        "result_unknown": False,
        "retry_class": "none",
        "recovery_count": 0,
    }
    lease.update(overrides)
    return lease


class ProjectModelSampleTests(unittest.TestCase):
    def test_reasoning_effort_prefers_canonical_field_then_frozen_strategy(self):
        self.assertEqual(parse_reasoning_effort(terminal_lease(reasoning_effort="xhigh")), "xhigh")
        self.assertEqual(parse_reasoning_effort(terminal_lease(reasoning_effort=None)), "high")
        self.assertEqual(parse_reasoning_effort(terminal_lease(reasoning_effort=None, strategy="provider=grok-build")), "unknown")

    def test_normalizer_preserves_exact_route_identity(self):
        sample = normalize_lease_sample("SelfAlone", "A-1", terminal_lease())
        self.assertIsNotNone(sample)
        self.assertEqual(
            sample["identity"],
            {
                "project": "SelfAlone",
                "provider": "grok-build",
                "model": "grok-4.6",
                "auth_mode": "oauth",
                "reasoning_effort": "high",
                "execution_role": "writer",
                "policy_class": "backend",
                "execution_transport": "external_process",
            },
        )
        self.assertEqual(sample["elapsed_seconds"], 720.0)

    def test_normalizer_excludes_nonterminal_and_missing_model(self):
        self.assertIsNone(normalize_lease_sample("P", "A", terminal_lease(terminal_at=None, terminal_state=None)))
        self.assertIsNone(normalize_lease_sample("P", "A", terminal_lease(model=None)))

    def test_load_project_samples_discovers_all_models_without_population_list(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "demo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            state_path = runtime_state_path(repo)
            state_path.parent.mkdir(parents=True, exist_ok=True)
            leases = {
                "grok": terminal_lease(assignment_id="grok", model="grok-4.6"),
                "kimi": terminal_lease(
                    assignment_id="kimi",
                    provider="kimi-code",
                    model="kimi-k3",
                    auth_mode="api",
                    policy_class="frontend",
                    strategy="engine=kimi-code;model=kimi-k3;auth_mode=api;reasoning_effort=medium",
                ),
                "sol": terminal_lease(
                    assignment_id="sol",
                    provider="codex-native",
                    model="gpt-5.6-sol",
                    auth_mode="host",
                    execution_transport="codex_native_subagent",
                    strategy="provider=codex-native;model=gpt-5.6-sol;auth_mode=host;reasoning_effort=high",
                ),
                "future": terminal_lease(
                    assignment_id="future",
                    provider="future-provider",
                    model="future-model-9",
                    auth_mode="oauth",
                    strategy="provider=future-provider;model=future-model-9;auth_mode=oauth;reasoning_effort=low",
                ),
            }
            state_path.write_text(json.dumps({"schema_version": 1, "leases": leases, "lineages": {}}), encoding="utf-8")

            samples = load_project_samples(repo, window_days=30, now=NOW)
            models = {sample["identity"]["model"] for sample in samples}
            self.assertEqual(models, {"grok-4.6", "kimi-k3", "gpt-5.6-sol", "future-model-9"})


if __name__ == "__main__":
    unittest.main()

from scripts.project_model_score import classify_attribution, score_model_groups, score_route_groups


class ProjectModelAttributionTests(unittest.TestCase):
    def normalized(self, **overrides):
        lease = terminal_lease(**overrides)
        sample = normalize_lease_sample("SelfAlone", lease.get("assignment_id", "A"), lease)
        self.assertIsNotNone(sample)
        return sample

    def test_semantic_failure_on_healthy_transport_is_model_attributed(self):
        sample = self.normalized(delivery_outcome="fail", evidence=["test-log:red"], artifacts=[])
        attribution, reasons = classify_attribution(sample)
        self.assertEqual(attribution, "model")
        self.assertIn("semantic_delivery_fail", reasons)

    def test_provider_timeout_is_infrastructure_not_model_failure(self):
        sample = self.normalized(
            terminal_state="failed",
            transport_outcome="failed",
            delivery_outcome="unresolved",
            failure_class="provider_timeout",
            outcome_code="PROVIDER_TIMEOUT",
        )
        attribution, reasons = classify_attribution(sample)
        self.assertEqual(attribution, "infrastructure")
        self.assertIn("provider_timeout", reasons)

    def test_result_unknown_is_infrastructure_even_if_partial_output_exists(self):
        sample = self.normalized(
            terminal_state="failed",
            transport_outcome="failed",
            delivery_outcome="unresolved",
            result_unknown=True,
            artifacts=["artifact:/tmp/partial"],
        )
        self.assertEqual(classify_attribution(sample)[0], "infrastructure")

    def test_quota_or_service_failure_is_external(self):
        sample = self.normalized(
            terminal_state="failed",
            transport_outcome="failed",
            delivery_outcome="unresolved",
            failure_class="quota_exhausted",
        )
        self.assertEqual(classify_attribution(sample)[0], "external")

    def test_controller_cancel_without_quality_or_transport_failure_is_unknown(self):
        sample = self.normalized(
            terminal_state="cancelled",
            transport_outcome="cancelled",
            delivery_outcome="unresolved",
            retry_class="cancelled_by_controller",
        )
        self.assertEqual(classify_attribution(sample)[0], "unknown")


class ProjectModelScoringTests(unittest.TestCase):
    def sample(self, **overrides):
        lease = terminal_lease(**overrides)
        sample = normalize_lease_sample("SelfAlone", lease.get("assignment_id", "A"), lease)
        self.assertIsNotNone(sample)
        return sample

    def test_infrastructure_failure_does_not_reduce_project_model_score(self):
        good = self.sample(assignment_id="good")
        timeout = self.sample(
            assignment_id="timeout",
            terminal_state="failed",
            transport_outcome="failed",
            delivery_outcome="unresolved",
            failure_class="provider_timeout",
            outcome_code="PROVIDER_TIMEOUT",
            result_unknown=False,
            evidence=[],
            artifacts=[],
        )
        groups = score_model_groups([good, timeout])
        self.assertEqual(len(groups), 1)
        group = groups[0]
        self.assertEqual(group["quality_sample_count"], 1)
        self.assertEqual(group["excluded_infrastructure_count"], 1)
        self.assertEqual(group["project_model_score"], 100.0)

    def test_semantic_failure_reduces_project_model_score(self):
        good = self.sample(assignment_id="good")
        bad = self.sample(
            assignment_id="bad",
            delivery_outcome="fail",
            evidence=["test-log:failed"],
            artifacts=[],
        )
        group = score_model_groups([good, bad])[0]
        self.assertEqual(group["quality_sample_count"], 2)
        self.assertLess(group["project_model_score"], 100.0)
        self.assertGreater(group["project_model_score"], 0.0)

    def test_route_reliability_counts_transport_failure_but_not_semantic_fail_as_route_failure(self):
        good = self.sample(assignment_id="good")
        semantic_fail = self.sample(assignment_id="semantic", delivery_outcome="fail")
        timeout = self.sample(
            assignment_id="timeout",
            terminal_state="failed",
            transport_outcome="failed",
            delivery_outcome="unresolved",
            failure_class="provider_timeout",
            evidence=[],
            artifacts=[],
        )
        route = score_route_groups([good, semantic_fail, timeout])[0]
        self.assertEqual(route["eligible_attempts"], 3)
        self.assertEqual(route["successful_transport_attempts"], 2)
        self.assertEqual(route["route_reliability_score"], 66.7)

    def test_groups_remain_separate_by_effort_role_and_task_class(self):
        high_backend = self.sample(assignment_id="a", reasoning_effort="high")
        xhigh_backend = self.sample(assignment_id="b", reasoning_effort="xhigh")
        frontend = self.sample(assignment_id="c", reasoning_effort="high", policy_class="frontend")
        groups = score_model_groups([high_backend, xhigh_backend, frontend])
        keys = {(g["identity"]["reasoning_effort"], g["identity"]["policy_class"]) for g in groups}
        self.assertEqual(keys, {("high", "backend"), ("xhigh", "backend"), ("high", "frontend")})
