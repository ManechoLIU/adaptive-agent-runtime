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
    def test_terminal_receipt_persists_before_desktop_runtime_is_needed(self) -> None:
        module = self._load_module("terminal_continuation_persist_before_wake_test")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = root / "controllers.json"
            registry.write_text(
                json.dumps({"controller-1": str(repo.resolve())}), encoding="utf-8"
            )
            receipt = root / "terminal.json"
            receipt.write_text(
                json.dumps({
                    "schema_version": 1,
                    "event_type": "external_agent_terminal",
                    "engine": "codex-native",
                    "model": "gpt",
                    "repo": str(repo.resolve()),
                    "exit_code": 0,
                    "summary": "writer completed",
                    "agent_id": "writer-1",
                }),
                encoding="utf-8",
            )
            state_root = root / "state"
            snapshot = {
                "root": str(repo.resolve()),
                "ready_ids": [],
                "runnable_ids": [],
                "candidate_revisions": [],
                "assignment_liveness": {},
                "rule_handshake": {},
            }
            with patch.object(module.lifecycle, "STATE_ROOT", state_root), patch.object(
                module.lifecycle, "project_snapshot", return_value=snapshot
            ), patch.object(
                module.web_bridge,
                "resolve_desktop_codex_executable",
                side_effect=ValueError("desktop runtime unavailable"),
            ):
                result = module.consume_terminal_receipt(
                    repo=repo,
                    receipt_path=receipt,
                    registry_path=registry,
                    dispatch_wake=False,
                )

            state = json.loads(
                (state_root / "controller-1.json").read_text(encoding="utf-8")
            )
            self.assertTrue(state["pending_control_event"])
            self.assertEqual(result["wake_result"], None)

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
            ), patch.object(
                module.web_bridge,
                "resolve_desktop_codex_executable",
                side_effect=AssertionError("host-neutral terminal receipt resolved Desktop runtime"),
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
            self.assertIsNone(wake_calls[0]["codex"])
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
            ), patch.object(
                module.web_bridge,
                "resolve_desktop_codex_executable",
                side_effect=AssertionError("host-neutral runtime event resolved Desktop runtime"),
            ):
                result = module.notify_runtime_change(
                    repo=repo, registry_path=registry, wake_dispatcher=fake_wake
                )
            state = json.loads((state_root / "controller-1.json").read_text(encoding="utf-8"))
            self.assertTrue(state["pending_control_event"])
            self.assertIn("active_without_progress:T-1", state["triggers"])
            self.assertIsNone(wake_calls[0]["codex"])
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

