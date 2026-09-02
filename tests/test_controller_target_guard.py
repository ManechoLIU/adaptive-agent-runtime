from __future__ import annotations

import importlib.util
import fcntl
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
