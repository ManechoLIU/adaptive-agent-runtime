import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from scripts.install_skill import install_skill
from scripts.rule_handshake import (
    acknowledge_rule_revision,
    evaluate_rule_handshake,
    rule_state_path,
)

UTC = timezone.utc
NOW = datetime(2026, 8, 30, 1, 0, tzinfo=UTC)


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)
    return result.stdout.strip()


def make_source(base: Path) -> tuple[Path, str]:
    source = base / "source"
    source.mkdir()
    git(source, "init", "-b", "main")
    git(source, "config", "user.email", "test@example.com")
    git(source, "config", "user.name", "Test")
    (source / "SKILL.md").write_text("# Skill\n", encoding="utf-8")
    (source / "scripts").mkdir()
    (source / "scripts" / "x.py").write_text("VALUE = 1\n", encoding="utf-8")
    git(source, "add", ".")
    git(source, "commit", "-m", "initial")
    return source, git(source, "rev-parse", "HEAD")


def make_project(base: Path, revision_text: str = "old") -> Path:
    repo = base / "project"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / "TASK_LEDGER.md").write_text(
        f"# Tasks\n\n- 规则版本：`adaptive-delivery@{revision_text}`\n\n| ID | 状态 | 证据 | 下一步 |\n| --- | --- | --- | --- |\n| `F1` | `ACTIVE` | x | y |\n",
        encoding="utf-8",
    )
    git(repo, "add", "TASK_LEDGER.md")
    git(repo, "commit", "-m", "init")
    return repo