class PendingTerminalReconcileTests(unittest.TestCase):
    def _module(self):
        return TerminalContinuationTests()._load_module("terminal_continuation_pending_reconcile_test")

    def test_reconcile_pending_discovers_canonical_receipts_without_receipt_cli_argument(self) -> None:
        module = self._module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); repo = root / "repo"; repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = root / "controllers.json"
            receipt = root / "terminal.json"
            receipt.write_text(json.dumps({
                "schema_version": 1, "event_type": "external_agent_terminal", "repo": str(repo.resolve()),
                "agent_id": "reviewer-1", "summary": "done", "delivery_outcome": "pass",
            }), encoding="utf-8")
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_targets__": {"controller-1": {"web": {
                    "status": "active", "session_id": "web-current", "generation": 3,
                    "provenance": "manual_user_authorized", "binding_mode": "temporary", "host_attested": False,
                }}},
                "__controller_sessions__": {"controller-1": {"web": ["web-current"]}},
                "__controller_execution_ownership__": {"controller-1": {
                    "active_host": "web", "execution_target_session_id": "web-current", "generation": 3,
                    "provenance": "manual_user_authorized",
                }},
            }), encoding="utf-8")
            lifecycle_state = {
                "pending_control_event": True,
                "pending_terminal_receipts": [str(receipt.resolve())],
                "controller_host": "web",
            }
            with patch.object(module.lifecycle, "load_json", return_value=lifecycle_state), patch.object(
                module.lifecycle, "state_path", return_value=root / "controller.json"
            ):
                result = module.reconcile_pending_terminal_receipts(repo=repo, registry_path=registry)
            self.assertEqual(result["controller_id"], "controller-1")
            self.assertEqual(result["pending_count"], 1)
            self.assertEqual(result["ownership_generation"], 3)
            self.assertEqual(result["execution_target_session_id"], "web-current")
            self.assertEqual(result["receipts"][0]["path"], str(receipt.resolve()))
            self.assertEqual(result["receipts"][0]["agent_id"], "reviewer-1")
            self.assertTrue(result["receipts"][0]["sha256"])

    def test_reconcile_pending_accepts_independent_desktop_target_and_ownership_generations(self) -> None:
        module = self._module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); repo = root / "repo"; repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = root / "controllers.json"
            receipt = root / "terminal.json"
            receipt.write_text(json.dumps({
                "schema_version": 1, "event_type": "external_agent_terminal", "repo": str(repo.resolve()),
                "agent_id": "reviewer-1", "summary": "done", "delivery_outcome": "pass",
            }), encoding="utf-8")
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_targets__": {"controller-1": {"desktop_codex": {
                    "status": "active", "session_id": "desktop-current", "generation": 5,
                }}},
                "__controller_sessions__": {"controller-1": {"desktop_codex": ["desktop-current"]}},
                "__controller_execution_ownership__": {"controller-1": {
                    "active_host": "desktop_codex",
                    "execution_target_session_id": "desktop-current",
                    "generation": 7,
                    "provenance": "desktop_entry",
                }},
            }), encoding="utf-8")
            lifecycle_state = {
                "pending_control_event": True,
                "pending_terminal_receipts": [str(receipt.resolve())],
                "controller_host": "desktop_codex",
            }
            with patch.object(module.lifecycle, "load_json", return_value=lifecycle_state), patch.object(
                module.lifecycle, "state_path", return_value=root / "controller.json"
            ):
                result = module.reconcile_pending_terminal_receipts(repo=repo, registry_path=registry)
            self.assertEqual(result["controller_id"], "controller-1")
            self.assertEqual(result["execution_host"], "desktop_codex")
            self.assertEqual(result["execution_target_session_id"], "desktop-current")
            self.assertEqual(result["target_generation"], 5)
            self.assertEqual(result["ownership_generation"], 7)

    def test_reconcile_pending_is_idempotent_and_does_not_mutate_lifecycle_or_dispatch_wake(self) -> None:
        module = self._module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); repo = root / "repo"; repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = root / "controllers.json"
            receipt = root / "terminal.json"
            receipt.write_text(json.dumps({
                "schema_version": 1, "event_type": "external_agent_terminal", "repo": str(repo.resolve()),
                "agent_id": "agent-1", "summary": "done",
            }), encoding="utf-8")
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_targets__": {"controller-1": {"desktop_codex": {
                    "status": "active", "session_id": "desktop-current", "generation": 2
                }}},
                "__controller_sessions__": {"controller-1": {"desktop_codex": ["desktop-current"]}},
                "__controller_execution_ownership__": {"controller-1": {
                    "active_host": "desktop_codex", "execution_target_session_id": "desktop-current", "generation": 2,
                    "provenance": "desktop_entry",
                }},
            }), encoding="utf-8")
            lifecycle_state = {"pending_terminal_receipts": [str(receipt.resolve()), str(receipt.resolve())]}
            with patch.object(module.lifecycle, "load_json", return_value=lifecycle_state), patch.object(
                module.lifecycle, "state_path", return_value=root / "controller.json"
            ), patch.object(module.lifecycle, "persist_event_state") as persist, patch.object(
                module.web_bridge, "dispatch_pending_lifecycle_wake") as wake:
                first = module.reconcile_pending_terminal_receipts(repo=repo, registry_path=registry)
                second = module.reconcile_pending_terminal_receipts(repo=repo, registry_path=registry)
            self.assertEqual(first["reconcile_fingerprint"], second["reconcile_fingerprint"])
            self.assertEqual(first["pending_count"], 1)
            persist.assert_not_called()
            wake.assert_not_called()

    def test_reconcile_pending_fails_closed_when_canonical_ownership_is_missing_or_mismatched(self) -> None:
        module = self._module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); repo = root / "repo"; repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            receipt = root / "terminal.json"
            receipt.write_text(json.dumps({"event_type": "external_agent_terminal", "repo": str(repo.resolve())}), encoding="utf-8")
            registry = root / "controllers.json"
            registry.write_text(json.dumps({"controller-1": str(repo.resolve())}), encoding="utf-8")
            with patch.object(module.lifecycle, "load_json", return_value={"pending_terminal_receipts": [str(receipt)]}), patch.object(
                module.lifecycle, "state_path", return_value=root / "controller.json"
            ):
                with self.assertRaisesRegex(PermissionError, "execution ownership"):
                    module.reconcile_pending_terminal_receipts(repo=repo, registry_path=registry)

    def test_reconcile_pending_cli_has_no_receipt_argument_and_never_self_spawns(self) -> None:
        module = self._module()
        with patch.object(module, "reconcile_pending_terminal_receipts", return_value={"pending_count": 0}) as reconcile, patch.object(
            module.subprocess, "Popen"
        ) as popen:
            self.assertEqual(module.main(["reconcile-pending", "--repo", "/tmp/repo"]), 0)
        reconcile.assert_called_once()
        popen.assert_not_called()

def _legacy_assignment_fixture(module, root: Path, repo: Path, registry: Path):
    receipt = root / "legacy-terminal.json"
    receipt.write_text(json.dumps({
        "event_type": "external_agent_terminal", "repo": str(repo.resolve()),
        "assignment_id": "A-legacy", "task_id": "T-legacy", "agent_id": "agent-legacy",
        "session_id": "session-legacy", "summary": "legacy terminal",
    }), encoding="utf-8")
    return receipt


def _ownership_registry(repo: Path, host: str = "web", target: str = "web-current", generation: int = 1):
    return {
        "controller-1": str(repo.resolve()),
        "__controller_targets__": {"controller-1": {host: {
            "status": "active", "session_id": target, "generation": generation,
            **({"provenance": "manual_user_authorized", "binding_mode": "temporary", "host_attested": False} if host == "web" else {}),
        }}},
        "__controller_sessions__": {"controller-1": {host: [target]}},
        "__controller_execution_ownership__": {"controller-1": {
            "active_host": host, "execution_target_session_id": target, "generation": generation,
            "provenance": "test",
        }},
    }


