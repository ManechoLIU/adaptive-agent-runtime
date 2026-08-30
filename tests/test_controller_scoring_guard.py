from pathlib import Path
import importlib.util
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "controller_scoring_guard.py"


def load_module():
    spec = importlib.util.spec_from_file_location("controller_scoring_guard", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


class ControllerScoringGuardTests(unittest.TestCase):
    def test_scoring_is_blocked_without_fresh_model_read_receipt(self):
        guard = load_module()
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            import subprocess
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            errors = guard.score_guard_errors(repo, skill_root=ROOT)
        self.assertTrue(any("scoring model read receipt" in error for error in errors))

    def test_record_read_then_guard_passes_for_exact_model_digest(self):
        guard = load_module()
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            import subprocess
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            receipt = guard.record_model_read(repo, skill_root=ROOT)
            self.assertEqual(guard.scoring_model_sha256(ROOT), receipt["model_sha256"])
            self.assertEqual([], guard.score_guard_errors(repo, skill_root=ROOT))

    def test_model_change_invalidates_old_receipt(self):
        guard = load_module()
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as sd:
            repo = Path(td)
            import subprocess
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            skill = Path(sd)
            (skill / "references").mkdir()
            model = skill / "references" / "controller-performance-scoring.md"
            model.write_text("version one\n", encoding="utf-8")
            guard.record_model_read(repo, skill_root=skill)
            model.write_text("version two\n", encoding="utf-8")
            errors = guard.score_guard_errors(repo, skill_root=skill)
        self.assertTrue(any("stale" in error for error in errors))

    def test_linked_worktrees_share_one_scoring_receipt_store(self):
        guard = load_module()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "repo"
            linked = Path(td) / "linked"
            import subprocess
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
            (root / "x").write_text("x", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "x"], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-qm", "init"], check=True)
            subprocess.run(["git", "-C", str(root), "worktree", "add", "-qb", "linked", str(linked)], check=True)
            guard.record_model_read(root, skill_root=ROOT)
            self.assertEqual(guard.receipt_path(root), guard.receipt_path(linked))
            self.assertEqual([], guard.score_guard_errors(linked, skill_root=ROOT))

    def test_record_read_cli_prints_model_content_before_receipt(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            import subprocess
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            completed = subprocess.run([
                "python3", str(SCRIPT), "record-read", "--repo", str(repo), "--skill-root", str(ROOT)
            ], check=True, capture_output=True, text=True)
            self.assertIn("# Controller Performance Scoring", completed.stdout)
            self.assertIn("七维评分模型", completed.stdout)
            self.assertIn('"model_sha256"', completed.stdout)


if __name__ == "__main__":
    unittest.main()