class RuleHandshakeTests(unittest.TestCase):
    def test_install_manifest_records_exact_revision_and_hashes(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            source, revision = make_source(base)
            target = base / "installed"
            manifest = install_skill(
                source,
                target,
                summary="runtime governance",
                impact="live_assignments",
                stop_condition="load exact revision before launch",
                previous_revision="deadbeef",
                now=NOW,
            )
            self.assertEqual(manifest["revision"], revision)
            self.assertEqual(manifest["previous_revision"], "deadbeef")
            self.assertEqual(manifest["impact"], "live_assignments")
            self.assertEqual(set(manifest["files"]), {"SKILL.md", "scripts/x.py"})
            self.assertTrue((target / ".adaptive-delivery-install.json").is_file())

    def test_pending_ack_wrong_revision_unregistered_and_tamper_fail_closed(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            source, revision = make_source(base)
            target = base / "installed"
            install_skill(source, target, summary="rules", impact="live_assignments", stop_condition="ack", now=NOW)
            repo = make_project(base)
            registry = base / "controllers.json"
            registry.write_text(json.dumps({"controller-1": str(repo.resolve())}), encoding="utf-8")

            pending = evaluate_rule_handshake(repo, skill_root=target, registry_path=registry)
            self.assertEqual(pending["state"], "pending_ack")
            self.assertTrue(pending["blocking"])
            with self.assertRaisesRegex(ValueError, "does not match installed revision"):
                acknowledge_rule_revision(repo, "controller-1", "wrong", skill_root=target, registry_path=registry, now=NOW)
            with self.assertRaisesRegex(ValueError, "registered controller"):
                acknowledge_rule_revision(repo, "other", revision, skill_root=target, registry_path=registry, now=NOW)

            (target / "SKILL.md").write_text("tampered\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "installation integrity"):
                acknowledge_rule_revision(repo, "controller-1", revision, skill_root=target, registry_path=registry, now=NOW)
            integrity = evaluate_rule_handshake(repo, skill_root=target, registry_path=registry)
            self.assertEqual(integrity["state"], "integrity_error")
            self.assertTrue(integrity["blocking"])

    def test_ack_then_ledger_sync_reaches_current_and_state_is_shared_by_worktree(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            source, revision = make_source(base)
            target = base / "installed"
            install_skill(source, target, summary="rules", impact="live_assignments", stop_condition="ack", now=NOW)
            repo = make_project(base)
            wt = base / "worker"
            git(repo, "worktree", "add", str(wt), "-b", "worker")
            registry = base / "controllers.json"
            registry.write_text(json.dumps({"controller-1": str(repo.resolve())}), encoding="utf-8")

            receipt = acknowledge_rule_revision(
                repo, "controller-1", revision, skill_root=target, registry_path=registry, now=NOW
            )
            self.assertEqual(receipt["loaded_revision"], revision)
            self.assertEqual(rule_state_path(repo), rule_state_path(wt))
            stale = evaluate_rule_handshake(repo, skill_root=target, registry_path=registry)
            self.assertEqual(stale["state"], "ledger_stale")
            self.assertTrue(stale["blocking"])

            ledger = repo / "TASK_LEDGER.md"
            ledger.write_text(ledger.read_text(encoding="utf-8").replace("adaptive-delivery@old", f"adaptive-delivery@{revision}"), encoding="utf-8")
            current = evaluate_rule_handshake(repo, skill_root=target, registry_path=registry)
            self.assertEqual(current["state"], "current")
            self.assertFalse(current["blocking"])
            current_from_wt = evaluate_rule_handshake(wt, ledger=ledger, skill_root=target, registry_path=registry)
            self.assertEqual(current_from_wt["loaded_revision"], revision)

    def test_later_nonimpacting_install_cannot_clear_unacked_live_impact_debt(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            source, revision1 = make_source(base)
            target = base / "installed"
            repo = make_project(base, revision_text=revision1)
            registry = base / "controllers.json"
            registry.write_text(json.dumps({"controller-1": str(repo.resolve())}), encoding="utf-8")

            install_skill(source, target, summary="baseline", impact="none", stop_condition="none", now=NOW)
            acknowledge_rule_revision(repo, "controller-1", revision1, skill_root=target, registry_path=registry, now=NOW)

            critical = source / "scripts" / "run_external_agent.mjs"
            critical.write_text("export const route = 2;\n", encoding="utf-8")
            git(source, "add", ".")
            git(source, "commit", "-m", "live routing change")
            revision2 = git(source, "rev-parse", "HEAD")
            install_skill(
                source, target, summary="live routing", impact="live_assignments",
                stop_condition="ack before launch", previous_revision=revision1, now=NOW,
            )

            (source / "README.md").write_text("docs only\n", encoding="utf-8")
            git(source, "add", ".")
            git(source, "commit", "-m", "docs only")
            revision3 = git(source, "rev-parse", "HEAD")
            install_skill(
                source, target, summary="docs only", impact="none", stop_condition="next turn",
                previous_revision=revision2, now=NOW,
            )

            result = evaluate_rule_handshake(repo, skill_root=target, registry_path=registry)
            self.assertEqual(result["loaded_revision"], revision1)
            self.assertEqual(result["installed_revision"], revision3)
            self.assertEqual(result["state"], "pending_ack")
            self.assertTrue(result["blocking"])
            self.assertEqual(result["effective_impact"], "live_assignments")
            self.assertIn("scripts/run_external_agent.mjs", result["unacked_changed_files"])

    def test_loaded_controller_with_only_unacked_nonimpacting_changes_stays_nonblocking(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            source, revision1 = make_source(base)
            target = base / "installed"
            repo = make_project(base, revision_text=revision1)
            registry = base / "controllers.json"
            registry.write_text(json.dumps({"controller-1": str(repo.resolve())}), encoding="utf-8")
            install_skill(source, target, summary="baseline", impact="none", stop_condition="none", now=NOW)
            acknowledge_rule_revision(repo, "controller-1", revision1, skill_root=target, registry_path=registry, now=NOW)

            (source / "README.md").write_text("docs only\n", encoding="utf-8")
            git(source, "add", ".")
            git(source, "commit", "-m", "docs only")
            revision2 = git(source, "rev-parse", "HEAD")
            install_skill(source, target, summary="docs only", impact="none", stop_condition="next turn", previous_revision=revision1, now=NOW)

            result = evaluate_rule_handshake(repo, skill_root=target, registry_path=registry)
            self.assertEqual(result["installed_revision"], revision2)
            self.assertEqual(result["effective_impact"], "none")
            self.assertFalse(result["blocking"])

    def test_unverifiable_cumulative_change_range_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            source, revision = make_source(base)
            target = base / "installed"
            repo = make_project(base)
            install_skill(source, target, summary="docs only", impact="none", stop_condition="next turn", now=NOW)
            state_path = rule_state_path(repo)
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(json.dumps({"loaded_revision": "missing-old-revision", "controller_session_id": "controller-1"}), encoding="utf-8")
            result = evaluate_rule_handshake(repo, skill_root=target, registry_path=base / "missing.json")
            self.assertEqual(result["effective_impact"], "live_assignments")
            self.assertTrue(result["blocking"])

    def test_nonimpacting_update_surfaces_drift_without_blocking_launch(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            source, revision = make_source(base)
            target = base / "installed"
            install_skill(source, target, summary="docs only", impact="none", stop_condition="none", now=NOW)
            repo = make_project(base)
            result = evaluate_rule_handshake(repo, skill_root=target, registry_path=base / "missing.json")
            self.assertEqual(result["installed_revision"], revision)
            self.assertEqual(result["state"], "pending_ack")
            self.assertFalse(result["blocking"])


if __name__ == "__main__":
    unittest.main()
