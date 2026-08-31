import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.reviewer_supervisor import git_common_state_root, validate_verdict


class ReviewerSupervisorCoreTests(unittest.TestCase):
    def test_state_root_uses_git_common_dir(self):
        repo = Path.cwd()
        common = subprocess.check_output(["git", "rev-parse", "--git-common-dir"], cwd=repo, text=True).strip()
        common_path = Path(common)
        if not common_path.is_absolute():
            common_path = (repo / common_path).resolve()
        self.assertEqual(git_common_state_root(repo), common_path / "adaptive-delivery" / "reviewer-runs")

    def test_valid_pass_is_bound_to_exact_head(self):
        head = "a" * 40
        payload = {
            "reviewed_head": head,
            "verdict": "PASS",
            "critical": [],
            "important": [],
            "minor": ["small note"],
        }
        self.assertEqual(validate_verdict(payload, head), payload)

    def test_revision_mismatch_fails_closed(self):
        payload = {
            "reviewed_head": "b" * 40,
            "verdict": "PASS",
            "critical": [],
            "important": [],
            "minor": [],
        }
        with self.assertRaisesRegex(ValueError, "reviewed_head"):
            validate_verdict(payload, "a" * 40)

    def test_severity_fields_must_be_lists(self):
        payload = {
            "reviewed_head": "a" * 40,
            "verdict": "FINDINGS",
            "critical": "bad",
            "important": [],
            "minor": [],
        }
        with self.assertRaisesRegex(ValueError, "critical"):
            validate_verdict(payload, "a" * 40)


if __name__ == "__main__":
    unittest.main()