def _pending_reconcile_partial_classification_test(self):
    module = self._module()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp); repo = root / "repo"; repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
        registry = root / "controllers.json"; registry.write_text(json.dumps(_ownership_registry(repo)), encoding="utf-8")
        legacy = _legacy_assignment_fixture(module, root, repo, registry)
        unbound = root / "unbound.json"
        unbound.write_text(json.dumps({
            "event_type": "external_agent_terminal", "repo": str(repo.resolve()),
            "summary": "old external result", "result_path": str(root / "result.txt")
        }), encoding="utf-8")
        lifecycle_state = {"pending_terminal_receipts": [str(legacy), str(unbound)]}
        with patch.object(module.lifecycle, "load_json", return_value=lifecycle_state), patch.object(
            module.lifecycle, "state_path", return_value=root / "controller.json"
        ), patch.object(module, "load_runtime_state", return_value={"leases": {"A-legacy": {
            "assignment_id": "A-legacy", "task_id": "T-legacy", "agent_id": "agent-legacy",
            "session_id": "session-legacy", "attempt": 1, "lease_id": "lease-1", "terminal_state": "failed"
        }}}):
            result = module.reconcile_pending_terminal_receipts(repo=repo, registry_path=registry)
        self.assertEqual(result["pending_count"], 2)
        by_path = {item["path"]: item for item in result["receipts"]}
        self.assertEqual(by_path[str(legacy.resolve())]["verification_state"], "legacy_unverifiable")
        self.assertEqual(by_path[str(legacy.resolve())]["action_suggestion"], "blocked")
        self.assertIn("canonical runtime lease identity", by_path[str(legacy.resolve())]["verification_error"])
        self.assertEqual(by_path[str(unbound.resolve())]["verification_state"], "verified_legacy_unbound")
        self.assertEqual(by_path[str(unbound.resolve())]["action_suggestion"], "executed")

PendingTerminalReconcileTests.test_reconcile_pending_classifies_legacy_assignment_without_weakening_current_lease_checks = _pending_reconcile_partial_classification_test

def _pending_reconcile_rejects_lifecycle_change_before_publish(self):
    import fcntl
    module = self._module()
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp); repo=root/'repo'; repo.mkdir(); subprocess.run(['git','init','-q','-b','main',str(repo)],check=True)
        registry=root/'controllers.json'; registry.write_text(json.dumps(_ownership_registry(repo)),encoding='utf-8')
        receipt=root/'terminal.json'; receipt.write_text(json.dumps({'event_type':'external_agent_terminal','repo':str(repo.resolve()),'summary':'done'}),encoding='utf-8')
        state_path=root/'controller.json'; state_path.write_text(json.dumps({'pending_terminal_receipts':[str(receipt)]}),encoding='utf-8')
        lifecycle_lock=state_path.with_suffix(state_path.suffix+'.lock')
        observed=[]
        original_write=module._write_json_atomic
        def inspect_write(path,value):
            with lifecycle_lock.open('a+') as handle:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:
                    observed.append('blocked')
                else:
                    observed.append('unexpectedly_unlocked')
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return original_write(path,value)
        with patch.object(module.lifecycle,'state_path',return_value=state_path), patch.object(module,'_write_json_atomic',side_effect=inspect_write):
            module.reconcile_pending_terminal_receipts(repo=repo,registry_path=registry)
        self.assertEqual(observed,['blocked'])


def _pending_reconcile_hashes_same_bytes_it_parses(self):
    module=self._module()
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp); repo=root/'repo'; repo.mkdir(); subprocess.run(['git','init','-q','-b','main',str(repo)],check=True)
        registry=root/'controllers.json'; registry.write_text(json.dumps(_ownership_registry(repo)),encoding='utf-8')
        receipt=root/'terminal.json'
        payload=json.dumps({'event_type':'external_agent_terminal','repo':str(repo.resolve()),'summary':'stable'}).encode()
        receipt.write_bytes(payload)
        state={'pending_terminal_receipts':[str(receipt)]}
        original_read_bytes=Path.read_bytes
        original_read_text=Path.read_text
        reads={'bytes':0,'text':0}
        def read_bytes(path):
            if path.resolve()==receipt.resolve():
                reads['bytes']+=1
                if reads['bytes']>1:
                    return json.dumps({'event_type':'external_agent_terminal','repo':str(repo.resolve()),'summary':'replaced'}).encode()
            return original_read_bytes(path)
        def read_text(path,*args,**kwargs):
            if path.resolve()==receipt.resolve():
                reads['text']+=1
                raise AssertionError('reconcile must not separately read terminal receipt text')
            return original_read_text(path,*args,**kwargs)
        with patch.object(module.lifecycle,'state_path',return_value=root/'controller.json'), patch.object(module.lifecycle,'load_json',return_value=state), patch.object(Path,'read_bytes',read_bytes), patch.object(Path,'read_text',read_text):
            result=module.reconcile_pending_terminal_receipts(repo=repo,registry_path=registry)
        self.assertEqual(reads['bytes'],1)
        self.assertEqual(reads['text'],0)
        self.assertEqual(result['receipts'][0]['summary'],'stable')
        import hashlib
        self.assertEqual(result['receipts'][0]['sha256'],hashlib.sha256(payload).hexdigest())

PendingTerminalReconcileTests.test_reconcile_pending_rejects_lifecycle_change_before_publish = _pending_reconcile_rejects_lifecycle_change_before_publish
PendingTerminalReconcileTests.test_reconcile_pending_hashes_same_bytes_it_parses = _pending_reconcile_hashes_same_bytes_it_parses

