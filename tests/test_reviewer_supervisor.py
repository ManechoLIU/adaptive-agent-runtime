import json
import subprocess
import time
import tempfile
import unittest
from pathlib import Path

from scripts.reviewer_supervisor import ReviewContract, AttemptResult, build_review_instructions, run_attempt, run_review, git_common_state_root, validate_verdict


class ReviewerSupervisorCoreTests(unittest.TestCase):
    def test_state_root_uses_git_common_dir(self):
        repo = Path.cwd()
        common = subprocess.check_output(["git", "rev-parse", "--git-common-dir"], cwd=repo, text=True).strip()
        common_path = Path(common)
        if not common_path.is_absolute():
            common_path = (repo / common_path).resolve()
        self.assertEqual(git_common_state_root(repo), common_path / "adaptive-delivery" / "reviewer-runs")


    def test_review_instructions_define_full_schema_and_exact_head(self):
        head = "c" * 40
        instructions = build_review_instructions("focus on runtime safety", head)
        self.assertIn(head, instructions)
        self.assertIn('"reviewed_head"', instructions)
        self.assertIn('"verdict"', instructions)
        self.assertIn('"critical"', instructions)
        self.assertIn('"important"', instructions)
        self.assertIn('"minor"', instructions)
        self.assertIn("PASS", instructions)
        self.assertIn("FINDINGS", instructions)
        self.assertIn("focus on runtime safety", instructions)

    def test_pass_requires_all_finding_lists_empty(self):
        head = "a" * 40
        payload = {
            "reviewed_head": head,
            "verdict": "PASS",
            "critical": [],
            "important": [],
            "minor": ["small note"],
        }
        with self.assertRaisesRegex(ValueError, "PASS"):
            validate_verdict(payload, head)

    def test_findings_requires_at_least_one_finding(self):
        head = "a" * 40
        payload = {
            "reviewed_head": head,
            "verdict": "FINDINGS",
            "critical": [],
            "important": [],
            "minor": [],
        }
        with self.assertRaisesRegex(ValueError, "FINDINGS"):
            validate_verdict(payload, head)

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


class _FakeStdin:
    def __init__(self):
        self.value = ""
        self.closed = False

    def write(self, text):
        self.value += text

    def close(self):
        self.closed = True


class _FakeProcess:
    def __init__(self, lines, returncode=0):
        self.stdout = iter(lines)
        self.stdin = _FakeStdin()
        self.pid = 4242
        self._returncode = returncode

    def wait(self, timeout=None):
        return self._returncode


class ReviewerSupervisorLaunchTests(unittest.TestCase):
    def test_direct_child_requires_valid_codex_event_before_running(self):
        calls = []

        proc_holder = []
        def factory(argv, **kwargs):
            calls.append((argv, kwargs))
            proc = _FakeProcess(["not-json\n"], returncode=1)
            proc_holder.append(proc)
            return proc

        contract = ReviewContract(
            repo=Path.cwd(), base="main", head="a" * 40, instructions="review exact head",
            event_path=Path(tempfile.mkdtemp()) / "events.jsonl",
            final_path=Path(tempfile.mkdtemp()) / "final.json",
        )
        result = run_attempt(contract, 0, popen_factory=factory, codex_executable="/usr/bin/codex")
        argv, kwargs = calls[0]
        self.assertEqual(argv[0:2], ["/usr/bin/codex", "exec"])
        self.assertEqual(argv[-3:], ["review", "--base", "main"])
        self.assertIn("--output-schema", argv)
        schema_path = Path(argv[argv.index("--output-schema") + 1])
        schema = json.loads(schema_path.read_text())
        self.assertEqual(schema["required"], ["reviewed_head", "verdict", "critical", "important", "minor"])
        self.assertNotIn(contract.instructions, argv)
        self.assertNotIn("nohup", argv)
        self.assertNotIn("sh", argv)
        self.assertTrue(kwargs["start_new_session"])
        self.assertIs(kwargs["stdin"], subprocess.PIPE)
        self.assertNotIn("input", kwargs)
        self.assertEqual(proc_holder[0].stdin.value, contract.instructions)
        self.assertTrue(proc_holder[0].stdin.closed)
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


class _SlowStdout:
    def __iter__(self):
        return self

    def __next__(self):
        time.sleep(5)
        return json.dumps({"type": "thread.started", "thread_id": "late"}) + "\n"


class _TimeoutProcess(_FakeProcess):
    def __init__(self, *, stdout=None):
        super().__init__([], returncode=-15)
        self.stdout = stdout if stdout is not None else _SlowStdout()
        self.killed = False

    def wait(self, timeout=None):
        if not self.killed:
            raise subprocess.TimeoutExpired("codex", timeout or 0)
        return self._returncode


