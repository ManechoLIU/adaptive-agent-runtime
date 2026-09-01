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


if __name__ == "__main__":
    unittest.main()
