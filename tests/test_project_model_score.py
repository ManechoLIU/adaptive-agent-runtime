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

from scripts.project_model_score import build_decisions, attach_benchmarks


def decision_group(
    *,
    model="grok-4.6",
    score=85.0,
    samples=5,
    effort="high",
    role="writer",
    policy="backend",
    provider="grok-build",
    transport="external_process",
    auth="oauth",
    elapsed=600.0,
):
    return {
        "identity": {
            "project": "SelfAlone",
            "provider": provider,
            "model": model,
            "auth_mode": auth,
            "reasoning_effort": effort,
            "execution_role": role,
            "policy_class": policy,
            "execution_transport": transport,
        },
        "project_model_score": score,
        "dimension_scores": {"delivery_success": score},
        "dimension_coverage": 0.55,
        "quality_sample_count": samples,
        "total_sample_count": samples,
        "excluded_infrastructure_count": 0,
        "excluded_external_count": 0,
        "mixed_count": 0,
        "unknown_count": 0,
        "confidence": "high" if samples >= 5 else ("medium" if samples >= 3 else "low"),
        "median_elapsed_seconds": elapsed,
    }


def route_group(*, model="grok-4.6", provider="grok-build", auth="oauth", transport="external_process", score=95.0, attempts=5):
    return {
        "route": {
            "provider": provider,
            "model": model,
            "auth_mode": auth,
            "execution_transport": transport,
        },
        "route_reliability_score": score,
        "eligible_attempts": attempts,
        "successful_transport_attempts": round(attempts * score / 100),
        "failed_transport_attempts": attempts - round(attempts * score / 100),
        "unknown_attempts": 0,
        "result_unknown_count": 0,
        "failure_classes": {},
    }


class ModelDecisionTests(unittest.TestCase):
    def test_high_project_quality_with_degraded_route_recommends_change_route_not_switch(self):
        groups = [decision_group(score=86, samples=6)]
        routes = [route_group(score=60, attempts=5)]
        decision = build_decisions(groups, routes)[0]
        self.assertEqual(decision["action"], "CHANGE_ROUTE")
        self.assertIn("route_reliability_degraded", decision["reason_codes"])

    def test_semantically_weak_model_on_healthy_route_can_switch_to_stronger_observed_alternative(self):
        weak = decision_group(model="grok-4.6", score=62, samples=7)
        strong = decision_group(
            model="gpt-5.6-sol",
            provider="codex-native",
            auth="host",
            transport="codex_native_subagent",
            score=84,
            samples=8,
            elapsed=500,
        )
        routes = [
            route_group(score=92, attempts=8),
            route_group(model="gpt-5.6-sol", provider="codex-native", auth="host", transport="codex_native_subagent", score=96, attempts=8),
        ]
        decisions = {item["identity"]["model"]: item for item in build_decisions([weak, strong], routes)}
        self.assertEqual(decisions["grok-4.6"]["action"], "SWITCH_MODEL")
        self.assertEqual(decisions["grok-4.6"]["suggested_target"]["model"], "gpt-5.6-sol")

    def test_low_sample_count_never_switches_model(self):
        weak = decision_group(model="grok-4.6", score=50, samples=2)
        strong = decision_group(model="gpt-5.6-sol", provider="codex-native", auth="host", transport="codex_native_subagent", score=90, samples=8)
        decisions = {item["identity"]["model"]: item for item in build_decisions([weak, strong], [route_group(), route_group(model="gpt-5.6-sol", provider="codex-native", auth="host", transport="codex_native_subagent")])}
        self.assertEqual(decisions["grok-4.6"]["action"], "INSUFFICIENT_EVIDENCE")

    def test_slower_effort_with_similar_quality_recommends_tune_effort(self):
        high = decision_group(effort="high", score=85, samples=4, elapsed=600)
        medium = decision_group(effort="medium", score=84, samples=4, elapsed=300)
        decisions = {(item["identity"]["reasoning_effort"]): item for item in build_decisions([high, medium], [route_group(score=95, attempts=8)])}
        self.assertEqual(decisions["high"]["action"], "TUNE_EFFORT")
        self.assertEqual(decisions["high"]["suggested_target"]["reasoning_effort"], "medium")

    def test_model_weak_in_writer_but_strong_in_reviewer_recommends_change_role(self):
        writer = decision_group(role="writer", score=65, samples=4)
        reviewer = decision_group(role="reviewer", score=88, samples=4)
        decisions = {item["identity"]["execution_role"]: item for item in build_decisions([writer, reviewer], [route_group(score=95, attempts=8)])}
        self.assertEqual(decisions["writer"]["action"], "CHANGE_ROLE")
        self.assertEqual(decisions["writer"]["suggested_target"]["execution_role"], "reviewer")

    def test_external_benchmark_is_sidecar_and_protocol_mismatch_is_visible(self):
        exact_group = decision_group(model="grok-4.6", effort="high", score=85, samples=6)
        project_score_before = exact_group["project_model_score"]
        enriched = attach_benchmarks(
            [exact_group],
            [{"model": "grok-4.6", "effort": "high", "route": "grok-build", "score": 79.5, "observed_at": "2026-09-14"}],
        )[0]
        self.assertEqual(enriched["benchmark"]["match"], "exact")
        self.assertEqual(enriched["baseline_comparison"], "IN_LINE")
        self.assertEqual(enriched["project_model_score"], project_score_before)

        partial = attach_benchmarks(
            [decision_group(model="gpt-5.6-sol", effort="high", provider="codex-native")],
            [{"model": "gpt-5.6-sol", "effort": "high", "route": "custom-endpoint", "score": 73, "observed_at": "2026-09-14"}],
        )[0]
        self.assertEqual(partial["benchmark"]["match"], "partial")
        self.assertEqual(partial["baseline_comparison"], "NOT_COMPARABLE")
