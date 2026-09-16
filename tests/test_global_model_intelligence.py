import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from scripts.assignment_runtime import runtime_state_path

UTC = timezone.utc
NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def make_repo(root: Path, name: str) -> Path:
    repo = root / name
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / "README.md").write_text(f"# {name}\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"], check=True)
    return repo


def terminal_lease(assignment_id: str, *, model: str = "grok-4.6", provider: str = "grok-build") -> dict:
    auth = "oauth" if provider == "grok-build" else "host"
    transport = "external_process" if provider == "grok-build" else "codex_native_subagent"
    return {
        "assignment_id": assignment_id,
        "task_id": f"task-{assignment_id}",
        "provider": provider,
        "model": model,
        "auth_mode": auth,
        "execution_transport": transport,
        "execution_role": "writer",
        "policy_class": "backend",
        "strategy": f"provider={provider};model={model};auth_mode={auth};reasoning_effort=high",
        "started_at": "2026-09-16T10:00:00+00:00",
        "terminal_at": "2026-09-16T10:05:00+00:00",
        "terminal_state": "completed",
        "transport_outcome": "completed",
        "delivery_outcome": "pass",
        "evidence": ["test-log:focused"],
        "artifacts": ["git:abc123"],
        "result_unknown": False,
        "retry_class": "none",
        "recovery_count": 0,
    }


def write_runtime(repo: Path, leases: dict[str, dict]) -> None:
    path = runtime_state_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": 2, "leases": leases, "lineages": {}}), encoding="utf-8")


class GlobalRepositoryDiscoveryTests(unittest.TestCase):
    def test_registry_explicit_and_common_dir_deduplicate_without_home_scan(self):
        from scripts.global_model_intelligence import discover_repositories

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo_a = make_repo(root, "alpha")
            repo_b = make_repo(root, "beta")
            worktree = root / "alpha-wt"
            subprocess.run(["git", "-C", str(repo_a), "worktree", "add", "-q", str(worktree), "-b", "feature"], check=True)
            registry = root / "registry.json"
            registry.write_text(json.dumps({
                "controller-a": str(repo_a),
                "__controller_sessions__": {"ignored": True},
                "metadata": {"also": "ignored"},
            }), encoding="utf-8")

            repos, diagnostics = discover_repositories(registry, [worktree, repo_b])

            self.assertEqual(len(repos), 2)
            self.assertEqual(diagnostics, [])
            common_dirs = {row["project_common_dir"] for row in repos}
            self.assertEqual(len(common_dirs), 2)
            alpha = next(row for row in repos if Path(row["project_root"]).name == "alpha")
            self.assertEqual(alpha["source"], "registry+explicit")
            self.assertTrue(alpha["project_id"])

    def test_invalid_or_missing_repo_is_diagnostic_not_negative_evidence(self):
        from scripts.global_model_intelligence import discover_repositories

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            registry = root / "registry.json"
            registry.write_text(json.dumps({"controller-x": str(root / "missing")}), encoding="utf-8")
            not_git = root / "not-git"
            not_git.mkdir()

            repos, diagnostics = discover_repositories(registry, [not_git])

            self.assertEqual(repos, [])
            self.assertEqual(len(diagnostics), 2)
            self.assertTrue(all(item["error"] for item in diagnostics))


class GlobalSampleCollectionTests(unittest.TestCase):
    def test_samples_keep_origin_project_and_same_assignment_id_does_not_collide(self):
        from scripts.global_model_intelligence import collect_global_samples, discover_repositories

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo_a = make_repo(root, "alpha")
            repo_b = make_repo(root, "beta")
            write_runtime(repo_a, {"same": terminal_lease("same")})
            write_runtime(repo_b, {"same": terminal_lease("same", model="gpt-5.6-sol", provider="codex-native")})
            registry = root / "registry.json"
            registry.write_text("{}", encoding="utf-8")
            repos, _ = discover_repositories(registry, [repo_a, repo_b])

            samples, diagnostics = collect_global_samples(repos, window_days=30, now=NOW)

            self.assertEqual(diagnostics, [])
            self.assertEqual(len(samples), 2)
            self.assertEqual({sample["assignment_id"] for sample in samples}, {"same"})
            self.assertEqual({sample["identity"]["project"] for sample in samples}, {"alpha", "beta"})
            self.assertEqual(len({sample["project_id"] for sample in samples}), 2)
            for sample in samples:
                self.assertTrue(sample["project_root"])
                self.assertTrue(sample["project_common_dir"])