def _pending_reconcile_holds_fences_through_audit(self):
    import fcntl
    module=self._module()
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp); repo=root/'repo'; repo.mkdir(); subprocess.run(['git','init','-q','-b','main',str(repo)],check=True)
        registry=root/'controllers.json'; registry.write_text(json.dumps(_ownership_registry(repo)),encoding='utf-8')
        receipt=root/'terminal.json'; receipt.write_text(json.dumps({'event_type':'external_agent_terminal','repo':str(repo.resolve()),'summary':'done'}),encoding='utf-8')
        state_path=root/'controller.json'; state_path.write_text(json.dumps({'pending_terminal_receipts':[str(receipt)]}),encoding='utf-8')
        lifecycle_lock=state_path.with_suffix(state_path.suffix+'.lock')
        registry_lock=module.web_bridge.target_guard.registry_lock_path(registry)
        observed={}
        original_write=module._write_json_atomic
        def inspect_write(path,value):
            for name,lock_path in [('lifecycle',lifecycle_lock),('registry',registry_lock)]:
                lock_path.parent.mkdir(parents=True,exist_ok=True)
                with lock_path.open('a+') as h:
                    try:
                        fcntl.flock(h.fileno(), fcntl.LOCK_EX|fcntl.LOCK_NB)
                    except BlockingIOError:
                        observed[name]=True
                    else:
                        observed[name]=False
                        fcntl.flock(h.fileno(), fcntl.LOCK_UN)
            return original_write(path,value)
        with patch.object(module.lifecycle,'state_path',return_value=state_path), patch.object(module,'_write_json_atomic',side_effect=inspect_write):
            module.reconcile_pending_terminal_receipts(repo=repo,registry_path=registry)
        self.assertEqual(observed,{'lifecycle':True,'registry':True})


def _pending_reconcile_fingerprint_is_order_independent(self):
    module=self._module()
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp); repo=root/'repo'; repo.mkdir(); subprocess.run(['git','init','-q','-b','main',str(repo)],check=True)
        registry=root/'controllers.json'; registry.write_text(json.dumps(_ownership_registry(repo)),encoding='utf-8')
        a=root/'a.json'; b=root/'b.json'
        for p,name in [(a,'a'),(b,'b')]: p.write_text(json.dumps({'event_type':'external_agent_terminal','repo':str(repo.resolve()),'summary':name}),encoding='utf-8')
        state_path=root/'controller.json'
        state_path.write_text(json.dumps({'pending_terminal_receipts':[str(a),str(b)]}),encoding='utf-8')
        with patch.object(module.lifecycle,'state_path',return_value=state_path):
            first=module.reconcile_pending_terminal_receipts(repo=repo,registry_path=registry)
        state_path.write_text(json.dumps({'pending_terminal_receipts':[str(b),str(a)]}),encoding='utf-8')
        with patch.object(module.lifecycle,'state_path',return_value=state_path):
            second=module.reconcile_pending_terminal_receipts(repo=repo,registry_path=registry)
        self.assertEqual(first['reconcile_fingerprint'],second['reconcile_fingerprint'])


def _atomic_audit_writer_handles_concurrent_publication(self):
    import threading
    module=self._module()
    with tempfile.TemporaryDirectory() as tmp:
        path=Path(tmp)/'audit.json'; errors=[]
        def worker(i):
            try: module._write_json_atomic(path,{'i':i})
            except Exception as e: errors.append(e)
        threads=[threading.Thread(target=worker,args=(i,)) for i in range(20)]
        [t.start() for t in threads]; [t.join() for t in threads]
        self.assertEqual(errors,[])
        self.assertIn(json.loads(path.read_text())['i'],range(20))
        self.assertEqual(list(path.parent.glob(path.name+'.tmp*')),[])

PendingTerminalReconcileTests.test_reconcile_pending_holds_lifecycle_and_registry_fences_through_audit = _pending_reconcile_holds_fences_through_audit
PendingTerminalReconcileTests.test_reconcile_pending_fingerprint_is_order_independent = _pending_reconcile_fingerprint_is_order_independent
PendingTerminalReconcileTests.test_atomic_audit_writer_handles_concurrent_publication = _atomic_audit_writer_handles_concurrent_publication

def _pending_reconcile_holds_runtime_assignment_fence_through_audit(self):
    import fcntl
    module = self._module()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        repo = root / 'repo'
        repo.mkdir()
        subprocess.run(['git', 'init', '-q', '-b', 'main', str(repo)], check=True)
        registry = root / 'controllers.json'
        registry.write_text(json.dumps(_ownership_registry(repo)), encoding='utf-8')
        receipt = root / 'terminal.json'
        receipt.write_text(json.dumps({
            'event_type': 'external_agent_terminal',
            'repo': str(repo.resolve()),
            'assignment_id': 'A-1',
            'task_id': 'T-1',
            'agent_id': 'agent-1',
            'session_id': 'session-1',
            'attempt': 1,
            'lease_id': 'lease-1',
            'summary': 'done',
            'delivery_outcome': 'pass',
        }), encoding='utf-8')
        state_dir = repo / '.git' / 'adaptive-delivery'
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / 'runtime-assignments.json').write_text(json.dumps({
            'schema_version': 2,
            'leases': {
                'A-1': {
                    'assignment_id': 'A-1',
                    'task_id': 'T-1',
                    'agent_id': 'agent-1',
                    'session_id': 'session-1',
                    'attempt': 1,
                    'lease_id': 'lease-1',
                    'terminal_state': 'completed',
                    'delivery_outcome': 'pass',
                }
            },
            'lineages': {},
        }), encoding='utf-8')
        runtime_lock = state_dir / 'runtime-assignments.lock'
        state_path = root / 'controller.json'
        state_path.write_text(json.dumps({'pending_terminal_receipts': [str(receipt)]}), encoding='utf-8')
        observed = []
        original_write = module._write_json_atomic

        def inspect_write(path, value):
            runtime_lock.parent.mkdir(parents=True, exist_ok=True)
            with runtime_lock.open('a+') as handle:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    observed.append('blocked')
                else:
                    observed.append('unexpectedly_unlocked')
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return original_write(path, value)

        with patch.object(module.lifecycle, 'state_path', return_value=state_path), patch.object(
            module, '_write_json_atomic', side_effect=inspect_write
        ):
            result = module.reconcile_pending_terminal_receipts(repo=repo, registry_path=registry)
        self.assertEqual(result['receipts'][0]['verification_state'], 'verified_current')
        self.assertEqual(observed, ['blocked'])

