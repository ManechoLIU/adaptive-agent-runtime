import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.reviewer_supervisor import ReviewContract, AttemptResult, run_attempt, run_review, git_common_state_root, validate_verdict


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
        self.assertEqual(argv[-3:], ["review", "--base", "main"])
        self.assertNotIn(contract.instructions, argv)
        self.assertEqual(kwargs["input"], contract.instructions)
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


class ReviewerSupervisorRunTests(unittest.TestCase):
    def _write_final(self, contract, verdict="PASS", head=None):
        contract.final_path.write_text(json.dumps({
            "reviewed_head": head or contract.head,
            "verdict": verdict,
            "critical": [], "important": [], "minor": [],
        }))

    def test_findings_are_terminal_and_not_retried(self):
        calls = []
        def attempt(contract, number):
            calls.append(number); self._write_final(contract, "FINDINGS")
            return AttemptResult("RUNNING", 1, 0, True, "s")
        result = run_review(Path.cwd(), "main", "review", attempt_runner=attempt)
        self.assertEqual(result.state, "FINDINGS")
        self.assertEqual(calls, [0])

    def test_infra_failure_retries_once_with_same_contract(self):
        seen = []
        def attempt(contract, number):
            seen.append((contract.repo, contract.base, contract.head, contract.instructions))
            if number == 0:
                return AttemptResult("REVIEW_INFRA_FAILED", 1, 1, False, None, "start failed")
            self._write_final(contract)
            return AttemptResult("RUNNING", 2, 0, True, "s")
        result = run_review(Path.cwd(), "main", "review", attempt_runner=attempt)
        self.assertEqual(result.state, "PASS")
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0], seen[1])

    def test_exit_zero_without_output_fails_closed_twice(self):
        calls = []
        def attempt(contract, number):
            calls.append(number)
            return AttemptResult("RUNNING", number + 1, 0, True, "s")
        result = run_review(Path.cwd(), "main", "review", attempt_runner=attempt)
        self.assertEqual(result.state, "REVIEW_INFRA_FAILED")
        self.assertEqual(calls, [0, 1])

    def test_revision_mismatch_is_infrastructure_failure(self):
        def attempt(contract, number):
            self._write_final(contract, head="f" * 40)
            return AttemptResult("RUNNING", 1, 0, True, "s")
        result = run_review(Path.cwd(), "main", "review", attempt_runner=attempt, max_infra_retries=0)
        self.assertEqual(result.state, "REVIEW_INFRA_FAILED")


if __name__ == "__main__":
    unittest.main()
