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
