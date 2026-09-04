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

    def test_runtime_health_change_marks_same_controller_pending_and_dispatches_wake(self) -> None:
        spec = importlib.util.spec_from_file_location("terminal_continuation_runtime_change_test", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); repo = root / "repo"; repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({"controller-1": str(repo.resolve())}), encoding="utf-8")
            state_root = root / "state"
            wake_calls = []
            snapshot = {
                "root": str(repo.resolve()), "head": "h1", "ledger_sha256": "l1",
                "worktree_status_sha256": "s1", "ready_ids": [], "runnable_ids": [],
                "candidate_revisions": [], "ledger_errors": [],
                "assignment_liveness": {
                    "T-1": {"ledger_state": "ACTIVE", "state": "progress_stale", "reason": "progress_deadline_exceeded"}
                },
                "rule_handshake": {"state": "current", "installed_revision": "rev-1"},
            }
            def fake_wake(**kwargs):
                wake_calls.append(kwargs)
                return {"result": "CONFIRMED", "controller_id": kwargs["session_id"]}
            with patch.object(module.lifecycle, "STATE_ROOT", state_root), patch.object(
                module.lifecycle, "project_snapshot", return_value=snapshot
            ):
                result = module.notify_runtime_change(
                    repo=repo, registry_path=registry, wake_dispatcher=fake_wake
                )
            state = json.loads((state_root / "controller-1.json").read_text(encoding="utf-8"))
            self.assertTrue(state["pending_control_event"])
            self.assertIn("active_without_progress:T-1", state["triggers"])
            self.assertEqual(result["controller_id"], "controller-1")
            self.assertEqual(len(wake_calls), 1)
            self.assertEqual(wake_calls[0]["session_id"], "controller-1")


    def test_terminal_continuation_uses_locked_lifecycle_transaction(self) -> None:
        module = self._load_module("terminal_continuation_locked_transaction_test")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); repo = root / "repo"; repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({"controller-1": str(repo.resolve())}), encoding="utf-8")
            receipt = root / "terminal.json"
            receipt.write_text(json.dumps({
                "event_type": "external_agent_terminal", "repo": str(repo.resolve()), "agent_id": "agent-1"
            }), encoding="utf-8")
            state_root = root / "state"
            snapshot = {"root": str(repo.resolve()), "assignment_liveness": {}, "ready_ids": [], "runnable_ids": [], "candidate_revisions": [], "rule_handshake": {}}
            next_state = {"pending_control_event": True, "triggers": ["subagent_stop:agent-1"]}
            with patch.object(module.lifecycle, "STATE_ROOT", state_root), patch.object(module.lifecycle, "project_snapshot", return_value=snapshot), patch.object(
                module.lifecycle, "persist_event_state", return_value=({}, next_state)
            ) as persist, patch.object(module.lifecycle, "load_json", side_effect=AssertionError("unlocked load forbidden")):
                result = module.consume_terminal_receipt(
                    repo=repo, receipt_path=receipt, registry_path=registry,
                    wake_dispatcher=lambda **kwargs: {"result": "CONFIRMED"},
                )
            self.assertEqual(result["controller_id"], "controller-1")
            persist.assert_called_once()

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

    def test_deferred_terminal_wake_handoffs_to_existing_continuation_supervisor(self) -> None:
        module = self._load_module("terminal_continuation_supervisor_handoff_test")
        lifecycle_state = {
            "pending_control_event": True,
            "requires_user": False,
            "controller_host": "web",
            "wake_generation": 5,
            "triggers": ["subagent_stopped:writer-1"],
        }
        with patch.object(module.web_bridge, "ensure_continuation_supervisor", return_value=True) as ensure:
            armed = module._handoff_unconfirmed_wake_to_existing_supervisor(
                lifecycle_state=lifecycle_state,
                wake_receipt={"result": "DEFERRED"},
                controller_id="controller-1",
                controller_repo=Path("/tmp/repo"),
                registry_path=Path("/tmp/controllers.json"),
                codex="codex",
            )
        self.assertTrue(armed)
        ensure.assert_called_once()
        self.assertEqual(ensure.call_args.kwargs["session_id"], "controller-1")


class AssignmentBoundTerminalReceiptIdentityTests(unittest.TestCase):
    def test_assignment_bound_terminal_receipt_must_match_current_attempt_and_lease(self) -> None:
        from scripts.assignment_runtime import apply_runtime_receipt
        module = TerminalContinuationTests()._load_module("terminal_continuation_assignment_binding_test")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); repo = root / "repo"; repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({"controller-1": str(repo.resolve())}), encoding="utf-8")
            start = {
                "event_type": "assignment_started", "assignment_id": "A-1", "task_id": "T-1",
                "agent_id": "agent-1", "provider": "grok-build", "session_id": "session-current",
                "worktree": str(repo), "issued_at": "2026-09-03T12:00:00+00:00", "attempt": 1,
                "lease_id": "lease-current", "event_seq": 1, "assignment_contract_version": 2,
                "side_effect": False, "idempotency_key": None, "primary_goal": "bounded",
                "success_criteria": ["done"], "owned_scope": ["README.md"], "strategy": "test",
            }
            apply_runtime_receipt(repo, start)
            receipt = root / "terminal.json"
            receipt.write_text(json.dumps({
                "schema_version": 1, "event_type": "external_agent_terminal", "repo": str(repo.resolve()),
                "assignment_id": "A-1", "task_id": "T-1", "agent_id": "agent-1",
                "session_id": "session-old", "attempt": 1, "lease_id": "lease-old",
                "summary": "stale terminal", "delivery_outcome": "unresolved",
            }), encoding="utf-8")
            with self.assertRaisesRegex(PermissionError, "current canonical runtime lease"):
                module.consume_terminal_receipt(repo=repo, receipt_path=receipt, registry_path=registry)

    def test_assignment_bound_terminal_receipt_cannot_wake_while_canonical_attempt_is_active(self) -> None:
        from scripts.assignment_runtime import apply_runtime_receipt
        module = TerminalContinuationTests()._load_module("terminal_continuation_active_binding_test")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); repo = root / "repo"; repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            start = {
                "event_type": "assignment_started", "assignment_id": "A-1", "task_id": "T-1",
                "agent_id": "agent-1", "provider": "grok-build", "session_id": "session-current",
                "worktree": str(repo), "issued_at": "2026-09-03T12:00:00+00:00", "attempt": 1,
                "lease_id": "lease-current", "event_seq": 1, "assignment_contract_version": 2,
                "side_effect": False, "idempotency_key": None, "primary_goal": "bounded",
                "success_criteria": ["done"], "owned_scope": ["README.md"], "strategy": "test",
            }
            apply_runtime_receipt(repo, start)
            receipt = {
                "assignment_id": "A-1", "task_id": "T-1", "agent_id": "agent-1",
                "session_id": "session-current", "attempt": 1, "lease_id": "lease-current",
            }
            with self.assertRaisesRegex(PermissionError, "canonical runtime terminal"):
                module._verify_assignment_bound_receipt(repo, receipt)


if __name__ == "__main__":
    unittest.main()
