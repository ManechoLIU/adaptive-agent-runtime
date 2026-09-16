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