PendingTerminalReconcileTests.test_reconcile_pending_holds_runtime_assignment_fence_through_audit = _pending_reconcile_holds_runtime_assignment_fence_through_audit

class ManualControlCycleReconcileTests(unittest.TestCase):
    def _module(self):
        return TerminalContinuationTests()._load_module("terminal_continuation_manual_control_cycle_test")

    def _fixture(self, root: Path):
        import hashlib, sys
        repo = root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
        (repo / "TASK_LEDGER.md").write_text("# ledger\n", encoding="utf-8")
        controller_id = "controller-1"
        target = "web-current"
        registry = root / "controllers.json"
        registry.write_text(json.dumps({
            controller_id: str(repo.resolve()),
            "__controller_sessions__": {controller_id: {"web": [target]}},
            "__controller_targets__": {controller_id: {"web": {
                "status": "active", "session_id": target, "generation": 3,
                "provenance": "manual_user_authorized", "binding_mode": "temporary",
                "host_attested": False,
            }}},
            "__controller_execution_ownership__": {controller_id: {
                "active_host": "web", "execution_target_session_id": target,
                "generation": 3, "provenance": "manual_user_authorized",
            }},
        }), encoding="utf-8")
        lease_file = root / "manual-leases.json"
        lease_file.write_text(json.dumps({"schema_version": 1, "leases": {controller_id: {
            "repo": str(repo.resolve()), "controller_id": controller_id,
            "web_session_id": target, "authorized_at_unix": 100,
            "expires_at_unix": 10000, "provenance": "manual_user_authorized",
            "mode": "resume_only",
        }}}), encoding="utf-8")
        terminal = root / "terminal.json"
        terminal.write_text(json.dumps({
            "event_type": "external_agent_terminal", "repo": str(repo.resolve()),
            "summary": "done",
        }), encoding="utf-8")
        state_path = root / "controller.json"
        state_path.write_text(json.dumps({
            "pending_control_event": True,
            "pending_terminal_receipts": [str(terminal.resolve())],
            "triggers": ["terminal_receipt_pending"], "wake_generation": 1,
            "controller_host": "web", "requires_user": False,
        }), encoding="utf-8")
        reconcile_path = repo / ".git" / "adaptive-delivery" / "terminal-reconcile.json"
        reconcile_path.parent.mkdir(parents=True, exist_ok=True)
        reconcile_path.write_text(json.dumps({
            "schema_version": 1, "operation": "reconcile_pending_terminal_receipts",
            "controller_id": controller_id, "repo": str(repo.resolve()),
            "pending_count": 1,
            "receipts": [{
                "path": str(terminal.resolve()),
                "sha256": hashlib.sha256(terminal.read_bytes()).hexdigest(),
                "verification_state": "verified_legacy_unbound",
                "action_suggestion": "executed",
            }],
            "execution_host": "web", "execution_target_session_id": target,
            "target_generation": 3, "ownership_generation": 3,
            "reconcile_fingerprint": "reconcile-fp", "clears_lifecycle_debt": False,
            "close_condition": "successful control-cycle receipt",
        }), encoding="utf-8")
        action_id = "terminal_receipt:" + hashlib.sha256(str(terminal.resolve()).encode("utf-8")).hexdigest()[:16]
        control_json = root / "control-cycle.json"
        evidence = "artifact:" + str(reconcile_path.resolve())
        cycle_id = "terminal-debt-cycle-1"
        control_snapshot = {
            "event_contract": {
                "event_id": cycle_id, "event_type": "terminal_debt_reconciliation",
                "primary_task": "T-1", "candidate_revision": "main-1",
                "terminal_receipt": "terminal debt reconciled",
            },
            "ledger_sha256": hashlib.sha256((repo / "TASK_LEDGER.md").read_bytes()).hexdigest(),
            "capacity_projection": {"evidence": evidence},
            "controller_actions": [{"id": action_id, "decision": "executed", "evidence": evidence}],
            "control_loop_receipt": {
                "controller_action_ids": [action_id],
                "continuation_debt_ids": [action_id],
                "open_continuation_debt_ids": [],
            },
        }
        control_json.write_text(json.dumps(control_snapshot), encoding="utf-8")
        cycle_dir = repo / ".git" / "adaptive-delivery" / "controller-cycle-evidence"
        cycle_dir.mkdir(parents=True, exist_ok=True)
        canonical = json.dumps(control_snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        cycle_path = cycle_dir / (hashlib.sha256(cycle_id.encode("utf-8")).hexdigest() + ".json")
        cycle_path.write_text(json.dumps({
            "schema_version": 1, "record_kind": "controller_cycle_evidence",
            "evidence_id": cycle_id, "controller_id": controller_id, "cycle_id": cycle_id,
            "event_type": "terminal_debt_reconciliation", "primary_task": "T-1",
            "terminal_status": "CLOSED", "evidence_summary": "terminal debt reconciled",
            "main_revision": "main-1", "ledger_sha256": control_snapshot["ledger_sha256"],
            "snapshot_sha256": hashlib.sha256(canonical).hexdigest(),
            "validation_errors": [],
        }), encoding="utf-8")
        command = (
            f"{sys.executable} {ROOT / 'scripts' / 'control_event_guard.py'} {control_json} "
            f"--ledger {repo / 'TASK_LEDGER.md'} --repo {repo} --controller-session {controller_id}"
        )
        audit_log = root / "activity.redacted.jsonl"
        audit_log.write_text(json.dumps({
            "receiptId": "receipt-control-1", "childTool": "shell_command", "state": "succeeded",
            "rootLabel": str(repo.resolve()), "targetLabel": command,
            "occurredAtUnixMs": 200000,
            "detail": f"命令：{command} · 工作目录：{repo}\n\n命令输出：\ncontrol-event: allowed; cycle complete",
        }) + "\n", encoding="utf-8")
        return repo, registry, lease_file, state_path, reconcile_path, audit_log, control_json

    def test_manual_fenced_control_cycle_reconcile_closes_only_reconciled_terminal_debt_without_verifying_web_identity(self):
        module = self._module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, registry, lease_file, state_path, _reconcile_path, audit_log, _control_json = self._fixture(root)
            snapshot = {
                "root": str(repo.resolve()), "head": None, "ledger_sha256": "x",
                "worktree_status_sha256": "y", "ready_ids": [], "runnable_ids": [],
                "candidate_revisions": [], "assignment_liveness": {}, "controller_corrections": [],
                "control_loop_required": True,
                "rule_handshake": {"state": "current", "blocking": False},
            }
            with patch.object(module.lifecycle, "state_path", return_value=state_path), patch.object(
                module.lifecycle, "project_snapshot", return_value=snapshot
):
                result = module.reconcile_control_cycle_closure(
                    repo=repo, registry_path=registry, audit_log=audit_log,
                    manual_lease_path=lease_file, now_unix=500,
                )
            state = json.loads(state_path.read_text(encoding="utf-8"))
            registry_after = json.loads(registry.read_text(encoding="utf-8"))
        self.assertEqual(state.get("pending_terminal_receipts"), [])
        self.assertEqual(result["closed_terminal_receipt_count"], 1)
        self.assertEqual(result["binding_verification"], "manual_fenced_not_host_attested")
        self.assertFalse(registry_after["__controller_targets__"]["controller-1"]["web"]["host_attested"])

    def test_manual_fenced_control_cycle_reconcile_requires_unexpired_matching_manual_lease(self):
        module = self._module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, registry, lease_file, state_path, _reconcile_path, audit_log, _control_json = self._fixture(root)
            lease_file.write_text(json.dumps({"schema_version": 1, "leases": {}}), encoding="utf-8")
            with patch.object(module.lifecycle, "state_path", return_value=state_path):
                with self.assertRaisesRegex(PermissionError, "manual Web resume lease"):
                    module.reconcile_control_cycle_closure(
                        repo=repo, registry_path=registry, audit_log=audit_log,
                        manual_lease_path=lease_file, now_unix=500,
                    )

    def test_reconcile_control_cycle_cli_accepts_no_receipt_or_web_session_identity_argument(self):
        module = self._module()
        parser = module.build_parser()
        args = parser.parse_args(["reconcile-control-cycle", "--repo", "/tmp/repo"])
        self.assertEqual(args.command, "reconcile-control-cycle")
        self.assertFalse(hasattr(args, "receipt"))
        self.assertFalse(hasattr(args, "web_session_id"))
        self.assertFalse(hasattr(args, "registry"))
        self.assertFalse(hasattr(args, "audit_log"))
        self.assertFalse(hasattr(args, "manual_lease_file"))

def _manual_reconcile_skips_later_unrelated_allowed_receipt(self):
    module = self._module()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        repo, registry, lease_file, state_path, reconcile_path, audit_log, _control_json = self._fixture(root)
        import sys
        unrelated = root / "unrelated.json"
        unrelated.write_text(json.dumps({"capacity_projection": {"evidence": "artifact:/tmp/other"}}), encoding="utf-8")
        command = (
            f"{sys.executable} {ROOT / 'scripts' / 'control_event_guard.py'} {unrelated} "
            f"--ledger {repo / 'TASK_LEDGER.md'} --repo {repo} --controller-session controller-1"
        )
        with audit_log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "receiptId": "receipt-unrelated-later", "childTool": "shell_command", "state": "succeeded",
                "rootLabel": str(repo.resolve()), "targetLabel": command, "occurredAtUnixMs": 300000,
                "detail": f"命令：{command} · 工作目录：{repo}\n\n命令输出：\ncontrol-event: allowed; other cycle",
            }) + "\n")
        snapshot = {
            "root": str(repo.resolve()), "ready_ids": [], "runnable_ids": [], "candidate_revisions": [],
            "assignment_liveness": {}, "controller_corrections": [], "control_loop_required": True,
            "rule_handshake": {"state": "current", "blocking": False},
        }
        with patch.object(module.lifecycle, "state_path", return_value=state_path), patch.object(
            module.lifecycle, "project_snapshot", return_value=snapshot
):
            result = module.reconcile_control_cycle_closure(
                repo=repo, registry_path=registry, audit_log=audit_log,
                manual_lease_path=lease_file, now_unix=500,
            )
        self.assertEqual(result["control_receipt_id"], "receipt-control-1")
        self.assertEqual(json.loads(state_path.read_text())["pending_terminal_receipts"], [])