if __name__ == "__main__":
    unittest.main()

from scripts.project_model_score import normalize_lease_sample


def normalized_global_sample(project: str, project_id: str, assignment_id: str, **overrides) -> dict:
    lease = terminal_lease(assignment_id, model=overrides.pop("model", "grok-4.6"), provider=overrides.pop("provider", "grok-build"))
    lease.update(overrides)
    sample = normalize_lease_sample(project, assignment_id, lease)
    assert sample is not None
    sample["project_id"] = project_id
    sample["project_root"] = f"/tmp/{project}"
    sample["project_common_dir"] = f"/tmp/{project}/.git"
    return sample


class GlobalModelSummaryTests(unittest.TestCase):
    def test_one_model_across_projects_is_one_summary_with_capped_project_contribution(self):
        from scripts.global_model_intelligence import score_global_model_summaries

        samples = []
        # A high-volume project has many semantic failures; a second project has one strong pass.
        for idx in range(10):
            samples.append(normalized_global_sample(
                "Alpha", "alpha", f"bad-{idx}", delivery_outcome="fail", evidence=["test-log:red"], artifacts=[]
            ))
        samples.append(normalized_global_sample("Beta", "beta", "good"))

        summaries = score_global_model_summaries(samples)

        self.assertEqual(len(summaries), 1)
        summary = summaries[0]
        self.assertEqual(summary["identity"]["model"], "grok-4.6")
        self.assertEqual(summary["projects_observed"], 2)
        self.assertEqual(summary["raw_quality_sample_count"], 11)
        self.assertLess(summary["quality_sample_count"], 11)  # project cap applied
        self.assertEqual({row["project"] for row in summary["project_breakdown"]}, {"Alpha", "Beta"})
        self.assertIsNotNone(summary["project_model_score"])

    def test_model_with_only_infrastructure_history_has_no_global_quality_score(self):
        from scripts.global_model_intelligence import score_global_model_summaries

        samples = [normalized_global_sample(
            "SelfAlone", "self", "timeout", terminal_state="failed", transport_outcome="failed",
            delivery_outcome="unresolved", failure_class="provider_timeout", evidence=[], artifacts=[]
        )]
        summary = score_global_model_summaries(samples)[0]
        self.assertIsNone(summary["project_model_score"])
        self.assertEqual(summary["quality_sample_count"], 0)
        self.assertEqual(summary["excluded_infrastructure_count"], 1)


class GlobalConfigurationCohortTests(unittest.TestCase):
    def test_same_config_combines_across_projects_but_effort_role_and_route_stay_separate(self):
        from scripts.global_model_intelligence import score_global_configuration_groups

        samples = [
            normalized_global_sample("Alpha", "a", "a-high"),
            normalized_global_sample("Beta", "b", "b-high"),
            normalized_global_sample("Beta", "b", "b-xhigh", reasoning_effort="xhigh", strategy="provider=grok-build;model=grok-4.6;auth_mode=oauth;reasoning_effort=xhigh"),
            normalized_global_sample("Beta", "b", "b-review", execution_role="reviewer"),
            normalized_global_sample("Gamma", "c", "c-api", auth_mode="api", strategy="provider=grok-build;model=grok-4.6;auth_mode=api;reasoning_effort=high"),
        ]

        groups = score_global_configuration_groups(samples, benchmarks=[])

        self.assertEqual(len(groups), 4)
        high_writer_oauth = next(g for g in groups if g["identity"]["reasoning_effort"] == "high" and g["identity"]["execution_role"] == "writer" and g["identity"]["auth_mode"] == "oauth")
        self.assertEqual(high_writer_oauth["projects_observed"], 2)
        self.assertEqual(high_writer_oauth["quality_sample_count"], 2)
        self.assertEqual({row["project"] for row in high_writer_oauth["project_breakdown"]}, {"Alpha", "Beta"})


