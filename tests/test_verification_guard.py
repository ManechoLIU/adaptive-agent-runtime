from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "scripts" / "verification_guard.py"


class VerificationGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        temporary_root = Path(self.temporary_directory.name)
        self.repository = temporary_root / "repository"
        self.repository.mkdir()
        self.counter = temporary_root / "counter.txt"

        subprocess.run(
            ["git", "init", "-q", str(self.repository)],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(self.repository), "config", "user.name", "Test User"],
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(self.repository),
                "config",
                "user.email",
                "test@example.com",
            ],
            check=True,
        )
        (self.repository / "source.txt").write_text("version one\n", encoding="utf-8")

    def guard_command(self, *, force_reason: str | None = None) -> list[str]:
        increment = (
            "from pathlib import Path; import sys; "
            "path = Path(sys.argv[1]); "
            "value = int(path.read_text()) + 1 if path.exists() else 1; "
            "path.write_text(str(value))"
        )
        command = [
            sys.executable,
            str(GUARD),
            "run",
            str(self.repository),
            "--check-id",
            "full-suite",
        ]
        if force_reason is not None:
            command.extend(["--force-reason", force_reason])
        command.extend(["--", sys.executable, "-c", increment, str(self.counter)])
        return command

    def run_guard(self, *, force_reason: str | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self.guard_command(force_reason=force_reason),
            capture_output=True,
            text=True,
        )

    def test_same_snapshot_reuses_passed_verification(self) -> None:
        first = self.run_guard()
        second = self.run_guard()

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("executed", first.stdout)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("reused", second.stdout)
        self.assertEqual(self.counter.read_text(encoding="utf-8"), "1")

    def test_content_change_invalidates_receipt(self) -> None:
        first = self.run_guard()
        (self.repository / "source.txt").write_text("version two\n", encoding="utf-8")
        second = self.run_guard()

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("executed", second.stdout)
        self.assertEqual(self.counter.read_text(encoding="utf-8"), "2")

    def test_commit_metadata_does_not_invalidate_content_receipt(self) -> None:
        first = self.run_guard()
        subprocess.run(
            ["git", "-C", str(self.repository), "add", "source.txt"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.repository), "commit", "-qm", "initial"],
            check=True,
        )
        second = self.run_guard()

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("reused", second.stdout)
        self.assertEqual(self.counter.read_text(encoding="utf-8"), "1")

    def test_force_reason_allows_an_explicit_rerun(self) -> None:
        first = self.run_guard()
        second = self.run_guard(force_reason="output-unavailable")

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("executed", second.stdout)
        self.assertEqual(self.counter.read_text(encoding="utf-8"), "2")

    def test_arbitrary_force_reason_is_rejected(self) -> None:
        first = self.run_guard()
        second = self.run_guard(force_reason="just run it again")

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 2)
        self.assertEqual(self.counter.read_text(encoding="utf-8"), "1")


if __name__ == "__main__":
    unittest.main()