def _manual_reconcile_requires_target_lineage_membership(self):
    module = self._module()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        repo, registry, lease_file, state_path, _reconcile_path, audit_log, _control_json = self._fixture(root)
        value = json.loads(registry.read_text())
        value["__controller_sessions__"]["controller-1"]["web"] = ["web-old"]
        registry.write_text(json.dumps(value), encoding="utf-8")
        with patch.object(module.lifecycle, "state_path", return_value=state_path), patch.object(
            module.lifecycle, "project_snapshot", side_effect=AssertionError("lineage must fail before project snapshot")
        ):
            with self.assertRaisesRegex(PermissionError, "lineage"):
                module.reconcile_control_cycle_closure(
                    repo=repo, registry_path=registry, audit_log=audit_log,
                    manual_lease_path=lease_file, now_unix=500,
                )

ManualControlCycleReconcileTests.test_manual_reconcile_skips_later_unrelated_allowed_receipt = _manual_reconcile_skips_later_unrelated_allowed_receipt
ManualControlCycleReconcileTests.test_manual_reconcile_requires_target_lineage_membership = _manual_reconcile_requires_target_lineage_membership

def _manual_reconcile_rejects_forged_immutable_cycle_evidence(self):
    module = self._module()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        repo, registry, lease_file, state_path, _reconcile_path, audit_log, control_json = self._fixture(root)
        control = json.loads(control_json.read_text())
        import hashlib
        cycle_id = control["event_contract"]["event_id"]
        cycle_path = repo / ".git" / "adaptive-delivery" / "controller-cycle-evidence" / (hashlib.sha256(cycle_id.encode()).hexdigest() + ".json")
        evidence = json.loads(cycle_path.read_text())
        evidence["snapshot_sha256"] = "0" * 64
        cycle_path.write_text(json.dumps(evidence), encoding="utf-8")
        snapshot = {
            "root": str(repo.resolve()), "ready_ids": [], "runnable_ids": [], "candidate_revisions": [],
            "assignment_liveness": {}, "controller_corrections": [], "control_loop_required": True,
            "rule_handshake": {"state": "current", "blocking": False},
        }
        with patch.object(module.lifecycle, "state_path", return_value=state_path), patch.object(
            module.lifecycle, "project_snapshot", return_value=snapshot
        ):
            with self.assertRaisesRegex(PermissionError, "snapshot_sha256"):
                module.reconcile_control_cycle_closure(
                    repo=repo, registry_path=registry, audit_log=audit_log,
                    manual_lease_path=lease_file, now_unix=500,
                )


