import unittest

from scripts.model_score_dashboard import render_dashboard


def sample_report():
    return {
        "schema_version": 1,
        "project": "SelfAlone <script>alert(1)</script>",
        "window_days": 30,
        "generated_at": "2026-09-16T12:00:00+00:00",
        "summary": {
            "observed_model_configurations": 3,
            "terminal_samples": 18,
            "quality_scored_samples": 12,
            "infrastructure_failures_excluded_from_model_score": 4,
            "external_failures_excluded_from_model_score": 1,
            "unknown_samples": 1,
        },
        "attribution_counts": {"model": 10, "infrastructure": 4, "external": 1, "mixed": 2, "unknown": 1},
        "model_groups": [
            {
                "identity": {"provider": "codex-native", "model": "gpt-5.6-sol", "auth_mode": "host", "reasoning_effort": "high", "execution_role": "controller", "policy_class": "general", "execution_transport": "codex_native_subagent", "project": "SelfAlone"},
                "project_model_score": 91.0,
                "quality_sample_count": 7,
                "total_sample_count": 7,
                "confidence": "high",
                "benchmark": {"source": "modeldial", "score": 84, "match": "partial", "route": "custom-endpoint"},
                "baseline_comparison": "NOT_COMPARABLE",
            },
            {
                "identity": {"provider": "grok-build", "model": "grok-4.6", "auth_mode": "oauth", "reasoning_effort": "high", "execution_role": "writer", "policy_class": "backend", "execution_transport": "external_process", "project": "SelfAlone"},
                "project_model_score": 84.0,
                "quality_sample_count": 5,
                "total_sample_count": 9,
                "confidence": "high",
                "benchmark": {"source": "modeldial", "score": 79.5, "match": "exact", "route": "grok-build"},
                "baseline_comparison": "IN_LINE",
            },
            {
                "identity": {"provider": "kimi-code", "model": "kimi-k3", "auth_mode": "api", "reasoning_effort": "medium", "execution_role": "writer", "policy_class": "frontend", "execution_transport": "external_process", "project": "SelfAlone"},
                "project_model_score": 88.0,
                "quality_sample_count": 4,
                "total_sample_count": 4,
                "confidence": "medium",
                "benchmark": {"source": "modeldial", "match": "none"},
                "baseline_comparison": "NOT_COMPARABLE",
            },
        ],
        "route_groups": [
            {"route": {"provider": "codex-native", "model": "gpt-5.6-sol", "auth_mode": "host", "execution_transport": "codex_native_subagent"}, "route_reliability_score": 96.0, "eligible_attempts": 7, "successful_transport_attempts": 7, "failed_transport_attempts": 0, "result_unknown_count": 0},
            {"route": {"provider": "grok-build", "model": "grok-4.6", "auth_mode": "oauth", "execution_transport": "external_process"}, "route_reliability_score": 68.0, "eligible_attempts": 8, "successful_transport_attempts": 5, "failed_transport_attempts": 3, "result_unknown_count": 1},
            {"route": {"provider": "kimi-code", "model": "kimi-k3", "auth_mode": "api", "execution_transport": "external_process"}, "route_reliability_score": 92.0, "eligible_attempts": 4, "successful_transport_attempts": 4, "failed_transport_attempts": 0, "result_unknown_count": 0},
        ],
        "decisions": [
            {"identity": {"model": "gpt-5.6-sol", "reasoning_effort": "high", "execution_role": "controller", "policy_class": "general", "provider": "codex-native"}, "action": "KEEP", "confidence": "high", "reason_codes": ["project_evidence_supports_current_route"], "project_model_score": 91.0, "route_reliability_score": 96.0, "quality_sample_count": 7, "suggested_target": None},
            {"identity": {"model": "grok-4.6", "reasoning_effort": "high", "execution_role": "writer", "policy_class": "backend", "provider": "grok-build"}, "action": "CHANGE_ROUTE", "confidence": "high", "reason_codes": ["route_reliability_degraded"], "project_model_score": 84.0, "route_reliability_score": 68.0, "quality_sample_count": 5, "suggested_target": None},
            {"identity": {"model": "kimi-k3", "reasoning_effort": "medium", "execution_role": "writer", "policy_class": "frontend", "provider": "kimi-code"}, "action": "KEEP", "confidence": "medium", "reason_codes": ["project_evidence_supports_current_route"], "project_model_score": 88.0, "route_reliability_score": 92.0, "quality_sample_count": 4, "suggested_target": None},
        ],
        "samples": [
            {"assignment_id": "A1", "task_id": "T1", "identity": {"model": "grok-4.6", "reasoning_effort": "high", "execution_role": "writer", "policy_class": "backend", "provider": "grok-build"}, "delivery_outcome": "pass", "transport_outcome": "completed", "attribution": "model", "terminal_at": "2026-09-16T10:00:00+00:00"}
        ],
        "thresholds": {"route_degraded_below": 75},
    }


class DashboardRenderTests(unittest.TestCase):
    def test_dashboard_is_self_contained_and_has_required_sections(self):
        html = render_dashboard(sample_report())
        self.assertTrue(html.startswith("<!doctype html>"))
        lowered = html.lower()
        self.assertNotIn('src="http', lowered)
        self.assertNotIn("src='http", lowered)
        self.assertNotIn('href="http', lowered)
        for section in ["overview", "model-comparison", "decisions", "attribution", "route-health", "evidence"]:
            self.assertIn(f'id="{section}"', html)
        self.assertIn("<svg", html)
        self.assertIn("model-orbit", html)
        self.assertIn("energy-core", html)

    def test_dashboard_escapes_untrusted_text_and_script_embedding(self):
        html = render_dashboard(sample_report())
        self.assertNotIn("SelfAlone <script>alert(1)</script>", html)
        self.assertIn("SelfAlone &lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertNotIn("</script><script>", html)

    def test_dashboard_renders_project_route_and_external_scores_side_by_side(self):
        html = render_dashboard(sample_report())
        self.assertIn("Project score", html)
        self.assertIn("Route reliability", html)
        self.assertIn("External baseline", html)
        self.assertIn("gpt-5.6-sol", html)
        self.assertIn("grok-4.6", html)
        self.assertIn("kimi-k3", html)
        self.assertIn("CHANGE_ROUTE", html)

    def test_dashboard_matches_bright_editorial_scifi_reference_language(self):
        html = render_dashboard(sample_report())
        self.assertIn("--paper:", html)
        self.assertIn("--ink:", html)
        self.assertIn("radial-gradient", html)
        self.assertIn("letter-spacing", html)
        self.assertIn("text-transform:uppercase", html.replace(" ", ""))
        self.assertNotIn("background:#000", html.replace(" ", "").lower())


if __name__ == "__main__":
    unittest.main()