class GlobalReportTests(unittest.TestCase):
    def test_global_report_contains_project_reports_and_global_groups(self):
        from scripts.global_model_intelligence import build_global_report_from_samples

        repositories = [
            {"project_id": "a", "project_name": "Alpha", "project_root": "/tmp/Alpha", "project_common_dir": "/tmp/Alpha/.git", "source": "explicit"},
            {"project_id": "b", "project_name": "Beta", "project_root": "/tmp/Beta", "project_common_dir": "/tmp/Beta/.git", "source": "explicit"},
        ]
        samples = [
            normalized_global_sample("Alpha", "a", "a"),
            normalized_global_sample("Beta", "b", "b", model="gpt-5.6-sol", provider="codex-native"),
        ]

        report = build_global_report_from_samples(repositories, samples, window_days=30, benchmarks=[], diagnostics=[])

        self.assertEqual(report["scope"], "global")
        self.assertEqual(report["summary"]["projects_observed"], 2)
        self.assertEqual(len(report["model_summaries"]), 2)
        self.assertEqual(len(report["configuration_groups"]), 2)
        self.assertEqual(len(report["route_groups"]), 2)
        self.assertEqual(set(report["project_reports"]), {"Alpha", "Beta"})

class GlobalDecisionTests(unittest.TestCase):
    def _report(self, samples, now=NOW, benchmarks=None):
        from scripts.global_model_intelligence import build_global_report_from_samples
        projects = sorted({
            (sample["project_id"], sample["identity"]["project"])
            for sample in samples
        })
        repositories = [
            {"project_id": pid, "project_name": name, "project_root": f"/tmp/{name}", "project_common_dir": f"/tmp/{name}/.git", "source": "explicit"}
            for pid, name in projects
        ]
        return build_global_report_from_samples(
            repositories, samples, window_days=30, benchmarks=benchmarks or [], diagnostics=[], now=now
        )

    def test_insufficient_global_quality_stays_insufficient_even_with_public_benchmark(self):
        samples = [normalized_global_sample(
            "Alpha", "a", "timeout", terminal_state="failed", transport_outcome="failed",
            delivery_outcome="unresolved", failure_class="provider_timeout", evidence=[], artifacts=[]
        )]
        report = self._report(samples, benchmarks=[{"model": "grok-4.6", "effort": "high", "route": "grok-build", "backend_score": 99}])
        summary = report["model_summaries"][0]
        self.assertEqual(summary["recommended_action"], "INSUFFICIENT_EVIDENCE")

    def test_strong_quality_with_degraded_route_recommends_route_fix(self):
        samples = [normalized_global_sample("Alpha", "a", f"good-{idx}") for idx in range(3)]
        samples.extend([
            normalized_global_sample("Alpha", "a", "timeout-1", terminal_at="2026-09-16T10:10:00+00:00", terminal_state="failed", transport_outcome="failed", delivery_outcome="unresolved", failure_class="provider_timeout", evidence=[], artifacts=[]),
            normalized_global_sample("Alpha", "a", "timeout-2", terminal_at="2026-09-16T10:20:00+00:00", terminal_state="failed", transport_outcome="failed", delivery_outcome="unresolved", failure_class="provider_timeout", evidence=[], artifacts=[]),
        ])
        report = self._report(samples)
        summary = report["model_summaries"][0]
        self.assertEqual(summary["recommended_action"], "CHANGE_ROUTE")
        self.assertEqual(summary["route_status"], "修复后待复测")
        self.assertEqual(report["route_health"][0]["state"], "PROBE_REQUIRED")

    def test_same_model_strong_reviewer_and_weak_writer_recommends_role_change(self):
        samples = []
        for idx in range(3):
            samples.append(normalized_global_sample("Alpha", "a", f"writer-{idx}", delivery_outcome="fail", evidence=["test-log:red"], artifacts=[]))
            samples.append(normalized_global_sample("Alpha", "a", f"review-{idx}", execution_role="reviewer"))
        report = self._report(samples)
        summary = report["model_summaries"][0]
        self.assertEqual(summary["recommended_action"], "CHANGE_ROLE")
        self.assertIn("reviewer", summary["preferred_roles"])

    def test_healthy_weak_model_can_switch_to_stronger_observed_alternative(self):
        samples = []
        for idx in range(5):
            samples.append(normalized_global_sample("Alpha", "a", f"grok-{idx}", delivery_outcome="fail", evidence=["test-log:red"], artifacts=[]))
            samples.append(normalized_global_sample(
                "Alpha", "a", f"sol-{idx}", model="gpt-5.6-sol", provider="codex-native",
                strategy="provider=codex-native;model=gpt-5.6-sol;auth_mode=host;reasoning_effort=high"
            ))
        report = self._report(samples)
        grok = next(item for item in report["model_summaries"] if item["identity"]["model"] == "grok-4.6")
        self.assertEqual(grok["recommended_action"], "SWITCH_MODEL")
        self.assertEqual(grok["suggested_target"]["model"], "gpt-5.6-sol")