def _manual_reconcile_rejects_receipt_from_before_current_target_rotation(self):
    module = self._module()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        repo, registry, lease_file, state_path, _reconcile_path, audit_log, _control_json = self._fixture(root)
        payload = json.loads(lease_file.read_text())
        payload["leases"]["controller-1"]["rotated_at_unix"] = 250
        lease_file.write_text(json.dumps(payload), encoding="utf-8")
        snapshot = {
            "root": str(repo.resolve()), "ready_ids": [], "runnable_ids": [], "candidate_revisions": [],
            "assignment_liveness": {}, "controller_corrections": [], "control_loop_required": True,
            "rule_handshake": {"state": "current", "blocking": False},
        }
        with patch.object(module.lifecycle, "state_path", return_value=state_path), patch.object(
            module.lifecycle, "project_snapshot", return_value=snapshot
        ):
            with self.assertRaisesRegex(PermissionError, "no durable AI-Bridge"):
                module.reconcile_control_cycle_closure(
                    repo=repo, registry_path=registry, audit_log=audit_log,
                    manual_lease_path=lease_file, now_unix=500,
                )

ManualControlCycleReconcileTests.test_manual_reconcile_rejects_forged_immutable_cycle_evidence = _manual_reconcile_rejects_forged_immutable_cycle_evidence
ManualControlCycleReconcileTests.test_manual_reconcile_rejects_receipt_from_before_current_target_rotation = _manual_reconcile_rejects_receipt_from_before_current_target_rotation

def _manual_reconcile_is_idempotent_after_durable_lifecycle_closure(self):
    module = self._module()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        repo, registry, lease_file, state_path, _reconcile_path, audit_log, _control_json = self._fixture(root)
        snapshot = {
            "root": str(repo.resolve()), "ready_ids": [], "runnable_ids": [], "candidate_revisions": [],
            "assignment_liveness": {}, "controller_corrections": [], "control_loop_required": True,
            "rule_handshake": {"state": "current", "blocking": False},
        }
        with patch.object(module.lifecycle, "state_path", return_value=state_path), patch.object(
            module.lifecycle, "project_snapshot", return_value=snapshot
        ):
            first = module.reconcile_control_cycle_closure(
                repo=repo, registry_path=registry, audit_log=audit_log,
                manual_lease_path=lease_file, now_unix=500,
            )
            with patch.object(module, "_latest_allowed_control_receipt", side_effect=AssertionError("idempotent replay must not reconsume audit")):
                second = module.reconcile_control_cycle_closure(
                    repo=repo, registry_path=registry, audit_log=audit_log,
                    manual_lease_path=lease_file, now_unix=501,
                )
        self.assertFalse(first.get("idempotent", False))
        self.assertTrue(second["idempotent"])
        self.assertEqual(second["control_receipt_id"], first["control_receipt_id"])
        self.assertEqual(json.loads(state_path.read_text())["pending_terminal_receipts"], [])

