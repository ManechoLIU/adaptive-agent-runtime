from __future__ import annotations

import importlib.util
import fcntl
import hashlib
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "scripts" / "controller_target_guard.py"


def load_guard():
    spec = importlib.util.spec_from_file_location("controller_target_guard_under_test", GUARD)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load controller_target_guard.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ControllerTargetGuardTests(unittest.TestCase):
    def make_repo(self, root: Path) -> Path:
        repo = root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
        return repo

    def test_desktop_target_rotation_carries_current_goal_rebind_contract(self) -> None:
        sys.path.insert(0, str(ROOT / "scripts"))
        import lifecycle_hook

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            (repo / "TASK_LEDGER.md").write_text(
                "# Ledger\n\n- 当前 Goal：`M1-F5-B / OUTLINE GENERATION AND EDITING`。\n",
                encoding="utf-8",
            )
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {
                    "controller-1": {"desktop_codex": ["desktop-current"]}
                },
                "__controller_targets__": {
                    "controller-1": {"desktop_codex": {
                        "status": "active", "session_id": "desktop-current", "generation": 4,
                    }}
                },
            }), encoding="utf-8")

            receipt = lifecycle_hook.replace_desktop_session(
                controller_id="controller-1",
                desktop_session_id="desktop-next",
                repo=repo,
                expected_generation=4,
                registry_path=registry,
            )

            contract = receipt["goal_rebind"]
            self.assertEqual(contract["objective"], "M1-F5-B / OUTLINE GENERATION AND EDITING")
            self.assertEqual(contract["target_generation"], 5)
            self.assertEqual(contract["execution_target_session_id"], "desktop-next")
            saved = json.loads(registry.read_text(encoding="utf-8"))
            self.assertEqual(
                saved["__controller_targets__"]["controller-1"]["desktop_codex"]["goal_rebind"],
                contract,
            )

    def make_web_receipt_registry(self, root: Path, repo: Path) -> Path:
        registry = root / "controllers.json"
        registry.write_text(json.dumps({
            "controller-1": str(repo.resolve()),
            "__controller_sessions__": {"controller-1": {"web": ["0123456789abcdef0123"]}},
            "__controller_targets__": {"controller-1": {"web": {
                "status": "active", "session_id": "0123456789abcdef0123", "generation": 4,
            }}},
            "__controller_execution_ownership__": {"controller-1": {
                "active_host": "web", "execution_target_session_id": "0123456789abcdef0123", "generation": 9,
            }},
        }), encoding="utf-8")
        return registry

    def verified_web_turn(self, guard, *, runtime_invocation_id: str = "runtime-invocation-1") -> dict[str, object]:
        identity = guard.agent_target.logical_agent_identity(
            agent_type="controller", agent_id="controller-1"
        )
        target = guard.agent_target.verified_execution_target(
            logical_agent=identity, host="web", execution_target_session_id="0123456789abcdef0123",
            target_generation=4, ownership_generation=9, provenance="test_host_entry",
        )
        return guard.agent_target.verified_execution_turn(
            verified_target=target, runtime_invocation_id=runtime_invocation_id,
            provenance="test_runtime_web_turn",
        )

    def write_active_web_turn_lease(
        self, root: Path, turn: dict[str, object]
    ) -> Path:
        lifecycle = root / "lifecycle.json"
        lifecycle.write_text(json.dumps({
            "active_turn_id": turn["turn_id"],
            "web_turn_lease": {
                "contract": "runtime_web_turn_lease_v1", "status": "active",
                "generation": 1, "turn_id": turn["turn_id"],
                "runtime_invocation_id": turn["runtime_invocation_id"],
                "execution_target_session_id": "0123456789abcdef0123",
                "target_generation": 4, "ownership_generation": 9,
                "watcher_nonce": "watcher-nonce",
            },
        }), encoding="utf-8")
        return lifecycle

    def host_pre_receipt(
        self, *, bridge_call_id: str = "bridge-1", execution_id: str = "hte-1",
        receipt_id: str = "pre-receipt-1", nonce: str = "pre-nonce-1",
    ) -> dict[str, object]:
        return {
            "schema_version": 1, "provenance": "lab_host_tool_pre_receipt_v1",
            "pre_receipt_id": receipt_id, "capability_id": "hic_1",
            "host_tool_execution_id": execution_id, "bridge_call_id": bridge_call_id,
            "bridge_challenge_nonce": "challenge-nonce", "transport_binding_id": "tb_1",
            "workspace": "/Users/echoman/Documents/ChatGPT/Local-Agent-Bridge",
            "production_release_revision": "1" * 40, "production_endpoint_generation": 1,
            "bridge_principal_id": "bridge-principal", "bridge_execution_session_id": "bridge-session",
            "bridge_capability_id": "bridge-capability", "bridge_workspace_id": "local-agent-bridge-production",
            "tool_name": "run_command", "outer_request_sha256": "c" * 64,
            "forwarded_request_sha256": "d" * 64, "normalized_request_sha256": "a" * 64,
            "conversation_id": "0123456789abcdef0123", "browser_target_id": "target-1",
            "top_frame_id": "frame-1", "loader_id": "loader-1", "secure_origin": "https://chatgpt.com",
            "generation_anchor_sha256": "e" * 64, "extension_binding_sha256": "f" * 64,
            "extension_instance_id_sha256": "0" * 64, "extension_loaded_at_ms": 1,
            "pre_nonce": nonce, "issued_at_unix_ms": 2, "host_epoch_binding_sha256": "1" * 64,
            "host_mac_sha256": "2" * 64,
        }

    def host_terminal_receipt(
        self, *, bridge_call_id: str = "bridge-1", execution_id: str = "hte-1",
        pre_receipt: dict[str, object], receipt_id: str = "terminal-receipt-1",
        response_sha256: str = "b" * 64,
    ) -> dict[str, object]:
        return {
            **{key: value for key, value in pre_receipt.items() if key not in {
                "pre_receipt_id", "pre_nonce", "issued_at_unix_ms", "host_mac_sha256",
            }},
            "schema_version": 1, "provenance": "lab_host_tool_terminal_receipt_v1",
            "terminal_receipt_id": receipt_id,
            "pre_receipt_id": pre_receipt["pre_receipt_id"],
            "pre_receipt_sha256": hashlib.sha256(json.dumps(
                pre_receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")).hexdigest(),
            "host_tool_execution_id": execution_id, "bridge_call_id": bridge_call_id,
            "backend_route": "/run-command", "http_status": 200,
            "response_body_sha256": response_sha256, "terminal_classification": "completed",
            "issued_at_unix_ms": 3, "host_epoch_binding_sha256": "1" * 64,
            "host_mac_sha256": "3" * 64,
        }

    def host_tool_guard_evidence(
        self, *, terminal_receipt_sha256: str, snapshot_sha256: str,
        turn: dict[str, object], execution_id: str = "hte-1",
    ) -> dict[str, object]:
        return {
            "controller_id": "controller-1", "host": "web",
            "execution_target_session_id": "0123456789abcdef0123", "turn_id": turn["turn_id"],
            "target_generation": 4, "ownership_generation": 9,
            "bridge_call_id": "bridge-1", "host_tool_execution_id": execution_id,
            "normalized_request_sha256": "a" * 64,
            "snapshot_sha256": snapshot_sha256,
            "terminal_receipt_sha256": terminal_receipt_sha256,
            "terminal_status": "CLOSED",
        }

    def test_host_tool_preparation_persists_full_tuple_and_redacts_receipt_material(self) -> None:
        """Would fail if prepare did not durably bind the canonical tuple or redacted receipts."""
        guard = load_guard()
        self.assertTrue(hasattr(guard, "prepare_host_tool_execution"), "prepare_host_tool_execution is missing")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            registry = self.make_web_receipt_registry(root, repo)
            snapshot = root / "snapshot.json"
            snapshot.write_text('{"snapshot":"current"}', encoding="utf-8")
            turn = self.verified_web_turn(guard)
            lifecycle = self.write_active_web_turn_lease(root, turn)
            pre = self.host_pre_receipt()

            prepared = guard.prepare_host_tool_execution(
                repo=repo, controller_id="controller-1", verified_turn=turn,
                host_pre_receipt=pre, snapshot_path=snapshot, registry_path=registry,
                lifecycle_path=lifecycle,
            )

            self.assertEqual(prepared["state"], "PREPARED")
            self.assertEqual(prepared["tuple"], {
                "controller_id": "controller-1", "host": "web",
                "execution_target_session_id": "0123456789abcdef0123", "turn_id": turn["turn_id"],
                "target_generation": 4, "ownership_generation": 9,
                "bridge_call_id": "bridge-1", "host_tool_execution_id": "hte-1",
                "normalized_request_sha256": "a" * 64,
                "snapshot_sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
            })
            saved = guard.load_json(registry)
            record = saved["__controller_host_tool_receipts__"]["controller-1"]["web"]["hte-1"]
            self.assertEqual(record["state"], "PREPARED")
            self.assertNotIn("bridge_capability_id", json.dumps(record, sort_keys=True))
            self.assertNotIn("host_mac_sha256", json.dumps(record, sort_keys=True))
            self.assertEqual(record["pre_receipt_sha256"], prepared["pre_receipt_sha256"])

    def test_host_tool_preparation_rejects_nonce_receipt_and_execution_replays_after_reload(self) -> None:
        """Would fail if any pre replay could claim a second persisted tuple after reload."""
        guard = load_guard()
        self.assertTrue(hasattr(guard, "prepare_host_tool_execution"), "prepare_host_tool_execution is missing")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            registry = self.make_web_receipt_registry(root, repo)
            snapshot = root / "snapshot.json"
            snapshot.write_text('{"snapshot":"current"}', encoding="utf-8")
            turn = self.verified_web_turn(guard)
            lifecycle = self.write_active_web_turn_lease(root, turn)
            guard.prepare_host_tool_execution(
                repo=repo, controller_id="controller-1", verified_turn=turn,
                host_pre_receipt=self.host_pre_receipt(), snapshot_path=snapshot, registry_path=registry,
                lifecycle_path=lifecycle,
            )
            for replay in (
                self.host_pre_receipt(
                    receipt_id="pre-receipt-2", nonce="pre-nonce-2",
                ),
                self.host_pre_receipt(
                    execution_id="hte-2", receipt_id="pre-receipt-2",
                ),
                self.host_pre_receipt(
                    execution_id="hte-3", nonce="pre-nonce-3",
                ),
            ):
                with self.assertRaisesRegex(PermissionError, "replay|already"):
                    guard.prepare_host_tool_execution(
                        repo=repo, controller_id="controller-1", verified_turn=turn,
                        host_pre_receipt=replay, snapshot_path=snapshot, registry_path=registry,
                        lifecycle_path=lifecycle,
                    )
            self.assertEqual(
                list(guard.load_json(registry)["__controller_host_tool_receipts__"]["controller-1"]["web"]),
                ["hte-1"],
            )

    def test_host_tool_terminal_retry_is_exact_and_close_requires_exact_guard_evidence(self) -> None:
        """Would fail if changed terminal data overwrote pending state or guard evidence could close another tuple."""
        guard = load_guard()
        self.assertTrue(hasattr(guard, "prepare_host_tool_execution"), "prepare_host_tool_execution is missing")
        self.assertTrue(hasattr(guard, "terminalize_host_tool_execution"), "terminalize_host_tool_execution is missing")
        self.assertTrue(hasattr(guard, "close_host_tool_execution"), "close_host_tool_execution is missing")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            registry = self.make_web_receipt_registry(root, repo)
            snapshot = root / "snapshot.json"
            snapshot.write_text('{"snapshot":"current"}', encoding="utf-8")
            turn = self.verified_web_turn(guard)
            lifecycle = self.write_active_web_turn_lease(root, turn)
            pre = self.host_pre_receipt()
            guard.prepare_host_tool_execution(
                repo=repo, controller_id="controller-1", verified_turn=turn,
                host_pre_receipt=pre, snapshot_path=snapshot, registry_path=registry,
                lifecycle_path=lifecycle,
            )
            terminal = self.host_terminal_receipt(pre_receipt=pre)
            pending = guard.terminalize_host_tool_execution(
                repo=repo, controller_id="controller-1", verified_turn=turn,
                host_terminal_receipt=terminal, snapshot_path=snapshot, registry_path=registry,
                lifecycle_path=lifecycle,
            )
            retry = guard.terminalize_host_tool_execution(
                repo=repo, controller_id="controller-1", verified_turn=turn,
                host_terminal_receipt=terminal, snapshot_path=snapshot, registry_path=registry,
                lifecycle_path=lifecycle,
            )
            self.assertEqual(pending, retry)
            self.assertEqual(pending["state"], "TERMINAL_PENDING")
            with self.assertRaisesRegex(PermissionError, "terminal receipt"):
                guard.terminalize_host_tool_execution(
                    repo=repo, controller_id="controller-1", verified_turn=turn,
                    host_terminal_receipt=self.host_terminal_receipt(pre_receipt=pre, response_sha256="c" * 64),
                    snapshot_path=snapshot, registry_path=registry, lifecycle_path=lifecycle,
                )
            before_pre_chain_mismatch = registry.read_bytes()
            for mismatch in (
                terminal | {"pre_receipt_id": "hpr_other"},
                terminal | {"pre_receipt_sha256": "9" * 64},
            ):
                with self.assertRaisesRegex(PermissionError, "pre receipt"):
                    guard.terminalize_host_tool_execution(
                        repo=repo, controller_id="controller-1", verified_turn=turn,
                        host_terminal_receipt=mismatch, snapshot_path=snapshot, registry_path=registry,
                        lifecycle_path=lifecycle,
                    )
                self.assertEqual(registry.read_bytes(), before_pre_chain_mismatch)
            evidence = self.host_tool_guard_evidence(
                terminal_receipt_sha256=pending["terminal_receipt_sha256"],
                snapshot_sha256=hashlib.sha256(snapshot.read_bytes()).hexdigest(),
                turn=turn,
            )
            closed = guard.close_host_tool_execution(
                repo=repo, controller_id="controller-1", verified_turn=turn,
                host_terminal_receipt=terminal, guard_evidence=evidence,
                snapshot_path=snapshot, registry_path=registry, lifecycle_path=lifecycle,
            )
            self.assertEqual(closed["state"], "CLOSED")
            bad_evidence = evidence | {"host_tool_execution_id": "hte-other"}
            with self.assertRaisesRegex(PermissionError, "guard evidence"):
                guard.close_host_tool_execution(
                    repo=repo, controller_id="controller-1", verified_turn=turn,
                    host_terminal_receipt=terminal, guard_evidence=bad_evidence,
                    snapshot_path=snapshot, registry_path=registry, lifecycle_path=lifecycle,
                )

    def test_host_tool_cas_has_one_concurrent_winner_and_fails_closed_on_tuple_drift_and_capacity(self) -> None:
        """Would fail if races, stale canonical bindings, or a full ledger admitted another execution."""
        guard = load_guard()
        self.assertTrue(hasattr(guard, "prepare_host_tool_execution"), "prepare_host_tool_execution is missing")
        self.assertTrue(
            hasattr(guard, "MAX_HOST_TOOL_RECEIPTS_PER_HOST"),
            "MAX_HOST_TOOL_RECEIPTS_PER_HOST is missing",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            registry = self.make_web_receipt_registry(root, repo)
            snapshot = root / "snapshot.json"
            snapshot.write_text('{"snapshot":"current"}', encoding="utf-8")
            turn = self.verified_web_turn(guard)
            lifecycle = self.write_active_web_turn_lease(root, turn)
            outcomes: list[str] = []

            def prepare() -> None:
                try:
                    guard.prepare_host_tool_execution(
                        repo=repo, controller_id="controller-1", verified_turn=turn,
                        host_pre_receipt=self.host_pre_receipt(), snapshot_path=snapshot, registry_path=registry,
                        lifecycle_path=lifecycle,
                    )
                    outcomes.append("winner")
                except PermissionError:
                    outcomes.append("blocked")

            threads = [threading.Thread(target=prepare), threading.Thread(target=prepare)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=2)
                self.assertFalse(thread.is_alive())
            self.assertEqual(sorted(outcomes), ["blocked", "winner"])

            saved = guard.load_json(registry)
            saved["__controller_targets__"]["controller-1"]["web"]["generation"] = 5
            registry.write_text(json.dumps(saved), encoding="utf-8")
            with self.assertRaisesRegex(PermissionError, "target generation"):
                guard.prepare_host_tool_execution(
                    repo=repo, controller_id="controller-1", verified_turn=turn,
                    host_pre_receipt=self.host_pre_receipt(execution_id="hte-drift", receipt_id="pre-drift", nonce="nonce-drift"),
                    snapshot_path=snapshot, registry_path=registry, lifecycle_path=lifecycle,
                )

            saved["__controller_targets__"]["controller-1"]["web"]["generation"] = 4
            saved["__controller_execution_ownership__"]["controller-1"]["generation"] = 10
            registry.write_text(json.dumps(saved), encoding="utf-8")
            with self.assertRaisesRegex(PermissionError, "ownership generation"):
                guard.prepare_host_tool_execution(
                    repo=repo, controller_id="controller-1", verified_turn=turn,
                    host_pre_receipt=self.host_pre_receipt(execution_id="hte-owner", receipt_id="pre-owner", nonce="nonce-owner"),
                    snapshot_path=snapshot, registry_path=registry, lifecycle_path=lifecycle,
                )

            saved["__controller_execution_ownership__"]["controller-1"]["generation"] = 9
            registry.write_text(json.dumps(saved), encoding="utf-8")
            before_coordinated_turn = registry.read_bytes()
            with self.assertRaisesRegex(PermissionError, "Runtime Web turn lease"):
                guard.prepare_host_tool_execution(
                    repo=repo, controller_id="controller-1",
                    verified_turn=self.verified_web_turn(guard, runtime_invocation_id="caller-invented-turn"),
                    host_pre_receipt=self.host_pre_receipt(execution_id="hte-turn", receipt_id="pre-turn", nonce="nonce-turn"),
                    snapshot_path=snapshot, registry_path=registry, lifecycle_path=lifecycle,
                )
            self.assertEqual(registry.read_bytes(), before_coordinated_turn)

            saved = self.make_web_receipt_registry(root, repo)
            lifecycle = self.write_active_web_turn_lease(root, turn)
            full = guard.load_json(saved)
            full["__controller_host_tool_receipts__"] = {"controller-1": {"web": {
                f"hte-{index}": {"state": "PREPARED"} for index in range(guard.MAX_HOST_TOOL_RECEIPTS_PER_HOST)
            }}}
            saved.write_text(json.dumps(full), encoding="utf-8")
            with self.assertRaisesRegex(PermissionError, "receipt limit"):
                guard.prepare_host_tool_execution(
                    repo=repo, controller_id="controller-1", verified_turn=turn,
                    host_pre_receipt=self.host_pre_receipt(execution_id="hte-overflow", receipt_id="pre-overflow", nonce="nonce-overflow"),
                    snapshot_path=snapshot, registry_path=saved, lifecycle_path=lifecycle,
                )
            self.assertEqual(
                len(guard.load_json(saved)["__controller_host_tool_receipts__"]["controller-1"]["web"]),
                guard.MAX_HOST_TOOL_RECEIPTS_PER_HOST,
            )

    def test_claim_controller_host_web_initializes_cross_host_ownership(self) -> None:
        guard = load_guard()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {
                    "controller-1": {"web": ["web-current"]}
                },
                "__controller_targets__": {
                    "controller-1": {
                        "web": {
                            "status": "active",
                            "session_id": "web-current",
                            "generation": 4,
                        }
                    }
                },
            }), encoding="utf-8")

            receipt = guard.claim_controller_host(
                repo=repo,
                controller_id="controller-1",
                requested_host="web",
                requested_target_session_id="web-current",
                expected_generation=0,
                registry_path=registry,
                provenance="test_web_entry",
            )

            self.assertEqual(receipt["controller_id"], "controller-1")
            self.assertEqual(receipt["active_host"], "web")
            self.assertEqual(receipt["execution_target_session_id"], "web-current")
            self.assertEqual(receipt["generation"], 1)
            resolved = guard.resolve_execution_ownership(repo=repo, registry_path=registry)
            self.assertEqual(resolved["controller_id"], "controller-1")
            self.assertEqual(resolved["active_host"], "web")
            self.assertEqual(resolved["execution_target_session_id"], "web-current")
            self.assertEqual(resolved["generation"], 1)
            saved = guard.load_json(registry)
            self.assertEqual(saved["controller-1"], str(repo.resolve()))

    def test_claim_controller_host_desktop_after_web_increments_one_cross_host_generation(self) -> None:
        guard = load_guard()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {
                    "controller-1": {
                        "web": ["web-current"],
                        "desktop_codex": ["desktop-current"],
                    }
                },
                "__controller_targets__": {
                    "controller-1": {
                        "web": {"status": "active", "session_id": "web-current", "generation": 4},
                        "desktop_codex": {"status": "active", "session_id": "desktop-current", "generation": 7},
                    }
                },
                "__controller_execution_ownership__": {
                    "controller-1": {
                        "active_host": "web",
                        "execution_target_session_id": "web-current",
                        "generation": 1,
                    }
                },
            }), encoding="utf-8")

            receipt = guard.claim_controller_host(
                repo=repo, controller_id="controller-1", requested_host="desktop_codex",
                requested_target_session_id="desktop-current", expected_generation=1,
                registry_path=registry, provenance="test_desktop_entry",
            )

            self.assertEqual(receipt["controller_id"], "controller-1")
            self.assertEqual(receipt["active_host"], "desktop_codex")
            self.assertEqual(receipt["execution_target_session_id"], "desktop-current")
            self.assertEqual(receipt["generation"], 2)
            saved = guard.load_json(registry)
            self.assertEqual(saved["__controller_targets__"]["controller-1"]["web"]["session_id"], "web-current")
            self.assertEqual(saved["__controller_targets__"]["controller-1"]["desktop_codex"]["session_id"], "desktop-current")

    def test_claim_controller_host_rejects_stale_cross_host_generation_without_mutation(self) -> None:
        guard = load_guard()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            registry = root / "controllers.json"
            payload = {
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {
                    "controller-1": {
                        "web": ["web-current"],
                        "desktop_codex": ["desktop-current"],
                    }
                },
                "__controller_targets__": {
                    "controller-1": {
                        "web": {"status": "active", "session_id": "web-current", "generation": 4},
                        "desktop_codex": {"status": "active", "session_id": "desktop-current", "generation": 7},
                    }
                },
                "__controller_execution_ownership__": {
                    "controller-1": {
                        "active_host": "desktop_codex",
                        "execution_target_session_id": "desktop-current",
                        "generation": 2,
                    }
                },
            }
            registry.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(PermissionError, "generation"):
                guard.claim_controller_host(
                    repo=repo, controller_id="controller-1", requested_host="web",
                    requested_target_session_id="web-current", expected_generation=1,
                    registry_path=registry,
                )

            self.assertEqual(guard.load_json(registry), payload)

    def test_collaboration_spawn_contract_captures_task_model_and_agent_type(self) -> None:
        guard = load_guard()
        contract = guard.collaboration_spawn_contract(
            tool_name="collaboration.spawn_agent",
            tool_input={
                "task_name": "WEB-1",
                "model": "gpt-5.6-terra",
                "agent_type": "ui_writer",
            },
        )
        self.assertEqual(contract, {
            "task_name": "WEB-1",
            "model": "gpt-5.6-terra",
            "agent_type": "ui_writer",
        })

    def test_identity_projection_keeps_unique_project_controller_when_session_id_unavailable(self) -> None:
        guard = load_guard()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            registry = root / "controllers.json"
            registry.write_text(
                json.dumps({"controller-1": str(repo.resolve())}),
                encoding="utf-8",
            )

            identity = guard.controller_identity_projection(
                repo=repo,
                host="web",
                source_session_id=None,
                registry_path=registry,
            )

            project = identity["project_controller_state"]
            binding = identity["session_binding_state"]
            self.assertEqual(project["project_controller"], "EXISTING")
            self.assertEqual(project["controller_id"], "controller-1")
            self.assertEqual(project["uniqueness"], "UNIQUE")
            self.assertEqual(project["ownership"], "ACTIVE")
            self.assertFalse(project["create_new_controller_allowed"])
            self.assertEqual(binding["verification"], "UNVERIFIED")
            self.assertEqual(binding["reason"], "HOST_SESSION_ID_UNAVAILABLE")
            self.assertEqual(
                binding["recovery"], "SAME_CONTROLLER_SESSION_RECOVERY_REQUIRED"
            )
            self.assertFalse(identity["controller_actions_allowed"])
            self.assertTrue(identity["same_controller_recovery_allowed"])
            self.assertFalse(identity["create_new_controller_allowed"])


    def test_identity_projection_marks_unique_controller_with_missing_host_session_as_degraded(self) -> None:
        guard = load_guard()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({"controller-1": str(repo.resolve())}), encoding="utf-8")

            identity = guard.controller_identity_projection(
                repo=repo, host="web", source_session_id=None, registry_path=registry
            )

            self.assertEqual(identity["identity_state"], "DEGRADED")
            self.assertEqual(identity["project_controller_state"]["controller_id"], "controller-1")
            self.assertFalse(identity["controller_actions_allowed"])
            self.assertTrue(identity["same_controller_recovery_allowed"])
            self.assertFalse(identity["create_new_controller_allowed"])

    def test_identity_projection_degrades_legacy_browser_attested_web_target(self) -> None:
        guard = load_guard()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {
                    "controller-1": {"web": ["web-current"]}
                },
                "__controller_targets__": {
                    "controller-1": {"web": {
                        "status": "active",
                        "session_id": "web-current",
                        "generation": 2,
                        "provenance": "host_attested_same_controller_recovery",
                        "binding_mode": "resume_only",
                        "host_identity_receipt_sha256": "a" * 64,
                    }}
                },
            }), encoding="utf-8")

            identity = guard.controller_identity_projection(
                repo=repo,
                host="web",
                source_session_id="web-current",
                registry_path=registry,
            )

            self.assertEqual(identity["identity_state"], "DEGRADED")
            self.assertEqual(
                identity["session_binding_state"]["reason"],
                "HOST_IDENTITY_UNAVAILABLE",
            )
            self.assertFalse(identity["controller_actions_allowed"])
            self.assertTrue(identity["same_controller_recovery_allowed"])

    def test_identity_projection_marks_real_controller_conflict_as_conflicted(self) -> None:
        guard = load_guard()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "controller-2": str(repo.resolve()),
            }), encoding="utf-8")

            identity = guard.controller_identity_projection(
                repo=repo, host="web", source_session_id="web-current", registry_path=registry
            )

            self.assertEqual(identity["identity_state"], "CONFLICTED")
            self.assertFalse(identity["controller_actions_allowed"])
            self.assertFalse(identity["same_controller_recovery_allowed"])
            self.assertFalse(identity["create_new_controller_allowed"])

    def test_identity_capability_contract_exposes_canonical_projection_and_cli(self) -> None:
        guard = load_guard()
        capabilities = guard.controller_identity_capabilities()
        self.assertEqual(capabilities["schema_version"], 1)
        self.assertEqual(capabilities["canonical_identity_cli"], "controller_target_guard.py identity")
        self.assertEqual(set(capabilities["capabilities"]), {
            "controller_identity_projection",
            "same_controller_recovery",
            "web_session_binding",
            "target_generation_fence",
            "logical_agent_target_resolution",
            "verified_execution_target_fence",
        })
        self.assertEqual(
            capabilities["logical_agent_target_resolution_contract"],
            "logical_agent_target_resolution_v1",
        )
        self.assertEqual(
            set(capabilities["supported_logical_agent_types"]),
            {"controller", "agent", "reviewer", "runtime_repair_agent"},
        )
        self.assertEqual(
            set(capabilities["logical_agent_target_resolution_states"]),
            {"VERIFIED", "UNRESOLVED", "STALE", "CONFLICTED"},
        )
        self.assertEqual(capabilities["ownership_resolver_scope"], "controller_registry_only")
        self.assertEqual(capabilities["automatic_problem_attribution"], "post_migration_enhancement")

    def test_verified_logical_agent_target_projects_controller_and_defers_other_agent_ownership(self) -> None:
        guard = load_guard()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {"controller-1": {"web": ["web-current"]}},
                "__controller_targets__": {"controller-1": {"web": {
                    "status": "active", "session_id": "web-current", "generation": 4,
                    "provenance": "host_attested_same_controller_recovery",
                    "binding_mode": "resume_only", "identity_proof": "host_attested_origin",
                }}},
                "__controller_execution_ownership__": {"controller-1": {
                    "active_host": "web", "execution_target_session_id": "web-current",
                    "generation": 9, "provenance": "web_entry",
                }},
            }), encoding="utf-8")
            target = guard.resolve_verified_logical_agent_execution_target(
                repo=repo, host="web",
                logical_agent_identity={
                    "schema_version": 1, "agent_type": "controller", "agent_id": "controller-1"
                },
                registry_path=registry,
            )
            self.assertEqual(target["contract"], "verified_execution_target_v1")
            self.assertEqual(target["logical_agent_identity"]["agent_type"], "controller")
            self.assertEqual(target["execution_target_session_id"], "web-current")
            self.assertEqual(target["target_generation"], 4)
            self.assertEqual(target["ownership_generation"], 9)
            for agent_type in ("agent", "reviewer", "runtime_repair_agent"):
                unresolved = guard.resolve_logical_agent_execution_target(
                    repo=repo, host="web",
                    logical_agent_identity={
                        "schema_version": 1, "agent_type": agent_type, "agent_id": f"{agent_type}-7"
                    },
                    registry_path=registry,
                )
                self.assertEqual(unresolved["contract"], "logical_agent_target_resolution_v1")
                self.assertEqual(unresolved["state"], "UNRESOLVED")
                self.assertEqual(unresolved["reason"], "OWNERSHIP_RESOLVER_REQUIRED")

            with self.assertRaisesRegex(PermissionError, "OWNERSHIP_RESOLVER_REQUIRED"):
                guard.resolve_verified_logical_agent_execution_target(
                    repo=repo, host="web",
                    logical_agent_identity={
                        "schema_version": 1, "agent_type": "runtime_repair_agent", "agent_id": "repair-7"
                    },
                    registry_path=registry,
                )

    def test_multiple_web_aliases_without_current_target_are_stale_not_verified(self) -> None:
        guard = load_guard()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {
                    "controller-1": {"web": ["web-old", "web-new"]}
                },
            }), encoding="utf-8")

            for session_id in ("web-old", "web-new"):
                with self.subTest(session_id=session_id):
                    identity = guard.controller_identity_projection(
                        repo=repo,
                        host="web",
                        source_session_id=session_id,
                        registry_path=registry,
                    )
                    self.assertEqual(
                        identity["project_controller_state"]["controller_id"],
                        "controller-1",
                    )
                    self.assertEqual(
                        identity["session_binding_state"]["verification"],
                        "STALE",
                    )
                    self.assertEqual(
                        identity["session_binding_state"]["reason"],
                        "EXPLICIT_CURRENT_TARGET_REQUIRED",
                    )
                    self.assertEqual(
                        identity["session_binding_state"]["recovery"],
                        "SAME_CONTROLLER_SESSION_RECOVERY_REQUIRED",
                    )
                    self.assertFalse(identity["controller_actions_allowed"])
                    self.assertFalse(identity["create_new_controller_allowed"])

    def test_identity_projection_verifies_current_desktop_target_without_changing_controller_id(self) -> None:
        guard = load_guard()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {
                    "controller-1": {"desktop_codex": ["desktop-old", "desktop-current"]}
                },
                "__controller_targets__": {
                    "controller-1": {
                        "desktop_codex": {
                            "status": "active",
                            "session_id": "desktop-current",
                            "generation": 9,
                        }
                    }
                },
            }), encoding="utf-8")

            identity = guard.controller_identity_projection(
                repo=repo,
                host="desktop_codex",
                source_session_id="desktop-current",
                registry_path=registry,
            )

            self.assertEqual(
                identity["project_controller_state"]["controller_id"],
                "controller-1",
            )
            binding = identity["session_binding_state"]
            self.assertEqual(binding["verification"], "VERIFIED")
            self.assertEqual(binding["binding_mode"], "explicit_current")
            self.assertEqual(binding["target_generation"], 9)
            self.assertTrue(identity["controller_actions_allowed"])

    def test_identity_projection_marks_old_target_stale_but_keeps_project_ownership(self) -> None:
        guard = load_guard()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {
                    "controller-1": {"desktop_codex": ["desktop-old", "desktop-current"]}
                },
                "__controller_targets__": {
                    "controller-1": {
                        "desktop_codex": {
                            "status": "active",
                            "session_id": "desktop-current",
                            "generation": 4,
                        }
                    }
                },
            }), encoding="utf-8")

            identity = guard.controller_identity_projection(
                repo=repo,
                host="desktop_codex",
                source_session_id="desktop-old",
                registry_path=registry,
            )

            self.assertEqual(
                identity["project_controller_state"]["project_controller"],
                "EXISTING",
            )
            self.assertEqual(
                identity["project_controller_state"]["controller_id"],
                "controller-1",
            )
            binding = identity["session_binding_state"]
            self.assertEqual(binding["verification"], "STALE")
            self.assertEqual(binding["reason"], "SESSION_NOT_CURRENT_TARGET")
            self.assertEqual(
                binding["recovery"], "SAME_CONTROLLER_SESSION_RECOVERY_REQUIRED"
            )
            self.assertFalse(identity["controller_actions_allowed"])
            self.assertFalse(identity["create_new_controller_allowed"])

    def test_identity_projection_reports_project_controller_conflict_without_silent_selection(self) -> None:
        guard = load_guard()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "controller-2": str(repo.resolve()),
            }), encoding="utf-8")

            identity = guard.controller_identity_projection(
                repo=repo,
                host="web",
                source_session_id="web-current",
                registry_path=registry,
            )

            project = identity["project_controller_state"]
            self.assertEqual(project["project_controller"], "CONFLICT")
            self.assertEqual(project["uniqueness"], "CONFLICT")
            self.assertIsNone(project["controller_id"])
            self.assertEqual(
                set(project["matching_controller_ids"]),
                {"controller-1", "controller-2"},
            )
            self.assertEqual(
                identity["session_binding_state"]["verification"],
                "CONFLICT",
            )
            self.assertFalse(identity["controller_actions_allowed"])
            self.assertFalse(identity["same_controller_recovery_allowed"])
            self.assertFalse(identity["create_new_controller_allowed"])

    def test_explicit_current_target_is_the_only_allowed_desktop_outbound_target(self) -> None:
        guard = load_guard()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-old": str(repo.resolve()),
                "__controller_sessions__": {
                    "controller-old": {"desktop_codex": ["desktop-old", "desktop-current"]}
                },
                "__controller_targets__": {
                    "controller-old": {
                        "desktop_codex": {
                            "status": "active",
                            "session_id": "desktop-current",
                            "generation": 7,
                        }
                    }
                },
            }), encoding="utf-8")

            resolved = guard.resolve_execution_target(
                repo=repo, host="desktop_codex", registry_path=registry
            )
            allowed = guard.check_execution_target(
                repo=repo,
                host="desktop_codex",
                action="message",
                target_session_id="desktop-current",
                registry_path=registry,
            )

            self.assertEqual(resolved["controller_id"], "controller-old")
            self.assertEqual(resolved["execution_target_session_id"], "desktop-current")
            self.assertEqual(resolved["generation"], 7)
            self.assertEqual(allowed["result"], "ALLOWED")
            for stale in ("controller-old", "desktop-old"):
                with self.assertRaisesRegex(PermissionError, "current desktop_codex target"):
                    guard.check_execution_target(
                        repo=repo,
                        host="desktop_codex",
                        action="navigate",
                        target_session_id=stale,
                        registry_path=registry,
                    )

    def test_alias_registry_without_explicit_target_fails_closed(self) -> None:
        guard = load_guard()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-old": str(repo.resolve()),
                "__controller_sessions__": {
                    "controller-old": {"desktop_codex": ["desktop-current"]}
                },
            }), encoding="utf-8")

            with self.assertRaisesRegex(PermissionError, "explicit current target"):
                guard.resolve_execution_target(
                    repo=repo, host="desktop_codex", registry_path=registry
                )

    def test_unbound_tombstone_never_falls_back_to_canonical(self) -> None:
        guard = load_guard()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-old": str(repo.resolve()),
                "__controller_targets__": {
                    "controller-old": {
                        "desktop_codex": {
                            "status": "unbound",
                            "session_id": None,
                            "generation": 3,
                        }
                    }
                },
            }), encoding="utf-8")

            with self.assertRaisesRegex(PermissionError, "unbound"):
                guard.resolve_execution_target(
                    repo=repo, host="desktop_codex", registry_path=registry
                )

    def test_malformed_target_metadata_never_falls_back_to_legacy_canonical(self) -> None:
        guard = load_guard()
        malformed_targets = (
            [],
            {"controller-old": []},
            {"controller-old": {"desktop_codex": []}},
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            registry = root / "controllers.json"
            for targets in malformed_targets:
                with self.subTest(targets=targets):
                    registry.write_text(json.dumps({
                        "controller-old": str(repo.resolve()),
                        "__controller_targets__": targets,
                    }), encoding="utf-8")

                    with self.assertRaisesRegex(ValueError, "target"):
                        guard.resolve_execution_target(
                            repo=repo, host="desktop_codex", registry_path=registry
                        )
                    with self.assertRaisesRegex(ValueError, "target"):
                        guard.active_source_controller_id(
                            guard.load_json(registry),
                            source_session_id="controller-old",
                            host="desktop_codex",
                        )

    def test_malformed_session_metadata_never_falls_back_to_legacy_canonical(self) -> None:
        guard = load_guard()
        malformed_sessions = (
            [],
            {"controller-old": []},
            {"controller-old": {"desktop_codex": {}}},
            {"controller-old": {"desktop_codex": [123]}},
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            registry = root / "controllers.json"
            for sessions in malformed_sessions:
                with self.subTest(sessions=sessions):
                    registry.write_text(json.dumps({
                        "controller-old": str(repo.resolve()),
                        "__controller_sessions__": sessions,
                    }), encoding="utf-8")

                    with self.assertRaisesRegex(ValueError, "session"):
                        guard.resolve_execution_target(
                            repo=repo, host="desktop_codex", registry_path=registry
                        )
                    with self.assertRaisesRegex(ValueError, "session"):
                        guard.active_source_controller_id(
                            guard.load_json(registry),
                            source_session_id="controller-old",
                            host="desktop_codex",
                        )

    def test_semantically_invalid_target_record_is_rejected_by_resolve_and_source_guard(self) -> None:
        guard = load_guard()
        invalid_records = (
            {"status": "active", "session_id": "controller-old", "generation": True},
            {"status": "active", "session_id": 7, "generation": 1},
            {"status": "unbound", "session_id": "controller-old", "generation": 1},
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            registry = root / "controllers.json"
            for record in invalid_records:
                with self.subTest(record=record):
                    registry.write_text(json.dumps({
                        "controller-old": str(repo.resolve()),
                        "__controller_targets__": {"controller-old": {"desktop_codex": record}},
                    }), encoding="utf-8")

                    with self.assertRaisesRegex(PermissionError, "target"):
                        guard.resolve_execution_target(
                            repo=repo, host="desktop_codex", registry_path=registry
                        )
                    with self.assertRaisesRegex(PermissionError, "target"):
                        guard.active_source_controller_id(
                            guard.load_json(registry),
                            source_session_id="controller-old",
                            host="desktop_codex",
                        )

    def test_legacy_controller_without_aliases_keeps_canonical_target(self) -> None:
        guard = load_guard()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            registry = root / "controllers.json"
            registry.write_text(
                json.dumps({"controller-only": str(repo.resolve())}), encoding="utf-8"
            )

            resolved = guard.resolve_execution_target(
                repo=repo, host="desktop_codex", registry_path=registry
            )

            self.assertEqual(resolved["execution_target_session_id"], "controller-only")
            self.assertEqual(resolved["generation"], 0)
            self.assertEqual(resolved["target_mode"], "legacy_canonical")

    def test_cli_rejects_stale_target_with_nonzero_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-old": str(repo.resolve()),
                "__controller_sessions__": {
                    "controller-old": {"desktop_codex": ["desktop-current"]}
                },
                "__controller_targets__": {
                    "controller-old": {
                        "desktop_codex": {
                            "status": "active",
                            "session_id": "desktop-current",
                            "generation": 1,
                        }
                    }
                },
            }), encoding="utf-8")

            completed = subprocess.run(
                [
                    sys.executable,
                    str(GUARD),
                    "check",
                    "--repo",
                    str(repo),
                    "--host",
                    "desktop_codex",
                    "--action",
                    "message",
                    "--target-session-id",
                    "controller-old",
                    "--registry",
                    str(registry),
                ],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 78)
            self.assertIn("current desktop_codex target", completed.stderr)

    def test_open_in_codex_is_guarded_only_when_it_names_a_thread_target(self) -> None:
        guard = load_guard()

        self.assertEqual(
            guard.codex_app_outbound_request(
                tool_name="mcp__codex_app__open_in_codex",
                tool_input={"threadId": "desktop-current", "target": {"type": "file"}},
            ),
            ("navigate", "desktop-current"),
        )
        self.assertIsNone(guard.codex_app_outbound_request(
            tool_name="mcp__codex_app__open_in_codex",
            tool_input={"target": {"type": "file"}},
        ))

    def test_locked_target_blocks_replace_writer_until_outbound_launch_boundary_closes(self) -> None:
        guard = load_guard()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-old": str(repo.resolve()),
                "__controller_sessions__": {
                    "controller-old": {"desktop_codex": ["desktop-current"]}
                },
                "__controller_targets__": {
                    "controller-old": {
                        "desktop_codex": {
                            "status": "active",
                            "session_id": "desktop-current",
                            "generation": 7,
                        }
                    }
                },
            }), encoding="utf-8")
            writer_started = threading.Event()
            writer_acquired = threading.Event()

            def acquire_writer_lock() -> None:
                lock_path = guard.registry_lock_path(registry)
                with lock_path.open("a+") as lock:
                    writer_started.set()
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                    writer_acquired.set()
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

            with guard.locked_execution_target(
                repo=repo,
                host="desktop_codex",
                registry_path=registry,
            ) as receipt:
                self.assertEqual(receipt["execution_target_session_id"], "desktop-current")
                thread = threading.Thread(target=acquire_writer_lock)
                thread.start()
                self.assertTrue(writer_started.wait(1))
                time.sleep(0.05)
                self.assertFalse(writer_acquired.is_set())

            thread.join(timeout=1)
            self.assertFalse(thread.is_alive())
            self.assertTrue(writer_acquired.is_set())

    def test_outbound_lease_persists_current_target_and_generation_for_tool_use(self) -> None:
        guard = load_guard()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-old": str(repo.resolve()),
                "__controller_sessions__": {
                    "controller-old": {"desktop_codex": ["desktop-current"]}
                },
                "__controller_targets__": {
                    "controller-old": {
                        "desktop_codex": {
                            "status": "active",
                            "session_id": "desktop-current",
                            "generation": 7,
                        }
                    }
                },
            }), encoding="utf-8")

            first = guard.acquire_outbound_lease(
                repo=repo,
                host="desktop_codex",
                action="message",
                target_session_id="desktop-current",
                tool_use_id="message-1",
                registry_path=registry,
            )

            self.assertEqual(first["tool_use_id"], "message-1")
            self.assertEqual(first["generation"], 7)
            self.assertTrue(
                guard.has_active_outbound_lease(
                    repo=repo, host="desktop_codex", registry_path=registry
                )
            )

            with self.assertRaisesRegex(PermissionError, "active lease"):
                guard.release_outbound_lease(
                    repo=repo,
                    host="desktop_codex",
                    tool_use_id="message-1",
                    expected_action="message",
                    expected_target_session_id="desktop-other",
                    registry_path=registry,
                )
            self.assertTrue(guard.has_active_outbound_lease(
                repo=repo, host="desktop_codex", registry_path=registry
            ))

    def test_outbound_lease_refuses_a_source_session_replaced_before_pretool_dispatch(self) -> None:
        guard = load_guard()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-old": str(repo.resolve()),
                "__controller_sessions__": {
                    "controller-old": {"desktop_codex": ["desktop-old", "desktop-current"]}
                },
                "__controller_targets__": {
                    "controller-old": {
                        "desktop_codex": {
                            "status": "active",
                            "session_id": "desktop-current",
                            "generation": 7,
                        }
                    }
                },
            }), encoding="utf-8")

            with self.assertRaisesRegex(PermissionError, "source session"):
                guard.acquire_outbound_lease(
                    repo=repo,
                    host="desktop_codex",
                    action="message",
                    target_session_id="desktop-current",
                    tool_use_id="message-1",
                    source_session_id="desktop-old",
                    registry_path=registry,
                )

            self.assertFalse(guard.has_active_outbound_lease(
                repo=repo, host="desktop_codex", registry_path=registry
            ))

    def test_outbound_lease_blocks_replace_and_unbind_until_its_tool_use_completes(self) -> None:
        guard = load_guard()
        sys.path.insert(0, str(ROOT / "scripts"))
        import lifecycle_hook

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-old": str(repo.resolve()),
                "__controller_sessions__": {
                    "controller-old": {"desktop_codex": ["desktop-current"]}
                },
                "__controller_targets__": {
                    "controller-old": {
                        "desktop_codex": {
                            "status": "active",
                            "session_id": "desktop-current",
                            "generation": 7,
                        }
                    }
                },
            }), encoding="utf-8")
            original_registry = lifecycle_hook.REGISTRY_PATH
            lifecycle_hook.REGISTRY_PATH = registry
            try:
                guard.acquire_outbound_lease(
                    repo=repo,
                    host="desktop_codex",
                    action="message",
                    target_session_id="desktop-current",
                    tool_use_id="message-1",
                    registry_path=registry,
                )

                with self.assertRaisesRegex(PermissionError, "active outbound lease"):
                    lifecycle_hook.replace_desktop_session(
                        controller_id="controller-old",
                        desktop_session_id="desktop-next",
                        repo=repo,
                        expected_generation=7,
                    )
                with self.assertRaisesRegex(PermissionError, "active outbound lease"):
                    lifecycle_hook.unbind_desktop_session(
                        controller_id="controller-old",
                        desktop_session_id="desktop-current",
                        repo=repo,
                        expected_generation=7,
                    )

                self.assertTrue(guard.release_outbound_lease(
                    repo=repo,
                    host="desktop_codex",
                    tool_use_id="message-1",
                    expected_action="message",
                    expected_target_session_id="desktop-current",
                    registry_path=registry,
                ))
                replaced = lifecycle_hook.replace_desktop_session(
                    controller_id="controller-old",
                    desktop_session_id="desktop-next",
                    repo=repo,
                    expected_generation=7,
                )
            finally:
                lifecycle_hook.REGISTRY_PATH = original_registry

        self.assertEqual(replaced["session_id"], "desktop-next")

    def test_admin_reconcile_releases_only_the_exact_terminal_lease_and_records_audit(self) -> None:
        guard = load_guard()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-old": str(repo.resolve()),
                "__controller_targets__": {"controller-old": {"desktop_codex": {
                    "status": "active", "session_id": "controller-old", "generation": 3,
                }}},
            }), encoding="utf-8")
            guard.acquire_outbound_lease(
                repo=repo, host="desktop_codex", action="message",
                target_session_id="controller-old", tool_use_id="message-1", registry_path=registry,
            )

            with self.assertRaisesRegex(PermissionError, "current desktop_codex target"):
                guard.reconcile_outbound_lease(
                    controller_id="controller-old", repo=repo, host="desktop_codex",
                    tool_use_id="message-1", action="message", target_session_id="desktop-other",
                    generation=3, host_receipt_reference="host:terminal:123", reason="host confirmed terminal",
                    registry_path=registry,
                )
            self.assertTrue(guard.has_active_outbound_lease(
                repo=repo, host="desktop_codex", registry_path=registry
            ))

            receipt = guard.reconcile_outbound_lease(
                controller_id="controller-old", repo=repo, host="desktop_codex",
                tool_use_id="message-1", action="message", target_session_id="controller-old",
                generation=3, host_receipt_reference="host:terminal:123", reason="host confirmed terminal",
                registry_path=registry,
            )

            self.assertEqual(receipt["result"], "RECONCILED")
            self.assertFalse(guard.has_active_outbound_lease(
                repo=repo, host="desktop_codex", registry_path=registry
            ))
            saved = guard.load_json(registry)
            self.assertEqual(saved["__controller_outbound_lease_reconciliations__"], [{
                "controller_id": "controller-old", "host": "desktop_codex", "tool_use_id": "message-1",
                "action": "message", "target_session_id": "controller-old", "generation": 3,
                "host_receipt_reference": "host:terminal:123", "reason": "host confirmed terminal",
            }])

    def test_reconcile_cli_requires_a_host_receipt_and_releases_after_exact_match(self) -> None:
        guard = load_guard()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-old": str(repo.resolve()),
                "__controller_targets__": {"controller-old": {"desktop_codex": {
                    "status": "active", "session_id": "controller-old", "generation": 3,
                }}},
            }), encoding="utf-8")
            guard.acquire_outbound_lease(
                repo=repo, host="desktop_codex", action="message",
                target_session_id="controller-old", tool_use_id="message-1", registry_path=registry,
            )

            missing_receipt = subprocess.run([
                sys.executable, str(GUARD), "reconcile", "--controller-id", "controller-old",
                "--repo", str(repo), "--host", "desktop_codex", "--tool-use-id", "message-1",
                "--action", "message", "--target-session-id", "controller-old", "--generation", "3",
                "--reason", "host confirmed terminal", "--registry", str(registry),
            ], text=True, capture_output=True, check=False)
            completed = subprocess.run([
                sys.executable, str(GUARD), "reconcile", "--controller-id", "controller-old",
                "--repo", str(repo), "--host", "desktop_codex", "--tool-use-id", "message-1",
                "--action", "message", "--target-session-id", "controller-old", "--generation", "3",
                "--host-receipt-reference", "host:terminal:123", "--reason", "host confirmed terminal",
                "--registry", str(registry),
            ], text=True, capture_output=True, check=False)

        self.assertNotEqual(missing_receipt.returncode, 0)
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(json.loads(completed.stdout)["result"], "RECONCILED")

    def test_outbound_lease_and_reconcile_reject_unbounded_identifiers_and_a_full_host_lease_set(self) -> None:
        guard = load_guard()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-old": str(repo.resolve()),
                "__controller_targets__": {"controller-old": {"desktop_codex": {
                    "status": "active", "session_id": "controller-old", "generation": 3,
                }}},
            }), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "too long"):
                guard.acquire_outbound_lease(
                    repo=repo, host="desktop_codex", action="message",
                    target_session_id="controller-old", tool_use_id="x" * 257, registry_path=registry,
                )
            for index in range(64):
                guard.acquire_outbound_lease(
                    repo=repo, host="desktop_codex", action="message",
                    target_session_id="controller-old", tool_use_id=f"message-{index}", registry_path=registry,
                )
            with self.assertRaisesRegex(PermissionError, "lease limit"):
                guard.acquire_outbound_lease(
                    repo=repo, host="desktop_codex", action="message",
                    target_session_id="controller-old", tool_use_id="message-overflow", registry_path=registry,
                )
            with self.assertRaisesRegex(ValueError, "too long"):
                guard.reconcile_outbound_lease(
                    controller_id="controller-old", repo=repo, host="desktop_codex",
                    tool_use_id="message-0", action="message", target_session_id="controller-old",
                    generation=3, host_receipt_reference="r" * 1025, reason="host terminal",
                    registry_path=registry,
                )

    def test_collaboration_spawn_agent_is_recognized_as_runtime_managed_dispatch(self) -> None:
        guard = load_guard()
        for tool_name in ("spawn_agent", "collaboration.spawn_agent", "mcp__collaboration__spawn_agent"):
            with self.subTest(tool_name=tool_name):
                self.assertEqual(
                    guard.collaboration_spawn_task_name(
                        tool_name=tool_name, tool_input={"task_name": "WEB-WRITER-1"}
                    ),
                    "WEB-WRITER-1",
                )
        self.assertIsNone(
            guard.collaboration_spawn_task_name(
                tool_name="send_message", tool_input={"task_name": "WEB-WRITER-1"}
            )
        )