class GlobalCliTests(unittest.TestCase):
    def test_report_cli_discovers_registry_and_explicit_repos_writes_json_and_cache(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            home.mkdir()
            repo_a = make_repo(root, "alpha")
            repo_b = make_repo(root, "beta")
            write_runtime(repo_a, {"a": terminal_lease("a")})
            write_runtime(repo_b, {"b": terminal_lease("b", model="gpt-5.6-sol", provider="codex-native")})
            registry = root / "registry.json"
            registry.write_text(json.dumps({"controller-a": str(repo_a)}), encoding="utf-8")
            output = root / "global.json"
            env = {**__import__("os").environ, "HOME": str(home)}

            result = subprocess.run([
                "python3", "scripts/global_model_intelligence.py", "report",
                "--registry", str(registry), "--repo", str(repo_b),
                "--window-days", "30", "--json", str(output),
                "--now", "2026-09-16T12:00:00+00:00",
            ], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, env=env)

            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(report["scope"], "global")
            self.assertEqual([row["project_name"] for row in report["projects"]], ["alpha", "beta"])
            self.assertEqual([row["identity"]["model"] for row in report["model_summaries"]], ["gpt-5.6-sol", "grok-4.6"])
            cached = home / ".codex" / "adaptive-delivery" / "model-intelligence" / "latest.json"
            self.assertTrue(cached.exists())
            self.assertEqual(json.loads(cached.read_text(encoding="utf-8"))["scope"], "global")

    def test_invalid_repo_is_reported_and_does_not_fail_valid_global_report(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = make_repo(root, "alpha")
            write_runtime(repo, {"a": terminal_lease("a")})
            registry = root / "registry.json"
            registry.write_text("{}", encoding="utf-8")
            output = root / "global.json"
            env = {**__import__("os").environ, "HOME": str(root / "home")}

            result = subprocess.run([
                "python3", "scripts/global_model_intelligence.py", "report",
                "--registry", str(registry), "--repo", str(repo), "--repo", str(root / "missing"),
                "--json", str(output), "--now", "2026-09-16T12:00:00+00:00",
            ], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, env=env)

            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(report["summary"]["projects_observed"], 1)
            self.assertEqual(len(report["diagnostics"]), 1)
            self.assertIn("missing", report["diagnostics"][0]["repo"])