ManualControlCycleReconcileTests.test_manual_reconcile_is_idempotent_after_durable_lifecycle_closure = _manual_reconcile_is_idempotent_after_durable_lifecycle_closure

def _manual_reconcile_rejects_non_terminal_debt_closed_cycle_even_with_matching_hash(self):
    module = self._module()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        repo, registry, lease_file, state_path, _reconcile_path, audit_log, control_json = self._fixture(root)
        import hashlib
        control = json.loads(control_json.read_text())
        cycle_id = control["event_contract"]["event_id"]
        control["event_contract"]["event_type"] = "generic_control_cycle"
        control_json.write_text(json.dumps(control), encoding="utf-8")
        cycle_path = repo / ".git" / "adaptive-delivery" / "controller-cycle-evidence" / (hashlib.sha256(cycle_id.encode()).hexdigest() + ".json")
        evidence = json.loads(cycle_path.read_text())
        canonical = json.dumps(control, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        evidence["event_type"] = "generic_control_cycle"
        evidence["snapshot_sha256"] = hashlib.sha256(canonical).hexdigest()
        cycle_path.write_text(json.dumps(evidence), encoding="utf-8")
        snapshot = {
            "root": str(repo.resolve()), "ready_ids": [], "runnable_ids": [], "candidate_revisions": [],
            "assignment_liveness": {}, "controller_corrections": [], "control_loop_required": True,
            "rule_handshake": {"state": "current", "blocking": False},
        }
        with patch.object(module.lifecycle, "state_path", return_value=state_path), patch.object(
            module.lifecycle, "project_snapshot", return_value=snapshot
        ):
            with self.assertRaisesRegex(PermissionError, "terminal_debt_reconciliation"):
                module.reconcile_control_cycle_closure(
                    repo=repo, registry_path=registry, audit_log=audit_log,
                    manual_lease_path=lease_file, now_unix=500,
                )

ManualControlCycleReconcileTests.test_manual_reconcile_rejects_non_terminal_debt_closed_cycle_even_with_matching_hash = _manual_reconcile_rejects_non_terminal_debt_closed_cycle_even_with_matching_hash

def _manual_reconcile_closes_only_terminal_debt_and_preserves_current_nonterminal_triggers(self):
    module = self._module()
    previous = {
        "pending_control_event": True,
        "pending_terminal_receipts": ["/tmp/a.json"],
        "triggers": [
            "terminal_receipt_pending",
            "subagent_stopped:old-agent",
            "ledger_changed",
            "READY:READY-1",
            "rule_update_pending:rev-new",
            "LEDGER_INVALID:current-ledger-error",
        ],
        "wake_generation": 4,
        "controller_host": "web",
        "session_id": "controller-1",
        "active_turn_id": "turn-current",
        "snapshot": {
            "head": "old-head", "ledger_sha256": "old-ledger", "worktree_status_sha256": "old-wt",
            "ready_ids": ["READY-1"], "runnable_ids": ["READY-1"], "candidate_revisions": [],
        },
    }
    snapshot = {
        "root": "/tmp/repo", "head": "new-head", "ledger_sha256": "new-ledger",
        "worktree_status_sha256": "new-wt", "ready_ids": ["READY-1"], "runnable_ids": ["READY-1"],
        "candidate_revisions": [], "ledger_errors": ["current-ledger-error"],
        "assignment_liveness": {}, "controller_corrections": [], "control_loop_required": True,
        "rule_handshake": {"state": "pending_ack", "installed_revision": "rev-new", "blocking": True},
    }
    with tempfile.TemporaryDirectory() as tmp:
        state_path = Path(tmp) / "controller.json"
        state_path.write_text(json.dumps(previous), encoding="utf-8")
        closure = {
            "operation": "reconcile_control_cycle_closure", "controller_id": "controller-1",
            "repo": "/tmp/repo", "closed_terminal_receipt_count": 1,
            "host_attested": False, "strong_web_identity_established": False,
        }
        next_state = module._persist_reconciled_control_event_locked(
            state_path=state_path,
            event={
                "hook_event_name": "PostToolUse", "session_id": "controller-1",
                "controller_session_id": "controller-1", "controller_host": "web",
                "cwd": "/tmp/repo", "turn_id": "turn-current",
                "tool_name": "AI-Bridge.shell_command",
                "tool_input": {"command": "python3 control_event_guard.py historical.json --ledger TASK_LEDGER.md --controller-session controller-1"},
                "tool_response": {"exit_code": 0, "output": "control-event: allowed"},
            },
            snapshot=snapshot, previous=previous, closure_evidence=closure,
        )
    self.assertEqual(next_state["pending_terminal_receipts"], [])
    self.assertNotIn("terminal_receipt_pending", next_state["triggers"])
    self.assertFalse(any(item.startswith("subagent_stopped:") for item in next_state["triggers"]))
    self.assertIn("ledger_changed", next_state["triggers"])
    self.assertIn("READY:READY-1", next_state["triggers"])
    self.assertIn("rule_update_pending:rev-new", next_state["triggers"])
    self.assertIn("LEDGER_INVALID:current-ledger-error", next_state["triggers"])
    self.assertTrue(next_state["pending_control_event"])

ManualControlCycleReconcileTests.test_manual_reconcile_closes_only_terminal_debt_and_preserves_current_nonterminal_triggers = _manual_reconcile_closes_only_terminal_debt_and_preserves_current_nonterminal_triggers
