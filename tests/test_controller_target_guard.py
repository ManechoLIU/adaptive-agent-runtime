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
        })

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
