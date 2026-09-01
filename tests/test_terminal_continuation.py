import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "terminal_continuation.py"


class TerminalContinuationTests(unittest.TestCase):
    def test_terminal_receipt_marks_existing_controller_pending_and_dispatches_wake(self) -> None:
        self.assertTrue(SCRIPT.exists(), "terminal continuation consumer must exist")
        spec = importlib.util.spec_from_file_location("terminal_continuation_test", SCRIPT)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({"controller-1": str(repo.resolve())}), encoding="utf-8")
            receipt = root / "terminal.json"
            receipt.write_text(json.dumps({
                "schema_version": 1,
                "event_type": "external_agent_terminal",
                "engine": "grok-build",
                "model": "grok-4.6",
                "repo": str(repo.resolve()),
                "exit_code": 0,
                "summary": "review finished with findings",
                "result_path": str(root / "review-output.log"),
                "agent_id": "reviewer-1",
            }), encoding="utf-8")
            state_root = root / "state"
            wake_calls = []

            def fake_wake(**kwargs):
                wake_calls.append(kwargs)
                return {"result": "CONFIRMED", "controller_id": kwargs["session_id"]}

            snapshot = {
                "root": str(repo.resolve()), "head": "h1", "ledger_sha256": "l1",
                "worktree_status_sha256": "s1", "ready_ids": [], "runnable_ids": [],
                "candidate_revisions": [], "assignment_liveness": {},
                "rule_handshake": {"state": "current", "installed_revision": "rev-1"},
            }
            with patch.object(module.lifecycle, "STATE_ROOT", state_root), patch.object(
                module.lifecycle, "project_snapshot", return_value=snapshot
            ):
                result = module.consume_terminal_receipt(
                    repo=repo, receipt_path=receipt, registry_path=registry, wake_dispatcher=fake_wake
                )

            state = json.loads((state_root / "controller-1.json").read_text(encoding="utf-8"))
            self.assertTrue(state["pending_control_event"])
            self.assertEqual(state["pending_terminal_receipts"], [str(receipt.resolve())])
            self.assertEqual(result["controller_id"], "controller-1")
            self.assertEqual(len(wake_calls), 1)
            self.assertEqual(wake_calls[0]["session_id"], "controller-1")
            self.assertEqual(wake_calls[0]["lifecycle_state"]["pending_terminal_receipts"], [str(receipt.resolve())])

    def test_missing_receipt_repo_fails_closed_before_controller_attribution(self) -> None:
        spec = importlib.util.spec_from_file_location("terminal_continuation_missing_repo_test", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); repo = root / "repo"; repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({"controller-1": str(repo.resolve())}), encoding="utf-8")
            receipt = root / "terminal.json"
            receipt.write_text(json.dumps({"schema_version": 1, "event_type": "external_agent_terminal", "agent_id": "reviewer-1"}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "repository"):
                module.consume_terminal_receipt(repo=repo, receipt_path=receipt, registry_path=registry)

    def _load_module(self, name: str):
        spec = importlib.util.spec_from_file_location(name, SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_receipt_repo_mismatch_fails_closed(self) -> None:
        module = self._load_module("terminal_continuation_repo_mismatch_test")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); repo = root / "repo"; other = root / "other"
            for target in (repo, other):
                target.mkdir(); subprocess.run(["git", "init", "-q", "-b", "main", str(target)], check=True)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({"controller-1": str(repo.resolve())}), encoding="utf-8")
            receipt = root / "terminal.json"
            receipt.write_text(json.dumps({"event_type": "external_agent_terminal", "repo": str(other.resolve())}), encoding="utf-8")
            with self.assertRaisesRegex(PermissionError, "does not match"):
                module.consume_terminal_receipt(repo=repo, receipt_path=receipt, registry_path=registry)

    def test_zero_registered_controllers_fails_closed(self) -> None:
        module = self._load_module("terminal_continuation_zero_controller_test")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); repo = root / "repo"; repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = root / "controllers.json"; registry.write_text("{}", encoding="utf-8")
            receipt = root / "terminal.json"
            receipt.write_text(json.dumps({"event_type": "external_agent_terminal", "repo": str(repo.resolve())}), encoding="utf-8")
            with self.assertRaisesRegex(PermissionError, "exactly one"):
                module.consume_terminal_receipt(repo=repo, receipt_path=receipt, registry_path=registry)

    def test_two_registered_controllers_for_same_repo_fails_closed(self) -> None:
        module = self._load_module("terminal_continuation_two_controller_test")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); repo = root / "repo"; repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({"controller-1": str(repo.resolve()), "controller-2": str(repo.resolve())}), encoding="utf-8")
            receipt = root / "terminal.json"
            receipt.write_text(json.dumps({"event_type": "external_agent_terminal", "repo": str(repo.resolve())}), encoding="utf-8")
            with self.assertRaisesRegex(PermissionError, "exactly one"):
                module.consume_terminal_receipt(repo=repo, receipt_path=receipt, registry_path=registry)

    def test_cli_stages_pending_state_then_launches_wake_asynchronously(self) -> None:
        spec = importlib.util.spec_from_file_location("terminal_continuation_deferred_test", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with patch.object(module, "consume_terminal_receipt", return_value={"wake_result": None}) as consume, patch.object(module.subprocess, "Popen") as popen:
            self.assertEqual(module.main(["consume", "--repo", "/tmp/repo", "--receipt", "/tmp/receipt.json"]), 0)
        self.assertFalse(consume.call_args.kwargs["dispatch_wake"])
        self.assertTrue(popen.called)
        self.assertTrue(popen.call_args.kwargs["start_new_session"])

    def test_wake_child_deferred_result_does_not_fail_completed_agent(self) -> None:
        spec = importlib.util.spec_from_file_location("terminal_continuation_wake_child_test", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with patch.dict(module.os.environ, {"AD_TERMINAL_CONTINUATION_WAKE_CHILD": "1"}), patch.object(
            module, "consume_terminal_receipt", return_value={"wake_result": {"result": "DEFERRED"}}
        ) as consume:
            self.assertEqual(module.main(["consume", "--repo", "/tmp/repo", "--receipt", "/tmp/receipt.json"]), 0)
        self.assertTrue(consume.call_args.kwargs["dispatch_wake"])


if __name__ == "__main__":
    unittest.main()
