import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.reviewer_supervisor import ReviewContract, run_attempt, git_common_state_root, validate_verdict


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


class _FakeProcess:
    def __init__(self, lines, returncode=0):
        self.stdout = iter(lines)
        self.pid = 4242
        self._returncode = returncode

    def wait(self):
        return self._returncode


class ReviewerSupervisorLaunchTests(unittest.TestCase):
    def test_direct_child_requires_valid_codex_event_before_running(self):
        calls = []

        def factory(argv, **kwargs):
            calls.append((argv, kwargs))
            return _FakeProcess(["not-json\n"], returncode=1)

        contract = ReviewContract(
            repo=Path.cwd(), base="main", head="a" * 40, instructions="review exact head",
            event_path=Path(tempfile.mkdtemp()) / "events.jsonl",
            final_path=Path(tempfile.mkdtemp()) / "final.json",
        )
        result = run_attempt(contract, 0, popen_factory=factory, codex_executable="/usr/bin/codex")
        argv, kwargs = calls[0]
        self.assertEqual(argv[0:2], ["/usr/bin/codex", "exec"])
        self.assertNotIn("nohup", argv)
        self.assertNotIn("sh", argv)
        self.assertTrue(kwargs["start_new_session"])
        self.assertFalse(result.running_observed)
        self.assertEqual(result.state, "REVIEW_INFRA_FAILED")

    def test_valid_json_event_proves_running(self):
        def factory(argv, **kwargs):
            return _FakeProcess([json.dumps({"type": "thread.started", "thread_id": "thread-1"}) + "\n"], returncode=1)

        contract = ReviewContract(
            repo=Path.cwd(), base="main", head="a" * 40, instructions="review exact head",
            event_path=Path(tempfile.mkdtemp()) / "events.jsonl",
            final_path=Path(tempfile.mkdtemp()) / "final.json",
        )
        result = run_attempt(contract, 0, popen_factory=factory, codex_executable="/usr/bin/codex")
        self.assertTrue(result.running_observed)
        self.assertEqual(result.session_id, "thread-1")
        self.assertEqual(result.pid, 4242)


if __name__ == "__main__":
    unittest.main()