class ReviewerSupervisorTimeoutTests(unittest.TestCase):
    def test_timeout_kills_process_group_and_fails_closed(self):
        holder = []
        killed = []
        def factory(argv, **kwargs):
            proc = _TimeoutProcess(); holder.append(proc); return proc
        def killer(pid):
            killed.append(pid); holder[0].killed = True

        root = Path(tempfile.mkdtemp())
        contract = ReviewContract(Path.cwd(), "main", "d" * 40, "schema", root / "events", root / "final")
        started = time.monotonic()
        result = run_attempt(contract, 0, popen_factory=factory, codex_executable="codex", timeout_seconds=0.05, process_group_killer=killer)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(killed, [4242])
        self.assertEqual(result.state, "REVIEW_INFRA_FAILED")
        self.assertIn("timeout", result.diagnostic.lower())

    def test_timeout_tracks_child_even_when_stdout_closes_early(self):
        holder = []
        killed = []
        def factory(argv, **kwargs):
            proc = _TimeoutProcess(stdout=iter([])); holder.append(proc); return proc
        def killer(pid):
            killed.append(pid); holder[0].killed = True
        root = Path(tempfile.mkdtemp())
        contract = ReviewContract(Path.cwd(), "main", "e" * 40, "schema", root / "events", root / "final")
        started = time.monotonic()
        result = run_attempt(contract, 0, popen_factory=factory, codex_executable="codex", timeout_seconds=0.05, process_group_killer=killer)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(killed, [4242])
        self.assertEqual(result.state, "REVIEW_INFRA_FAILED")
        self.assertIn("timeout", result.diagnostic.lower())


class ReviewerSupervisorRunTests(unittest.TestCase):
    def setUp(self):
        self._repo_tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._repo_tmp.name)
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.repo, check=True)
        (self.repo / "base.txt").write_text("base")
        subprocess.run(["git", "add", "base.txt"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=self.repo, check=True)

    def tearDown(self):
        self._repo_tmp.cleanup()

    def _write_final(self, contract, verdict="PASS", head=None):
        contract.final_path.write_text(json.dumps({
            "reviewed_head": head or contract.head,
            "verdict": verdict,
            "critical": [], "important": [], "minor": [],
        }))

    def test_findings_are_terminal_and_not_retried(self):
        calls = []
        def attempt(contract, number):
            calls.append(number)
            contract.final_path.write_text(json.dumps({
                "reviewed_head": contract.head, "verdict": "FINDINGS",
                "critical": [], "important": ["issue"], "minor": [],
            }))
            return AttemptResult("RUNNING", 1, 0, True, "s")
        result = run_review(self.repo, "HEAD", "review", attempt_runner=attempt)
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
        result = run_review(self.repo, "HEAD", "review", attempt_runner=attempt)
        self.assertEqual(result.state, "PASS")
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0], seen[1])

    def test_exit_zero_without_output_fails_closed_twice(self):
        calls = []
        def attempt(contract, number):
            calls.append(number)
            return AttemptResult("RUNNING", number + 1, 0, True, "s")
        result = run_review(self.repo, "HEAD", "review", attempt_runner=attempt)
        self.assertEqual(result.state, "REVIEW_INFRA_FAILED")
        self.assertEqual(calls, [0, 1])

    def test_revision_mismatch_is_infrastructure_failure(self):
        def attempt(contract, number):
            self._write_final(contract, head="f" * 40)
            return AttemptResult("RUNNING", 1, 0, True, "s")
        result = run_review(self.repo, "HEAD", "review", attempt_runner=attempt, max_infra_retries=0)
        self.assertEqual(result.state, "REVIEW_INFRA_FAILED")

    def test_base_ref_is_resolved_once_to_immutable_commit(self):
        base_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.repo, text=True).strip()
        seen = []
        def attempt(contract, number):
            seen.append(contract.base)
            self._write_final(contract)
            return AttemptResult("RUNNING", 1, 0, True, "s")
        result = run_review(self.repo, "HEAD", "review", attempt_runner=attempt, max_infra_retries=0)
        self.assertEqual(result.state, "PASS")
        self.assertEqual(seen, [base_sha])
        state = json.loads(result.state_path.read_text())
        self.assertEqual(state["base_ref"], "HEAD")
        self.assertEqual(state["base_revision"], base_sha)

    def test_dirty_worktree_is_rejected_before_attempt(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
            (repo / "x.txt").write_text("one")
            subprocess.run(["git", "add", "x.txt"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
            (repo / "x.txt").write_text("dirty")
            calls = []
            def attempt(contract, number):
                calls.append(number)
                return AttemptResult("RUNNING", 1, 0, True, "s")
            result = run_review(repo, "HEAD~0", "review", attempt_runner=attempt, max_infra_retries=0)
            self.assertEqual(result.state, "REVIEW_INFRA_FAILED")
            self.assertEqual(calls, [])
            state = json.loads(result.state_path.read_text())
            self.assertIn("dirty", state["diagnostic"].lower())

    def test_head_change_during_review_invalidates_pass(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
            (repo / "x.txt").write_text("one")
            subprocess.run(["git", "add", "x.txt"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
            def attempt(contract, number):
                self._write_final(contract)
                (repo / "y.txt").write_text("two")
                subprocess.run(["git", "add", "y.txt"], cwd=repo, check=True)
                subprocess.run(["git", "commit", "-qm", "move head"], cwd=repo, check=True)
                return AttemptResult("RUNNING", 1, 0, True, "s")
            result = run_review(repo, "HEAD", "review", attempt_runner=attempt, max_infra_retries=0)
            self.assertEqual(result.state, "REVIEW_INFRA_FAILED")
            state = json.loads(result.state_path.read_text())
            self.assertIn("head changed", state["diagnostic"].lower())


if __name__ == "__main__":
    unittest.main()
