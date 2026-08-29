import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.project_state import adaptive_delivery_state_dir, git_common_dir, repository_root
from scripts.assignment_runtime import runtime_state_path


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)
    return result.stdout.strip()


class ProjectStatePathTests(unittest.TestCase):
    def test_main_and_linked_worktree_share_adaptive_delivery_state_root(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            repo = base / "repo"
            wt = base / "wt"
            repo.mkdir()
            git(repo, "init", "-b", "main")
            git(repo, "config", "user.email", "test@example.com")
            git(repo, "config", "user.name", "Test")
            (repo / "README.md").write_text("x\n", encoding="utf-8")
            git(repo, "add", "README.md")
            git(repo, "commit", "-m", "init")
            git(repo, "worktree", "add", str(wt), "-b", "worker")

            self.assertEqual(repository_root(repo), repo.resolve())
            self.assertEqual(repository_root(wt), wt.resolve())
            self.assertEqual(git_common_dir(repo), git_common_dir(wt))
            self.assertEqual(adaptive_delivery_state_dir(repo), adaptive_delivery_state_dir(wt))
            self.assertEqual(runtime_state_path(repo), runtime_state_path(wt))
            self.assertEqual(runtime_state_path(repo).name, "runtime-assignments.json")


if __name__ == "__main__":
    unittest.main()
