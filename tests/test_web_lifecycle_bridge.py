from __future__ import annotations

import atexit
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


_TEST_LIFECYCLE_STATE = tempfile.TemporaryDirectory(prefix="adaptive-runtime-lifecycle-test-")
atexit.register(_TEST_LIFECYCLE_STATE.cleanup)
os.environ["AD_LIFECYCLE_STATE_DIR"] = _TEST_LIFECYCLE_STATE.name

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "scripts" / "web_lifecycle_bridge.py"
GUARD_COMMAND = (
    f"{sys.executable} scripts/control_event_guard.py event.json "
    "--ledger TASK_LEDGER.md --repo . --controller-session controller-1"
)
_SPEC = importlib.util.spec_from_file_location("web_lifecycle_bridge_under_test", BRIDGE)
assert _SPEC and _SPEC.loader
web_bridge = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(web_bridge)




def _provision_verified_current_web_target(
    registry: Path, *, controller_id: str = "controller-1",
    web_session_id: str = "web-session-1", target_generation: int = 1,
    ownership_generation: int | None = None,
) -> None:
    """Test fixture: make an explicitly current, Host-attested Web execution target."""
    payload = json.loads(registry.read_text(encoding="utf-8"))
    sessions = payload.setdefault("__controller_sessions__", {})
    controller_sessions = sessions.setdefault(controller_id, {})
    web_sessions = controller_sessions.get("web") or []
    if isinstance(web_sessions, str):
        web_sessions = [web_sessions]
    if web_session_id not in web_sessions:
        web_sessions.append(web_session_id)
    controller_sessions["web"] = web_sessions
    targets = payload.setdefault("__controller_targets__", {})
    targets.setdefault(controller_id, {})["web"] = {
        "status": "active",
        "session_id": web_session_id,
        "generation": target_generation,
        "provenance": "host_attested_same_controller_recovery",
        "binding_mode": "resume_only",
        "identity_proof": "host_attested_origin",
    }
    payload.setdefault("__controller_execution_ownership__", {})[controller_id] = {
        "active_host": "web",
        "execution_target_session_id": web_session_id,
        "generation": ownership_generation if ownership_generation is not None else target_generation,
        "provenance": "web_entry",
    }
    registry.write_text(json.dumps(payload), encoding="utf-8")



def _provision_manual_current_web_target(
    registry: Path, *, controller_id: str = "controller-1",
    web_session_id: str = "web-session-1", generation: int = 1,
) -> None:
    payload = json.loads(registry.read_text(encoding="utf-8"))
    sessions = payload.setdefault("__controller_sessions__", {})
    controller_sessions = sessions.setdefault(controller_id, {})
    aliases = controller_sessions.get("web") or []
    if isinstance(aliases, str):
        aliases = [aliases]
    if web_session_id not in aliases:
        aliases.append(web_session_id)
    controller_sessions["web"] = aliases
    payload.setdefault("__controller_targets__", {}).setdefault(controller_id, {})["web"] = {
        "status": "active", "session_id": web_session_id, "generation": generation,
        "provenance": "manual_user_authorized", "binding_mode": "temporary",
        "host_attested": False,
    }
    payload.setdefault("__controller_execution_ownership__", {})[controller_id] = {
        "active_host": "web", "execution_target_session_id": web_session_id,
        "generation": generation, "provenance": "manual_user_authorized",
    }
    registry.write_text(json.dumps(payload), encoding="utf-8")

class WebLifecycleBridgeTests(unittest.TestCase):
    def test_dispatch_event_result_treats_decision_block_as_logical_yield_rejection(self) -> None:
        from unittest.mock import patch
        completed = subprocess.CompletedProcess(
            args=["lifecycle_hook.py"], returncode=0,
            stdout=json.dumps({"decision": "block", "reason": "Yield Gate rejected"}) + "\n",
            stderr="",
        )
        with patch.object(web_bridge.subprocess, "run", return_value=completed):
            result = web_bridge.dispatch_event_result({"hook_event_name": "Stop"})
        self.assertEqual(result["transport_returncode"], 0)
        self.assertTrue(result["yield_blocked"])
        self.assertEqual(result["lifecycle_output"]["decision"], "block")
        self.assertIn("Yield Gate", result["reason"])

    def test_blocked_web_lifecycle_dispatch_arms_continuation_before_returning_blocked(self) -> None:
        from unittest.mock import patch
        lifecycle = {
            "pending_control_event": True, "requires_user": False,
            "controller_host": "web", "wake_generation": 9,
            "triggers": ["YIELD_GATE_REJECTED"],
        }
        outcome = {
            "transport_returncode": 0, "yield_blocked": True,
            "lifecycle_output": {"decision": "block", "reason": "Yield Gate rejected"},
            "reason": "Yield Gate rejected",
        }
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"; repo.mkdir()
            registry = Path(tmp) / "controllers.json"; registry.write_text("{}", encoding="utf-8")
            with patch.object(web_bridge, "_load_lifecycle_state", return_value=lifecycle), patch.object(
                web_bridge, "dispatch_pending_lifecycle_wake",
                return_value={"result": "CONFIRMED", "pending_control_event": True},
            ) as wake, patch.object(
                web_bridge, "ensure_continuation_supervisor", return_value=True
            ) as ensure:
                code = web_bridge.complete_web_lifecycle_dispatch(
                    dispatch_outcome=outcome, session_id="controller-1", repo=repo, registry=registry,
                    codex="codex", receipt_prefix="yield-test",
                )
        self.assertEqual(code, 78)
        wake.assert_called_once()
        ensure.assert_called_once()
        self.assertEqual(ensure.call_args.kwargs["session_id"], "controller-1")
        self.assertTrue(ensure.call_args.kwargs["lifecycle_state"]["pending_control_event"])

    def run_bridge(self, *args: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["/usr/bin/python3", str(BRIDGE), *args],
            input=stdin,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_periodic_reconcile_arms_existing_supervisor_for_desktop_pending_continuation(self) -> None:
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = tmp_path / "controllers.json"
            registry.write_text(json.dumps({"controller-1": str(repo)}) + "\n", encoding="utf-8")
            continuation = {
                "should_continue": True,
                "controller_host": "desktop_codex",
                "lifecycle_state": {
                    "pending_control_event": True,
                    "requires_user": False,
                    "controller_host": "desktop_codex",
                    "wake_generation": 7,
                },
            }
            with patch.object(web_bridge, "_registered_controller_for_common_dir", return_value="controller-1"), \
                 patch("scripts.web_agent_execution._load_dispatch_state", return_value={"dispatches": {}}), \
                 patch("scripts.assignment_runtime.load_runtime_state", return_value={"leases": {}}), \
                 patch.object(web_bridge, "controller_continuation_projection", return_value=continuation), \
                 patch.object(web_bridge, "ensure_continuation_supervisor", return_value=True) as ensure:
                result = web_bridge.reconcile_managed_web_assignments(
                    repo=repo, controller_id="controller-1", registry=registry, event_paths=[]
                )

            self.assertTrue(result["controller_continuation"]["supervisor_armed"])
            ensure.assert_called_once()

    def test_translate_selfalone_shell_receipt_into_post_tool_event(self) -> None:
        receipt = {
            "receiptId": "receipt-1",
            "childTool": "shell_command",
            "state": "succeeded",
            "rootLabel": "~/Documents/SelfAlone",
            "targetLabel": "git status --short",
            "detail": (
                "命令：git status --short · 工作目录：~/Documents/SelfAlone"
                "\n\n命令输出：\n"
            ),
        }

        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp) / "controllers.json"
            repo = Path.home() / "Documents" / "SelfAlone"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {"controller-1": {"web": ["web-session-1"]}},
            }), encoding="utf-8")
            _provision_verified_current_web_target(registry, web_session_id="web-session-1")
            result = self.run_bridge(
                "translate-receipt", "--session-id", "controller-1", "--repo", str(repo),
                "--registry", str(registry), "--web-session-id", "web-session-1",
                stdin=json.dumps(receipt),
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        event = json.loads(result.stdout)
        self.assertEqual(event["hook_event_name"], "PostToolUse")
        self.assertEqual(event["session_id"], "controller-1")
        self.assertEqual(event["cwd"], str(Path.home() / "Documents" / "SelfAlone"))
        self.assertEqual(event["tool_input"]["command"], "git status --short")
        self.assertIn("命令输出", event["tool_response"]["output"])

    def test_translate_ignores_receipt_from_another_root(self) -> None:
        receipt = {
            "receiptId": "receipt-2",
            "childTool": "shell_command",
            "state": "succeeded",
            "rootLabel": "~/Documents/OtherProject",
            "targetLabel": "git status --short",
            "detail": "命令：git status --short",
        }

        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp) / "controllers.json"
            repo = Path.home() / "Documents" / "SelfAlone"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {"controller-1": {"web": ["web-session-1"]}},
            }), encoding="utf-8")
            _provision_verified_current_web_target(registry, web_session_id="web-session-1")
            result = self.run_bridge(
                "translate-receipt", "--session-id", "controller-1", "--repo", str(repo),
                "--registry", str(registry), "--web-session-id", "web-session-1",
                stdin=json.dumps(receipt),
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_post_shell_refuses_registered_repo_without_verified_web_controller_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = tmp_path / "controllers.json"
            registry.write_text(
                json.dumps({"controller-1": str(repo)}) + "\n", encoding="utf-8"
            )
            capture = tmp_path / "capture.json"

            result = self.run_bridge(
                "post-shell",
                "--cwd", str(repo),
                "--command", "git status --short",
                "--exit-code", "0",
                "--registry", str(registry),
                "--capture-event", str(capture),
            )

            self.assertEqual(result.returncode, 78)
            self.assertIn("verified Web Controller Session identity", result.stderr)
            self.assertFalse(capture.exists())

    def test_post_shell_resolves_the_single_registered_controller_for_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = tmp_path / "controllers.json"
            registry.write_text(
                json.dumps({"controller-1": str(repo), "__controller_sessions__": {"controller-1": {"web": ["web-session-1"]}}}) + "\n", encoding="utf-8"
            )
            _provision_verified_current_web_target(registry, web_session_id="web-session-1")
            capture = tmp_path / "capture.json"

            result = self.run_bridge(
                "post-shell",
                "--cwd",
                str(repo),
                "--command",
                "git status --short",
                "--exit-code",
                "0",
                "--registry",
                str(registry),
                "--web-session-id",
                "web-session-1",
                "--capture-event",
                str(capture),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            event = json.loads(capture.read_text(encoding="utf-8"))
            self.assertEqual(event["session_id"], "controller-1")
            self.assertEqual(event["controller_id"], "controller-1")
            self.assertEqual(event["controller_session_id"], "controller-1")
            self.assertEqual(event["web_session_id"], "web-session-1")
            self.assertEqual(event["event_source"], "web")
            self.assertEqual(event["execution_host"], "web")
            self.assertEqual(event["cwd"], str(repo.resolve()))
            self.assertEqual(event["tool_response"]["exit_code"], 0)

    def test_post_shell_refuses_web_session_bound_to_another_controller(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            other = root / "other"
            other.mkdir()
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo),
                "controller-2": str(other),
                "__controller_sessions__": {
                    "controller-1": {"web": ["web-owner"]},
                    "controller-2": {"web": ["web-other"]},
                },
            }), encoding="utf-8")

            result = self.run_bridge(
                "post-shell", "--cwd", str(repo), "--command", "git status --short",
                "--exit-code", "0", "--registry", str(registry),
                "--web-session-id", "web-other",
            )

            self.assertEqual(result.returncode, 78)
            self.assertIn("verified Web Controller Session identity", result.stderr)

    def test_web_session_cannot_be_owned_by_two_controllers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"; repo.mkdir()
            other = root / "other"; other.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo),
                "controller-2": str(other),
                "__controller_sessions__": {
                    "controller-1": {"web": ["web-shared"]},
                    "controller-2": {"web": ["web-shared"]},
                },
            }), encoding="utf-8")

            result = self.run_bridge(
                "post-shell", "--cwd", str(repo), "--command", "git status --short",
                "--exit-code", "0", "--registry", str(registry), "--web-session-id", "web-shared",
            )

            self.assertEqual(result.returncode, 78)
            self.assertIn("verified Web Controller Session identity", result.stderr)

    def test_session_start_verified_target_does_not_rotate_manual_resume_lease(self) -> None:
        from contextlib import redirect_stdout
        from io import StringIO
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {"controller-1": {"web": ["web-current"]}},
                "__controller_targets__": {
                    "controller-1": {
                        "web": {"status": "active", "session_id": "web-current", "generation": 4}
                    }
                },
                "__controller_execution_ownership__": {
                    "controller-1": {
                        "active_host": "web",
                        "execution_target_session_id": "web-current",
                        "generation": 7,
                    }
                },
            }), encoding="utf-8")
            lease = root / "manual-web-leases.json"
            lease.write_text(json.dumps({
                "schema_version": 1,
                "leases": {
                    "controller-1": {
                        "repo": str(repo.resolve()),
                        "controller_id": "controller-1",
                        "web_session_id": "web-old",
                        "authorized_at_unix": 100,
                        "expires_at_unix": 4102444800,
                        "provenance": "manual_user_authorized",
                        "mode": "resume_only",
                    }
                },
            }), encoding="utf-8")
            output = StringIO()
            with patch.object(web_bridge, "DEFAULT_MANUAL_WEB_LEASES", lease), patch.object(
                web_bridge, "web_session_restore_payload", return_value={
                    "session_binding_state": {"verification": "VERIFIED"},
                    "controller_actions_allowed": True,
                }
            ), redirect_stdout(output):
                rc = web_bridge.main([
                    "session-start",
                    "--repo", str(repo),
                    "--registry", str(registry),
                    "--web-session-id", "web-current",
                ])

            self.assertEqual(rc, 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["session_recovery_result"]["result"], "ALREADY_VERIFIED")
            self.assertFalse(payload["session_recovery_result"]["resume_lease_rotated"])
            record = json.loads(lease.read_text(encoding="utf-8"))["leases"]["controller-1"]
            self.assertEqual(record["web_session_id"], "web-old")
            saved = json.loads(registry.read_text(encoding="utf-8"))
            self.assertEqual(saved["__controller_execution_ownership__"]["controller-1"]["generation"], 7)
            self.assertEqual(saved["__controller_targets__"]["controller-1"]["web"]["generation"], 4)

    def test_session_start_rejects_legacy_browser_attested_current_target(self) -> None:
        from contextlib import redirect_stderr
        from io import StringIO

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = root / "controllers.json"
            original = {
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {
                    "controller-1": {"web": ["web-current"]}
                },
                "__controller_targets__": {"controller-1": {"web": {
                    "status": "active",
                    "session_id": "web-current",
                    "generation": 2,
                    "provenance": "host_attested_same_controller_recovery",
                    "binding_mode": "resume_only",
                    "host_identity_receipt_sha256": "a" * 64,
                }}},
            }
            registry.write_text(json.dumps(original), encoding="utf-8")
            stderr = StringIO()

            with redirect_stderr(stderr):
                code = web_bridge.main([
                    "session-start",
                    "--repo", str(repo),
                    "--registry", str(registry),
                    "--web-session-id", "web-current",
                ])

            self.assertEqual(code, 78)
            self.assertIn("verified Web Controller Session identity required", stderr.getvalue())
            self.assertEqual(json.loads(registry.read_text(encoding="utf-8")), original)

    def test_session_start_without_host_session_id_reports_existing_controller_not_new_controller(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            (repo / "AGENTS.md").write_text("rules" + chr(10), encoding="utf-8")
            registry = root / "controllers.json"
            registry.write_text(
                json.dumps({"controller-1": str(repo.resolve())}),
                encoding="utf-8",
            )
            result = self.run_bridge(
                "session-start",
                "--repo", str(repo),
                "--registry", str(registry),
            )

            self.assertEqual(result.returncode, 78)
            diagnostic = json.loads(result.stderr.strip().splitlines()[-1])
            self.assertEqual(
                diagnostic["project_controller_state"]["project_controller"],
                "EXISTING",
            )
            self.assertEqual(
                diagnostic["project_controller_state"]["controller_id"],
                "controller-1",
            )
            self.assertEqual(
                diagnostic["session_binding_state"]["verification"],
                "UNVERIFIED",
            )
            self.assertEqual(
                diagnostic["session_binding_state"]["reason"],
                "HOST_SESSION_ID_UNAVAILABLE",
            )
            self.assertEqual(
                diagnostic["session_binding_state"]["recovery"],
                "SAME_CONTROLLER_SESSION_RECOVERY_REQUIRED",
            )
            self.assertFalse(diagnostic["controller_actions_allowed"])

    def test_session_start_host_attested_recovery_restores_pending_control_loop_same_controller(self) -> None:
        from contextlib import redirect_stdout, redirect_stderr
        from io import StringIO
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            (repo / "AGENTS.md").write_text("rules" + chr(10), encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "AGENTS.md"], check=True)
            subprocess.run(
                [
                    "git", "-C", str(repo),
                    "-c", "user.name=Test", "-c", "user.email=test@example.com",
                    "commit", "-qm", "init",
                ],
                check=True,
            )
            registry = root / "controllers.json"
            registry.write_text(
                json.dumps({"controller-1": str(repo.resolve())}),
                encoding="utf-8",
            )
            _provision_manual_current_web_target(
                registry, web_session_id="web-new", generation=1
            )
            pending_state = {
                "pending_control_event": True,
                "requires_user": False,
                "triggers": ["RUNNABLE:MINI-READY", "reviewer_terminal:WEB-1"],
                "next_action": "consume reviewer and recompute project",
                "wake_generation": 8,
                "snapshot": {
                    "runnable_ids": ["MINI-READY"],
                    "candidate_revisions": ["candidate-web"],
                },
            }
            host_receipt = {
                "host": "web",
                "session_id": "web-new",
                "attested": True,
            }
            out, err = StringIO(), StringIO()

            def verifier(**kwargs: object) -> bool:
                return (
                    kwargs.get("controller_id") == "controller-1"
                    and kwargs.get("expected_target_session_id") == "web-new"
                    and kwargs.get("host_execution_receipt") == host_receipt
                )

            with patch.object(
                web_bridge,
                "_registered_peer_attestation_verifier",
                return_value=verifier,
            ), patch.object(
                web_bridge,
                "_load_lifecycle_state",
                return_value=pending_state,
            ), redirect_stdout(out), redirect_stderr(err):
                code = web_bridge.main([
                    "session-start",
                    "--repo", str(repo),
                    "--registry", str(registry),
                    "--web-session-id", "web-new",
                    "--host-identity-receipt-json", json.dumps(host_receipt),
                ])

            self.assertEqual(code, 0, err.getvalue())
            payload = json.loads(out.getvalue())
            self.assertEqual(payload["controller_id"], "controller-1")
            self.assertEqual(payload["web_session_id"], "web-new")
            self.assertEqual(
                payload["session_binding_state"]["verification"],
                "VERIFIED",
            )
            self.assertTrue(payload["controller_actions_allowed"])
            self.assertTrue(payload["resume_control_loop_required"])
            lifecycle = payload["controller_lifecycle"]
            self.assertTrue(lifecycle["pending_control_event"])
            self.assertIn("RUNNABLE:MINI-READY", lifecycle["triggers"])
            self.assertIn("MINI-READY", lifecycle["runnable_ids"])
            self.assertEqual(
                payload["session_recovery_result"]["controller_id"],
                "controller-1",
            )

    def test_registered_web_verifier_timeout_covers_product_host_request_budget(self) -> None:
        self.assertGreaterEqual(
            web_bridge.PEER_ATTESTATION_VERIFIER_TIMEOUT_SECONDS,
            15,
        )

    def test_registered_web_verifier_loads_pinned_external_runtime_host_cli(self) -> None:
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = root / "runtime-verifier"
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "import json,sys\n"
                "request=json.loads(sys.stdin.read())\n"
                "print(json.dumps({'ok': True, 'operation': 'attest_and_verify', "
                "'challenge_id': 'hc_runtime', 'host_receipt_id': 'hr_runtime', "
                "'verified_target': {'provenance': 'runtime_host_verifier_v1', "
                "'conversation_id': request['conversation_id'], "
                "'browser_target_id': 'target-runtime', 'top_frame_id': 'top-runtime', "
                "'loader_id': 'loader-runtime', 'secure_origin': 'https://chatgpt.com', "
                "'target_generation': request['target_generation'], "
                "'ownership_generation': request['ownership_generation']}}))\n",
                encoding="utf-8",
            )
            executable.chmod(0o700)
            digest = __import__("hashlib").sha256(executable.read_bytes()).hexdigest()
            config = root / "host-verifiers.json"
            config.write_text(json.dumps({
                "schema_version": 1,
                "verifiers": {
                    "web": {
                        "protocol": "runtime_host_verifier_cli_v1",
                        "executable": str(executable),
                        "sha256": digest,
                        "bundle_sha256": {str(executable): digest},
                    }
                },
            }), encoding="utf-8")
            config.chmod(0o600)
            with patch.object(
                web_bridge, "DEFAULT_PEER_ATTESTATION_VERIFIER_CONFIG", config,
                create=True,
            ):
                verifier = web_bridge._registered_peer_attestation_verifier("web")
                self.assertTrue(callable(verifier))
                origin = verifier(
                    phase="pre_delivery",
                    controller_id="controller-1",
                    host="web",
                    expected_target_session_id="web-new",
                    expected_target_generation=4,
                    expected_target_mode="same_controller_session_recovery",
                    expected_ownership_generation=9,
                )
                self.assertEqual(origin, {
                    "origin_host": "chatgpt_web",
                    "origin_conversation_id": "web-new",
                    "origin_attested": True,
                    "call_receipt": "hr_runtime",
                })
                self.assertIs(
                    verifier(
                        controller_id="controller-1",
                        host="web",
                        expected_target_session_id="web-new",
                        expected_target_generation=4,
                        expected_target_mode="same_controller_session_recovery",
                        expected_ownership_generation=9,
                    ),
                    True,
                )

    def test_registered_web_verifier_classifies_frame_tree_timeout_as_transient(self) -> None:
        self.assertTrue(
            web_bridge._peer_host_error_is_transient("Page.getFrameTree timed out")
        )

    def test_registered_web_verifier_classifies_exact_target_unavailable_as_transient(self) -> None:
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = root / "runtime-verifier"
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "import json,sys\n"
                "print(json.dumps({'ok':False,'error_code':'RUNTIME_HOST_VERIFIER_FAILED',"
                "'error':'exact ChatGPT conversation target is unavailable'}))\n"
                "raise SystemExit(1)\n",
                encoding="utf-8",
            )
            executable.chmod(0o700)
            digest = __import__("hashlib").sha256(executable.read_bytes()).hexdigest()
            config = root / "host-verifiers.json"
            config.write_text(json.dumps({"schema_version":1,"verifiers":{"web":{
                "protocol":"runtime_host_verifier_cli_v1","executable":str(executable),
                "sha256":digest,"bundle_sha256":{str(executable):digest},
            }}}), encoding="utf-8")
            config.chmod(0o600)
            with patch.object(
                web_bridge, "DEFAULT_PEER_ATTESTATION_VERIFIER_CONFIG", config, create=True
            ):
                verifier = web_bridge._registered_peer_attestation_verifier("web")
                with self.assertRaises(web_bridge.PeerHostTransientUnavailable):
                    verifier(
                        phase="pre_delivery",
                        controller_id="controller-1",
                        host="web",
                        expected_target_session_id="web-current",
                        expected_target_generation=4,
                        expected_ownership_generation=8,
                    )

    def test_registered_web_verifier_classifies_transient_spa_route_as_retryable(self) -> None:
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = root / "runtime-verifier"
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "import json,sys\n"
                "print(json.dumps({'ok':False,'error_code':'RUNTIME_HOST_VERIFIER_FAILED',"
                "'error':'target has no stable ChatGPT conversation route'}))\n"
                "raise SystemExit(1)\n",
                encoding="utf-8",
            )
            executable.chmod(0o700)
            digest = __import__("hashlib").sha256(executable.read_bytes()).hexdigest()
            config = root / "host-verifiers.json"
            config.write_text(json.dumps({"schema_version":1,"verifiers":{"web":{
                "protocol":"runtime_host_verifier_cli_v1","executable":str(executable),
                "sha256":digest,"bundle_sha256":{str(executable):digest},
            }}}), encoding="utf-8")
            config.chmod(0o600)
            with patch.object(
                web_bridge, "DEFAULT_PEER_ATTESTATION_VERIFIER_CONFIG", config, create=True
            ):
                verifier = web_bridge._registered_peer_attestation_verifier("web")
                with self.assertRaises(web_bridge.PeerHostTransientUnavailable):
                    verifier(
                        phase="pre_delivery",
                        controller_id="controller-1",
                        host="web",
                        expected_target_session_id="web-current",
                        expected_target_generation=4,
                        expected_ownership_generation=8,
                    )

    def test_registered_web_verifier_keeps_unrecognized_runtime_failure_permanent(self) -> None:
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = root / "runtime-verifier"
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "import json,sys\n"
                "print(json.dumps({'ok':False,'error_code':'RUNTIME_HOST_VERIFIER_FAILED',"
                "'error':'verified target signature mismatch'}))\n"
                "raise SystemExit(1)\n",
                encoding="utf-8",
            )
            executable.chmod(0o700)
            digest = __import__("hashlib").sha256(executable.read_bytes()).hexdigest()
            config = root / "host-verifiers.json"
            config.write_text(json.dumps({"schema_version":1,"verifiers":{"web":{
                "protocol":"runtime_host_verifier_cli_v1","executable":str(executable),
                "sha256":digest,"bundle_sha256":{str(executable):digest},
            }}}), encoding="utf-8")
            config.chmod(0o600)
            with patch.object(
                web_bridge, "DEFAULT_PEER_ATTESTATION_VERIFIER_CONFIG", config, create=True
            ):
                verifier = web_bridge._registered_peer_attestation_verifier("web")
                with self.assertRaisesRegex(PermissionError, "rejected machine request"):
                    verifier(
                        phase="pre_delivery",
                        controller_id="controller-1",
                        host="web",
                        expected_target_session_id="web-current",
                        expected_target_generation=4,
                        expected_ownership_generation=8,
                    )

    def test_registered_web_verifier_exposes_pinned_host_submit_adapter(self) -> None:
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = root / "runtime-verifier"
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "import json,sys\n"
                "request=json.loads(sys.stdin.read())\n"
                "if request['operation']=='attest_and_verify':\n"
                " print(json.dumps({'ok': True, 'operation':'attest_and_verify', 'host_receipt_id':'hr_submit', 'verified_target': {'provenance':'runtime_host_verifier_v1','conversation_id':request['conversation_id'],'target_generation':request['target_generation'],'ownership_generation':request['ownership_generation']}}))\n"
                "else:\n"
                " print(json.dumps({'ok': True, 'operation':'submit_reentry', 'reentry_receipt': {'provenance':'browser_host_reentry_receipt_v1','conversation_id':request['conversation_id'],'target_generation':request['expected_target_generation'],'ownership_generation':request['expected_ownership_generation'],'dispatch_attempted':True,'submit_confirmed':True,'retryable':False,'auto_retry_allowed':False,'result_class':'SUBMIT_CONFIRMED','status':'submit_confirmed','receipt_id':'wr_submit'}}))\n",
                encoding="utf-8",
            )
            executable.chmod(0o700)
            digest = __import__("hashlib").sha256(executable.read_bytes()).hexdigest()
            config = root / "host-verifiers.json"
            config.write_text(json.dumps({"schema_version":1,"verifiers":{"web":{
                "protocol":"runtime_host_verifier_cli_v1","executable":str(executable),"sha256":digest,
                "bundle_sha256":{str(executable):digest},
            }}}), encoding="utf-8")
            config.chmod(0o600)
            with patch.object(web_bridge, "DEFAULT_PEER_ATTESTATION_VERIFIER_CONFIG", config, create=True):
                verifier = web_bridge._registered_peer_attestation_verifier("web")
                origin = verifier(
                    phase="pre_delivery", controller_id="controller-1", host="web",
                    expected_target_session_id="web-new", expected_target_generation=4,
                    expected_ownership_generation=9,
                )
                adapter = getattr(verifier, "submit_reentry")
                attempt = adapter(
                    controller_id="controller-1", execution_target_session_id="web-new",
                    target_generation=4, ownership_generation=9, target_mode="explicit_current",
                    lifecycle_state={"wake_generation": 7, "next_action": "continue"},
                    host_origin_attestation=origin,
                )
                self.assertEqual(attempt["result"], "CONFIRMED")
                self.assertEqual(attempt["execution_target_session_id"], "web-new")
                self.assertEqual(attempt["target_generation"], 4)
                self.assertEqual(attempt["ownership_generation"], 9)
                self.assertTrue(attempt["host_attested"])
                self.assertTrue(attempt["strong_web_identity_established"])
                self.assertEqual(attempt["host_execution_receipt"]["call_receipt"], "hr_submit")
                self.assertEqual(attempt["host_execution_receipt"]["reentry_receipt"]["receipt_id"], "wr_submit")

    def test_registered_web_verifier_rechecks_bundle_before_each_execution(self) -> None:
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = root / "runtime-verifier"
            executable.write_text(
                "#!/usr/bin/env python3\nimport json,sys\nr=json.loads(sys.stdin.read())\nprint(json.dumps({'ok':True,'operation':'attest_and_verify','host_receipt_id':'hr','verified_target':{'provenance':'runtime_host_verifier_v1','conversation_id':r['conversation_id'],'target_generation':r['target_generation'],'ownership_generation':r['ownership_generation']}}))\n",
                encoding="utf-8",
            )
            executable.chmod(0o700)
            dependency = root / "integration.mjs"
            dependency.write_text("export const value = 1;\n", encoding="utf-8")
            digest = __import__("hashlib").sha256(executable.read_bytes()).hexdigest()
            dep_digest = __import__("hashlib").sha256(dependency.read_bytes()).hexdigest()
            config = root / "host-verifiers.json"
            config.write_text(json.dumps({"schema_version":1,"verifiers":{"web":{
                "protocol":"runtime_host_verifier_cli_v1","executable":str(executable),"sha256":digest,
                "bundle_sha256":{str(executable):digest,str(dependency):dep_digest},
            }}}), encoding="utf-8")
            config.chmod(0o600)
            with patch.object(web_bridge, "DEFAULT_PEER_ATTESTATION_VERIFIER_CONFIG", config, create=True):
                verifier = web_bridge._registered_peer_attestation_verifier("web")
                self.assertTrue(callable(verifier))
                dependency.write_text("export const value = 2;\n", encoding="utf-8")
                with self.assertRaisesRegex(PermissionError, "bundle.*hash"):
                    verifier(
                        phase="pre_delivery", controller_id="controller-1", host="web",
                        expected_target_session_id="web-new", expected_target_generation=4,
                        expected_ownership_generation=9,
                    )

    def test_registered_web_verifier_rejects_mutated_bundle_member(self) -> None:
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = root / "runtime-verifier"
            executable.write_text("#!/bin/sh\nprintf '{\"ok\":false}\n'\n", encoding="utf-8")
            executable.chmod(0o700)
            dependency = root / "integration.mjs"
            dependency.write_text("export const value = 1;\n", encoding="utf-8")
            digest = __import__("hashlib").sha256(executable.read_bytes()).hexdigest()
            dependency_digest = __import__("hashlib").sha256(dependency.read_bytes()).hexdigest()
            config = root / "host-verifiers.json"
            config.write_text(json.dumps({
                "schema_version": 1,
                "verifiers": {"web": {
                    "protocol": "runtime_host_verifier_cli_v1",
                    "executable": str(executable),
                    "sha256": digest,
                    "bundle_sha256": {
                        str(executable): digest,
                        str(dependency): dependency_digest,
                    },
                }},
            }), encoding="utf-8")
            config.chmod(0o600)
            dependency.write_text("export const value = 2;\n", encoding="utf-8")
            with patch.object(web_bridge, "DEFAULT_PEER_ATTESTATION_VERIFIER_CONFIG", config, create=True):
                verifier = web_bridge._registered_peer_attestation_verifier("web")
                self.assertTrue(callable(verifier))
                with self.assertRaisesRegex(PermissionError, "bundle.*hash"):
                    verifier(
                        phase="pre_delivery", controller_id="controller-1", host="web",
                        expected_target_session_id="web-new", expected_target_generation=4,
                        expected_ownership_generation=9,
                    )

    def test_malformed_registered_web_verifier_config_fails_closed_without_manual_fallback(self) -> None:
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = root / "runtime-verifier"
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o700)
            config = root / "host-verifiers.json"
            config.write_text(json.dumps({
                "schema_version": 1,
                "verifiers": {
                    "web": {
                        "protocol": "runtime_host_verifier_cli_v1",
                        "executable": str(executable),
                        "sha256": "0" * 64,
                        "bundle_sha256": {str(executable): "0" * 64},
                    }
                },
            }), encoding="utf-8")
            config.chmod(0o600)
            with patch.object(
                web_bridge, "DEFAULT_PEER_ATTESTATION_VERIFIER_CONFIG", config,
                create=True,
            ):
                verifier = web_bridge._registered_peer_attestation_verifier("web")
                self.assertTrue(callable(verifier))
                with self.assertRaisesRegex(PermissionError, "hash|registered.*verifier"):
                    verifier(
                        phase="pre_delivery",
                        controller_id="controller-1",
                        host="web",
                        expected_target_session_id="web-new",
                        expected_target_generation=4,
                        expected_ownership_generation=9,
                    )

    def test_production_bridge_has_no_trusted_web_attestation_verifier(self) -> None:
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing-host-verifiers.json"
            with patch.object(web_bridge, "DEFAULT_PEER_ATTESTATION_VERIFIER_CONFIG", missing, create=True):
                self.assertIsNone(web_bridge._registered_peer_attestation_verifier("web"))
                self.assertIsNone(web_bridge._registered_peer_attestation_verifier("desktop_codex"))

    def test_browser_tab_receipt_cannot_recover_an_unverified_web_session(self) -> None:
        from unittest.mock import patch
        from scripts import web_reentry_adapter

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = root / "controllers.json"
            original = {
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {
                    "controller-1": {
                        "desktop_codex": ["desktop-current"],
                        "web": ["web-old"],
                    }
                },
                "__controller_targets__": {
                    "controller-1": {
                        "desktop_codex": {
                            "status": "active",
                            "session_id": "desktop-current",
                            "generation": 4,
                        },
                        "web": {
                            "status": "active",
                            "session_id": "web-old",
                            "generation": 1,
                        },
                    }
                },
                "__controller_execution_ownership__": {
                    "controller-1": {
                        "active_host": "desktop_codex",
                        "execution_target_session_id": "desktop-current",
                        "generation": 7,
                    }
                },
            }
            registry.write_text(json.dumps(original), encoding="utf-8")
            receipt = {
                "host": "web",
                "web_session_id": "unverified-web-session",
                "source": "ai_bridge_browser",
                "tab_id": "tab-unverified",
                "url": "https://chatgpt.com/c/unverified-web-session",
            }

            with patch.object(
                web_reentry_adapter,
                "_default_browser_call",
                return_value={
                    "tabs": [{
                        "tab_id": "tab-unverified",
                        "url": "https://chatgpt.com/c/unverified-web-session",
                    }]
                },
            ), patch.object(
                web_bridge,
                "DEFAULT_PEER_ATTESTATION_VERIFIER_CONFIG",
                root / "missing-host-verifiers.json",
                create=True,
            ):
                result = web_bridge.recover_same_controller_web_session(
                    repo=repo,
                    web_session_id="unverified-web-session",
                    registry_path=registry,
                    host_identity_receipt=receipt,
                )

            self.assertEqual(result["result"], "DEFERRED")
            self.assertEqual(result["reason"], "HOST_IDENTITY_UNAVAILABLE")
            self.assertEqual(json.loads(registry.read_text(encoding="utf-8")), original)

    def test_same_controller_web_recovery_rebinds_trusted_new_session_without_new_controller(self) -> None:
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {
                    "controller-1": {"web": ["web-old"]}
                },
                "__controller_targets__": {
                    "controller-1": {
                        "web": {
                            "status": "active",
                            "session_id": "web-old",
                            "generation": 3,
                        }
                    }
                },
            }), encoding="utf-8")
            host_receipt = {
                "host": "web",
                "session_id": "web-new",
                "attested": True,
            }

            def verifier(**kwargs: object) -> bool:
                return (
                    kwargs.get("controller_id") == "controller-1"
                    and kwargs.get("expected_target_session_id") == "web-new"
                    and kwargs.get("host_execution_receipt") == host_receipt
                )

            with patch.object(
                web_bridge,
                "_registered_peer_attestation_verifier",
                return_value=verifier,
            ):
                recovered = web_bridge.recover_same_controller_web_session(
                    repo=repo,
                    web_session_id="web-new",
                    registry_path=registry,
                    host_identity_receipt=host_receipt,
                )

            self.assertEqual(recovered["result"], "RECOVERED")
            self.assertEqual(recovered["state"], "VERIFIED")
            self.assertEqual(recovered["identity"]["identity_state"], "VERIFIED")
            self.assertEqual(recovered["controller_id"], "controller-1")
            self.assertEqual(recovered["target_generation"], 4)
            self.assertEqual(recovered["active_host"], "web")
            self.assertEqual(recovered["ownership_generation"], 1)
            self.assertEqual(
                recovered["identity"]["session_binding_state"]["provenance"],
                "host_attested_same_controller_recovery",
            )
            self.assertEqual(
                recovered["identity"]["session_binding_state"]["binding_mode"],
                "resume_only",
            )
            self.assertRegex(
                recovered["identity"]["session_binding_state"][
                    "host_identity_receipt_sha256"
                ],
                r"^[0-9a-f]{64}$",
            )
            saved = json.loads(registry.read_text(encoding="utf-8"))
            self.assertEqual(
                saved["__controller_targets__"]["controller-1"]["web"].get(
                    "identity_proof"
                ),
                "host_attested_origin",
            )
            controllers = [
                key for key, value in saved.items()
                if isinstance(key, str)
                and not key.startswith("__")
                and isinstance(value, str)
            ]
            self.assertEqual(controllers, ["controller-1"])
            self.assertEqual(
                saved["__controller_targets__"]["controller-1"]["web"]["session_id"],
                "web-new",
            )
            self.assertEqual(saved["__controller_execution_ownership__"]["controller-1"], {
                "active_host": "web",
                "execution_target_session_id": "web-new",
                "generation": 1,
                "provenance": "web_entry",
            })
            old_identity = web_bridge.target_guard.controller_identity_projection(
                repo=repo,
                host="web",
                source_session_id="web-old",
                registry_path=registry,
            )
            self.assertEqual(
                old_identity["session_binding_state"]["verification"],
                "STALE",
            )
            self.assertEqual(
                old_identity["project_controller_state"]["controller_id"],
                "controller-1",
            )

    def test_same_controller_web_recovery_cannot_replace_different_host_attested_current_target(self) -> None:
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"; repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = root / "controllers.json"
            original = {
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {
                    "controller-1": {"web": ["web-strong", "web-old"]}
                },
                "__controller_targets__": {
                    "controller-1": {"web": {
                        "status": "active",
                        "session_id": "web-strong",
                        "generation": 4,
                        "provenance": "host_attested_same_controller_recovery",
                        "binding_mode": "resume_only",
                        "identity_proof": "host_attested_origin",
                    }}
                },
                "__controller_execution_ownership__": {
                    "controller-1": {
                        "active_host": "web",
                        "execution_target_session_id": "web-strong",
                        "generation": 4,
                        "provenance": "web_entry",
                    }
                },
            }
            registry.write_text(json.dumps(original), encoding="utf-8")
            verifier_calls = []
            def verifier(**kwargs: object) -> bool:
                verifier_calls.append(kwargs)
                return True
            with patch.object(
                web_bridge, "_registered_peer_attestation_verifier", return_value=verifier
            ):
                result = web_bridge.recover_same_controller_web_session(
                    repo=repo,
                    web_session_id="web-old",
                    registry_path=registry,
                    host_identity_receipt=None,
                )
            self.assertEqual(result["result"], "DEFERRED")
            self.assertEqual(result["reason"], "HOST_ATTESTED_CURRENT_TARGET_ALREADY_ACTIVE")
            self.assertEqual(verifier_calls, [])
            self.assertEqual(json.loads(registry.read_text()), original)

    def test_legacy_quarantined_target_requires_explicit_replacement(self) -> None:
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"; repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {
                    "controller-1": {"web": ["web-legacy", "web-new"]}
                },
                "__controller_targets__": {
                    "controller-1": {"web": {
                        "status": "active",
                        "session_id": "web-legacy",
                        "generation": 4,
                        "provenance": "host_attested_same_controller_recovery",
                        "binding_mode": "resume_only",
                        "host_identity_receipt_sha256": "a" * 64,
                    }}
                },
                "__controller_execution_ownership__": {
                    "controller-1": {
                        "active_host": "web",
                        "execution_target_session_id": "web-legacy",
                        "generation": 4,
                        "provenance": "web_entry",
                    }
                },
            }), encoding="utf-8")
            before = json.loads(registry.read_text())
            verifier_calls = []

            def verifier(**kwargs: object) -> bool:
                verifier_calls.append(kwargs)
                return True

            with patch.object(
                web_bridge, "_registered_peer_attestation_verifier", return_value=verifier
            ), self.assertRaisesRegex(PermissionError, "historical/unbound"):
                web_bridge.recover_same_controller_web_session(
                    repo=repo,
                    web_session_id="web-new",
                    registry_path=registry,
                    host_identity_receipt={"attested": True},
                )

            self.assertEqual(verifier_calls, [])
            self.assertEqual(json.loads(registry.read_text()), before)

    def test_legacy_quarantined_target_keeps_manual_replacement_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"; repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {
                    "controller-1": {"web": ["web-legacy", "web-manual"]}
                },
                "__controller_targets__": {
                    "controller-1": {"web": {
                        "status": "active", "session_id": "web-legacy", "generation": 4,
                        "provenance": "host_attested_same_controller_recovery",
                        "binding_mode": "resume_only",
                        "host_identity_receipt_sha256": "a" * 64,
                    }}
                },
                "__controller_execution_ownership__": {
                    "controller-1": {
                        "active_host": "web",
                        "execution_target_session_id": "web-legacy",
                        "generation": 4,
                        "provenance": "web_entry",
                    }
                },
            }), encoding="utf-8")
            result = web_bridge.replace_web_session(
                repo=repo,
                controller_id="controller-1",
                web_session_id="web-manual",
                expected_generation=4,
                expected_ownership_generation=4,
                registry_path=registry,
                lease_path=root / "leases.json",
            )
            saved = json.loads(registry.read_text())

        self.assertEqual(result["execution_target_session_id"], "web-manual")
        self.assertFalse(result["host_attested"])
        self.assertEqual(
            saved["__controller_targets__"]["controller-1"]["web"]["generation"], 5
        )

    def test_same_controller_web_recovery_rejects_attestation_if_target_generation_changes_before_lock(self) -> None:
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {
                    "controller-1": {"web": ["web-old", "web-concurrent"]}
                },
                "__controller_targets__": {
                    "controller-1": {
                        "web": {
                            "status": "active",
                            "session_id": "web-old",
                            "generation": 3,
                        }
                    }
                },
            }), encoding="utf-8")

            def verifier(**kwargs: object) -> bool:
                self.assertEqual(kwargs.get("expected_target_generation"), 3)
                concurrent = json.loads(registry.read_text(encoding="utf-8"))
                concurrent["__controller_targets__"]["controller-1"]["web"] = {
                    "status": "active",
                    "session_id": "web-concurrent",
                    "generation": 4,
                }
                registry.write_text(json.dumps(concurrent), encoding="utf-8")
                return True

            with patch.object(
                web_bridge, "_registered_peer_attestation_verifier", return_value=verifier
            ):
                with self.assertRaisesRegex(PermissionError, "generation.*changed|stale.*generation"):
                    web_bridge.recover_same_controller_web_session(
                        repo=repo, web_session_id="web-new", registry_path=registry,
                        host_identity_receipt={"attested": True},
                    )

            saved = json.loads(registry.read_text(encoding="utf-8"))
            self.assertEqual(
                saved["__controller_targets__"]["controller-1"]["web"],
                {"status": "active", "session_id": "web-concurrent", "generation": 4},
            )
            self.assertNotIn("web-new", saved["__controller_sessions__"]["controller-1"]["web"])

    def test_same_controller_web_recovery_without_host_verifier_preserves_existing_controller_and_state(self) -> None:
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = root / "controllers.json"
            registry.write_text(
                json.dumps({"controller-1": str(repo.resolve())}),
                encoding="utf-8",
            )
            lifecycle = root / "lifecycle.json"
            lifecycle_payload = {
                "pending_control_event": True,
                "triggers": ["RUNNABLE:MINI-1", "continuation_debt:review"],
                "requires_user": False,
            }
            lifecycle.write_text(
                json.dumps(lifecycle_payload, sort_keys=True),
                encoding="utf-8",
            )
            before_registry = registry.read_text(encoding="utf-8")
            before_lifecycle = lifecycle.read_text(encoding="utf-8")

            with patch.object(
                web_bridge,
                "_registered_peer_attestation_verifier",
                return_value=None,
            ):
                result = web_bridge.recover_same_controller_web_session(
                    repo=repo,
                    web_session_id="web-new",
                    registry_path=registry,
                    host_identity_receipt=None,
                )

            self.assertEqual(result["result"], "DEFERRED")
            self.assertEqual(result["state"], "CONTROLLER_IDENTITY_DEGRADED")
            self.assertEqual(result["reason"], "HOST_IDENTITY_UNAVAILABLE")
            self.assertTrue(result["safe_control_actions_allowed"])
            identity = result["identity"]
            self.assertEqual(
                identity["project_controller_state"]["project_controller"],
                "EXISTING",
            )
            self.assertEqual(
                identity["project_controller_state"]["controller_id"],
                "controller-1",
            )
            self.assertEqual(
                identity["session_binding_state"]["verification"],
                "UNVERIFIED",
            )
            self.assertEqual(
                identity["session_binding_state"]["recovery"],
                "SAME_CONTROLLER_SESSION_RECOVERY_REQUIRED",
            )
            self.assertFalse(identity["controller_actions_allowed"])
            self.assertFalse(identity["create_new_controller_allowed"])
            self.assertEqual(registry.read_text(encoding="utf-8"), before_registry)
            self.assertEqual(lifecycle.read_text(encoding="utf-8"), before_lifecycle)

    def test_same_controller_web_recovery_verifier_exception_degrades_without_revoking_controller(self) -> None:
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({"controller-1": str(repo.resolve())}), encoding="utf-8")
            before = registry.read_text(encoding="utf-8")

            def unavailable(**_kwargs: object) -> bool:
                raise RuntimeError("verifier temporarily unavailable")

            with patch.object(web_bridge, "_registered_peer_attestation_verifier", return_value=unavailable):
                result = web_bridge.recover_same_controller_web_session(
                    repo=repo, web_session_id="web-new", registry_path=registry,
                    host_identity_receipt={"host": "web", "session_id": "web-new"},
                )

            self.assertEqual(result["result"], "DEFERRED")
            self.assertEqual(result["state"], "CONTROLLER_IDENTITY_DEGRADED")
            self.assertEqual(result["reason"], "HOST_IDENTITY_VERIFIER_UNAVAILABLE")
            self.assertEqual(result["controller_id"], "controller-1")
            self.assertTrue(result["safe_control_actions_allowed"])
            self.assertFalse(result["identity"]["create_new_controller_allowed"])
            self.assertEqual(registry.read_text(encoding="utf-8"), before)

    def test_same_controller_web_recovery_is_idempotent_after_user_reconfirms_ownership(self) -> None:
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = root / "controllers.json"
            registry.write_text(
                json.dumps({"controller-1": str(repo.resolve())}),
                encoding="utf-8",
            )
            with patch.object(
                web_bridge,
                "_registered_peer_attestation_verifier",
                return_value=lambda **_kwargs: True,
            ):
                first = web_bridge.recover_same_controller_web_session(
                    repo=repo,
                    web_session_id="web-current",
                    registry_path=registry,
                    host_identity_receipt={"attested": True},
                )
                second = web_bridge.recover_same_controller_web_session(
                    repo=repo,
                    web_session_id="web-current",
                    registry_path=registry,
                    host_identity_receipt={"attested": True},
                )

            self.assertEqual(first["controller_id"], "controller-1")
            self.assertEqual(second["result"], "ALREADY_VERIFIED")
            self.assertEqual(second["controller_id"], "controller-1")
            saved = json.loads(registry.read_text(encoding="utf-8"))
            self.assertEqual(
                [key for key in saved if not key.startswith("__")],
                ["controller-1"],
            )

    def test_web_recovery_preserves_desktop_target_and_only_advances_web_generation(self) -> None:
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {
                    "controller-1": {
                        "desktop_codex": ["desktop-current"],
                        "web": ["web-old"],
                    }
                },
                "__controller_targets__": {
                    "controller-1": {
                        "desktop_codex": {
                            "status": "active",
                            "session_id": "desktop-current",
                            "generation": 7,
                        },
                        "web": {
                            "status": "active",
                            "session_id": "web-old",
                            "generation": 2,
                        },
                    }
                },
            }), encoding="utf-8")
            with patch.object(
                web_bridge,
                "_registered_peer_attestation_verifier",
                return_value=lambda **_kwargs: True,
            ):
                recovered = web_bridge.recover_same_controller_web_session(
                    repo=repo,
                    web_session_id="web-new",
                    registry_path=registry,
                    host_identity_receipt={"attested": True},
                )

            saved = json.loads(registry.read_text(encoding="utf-8"))
            self.assertEqual(recovered["target_generation"], 3)
            self.assertEqual(
                saved["__controller_targets__"]["controller-1"]["desktop_codex"],
                {
                    "status": "active",
                    "session_id": "desktop-current",
                    "generation": 7,
                },
            )
            web_target = saved["__controller_targets__"]["controller-1"]["web"]
            self.assertEqual(web_target["status"], "active")
            self.assertEqual(web_target["session_id"], "web-new")
            self.assertEqual(web_target["generation"], 3)
            self.assertEqual(
                web_target["provenance"],
                "host_attested_same_controller_recovery",
            )
            self.assertEqual(web_target["binding_mode"], "resume_only")
            self.assertEqual(
                recovered["identity"]["project_controller_state"]["controller_id"],
                "controller-1",
            )

    def test_same_controller_web_recovery_rotates_existing_resume_only_lease_to_new_verified_target(self) -> None:
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {"controller-1": {"web": ["web-old"]}},
                "__controller_targets__": {
                    "controller-1": {
                        "web": {"status": "active", "session_id": "web-old", "generation": 3}
                    }
                },
            }), encoding="utf-8")
            lease = root / "manual-web-leases.json"
            lease.write_text(json.dumps({
                "schema_version": 1,
                "leases": {
                    "controller-1": {
                        "repo": str(repo.resolve()),
                        "controller_id": "controller-1",
                        "web_session_id": "web-old",
                        "authorized_at_unix": 100,
                        "expires_at_unix": 4102444800,
                        "provenance": "manual_user_authorized",
                        "mode": "resume_only",
                    }
                },
            }), encoding="utf-8")

            with patch.object(web_bridge, "DEFAULT_MANUAL_WEB_LEASES", lease), patch.object(
                web_bridge,
                "_registered_peer_attestation_verifier",
                return_value=lambda **_kwargs: True,
            ):
                recovered = web_bridge.recover_same_controller_web_session(
                    repo=repo,
                    web_session_id="web-new",
                    registry_path=registry,
                    host_identity_receipt={"attested": True},
                )

            self.assertEqual(recovered["result"], "RECOVERED")
            self.assertTrue(recovered["resume_lease_rotated"])
            record = json.loads(lease.read_text(encoding="utf-8"))["leases"]["controller-1"]
            self.assertEqual(record["web_session_id"], "web-new")
            self.assertEqual(record["authorized_at_unix"], 100)
            self.assertEqual(record["expires_at_unix"], 4102444800)
            self.assertEqual(record["provenance"], "manual_user_authorized")
            self.assertEqual(record["mode"], "resume_only")

    def test_same_controller_web_recovery_does_not_create_resume_lease_without_prior_authorization(self) -> None:
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({"controller-1": str(repo.resolve())}), encoding="utf-8")
            lease = root / "manual-web-leases.json"

            with patch.object(web_bridge, "DEFAULT_MANUAL_WEB_LEASES", lease), patch.object(
                web_bridge,
                "_registered_peer_attestation_verifier",
                return_value=lambda **_kwargs: True,
            ):
                recovered = web_bridge.recover_same_controller_web_session(
                    repo=repo,
                    web_session_id="web-new",
                    registry_path=registry,
                    host_identity_receipt={"attested": True},
                )

            self.assertEqual(recovered["result"], "RECOVERED")
            self.assertFalse(recovered["resume_lease_rotated"])
            self.assertFalse(lease.exists())

    def test_replace_web_session_bootstrap_rotates_target_and_manual_lease_without_host_attestation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"; repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo),
                "__controller_sessions__": {"controller-1": {"web": ["web-old", "web-new"]}},
            }), encoding="utf-8")
            lease = root / "manual-web-leases.json"
            lease.write_text(json.dumps({
                "schema_version": 1,
                "leases": {"controller-1": {
                    "repo": str(repo.resolve()), "controller_id": "controller-1",
                    "web_session_id": "web-old", "authorized_at_unix": 10,
                    "expires_at_unix": 4102444800,
                    "provenance": "manual_user_authorized", "mode": "resume_only",
                }},
            }), encoding="utf-8")

            result = self.run_bridge(
                "replace-web-session", "--repo", str(repo),
                "--controller-id", "controller-1", "--web-session-id", "web-new",
                "--expected-generation", "0", "--expected-ownership-generation", "0", "--registry", str(registry),
                "--lease-file", str(lease),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            receipt = json.loads(result.stdout)
            self.assertEqual(receipt["controller_id"], "controller-1")
            self.assertEqual(receipt["execution_target_session_id"], "web-new")
            self.assertEqual(receipt["generation"], 1)
            self.assertEqual(receipt["ownership_generation"], 1)
            self.assertEqual(receipt["binding"], "temporary")
            self.assertFalse(receipt["host_attested"])
            self.assertTrue(receipt["resume_lease_rotated"])
            saved = json.loads(registry.read_text())
            self.assertEqual(saved["__controller_sessions__"]["controller-1"]["web"], ["web-old", "web-new"])
            target = saved["__controller_targets__"]["controller-1"]["web"]
            self.assertEqual(target["session_id"], "web-new")
            self.assertEqual(target["generation"], 1)
            self.assertEqual(target["provenance"], "manual_user_authorized")
            self.assertFalse(target["host_attested"])
            ownership = saved["__controller_execution_ownership__"]["controller-1"]
            self.assertEqual(ownership["active_host"], "web")
            self.assertEqual(ownership["execution_target_session_id"], "web-new")
            self.assertEqual(ownership["generation"], 1)
            self.assertEqual(ownership["provenance"], "manual_user_authorized")
            rotated = json.loads(lease.read_text())["leases"]["controller-1"]
            self.assertEqual(rotated["web_session_id"], "web-new")
            self.assertEqual(rotated["authorized_at_unix"], 10)
            self.assertEqual(rotated["expires_at_unix"], 4102444800)
            self.assertEqual(rotated["provenance"], "manual_user_authorized")
            self.assertEqual(rotated["mode"], "resume_only")
            self.assertIn("rotated_at_unix", rotated)

            resolved = web_bridge.target_guard.resolve_execution_target(
                repo=repo, host="web", registry_path=registry
            )
            self.assertEqual(resolved["execution_target_session_id"], "web-new")
            self.assertEqual(resolved["generation"], 1)
            with self.assertRaises(PermissionError):
                web_bridge.target_guard.check_execution_target(
                    repo=repo, host="web", action="message", target_session_id="web-old",
                    registry_path=registry,
                )
            identity = web_bridge.target_guard.controller_identity_projection(
                repo=repo, host="web", source_session_id="web-new", registry_path=registry
            )
            self.assertEqual(identity["session_binding_state"]["verification"], "UNVERIFIED")
            self.assertEqual(identity["session_binding_state"]["binding_mode"], "temporary")
            self.assertEqual(identity["session_binding_state"]["provenance"], "manual_user_authorized")
            self.assertFalse(identity["session_binding_state"]["host_attested"])
            self.assertFalse(identity["controller_actions_allowed"])
            with self.assertRaises(PermissionError):
                web_bridge.require_web_controller_session(
                    controller_id="controller-1", web_session_id="web-new", registry_path=registry
                )
            self.assertEqual(
                web_bridge.resolve_reentry_session(
                    controller_id="controller-1", repo=repo, registry_path=registry, lease_path=lease,
                    now_unix=11,
                ),
                "web-new",
            )

    def test_manual_web_mutations_cannot_downgrade_host_attested_current_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"; repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = root / "controllers.json"
            original_registry = {
                "controller-1": str(repo),
                "__controller_sessions__": {
                    "controller-1": {"web": ["web-strong", "web-manual"]}
                },
                "__controller_targets__": {
                    "controller-1": {"web": {
                        "status": "active",
                        "session_id": "web-strong",
                        "generation": 4,
                        "provenance": "host_attested_same_controller_recovery",
                        "binding_mode": "resume_only",
                        "identity_proof": "host_attested_origin",
                    }}
                },
                "__controller_execution_ownership__": {
                    "controller-1": {
                        "active_host": "web",
                        "execution_target_session_id": "web-strong",
                        "generation": 4,
                        "provenance": "web_entry",
                    }
                },
            }
            registry.write_text(json.dumps(original_registry), encoding="utf-8")
            lease = root / "manual.json"
            original_lease = {
                "schema_version": 1,
                "leases": {"controller-1": {
                    "repo": str(repo.resolve()),
                    "controller_id": "controller-1",
                    "web_session_id": "web-manual",
                    "authorized_at_unix": 10,
                    "expires_at_unix": 4102444800,
                    "provenance": "manual_user_authorized",
                    "mode": "resume_only",
                }},
            }
            lease.write_text(json.dumps(original_lease), encoding="utf-8")

            replace = self.run_bridge(
                "replace-web-session", "--repo", str(repo),
                "--controller-id", "controller-1", "--web-session-id", "web-manual",
                "--expected-generation", "4", "--expected-ownership-generation", "4",
                "--registry", str(registry), "--lease-file", str(lease),
            )
            self.assertEqual(replace.returncode, 78)
            self.assertIn("Host-attested current target", replace.stderr)
            self.assertEqual(json.loads(registry.read_text()), original_registry)
            self.assertEqual(json.loads(lease.read_text()), original_lease)

            unbind = self.run_bridge(
                "unbind-web-session", "--repo", str(repo),
                "--controller-id", "controller-1", "--web-session-id", "web-strong",
                "--expected-generation", "4", "--expected-ownership-generation", "4",
                "--registry", str(registry),
            )
            self.assertEqual(unbind.returncode, 78)
            self.assertIn("Host-attested current target", unbind.stderr)
            self.assertEqual(json.loads(registry.read_text()), original_registry)
            self.assertEqual(json.loads(lease.read_text()), original_lease)

    def test_replace_web_session_rejects_foreign_session_repo_mismatch_and_active_outbound_lease(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"; repo.mkdir(); other = root / "other"; other.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            subprocess.run(["git", "init", "-q", "-b", "main", str(other)], check=True)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo), "controller-2": str(other),
                "__controller_sessions__": {
                    "controller-1": {"web": ["web-old", "web-new"]},
                    "controller-2": {"web": ["web-foreign"]},
                },
            }), encoding="utf-8")
            lease = root / "manual.json"
            foreign = self.run_bridge(
                "replace-web-session", "--repo", str(repo), "--controller-id", "controller-1",
                "--web-session-id", "web-foreign", "--expected-generation", "0",
                "--expected-ownership-generation", "0", "--registry", str(registry), "--lease-file", str(lease),
            )
            self.assertEqual(foreign.returncode, 78)
            mismatch = self.run_bridge(
                "replace-web-session", "--repo", str(other), "--controller-id", "controller-1",
                "--web-session-id", "web-new", "--expected-generation", "0",
                "--expected-ownership-generation", "0", "--registry", str(registry), "--lease-file", str(lease),
            )
            self.assertEqual(mismatch.returncode, 78)

            data = json.loads(registry.read_text())
            data["__controller_outbound_leases__"] = {
                "controller-1": {"web": {"tool-1": {
                    "action": "message", "target_session_id": "web-old", "generation": 0,
                }}}
            }
            registry.write_text(json.dumps(data), encoding="utf-8")
            blocked = self.run_bridge(
                "replace-web-session", "--repo", str(repo), "--controller-id", "controller-1",
                "--web-session-id", "web-new", "--expected-generation", "0",
                "--expected-ownership-generation", "0", "--registry", str(registry), "--lease-file", str(lease),
            )
            self.assertEqual(blocked.returncode, 78)
            self.assertIn("outbound", blocked.stderr.lower())

    def test_replace_web_session_rejects_unapproved_session_and_stale_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"; repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo),
                "__controller_sessions__": {"controller-1": {"web": ["web-old", "web-approved"]}},
                "__controller_targets__": {"controller-1": {"web": {
                    "status": "active", "session_id": "web-old", "generation": 3,
                    "provenance": "manual_user_authorized", "binding_mode": "temporary", "host_attested": False,
                }}},
            }), encoding="utf-8")
            lease = root / "manual.json"
            unapproved = self.run_bridge(
                "replace-web-session", "--repo", str(repo), "--controller-id", "controller-1",
                "--web-session-id", "web-unapproved", "--expected-generation", "3", "--expected-ownership-generation", "0",
                "--registry", str(registry), "--lease-file", str(lease),
            )
            self.assertEqual(unapproved.returncode, 78)
            self.assertIn("lineage", unapproved.stderr)
            stale = self.run_bridge(
                "replace-web-session", "--repo", str(repo), "--controller-id", "controller-1",
                "--web-session-id", "web-approved", "--expected-generation", "2", "--expected-ownership-generation", "0",
                "--registry", str(registry), "--lease-file", str(lease),
            )
            self.assertEqual(stale.returncode, 78)
            self.assertIn("generation", stale.stderr)
            current = web_bridge.target_guard.resolve_execution_target(
                repo=repo, host="web", registry_path=registry
            )
            self.assertEqual(current["execution_target_session_id"], "web-old")
            self.assertEqual(current["generation"], 3)

    def test_replace_same_web_target_persists_ownership_handoff_from_desktop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"; repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo),
                "__controller_sessions__": {
                    "controller-1": {"web": ["web-current"], "desktop_codex": ["desktop-current"]}
                },
                "__controller_targets__": {
                    "controller-1": {
                        "web": {
                            "status": "active", "session_id": "web-current", "generation": 4,
                            "provenance": "manual_user_authorized", "binding_mode": "temporary",
                            "host_attested": False,
                        },
                        "desktop_codex": {
                            "status": "active", "session_id": "desktop-current", "generation": 2,
                        },
                    }
                },
                "__controller_execution_ownership__": {
                    "controller-1": {
                        "active_host": "desktop_codex",
                        "execution_target_session_id": "desktop-current",
                        "generation": 7,
                        "provenance": "desktop_entry",
                    }
                },
            }), encoding="utf-8")

            result = self.run_bridge(
                "replace-web-session", "--repo", str(repo), "--controller-id", "controller-1",
                "--web-session-id", "web-current", "--expected-generation", "4",
                "--expected-ownership-generation", "7", "--registry", str(registry),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            receipt = json.loads(result.stdout)
            self.assertEqual(receipt["generation"], 4)
            self.assertEqual(receipt["ownership_generation"], 8)
            saved = json.loads(registry.read_text(encoding="utf-8"))
            ownership = saved["__controller_execution_ownership__"]["controller-1"]
            self.assertEqual(ownership["active_host"], "web")
            self.assertEqual(ownership["execution_target_session_id"], "web-current")
            self.assertEqual(ownership["generation"], 8)
            self.assertEqual(ownership["provenance"], "manual_user_authorized")

    def test_replace_web_session_rolls_back_manual_lease_when_registry_commit_fails(self) -> None:
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"; repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo),
                "__controller_sessions__": {"controller-1": {"web": ["web-old", "web-new"]}},
            }), encoding="utf-8")
            lease = root / "manual.json"
            original_lease = {
                "schema_version": 1,
                "leases": {"controller-1": {
                    "repo": str(repo.resolve()), "controller_id": "controller-1",
                    "web_session_id": "web-old", "authorized_at_unix": 10,
                    "expires_at_unix": 4102444800,
                    "provenance": "manual_user_authorized", "mode": "resume_only",
                }},
            }
            lease.write_text(json.dumps(original_lease), encoding="utf-8")
            original_registry = json.loads(registry.read_text())
            real_write = web_bridge._write_json_atomic_file
            def failing_write(path, payload):
                if Path(path) == registry:
                    raise OSError("registry commit failed")
                return real_write(Path(path), payload)
            with patch.object(web_bridge, "_write_json_atomic_file", side_effect=failing_write):
                with self.assertRaisesRegex(OSError, "registry commit failed"):
                    web_bridge.replace_web_session(
                        repo=repo, controller_id="controller-1", web_session_id="web-new",
                        expected_generation=0, expected_ownership_generation=0,
                        registry_path=registry, lease_path=lease,
                    )
            self.assertEqual(json.loads(registry.read_text()), original_registry)
            self.assertEqual(json.loads(lease.read_text()), original_lease)

    def test_replace_web_session_rejects_stale_ownership_generation_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"; repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = root / "controllers.json"
            original = {
                "controller-1": str(repo),
                "__controller_sessions__": {"controller-1": {"web": ["web-old", "web-new"]}},
                "__controller_targets__": {"controller-1": {"web": {
                    "status": "active", "session_id": "web-old", "generation": 2,
                    "provenance": "manual_user_authorized", "binding_mode": "temporary", "host_attested": False,
                }}},
                "__controller_execution_ownership__": {"controller-1": {
                    "active_host": "web", "execution_target_session_id": "web-old", "generation": 4,
                    "provenance": "manual_user_authorized",
                }},
            }
            registry.write_text(json.dumps(original), encoding="utf-8")
            result = self.run_bridge(
                "replace-web-session", "--repo", str(repo), "--controller-id", "controller-1",
                "--web-session-id", "web-new", "--expected-generation", "2",
                "--expected-ownership-generation", "3", "--registry", str(registry),
            )
            self.assertEqual(result.returncode, 78)
            self.assertIn("ownership generation", result.stderr)
            self.assertEqual(json.loads(registry.read_text()), original)

    def test_unbind_web_session_rejects_session_outside_controller_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"; repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo),
                "__controller_sessions__": {"controller-1": {"web": ["web-current"]}},
                "__controller_targets__": {"controller-1": {"web": {
                    "status": "active", "session_id": "web-current", "generation": 4,
                    "provenance": "manual_user_authorized", "binding_mode": "temporary", "host_attested": False,
                }}},
                "__controller_execution_ownership__": {"controller-1": {
                    "active_host": "web", "execution_target_session_id": "web-current", "generation": 1,
                    "provenance": "manual_user_authorized",
                }},
            }), encoding="utf-8")
            result = self.run_bridge(
                "unbind-web-session", "--repo", str(repo), "--controller-id", "controller-1",
                "--web-session-id", "web-unknown", "--expected-generation", "4",
                "--expected-ownership-generation", "1", "--registry", str(registry),
            )
            self.assertEqual(result.returncode, 78)
            self.assertIn("lineage", result.stderr)
            saved = json.loads(registry.read_text(encoding="utf-8"))
            self.assertEqual(saved["__controller_targets__"]["controller-1"]["web"]["session_id"], "web-current")

    def test_replace_same_web_target_is_idempotent_and_unbind_tombstones_without_losing_alias_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"; repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo),
                "__controller_sessions__": {"controller-1": {"web": ["web-old", "web-current"]}},
                "__controller_targets__": {"controller-1": {"web": {
                    "status": "active", "session_id": "web-current", "generation": 4,
                    "provenance": "manual_user_authorized", "binding_mode": "temporary", "host_attested": False,
                }}},
                "__controller_execution_ownership__": {"controller-1": {
                    "active_host": "web", "execution_target_session_id": "web-current", "generation": 1,
                    "provenance": "manual_user_authorized",
                }},
            }), encoding="utf-8")
            lease = root / "manual.json"
            same = self.run_bridge(
                "replace-web-session", "--repo", str(repo), "--controller-id", "controller-1",
                "--web-session-id", "web-current", "--expected-generation", "4", "--expected-ownership-generation", "1",
                "--registry", str(registry), "--lease-file", str(lease),
            )
            self.assertEqual(same.returncode, 0, same.stderr)
            self.assertEqual(json.loads(same.stdout)["generation"], 4)

            unbound = self.run_bridge(
                "unbind-web-session", "--repo", str(repo), "--controller-id", "controller-1",
                "--web-session-id", "web-current", "--expected-generation", "4", "--expected-ownership-generation", "1",
                "--registry", str(registry),
            )
            self.assertEqual(unbound.returncode, 0, unbound.stderr)
            saved = json.loads(registry.read_text())
            self.assertEqual(saved["__controller_sessions__"]["controller-1"]["web"], ["web-old", "web-current"])
            target = saved["__controller_targets__"]["controller-1"]["web"]
            self.assertEqual(target, {"status": "unbound", "session_id": None, "generation": 5,
                                      "provenance": "manual_user_authorized", "binding_mode": "temporary",
                                      "host_attested": False})
            with self.assertRaises(PermissionError):
                web_bridge.target_guard.resolve_execution_target(repo=repo, host="web", registry_path=registry)

    def test_bind_web_session_cli_refuses_session_already_bound_to_other_controller(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"; repo.mkdir()
            other = root / "other"; other.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo),
                "controller-2": str(other),
                "__controller_sessions__": {"controller-2": {"web": ["web-shared"]}},
            }), encoding="utf-8")

            result = self.run_bridge(
                "bind-web-session", "--repo", str(repo), "--controller-id", "controller-1",
                "--web-session-id", "web-shared", "--registry", str(registry),
            )

            self.assertEqual(result.returncode, 78)
            self.assertIn("already bound to another Controller", result.stderr)

    def test_bind_web_session_persists_unique_binding_for_registered_controller(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"; repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({"controller-1": str(repo)}), encoding="utf-8")

            result = self.run_bridge(
                "bind-web-session", "--repo", str(repo), "--controller-id", "controller-1",
                "--web-session-id", "web-session-1", "--registry", str(registry),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            saved = json.loads(registry.read_text(encoding="utf-8"))
            self.assertEqual(saved["__controller_sessions__"]["controller-1"]["web"], ["web-session-1"])

    def test_authorize_manual_web_session_persists_scoped_resume_lease(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"; repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo),
                "__controller_sessions__": {"controller-1": {"web": ["web-session-1"]}},
            }), encoding="utf-8")
            _provision_verified_current_web_target(registry, web_session_id="web-session-1")
            lease = root / "manual-web-leases.json"

            result = self.run_bridge(
                "authorize-manual-web-session",
                "--repo", str(repo),
                "--controller-id", "controller-1",
                "--web-session-id", "web-session-1",
                "--registry", str(registry),
                "--lease-file", str(lease),
                "--ttl-seconds", str(30 * 24 * 60 * 60),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            saved = json.loads(lease.read_text(encoding="utf-8"))
            item = saved["leases"]["controller-1"]
            self.assertEqual(payload["mode"], "resume_only")
            self.assertEqual(item["repo"], str(repo.resolve()))
            self.assertEqual(item["web_session_id"], "web-session-1")
            self.assertEqual(item["provenance"], "manual_user_authorized")
            self.assertEqual(item["mode"], "resume_only")
            self.assertGreaterEqual(item["expires_at_unix"] - item["authorized_at_unix"], 30 * 24 * 60 * 60)

    def test_authorize_manual_web_session_requires_existing_verified_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"; repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({"controller-1": str(repo)}), encoding="utf-8")
            lease = root / "manual-web-leases.json"

            result = self.run_bridge(
                "authorize-manual-web-session",
                "--repo", str(repo),
                "--controller-id", "controller-1",
                "--web-session-id", "web-session-1",
                "--registry", str(registry),
                "--lease-file", str(lease),
            )

            self.assertEqual(result.returncode, 78)
            self.assertIn("must already be bound", result.stderr)
            self.assertFalse(lease.exists())

    def test_resolve_manual_web_session_returns_only_live_verified_repo_lease(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"; repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo),
                "__controller_sessions__": {"controller-1": {"web": ["web-session-1"]}},
            }), encoding="utf-8")
            _provision_verified_current_web_target(registry, web_session_id="web-session-1")
            lease = root / "manual-web-leases.json"
            lease.write_text(json.dumps({
                "schema_version": 1,
                "leases": {
                    "controller-1": {
                        "repo": str(repo.resolve()),
                        "web_session_id": "web-session-1",
                        "authorized_at_unix": 1,
                        "expires_at_unix": 4102444800,
                        "provenance": "manual_user_authorized",
                        "mode": "resume_only",
                    }
                },
            }), encoding="utf-8")

            result = self.run_bridge(
                "resolve-manual-web-session",
                "--cwd", str(repo),
                "--registry", str(registry),
                "--lease-file", str(lease),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "web-session-1")

    def test_resolve_manual_web_session_silently_ignores_expired_or_unverified_lease(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"; repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo),
                "__controller_sessions__": {"controller-1": {"web": ["web-session-1"]}},
            }), encoding="utf-8")
            lease = root / "manual-web-leases.json"
            base = {
                "schema_version": 1,
                "leases": {
                    "controller-1": {
                        "repo": str(repo.resolve()),
                        "web_session_id": "web-session-1",
                        "authorized_at_unix": 1,
                        "expires_at_unix": 2,
                        "provenance": "manual_user_authorized",
                        "mode": "resume_only",
                    }
                },
            }
            lease.write_text(json.dumps(base), encoding="utf-8")
            expired = self.run_bridge(
                "resolve-manual-web-session", "--cwd", str(repo),
                "--registry", str(registry), "--lease-file", str(lease),
            )
            self.assertEqual(expired.returncode, 0, expired.stderr)
            self.assertEqual(expired.stdout, "")

            base["leases"]["controller-1"]["expires_at_unix"] = 4102444800
            base["leases"]["controller-1"]["web_session_id"] = "web-not-bound"
            lease.write_text(json.dumps(base), encoding="utf-8")
            unverified = self.run_bridge(
                "resolve-manual-web-session", "--cwd", str(repo),
                "--registry", str(registry), "--lease-file", str(lease),
            )
            self.assertEqual(unverified.returncode, 0, unverified.stderr)
            self.assertEqual(unverified.stdout, "")

    def test_post_shell_silently_skips_unregistered_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = tmp_path / "controllers.json"
            registry.write_text("{}\n", encoding="utf-8")

            result = self.run_bridge(
                "post-shell",
                "--cwd",
                str(repo),
                "--command",
                "git status --short",
                "--exit-code",
                "0",
                "--registry",
                str(registry),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertEqual(result.stderr, "")

    def test_post_shell_refuses_ambiguous_controller_registration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = tmp_path / "controllers.json"
            registry.write_text(
                json.dumps(
                    {
                        "controller-1": str(repo),
                        "controller-2": str(repo),
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            result = self.run_bridge(
                "post-shell",
                "--cwd",
                str(repo),
                "--command",
                "git status --short",
                "--exit-code",
                "0",
                "--registry",
                str(registry),
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("exactly one registered controller", result.stderr)

    def test_post_shell_resolves_explicit_bound_controller_worktree_surface(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            main = root / "repo"
            main.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(main)], check=True)
            subprocess.run(["git", "-C", str(main), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(main), "config", "user.name", "Test"], check=True)
            (main / "seed").write_text("x", encoding="utf-8")
            subprocess.run(["git", "-C", str(main), "add", "seed"], check=True)
            subprocess.run(["git", "-C", str(main), "commit", "-q", "-m", "seed"], check=True)
            surface = root / "controller-surface"
            subprocess.run(["git", "-C", str(main), "worktree", "add", "-q", "-b", "controller-surface", str(surface)], check=True)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(main.resolve()),
                "__controller_surfaces__": {"controller-1": str(surface.resolve())},
                "__controller_sessions__": {"controller-1": {"web": ["web-session-1"]}},
            }), encoding="utf-8")
            _provision_verified_current_web_target(registry, web_session_id="web-session-1")
            capture = root / "capture.json"
            result = self.run_bridge(
                "post-shell", "--cwd", str(surface), "--command", "git status --short",
                "--exit-code", "0", "--registry", str(registry), "--web-session-id", "web-session-1",
                "--capture-event", str(capture),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(capture.read_text(encoding="utf-8"))["session_id"], "controller-1")

    def test_zshenv_exit_bridge_executes_and_preserves_exit_precedence(self) -> None:
        block = web_bridge.zshenv_block()
        self.assertNotIn("|| true", block)
        self.assertNotIn("\\n      --cwd", block)
        self.assertIn('post-shell --cwd "$_ad_web_cwd"', block)
        self.assertIn('ADAPTIVE_DELIVERY_WEB_SESSION_ID', block)
        self.assertIn('-o comm=', block)
        self.assertNotIn('== *\"', block)
        self.assertNotIn('resolve-manual-web-session', block)
        self.assertIn('--web-session-id "$_ad_web_session_id"', block)
        self.assertNotIn("unset _ad_web_parent _ad_web_session_id", block)

        function = block.split("  _ad_web_lifecycle_exit() {", 1)[1].split("  }\n  trap", 1)[0]
        function = "_ad_web_lifecycle_exit() {" + function + "}"
        bridge_call = '"$_ad_web_bridge_python" "$_ad_web_bridge_script" post-shell --cwd "$_ad_web_cwd" --command "$_ad_web_command" --exit-code "$_ad_web_exit_code" --web-session-id "$_ad_web_session_id"'

        def run_exit_function(original_exit: int, bridge_exit: int) -> int:
            script = (
                "_ad_web_cwd=/tmp; _ad_web_command=true; "
                + function.replace(bridge_call, f"/bin/sh -c 'exit {bridge_exit}'")
                + f"\n/bin/sh -c 'exit {original_exit}'; _ad_web_lifecycle_exit"
            )
            return subprocess.run(["/bin/zsh", "-c", script], check=False).returncode

        self.assertEqual(run_exit_function(0, 0), 0)
        self.assertEqual(run_exit_function(0, 78), 78)
        self.assertEqual(run_exit_function(7, 0), 7)
        self.assertEqual(run_exit_function(7, 78), 7)

    def test_print_zshenv_block_is_scoped_to_ai_bridge_parent(self) -> None:
        result = self.run_bridge("print-zshenv-block")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("AI-Bridge.app/Contents/MacOS/ai-bridge", result.stdout)
        self.assertIn("ZSH_EXECUTION_STRING", result.stdout)
        self.assertIn("post-shell", result.stdout)
        self.assertIn("trap - EXIT", result.stdout)


if __name__ == "__main__":
    unittest.main()


class WebLifecycleAuditTests(unittest.TestCase):
    def run_bridge(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(BRIDGE), *args],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_audit_once_captures_successful_control_guard_receipt_and_advances_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            registry = tmp_path / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {"controller-1": {"web": ["web-session-1"]}},
            }), encoding="utf-8")
            _provision_verified_current_web_target(registry, web_session_id="web-session-1")
            audit = tmp_path / "audit.jsonl"
            receipt = {
                "receiptId": "guard-1",
                "childTool": "shell_command",
                "state": "succeeded",
                "rootLabel": str(repo),
                "targetLabel": GUARD_COMMAND,
                "detail": (
                    f"命令：{GUARD_COMMAND}"
                    f" · 工作目录：{repo}\n\n命令输出：\n"
                    "control-event: allowed; declared READY decisions are complete\n"
                ),
            }
            audit.write_text(json.dumps(receipt) + "\n", encoding="utf-8")
            cursor = tmp_path / "cursor.json"
            capture = tmp_path / "events.jsonl"

            result = self.run_bridge(
                "audit-once",
                "--session-id",
                "controller-1",
                "--repo",
                str(repo),
                "--registry", str(registry),
                "--web-session-id", "web-session-1",
                "--audit-log",
                str(audit),
                "--cursor",
                str(cursor),
                "--capture-events",
                str(capture),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            events = [json.loads(line) for line in capture.read_text().splitlines()]
            self.assertEqual(len(events), 1)
            self.assertIn("control_event_guard.py", events[0]["tool_input"]["command"])
            self.assertIn("control-event: allowed", events[0]["tool_response"]["output"])
            self.assertEqual(json.loads(cursor.read_text())["offset"], audit.stat().st_size)

    def test_audit_consumer_waits_for_cross_process_cursor_lock_before_dispatch(self) -> None:
        import fcntl, time
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); repo = root / "repo"; repo.mkdir()
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {"controller-1": {"web": ["web-session-1"]}},
            }), encoding="utf-8")
            _provision_verified_current_web_target(registry, web_session_id="web-session-1")
            audit = root / "audit.jsonl"; cursor = root / "cursor.json"
            receipt = {
                "receiptId":"guard-lock-1", "childTool":"shell_command", "state":"succeeded",
                "rootLabel":str(repo), "targetLabel":GUARD_COMMAND,
                "detail":f"命令：{GUARD_COMMAND} · 工作目录：{repo}\n\n命令输出：\ncontrol-event: allowed; done\n",
            }
            audit.write_text(json.dumps(receipt) + "\n", encoding="utf-8")
            lock_path = cursor.with_suffix(cursor.suffix + ".consumer.lock")
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            holder = lock_path.open("a+")
            fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            released = []
            import threading
            def release_later():
                time.sleep(0.35)
                fcntl.flock(holder.fileno(), fcntl.LOCK_UN); holder.close(); released.append(True)
            thread = threading.Thread(target=release_later); thread.start()
            started = time.monotonic()
            with patch.object(web_bridge, "dispatch_event", return_value=0) as dispatch, patch.object(
                web_bridge, "_load_lifecycle_state", return_value={}
            ):
                code = web_bridge.main(["audit-once", "--session-id", "controller-1", "--repo", str(repo),
                    "--registry", str(registry), "--web-session-id", "web-session-1",
                    "--audit-log", str(audit), "--cursor", str(cursor)])
            elapsed = time.monotonic() - started
            thread.join()
        self.assertEqual(code, 0)
        self.assertTrue(released)
        self.assertGreater(elapsed, 0.25)
        self.assertEqual(dispatch.call_count, 1)

    def test_audit_dispatch_failure_does_not_advance_cursor_and_replay_is_fail_closed(self) -> None:
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); repo = root / "repo"; repo.mkdir()
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {"controller-1": {"web": ["web-session-1"]}},
            }), encoding="utf-8")
            _provision_verified_current_web_target(registry, web_session_id="web-session-1")
            audit = root / "audit.jsonl"; cursor = root / "cursor.json"
            receipt = {
                "receiptId":"guard-fail-1", "childTool":"shell_command", "state":"succeeded",
                "rootLabel":str(repo), "targetLabel":GUARD_COMMAND,
                "detail":f"命令：{GUARD_COMMAND} · 工作目录：{repo}\n\n命令输出：\ncontrol-event: allowed; done\n",
            }
            audit.write_text(json.dumps(receipt) + "\n", encoding="utf-8")
            args = ["audit-once", "--session-id", "controller-1", "--repo", str(repo),
                    "--registry", str(registry), "--web-session-id", "web-session-1",
                    "--audit-log", str(audit), "--cursor", str(cursor)]
            with patch.object(web_bridge, "dispatch_event", return_value=9) as first:
                code1 = web_bridge.main(args)
            offset1 = json.loads(cursor.read_text()).get("offset", 0) if cursor.exists() else 0
            with patch.object(web_bridge, "dispatch_event", return_value=0) as second:
                code2 = web_bridge.main(args)
        self.assertNotEqual(code1, 0)
        self.assertEqual(offset1, 0)
        self.assertEqual(first.call_count, 1)
        self.assertNotEqual(code2, 0)
        self.assertEqual(second.call_count, 0)

    def test_audit_once_rule_update_uses_guarded_scheduler_and_never_direct_scheduler(self) -> None:
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); repo = root / "repo"; repo.mkdir()
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {"controller-1": {"web": ["web-session-1"]}},
            }), encoding="utf-8")
            _provision_verified_current_web_target(registry, web_session_id="web-session-1")
            audit = root / "audit.jsonl"; cursor = root / "cursor.json"
            receipt = {
                "receiptId":"guard-rule-1", "childTool":"shell_command", "state":"succeeded",
                "rootLabel":str(repo), "targetLabel":GUARD_COMMAND,
                "detail":f"命令：{GUARD_COMMAND} · 工作目录：{repo}\n\n命令输出：\ncontrol-event: allowed; done\n",
            }
            audit.write_text(json.dumps(receipt) + "\n", encoding="utf-8")
            lifecycle = {
                "pending_control_event": True, "rule_wake_policy": "immediate",
                "triggers": ["rule_update_pending:rev-new"],
                "snapshot": {"rule_handshake": {"installed_revision": "rev-new"}},
            }
            args = ["audit-once", "--session-id", "controller-1", "--repo", str(repo),
                    "--registry", str(registry), "--web-session-id", "web-session-1",
                    "--audit-log", str(audit), "--cursor", str(cursor), "--auto-native-stop"]
            with patch.object(web_bridge, "dispatch_event", return_value=0), patch.object(
                web_bridge, "_load_lifecycle_state", return_value={}
            ), patch.object(
                web_bridge, "refresh_rule_wake_state", return_value=lifecycle
            ), patch.object(
                web_bridge, "schedule_auto_native_stop", return_value=True
            ), patch.object(
                web_bridge, "schedule_guarded_rule_wake", return_value={"schedule":"blocked","reason":"explicit current execution target required"}
            ) as guarded, patch.object(web_bridge, "maybe_schedule_rule_wake") as direct:
                code = web_bridge.main(args)
        self.assertEqual(code, 0)
        guarded.assert_called_once()
        direct.assert_not_called()

    def test_auto_native_stop_reschedules_same_controller_after_active_writer_deferral(self) -> None:
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"; repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = root / "registry.json"
            registry.write_text(
                json.dumps({"controller-1": str(repo.resolve())}), encoding="utf-8"
            )
            state = root / "auto-stop.json"
            state.write_text(json.dumps({
                "receipt_id": "pending-click-1", "session_id": "controller-1",
                "repo": str(repo.resolve()), "state": "RESUME_PENDING", "pending_control_event": True,
            }), encoding="utf-8")
            deferred = {
                "operation": "native_resume", "result": "DEFERRED", "state": "RESUME_DEFERRED_ACTIVE_WRITER",
                "pending_control_event": True, "returncode": 1, "stdout_tail": "",
                "stderr_tail": "thread already has an active writer", "failure_class": "active_writer_present",
            }
            with patch.object(web_bridge, "_load_lifecycle_state", return_value={"pending_control_event": True, "controller_host": "desktop_codex"}), patch.object(
                web_bridge, "execute_native_resume", return_value=deferred
            ), patch.object(web_bridge, "schedule_auto_native_stop") as schedule:
                code = web_bridge.run_auto_native_stop(
                    session_id="controller-1", repo=repo, receipt_id="pending-click-1",
                    registry=registry, codex="/opt/homebrew/bin/codex", delay_seconds=0,
                    state_path=state, runtime_path="/opt/homebrew/bin:/usr/bin:/bin",
                )
            self.assertEqual(code, 0)
            schedule.assert_called_once()
            self.assertEqual(schedule.call_args.kwargs["receipt_id"], "pending-click-1")
            self.assertEqual(schedule.call_args.kwargs["session_id"], "controller-1")

    def test_auto_native_stop_confirms_host_observed_canonical_target_already_foreground(self) -> None:
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"; repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = root / "registry.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {"controller-1": {
                    "desktop_codex": ["desktop-current"],
                }},
                "__controller_targets__": {"controller-1": {"desktop_codex": {
                    "status": "active", "session_id": "desktop-current", "generation": 4,
                }}},
                "__controller_execution_ownership__": {"controller-1": {
                    "active_host": "desktop_codex",
                    "execution_target_session_id": "desktop-current",
                    "generation": 7,
                }},
            }), encoding="utf-8")
            state = root / "auto-stop.json"
            state.write_text(json.dumps({
                "receipt_id": "rule-update:rev-1", "session_id": "controller-1",
                "repo": str(repo.resolve()), "state": "RESUME_PENDING",
                "pending_control_event": True,
            }), encoding="utf-8")
            initial = {
                "pending_control_event": True, "requires_user": False,
                "controller_host": "desktop_codex", "wake_generation": 11,
                "triggers": ["rule_update_pending:rev-1"],
            }
            closed = dict(initial, pending_control_event=False)
            already_foreground = {
                "operation": "native_resume", "result": "DEFERRED",
                "state": "RESUME_DEFERRED_ACTIVE_WRITER", "returncode": 1,
                "failure_class": "active_writer_present",
                "error_code": "WEB_LIFECYCLE_ACTIVE_WRITER",
                "stderr_tail": "thread desktop-current already has an active writer",
                "execution_target_session_id": "desktop-current", "target_generation": 4,
                "target_mode": "explicit_current",
                "host_observation": web_bridge._HOST_OBSERVED_CANONICAL_TARGET_FOREGROUND,
            }
            with patch.object(
                web_bridge, "_load_lifecycle_state", side_effect=[initial, closed]
            ), patch.object(
                web_bridge, "execute_native_resume", return_value=already_foreground
            ), patch.object(web_bridge, "_rearm_auto_native_stop") as rearm:
                code = web_bridge.run_auto_native_stop(
                    session_id="controller-1", repo=repo,
                    receipt_id="rule-update:rev-1", registry=registry,
                    codex="codex", delay_seconds=0, state_path=state,
                )
            self.assertEqual(code, 0)
            rearm.assert_not_called()
            wake = json.loads(
                web_bridge.default_wake_receipt_path(repo).read_text(encoding="utf-8")
            )
            self.assertEqual(wake["result"], "CONFIRMED")
            self.assertEqual(wake["operation"], "native_resume_already_foreground")
            self.assertEqual(wake["execution_target_session_id"], "desktop-current")
            self.assertEqual(wake["target_generation"], 4)
            self.assertEqual(wake["ownership_generation"], 7)

    def test_auto_native_stop_yields_external_wait_when_desktop_host_reload_is_required(self) -> None:
        import hashlib
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"; repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = root / "registry.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {"controller-1": {
                    "desktop_codex": ["desktop-current"],
                }},
                "__controller_targets__": {"controller-1": {"desktop_codex": {
                    "status": "active", "session_id": "desktop-current", "generation": 4,
                }}},
                "__controller_execution_ownership__": {"controller-1": {
                    "active_host": "desktop_codex",
                    "execution_target_session_id": "desktop-current",
                    "generation": 7,
                }},
            }), encoding="utf-8")
            runtime_state = repo / ".git" / "adaptive-delivery"
            runtime_state.mkdir()
            (runtime_state / "rule-handshake.json").write_text(json.dumps({
                "live_e2e_required": True,
                "installed_revision": "rev-2",
                "loaded_revision": "rev-2",
            }), encoding="utf-8")
            hooks = root / "hooks.json"
            hooks.write_text('{"hooks":{}}\n', encoding="utf-8")
            canary = root / "desktop-canary.json"
            canary.write_text(json.dumps({
                "schema_version": 4,
                "controller_id": "controller-1",
                "controller_session_id": "controller-1",
                "execution_target_session_id": "desktop-current",
                "target_generation": 4,
                "ownership_generation": 7,
                "canonical_repo": str(repo.resolve()),
                "controller_registry_path": str(registry.resolve()),
                "status": "armed",
                "sequence_index": 0,
                "observations": [],
                "hooks_sha256": hashlib.sha256(hooks.read_bytes()).hexdigest(),
            }), encoding="utf-8")
            state = root / "auto-stop.json"
            state.write_text(json.dumps({
                "receipt_id": "rule-update:rev-2", "session_id": "controller-1",
                "repo": str(repo.resolve()), "state": "RESUME_PENDING",
                "pending_control_event": True,
            }), encoding="utf-8")
            lifecycle = {
                "pending_control_event": True, "requires_user": False,
                "controller_host": "desktop_codex", "wake_generation": 12,
                "triggers": ["rule_update_pending:rev-2"],
            }
            already_foreground = {
                "operation": "native_resume", "result": "DEFERRED",
                "state": "RESUME_DEFERRED_ACTIVE_WRITER", "returncode": 1,
                "failure_class": "active_writer_present",
                "error_code": "WEB_LIFECYCLE_ACTIVE_WRITER",
                "stderr_tail": "thread desktop-current already has an active writer",
                "execution_target_session_id": "desktop-current", "target_generation": 4,
                "target_mode": "explicit_current",
                "host_observation": web_bridge._HOST_OBSERVED_CANONICAL_TARGET_FOREGROUND,
            }
            actual_reload_gate = web_bridge.desktop_host_reload_required

            def reload_required(**kwargs):
                return actual_reload_gate(
                    **kwargs, canary_path=canary, hooks_path=hooks
                )

            with patch.object(
                web_bridge, "_load_lifecycle_state", return_value=lifecycle
            ), patch.object(
                web_bridge, "execute_native_resume", return_value=already_foreground
            ), patch.object(
                web_bridge, "desktop_host_reload_required", side_effect=reload_required
            ) as reload_gate, patch.object(web_bridge, "_rearm_auto_native_stop") as rearm:
                code = web_bridge.run_auto_native_stop(
                    session_id="controller-1", repo=repo,
                    receipt_id="rule-update:rev-2", registry=registry,
                    codex="codex", delay_seconds=0, state_path=state,
                )

            self.assertEqual(code, 0)
            self.assertEqual(reload_gate.call_args.kwargs["registry_path"], registry)
            rearm.assert_not_called()
            self.assertFalse(web_bridge.default_wake_receipt_path(repo).exists())
            persisted = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(persisted["state"], "WAITING_EXTERNAL_HOST_RELOAD")
            self.assertTrue(persisted["host_reload_required"])
            self.assertEqual(persisted["activation_gate"], "host_reload_required")
            self.assertFalse(web_bridge.schedule_auto_native_stop(
                session_id="controller-1", repo=repo,
                receipt_id="rule-update:rev-2", registry=registry,
                codex="codex", delay_seconds=0, state_path=state,
            ))

    def test_desktop_host_reload_gate_requires_exact_armed_zero_sequence_canary(self) -> None:
        import hashlib
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"; repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            runtime_state = repo / ".git" / "adaptive-delivery"
            runtime_state.mkdir()
            (runtime_state / "rule-handshake.json").write_text(json.dumps({
                "live_e2e_required": True,
                "installed_revision": "rev-1",
                "loaded_revision": "rev-1",
            }), encoding="utf-8")
            hooks = root / "hooks.json"
            hooks.write_text('{"hooks":{}}\n', encoding="utf-8")
            registry = root / "registry.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_targets__": {"controller-1": {"desktop_codex": {
                    "status": "active", "session_id": "desktop-current", "generation": 4,
                }}},
                "__controller_execution_ownership__": {"controller-1": {
                    "active_host": "desktop_codex",
                    "execution_target_session_id": "desktop-current",
                    "generation": 7,
                }},
            }), encoding="utf-8")
            canary = root / "desktop-canary.json"
            value = {
                "controller_session_id": "controller-1",
                "status": "armed",
                "sequence_index": 0,
                "observations": [],
                "hooks_sha256": hashlib.sha256(hooks.read_bytes()).hexdigest(),
            }
            canary.write_text(json.dumps(value), encoding="utf-8")

            self.assertFalse(web_bridge.desktop_host_reload_required(
                session_id="controller-1", repo=repo,
                canary_path=canary, hooks_path=hooks,
            ))
            value.update({
                "schema_version": 4,
                "controller_id": "controller-1",
                "execution_target_session_id": "desktop-current",
                "target_generation": 4,
                "ownership_generation": 7,
                "canonical_repo": str(repo.resolve()),
                "controller_registry_path": str(registry.resolve()),
            })
            canary.write_text(json.dumps(value), encoding="utf-8")
            self.assertTrue(web_bridge.desktop_host_reload_required(
                session_id="controller-1", repo=repo,
                canary_path=canary, hooks_path=hooks, registry_path=registry,
            ))
            conflicted_registry = json.loads(registry.read_text(encoding="utf-8"))
            conflicted_registry["controller-2"] = str(repo.resolve())
            registry.write_text(json.dumps(conflicted_registry), encoding="utf-8")
            self.assertFalse(web_bridge.desktop_host_reload_required(
                session_id="controller-1", repo=repo,
                canary_path=canary, hooks_path=hooks, registry_path=registry,
            ))
            conflicted_registry.pop("controller-2")
            registry.write_text(json.dumps(conflicted_registry), encoding="utf-8")
            value["sequence_index"] = 1
            value["observations"] = ["session_started"]
            canary.write_text(json.dumps(value), encoding="utf-8")
            self.assertFalse(web_bridge.desktop_host_reload_required(
                session_id="controller-1", repo=repo,
                canary_path=canary, hooks_path=hooks, registry_path=registry,
            ))

    def test_mocked_active_writer_without_host_observation_still_rearms(self) -> None:
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"; repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = root / "registry.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_targets__": {"controller-1": {"desktop_codex": {
                    "status": "active", "session_id": "desktop-current", "generation": 4,
                }}},
                "__controller_execution_ownership__": {"controller-1": {
                    "active_host": "desktop_codex",
                    "execution_target_session_id": "desktop-current", "generation": 7,
                }},
            }), encoding="utf-8")
            state = root / "auto-stop.json"
            state.write_text(json.dumps({
                "receipt_id": "rule-update:rev-1", "session_id": "controller-1",
                "repo": str(repo.resolve()), "state": "RESUME_PENDING",
                "pending_control_event": True,
            }), encoding="utf-8")
            deferred = {
                "operation": "native_resume", "result": "DEFERRED",
                "state": "RESUME_DEFERRED_ACTIVE_WRITER", "returncode": 1,
                "failure_class": "active_writer_present",
                "execution_target_session_id": "desktop-current", "target_generation": 4,
            }
            lifecycle = {
                "pending_control_event": True, "requires_user": False,
                "controller_host": "desktop_codex", "wake_generation": 11,
            }
            with patch.object(
                web_bridge, "_load_lifecycle_state", return_value=lifecycle
            ), patch.object(
                web_bridge, "execute_native_resume", return_value=deferred
            ), patch.object(web_bridge, "_rearm_auto_native_stop") as rearm:
                code = web_bridge.run_auto_native_stop(
                    session_id="controller-1", repo=repo,
                    receipt_id="rule-update:rev-1", registry=registry,
                    codex="codex", delay_seconds=0, state_path=state,
                )
            self.assertEqual(code, 0)
            rearm.assert_called_once()
            self.assertFalse(web_bridge.default_wake_receipt_path(repo).exists())

    def test_serialized_active_writer_claim_cannot_confirm_already_foreground(self) -> None:
        attempt = {
            "operation": "native_resume",
            "result": "DEFERRED",
            "state": "RESUME_DEFERRED_ACTIVE_WRITER",
            "returncode": 1,
            "failure_class": "active_writer_present",
            "execution_target_session_id": "desktop-current",
            "target_generation": 4,
            "target_mode": "explicit_current",
            # A caller-controlled or persisted JSON value cannot reproduce the
            # process-local observation emitted by execute_native_resume().
            "host_observation": "canonical_target_already_foreground",
        }
        result = web_bridge.confirm_host_observed_desktop_foreground(
            attempt=attempt,
            lifecycle_state={"pending_control_event": True, "requires_user": False},
            ownership_fence={
                "controller_id": "controller-1",
                "active_host": "desktop_codex",
                "execution_target_session_id": "desktop-current",
                "generation": 7,
            },
        )
        self.assertIs(result, attempt)
        self.assertEqual(result["result"], "DEFERRED")

    def test_execute_native_resume_marks_host_observed_active_writer_process_locally(self) -> None:
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"; repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = root / "registry.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {"controller-1": {
                    "desktop_codex": ["desktop-current"],
                }},
                "__controller_targets__": {"controller-1": {"desktop_codex": {
                    "status": "active", "session_id": "desktop-current", "generation": 4,
                }}},
            }), encoding="utf-8")
            codex = root / "codex"
            codex.write_text(
                "#!/bin/sh\nprintf '%s\\n' 'thread desktop-current already has an active writer' >&2\nexit 1\n",
                encoding="utf-8",
            )
            codex.chmod(0o755)
            with patch.object(
                web_bridge, "preflight_native_resume", return_value=(True, "", {})
            ):
                attempt = web_bridge.execute_native_resume(
                    session_id="controller-1", repo=repo,
                    registry=registry, codex=str(codex),
                )
            self.assertEqual(attempt["state"], "RESUME_DEFERRED_ACTIVE_WRITER")
            self.assertIs(
                attempt["host_observation"],
                web_bridge._HOST_OBSERVED_CANONICAL_TARGET_FOREGROUND,
            )

    def test_desktop_codex_resolution_prefers_the_app_bundled_runtime(self) -> None:
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundled = root / "ChatGPT.app" / "Contents" / "Resources" / "codex"
            bundled.parent.mkdir(parents=True)
            bundled.write_text("#!/bin/sh\n", encoding="utf-8")
            bundled.chmod(0o755)
            path_cli = root / "homebrew" / "codex"
            path_cli.parent.mkdir(parents=True)
            path_cli.write_text("#!/bin/sh\n", encoding="utf-8")
            path_cli.chmod(0o755)

            with patch.object(web_bridge.shutil, "which", return_value=str(path_cli)):
                selected = web_bridge.resolve_desktop_codex_executable(
                    app_candidates=[bundled],
                    environ={},
                )

            self.assertEqual(selected, str(bundled.resolve()))

    def test_desktop_codex_resolution_rejects_an_invalid_explicit_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing-codex"
            with self.assertRaisesRegex(ValueError, "Desktop Codex executable"):
                web_bridge.resolve_desktop_codex_executable(
                    explicit=str(missing),
                    app_candidates=[],
                    environ={},
                )

    def test_rule_wake_resolves_desktop_runtime_only_for_desktop_target(self) -> None:
        from unittest.mock import patch

        lifecycle = {
            "pending_control_event": True,
            "rule_wake_policy": "immediate",
            "triggers": ["rule_update_pending:rev-new"],
            "snapshot": {"rule_handshake": {"installed_revision": "rev-new"}},
        }
        target = {
            "host": "desktop_codex",
            "execution_target_session_id": "desktop-current",
            "generation": 4,
            "ownership_generation": 4,
        }
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            web_bridge, "canonical_rule_wake_target", return_value=target
        ), patch.object(
            web_bridge,
            "resolve_desktop_codex_executable",
            return_value="/Applications/ChatGPT.app/Contents/Resources/codex",
        ) as resolve, patch.object(
            web_bridge, "maybe_schedule_rule_wake", return_value="scheduled"
        ) as schedule:
            result = web_bridge.schedule_guarded_rule_wake(
                lifecycle_state=lifecycle,
                session_id="controller-1",
                repo=Path(tmp),
                registry=Path(tmp) / "registry.json",
                codex=None,
                delay_seconds=1.0,
                state_path=Path(tmp) / "state.json",
            )

        resolve.assert_called_once_with(None)
        self.assertEqual(
            schedule.call_args.kwargs["codex"],
            "/Applications/ChatGPT.app/Contents/Resources/codex",
        )
        self.assertEqual(
            result["codex_executable"],
            "/Applications/ChatGPT.app/Contents/Resources/codex",
        )

    def test_rule_wake_does_not_require_desktop_runtime_for_web_target(self) -> None:
        from unittest.mock import patch

        lifecycle = {
            "pending_control_event": True,
            "rule_wake_policy": "immediate",
            "triggers": ["rule_update_pending:rev-new"],
            "snapshot": {"rule_handshake": {"installed_revision": "rev-new"}},
        }
        target = {
            "host": "web",
            "execution_target_session_id": "web-current",
            "generation": 4,
            "ownership_generation": 4,
        }
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            web_bridge, "canonical_rule_wake_target", return_value=target
        ), patch.object(
            web_bridge, "resolve_desktop_codex_executable"
        ) as resolve, patch.object(
            web_bridge, "maybe_schedule_rule_wake", return_value="scheduled"
        ) as schedule:
            result = web_bridge.schedule_guarded_rule_wake(
                lifecycle_state=lifecycle,
                session_id="controller-1",
                repo=Path(tmp),
                registry=Path(tmp) / "registry.json",
                codex=None,
                delay_seconds=1.0,
                state_path=Path(tmp) / "state.json",
            )

        resolve.assert_not_called()
        self.assertIsNone(schedule.call_args.kwargs["codex"])
        self.assertIsNone(result["codex_executable"])

    def test_execute_native_resume_reaps_process_group_after_codex_turn_completed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {
                    "controller-1": {"desktop_codex": ["controller-1"]}
                },
                "__controller_targets__": {"controller-1": {"desktop_codex": {
                    "status": "active", "session_id": "controller-1", "generation": 1,
                }}},
            }), encoding="utf-8")
            codex = root / "codex"
            codex.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = \"--version\" ]; then echo codex-test; exit 0; fi\n"
                "printf '%s\\n' '{\"type\":\"turn.completed\",\"usage\":{}}'\n"
                "sleep 30\n",
                encoding="utf-8",
            )
            codex.chmod(0o755)

            started = time.monotonic()
            attempt = web_bridge.execute_native_resume(
                session_id="controller-1",
                repo=repo,
                registry=registry,
                codex=str(codex),
                completion_grace_seconds=0.05,
                max_runtime_seconds=2.0,
            )

            self.assertLess(time.monotonic() - started, 1.5)
            self.assertEqual(attempt["result"], "CONFIRMED")
            self.assertEqual(attempt["state"], "RESUME_SUCCEEDED")
            self.assertEqual(attempt["completion_source"], "codex_turn_completed")
            self.assertEqual(attempt["returncode"], 0)
            self.assertIn('"type":"turn.completed"', attempt["stdout_tail"])

    def test_execute_native_resume_timeout_fails_closed_when_sigterm_handler_exits_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {
                    "controller-1": {"desktop_codex": ["controller-1"]}
                },
                "__controller_targets__": {"controller-1": {"desktop_codex": {
                    "status": "active", "session_id": "controller-1", "generation": 1,
                }}},
            }), encoding="utf-8")
            codex = root / "codex"
            codex.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = \"--version\" ]; then echo codex-test; exit 0; fi\n"
                "trap 'exit 0' TERM\n"
                "while :; do sleep 1; done\n",
                encoding="utf-8",
            )
            codex.chmod(0o755)

            attempt = web_bridge.execute_native_resume(
                session_id="controller-1",
                repo=repo,
                registry=registry,
                codex=str(codex),
                completion_grace_seconds=0.05,
                max_runtime_seconds=0.05,
            )

            self.assertEqual(attempt["result"], "FAILED")
            self.assertEqual(attempt["state"], "RESUME_FAILED")
            self.assertEqual(attempt["failure_class"], "native_resume_timeout")
            self.assertEqual(attempt["error_code"], "WEB_LIFECYCLE_RESUME_TIMEOUT")
            self.assertEqual(attempt["returncode"], 124)
            self.assertEqual(attempt["host_returncode"], 0)

    def test_execute_native_resume_reaps_children_after_completed_leader_exits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {
                    "controller-1": {"desktop_codex": ["controller-1"]}
                },
                "__controller_targets__": {"controller-1": {"desktop_codex": {
                    "status": "active", "session_id": "controller-1", "generation": 1,
                }}},
            }), encoding="utf-8")
            child_pid = root / "child.pid"
            codex = root / "codex"
            codex.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = \"--version\" ]; then echo codex-test; exit 0; fi\n"
                "sleep 3 &\n"
                f"printf '%s' \"$!\" > {child_pid}\n"
                "printf '%s\\n' '{\"type\":\"turn.completed\",\"usage\":{}}'\n"
                "exit 0\n",
                encoding="utf-8",
            )
            codex.chmod(0o755)

            started = time.monotonic()
            attempt = web_bridge.execute_native_resume(
                session_id="controller-1",
                repo=repo,
                registry=registry,
                codex=str(codex),
                completion_grace_seconds=0.05,
                max_runtime_seconds=2.0,
            )

            self.assertLess(time.monotonic() - started, 1.5)
            self.assertEqual(attempt["result"], "CONFIRMED")
            child = int(child_pid.read_text(encoding="utf-8"))
            with self.assertRaises(ProcessLookupError):
                os.kill(child, 0)

    def test_rule_wake_target_resolution_fails_closed_instead_of_falling_back_to_logical_controller(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {
                    "controller-1": {"web": ["web-old", "web-current"]}
                },
            }), encoding="utf-8")
            with self.assertRaisesRegex(PermissionError, "explicit current execution target"):
                web_bridge.canonical_rule_wake_target(
                    lifecycle_state={"controller_host": "desktop_codex"},
                    session_id="controller-1", repo=repo, registry=registry,
                )

    def test_rule_wake_rejects_explicit_target_without_canonical_execution_ownership(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {"controller-1": {"desktop_codex": ["desktop-current"]}},
                "__controller_targets__": {"controller-1": {"desktop_codex": {
                    "status": "active", "session_id": "desktop-current", "generation": 3,
                }}},
            }), encoding="utf-8")
            with self.assertRaisesRegex(PermissionError, "execution ownership"):
                web_bridge.canonical_rule_wake_target(
                    lifecycle_state={"controller_host": "desktop_codex"},
                    session_id="controller-1", repo=repo, registry=registry,
                )

    def test_rule_wake_rejects_legacy_recovery_target_without_trusted_host_origin_proof(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {"controller-1": {"web": ["web-stale"]}},
                "__controller_targets__": {"controller-1": {"web": {
                    "status": "active", "session_id": "web-stale", "generation": 2,
                    "provenance": "host_attested_same_controller_recovery",
                    "binding_mode": "resume_only",
                    "host_identity_receipt_sha256": "deadbeef",
                }}},
                "__controller_execution_ownership__": {"controller-1": {
                    "active_host": "web", "execution_target_session_id": "web-stale",
                    "generation": 2, "provenance": "web_entry",
                }},
            }), encoding="utf-8")
            with self.assertRaisesRegex(PermissionError, "trusted Host origin"):
                web_bridge.canonical_rule_wake_target(
                    lifecycle_state={"controller_host": "web"},
                    session_id="controller-1", repo=repo, registry=registry,
                )

    def test_rule_wake_schedule_immediate_is_ready_now(self) -> None:
        decision = web_bridge.rule_wake_schedule_decision({
            "rule_wake_policy": "immediate",
            "triggers": ["rule_update_pending:rev-new", "agent_session_terminal:T1"],
        })
        self.assertEqual(decision, "schedule_now")

    def test_rule_wake_schedule_after_event_waits_for_nonrule_control_work(self) -> None:
        waiting = web_bridge.rule_wake_schedule_decision({
            "rule_wake_policy": "after_event",
            "triggers": ["rule_update_pending:rev-new", "candidate_queue_changed"],
        })
        ready = web_bridge.rule_wake_schedule_decision({
            "rule_wake_policy": "after_event",
            "triggers": ["rule_update_pending:rev-new"],
        })
        self.assertEqual(waiting, "wait_for_event")
        self.assertEqual(ready, "schedule_now")

    def test_rule_wake_schedule_next_turn_never_forces_resume(self) -> None:
        decision = web_bridge.rule_wake_schedule_decision({
            "rule_wake_policy": "next_turn",
            "triggers": ["rule_update_pending:rev-docs"],
        })
        self.assertEqual(decision, "natural_turn")

    def test_rule_wake_scheduler_uses_same_controller_and_exact_revision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "repo"
            repo.mkdir()
            registry = base / "controllers.json"
            registry.write_text(json.dumps({
            "controller-1": str(repo.resolve()),
            "__controller_sessions__": {"controller-1": {"web": ["web-session-1"]}},
        }), encoding="utf-8")
            state_path = base / "auto-stop.json"
            capture = base / "capture.json"
            result = web_bridge.maybe_schedule_rule_wake(
                lifecycle_state={
                    "rule_wake_policy": "immediate",
                    "triggers": ["rule_update_pending:rev-new"],
                    "snapshot": {"rule_handshake": {"installed_revision": "rev-new"}},
                },
                session_id="controller-1", repo=repo, registry=registry, codex="/opt/homebrew/bin/codex",
                delay_seconds=0, state_path=state_path, capture_path=capture, runtime_path="/opt/homebrew/bin:/usr/bin:/bin",
            )
            self.assertEqual(result, "scheduled")
            command = json.loads(capture.read_text(encoding="utf-8"))
            self.assertIn("controller-1", command)
            self.assertIn("rule-update:rev-new", command)

    def test_rule_wake_scheduler_does_not_force_next_turn_or_mid_event_after_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "repo"
            repo.mkdir()
            registry = base / "controllers.json"
            registry.write_text(json.dumps({"controller-1": str(repo.resolve())}), encoding="utf-8")
            for name, lifecycle_state in {
                "next": {"rule_wake_policy": "next_turn", "triggers": ["rule_update_pending:rev-docs"]},
                "after": {"rule_wake_policy": "after_event", "triggers": ["rule_update_pending:rev-new", "candidate_queue_changed"]},
            }.items():
                capture = base / f"{name}.json"
                result = web_bridge.maybe_schedule_rule_wake(
                    lifecycle_state=lifecycle_state,
                    session_id="controller-1", repo=repo, registry=registry, codex="/opt/homebrew/bin/codex",
                    delay_seconds=0, state_path=base / f"{name}.state.json", capture_path=capture, runtime_path="/opt/homebrew/bin:/usr/bin:/bin",
                )
                self.assertNotEqual(result, "scheduled")
                self.assertFalse(capture.exists())

    def test_audit_once_schedules_native_stop_after_allowed_guard_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = tmp_path / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {"controller-1": {"web": ["web-session-1"]}},
            }), encoding="utf-8")
            _provision_verified_current_web_target(registry, web_session_id="web-session-1")
            audit = tmp_path / "audit.jsonl"
            receipt = {
                "receiptId": "guard-auto-stop-1",
                "childTool": "shell_command",
                "state": "succeeded",
                "rootLabel": str(repo),
                "targetLabel": GUARD_COMMAND,
                "detail": (
                    f"命令：{GUARD_COMMAND}"
                    f" · 工作目录：{repo}\n\n命令输出：\n"
                    "control-event: allowed; declared decisions are complete\n"
                ),
            }
            audit.write_text(json.dumps(receipt) + "\n", encoding="utf-8")
            cursor = tmp_path / "cursor.json"
            capture = tmp_path / "auto-stop.json"
            state = tmp_path / "auto-stop-state.json"

            result = self.run_bridge(
                "audit-once",
                "--session-id", "controller-1",
                "--repo", str(repo),
                "--audit-log", str(audit),
                "--cursor", str(cursor),
                "--registry", str(registry),
                "--web-session-id", "web-session-1",
                "--auto-native-stop",
                "--auto-stop-delay-seconds", "5",
                "--auto-stop-state", str(state),
                "--capture-auto-stop", str(capture),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            scheduled = json.loads(capture.read_text(encoding="utf-8"))
            self.assertIn("auto-native-stop", scheduled)
            self.assertIn("controller-1", scheduled)
            self.assertIn(str(repo.resolve()), scheduled)
            self.assertIn("guard-auto-stop-1", scheduled)

    def test_audit_once_ignores_non_guard_shell_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            registry = tmp_path / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {"controller-1": {"web": ["web-session-1"]}},
            }), encoding="utf-8")
            _provision_verified_current_web_target(registry, web_session_id="web-session-1")
            audit = tmp_path / "audit.jsonl"
            receipt = {
                "receiptId": "shell-1",
                "childTool": "shell_command",
                "state": "succeeded",
                "rootLabel": str(repo),
                "targetLabel": "git status --short",
                "detail": f"命令：git status --short · 工作目录：{repo}",
            }
            audit.write_text(json.dumps(receipt) + "\n", encoding="utf-8")
            cursor = tmp_path / "cursor.json"
            capture = tmp_path / "events.jsonl"

            result = self.run_bridge(
                "audit-once",
                "--session-id",
                "controller-1",
                "--repo",
                str(repo),
                "--registry", str(registry),
                "--web-session-id", "web-session-1",
                "--audit-log",
                str(audit),
                "--cursor",
                str(cursor),
                "--capture-events",
                str(capture),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(capture.exists())
            self.assertEqual(json.loads(cursor.read_text())["offset"], audit.stat().st_size)


class WebLifecycleComputerLeaseTests(unittest.TestCase):
    def run_bridge(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["/usr/bin/python3", str(BRIDGE), *args],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_audit_once_never_uses_manual_resume_lease_as_caller_identity(self) -> None:
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"; repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {"controller-1": {"web": ["web-session-1"]}},
                "__controller_targets__": {"controller-1": {"web": {
                    "status": "active", "session_id": "web-session-1", "generation": 1,
                    "provenance": "host_attested_same_controller_recovery",
                    "binding_mode": "resume_only", "identity_proof": "host_attested_origin",
                }}},
                "__controller_execution_ownership__": {"controller-1": {
                    "active_host": "web", "execution_target_session_id": "web-session-1",
                    "generation": 1, "provenance": "web_entry",
                }},
            }), encoding="utf-8")
            lease_file = root / "manual-leases.json"
            lease_file.write_text(json.dumps({
                "schema_version": 1, "leases": {"controller-1": {
                    "repo": str(repo.resolve()), "controller_id": "controller-1",
                    "web_session_id": "web-session-1", "authorized_at_unix": 1,
                    "expires_at_unix": 4102444800, "provenance": "manual_user_authorized", "mode": "resume_only",
                }}
            }), encoding="utf-8")
            audit = root / "audit.jsonl"; audit.write_text("", encoding="utf-8")
            cursor = root / "cursor.json"
            with patch.object(web_bridge, "DEFAULT_MANUAL_WEB_LEASES", lease_file):
                code = web_bridge.main([
                    "audit-once", "--session-id", "controller-1", "--repo", str(repo),
                    "--registry", str(registry), "--audit-log", str(audit), "--cursor", str(cursor),
                ])
            self.assertEqual(code, 78)
            self.assertFalse(cursor.exists())

    def test_audit_once_refuses_registered_repo_without_verified_web_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {"controller-1": {"web": ["web-session-1"]}},
            }), encoding="utf-8")
            audit = root / "audit.jsonl"
            audit.write_text("", encoding="utf-8")
            cursor = root / "cursor.json"

            result = self.run_bridge(
                "audit-once", "--session-id", "controller-1", "--repo", str(repo),
                "--registry", str(registry), "--audit-log", str(audit), "--cursor", str(cursor),
            )

            self.assertEqual(result.returncode, 78)
            self.assertIn("explicit Web session identity", result.stderr)

    def test_audit_once_ignores_computer_lease_bound_to_different_web_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"; repo.mkdir()
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {"controller-1": {"web": ["web-session-1", "web-session-2"]}},
                "__controller_targets__": {
                    "controller-1": {
                        "web": {
                            "status": "active",
                            "session_id": "web-session-1",
                            "generation": 2,
                        }
                    }
                },
            }), encoding="utf-8")
            audit = root / "audit.jsonl"
            audit.write_text(json.dumps({
                "receiptId":"computer-mismatch", "childTool":"computer", "state":"succeeded",
                "targetLabel":"Google Chrome", "detail":"电脑操作：click · 应用 Google Chrome"
            }) + "\n", encoding="utf-8")
            cursor = root / "cursor.json"
            capture = root / "events.jsonl"
            lease = root / "lease.json"
            lease.write_text(json.dumps({
                "session_id":"controller-1", "web_session_id":"web-session-2",
                "repo":str(repo.resolve()), "expires_at_unix_ms":4102444800000, "remaining_uses":1
            }), encoding="utf-8")

            result = self.run_bridge(
                "audit-once", "--session-id", "controller-1", "--repo", str(repo),
                "--registry", str(registry), "--web-session-id", "web-session-1",
                "--audit-log", str(audit), "--cursor", str(cursor),
                "--computer-lease", str(lease), "--capture-events", str(capture),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(capture.exists())
            self.assertTrue(lease.exists())

    def test_audit_once_ignores_computer_without_valid_lease(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            registry = tmp_path / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {"controller-1": {"web": ["web-session-1"]}},
            }), encoding="utf-8")
            _provision_verified_current_web_target(registry, web_session_id="web-session-1")
            audit = tmp_path / "audit.jsonl"
            audit.write_text(json.dumps({
                "receiptId":"computer-1", "childTool":"computer", "state":"succeeded",
                "targetLabel":"Google Chrome", "detail":"电脑操作：get_app_state · 应用 Google Chrome"
            }) + "\n", encoding="utf-8")
            cursor = tmp_path / "cursor.json"
            capture = tmp_path / "events.jsonl"
            lease = tmp_path / "lease.json"

            result = self.run_bridge(
                "audit-once", "--session-id", "controller-1", "--repo", str(repo),
                "--registry", str(registry), "--web-session-id", "web-session-1",
                "--audit-log", str(audit), "--cursor", str(cursor),
                "--computer-lease", str(lease), "--capture-events", str(capture),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(capture.exists())

    def test_computer_click_event_declares_non_user_followup_to_read_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"; repo.mkdir()
            lease = root / "lease.json"
            lease.write_text(json.dumps({
                "session_id": "controller-1", "web_session_id": "web-session-1",
                "repo": str(repo.resolve()), "issued_at_unix_ms": 1000,
                "expires_at_unix_ms": 9999999999999, "remaining_uses": 1,
            }), encoding="utf-8")
            event = web_bridge.computer_event_from_receipt(
                {
                    "receiptId": "claim-click-1", "childTool": "computer", "state": "succeeded",
                    "targetLabel": "Google Chrome", "detail": "电脑操作：click · 应用 Google Chrome",
                    "occurredAtUnixMs": 2000,
                },
                session_id="controller-1", repo=repo, lease_path=lease, web_session_id="web-session-1",
                now_unix_ms=3000,
            )
            self.assertIsNotNone(event)
            self.assertEqual(event["next_action"], "observe and read the result of the computer action before yielding")
            self.assertFalse(event["requires_user"])

    def test_computer_mutation_with_screen_word_still_requires_followup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"; repo.mkdir()
            lease = root / "lease.json"
            lease.write_text(json.dumps({
                "session_id": "controller-1", "web_session_id": "web-session-1",
                "repo": str(repo.resolve()), "issued_at_unix_ms": 1000,
                "expires_at_unix_ms": 9999999999999, "remaining_uses": 1,
            }), encoding="utf-8")
            event = web_bridge.computer_event_from_receipt(
                {
                    "receiptId": "claim-click-onscreen-1", "childTool": "computer", "state": "succeeded",
                    "targetLabel": "Google Chrome", "detail": "电脑操作：click onscreen button · 应用 Google Chrome",
                    "occurredAtUnixMs": 2000,
                },
                session_id="controller-1", repo=repo, lease_path=lease, web_session_id="web-session-1",
                now_unix_ms=3000,
            )
            self.assertIsNotNone(event)
            self.assertEqual(event["next_action"], "observe and read the result of the computer action before yielding")
            self.assertFalse(event["requires_user"])

    def test_computer_exact_observation_does_not_create_followup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"; repo.mkdir()
            lease = root / "lease.json"
            lease.write_text(json.dumps({
                "session_id": "controller-1", "web_session_id": "web-session-1",
                "repo": str(repo.resolve()), "issued_at_unix_ms": 1000,
                "expires_at_unix_ms": 9999999999999, "remaining_uses": 1,
            }), encoding="utf-8")
            event = web_bridge.computer_event_from_receipt(
                {
                    "receiptId": "observe-state-1", "childTool": "computer", "state": "succeeded",
                    "targetLabel": "Google Chrome", "detail": "电脑操作：get_app_state · 应用 Google Chrome",
                    "occurredAtUnixMs": 2000,
                },
                session_id="controller-1", repo=repo, lease_path=lease, web_session_id="web-session-1",
                now_unix_ms=3000,
            )
            self.assertIsNotNone(event)
            self.assertNotIn("next_action", event)
            self.assertNotIn("requires_user", event)

    def test_audit_once_consumes_one_computer_event_from_valid_lease(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            registry = tmp_path / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {"controller-1": {"web": ["web-session-1"]}},
            }), encoding="utf-8")
            _provision_verified_current_web_target(registry, web_session_id="web-session-1")
            audit = tmp_path / "audit.jsonl"
            audit.write_text(json.dumps({
                "receiptId":"computer-2", "childTool":"computer", "state":"succeeded",
                "targetLabel":"Google Chrome", "detail":"电脑操作：click · 应用 Google Chrome"
            }) + "\n", encoding="utf-8")
            cursor = tmp_path / "cursor.json"
            capture = tmp_path / "events.jsonl"
            lease = tmp_path / "lease.json"
            lease.write_text(json.dumps({
                "session_id":"controller-1", "web_session_id":"web-session-1",
                "repo":str(repo.resolve()), "expires_at_unix_ms":4102444800000, "remaining_uses":1
            }), encoding="utf-8")

            result = self.run_bridge(
                "audit-once", "--session-id", "controller-1", "--repo", str(repo),
                "--registry", str(registry), "--web-session-id", "web-session-1",
                "--audit-log", str(audit), "--cursor", str(cursor),
                "--computer-lease", str(lease), "--capture-events", str(capture),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            event = json.loads(capture.read_text().splitlines()[0])
            self.assertEqual(event["tool_name"], "AI-Bridge.computer")
            self.assertEqual(event["controller_host"], "web")
            self.assertEqual(event["controller_session_id"], "controller-1")
            self.assertEqual(event["web_session_id"], "web-session-1")
            self.assertIn("click", event["tool_input"]["detail"])
            self.assertFalse(lease.exists())

    def test_arm_computer_refuses_registered_repo_without_verified_web_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {"controller-1": {"web": ["web-session-1"]}},
            }), encoding="utf-8")
            lease = root / "lease.json"

            result = self.run_bridge(
                "arm-computer", "--cwd", str(repo), "--registry", str(registry), "--lease", str(lease),
            )

            self.assertEqual(result.returncode, 78)
            self.assertIn("verified Web Controller Session identity", result.stderr)
            self.assertFalse(lease.exists())

    def test_arm_computer_resolves_registered_controller_and_writes_bounded_lease(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = tmp_path / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {"controller-1": {"web": ["web-session-1"]}},
            }), encoding="utf-8")
            _provision_verified_current_web_target(registry, web_session_id="web-session-1")
            lease = tmp_path / "lease.json"

            result = self.run_bridge(
                "arm-computer", "--cwd", str(repo), "--registry", str(registry),
                "--web-session-id", "web-session-1", "--lease", str(lease),
                "--ttl-seconds", "90", "--uses", "2",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            value=json.loads(lease.read_text())
            self.assertEqual(value["session_id"], "controller-1")
            self.assertEqual(value["web_session_id"], "web-session-1")
            self.assertEqual(value["remaining_uses"], 2)
            self.assertGreater(value["expires_at_unix_ms"], value["issued_at_unix_ms"])


class TerminalReceiptResumeContextTests(unittest.TestCase):
    def test_native_resume_prompt_names_persisted_non_user_next_action(self) -> None:
        command = web_bridge.native_resume_command(
            codex="/usr/bin/codex", session_id="controller-1", repo=Path("/tmp/project"),
            next_action="read the claimed task result and continue",
        )
        self.assertIn("read the claimed task result and continue", command[-1])
        self.assertIn("before yielding", command[-1])

    def test_native_resume_prompt_names_pending_terminal_receipt(self) -> None:
        receipt = "/tmp/reviewer-terminal.json"
        command = web_bridge.native_resume_command(
            codex="/usr/bin/codex", session_id="controller-1", repo=Path("/tmp/project"),
            terminal_receipts=[receipt],
        )
        self.assertIn(receipt, command[-1])


class WebLifecycleNativeStopTests(unittest.TestCase):
    def run_bridge(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["/usr/bin/python3", str(BRIDGE), *args],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_native_stop_dry_run_reuses_exact_registered_thread(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path=Path(tmp)
            repo=tmp_path/"repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry=tmp_path/"controllers.json"
            registry.write_text(json.dumps({"controller-1":str(repo.resolve())}), encoding="utf-8")

            result=self.run_bridge(
                "native-stop", "--session-id", "controller-1", "--repo", str(repo),
                "--registry", str(registry), "--codex", "/opt/homebrew/bin/codex", "--dry-run"
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            argv=json.loads(result.stdout)
            self.assertEqual(
                argv[:7],
                [
                    "/opt/homebrew/bin/codex", "exec", "--json", "-C",
                    str(repo.resolve()), "resume", "controller-1",
                ],
            )
            self.assertNotIn("fork", argv)

    def test_native_stop_dry_run_targets_explicit_current_desktop_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = tmp_path / "controllers.json"
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
                            "generation": 4,
                        }
                    }
                },
            }), encoding="utf-8")

            result = self.run_bridge(
                "native-stop",
                "--session-id",
                "controller-old",
                "--repo",
                str(repo),
                "--registry",
                str(registry),
                "--codex",
                "/opt/homebrew/bin/codex",
                "--dry-run",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            argv = json.loads(result.stdout)
            self.assertEqual(argv[5], "resume")
            self.assertEqual(argv[6], "desktop-current")
            self.assertNotIn("controller-old", argv[5:7])

    def test_native_stop_missing_lifecycle_state_does_not_direct_resume(self) -> None:
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({"controller-1": str(repo.resolve())}), encoding="utf-8")
            codex = root / "codex"
            codex.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            codex.chmod(0o755)
            with patch.object(web_bridge, "_load_lifecycle_state", return_value={}), patch.object(
                web_bridge, "dispatch_pending_lifecycle_wake", return_value=None
            ), patch.object(
                web_bridge, "preflight_native_resume", side_effect=AssertionError("direct resume must not run")
            ):
                code = web_bridge.main([
                    "native-stop", "--session-id", "controller-1", "--repo", str(repo),
                    "--registry", str(registry), "--codex", str(codex),
                ])
            self.assertNotEqual(code, 0)

    def test_auto_native_stop_skips_stale_superseded_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = tmp_path / "controllers.json"
            registry.write_text(json.dumps({"controller-1": str(repo.resolve())}), encoding="utf-8")
            state = tmp_path / "auto-stop.json"
            state.write_text(json.dumps({"receipt_id": "newer-receipt"}), encoding="utf-8")

            result = self.run_bridge(
                "auto-native-stop",
                "--session-id", "controller-1",
                "--repo", str(repo),
                "--receipt-id", "older-receipt",
                "--registry", str(registry),
                "--state", str(state),
                "--delay-seconds", "0",
                "--codex", "/definitely/not/a/codex/binary",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("completed_at_unix_ms", json.loads(state.read_text()))

    def test_native_stop_rejects_session_not_registered_for_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path=Path(tmp)
            repo=tmp_path/"repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry=tmp_path/"controllers.json"
            registry.write_text(json.dumps({"other-controller":str(repo.resolve())}), encoding="utf-8")

            result=self.run_bridge(
                "native-stop", "--session-id", "controller-1", "--repo", str(repo),
                "--registry", str(registry), "--dry-run"
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("registered controller", result.stderr)


class WebAutoStopSupervisorCoalescingTests(unittest.TestCase):
    def make_paths(self, root: Path) -> tuple[Path, Path, Path]:
        repo = root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
        registry = root / "controllers.json"
        registry.write_text(json.dumps({"controller-1": str(repo.resolve())}), encoding="utf-8")
        state = root / "auto-stop.json"
        return repo, registry, state

    def test_host_neutral_supervisor_omits_missing_desktop_codex_argument(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, registry, state = self.make_paths(Path(tmp))
            capture = Path(tmp) / "command.json"

            self.assertTrue(web_bridge.schedule_auto_native_stop(
                session_id="controller-1",
                repo=repo,
                receipt_id="r1",
                registry=registry,
                codex=None,
                delay_seconds=1,
                state_path=state,
                capture_path=capture,
            ))

            command = json.loads(capture.read_text(encoding="utf-8"))
            self.assertNotIn(None, command)
            self.assertNotIn("--codex", command)

    def test_same_receipt_live_supervisor_is_coalesced(self) -> None:
        from unittest.mock import Mock, patch
        with tempfile.TemporaryDirectory() as tmp:
            repo, registry, state = self.make_paths(Path(tmp))
            with patch.object(web_bridge.subprocess, "Popen", return_value=Mock(pid=1111)) as popen, patch.object(
                web_bridge, "_pid_is_alive", return_value=True
            ):
                self.assertTrue(web_bridge.schedule_auto_native_stop(
                    session_id="controller-1", repo=repo, receipt_id="r1", registry=registry,
                    codex="codex", delay_seconds=32, state_path=state,
                ))
                token = json.loads(state.read_text())["supervisor_token"]
                self.assertFalse(web_bridge.schedule_auto_native_stop(
                    session_id="controller-1", repo=repo, receipt_id="r1", registry=registry,
                    codex="codex", delay_seconds=32, state_path=state,
                ))
            self.assertEqual(popen.call_count, 1)
            saved = json.loads(state.read_text())
            self.assertEqual(saved["supervisor_token"], token)
            self.assertEqual(saved["supervisor_pid"], 1111)
            self.assertEqual(saved["coalesced_schedule_count"], 1)

    def test_new_receipt_supersedes_live_old_supervisor(self) -> None:
        from unittest.mock import Mock, patch
        with tempfile.TemporaryDirectory() as tmp:
            repo, registry, state = self.make_paths(Path(tmp))
            with patch.object(
                web_bridge.subprocess, "Popen", side_effect=[Mock(pid=1111), Mock(pid=2222)]
            ) as popen, patch.object(web_bridge, "_pid_is_alive", return_value=True):
                web_bridge.schedule_auto_native_stop(
                    session_id="controller-1", repo=repo, receipt_id="r1", registry=registry,
                    codex="codex", delay_seconds=32, state_path=state,
                )
                first_token = json.loads(state.read_text())["supervisor_token"]
                self.assertTrue(web_bridge.schedule_auto_native_stop(
                    session_id="controller-1", repo=repo, receipt_id="r2", registry=registry,
                    codex="codex", delay_seconds=1, state_path=state,
                ))
            self.assertEqual(popen.call_count, 2)
            saved = json.loads(state.read_text())
            self.assertEqual(saved["receipt_id"], "r2")
            self.assertEqual(saved["supervisor_pid"], 2222)
            self.assertNotEqual(saved["supervisor_token"], first_token)

    def test_only_current_supervisor_token_can_force_rearm(self) -> None:
        from unittest.mock import Mock, patch
        with tempfile.TemporaryDirectory() as tmp:
            repo, registry, state = self.make_paths(Path(tmp))
            with patch.object(
                web_bridge.subprocess, "Popen", side_effect=[Mock(pid=1111), Mock(pid=2222)]
            ) as popen, patch.object(web_bridge, "_pid_is_alive", return_value=True):
                web_bridge.schedule_auto_native_stop(
                    session_id="controller-1", repo=repo, receipt_id="r1", registry=registry,
                    codex="codex", delay_seconds=5, state_path=state,
                )
                token = json.loads(state.read_text())["supervisor_token"]
                self.assertFalse(web_bridge.schedule_auto_native_stop(
                    session_id="controller-1", repo=repo, receipt_id="r1", registry=registry,
                    codex="codex", delay_seconds=5, state_path=state,
                    force_rearm=True, replace_supervisor_token="wrong-token",
                ))
                self.assertTrue(web_bridge.schedule_auto_native_stop(
                    session_id="controller-1", repo=repo, receipt_id="r1", registry=registry,
                    codex="codex", delay_seconds=5, state_path=state,
                    force_rearm=True, replace_supervisor_token=token,
                ))
            self.assertEqual(popen.call_count, 2)
            saved = json.loads(state.read_text())
            self.assertEqual(saved["supervisor_pid"], 2222)
            self.assertNotEqual(saved["supervisor_token"], token)

    def test_current_token_web_rearm_hands_off_with_force_rearm_proof(self) -> None:
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            repo, registry, state = self.make_paths(Path(tmp))
            token = "current-token"
            state.write_text(json.dumps({
                "receipt_id": "r1",
                "session_id": "controller-1",
                "repo": str(repo.resolve()),
                "state": "RESUME_PENDING",
                "pending_control_event": True,
                "supervisor_receipt_id": "r1",
                "supervisor_token": token,
                "supervisor_pid": 999,
            }), encoding="utf-8")
            lifecycle = {
                "pending_control_event": True,
                "requires_user": False,
                "controller_host": "web",
                "wake_generation": 1,
            }
            deferred = {
                "operation": "web_reentry",
                "result": "DEFERRED",
                "state": "WEB_REENTRY_DEFERRED_ACTIVE",
                "returncode": 0,
                "failure_class": "web_host_active",
            }
            with patch.object(
                web_bridge, "_load_lifecycle_state", return_value=lifecycle
            ), patch.object(
                web_bridge, "execute_web_reentry", return_value=deferred
            ), patch.object(
                web_bridge, "_schedule_auto_native_stop_locked", return_value=True
            ) as schedule:
                code = web_bridge.run_auto_native_stop(
                    session_id="controller-1",
                    repo=repo,
                    receipt_id="r1",
                    registry=registry,
                    codex="codex",
                    delay_seconds=0,
                    state_path=state,
                    supervisor_token=token,
                )
            self.assertEqual(code, 0)
            schedule.assert_called_once()
            self.assertTrue(schedule.call_args.kwargs["force_rearm"])
            self.assertEqual(
                schedule.call_args.kwargs["replace_supervisor_token"], token
            )

    def test_locked_supervisor_rearm_uses_locked_scheduler_without_recursive_lock(self) -> None:
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            state = root / "state.json"
            registry = root / "registry.json"
            with (
                patch.object(web_bridge, "_schedule_auto_native_stop_locked", return_value=True) as locked,
                patch.object(
                    web_bridge,
                    "schedule_auto_native_stop",
                    side_effect=AssertionError("locked rearm must not acquire supervisor lock again"),
                ),
            ):
                result = web_bridge._rearm_auto_native_stop(
                    session_id="web-1",
                    repo=repo,
                    receipt_id="r1",
                    registry=registry,
                    codex="codex",
                    delay_seconds=1.0,
                    state_path=state,
                    runtime_path=None,
                    supervisor_token="token-1",
                    supervisor_lock_held=True,
                )
            self.assertTrue(result)
            locked.assert_called_once()
            kwargs = locked.call_args.kwargs
            self.assertTrue(kwargs["force_rearm"])
            self.assertEqual(kwargs["replace_supervisor_token"], "token-1")

    def test_new_receipt_can_supersede_after_old_validation_before_old_wake(self) -> None:
        import threading
        from unittest.mock import Mock, patch

        with tempfile.TemporaryDirectory() as tmp:
            repo, registry, state = self.make_paths(Path(tmp))
            old_token = "old-token"
            state.write_text(json.dumps({
                "receipt_id": "r1",
                "session_id": "controller-1",
                "repo": str(repo.resolve()),
                "state": "RESUME_PENDING",
                "pending_control_event": True,
                "supervisor_receipt_id": "r1",
                "supervisor_token": old_token,
                "supervisor_pid": 1111,
            }), encoding="utf-8")
            validated = threading.Event()
            release_old = threading.Event()
            scheduled = threading.Event()
            old_result: list[int] = []
            new_result: list[bool] = []

            def paused_lifecycle(_session_id: str) -> dict[str, object]:
                validated.set()
                self.assertTrue(release_old.wait(2), "test did not release old supervisor")
                return {
                    "pending_control_event": True,
                    "requires_user": False,
                    "controller_host": "web",
                    "wake_generation": 1,
                }

            def run_old() -> None:
                old_result.append(web_bridge.run_auto_native_stop(
                    session_id="controller-1", repo=repo, receipt_id="r1", registry=registry,
                    codex="codex", delay_seconds=0, state_path=state, supervisor_token=old_token,
                ))

            def install_new() -> None:
                try:
                    new_result.append(web_bridge.schedule_auto_native_stop(
                        session_id="controller-1", repo=repo, receipt_id="r2", registry=registry,
                        codex="codex", delay_seconds=1, state_path=state,
                    ))
                finally:
                    scheduled.set()

            old_thread = threading.Thread(target=run_old)
            new_thread = threading.Thread(target=install_new)
            try:
                with patch.object(web_bridge, "_load_lifecycle_state", side_effect=paused_lifecycle), patch.object(
                    web_bridge, "execute_web_reentry", side_effect=AssertionError("superseded old supervisor must not wake")
                ), patch.object(web_bridge.subprocess, "Popen", return_value=Mock(pid=2222)), patch.object(
                    web_bridge, "_pid_is_alive", return_value=True
                ):
                    old_thread.start()
                    self.assertTrue(validated.wait(1), "old supervisor never reached post-validation pause")
                    new_thread.start()
                    self.assertTrue(
                        scheduled.wait(0.5),
                        "new receipt must be installable while old supervisor is paused after validation",
                    )
                    saved_after_supersede = json.loads(state.read_text(encoding="utf-8"))
                    self.assertEqual(saved_after_supersede["receipt_id"], "r2")
                    self.assertEqual(saved_after_supersede["supervisor_receipt_id"], "r2")
                    new_token = saved_after_supersede["supervisor_token"]
            finally:
                release_old.set()
                if old_thread.ident is not None:
                    old_thread.join(2)
                if new_thread.ident is not None:
                    new_thread.join(2)

            self.assertEqual(old_result, [0])
            self.assertEqual(new_result, [True])
            final = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(final["receipt_id"], "r2")
            self.assertEqual(final["supervisor_receipt_id"], "r2")
            self.assertEqual(final["supervisor_token"], new_token)
            self.assertEqual(final["supervisor_pid"], 2222)

    def test_same_receipt_new_token_can_supersede_after_old_validation_before_old_wake(self) -> None:
        import threading
        from unittest.mock import Mock, patch

        with tempfile.TemporaryDirectory() as tmp:
            repo, registry, state = self.make_paths(Path(tmp))
            old_token = "old-token"
            state.write_text(json.dumps({
                "receipt_id": "r1",
                "session_id": "controller-1",
                "repo": str(repo.resolve()),
                "state": "RESUME_PENDING",
                "pending_control_event": True,
                "supervisor_receipt_id": "r1",
                "supervisor_token": old_token,
                "supervisor_pid": 1111,
            }), encoding="utf-8")
            validated = threading.Event()
            release_old = threading.Event()
            scheduled = threading.Event()

            def paused_lifecycle(_session_id: str) -> dict[str, object]:
                validated.set()
                self.assertTrue(release_old.wait(2), "test did not release old supervisor")
                return {
                    "pending_control_event": True,
                    "requires_user": False,
                    "controller_host": "web",
                    "wake_generation": 1,
                }

            old_result: list[int] = []
            new_result: list[bool] = []
            def run_old() -> None:
                old_result.append(web_bridge.run_auto_native_stop(
                    session_id="controller-1", repo=repo, receipt_id="r1", registry=registry,
                    codex="codex", delay_seconds=0, state_path=state, supervisor_token=old_token,
                ))
            def replace_token() -> None:
                try:
                    new_result.append(web_bridge.schedule_auto_native_stop(
                        session_id="controller-1", repo=repo, receipt_id="r1", registry=registry,
                        codex="codex", delay_seconds=1, state_path=state, force_rearm=True,
                        replace_supervisor_token=old_token,
                    ))
                finally:
                    scheduled.set()

            old_thread = threading.Thread(target=run_old)
            new_thread = threading.Thread(target=replace_token)
            try:
                with patch.object(web_bridge, "_load_lifecycle_state", side_effect=paused_lifecycle), patch.object(
                    web_bridge, "execute_web_reentry", side_effect=AssertionError("superseded old token must not wake")
                ), patch.object(web_bridge.subprocess, "Popen", return_value=Mock(pid=3333)), patch.object(
                    web_bridge, "_pid_is_alive", return_value=True
                ):
                    old_thread.start()
                    self.assertTrue(validated.wait(1))
                    new_thread.start()
                    self.assertTrue(
                        scheduled.wait(0.5),
                        "replacement token must be installable while old supervisor is paused after validation",
                    )
                    superseded = json.loads(state.read_text(encoding="utf-8"))
                    self.assertEqual(superseded["receipt_id"], "r1")
                    self.assertNotEqual(superseded["supervisor_token"], old_token)
                    new_token = superseded["supervisor_token"]
            finally:
                release_old.set()
                if old_thread.ident is not None:
                    old_thread.join(2)
                if new_thread.ident is not None:
                    new_thread.join(2)

            self.assertEqual(old_result, [0])
            self.assertEqual(new_result, [True])
            final = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(final["receipt_id"], "r1")
            self.assertEqual(final["supervisor_token"], new_token)
            self.assertEqual(final["supervisor_pid"], 3333)

    def _assert_web_supersession_prevents_stale_attempt_and_rearm(self, attempted_result: dict[str, object]) -> None:
        import threading
        from unittest.mock import Mock, patch

        with tempfile.TemporaryDirectory() as tmp:
            repo, registry, state = self.make_paths(Path(tmp))
            old_token = "old-token"
            state.write_text(json.dumps({
                "receipt_id": "r1", "session_id": "controller-1", "repo": str(repo.resolve()),
                "state": "RESUME_PENDING", "pending_control_event": True,
                "supervisor_receipt_id": "r1", "supervisor_token": old_token, "supervisor_pid": 1111,
            }), encoding="utf-8")
            validated = threading.Event()
            release_old = threading.Event()
            old_done = threading.Event()

            def paused_lifecycle(_session_id: str) -> dict[str, object]:
                validated.set()
                release_old.wait(2)
                return {
                    "pending_control_event": True, "requires_user": False,
                    "controller_host": "web", "wake_generation": 1,
                }

            execute = Mock(return_value=attempted_result)
            rearm = Mock(side_effect=AssertionError("superseded supervisor must not rearm"))
            old_result: list[int] = []

            def run_old() -> None:
                try:
                    old_result.append(web_bridge.run_auto_native_stop(
                        session_id="controller-1", repo=repo, receipt_id="r1", registry=registry,
                        codex="codex", delay_seconds=0, state_path=state, supervisor_token=old_token,
                    ))
                finally:
                    old_done.set()

            old_thread = threading.Thread(target=run_old)
            try:
                with patch.object(web_bridge, "_load_lifecycle_state", side_effect=paused_lifecycle), patch.object(
                    web_bridge, "execute_web_reentry", execute
                ), patch.object(web_bridge, "_rearm_auto_native_stop", rearm), patch.object(
                    web_bridge.subprocess, "Popen", return_value=Mock(pid=2222)
                ), patch.object(web_bridge, "_pid_is_alive", return_value=True):
                    old_thread.start()
                    self.assertTrue(validated.wait(1))
                    self.assertTrue(web_bridge.schedule_auto_native_stop(
                        session_id="controller-1", repo=repo, receipt_id="r1", registry=registry,
                        codex="codex", delay_seconds=1, state_path=state, force_rearm=True,
                        replace_supervisor_token=old_token,
                    ))
                    superseded = json.loads(state.read_text(encoding="utf-8"))
                    new_token = superseded["supervisor_token"]
                    release_old.set()
                    self.assertTrue(old_done.wait(2))
            finally:
                release_old.set()
                old_thread.join(2)
            self.assertEqual(old_result, [0])
            execute.assert_not_called()
            rearm.assert_not_called()
            final = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(final["supervisor_token"], new_token)
            self.assertEqual(final["supervisor_pid"], 2222)

    def test_transient_web_reentry_retry_does_not_rearm_after_supersession(self) -> None:
        self._assert_web_supersession_prevents_stale_attempt_and_rearm({
            "result": "FAILED", "state": "WEB_REENTRY_PENDING", "returncode": 78,
            "failure_class": "web_reentry_unavailable", "error_code": "WEB_REENTRY_UNAVAILABLE",
        })

    def test_approval_waiting_does_not_rearm_after_supersession(self) -> None:
        self._assert_web_supersession_prevents_stale_attempt_and_rearm({
            "result": "DEFERRED", "state": "WEB_REENTRY_WAITING_LOCAL_APPROVAL", "returncode": 0,
            "failure_class": "local_approval_required", "approval_id": "approval-1",
        })

    def test_active_writer_defer_does_not_rearm_after_supersession(self) -> None:
        import threading
        from unittest.mock import Mock, patch

        with tempfile.TemporaryDirectory() as tmp:
            repo, registry, state = self.make_paths(Path(tmp))
            old_token = "old-token"
            state.write_text(json.dumps({
                "receipt_id": "r1", "session_id": "controller-1", "repo": str(repo.resolve()),
                "state": "RESUME_PENDING", "pending_control_event": True,
                "supervisor_receipt_id": "r1", "supervisor_token": old_token, "supervisor_pid": 1111,
            }), encoding="utf-8")
            validated = threading.Event()
            release_old = threading.Event()
            old_done = threading.Event()
            execute = Mock(return_value={
                "result": "DEFERRED", "state": "RESUME_DEFERRED_ACTIVE_WRITER", "returncode": 0,
                "failure_class": "active_writer_present",
            })
            rearm = Mock(side_effect=AssertionError("superseded active-writer supervisor must not rearm"))

            def paused_lifecycle(_session_id: str) -> dict[str, object]:
                validated.set(); release_old.wait(2)
                return {"pending_control_event": True, "requires_user": False, "controller_host": "desktop"}

            def run_old() -> None:
                try:
                    web_bridge.run_auto_native_stop(
                        session_id="controller-1", repo=repo, receipt_id="r1", registry=registry,
                        codex="codex", delay_seconds=0, state_path=state, supervisor_token=old_token,
                    )
                finally:
                    old_done.set()

            old_thread = threading.Thread(target=run_old)
            try:
                with patch.object(web_bridge, "_load_lifecycle_state", side_effect=paused_lifecycle), patch.object(
                    web_bridge, "execute_native_resume", execute
                ), patch.object(web_bridge, "_rearm_auto_native_stop", rearm), patch.object(
                    web_bridge.subprocess, "Popen", return_value=Mock(pid=2222)
                ), patch.object(web_bridge, "_pid_is_alive", return_value=True):
                    old_thread.start(); self.assertTrue(validated.wait(1))
                    self.assertTrue(web_bridge.schedule_auto_native_stop(
                        session_id="controller-1", repo=repo, receipt_id="r1", registry=registry, codex="codex",
                        delay_seconds=1, state_path=state, force_rearm=True, replace_supervisor_token=old_token,
                    ))
                    new_token = json.loads(state.read_text())["supervisor_token"]
                    release_old.set(); self.assertTrue(old_done.wait(2))
            finally:
                release_old.set(); old_thread.join(2)
            execute.assert_not_called(); rearm.assert_not_called()
            final = json.loads(state.read_text())
            self.assertEqual(final["supervisor_token"], new_token)
            self.assertEqual(final["supervisor_pid"], 2222)

    def test_pid_persistence_cannot_overwrite_replacement_generation(self) -> None:
        import threading
        from unittest.mock import Mock, patch

        with tempfile.TemporaryDirectory() as tmp:
            repo, registry, state = self.make_paths(Path(tmp))
            popen_entered = threading.Event(); release_first = threading.Event(); second_done = threading.Event()
            calls = 0
            calls_lock = threading.Lock()

            def popen(*_args, **_kwargs):
                nonlocal calls
                with calls_lock:
                    calls += 1; call = calls
                if call == 1:
                    popen_entered.set(); release_first.wait(2); return Mock(pid=1111)
                return Mock(pid=2222)

            def first() -> None:
                web_bridge.schedule_auto_native_stop(
                    session_id="controller-1", repo=repo, receipt_id="r1", registry=registry,
                    codex="codex", delay_seconds=1, state_path=state,
                )

            def second() -> None:
                try:
                    web_bridge.schedule_auto_native_stop(
                        session_id="controller-1", repo=repo, receipt_id="r2", registry=registry,
                        codex="codex", delay_seconds=1, state_path=state,
                    )
                finally:
                    second_done.set()

            t1=threading.Thread(target=first); t2=threading.Thread(target=second)
            try:
                with patch.object(web_bridge.subprocess, "Popen", side_effect=popen), patch.object(
                    web_bridge, "_pid_is_alive", return_value=True
                ):
                    t1.start(); self.assertTrue(popen_entered.wait(1)); t2.start()
                    self.assertFalse(second_done.wait(0.1), "replacement must wait for atomic first PID persistence")
                    release_first.set(); t1.join(2); t2.join(2)
            finally:
                release_first.set(); t1.join(2); t2.join(2)
            final=json.loads(state.read_text())
            self.assertEqual(final["receipt_id"], "r2")
            self.assertEqual(final["supervisor_receipt_id"], "r2")
            self.assertEqual(final["supervisor_pid"], 2222)

    def test_duplicate_scheduling_concurrently_coalesces_to_single_owner(self) -> None:
        import threading
        from unittest.mock import Mock, patch

        with tempfile.TemporaryDirectory() as tmp:
            repo, registry, state = self.make_paths(Path(tmp))
            gate=threading.Barrier(3); results: list[bool]=[]
            def schedule() -> None:
                gate.wait(); results.append(web_bridge.schedule_auto_native_stop(
                    session_id="controller-1", repo=repo, receipt_id="r1", registry=registry,
                    codex="codex", delay_seconds=1, state_path=state,
                ))
            t1=threading.Thread(target=schedule); t2=threading.Thread(target=schedule)
            with patch.object(web_bridge.subprocess, "Popen", return_value=Mock(pid=1111)) as popen, patch.object(
                web_bridge, "_pid_is_alive", return_value=True
            ):
                t1.start(); t2.start(); gate.wait(); t1.join(2); t2.join(2)
            self.assertCountEqual(results, [True, False])
            self.assertEqual(popen.call_count, 1)
            final=json.loads(state.read_text())
            self.assertEqual(final["coalesced_schedule_count"], 1)
            self.assertEqual(final["supervisor_pid"], 1111)

    def test_execute_native_resume_stale_supervisor_token_blocks_process_launch(self) -> None:
        from unittest.mock import Mock, patch

        with tempfile.TemporaryDirectory() as tmp:
            repo, registry, state = self.make_paths(Path(tmp))
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {
                    "controller-1": {"desktop_codex": ["desktop-good"]}
                },
                "__controller_targets__": {
                    "controller-1": {
                        "desktop_codex": {
                            "status": "active",
                            "session_id": "desktop-good",
                            "generation": 2,
                        }
                    }
                },
            }), encoding="utf-8")
            state.write_text(json.dumps({
                "receipt_id": "r1",
                "supervisor_receipt_id": "r1",
                "supervisor_token": "new-token",
                "supervisor_pid": 2222,
                "pending_control_event": True,
            }), encoding="utf-8")

            real_popen = subprocess.Popen
            native_launches = []

            def guarded_popen(command, *args, **kwargs):
                if isinstance(command, (list, tuple)) and command and str(command[0]).endswith("git"):
                    return real_popen(command, *args, **kwargs)
                native_launches.append(command)
                raise AssertionError(
                    "stale supervisor token must not cross the native process launch boundary"
                )

            with patch.object(
                web_bridge, "preflight_native_resume", return_value=(True, "", {})
            ), patch.object(web_bridge.subprocess, "Popen", side_effect=guarded_popen):
                result = web_bridge.execute_native_resume(
                    session_id="controller-1",
                    repo=repo,
                    registry=registry,
                    codex="codex",
                    runtime_path="/usr/bin:/bin",
                    terminal_receipts=["terminal.json"],
                    next_action="continue",
                    supervisor_state_path=state,
                    supervisor_receipt_id="r1",
                    supervisor_token="old-token",
                )

            self.assertEqual(result["result"], "DEFERRED")
            self.assertEqual(result["state"], "RESUME_SUPERSEDED")
            self.assertEqual(result["failure_class"], "supervisor_superseded")
            self.assertEqual(result["returncode"], 0)
            self.assertEqual(result["execution_target_session_id"], "desktop-good")
            self.assertEqual(result["target_generation"], 2)
            self.assertEqual(native_launches, [])

    def test_stale_supervisor_cannot_native_wake_after_supersession(self) -> None:
        import threading
        from unittest.mock import Mock, patch
        with tempfile.TemporaryDirectory() as tmp:
            repo, registry, state=self.make_paths(Path(tmp)); old_token="old-token"
            state.write_text(json.dumps({"receipt_id":"r1","supervisor_receipt_id":"r1","supervisor_token":old_token,"supervisor_pid":1111}), encoding="utf-8")
            paused=threading.Event(); release=threading.Event(); done=threading.Event()
            def lifecycle(_sid): paused.set(); release.wait(2); return {"pending_control_event":True,"requires_user":False,"controller_host":"desktop_codex"}
            execute=Mock(side_effect=AssertionError("superseded supervisor must not native wake"))
            def old():
                try: web_bridge.run_auto_native_stop(session_id="controller-1",repo=repo,receipt_id="r1",registry=registry,codex="codex",delay_seconds=0,state_path=state,supervisor_token=old_token)
                finally: done.set()
            t=threading.Thread(target=old)
            try:
                with patch.object(web_bridge,"_load_lifecycle_state",side_effect=lifecycle), patch.object(web_bridge,"execute_native_resume",execute), patch.object(web_bridge.subprocess,"Popen",return_value=Mock(pid=2222)), patch.object(web_bridge,"_pid_is_alive",return_value=True):
                    t.start(); self.assertTrue(paused.wait(1)); web_bridge.schedule_auto_native_stop(session_id="controller-1",repo=repo,receipt_id="r2",registry=registry,codex="codex",delay_seconds=1,state_path=state); release.set(); self.assertTrue(done.wait(2))
            finally: release.set(); t.join(2)
            execute.assert_not_called(); final=json.loads(state.read_text()); self.assertEqual(final["receipt_id"],"r2"); self.assertEqual(final["supervisor_pid"],2222)

    def test_replacement_can_supersede_while_old_supervisor_waits_in_web_reentry(self) -> None:
        import threading
        from unittest.mock import Mock, patch
        with tempfile.TemporaryDirectory() as tmp:
            repo, registry, state = self.make_paths(Path(tmp)); old_token = "old-token"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {"controller-1": {"web": ["web-1"]}},
                "__controller_targets__": {"controller-1": {"web": {
                    "status": "active", "session_id": "web-1", "generation": 1,
                }}},
            }), encoding="utf-8")
            state.write_text(json.dumps({
                "receipt_id": "r1", "supervisor_receipt_id": "r1",
                "supervisor_token": old_token, "supervisor_pid": 1111,
                "pending_control_event": True,
            }), encoding="utf-8")
            reentry_entered = threading.Event(); release_reentry = threading.Event()
            replacement_done = threading.Event(); old_done = threading.Event()

            def web_reentry(**_kwargs):
                reentry_entered.set(); release_reentry.wait(2)
                return {"result": "CONFIRMED", "state": "WEB_REENTRY_SUBMITTED", "returncode": 0}

            def old() -> None:
                try:
                    web_bridge.run_auto_native_stop(
                        session_id="controller-1", repo=repo, receipt_id="r1", registry=registry,
                        codex="codex", delay_seconds=0, state_path=state, supervisor_token=old_token,
                    )
                finally:
                    old_done.set()

            replacement_result: list[bool] = []
            def replace() -> None:
                try:
                    replacement_result.append(web_bridge.schedule_auto_native_stop(
                        session_id="controller-1", repo=repo, receipt_id="r1", registry=registry,
                        codex="codex", delay_seconds=1, state_path=state, force_rearm=True,
                        replace_supervisor_token=old_token,
                    ))
                finally:
                    replacement_done.set()

            old_thread = threading.Thread(target=old); replacement_thread = threading.Thread(target=replace)
            try:
                with patch.object(web_bridge, "_load_lifecycle_state", return_value={
                    "pending_control_event": True, "requires_user": False,
                    "controller_host": "web", "wake_generation": 1,
                }), patch.object(web_bridge, "execute_web_reentry", side_effect=web_reentry), patch.object(
                    web_bridge.subprocess, "Popen", return_value=Mock(pid=2222)
                ), patch.object(web_bridge, "_pid_is_alive", return_value=True):
                    old_thread.start(); self.assertTrue(reentry_entered.wait(1))
                    replacement_thread.start()
                    self.assertTrue(
                        replacement_done.wait(0.3),
                        "replacement must not wait for the old supervisor's external web re-entry",
                    )
                    self.assertEqual(replacement_result, [True])
                    new_token = json.loads(state.read_text())["supervisor_token"]
                    self.assertNotEqual(new_token, old_token)
                    release_reentry.set(); self.assertTrue(old_done.wait(2))
            finally:
                release_reentry.set(); old_thread.join(2); replacement_thread.join(2)
            final = json.loads(state.read_text())
            self.assertEqual(final["supervisor_token"], new_token)
            self.assertEqual(final["supervisor_pid"], 2222)
            self.assertEqual(final["state"], "RESUME_PENDING")

    def test_superseded_supervisor_cannot_start_native_recovery_bootstrap_after_resume(self) -> None:
        from unittest.mock import Mock, patch

        with tempfile.TemporaryDirectory() as tmp:
            repo, registry, state = self.make_paths(Path(tmp))
            old_token = "old-token"
            state.write_text(json.dumps({
                "receipt_id": "r1",
                "supervisor_receipt_id": "r1",
                "supervisor_token": old_token,
                "supervisor_pid": 1111,
                "pending_control_event": True,
            }), encoding="utf-8")
            incompatible = {
                "operation": "native_resume",
                "result": "FAILED",
                "state": "RESUME_TARGET_INCOMPATIBLE",
                "pending_control_event": True,
                "returncode": 78,
                "stderr_tail": "bad target",
                "replacement_eligible": True,
                "execution_target_session_id": "desktop-bad",
                "target_generation": 1,
            }

            def resume_then_supersede(**_kwargs):
                current = json.loads(state.read_text())
                current["supervisor_token"] = "new-token"
                current["supervisor_pid"] = 2222
                state.write_text(json.dumps(current), encoding="utf-8")
                return incompatible

            bootstrap = Mock(side_effect=AssertionError(
                "superseded supervisor must not start native recovery bootstrap"
            ))
            with patch.object(web_bridge, "_load_lifecycle_state", return_value={
                "pending_control_event": True,
                "requires_user": False,
                "controller_host": "desktop_codex",
                "wake_generation": 1,
            }), patch.object(
                web_bridge, "execute_native_resume", side_effect=resume_then_supersede
            ), patch.object(
                web_bridge, "preflight_native_resume", return_value=(True, "", {})
            ), patch.object(
                web_bridge.target_guard,
                "unique_controller_id_for_repo_in_registry",
                return_value="controller-1",
            ), patch.object(web_bridge.subprocess, "run", bootstrap):
                code = web_bridge.run_auto_native_stop(
                    session_id="controller-1", repo=repo, receipt_id="r1", registry=registry,
                    codex="codex", delay_seconds=0, state_path=state, supervisor_token=old_token,
                )

            self.assertEqual(code, 0)
            bootstrap.assert_not_called()
            final = json.loads(state.read_text())
            self.assertEqual(final["supervisor_token"], "new-token")
            self.assertEqual(final["supervisor_pid"], 2222)

    def test_superseded_supervisor_cannot_start_recovery_bootstrap_between_ownership_check_and_launch(self) -> None:
        from unittest.mock import Mock, patch

        with tempfile.TemporaryDirectory() as tmp:
            repo, registry, state = self.make_paths(Path(tmp))
            old_token = "old-token"
            state.write_text(json.dumps({
                "receipt_id": "r1", "supervisor_receipt_id": "r1",
                "supervisor_token": old_token, "supervisor_pid": 1111,
                "pending_control_event": True,
            }), encoding="utf-8")

            def preflight_then_supersede(**_kwargs):
                current = json.loads(state.read_text())
                current["supervisor_token"] = "new-token"
                current["supervisor_pid"] = 2222
                state.write_text(json.dumps(current), encoding="utf-8")
                return True, "", {}

            bootstrap = Mock(side_effect=AssertionError(
                "superseded supervisor must not cross the recovery bootstrap launch boundary"
            ))
            with patch.object(
                web_bridge, "preflight_native_resume", side_effect=preflight_then_supersede
            ), patch.object(web_bridge.subprocess, "run", bootstrap):
                result = web_bridge.recover_incompatible_native_target(
                    session_id="controller-1", repo=repo, registry=registry, codex="codex",
                    failed_target_session_id="desktop-bad", expected_generation=1,
                    expected_ownership_generation=1,
                    supervisor_state_path=state, supervisor_receipt_id="r1",
                    supervisor_token=old_token,
                )

            self.assertEqual(result["state"], "RESUME_SUPERSEDED")
            bootstrap.assert_not_called()

    def test_superseded_supervisor_cannot_replace_native_target_after_recovery_bootstrap(self) -> None:
        import threading
        from unittest.mock import Mock, patch
        with tempfile.TemporaryDirectory() as tmp:
            repo, registry, state = self.make_paths(Path(tmp)); old_token = "old-token"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {"controller-1": {"desktop_codex": ["desktop-bad"]}},
                "__controller_targets__": {"controller-1": {"desktop_codex": {
                    "status": "active", "session_id": "desktop-bad", "generation": 1,
                }}},
            }), encoding="utf-8")
            state.write_text(json.dumps({
                "receipt_id": "r1", "supervisor_receipt_id": "r1",
                "supervisor_token": old_token, "supervisor_pid": 1111,
                "pending_control_event": True,
            }), encoding="utf-8")
            bootstrap_entered = threading.Event(); release_bootstrap = threading.Event(); old_done = threading.Event()
            incompatible = {
                "operation": "native_resume", "result": "FAILED",
                "state": "RESUME_TARGET_INCOMPATIBLE", "pending_control_event": True,
                "returncode": 78, "stderr_tail": "bad target",
                "replacement_eligible": True,
                "execution_target_session_id": "desktop-bad", "target_generation": 1,
            }

            def bootstrap(*_args, **_kwargs):
                bootstrap_entered.set(); release_bootstrap.wait(2)
                return Mock(returncode=0, stdout='{"type":"thread.started","thread_id":"desktop-new"}\n', stderr="")

            def old() -> None:
                try:
                    web_bridge.run_auto_native_stop(
                        session_id="controller-1", repo=repo, receipt_id="r1", registry=registry,
                        codex="codex", delay_seconds=0, state_path=state, supervisor_token=old_token,
                    )
                finally:
                    old_done.set()

            old_thread = threading.Thread(target=old)
            replace_target = Mock(return_value={
                "controller_id": "controller-1", "execution_target_session_id": "desktop-new",
                "status": "active", "generation": 2,
            })
            try:
                with patch.object(web_bridge, "_load_lifecycle_state", return_value={
                    "pending_control_event": True, "requires_user": False,
                    "controller_host": "desktop_codex", "wake_generation": 1,
                }), patch.object(web_bridge, "execute_native_resume", return_value=incompatible), patch.object(
                    web_bridge, "preflight_native_resume", return_value=(True, "", {})
                ), patch.object(web_bridge.subprocess, "run", side_effect=bootstrap), patch.object(
                    web_bridge, "replace_desktop_execution_target", replace_target
                ), patch.object(web_bridge.subprocess, "Popen", return_value=Mock(pid=2222)), patch.object(
                    web_bridge, "_pid_is_alive", return_value=True
                ):
                    old_thread.start(); self.assertTrue(bootstrap_entered.wait(1))
                    schedule_done = threading.Event(); schedule_result: list[bool] = []
                    def supersede() -> None:
                        schedule_result.append(web_bridge.schedule_auto_native_stop(
                            session_id="controller-1", repo=repo, receipt_id="r1", registry=registry,
                            codex="codex", delay_seconds=1, state_path=state, force_rearm=True,
                            replace_supervisor_token=old_token,
                        ))
                        schedule_done.set()
                    supersede_thread = threading.Thread(target=supersede)
                    supersede_thread.start()
                    self.assertTrue(
                        schedule_done.wait(0.5),
                        "replacement supervisor must be able to supersede while recovery bootstrap is still blocked",
                    )
                    self.assertEqual(schedule_result, [True])
                    new_token = json.loads(state.read_text())["supervisor_token"]
                    release_bootstrap.set(); self.assertTrue(old_done.wait(2)); supersede_thread.join(2)
            finally:
                release_bootstrap.set(); old_thread.join(2)
            replace_target.assert_not_called()
            final = json.loads(state.read_text())
            self.assertEqual(final["supervisor_token"], new_token)
            self.assertEqual(final["supervisor_pid"], 2222)

    def test_superseded_supervisor_cannot_launch_recovery_resume_after_target_replacement(self) -> None:
        from unittest.mock import Mock, patch
        with tempfile.TemporaryDirectory() as tmp:
            repo, registry, state = self.make_paths(Path(tmp)); old_token = "old-token"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {"controller-1": {"desktop_codex": ["desktop-bad"]}},
                "__controller_targets__": {"controller-1": {"desktop_codex": {
                    "status": "active", "session_id": "desktop-bad", "generation": 1,
                }}},
            }), encoding="utf-8")
            state.write_text(json.dumps({
                "receipt_id": "r1", "supervisor_receipt_id": "r1",
                "supervisor_token": old_token, "supervisor_pid": 1111,
                "pending_control_event": True,
            }), encoding="utf-8")

            bootstrap = Mock(return_value=Mock(
                returncode=0, stdout='{"type":"thread.started","thread_id":"desktop-new"}\n', stderr=""
            ))

            def replace_then_supersede(**_kwargs):
                superseded = json.loads(state.read_text())
                superseded["supervisor_token"] = "new-token"
                superseded["supervisor_pid"] = 2222
                state.write_text(json.dumps(superseded), encoding="utf-8")
                return {
                    "controller_id": "controller-1",
                    "execution_target_session_id": "desktop-new",
                    "status": "active",
                    "generation": 2,
                }

            popen = Mock(side_effect=AssertionError("superseded recovery must not launch stale native resume"))
            with patch.object(web_bridge, "preflight_native_resume", return_value=(True, "", {})), patch.object(
                web_bridge.subprocess, "run", bootstrap
            ), patch.object(
                web_bridge, "replace_desktop_execution_target", side_effect=replace_then_supersede
            ), patch.object(web_bridge.subprocess, "Popen", popen):
                result = web_bridge.recover_incompatible_native_target(
                    session_id="controller-1", repo=repo, registry=registry, codex="codex",
                    failed_target_session_id="desktop-bad", expected_generation=1,
                    expected_ownership_generation=1,
                    runtime_path="/usr/bin:/bin", terminal_receipts=["terminal.json"],
                    next_action="continue", supervisor_state_path=state,
                    supervisor_receipt_id="r1", supervisor_token=old_token,
                )

            self.assertEqual(result["state"], "RESUME_SUPERSEDED")
            self.assertEqual(result["failure_class"], "supervisor_superseded")
            popen.assert_not_called()

    def test_replacement_can_supersede_while_old_supervisor_waits_in_native_resume(self) -> None:
        import threading
        from unittest.mock import Mock, patch
        with tempfile.TemporaryDirectory() as tmp:
            repo, registry, state = self.make_paths(Path(tmp)); old_token = "old-token"
            state.write_text(json.dumps({
                "receipt_id": "r1", "supervisor_receipt_id": "r1",
                "supervisor_token": old_token, "supervisor_pid": 1111,
                "pending_control_event": True,
            }), encoding="utf-8")
            resume_entered = threading.Event(); release_resume = threading.Event()
            replacement_done = threading.Event(); old_done = threading.Event()
            confirmed = {
                "result": "CONFIRMED", "state": "RESUME_CONFIRMED", "returncode": 0,
                "stdout_tail": "", "stderr_tail": "",
            }

            def native_resume(**_kwargs):
                resume_entered.set(); release_resume.wait(2); return confirmed

            def old() -> None:
                try:
                    web_bridge.run_auto_native_stop(
                        session_id="controller-1", repo=repo, receipt_id="r1", registry=registry,
                        codex="codex", delay_seconds=0, state_path=state, supervisor_token=old_token,
                    )
                finally:
                    old_done.set()

            replacement_result: list[bool] = []
            def replace() -> None:
                try:
                    replacement_result.append(web_bridge.schedule_auto_native_stop(
                        session_id="controller-1", repo=repo, receipt_id="r1", registry=registry,
                        codex="codex", delay_seconds=1, state_path=state, force_rearm=True,
                        replace_supervisor_token=old_token,
                    ))
                finally:
                    replacement_done.set()

            old_thread = threading.Thread(target=old); replacement_thread = threading.Thread(target=replace)
            try:
                with patch.object(web_bridge, "_load_lifecycle_state", return_value={
                    "pending_control_event": True, "requires_user": False,
                    "controller_host": "desktop_codex", "wake_generation": 1,
                }), patch.object(web_bridge, "execute_native_resume", side_effect=native_resume), patch.object(
                    web_bridge.subprocess, "Popen", return_value=Mock(pid=2222)
                ), patch.object(
                    web_bridge.target_guard,
                    "unique_controller_id_for_repo_in_registry",
                    return_value="controller-1",
                ), patch.object(web_bridge, "_pid_is_alive", return_value=True):
                    old_thread.start(); self.assertTrue(resume_entered.wait(1))
                    replacement_thread.start()
                    self.assertTrue(
                        replacement_done.wait(0.3),
                        "replacement must not wait for the old supervisor's external native resume",
                    )
                    self.assertEqual(replacement_result, [True])
                    new_token = json.loads(state.read_text())["supervisor_token"]
                    self.assertNotEqual(new_token, old_token)
                    release_resume.set(); self.assertTrue(old_done.wait(2))
            finally:
                release_resume.set(); old_thread.join(2); replacement_thread.join(2)
            final = json.loads(state.read_text())
            self.assertEqual(final["supervisor_token"], new_token)
            self.assertEqual(final["supervisor_pid"], 2222)
            self.assertEqual(final["state"], "RESUME_PENDING")

    def test_confirmed_old_supervisor_cannot_restore_state_after_replacement_during_fresh_read(self) -> None:
        import threading
        from unittest.mock import Mock, patch
        with tempfile.TemporaryDirectory() as tmp:
            repo, registry, state=self.make_paths(Path(tmp)); old_token="old-token"
            state.write_text(json.dumps({"receipt_id":"r1","supervisor_receipt_id":"r1","supervisor_token":old_token,"supervisor_pid":1111,"pending_control_event":True}), encoding="utf-8")
            second_read=threading.Event(); release=threading.Event(); calls=0
            def lifecycle(_sid):
                nonlocal calls
                calls += 1
                if calls == 1: return {"pending_control_event":True,"requires_user":False,"controller_host":"desktop_codex","wake_generation":1}
                second_read.set(); release.wait(2); return {"pending_control_event":True,"requires_user":False,"controller_host":"desktop_codex","wake_generation":2}
            confirmed={"result":"CONFIRMED","state":"RESUME_CONFIRMED","returncode":0,"stdout_tail":"","stderr_tail":""}
            done=threading.Event()
            def old():
                try: web_bridge.run_auto_native_stop(session_id="controller-1",repo=repo,receipt_id="r1",registry=registry,codex="codex",delay_seconds=0,state_path=state,supervisor_token=old_token)
                finally: done.set()
            t=threading.Thread(target=old)
            try:
                with patch.object(web_bridge,"_load_lifecycle_state",side_effect=lifecycle), patch.object(web_bridge,"execute_native_resume",return_value=confirmed), patch.object(web_bridge.subprocess,"Popen",return_value=Mock(pid=2222)), patch.object(web_bridge.target_guard,"unique_controller_id_for_repo_in_registry",return_value="controller-1"), patch.object(web_bridge,"_pid_is_alive",return_value=True):
                    t.start(); self.assertTrue(second_read.wait(1)); self.assertTrue(web_bridge.schedule_auto_native_stop(session_id="controller-1",repo=repo,receipt_id="r1",registry=registry,codex="codex",delay_seconds=1,state_path=state,force_rearm=True,replace_supervisor_token=old_token)); new_token=json.loads(state.read_text())["supervisor_token"]; release.set(); self.assertTrue(done.wait(2))
            finally: release.set(); t.join(2)
            final=json.loads(state.read_text()); self.assertEqual(final["supervisor_token"],new_token); self.assertEqual(final["supervisor_pid"],2222); self.assertEqual(final["state"],"RESUME_PENDING")

    def test_bootstrap_and_replacement_generation_concurrently_keep_one_owner(self) -> None:
        import threading
        from unittest.mock import Mock, patch
        with tempfile.TemporaryDirectory() as tmp:
            repo, registry, state=self.make_paths(Path(tmp)); lifecycle={"pending_control_event":True,"requires_user":False,"wake_generation":7}
            gate=threading.Barrier(3); results: list[bool]=[]
            def ensure(): gate.wait(); results.append(web_bridge.ensure_continuation_supervisor(lifecycle_state=lifecycle,session_id="controller-1",repo=repo,registry=registry,codex="codex",delay_seconds=1))
            t1=threading.Thread(target=ensure); t2=threading.Thread(target=ensure)
            with patch.object(web_bridge,"default_auto_stop_state_path",return_value=state), patch.object(web_bridge.subprocess,"Popen",return_value=Mock(pid=1111)) as popen, patch.object(web_bridge,"_pid_is_alive",return_value=True):
                t1.start(); t2.start(); gate.wait(); t1.join(2); t2.join(2)
            self.assertEqual(popen.call_count,1); self.assertEqual(sum(bool(x) for x in results),2)
            final=json.loads(state.read_text()); self.assertEqual(final["receipt_id"],"bootstrap:7"); self.assertEqual(final["supervisor_pid"],1111); self.assertTrue(final["supervisor_token"])

    def test_stale_supervisor_token_exits_without_running_impl(self) -> None:
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            repo, registry, state = self.make_paths(Path(tmp))
            state.write_text(json.dumps({
                "receipt_id": "r1",
                "supervisor_receipt_id": "r1",
                "supervisor_token": "current-token",
            }), encoding="utf-8")
            with patch.object(
                web_bridge, "_run_auto_native_stop_impl",
                side_effect=AssertionError("stale supervisor must not execute"),
            ):
                started = __import__("time").monotonic()
                code = web_bridge.run_auto_native_stop(
                    session_id="controller-1", repo=repo, receipt_id="r1", registry=registry,
                    codex="codex", delay_seconds=1.0, state_path=state,
                    supervisor_token="stale-token",
                )
                elapsed = __import__("time").monotonic() - started
            self.assertEqual(code, 0)
            self.assertLess(elapsed, 0.25, "already-stale supervisor must exit before delay sleep")


class WebContinuationSupervisorBootstrapTests(unittest.TestCase):
    def test_identity_blocked_same_event_and_registry_are_not_bootstrapped_again(self) -> None:
        lifecycle = {
            "pending_control_event": True,
            "requires_user": False,
            "wake_generation": 9,
            "triggers": ["terminal_receipt_pending"],
        }
        self.assertFalse(web_bridge.continuation_supervisor_needs_bootstrap(
            lifecycle,
            {
                "state": "WEB_REENTRY_IDENTITY_UNAVAILABLE",
                "pending_control_event": True,
                "receipt_id": "bootstrap:9",
                "last_lifecycle_fingerprint": web_bridge._wake_event_fingerprint(
                    lifecycle
                ),
                "blocked_registry_sha256": "registry-v1",
            },
            current_registry_sha256="registry-v1",
        ))

    def test_identity_blocked_event_retries_after_registry_changes(self) -> None:
        lifecycle = {
            "pending_control_event": True,
            "requires_user": False,
            "wake_generation": 9,
            "triggers": ["terminal_receipt_pending"],
        }
        self.assertTrue(web_bridge.continuation_supervisor_needs_bootstrap(
            lifecycle,
            {
                "state": "WEB_REENTRY_IDENTITY_UNAVAILABLE",
                "pending_control_event": True,
                "receipt_id": "bootstrap:9",
                "last_lifecycle_fingerprint": web_bridge._wake_event_fingerprint(
                    lifecycle
                ),
                "blocked_registry_sha256": "registry-v1",
            },
            current_registry_sha256="registry-v2",
        ))

    def test_stalled_same_event_is_not_bootstrapped_again(self) -> None:
        lifecycle = {
            "pending_control_event": True,
            "requires_user": False,
            "wake_generation": 9,
            "triggers": ["terminal_receipt_pending"],
        }
        self.assertFalse(web_bridge.continuation_supervisor_needs_bootstrap(
            lifecycle,
            {
                "state": "RESUME_STALLED_NO_PROGRESS",
                "pending_control_event": True,
                "receipt_id": "bootstrap:9",
                "last_lifecycle_fingerprint": web_bridge._wake_event_fingerprint(
                    lifecycle
                ),
                "unchanged_continuation_count": 3,
            },
        ))

    def test_confirmed_old_supervisor_needs_bootstrap_while_lifecycle_pending(self) -> None:
        self.assertTrue(web_bridge.continuation_supervisor_needs_bootstrap(
            {"pending_control_event": True, "requires_user": False},
            {"state": "RESUME_CONFIRMED", "pending_control_event": True},
        ))

    def test_live_active_supervisor_does_not_need_duplicate_bootstrap(self) -> None:
        from unittest.mock import patch

        with patch.object(web_bridge, "_pid_is_alive", return_value=True):
            self.assertFalse(web_bridge.continuation_supervisor_needs_bootstrap(
                {"pending_control_event": True, "requires_user": False},
                {
                    "state": "RESUME_PENDING",
                    "pending_control_event": True,
                    "receipt_id": "bootstrap:7",
                    "supervisor_pid": 1234,
                    "supervisor_token": "token-1",
                    "supervisor_receipt_id": "bootstrap:7",
                },
            ))

    def test_dead_or_untracked_active_supervisor_requires_bootstrap(self) -> None:
        from unittest.mock import patch

        lifecycle = {"pending_control_event": True, "requires_user": False}
        with patch.object(web_bridge, "_pid_is_alive", return_value=False):
            self.assertTrue(web_bridge.continuation_supervisor_needs_bootstrap(
                lifecycle,
                {
                    "state": "RESUME_PENDING",
                    "pending_control_event": True,
                    "supervisor_pid": 999,
                    "supervisor_token": "token-dead",
                    "supervisor_receipt_id": "bootstrap:7",
                },
            ))
        self.assertTrue(web_bridge.continuation_supervisor_needs_bootstrap(
            lifecycle,
            {"state": "RESUME_PENDING", "pending_control_event": True},
        ))

    def test_user_wait_does_not_bootstrap_supervisor(self) -> None:
        self.assertFalse(web_bridge.continuation_supervisor_needs_bootstrap(
            {"pending_control_event": True, "requires_user": True},
            {"state": "RESUME_CONFIRMED", "pending_control_event": True},
        ))


class WebLifecycleNativeStopRootFixTests(unittest.TestCase):
    def run_bridge(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["/usr/bin/python3", str(BRIDGE), *args], text=True, capture_output=True, check=False
        )

    def make_repo_registry(self, root: Path) -> tuple[Path, Path]:
        repo = root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
        registry = root / "controllers.json"
        registry.write_text(json.dumps({
            "controller-1": str(repo.resolve()),
            "__controller_sessions__": {"controller-1": {"desktop_codex": ["controller-1"]}},
            "__controller_targets__": {"controller-1": {"desktop_codex": {
                "status": "active", "session_id": "controller-1", "generation": 1,
            }}},
        }), encoding="utf-8")
        return repo, registry

    def test_auto_native_stop_preflight_names_missing_node_in_launchagent_like_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); repo, registry = self.make_repo_registry(root)
            codex = root / "codex"
            codex.write_text("#!/usr/bin/env node\nprocess.exit(0)\n", encoding="utf-8")
            codex.chmod(0o755)
            state = root / "auto-stop.json"
            state.write_text(json.dumps({"receipt_id":"r1","session_id":"controller-1","repo":str(repo.resolve()),"state":"RESUME_PENDING","pending_control_event":True}), encoding="utf-8")
            empty_bin = root / "empty-bin"; empty_bin.mkdir()

            result = self.run_bridge(
                "auto-native-stop", "--session-id", "controller-1", "--repo", str(repo),
                "--receipt-id", "r1", "--registry", str(registry), "--state", str(state),
                "--delay-seconds", "0", "--codex", str(codex), "--runtime-path", str(empty_bin),
            )

            self.assertNotEqual(result.returncode, 0)
            saved = json.loads(state.read_text())
            self.assertEqual(saved["state"], "RESUME_FAILED")
            self.assertTrue(saved["pending_control_event"])
            self.assertIn("missing node runtime", saved["stderr_tail"].lower())
            self.assertNotEqual(saved.get("returncode"), 127)

    def test_auto_native_stop_failure_is_fail_closed_and_keeps_bounded_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); repo, registry = self.make_repo_registry(root)
            codex = root / "codex"
            codex.write_text(
                "#!/bin/sh\nif [ \"$1\" = \"--version\" ]; then echo codex-test; exit 0; fi\nprintf 'resume exploded:' >&2\npython3 - <<'EOF' >&2\nprint('x'*20000)\nEOF\nexit 7\n",
                encoding="utf-8",
            )
            codex.chmod(0o755)
            state = root / "auto-stop.json"
            state.write_text(json.dumps({"receipt_id":"r2","session_id":"controller-1","repo":str(repo.resolve()),"state":"RESUME_PENDING","pending_control_event":True}), encoding="utf-8")

            result = self.run_bridge(
                "auto-native-stop", "--session-id", "controller-1", "--repo", str(repo),
                "--receipt-id", "r2", "--registry", str(registry), "--state", str(state),
                "--delay-seconds", "0", "--codex", str(codex),
            )

            self.assertEqual(result.returncode, 7)
            saved = json.loads(state.read_text())
            self.assertEqual(saved["state"], "RESUME_FAILED")
            self.assertTrue(saved["pending_control_event"])
            self.assertEqual(saved["returncode"], 7)
            self.assertIn("resume exploded", saved["stderr_tail"])
            self.assertLessEqual(len(saved["stderr_tail"]), 8192)
            self.assertIn("command", saved)

    def test_auto_native_stop_success_confirms_same_controller_resume_without_closing_pending_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); repo, registry = self.make_repo_registry(root)
            codex = root / "codex"
            marker = root / "resume.txt"
            codex.write_text(
                f"#!/bin/sh\nif [ \"$1\" = \"--version\" ]; then echo codex-test; exit 0; fi\nprintf '%s\n' \"$*\" > {marker}\nexit 0\n",
                encoding="utf-8",
            )
            codex.chmod(0o755)
            state = root / "auto-stop.json"
            state.write_text(json.dumps({"receipt_id":"r3","session_id":"controller-1","repo":str(repo.resolve()),"state":"RESUME_PENDING","pending_control_event":True}), encoding="utf-8")

            result = self.run_bridge(
                "auto-native-stop", "--session-id", "controller-1", "--repo", str(repo),
                "--receipt-id", "r3", "--registry", str(registry), "--state", str(state),
                "--delay-seconds", "0", "--codex", str(codex),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            saved = json.loads(state.read_text())
            self.assertEqual(saved["state"], "RESUME_CONFIRMED")
            self.assertTrue(saved["pending_control_event"])
            self.assertIn("resume controller-1", marker.read_text())
            wake = json.loads(
                (repo / ".git" / "adaptive-delivery" / "controller-wake-receipt.json")
                .read_text(encoding="utf-8")
            )
            self.assertEqual(wake["result"], "CONFIRMED")
            self.assertEqual(wake["selected_host"], "desktop_codex")
            self.assertEqual(wake["controller_id"], "controller-1")
            self.assertEqual(wake["execution_target_session_id"], "controller-1")
            self.assertEqual(wake["target_generation"], 1)
            self.assertTrue(wake["pending_control_event"])

    def test_auto_native_stop_rearms_after_confirmed_resume_while_lifecycle_remains_pending(self) -> None:
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); repo, registry = self.make_repo_registry(root)
            state = root / "auto-stop.json"
            state.write_text(json.dumps({
                "receipt_id": "r-cont", "session_id": "controller-1",
                "repo": str(repo.resolve()), "state": "RESUME_PENDING",
                "pending_control_event": True,
            }), encoding="utf-8")
            initial = {
                "pending_control_event": True, "requires_user": False, "wake_generation": 7,
                "triggers": ["RUNNABLE:STEP-2"], "snapshot": {
                    "head": "a", "ledger_sha256": "l1", "worktree_status_sha256": "w1",
                    "ready_ids": ["STEP-2"], "runnable_ids": ["STEP-2"],
                    "candidate_revisions": [], "rule_handshake": {},
                },
            }
            fresh = dict(initial)
            confirmed = {
                "operation": "native_resume", "result": "CONFIRMED", "state": "RESUME_SUCCEEDED",
                "pending_control_event": True, "returncode": 0, "stdout_tail": "checkpoint",
                "stderr_tail": "", "controller_id": "controller-1",
                "execution_target_session_id": "desktop-current", "target_generation": 1,
            }
            with patch.object(web_bridge, "_load_lifecycle_state", side_effect=[initial, fresh]), patch.object(
                web_bridge, "execute_native_resume", return_value=confirmed
            ), patch.object(web_bridge, "schedule_auto_native_stop") as schedule:
                code = web_bridge.run_auto_native_stop(
                    session_id="controller-1", repo=repo, receipt_id="r-cont",
                    registry=registry, codex="/opt/homebrew/bin/codex", delay_seconds=0,
                    state_path=state, runtime_path="/usr/bin:/bin",
                )
            self.assertEqual(code, 0)
            schedule.assert_called_once()
            self.assertEqual(schedule.call_args.kwargs["receipt_id"], "r-cont")
            saved = json.loads(state.read_text())
            self.assertTrue(saved["pending_control_event"])
            self.assertEqual(saved["continuation_count"], 1)

    def test_auto_native_stop_backs_off_provider_usage_limit_without_health_loop_spin(self) -> None:
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); repo, registry = self.make_repo_registry(root)
            state = root / "auto-stop.json"
            state.write_text(json.dumps({
                "receipt_id": "r-limit", "session_id": "controller-1",
                "repo": str(repo.resolve()), "state": "RESUME_PENDING",
                "pending_control_event": True,
            }), encoding="utf-8")
            lifecycle = {
                "pending_control_event": True, "requires_user": False,
                "controller_host": "desktop_codex", "wake_generation": 9,
            }
            limited = {
                "operation": "native_resume", "result": "FAILED", "state": "RESUME_FAILED",
                "pending_control_event": True, "returncode": 1,
                "stdout_tail": "", "stderr_tail": "You've hit your usage limit. Try again later.",
                "controller_id": "controller-1", "failure_class": "usage_limit_exceeded",
                "error_code": "WEB_LIFECYCLE_RESUME_FAILED",
            }
            with patch.object(web_bridge, "_load_lifecycle_state", return_value=lifecycle), patch.object(
                web_bridge, "execute_native_resume", return_value=limited
            ), patch.object(web_bridge, "_rearm_auto_native_stop", return_value=True) as rearm:
                code = web_bridge.run_auto_native_stop(
                    session_id="controller-1", repo=repo, receipt_id="r-limit",
                    registry=registry, codex="/opt/homebrew/bin/codex", delay_seconds=0,
                    state_path=state, runtime_path="/usr/bin:/bin",
                )
            self.assertEqual(code, 0)
            rearm.assert_called_once()
            self.assertGreaterEqual(rearm.call_args.kwargs["delay_seconds"], 300.0)
            saved = json.loads(state.read_text())
            self.assertEqual(saved["state"], "RESUME_RETRY_BACKOFF")
            self.assertEqual(saved["failure_class"], "usage_limit_exceeded")
            self.assertTrue(saved["pending_control_event"])

    def test_provider_limit_backoff_counts_as_live_supervisor_state(self) -> None:
        from unittest.mock import patch
        with patch.object(web_bridge, "_pid_is_alive", return_value=True):
            self.assertFalse(web_bridge.continuation_supervisor_needs_bootstrap(
                {"pending_control_event": True, "requires_user": False},
                {
                    "state": "RESUME_RETRY_BACKOFF", "receipt_id": "r-limit",
                    "supervisor_receipt_id": "r-limit", "supervisor_token": "token",
                    "supervisor_pid": 42,
                },
            ))

    def test_auto_native_stop_does_not_rearm_after_confirmed_resume_when_lifecycle_closes(self) -> None:
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); repo, registry = self.make_repo_registry(root)
            state = root / "auto-stop.json"
            state.write_text(json.dumps({
                "receipt_id": "r-closed", "session_id": "controller-1",
                "repo": str(repo.resolve()), "state": "RESUME_PENDING",
                "pending_control_event": True,
            }), encoding="utf-8")
            initial = {"pending_control_event": True, "requires_user": False, "controller_host": "desktop_codex", "wake_generation": 1}
            closed = {"pending_control_event": False, "requires_user": False, "controller_host": "desktop_codex", "wake_generation": 1}
            confirmed = {
                "operation": "native_resume", "result": "CONFIRMED", "state": "RESUME_SUCCEEDED",
                "pending_control_event": True, "returncode": 0, "stdout_tail": "done",
                "stderr_tail": "", "controller_id": "controller-1",
            }
            with patch.object(web_bridge, "_load_lifecycle_state", side_effect=[initial, closed]), patch.object(
                web_bridge, "execute_native_resume", return_value=confirmed
            ), patch.object(web_bridge, "schedule_auto_native_stop") as schedule:
                code = web_bridge.run_auto_native_stop(
                    session_id="controller-1", repo=repo, receipt_id="r-closed",
                    registry=registry, codex="/opt/homebrew/bin/codex", delay_seconds=0,
                    state_path=state, runtime_path="/usr/bin:/bin",
                )
            self.assertEqual(code, 0)
            schedule.assert_not_called()
            saved = json.loads(state.read_text())
            self.assertFalse(saved["pending_control_event"])

    def test_auto_native_stop_waits_for_user_only_on_explicit_requires_user(self) -> None:
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); repo, registry = self.make_repo_registry(root)
            state = root / "auto-stop.json"
            state.write_text(json.dumps({
                "receipt_id": "r-user", "session_id": "controller-1",
                "repo": str(repo.resolve()), "state": "RESUME_PENDING",
                "pending_control_event": True,
            }), encoding="utf-8")
            initial = {"pending_control_event": True, "requires_user": False, "controller_host": "desktop_codex", "wake_generation": 3}
            waiting = {"pending_control_event": True, "requires_user": True, "controller_host": "desktop_codex", "wake_generation": 3}
            confirmed = {
                "operation": "native_resume", "result": "CONFIRMED", "state": "RESUME_SUCCEEDED",
                "pending_control_event": True, "returncode": 0, "stdout_tail": "need decision",
                "stderr_tail": "", "controller_id": "controller-1",
            }
            with patch.object(web_bridge, "_load_lifecycle_state", side_effect=[initial, waiting]), patch.object(
                web_bridge, "execute_native_resume", return_value=confirmed
            ), patch.object(web_bridge, "schedule_auto_native_stop") as schedule:
                code = web_bridge.run_auto_native_stop(
                    session_id="controller-1", repo=repo, receipt_id="r-user", registry=registry,
                    codex="/opt/homebrew/bin/codex", delay_seconds=0, state_path=state,
                    runtime_path="/usr/bin:/bin",
                )
            self.assertEqual(code, 0)
            schedule.assert_not_called()
            saved = json.loads(state.read_text())
            self.assertEqual(saved["state"], "WAITING_USER")
            self.assertEqual(saved["failure_class"], "user_decision_required")

    def test_auto_native_stop_stops_rearming_after_repeated_confirmed_no_progress(self) -> None:
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); repo, registry = self.make_repo_registry(root)
            lifecycle = {
                "pending_control_event": True, "requires_user": False, "wake_generation": 4,
                "triggers": ["RUNNABLE:STEP-2"], "snapshot": {
                    "head": "a", "ledger_sha256": "l", "worktree_status_sha256": "w",
                    "ready_ids": ["STEP-2"], "runnable_ids": ["STEP-2"],
                    "candidate_revisions": [], "rule_handshake": {},
                },
            }
            fingerprint = web_bridge._wake_event_fingerprint(lifecycle)
            state = root / "auto-stop.json"
            state.write_text(json.dumps({
                "receipt_id": "r-stall", "session_id": "controller-1",
                "repo": str(repo.resolve()), "state": "RESUME_PENDING",
                "pending_control_event": True, "continuation_count": 3,
                "unchanged_continuation_count": web_bridge.AUTO_CONTINUATION_STALL_LIMIT - 1,
                "last_lifecycle_fingerprint": fingerprint,
            }), encoding="utf-8")
            confirmed = {
                "operation": "native_resume", "result": "CONFIRMED", "state": "RESUME_SUCCEEDED",
                "pending_control_event": True, "returncode": 0, "stdout_tail": "same checkpoint",
                "stderr_tail": "", "controller_id": "controller-1",
            }
            with patch.object(web_bridge, "_load_lifecycle_state", side_effect=[lifecycle, lifecycle]), patch.object(
                web_bridge, "execute_native_resume", return_value=confirmed
            ), patch.object(web_bridge, "schedule_auto_native_stop") as schedule:
                code = web_bridge.run_auto_native_stop(
                    session_id="controller-1", repo=repo, receipt_id="r-stall", registry=registry,
                    codex="/opt/homebrew/bin/codex", delay_seconds=0, state_path=state,
                    runtime_path="/usr/bin:/bin",
                )
            self.assertEqual(code, 78)
            schedule.assert_not_called()
            saved = json.loads(state.read_text())
            self.assertEqual(saved["state"], "RESUME_STALLED_NO_PROGRESS")
            self.assertEqual(saved["failure_class"], "confirmed_resume_without_machine_progress")

    def test_resume_uses_target_replaced_during_preflight_not_the_retired_target(self) -> None:
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); repo, registry = self.make_repo_registry(root)
            marker = root / "resume.txt"
            codex = root / "codex"
            codex.write_text(
                f"#!/bin/sh\nprintf '%s\\n' \"$*\" > {marker}\nexit 0\n",
                encoding="utf-8",
            )
            codex.chmod(0o755)
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {
                    "controller-1": {"desktop_codex": ["desktop-old", "desktop-new"]}
                },
                "__controller_targets__": {
                    "controller-1": {
                        "desktop_codex": {
                            "status": "active",
                            "session_id": "desktop-old",
                            "generation": 1,
                        }
                    }
                },
            }), encoding="utf-8")

            def replace_during_preflight(**_kwargs: object) -> tuple[bool, str, dict[str, str]]:
                value = json.loads(registry.read_text(encoding="utf-8"))
                value["__controller_targets__"]["controller-1"]["desktop_codex"] = {
                    "status": "active",
                    "session_id": "desktop-new",
                    "generation": 2,
                }
                registry.write_text(json.dumps(value), encoding="utf-8")
                return True, "", dict(web_bridge.native_runtime_env())

            with patch.object(
                web_bridge, "preflight_native_resume", side_effect=replace_during_preflight
            ):
                attempt = web_bridge.execute_native_resume(
                    session_id="controller-1",
                    repo=repo,
                    registry=registry,
                    codex=str(codex),
                )

            self.assertEqual(attempt["result"], "CONFIRMED")
            self.assertEqual(attempt["execution_target_session_id"], "desktop-new")
            self.assertEqual(attempt["target_generation"], 2)
            self.assertIn("resume desktop-new", marker.read_text(encoding="utf-8"))

    def test_detached_scheduler_does_not_discard_stderr_to_devnull(self) -> None:
        source = BRIDGE.read_text(encoding="utf-8")
        self.assertNotIn("stderr=subprocess.DEVNULL", source)
        self.assertNotIn("stdout=subprocess.DEVNULL", source)
        self.assertIn("rotate_launcher_log", source)
        self.assertIn(".launcher.log", source)
        self.assertIn("stderr_tail", source)


class IdentityFieldSemanticsTests(unittest.TestCase):
    def test_wake_receipt_names_logical_controller_as_controller_id(self) -> None:
        receipt = web_bridge._wake_receipt(
            common_dir=Path("/tmp/common"), session_id="controller-1", event_fingerprint="fp",
            health={"state": "STALLED", "controller_host": "web"}, decision="RESUME_CURRENT_HOST",
            selected_host="web", reason="test", operation="native_resume", result="CONFIRMED",
        )
        self.assertEqual(receipt["controller_id"], "controller-1")
        self.assertNotIn("controller_session_id", receipt)

class ControllerWakeSupervisorTests(unittest.TestCase):
    def make_controller(self, root: Path) -> tuple[Path, Path, Path, Path, Path]:
        repo = root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
        registry = root / "controllers.json"
        registry.write_text(
            json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {"controller-1": {"web": ["web-session-1"]}},
            }),
            encoding="utf-8",
        )
        marker = root / "resume.txt"
        codex = root / "codex"
        codex.write_text(
            f"#!/bin/sh\nif [ \"$1\" = \"--version\" ]; then exit 0; fi\nprintf '%s\\n' \"$*\" > {marker}\n",
            encoding="utf-8",
        )
        codex.chmod(0o755)
        return repo, registry, codex, root / "wake-receipt.json", marker

    def wake(
        self,
        root: Path,
        *,
        lifecycle_state: dict,
        host_facts: dict,
        resume_adapters: dict | None = None,
    ) -> tuple[dict, Path, Path]:
        repo, registry, codex, receipt_path, marker = self.make_controller(root)
        receipt = web_bridge.wake_existing_controller(
            lifecycle_state=lifecycle_state,
            session_id="controller-1",
            repo=repo,
            registry=registry,
            codex=str(codex),
            receipt_path=receipt_path,
            host_facts=host_facts,
            resume_adapters=resume_adapters,
        )
        return receipt, receipt_path, marker

    def test_active_lease_expired_wakes_same_controller_without_stop_or_audit_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            receipt, receipt_path, marker = self.wake(
                Path(tmp),
                lifecycle_state={
                    "pending_control_event": True,
                    "triggers": ["active_lease_expired:F1"],
                },
                host_facts={"controller_host": "desktop_codex", "resume_actionable": True},
            )

            self.assertEqual(receipt["decision"], "RESUME_CURRENT_HOST")
            self.assertEqual(receipt["controller_id"], "controller-1")
            self.assertEqual(receipt["selected_host"], "desktop_codex")
            self.assertTrue(receipt["pending_control_event"])
            self.assertEqual(receipt["result"], "CONFIRMED")
            self.assertIn("resume controller-1", marker.read_text(encoding="utf-8"))
            self.assertEqual(
                json.loads(receipt_path.read_text(encoding="utf-8"))["decision"],
                "RESUME_CURRENT_HOST",
            )

    def test_wake_keeps_logical_controller_but_resumes_explicit_desktop_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, registry, codex, receipt_path, marker = self.make_controller(root)
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {
                    "controller-1": {
                        "web": ["web-session-1"],
                        "desktop_codex": ["desktop-current"],
                    }
                },
                "__controller_targets__": {
                    "controller-1": {
                        "desktop_codex": {
                            "status": "active",
                            "session_id": "desktop-current",
                            "generation": 2,
                        }
                    }
                },
            }), encoding="utf-8")

            receipt = web_bridge.wake_existing_controller(
                lifecycle_state={"pending_control_event": True, "triggers": ["READY:F1"]},
                session_id="controller-1",
                repo=repo,
                registry=registry,
                codex=str(codex),
                receipt_path=receipt_path,
                host_facts={"controller_host": "desktop_codex", "resume_actionable": True},
            )

            self.assertEqual(receipt["controller_id"], "controller-1")
            self.assertEqual(receipt["execution_target_session_id"], "desktop-current")
            self.assertEqual(receipt["target_generation"], 2)
            self.assertIn("resume desktop-current", marker.read_text(encoding="utf-8"))

    def test_target_generation_change_invalidates_prior_confirmed_wake_debounce(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, registry, codex, receipt_path, marker = self.make_controller(root)
            state = {
                "pending_control_event": True,
                "triggers": ["READY:F1"],
                "controller_host": "desktop_codex",
                "wake_generation": 1,
            }

            def write_target(session_id: str, generation: int) -> None:
                registry.write_text(json.dumps({
                    "controller-1": str(repo.resolve()),
                    "__controller_sessions__": {
                        "controller-1": {"desktop_codex": [session_id]}
                    },
                    "__controller_targets__": {
                        "controller-1": {
                            "desktop_codex": {
                                "status": "active",
                                "session_id": session_id,
                                "generation": generation,
                            }
                        }
                    },
                }), encoding="utf-8")

            write_target("desktop-old", 1)
            first = web_bridge.dispatch_pending_lifecycle_wake(
                lifecycle_state=state,
                session_id="controller-1",
                repo=repo,
                registry=registry,
                codex=str(codex),
                receipt_path=receipt_path,
                host_facts={"controller_host": "desktop_codex", "resume_actionable": True},
            )
            write_target("desktop-current", 2)
            second = web_bridge.dispatch_pending_lifecycle_wake(
                lifecycle_state=state,
                session_id="controller-1",
                repo=repo,
                registry=registry,
                codex=str(codex),
                receipt_path=receipt_path,
                host_facts={"controller_host": "desktop_codex", "resume_actionable": True},
            )

            self.assertEqual(first["execution_target_session_id"], "desktop-old")
            self.assertEqual(second["execution_target_session_id"], "desktop-current")
            self.assertFalse(second.get("debounced", False))
            self.assertIn("resume desktop-current", marker.read_text(encoding="utf-8"))

    def test_active_controller_is_a_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            receipt, _, marker = self.wake(
                Path(tmp),
                lifecycle_state={"pending_control_event": True, "triggers": ["active_lease_expired:F1"]},
                host_facts={"controller_host": "web", "controller_execution_active": True},
            )

            self.assertEqual(receipt["decision"], "NOOP_ACTIVE")
            self.assertEqual(receipt["result"], "CONFIRMED")
            self.assertFalse(marker.exists())

    def test_active_writer_defers_without_resuming_or_falling_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            receipt, _, marker = self.wake(
                Path(tmp),
                lifecycle_state={"pending_control_event": True, "triggers": ["active_lease_expired:F1"]},
                host_facts={
                    "controller_host": "web",
                    "active_writer": True,
                    "resume_state": "RESUME_DEFERRED_ACTIVE_WRITER",
                    "peer_host_available": True,
                },
            )

            self.assertEqual(receipt["decision"], "DEFER")
            self.assertEqual(receipt["result"], "DEFERRED")
            self.assertFalse(marker.exists())

    def test_peer_adapter_without_host_attestation_verifier_defers(self) -> None:
        calls: list[dict] = []

        def desktop_resume(**kwargs: object) -> dict:
            calls.append(dict(kwargs))
            return {
                "result": "CONFIRMED",
                "operation": "desktop-resume",
                "execution_target_session_id": kwargs["execution_target_session_id"],
                "target_generation": kwargs["target_generation"],
            }

        with tempfile.TemporaryDirectory() as tmp:
            receipt, _, marker = self.wake(
                Path(tmp),
                lifecycle_state={"pending_control_event": True, "triggers": ["active_lease_expired:F1"]},
                host_facts={
                    "controller_host": "web",
                    "active_writer": False,
                    "resume_state": "RESUME_FAILED",
                    "failure_class": "quota_exhausted",
                    "fallback_eligible": True,
                    "peer_host_available": True,
                    "peer_host": "desktop_codex",
                    "fallback_safe": True,
                    "peer_wake_authorized": True,
                },
                resume_adapters={"desktop_codex": desktop_resume},
            )

            self.assertEqual(receipt["decision"], "FALLBACK_PEER_HOST")
            self.assertEqual(receipt["selected_host"], "desktop_codex")
            self.assertEqual(receipt["controller_id"], "controller-1")
            self.assertEqual(receipt["result"], "DEFERRED")
            self.assertIn("host-attested verifier", receipt.get("diagnostics", ""))
            self.assertEqual(calls, [])
            self.assertFalse(marker.exists())

    def test_wake_callers_cannot_inject_a_peer_attestation_verifier(self) -> None:
        def desktop_resume(**kwargs: object) -> dict:
            return {
                "result": "CONFIRMED",
                "operation": "desktop-resume",
                "execution_target_session_id": kwargs["execution_target_session_id"],
                "target_generation": kwargs["target_generation"],
                "host_execution_receipt": {"host": "desktop_codex", "launch_id": "untrusted"},
            }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, registry, codex, receipt_path, marker = self.make_controller(root)
            with self.assertRaises(TypeError):
                web_bridge.wake_existing_controller(
                    lifecycle_state={"pending_control_event": True, "triggers": ["active_lease_expired:F1"]},
                    session_id="controller-1",
                    repo=repo,
                    registry=registry,
                    codex=str(codex),
                    receipt_path=receipt_path,
                    host_facts={
                        "controller_host": "web",
                        "active_writer": False,
                        "resume_state": "RESUME_FAILED",
                        "failure_class": "quota_exhausted",
                        "fallback_eligible": True,
                        "peer_host_available": True,
                        "peer_host": "desktop_codex",
                        "fallback_safe": True,
                    "peer_wake_authorized": True,
                    },
                    resume_adapters={"desktop_codex": desktop_resume},
                    peer_attestation_verifiers={"desktop_codex": lambda **_kwargs: True},
                )
            self.assertFalse(marker.exists())

    def test_peer_desktop_adapter_receives_and_attests_current_target(self) -> None:
        calls: list[dict] = []
        expected_host_receipt = {
            "host": "desktop_codex",
            "execution_target_session_id": "desktop-current",
            "target_generation": 8,
            "launch_id": "desktop-launch-17",
        }

        def desktop_resume(**kwargs: object) -> dict:
            calls.append(dict(kwargs))
            return {
                "result": "CONFIRMED",
                "operation": "desktop-resume",
                "execution_target_session_id": kwargs["execution_target_session_id"],
                "target_generation": kwargs["target_generation"],
                "host_execution_receipt": expected_host_receipt,
            }

        def verify_desktop_attestation(**kwargs: object) -> bool:
            return (
                kwargs["controller_id"] == "controller-1"
                and kwargs["expected_target_session_id"] == "desktop-current"
                and kwargs["expected_target_generation"] == 8
                and kwargs["host_execution_receipt"] == expected_host_receipt
            )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, registry, codex, receipt_path, marker = self.make_controller(root)
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {
                    "controller-1": {
                        "web": ["web-session-1"],
                        "desktop_codex": ["desktop-current"],
                    }
                },
                "__controller_targets__": {
                    "controller-1": {
                        "desktop_codex": {
                            "status": "active",
                            "session_id": "desktop-current",
                            "generation": 8,
                        }
                    }
                },
            }), encoding="utf-8")

            from unittest.mock import patch
            with patch.object(
                web_bridge,
                "_registered_peer_attestation_verifier",
                return_value=verify_desktop_attestation,
                create=True,
            ):
                receipt = web_bridge.wake_existing_controller(
                    lifecycle_state={"pending_control_event": True, "triggers": ["active_lease_expired:F1"]},
                    session_id="controller-1",
                    repo=repo,
                    registry=registry,
                    codex=str(codex),
                    receipt_path=receipt_path,
                    host_facts={
                        "controller_host": "web",
                        "active_writer": False,
                        "resume_state": "RESUME_FAILED",
                        "failure_class": "quota_exhausted",
                        "fallback_eligible": True,
                        "peer_host_available": True,
                        "peer_host": "desktop_codex",
                        "fallback_safe": True,
                    "peer_wake_authorized": True,
                    },
                    resume_adapters={"desktop_codex": desktop_resume},
                )

            self.assertEqual(receipt["result"], "CONFIRMED")
            self.assertEqual(calls[0]["controller_id"], "controller-1")
            self.assertEqual(calls[0]["session_id"], "desktop-current")
            self.assertEqual(calls[0]["execution_target_session_id"], "desktop-current")
            self.assertEqual(calls[0]["target_generation"], 8)
            self.assertEqual(receipt["execution_target_session_id"], "desktop-current")
            self.assertEqual(receipt["target_generation"], 8)
            self.assertFalse(marker.exists())

    def test_peer_host_attestation_rejects_stale_actual_execution(self) -> None:
        def desktop_resume(**kwargs: object) -> dict:
            return {
                "result": "CONFIRMED",
                "operation": "desktop-resume",
                # A stale host may self-report the target it was asked to use.
                "execution_target_session_id": kwargs["execution_target_session_id"],
                "target_generation": kwargs["target_generation"],
                "host_execution_receipt": {
                    "host": "desktop_codex",
                    "execution_target_session_id": "desktop-stale",
                    "target_generation": 1,
                    "launch_id": "desktop-launch-stale",
                },
            }

        def verify_desktop_attestation(**kwargs: object) -> bool:
            host_receipt = kwargs["host_execution_receipt"]
            return (
                isinstance(host_receipt, dict)
                and host_receipt.get("execution_target_session_id")
                == kwargs["expected_target_session_id"]
                and host_receipt.get("target_generation")
                == kwargs["expected_target_generation"]
            )

        with tempfile.TemporaryDirectory() as tmp:
            from unittest.mock import patch
            with patch.object(
                web_bridge,
                "_registered_peer_attestation_verifier",
                return_value=verify_desktop_attestation,
                create=True,
            ):
                receipt, _, marker = self.wake(
                    Path(tmp),
                    lifecycle_state={"pending_control_event": True, "triggers": ["active_lease_expired:F1"]},
                    host_facts={
                        "controller_host": "web",
                        "active_writer": False,
                        "resume_state": "RESUME_FAILED",
                        "failure_class": "quota_exhausted",
                        "fallback_eligible": True,
                        "peer_host_available": True,
                        "peer_host": "desktop_codex",
                        "fallback_safe": True,
                    "peer_wake_authorized": True,
                    },
                    resume_adapters={"desktop_codex": desktop_resume},
                )

            self.assertEqual(receipt["result"], "FAILED")
            self.assertIn("host attestation rejected", receipt.get("diagnostics", ""))
            self.assertFalse(marker.exists())

    def test_peer_adapter_exception_persists_a_bounded_failed_receipt(self) -> None:
        from unittest.mock import patch

        for error_type in (TypeError, RuntimeError):
            with self.subTest(error_type=error_type.__name__), tempfile.TemporaryDirectory() as tmp:
                def desktop_resume(**_kwargs: object) -> dict:
                    raise error_type("peer host launch failed: " + "x" * 4096)

                with patch.object(
                    web_bridge,
                    "_registered_peer_attestation_verifier",
                    return_value=lambda **_kwargs: True,
                    create=True,
                ):
                    receipt, receipt_path, marker = self.wake(
                        Path(tmp),
                        lifecycle_state={"pending_control_event": True, "triggers": ["active_lease_expired:F1"]},
                        host_facts={
                            "controller_host": "web",
                            "active_writer": False,
                            "resume_state": "RESUME_FAILED",
                            "failure_class": "quota_exhausted",
                            "fallback_eligible": True,
                            "peer_host_available": True,
                            "peer_host": "desktop_codex",
                            "fallback_safe": True,
                    "peer_wake_authorized": True,
                        },
                        resume_adapters={"desktop_codex": desktop_resume},
                    )

                self.assertEqual(receipt["result"], "FAILED")
                self.assertEqual(receipt["error_code"], "PEER_HOST_ADAPTER_FAILED")
                self.assertTrue(receipt["pending_control_event"])
                self.assertLessEqual(len(receipt["diagnostics"]), web_bridge.STDERR_TAIL_LIMIT)
                self.assertEqual(json.loads(receipt_path.read_text(encoding="utf-8")), receipt)
                self.assertFalse(marker.exists())

    def test_ambiguous_or_unsafe_failure_does_not_fall_back(self) -> None:
        calls: list[dict] = []

        def desktop_resume(**kwargs: object) -> dict:
            calls.append(dict(kwargs))
            return {"result": "CONFIRMED"}

        unsafe_cases = (
            {"failure_class": "resume_failed"},
            {"failure_class": "quota_exhausted", "unknown_side_effect": True},
            {"failure_class": "quota_exhausted", "partial_write": True},
        )
        for unsafe in unsafe_cases:
            with self.subTest(**unsafe), tempfile.TemporaryDirectory() as tmp:
                receipt, _, _ = self.wake(
                    Path(tmp),
                    lifecycle_state={"pending_control_event": True, "triggers": ["active_lease_expired:F1"]},
                    host_facts={
                        "controller_host": "web",
                        "active_writer": False,
                        "resume_state": "RESUME_FAILED",
                        "fallback_eligible": True,
                        "peer_host_available": True,
                        "peer_host": "desktop_codex",
                        "fallback_safe": True,
                        **unsafe,
                    },
                    resume_adapters={"desktop_codex": desktop_resume},
                )

                self.assertEqual(receipt["decision"], "DEFER")
                self.assertEqual(receipt["result"], "DEFERRED")
        self.assertEqual(calls, [])

    def test_dead_health_blocks_automatic_wake(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            receipt, _, marker = self.wake(
                Path(tmp),
                lifecycle_state={"pending_control_event": True, "triggers": ["active_lease_expired:F1"]},
                host_facts={
                    "controller_host": "web",
                    "active_writer": False,
                    "resume_state": "RESUME_FAILED",
                    "failure_class": "runtime_unavailable",
                    "fallback_eligible": True,
                    "peer_host_available": False,
                    "fallback_safe": True,
                    "failure_conclusive": True,
                },
            )

            self.assertEqual(receipt["decision"], "DEAD_BLOCK")
            self.assertEqual(receipt["result"], "BLOCKED")
            self.assertFalse(marker.exists())

    def test_common_dir_wake_lock_rejects_concurrent_wake(self) -> None:
        import fcntl

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, registry, codex, receipt_path, marker = self.make_controller(root)
            lock_path = web_bridge.controller_wake_lock_path(repo)
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            holder = lock_path.open("a+")
            fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                receipt = web_bridge.wake_existing_controller(
                    lifecycle_state={"pending_control_event": True, "triggers": ["active_lease_expired:F1"]},
                    session_id="controller-1",
                    repo=repo,
                    registry=registry,
                    codex=str(codex),
                    receipt_path=receipt_path,
                    host_facts={"controller_host": "web", "resume_actionable": True},
                )
            finally:
                fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
                holder.close()

            self.assertEqual(receipt["decision"], "DEFER")
            self.assertEqual(receipt["result"], "DEFERRED")
            self.assertEqual(receipt["reason"], "common_dir_wake_locked")
            self.assertFalse(marker.exists())
            self.assertFalse(receipt_path.exists())
            self.assertFalse(list(root.glob(f".{receipt_path.name}.*")))

    def test_common_dir_resolution_failure_persists_a_bounded_atomic_receipt(self) -> None:
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, registry, codex, receipt_path, _ = self.make_controller(root)
            failure = subprocess.CalledProcessError(
                128,
                ["git", "rev-parse", "--git-common-dir"],
                stderr="x" * 4096,
            )

            with patch.object(web_bridge, "_git_common_dir", side_effect=failure):
                receipt = web_bridge.wake_existing_controller(
                    lifecycle_state={"pending_control_event": True, "triggers": ["READY:F1"]},
                    session_id="controller-1",
                    repo=repo,
                    registry=registry,
                    codex=str(codex),
                    receipt_path=receipt_path,
                    host_facts={"controller_host": "web", "resume_actionable": True},
                )

            saved = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertEqual(receipt, saved)
            self.assertEqual(saved["decision"], "DEAD_BLOCK")
            self.assertEqual(saved["result"], "BLOCKED")
            self.assertLessEqual(len(saved["reason"]), 512)
            self.assertFalse(list(root.glob(f".{receipt_path.name}.*")))

    def test_unregistered_current_web_host_adapter_is_not_called(self) -> None:
        from unittest.mock import patch

        adapter_calls: list[dict] = []

        def supplied_current_host_adapter(**kwargs: object) -> dict:
            adapter_calls.append(dict(kwargs))
            return {"result": "CONFIRMED", "operation": "untrusted-current-host-adapter"}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, registry, codex, receipt_path, marker = self.make_controller(root)
            _provision_verified_current_web_target(
                registry, web_session_id="web-session-1", target_generation=1, ownership_generation=1
            )
            missing_verifier = root / "missing-host-verifiers.json"
            with patch.object(
                web_bridge,
                "DEFAULT_PEER_ATTESTATION_VERIFIER_CONFIG",
                missing_verifier,
                create=True,
            ), patch.object(
                web_bridge,
                "execute_native_resume",
                wraps=web_bridge.execute_native_resume,
            ) as native_resume, patch.object(
                web_bridge, "_registered_peer_attestation_verifier", return_value=None
            ):
                receipt = web_bridge.wake_existing_controller(
                    lifecycle_state={"pending_control_event": True, "triggers": ["READY:F1"]},
                    session_id="controller-1", repo=repo, registry=registry, codex=str(codex),
                    receipt_path=receipt_path,
                    host_facts={"controller_host": "web", "resume_actionable": True},
                    resume_adapters={"web": supplied_current_host_adapter},
                )

            self.assertEqual(receipt["decision"], "RESUME_CURRENT_HOST")
            self.assertEqual(receipt["result"], "DEFERRED")
            self.assertEqual(
                receipt["error_code"],
                "WEB_HOST_ATTESTATION_VERIFIER_UNAVAILABLE",
            )
            self.assertEqual(adapter_calls, [])
            self.assertEqual(native_resume.call_count, 0)
            self.assertFalse(marker.exists())

    def test_peer_adapter_metadata_is_json_safe_bounded_and_persisted(self) -> None:
        calls: list[dict] = []

        def desktop_resume(**kwargs: object) -> dict:
            calls.append(dict(kwargs))
            return {
                "result": "CONFIRMED",
                "operation": "peer-operation-" * 1024,
                "command": ["peer-resume", object(), "unbounded-command-argument" * 1024],
                "stderr_tail": {"diagnostic": object()},
                "execution_target_session_id": kwargs["execution_target_session_id"],
                "target_generation": kwargs["target_generation"],
                "host_execution_receipt": {"host": "desktop_codex", "launch_id": "metadata-test"},
            }

        def verify_desktop_attestation(**kwargs: object) -> bool:
            return kwargs["host_execution_receipt"] == {
                "host": "desktop_codex", "launch_id": "metadata-test"
            }

        with tempfile.TemporaryDirectory() as tmp:
            from unittest.mock import patch
            with patch.object(
                web_bridge,
                "_registered_peer_attestation_verifier",
                return_value=verify_desktop_attestation,
                create=True,
            ):
                receipt, receipt_path, _ = self.wake(
                    Path(tmp),
                    lifecycle_state={"pending_control_event": True, "triggers": ["READY:F1"]},
                    host_facts={
                        "controller_host": "web",
                        "active_writer": False,
                        "resume_state": "RESUME_FAILED",
                        "failure_class": "quota_exhausted",
                        "fallback_eligible": True,
                        "peer_host_available": True,
                        "peer_host": "desktop_codex",
                        "fallback_safe": True,
                    "peer_wake_authorized": True,
                    },
                    resume_adapters={"desktop_codex": desktop_resume},
                )

            saved = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertEqual(receipt, saved)
            self.assertEqual(len(calls), 1)
            self.assertLessEqual(len(saved["operation"]), 512)
            self.assertNotIn("command", saved)
            self.assertLessEqual(len(saved["diagnostics"]), web_bridge.STDERR_TAIL_LIMIT)
            self.assertIn("non-text adapter diagnostics", saved["diagnostics"])

    def test_linked_worktree_uses_registered_controller_from_its_common_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, registry, codex, receipt_path, marker = self.make_controller(root)
            subprocess.run(
                ["git", "-C", str(repo), "-c", "user.email=test@example.com", "-c", "user.name=Test", "commit", "--allow-empty", "-qm", "init"],
                check=True,
            )
            worktree = root / "controller-worktree"
            subprocess.run(
                ["git", "-C", str(repo), "worktree", "add", "-q", "-b", "controller-feature", str(worktree)],
                check=True,
            )
            try:
                receipt = web_bridge.wake_existing_controller(
                    lifecycle_state={"pending_control_event": True, "triggers": ["active_lease_expired:F1"]},
                    session_id="controller-1",
                    repo=worktree,
                    registry=registry,
                    codex=str(codex),
                    receipt_path=receipt_path,
                    host_facts={"controller_host": "desktop_codex", "resume_actionable": True},
                )
            finally:
                subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(worktree)], check=True)

            self.assertEqual(receipt["decision"], "RESUME_CURRENT_HOST")
            self.assertEqual(receipt["result"], "CONFIRMED")
            self.assertEqual(receipt["canonical_common_dir"], str(web_bridge._git_common_dir(repo)))
            self.assertIn("resume controller-1", marker.read_text(encoding="utf-8"))

    def test_wake_passes_pending_terminal_receipts_to_native_resume(self) -> None:
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, registry, codex, receipt_path, _ = self.make_controller(root)
            terminal = str(root / "reviewer-terminal.json")
            with patch.object(
                web_bridge, "execute_native_resume",
                return_value={
                    "operation": "native_resume", "result": "CONFIRMED",
                    "state": "RESUME_SUCCEEDED", "pending_control_event": True,
                    "returncode": 0, "stdout_tail": "", "stderr_tail": "",
                },
            ) as resume:
                receipt = web_bridge.wake_existing_controller(
                    lifecycle_state={
                        "pending_control_event": True, "triggers": ["subagent_stopped:reviewer-1"],
                        "pending_terminal_receipts": [terminal],
                    },
                    session_id="controller-1", repo=repo, registry=registry, codex=str(codex),
                    receipt_path=receipt_path,
                    host_facts={"controller_host": "desktop_codex", "resume_actionable": True},
                )
            self.assertEqual(receipt["result"], "CONFIRMED")
            self.assertEqual(resume.call_args.kwargs["terminal_receipts"], [terminal])

    def test_confirmed_wake_keeps_pending_control_event_true(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            receipt, receipt_path, _ = self.wake(
                Path(tmp),
                lifecycle_state={"pending_control_event": True, "triggers": ["READY:F1"]},
                host_facts={"controller_host": "desktop_codex", "resume_actionable": True},
            )

            self.assertEqual(receipt["result"], "CONFIRMED")
            self.assertTrue(receipt["pending_control_event"])
            self.assertTrue(json.loads(receipt_path.read_text(encoding="utf-8"))["pending_control_event"])

    def test_pending_trigger_classes_share_one_generic_wake_dispatcher(self) -> None:
        trigger_sets = (
            ["active_lease_expired:F1"],
            ["READY:F1"],
            ["CANDIDATE:candidate-123", "candidate_queue_changed"],
            ["rule_update_pending:rev-goal"],
            ["ledger_changed", "main_worktree_changed"],
        )
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, registry, codex, receipt_path, marker = self.make_controller(root)
            with patch.object(
                web_bridge, "execute_native_resume", wraps=web_bridge.execute_native_resume
            ) as native_resume:
                for triggers in trigger_sets:
                    receipt = web_bridge.dispatch_pending_lifecycle_wake(
                        lifecycle_state={"pending_control_event": True, "triggers": triggers, "controller_host": "desktop_codex"},
                        session_id="controller-1",
                        repo=repo,
                        registry=registry,
                        codex=str(codex),
                        receipt_path=receipt_path,
                        host_facts={"controller_host": "desktop_codex", "resume_actionable": True},
                    )
                    self.assertEqual(receipt["decision"], "RESUME_CURRENT_HOST")
                    self.assertEqual(receipt["controller_id"], "controller-1")
                    self.assertTrue(receipt["pending_control_event"])
            self.assertEqual(native_resume.call_count, len(trigger_sets))
            self.assertIn("resume controller-1", marker.read_text(encoding="utf-8"))

    def test_unchanged_pending_fingerprint_does_not_storm_duplicate_continuations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, registry, codex, receipt_path, marker = self.make_controller(root)
            state = {
                "pending_control_event": True,
                "triggers": ["active_lease_expired:F1"],
                "controller_host": "desktop_codex",
            }
            first = web_bridge.dispatch_pending_lifecycle_wake(
                lifecycle_state=state,
                session_id="controller-1",
                repo=repo,
                registry=registry,
                codex=str(codex),
                receipt_path=receipt_path,
                host_facts={"controller_host": "desktop_codex", "resume_actionable": True},
            )
            second = web_bridge.dispatch_pending_lifecycle_wake(
                lifecycle_state=state,
                session_id="controller-1",
                repo=repo,
                registry=registry,
                codex=str(codex),
                receipt_path=receipt_path,
                host_facts={"controller_host": "desktop_codex", "resume_actionable": True},
            )
            self.assertEqual(first["result"], "CONFIRMED")
            self.assertEqual(second["event_fingerprint"], first["event_fingerprint"])
            self.assertTrue(second.get("debounced") is True)
            self.assertEqual(marker.read_text(encoding="utf-8").count("resume controller-1"), 1)

    def test_lifecycle_wake_generation_stays_stable_for_unchanged_pending_event(self) -> None:
        scripts_dir = str(ROOT / "scripts")
        inserted = scripts_dir not in sys.path
        if inserted:
            sys.path.insert(0, scripts_dir)
        try:
            lifecycle = web_bridge._lifecycle_module()
        finally:
            if inserted:
                sys.path.remove(scripts_dir)
        snapshot = {
            "root": str(ROOT), "head": "h", "ledger_sha256": "l", "worktree_status_sha256": "s",
            "ready_ids": ["F1"], "runnable_ids": ["F1"], "candidate_revisions": [],
            "assignment_liveness": {}, "rule_handshake": {},
        }
        event = {
            "hook_event_name": "PostToolUse", "session_id": "controller-1", "controller_host": "web",
            "tool_input": {"command": "true"}, "tool_response": {"exit_code": 0},
        }
        _, first = lifecycle.evaluate_event(event, snapshot=snapshot, prior_state=None)
        _, second = lifecycle.evaluate_event(event, snapshot=snapshot, prior_state=first)
        self.assertEqual(first["wake_generation"], 1)
        self.assertEqual(second["wake_generation"], 1)

    def test_same_snapshot_and_triggers_with_new_wake_generation_are_not_debounced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, registry, codex, receipt_path, _ = self.make_controller(root)
            base = {
                "pending_control_event": True,
                "triggers": ["READY:F1"],
                "controller_host": "desktop_codex",
                "snapshot": {
                    "head": "h1", "ledger_sha256": "l1", "worktree_status_sha256": "s1",
                    "ready_ids": ["F1"], "runnable_ids": ["F1"], "candidate_revisions": [],
                },
            }
            first_state = {**base, "wake_generation": 10}
            second_state = {**base, "wake_generation": 11}
            from unittest.mock import patch
            with patch.object(web_bridge, "execute_native_resume", wraps=web_bridge.execute_native_resume) as resume:
                first = web_bridge.dispatch_pending_lifecycle_wake(
                    lifecycle_state=first_state, session_id="controller-1", repo=repo, registry=registry,
                    codex=str(codex), receipt_path=receipt_path,
                    host_facts={"controller_host": "desktop_codex", "resume_actionable": True},
                )
                second = web_bridge.dispatch_pending_lifecycle_wake(
                    lifecycle_state=second_state, session_id="controller-1", repo=repo, registry=registry,
                    codex=str(codex), receipt_path=receipt_path,
                    host_facts={"controller_host": "desktop_codex", "resume_actionable": True},
                )
            self.assertEqual(first["result"], "CONFIRMED")
            self.assertEqual(second["result"], "CONFIRMED")
            self.assertNotEqual(first["event_fingerprint"], second["event_fingerprint"])
            self.assertFalse(second.get("debounced", False))
            self.assertEqual(resume.call_count, 2)

    def test_confirmed_wake_receipt_requires_same_controller_and_common_dir_to_debounce(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, registry, codex, receipt_path, _ = self.make_controller(root)
            state = {
                "pending_control_event": True, "triggers": ["READY:F1"], "wake_generation": 4,
                "controller_host": "desktop_codex", "snapshot": {"head": "h", "ledger_sha256": "l", "worktree_status_sha256": "s"},
            }
            fingerprint = web_bridge._wake_event_fingerprint(state)
            receipt_path.write_text(json.dumps({
                "result": "CONFIRMED", "event_fingerprint": fingerprint,
                "controller_id": "old-controller",
                "canonical_common_dir": str(web_bridge._git_common_dir(repo)),
            }), encoding="utf-8")
            from unittest.mock import patch
            with patch.object(web_bridge, "execute_native_resume", wraps=web_bridge.execute_native_resume) as resume:
                result = web_bridge.dispatch_pending_lifecycle_wake(
                    lifecycle_state=state, session_id="controller-1", repo=repo, registry=registry,
                    codex=str(codex), receipt_path=receipt_path,
                    host_facts={"controller_host": "desktop_codex", "resume_actionable": True},
                )
            self.assertEqual(result["result"], "CONFIRMED")
            self.assertFalse(result.get("debounced", False))
            self.assertEqual(resume.call_count, 1)

    def test_confirmed_debounce_requires_current_controller_and_common_dir_identity(self) -> None:
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, registry, codex, receipt_path, _ = self.make_controller(root)
            state = {
                "pending_control_event": True,
                "triggers": ["READY:F1"],
                "controller_host": "desktop_codex",
                "wake_generation": 7,
                "snapshot": {
                    "head": "h1", "ledger_sha256": "l1", "worktree_status_sha256": "s1",
                    "ready_ids": ["F1"], "runnable_ids": ["F1"], "candidate_revisions": [],
                },
            }
            fingerprint = web_bridge._wake_event_fingerprint(state)
            common_dir = str(web_bridge._git_common_dir(repo))
            stale_receipts = (
                {
                    "event_fingerprint": fingerprint, "result": "CONFIRMED",
                    "controller_id": "old-controller", "canonical_common_dir": common_dir,
                },
                {
                    "event_fingerprint": fingerprint, "result": "CONFIRMED",
                    "controller_id": "controller-1", "canonical_common_dir": str(root / "wrong-common-dir"),
                },
            )
            for prior in stale_receipts:
                with self.subTest(prior=prior):
                    receipt_path.write_text(json.dumps(prior), encoding="utf-8")
                    with patch.object(
                        web_bridge, "execute_native_resume", wraps=web_bridge.execute_native_resume
                    ) as native_resume:
                        receipt = web_bridge.dispatch_pending_lifecycle_wake(
                            lifecycle_state=state, session_id="controller-1", repo=repo, registry=registry,
                            codex=str(codex), receipt_path=receipt_path,
                            host_facts={"controller_host": "desktop_codex", "resume_actionable": True},
                        )
                    self.assertEqual(receipt["result"], "CONFIRMED")
                    self.assertFalse(receipt.get("debounced", False))
                    self.assertEqual(native_resume.call_count, 1)
                    self.assertEqual(receipt["controller_id"], "controller-1")
                    self.assertEqual(receipt["canonical_common_dir"], common_dir)

    def test_confirmed_debounce_rejects_session_no_longer_registered_for_common_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, registry, codex, receipt_path, _ = self.make_controller(root)
            state = {
                "pending_control_event": True, "triggers": ["READY:F1"], "wake_generation": 9,
                "controller_host": "web",
                "snapshot": {"head": "h", "ledger_sha256": "l", "worktree_status_sha256": "s"},
            }
            receipt_path.write_text(json.dumps({
                "event_fingerprint": web_bridge._wake_event_fingerprint(state),
                "result": "CONFIRMED",
                "controller_id": "controller-1",
                "canonical_common_dir": str(web_bridge._git_common_dir(repo)),
            }), encoding="utf-8")
            registry.write_text(json.dumps({"controller-new": str(repo.resolve())}), encoding="utf-8")
            result = web_bridge.dispatch_pending_lifecycle_wake(
                lifecycle_state=state, session_id="controller-1", repo=repo, registry=registry,
                codex=str(codex), receipt_path=receipt_path,
                host_facts={"controller_host": "web", "resume_actionable": True},
            )
            self.assertFalse(result.get("debounced", False))
            self.assertIn(result["result"], {"BLOCKED", "FAILED", "DEFERRED"})

    def test_same_trigger_labels_with_new_snapshot_are_not_debounced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, registry, codex, receipt_path, marker = self.make_controller(root)
            first_state = {
                "pending_control_event": True,
                "triggers": ["main_worktree_changed"],
                "controller_host": "desktop_codex",
                "snapshot": {
                    "head": "head-1",
                    "ledger_sha256": "ledger-1",
                    "worktree_status_sha256": "status-1",
                    "ready_ids": ["F1"],
                    "runnable_ids": ["F1"],
                    "candidate_revisions": [],
                    "rule_handshake": {"state": "current", "installed_revision": "rev-1"},
                },
            }
            second_state = {
                **first_state,
                "snapshot": {**first_state["snapshot"], "worktree_status_sha256": "status-2"},
            }
            from unittest.mock import patch
            with patch.object(
                web_bridge, "execute_native_resume", wraps=web_bridge.execute_native_resume
            ) as native_resume:
                first = web_bridge.dispatch_pending_lifecycle_wake(
                    lifecycle_state=first_state, session_id="controller-1", repo=repo, registry=registry,
                    codex=str(codex), receipt_path=receipt_path,
                    host_facts={"controller_host": "desktop_codex", "resume_actionable": True},
                )
                second = web_bridge.dispatch_pending_lifecycle_wake(
                    lifecycle_state=second_state, session_id="controller-1", repo=repo, registry=registry,
                    codex=str(codex), receipt_path=receipt_path,
                    host_facts={"controller_host": "desktop_codex", "resume_actionable": True},
                )
            self.assertEqual(first["result"], "CONFIRMED")
            self.assertEqual(second["result"], "CONFIRMED")
            self.assertNotEqual(second["event_fingerprint"], first["event_fingerprint"])
            self.assertFalse(second.get("debounced", False))
            self.assertEqual(native_resume.call_count, 2)
            self.assertIn("resume controller-1", marker.read_text(encoding="utf-8"))

    def test_deferred_wake_is_retryable_for_same_pending_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, registry, codex, receipt_path, marker = self.make_controller(root)
            state = {
                "pending_control_event": True,
                "triggers": ["active_lease_expired:F1"],
                "controller_host": "desktop_codex",
            }
            first = web_bridge.dispatch_pending_lifecycle_wake(
                lifecycle_state=state,
                session_id="controller-1",
                repo=repo,
                registry=registry,
                codex=str(codex),
                receipt_path=receipt_path,
                host_facts={"controller_host": "desktop_codex", "active_writer": True},
            )
            second = web_bridge.dispatch_pending_lifecycle_wake(
                lifecycle_state=state,
                session_id="controller-1",
                repo=repo,
                registry=registry,
                codex=str(codex),
                receipt_path=receipt_path,
                host_facts={"controller_host": "desktop_codex", "resume_actionable": True},
            )
            self.assertEqual(first["result"], "DEFERRED")
            self.assertEqual(second["result"], "CONFIRMED")
            self.assertFalse(second.get("debounced", False))
            self.assertIn("resume controller-1", marker.read_text(encoding="utf-8"))

    def test_lock_contention_does_not_overwrite_shared_wake_receipt(self) -> None:
        import fcntl

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, registry, codex, receipt_path, marker = self.make_controller(root)
            prior = {"event_fingerprint": "prior", "result": "CONFIRMED", "decision": "RESUME_CURRENT_HOST"}
            receipt_path.write_text(json.dumps(prior), encoding="utf-8")
            lock_path = web_bridge.controller_wake_lock_path(repo)
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            holder = lock_path.open("a+")
            fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                receipt = web_bridge.wake_existing_controller(
                    lifecycle_state={"pending_control_event": True, "triggers": ["READY:F1"]},
                    session_id="controller-1",
                    repo=repo,
                    registry=registry,
                    codex=str(codex),
                    receipt_path=receipt_path,
                    host_facts={"controller_host": "web", "resume_actionable": True},
                )
            finally:
                fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
                holder.close()
            self.assertEqual(receipt["result"], "DEFERRED")
            self.assertEqual(json.loads(receipt_path.read_text(encoding="utf-8")), prior)
            self.assertFalse(marker.exists())

    def test_confirmed_wake_stays_pending_until_control_event_guard_closes(self) -> None:
        spec = importlib.util.spec_from_file_location("task4_lifecycle_hook", ROOT / "scripts" / "lifecycle_hook.py")
        assert spec is not None and spec.loader is not None
        lifecycle = importlib.util.module_from_spec(spec)
        scripts_dir = str(ROOT / "scripts")
        inserted = scripts_dir not in sys.path
        if inserted:
            sys.path.insert(0, scripts_dir)
        try:
            spec.loader.exec_module(lifecycle)
        finally:
            if inserted:
                sys.path.remove(scripts_dir)
        snapshot = {
            "head": "abc123",
            "ledger_sha256": "ledger-2",
            "worktree_status_sha256": "status-2",
            "ready_ids": [],
            "runnable_ids": [],
            "candidate_revisions": [],
            "ledger_errors": [],
            "assignment_liveness": {},
        }
        with tempfile.TemporaryDirectory() as tmp:
            receipt, receipt_path, _ = self.wake(
                Path(tmp),
                lifecycle_state={"pending_control_event": True, "triggers": ["active_lease_expired:F1"]},
                host_facts={"controller_host": "desktop_codex", "resume_actionable": True},
            )
            self.assertEqual(receipt["result"], "CONFIRMED")
            self.assertTrue(receipt["pending_control_event"])
            self.assertTrue(json.loads(receipt_path.read_text(encoding="utf-8"))["pending_control_event"])

            prior = {
                "pending_control_event": True,
                "triggers": ["active_lease_expired:F1"],
                "snapshot": dict(snapshot),
            }
            _, still_pending = lifecycle.evaluate_event(
                {
                    "hook_event_name": "PostToolUse",
                    "session_id": "controller-1",
                    "tool_input": {"command": "git status --short"},
                    "tool_response": {"output": "", "exit_code": 0},
                },
                snapshot=snapshot,
                prior_state=prior,
            )
            self.assertTrue(still_pending["pending_control_event"])

            _, closed = lifecycle.evaluate_event(
                {
                    "hook_event_name": "PostToolUse",
                    "session_id": "controller-1",
                    "tool_input": {
                        "command": (
                            f"{sys.executable} {ROOT / 'scripts' / 'control_event_guard.py'} "
                            "receipt.json --ledger TASK_LEDGER.md"
                        )
                    },
                    "tool_response": {"output": "control-event: allowed", "exit_code": 0},
                },
                snapshot=snapshot,
                prior_state=still_pending,
            )
            self.assertFalse(closed["pending_control_event"])
            self.assertEqual(closed["triggers"], [])

    def test_entrypoints_fail_closed_when_wake_is_not_confirmed(self) -> None:
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, registry, codex, _receipt_path, _ = self.make_controller(root)
            state = {"pending_control_event": True, "triggers": ["READY:F1"], "controller_host": "web"}
            for result in ("DEFERRED", "FAILED", "BLOCKED"):
                with self.subTest(result=result), patch.object(
                    web_bridge, "_load_lifecycle_state", return_value=state
                ), patch.object(
                    web_bridge, "dispatch_pending_lifecycle_wake", return_value={
                        "result": result, "decision": "DEFER", "pending_control_event": True
                    }
                ), patch.object(web_bridge, "dispatch_event", return_value=0), patch.object(
                    web_bridge, "schedule_auto_native_stop", return_value=True
                ):
                    post = web_bridge.main([
                        "post-shell", "--cwd", str(repo), "--command", "true",
                        "--exit-code", "0", "--registry", str(registry), "--web-session-id", "web-session-1",
                    ])
                    native = web_bridge.main([
                        "native-stop", "--session-id", "controller-1", "--repo", str(repo),
                        "--registry", str(registry), "--codex", str(codex),
                    ])
                    self.assertNotEqual(post, 0)
                    self.assertNotEqual(native, 0)

    def test_entrypoints_fail_closed_when_pending_wake_dispatch_returns_none(self) -> None:
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, registry, codex, _receipt_path, marker = self.make_controller(root)
            state = {"pending_control_event": True, "triggers": ["READY:F1"], "controller_host": "web"}
            with patch.object(web_bridge, "_load_lifecycle_state", return_value=state), patch.object(
                web_bridge, "dispatch_pending_lifecycle_wake", return_value=None
            ), patch.object(web_bridge, "dispatch_event", return_value=0), patch.object(
                web_bridge, "preflight_native_resume", side_effect=AssertionError("direct resume must not run")
            ):
                post = web_bridge.main([
                    "post-shell", "--cwd", str(repo), "--command", "true",
                    "--exit-code", "0", "--registry", str(registry), "--web-session-id", "web-session-1",
                ])
                native = web_bridge.main([
                    "native-stop", "--session-id", "controller-1", "--repo", str(repo),
                    "--registry", str(registry), "--codex", str(codex),
                ])
            self.assertNotEqual(post, 0)
            self.assertNotEqual(native, 0)
            self.assertFalse(marker.exists())

    def test_audit_wake_pending_ignores_capture_mode_and_retries_only_wake(self) -> None:
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, registry, codex, _receipt_path, _ = self.make_controller(root)
            _provision_verified_current_web_target(registry, web_session_id="web-session-1")
            audit = root / "audit.jsonl"
            receipt = {
                "receiptId": "wake-capture-retry-1", "childTool": "shell_command", "state": "succeeded",
                "rootLabel": str(repo), "targetLabel": GUARD_COMMAND,
                "detail": f"命令：{GUARD_COMMAND}\n\n命令输出：\ncontrol-event: allowed\n",
            }
            audit.write_text(json.dumps(receipt) + "\n", encoding="utf-8")
            cursor = root / "cursor.json"
            capture = root / "events.jsonl"
            state = {
                "pending_control_event": True, "triggers": ["READY:F1"],
                "snapshot": {"head": "h1", "ledger_sha256": "l1", "worktree_status_sha256": "s1"},
            }
            base_args = [
                "audit-once", "--repo", str(repo), "--session-id", "controller-1",
                "--audit-log", str(audit), "--cursor", str(cursor), "--registry", str(registry), "--web-session-id", "web-session-1",
                "--codex", str(codex),
            ]
            wake_results = [
                {"result": "DEFERRED", "decision": "DEFER", "pending_control_event": True},
                {"result": "CONFIRMED", "decision": "RESUME_CURRENT_HOST", "pending_control_event": True},
            ]
            with patch.object(web_bridge, "dispatch_event", return_value=0) as dispatch, patch.object(
                web_bridge, "_load_lifecycle_state", return_value=state
            ), patch.object(web_bridge, "dispatch_pending_lifecycle_wake", side_effect=wake_results) as wake:
                first = web_bridge.main(base_args)
                second = web_bridge.main(base_args + ["--capture-events", str(capture)])
            self.assertNotEqual(first, 0)
            self.assertEqual(second, 0)
            self.assertEqual(dispatch.call_count, 1)
            self.assertEqual(wake.call_count, 2)
            self.assertFalse(capture.exists())
            stored = json.loads(cursor.with_suffix(cursor.suffix + ".receipts.json").read_text(encoding="utf-8"))
            self.assertEqual(stored["receipts"]["wake-capture-retry-1"], "handled")
            self.assertEqual(json.loads(cursor.read_text(encoding="utf-8"))["offset"], audit.stat().st_size)

    def test_audit_wake_pending_is_strict_wake_only_even_with_capture_events(self) -> None:
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, registry, codex, _receipt_path, _ = self.make_controller(root)
            _provision_verified_current_web_target(registry, web_session_id="web-session-1")
            audit = root / "audit.jsonl"
            receipt = {
                "receiptId": "wake-only-capture-1", "childTool": "shell_command", "state": "succeeded",
                "rootLabel": str(repo), "targetLabel": GUARD_COMMAND,
                "detail": f"命令：{GUARD_COMMAND}\n\n命令输出：\ncontrol-event: allowed\n",
            }
            audit.write_text(json.dumps(receipt) + "\n", encoding="utf-8")
            cursor = root / "cursor.json"
            capture = root / "capture.jsonl"
            lifecycle_state = {
                "pending_control_event": True, "triggers": ["READY:F1"],
                "snapshot": {"head": "h1", "ledger_sha256": "l1", "worktree_status_sha256": "s1"},
            }
            state_path = cursor.with_suffix(cursor.suffix + ".receipts.json")
            state_path.write_text(json.dumps({
                "receipts": {"wake-only-capture-1": "wake_pending"},
                "wake_fingerprints": {
                    "wake-only-capture-1": web_bridge._wake_event_fingerprint(lifecycle_state)
                },
            }), encoding="utf-8")
            with patch.object(
                web_bridge, "successful_guard_event_from_receipt",
                side_effect=AssertionError("wake_pending must not reconstruct event"),
            ), patch.object(
                web_bridge, "computer_event_from_receipt",
                side_effect=AssertionError("wake_pending must not consume computer lease"),
            ), patch.object(
                web_bridge, "dispatch_event", side_effect=AssertionError("wake_pending must not redispatch")
            ), patch.object(
                web_bridge, "append_captured_event", side_effect=AssertionError("wake_pending must not capture")
            ), patch.object(
                web_bridge, "_load_lifecycle_state", return_value=lifecycle_state
            ), patch.object(
                web_bridge, "dispatch_pending_lifecycle_wake", return_value={
                    "result": "CONFIRMED", "decision": "RESUME_CURRENT_HOST", "pending_control_event": True
                }
            ) as wake:
                code = web_bridge.main([
                    "audit-once", "--repo", str(repo), "--session-id", "controller-1",
                    "--audit-log", str(audit), "--cursor", str(cursor), "--registry", str(registry), "--web-session-id", "web-session-1",
                    "--codex", str(codex), "--capture-events", str(capture),
                ])
            self.assertEqual(code, 0)
            self.assertEqual(wake.call_count, 1)
            self.assertFalse(capture.exists())
            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["receipts"]["wake-only-capture-1"], "handled")
            self.assertEqual(json.loads(cursor.read_text(encoding="utf-8"))["offset"], audit.stat().st_size)

    def test_audit_wake_pending_requires_same_generation_and_present_state(self) -> None:
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, registry, codex, _receipt_path, _ = self.make_controller(root)
            _provision_verified_current_web_target(registry, web_session_id="web-session-1")
            audit = root / "audit.jsonl"
            receipt = {
                "receiptId": "wake-generation-1", "childTool": "shell_command", "state": "succeeded",
                "rootLabel": str(repo), "targetLabel": GUARD_COMMAND,
                "detail": f"命令：{GUARD_COMMAND}\n\n命令输出：\ncontrol-event: allowed\n",
            }
            audit.write_text(json.dumps(receipt) + "\n", encoding="utf-8")
            cursor = root / "cursor.json"
            first_state = {
                "pending_control_event": True, "triggers": ["READY:F1"],
                "snapshot": {"head": "h1", "ledger_sha256": "l1", "worktree_status_sha256": "s1"},
            }
            changed_state = {
                "pending_control_event": True, "triggers": ["READY:F1"],
                "snapshot": {"head": "h2", "ledger_sha256": "l1", "worktree_status_sha256": "s1"},
            }
            args = [
                "audit-once", "--repo", str(repo), "--session-id", "controller-1",
                "--audit-log", str(audit), "--cursor", str(cursor), "--registry", str(registry), "--web-session-id", "web-session-1",
                "--codex", str(codex),
            ]
            with patch.object(web_bridge, "dispatch_event", return_value=0) as dispatch, patch.object(
                web_bridge, "_load_lifecycle_state", side_effect=[first_state, changed_state, {}]
            ), patch.object(
                web_bridge, "dispatch_pending_lifecycle_wake", return_value={
                    "result": "DEFERRED", "decision": "DEFER", "pending_control_event": True
                }
            ) as wake:
                first = web_bridge.main(args)
                changed = web_bridge.main(args)
                missing = web_bridge.main(args)
            self.assertNotEqual(first, 0)
            self.assertNotEqual(changed, 0)
            self.assertNotEqual(missing, 0)
            self.assertEqual(dispatch.call_count, 1)
            self.assertEqual(wake.call_count, 1)
            self.assertFalse(cursor.exists())
            state_path = cursor.with_suffix(cursor.suffix + ".receipts.json")
            stored = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(stored["receipts"]["wake-generation-1"], "wake_pending")
            self.assertEqual(
                stored["wake_fingerprints"]["wake-generation-1"],
                web_bridge._wake_event_fingerprint(first_state),
            )

    def test_audit_once_schedules_background_retry_when_computer_wake_hits_active_writer(self) -> None:
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, registry, codex, _receipt_path, _ = self.make_controller(root)
            _provision_verified_current_web_target(registry, web_session_id="web-session-1")
            audit = root / "audit.jsonl"
            receipt = {
                "receiptId": "claim-click-deferred-1", "childTool": "computer", "state": "succeeded",
                "targetLabel": "Google Chrome", "detail": "电脑操作：click · 应用 Google Chrome",
                "occurredAtUnixMs": 2000,
            }
            audit.write_text(json.dumps(receipt) + "\n", encoding="utf-8")
            cursor = root / "cursor.json"
            lease = root / "lease.json"
            lease.write_text(json.dumps({
                "session_id": "controller-1", "web_session_id": "web-session-1",
                "repo": str(repo.resolve()), "issued_at_unix_ms": 1000,
                "expires_at_unix_ms": 9999999999999, "remaining_uses": 1,
            }), encoding="utf-8")
            state = {
                "pending_control_event": True, "triggers": ["next_action_pending"],
                "next_action": "observe and read the result of the computer action before yielding",
                "requires_user": False,
            }
            args = [
                "audit-once", "--repo", str(repo), "--session-id", "controller-1",
                "--audit-log", str(audit), "--cursor", str(cursor), "--registry", str(registry),
                "--web-session-id", "web-session-1", "--codex", str(codex),
                "--computer-lease", str(lease), "--auto-native-stop", "--auto-stop-delay-seconds", "5",
                "--auto-stop-state", str(root / "auto-stop.json"),
            ]
            with patch.object(web_bridge, "dispatch_event", return_value=0), patch.object(
                web_bridge, "_load_lifecycle_state", return_value=state
            ), patch.object(
                web_bridge, "dispatch_pending_lifecycle_wake", return_value={
                    "result": "DEFERRED", "decision": "DEFER", "pending_control_event": True,
                    "reason": "active_writer_present",
                }
            ), patch.object(web_bridge, "schedule_auto_native_stop") as schedule:
                code = web_bridge.main(args)
            self.assertNotEqual(code, 0)
            schedule.assert_called_once()
            self.assertEqual(schedule.call_args.kwargs["receipt_id"], "claim-click-deferred-1")
            self.assertEqual(schedule.call_args.kwargs["session_id"], "controller-1")

    def test_audit_once_retries_wake_without_reconsuming_one_use_computer_lease(self) -> None:
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, registry, codex, _receipt_path, _ = self.make_controller(root)
            _provision_verified_current_web_target(registry, web_session_id="web-session-1")
            audit = root / "audit.jsonl"
            receipt = {
                "receiptId": "computer-wake-retry-1", "childTool": "computer", "state": "succeeded",
                "targetLabel": "Google Chrome", "detail": "电脑操作：get_app_state · 应用 Google Chrome",
                "occurredAtUnixMs": 2000,
            }
            audit.write_text(json.dumps(receipt) + "\n", encoding="utf-8")
            cursor = root / "cursor.json"
            lease = root / "lease.json"
            lease.write_text(json.dumps({
                "session_id": "controller-1", "web_session_id": "web-session-1",
                "repo": str(repo.resolve()), "issued_at_unix_ms": 1000, "expires_at_unix_ms": 9999999999999, "remaining_uses": 1,
            }), encoding="utf-8")
            state = {"pending_control_event": True, "triggers": ["main_worktree_changed"]}
            wake_results = [
                {"result": "DEFERRED", "decision": "DEFER", "pending_control_event": True},
                {"result": "CONFIRMED", "decision": "RESUME_CURRENT_HOST", "pending_control_event": True},
            ]
            args = [
                "audit-once", "--repo", str(repo), "--session-id", "controller-1",
                "--audit-log", str(audit), "--cursor", str(cursor), "--registry", str(registry), "--web-session-id", "web-session-1",
                "--codex", str(codex), "--computer-lease", str(lease),
            ]
            with patch.object(web_bridge, "dispatch_event", return_value=0) as dispatch, patch.object(
                web_bridge, "_load_lifecycle_state", return_value=state
            ), patch.object(web_bridge, "dispatch_pending_lifecycle_wake", side_effect=wake_results) as wake:
                first = web_bridge.main(args)
                self.assertFalse(lease.exists())
                second = web_bridge.main(args)
            self.assertNotEqual(first, 0)
            self.assertEqual(second, 0)
            self.assertEqual(dispatch.call_count, 1)
            self.assertEqual(wake.call_count, 2)
            self.assertEqual(json.loads(cursor.read_text(encoding="utf-8"))["offset"], audit.stat().st_size)

    def test_audit_once_retries_wake_after_dispatch_succeeded_but_wake_deferred(self) -> None:
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, registry, codex, _receipt_path, _ = self.make_controller(root)
            _provision_verified_current_web_target(registry, web_session_id="web-session-1")
            audit = root / "audit.jsonl"
            receipt = {
                "receiptId": "wake-retry-1", "childTool": "shell_command", "state": "succeeded",
                "rootLabel": str(repo), "targetLabel": GUARD_COMMAND,
                "detail": f"命令：{GUARD_COMMAND}\n\n命令输出：\ncontrol-event: allowed\n",
            }
            audit.write_text(json.dumps(receipt) + "\n", encoding="utf-8")
            cursor = root / "cursor.json"
            state = {"pending_control_event": True, "triggers": ["READY:F1"]}
            wake_results = [
                {"result": "DEFERRED", "decision": "DEFER", "pending_control_event": True},
                {"result": "CONFIRMED", "decision": "RESUME_CURRENT_HOST", "pending_control_event": True},
            ]
            args = [
                "audit-once", "--repo", str(repo), "--session-id", "controller-1",
                "--audit-log", str(audit), "--cursor", str(cursor), "--registry", str(registry), "--web-session-id", "web-session-1",
                "--codex", str(codex),
            ]
            with patch.object(web_bridge, "dispatch_event", return_value=0) as dispatch, patch.object(
                web_bridge, "_load_lifecycle_state", return_value=state
            ), patch.object(web_bridge, "dispatch_pending_lifecycle_wake", side_effect=wake_results) as wake:
                first = web_bridge.main(args)
                second = web_bridge.main(args)
            self.assertNotEqual(first, 0)
            self.assertEqual(second, 0)
            self.assertEqual(dispatch.call_count, 1)
            self.assertEqual(wake.call_count, 2)
            state_path = cursor.with_suffix(cursor.suffix + ".receipts.json")
            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["receipts"]["wake-retry-1"], "handled")
            self.assertEqual(json.loads(cursor.read_text(encoding="utf-8"))["offset"], audit.stat().st_size)

    def test_audit_once_keeps_receipt_pending_when_pending_wake_dispatch_returns_none(self) -> None:
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, registry, codex, _receipt_path, _ = self.make_controller(root)
            _provision_verified_current_web_target(registry, web_session_id="web-session-1")
            audit = root / "audit.jsonl"
            receipt = {
                "receiptId": "wake-none-1", "childTool": "shell_command", "state": "succeeded",
                "rootLabel": str(repo), "targetLabel": GUARD_COMMAND,
                "detail": f"命令：{GUARD_COMMAND}\n\n命令输出：\ncontrol-event: allowed\n",
            }
            audit.write_text(json.dumps(receipt) + "\n", encoding="utf-8")
            cursor = root / "cursor.json"
            with patch.object(web_bridge, "dispatch_event", return_value=0), patch.object(
                web_bridge, "_load_lifecycle_state", return_value={"pending_control_event": True, "triggers": ["READY:F1"]}
            ), patch.object(web_bridge, "dispatch_pending_lifecycle_wake", return_value=None):
                code = web_bridge.main([
                    "audit-once", "--repo", str(repo), "--session-id", "controller-1",
                    "--audit-log", str(audit), "--cursor", str(cursor), "--registry", str(registry), "--web-session-id", "web-session-1",
                    "--codex", str(codex),
                ])
            self.assertNotEqual(code, 0)
            state_path = cursor.with_suffix(cursor.suffix + ".receipts.json")
            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["receipts"]["wake-none-1"], "wake_pending")

    def test_audit_once_does_not_mark_receipt_handled_when_wake_is_not_confirmed(self) -> None:
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, registry, codex, _receipt_path, _ = self.make_controller(root)
            _provision_verified_current_web_target(registry, web_session_id="web-session-1")
            audit = root / "audit.jsonl"
            receipt = {
                "receiptId": "wake-fail-1", "childTool": "shell_command", "state": "succeeded",
                "rootLabel": str(repo), "targetLabel": GUARD_COMMAND,
                "detail": f"命令：{GUARD_COMMAND}\n\n命令输出：\ncontrol-event: allowed\n",
            }
            audit.write_text(json.dumps(receipt) + "\n", encoding="utf-8")
            cursor = root / "cursor.json"
            with patch.object(web_bridge, "dispatch_event", return_value=0), patch.object(
                web_bridge, "_load_lifecycle_state", return_value={"pending_control_event": True, "triggers": ["READY:F1"]}
            ), patch.object(
                web_bridge, "dispatch_pending_lifecycle_wake", return_value={
                    "result": "FAILED", "decision": "DEFER", "pending_control_event": True
                }
            ):
                code = web_bridge.main([
                    "audit-once", "--repo", str(repo), "--session-id", "controller-1",
                    "--audit-log", str(audit), "--cursor", str(cursor), "--registry", str(registry), "--web-session-id", "web-session-1",
                    "--codex", str(codex),
                ])
            self.assertNotEqual(code, 0)
            state_path = cursor.with_suffix(cursor.suffix + ".receipts.json")
            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["receipts"]["wake-fail-1"], "wake_pending")

    def test_post_shell_audit_and_native_stop_route_pending_events_through_one_dispatcher(self) -> None:
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, registry, codex, _receipt_path, _ = self.make_controller(root)
            _provision_verified_current_web_target(registry, web_session_id="web-session-1")
            calls: list[str] = []

            def capture_dispatch(**kwargs: object) -> dict:
                state = kwargs.get("lifecycle_state")
                triggers = state.get("triggers") if isinstance(state, dict) else None
                calls.append(str(triggers))
                return {"result": "CONFIRMED", "pending_control_event": True, "decision": "RESUME_CURRENT_HOST"}

            with patch.object(web_bridge, "dispatch_pending_lifecycle_wake", side_effect=capture_dispatch), patch.object(
                web_bridge, "dispatch_event", return_value=0
            ):
                post = web_bridge.main(
                    [
                        "post-shell",
                        "--cwd",
                        str(repo),
                        "--command",
                        "true",
                        "--exit-code",
                        "0",
                        "--registry",
                        str(registry),
                        "--web-session-id",
                        "web-session-1",
                    ]
                )
                native = web_bridge.main(
                    [
                        "native-stop",
                        "--session-id",
                        "controller-1",
                        "--repo",
                        str(repo),
                        "--registry",
                        str(registry),
                        "--codex",
                        str(codex),
                    ]
                )
            self.assertEqual(post, 0)
            self.assertEqual(native, 0)
            self.assertGreaterEqual(len(calls), 2)



class WebControllerSessionIdentityTests(unittest.TestCase):
    def run_bridge(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["/usr/bin/python3", str(BRIDGE), *args], text=True, capture_output=True, check=False)

    def test_session_start_refuses_repo_only_controller_attribution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
            (repo / "AGENTS.md").write_text("rules\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({"controller-1": str(repo)}), encoding="utf-8")

            result = self.run_bridge("session-start", "--repo", str(repo), "--registry", str(registry))

            self.assertEqual(result.returncode, 78)
            self.assertIn("verified Web Controller Session identity", result.stderr)

    def test_session_start_refuses_web_session_bound_to_another_controller(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"; repo.mkdir()
            other = root / "other"; other.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo),
                "controller-2": str(other),
                "__controller_sessions__": {
                    "controller-1": {"web": ["web-owner"]},
                    "controller-2": {"web": ["web-other"]},
                },
            }), encoding="utf-8")
            result = self.run_bridge(
                "session-start", "--repo", str(repo), "--registry", str(registry),
                "--web-session-id", "web-other",
            )
            self.assertEqual(result.returncode, 78)
            self.assertIn("verified Web Controller Session identity", result.stderr)

    def test_session_start_accepts_only_bound_web_controller_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
            (repo / "AGENTS.md").write_text("rules\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo),
                "__controller_sessions__": {"controller-1": {"web": ["web-session-1"]}},
            }), encoding="utf-8")

            result = self.run_bridge(
                "session-start", "--repo", str(repo), "--registry", str(registry),
                "--web-session-id", "web-session-1",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["controller_id"], "controller-1")
            self.assertEqual(payload["controller_session_id"], "controller-1")
            self.assertEqual(payload["web_session_id"], "web-session-1")
            self.assertEqual(payload["event_source"], "web")


class WebSessionRestoreAndResumeClassificationTests(unittest.TestCase):
    def test_restore_payload_allows_unborn_main_before_first_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            (repo / "AGENTS.md").write_text("rules\n", encoding="utf-8")
            (repo / "TASK_LEDGER.md").write_text("task ledger\n", encoding="utf-8")
            registry = root / "controllers.json"
            registry.write_text(json.dumps({"controller-1": str(repo.resolve())}), encoding="utf-8")

            payload = web_bridge.web_session_restore_payload(repo, registry)

        self.assertIsNone(payload["git"]["head"])
        self.assertEqual(payload["git"]["branch"], "main")
        self.assertEqual(payload["controller_id"], "controller-1")

    def test_restore_payload_binds_unique_controller_and_restores_authoritative_files_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
            for name, text in (("AGENTS.md", "agent rules"), ("TASK_LEDGER.md", "task ledger"), ("MEMORY.md", "stable memory"), ("WIKI_INDEX.md", "wiki index")):
                (repo / name).write_text(text + "\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({"controller-1": str(repo.resolve())}), encoding="utf-8")

            payload = web_bridge.web_session_restore_payload(repo, registry)

        self.assertEqual(payload["controller_id"], "controller-1")
        self.assertEqual(payload["restore_order"], ["AGENTS.md", "TASK_LEDGER.md", "MEMORY.md", "WIKI_INDEX.md", "git_runtime"])
        self.assertEqual([item["name"] for item in payload["documents"]], ["AGENTS.md", "TASK_LEDGER.md", "MEMORY.md", "WIKI_INDEX.md"])
        self.assertIn("agent rules", payload["documents"][0]["content"])
        self.assertIn("adaptive-delivery", payload["runtime_state_path"])
        self.assertNotIn("adaptive-agent-runtime", payload["runtime_state_path"])

    def test_restore_payload_includes_project_skill_immediately_after_agents_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
            for name, text in (
                ("AGENTS.md", "agent rules"),
                ("SKILL.md", "project workflow"),
                ("TASK_LEDGER.md", "task ledger"),
            ):
                (repo / name).write_text(text + chr(10), encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(repo), "commit", "-m", "init"],
                check=True,
                capture_output=True,
            )
            registry = root / "controllers.json"
            registry.write_text(
                json.dumps({"controller-1": str(repo.resolve())}),
                encoding="utf-8",
            )

            payload = web_bridge.web_session_restore_payload(repo, registry)

        self.assertEqual(
            payload["restore_order"][:3],
            ["AGENTS.md", "SKILL.md", "TASK_LEDGER.md"],
        )
        self.assertEqual(
            [item["name"] for item in payload["documents"]][:3],
            ["AGENTS.md", "SKILL.md", "TASK_LEDGER.md"],
        )

    def test_restore_payload_contains_bounded_dirty_git_and_runtime_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
            (repo / "TASK_LEDGER.md").write_text("task ledger\n", encoding="utf-8")
            (repo / "tracked.txt").write_text("clean\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True)
            (repo / "tracked.txt").write_text("dirty\n", encoding="utf-8")
            runtime_dir = repo / ".git" / "adaptive-delivery"
            runtime_dir.mkdir(parents=True)
            runtime = {"schema_version": 2, "leases": {"a1": {"assignment_id": "a1", "task_id": "T1", "terminal_state": None}}}
            (runtime_dir / "runtime-assignments.json").write_text(json.dumps(runtime), encoding="utf-8")
            registry = root / "controllers.json"
            registry.write_text(json.dumps({"controller-1": str(repo.resolve())}), encoding="utf-8")

            payload = web_bridge.web_session_restore_payload(repo, registry)

        self.assertIn("tracked.txt", payload["git"]["status"])
        self.assertFalse(payload["git"]["status_truncated"])
        self.assertTrue(payload["runtime"]["present"])
        self.assertIn('"assignment_id": "a1"', payload["runtime"]["content"])
        self.assertFalse(payload["runtime"]["truncated"])

    def test_restore_payload_uses_legacy_project_status_when_it_is_the_only_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
            (repo / "AGENTS.md").write_text("rules\n", encoding="utf-8")
            (repo / "PROJECT_STATUS.md").write_text("legacy ledger\n", encoding="utf-8")
            (repo / "SPEC.md").write_text("product contract\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({"controller-1": str(repo.resolve())}), encoding="utf-8")

            payload = web_bridge.web_session_restore_payload(repo, registry)

        self.assertIn("PROJECT_STATUS.md", payload["restore_order"])
        self.assertNotIn("TASK_LEDGER.md", payload["restore_order"])
        self.assertIn("PROJECT_STATUS.md", [item["name"] for item in payload["documents"]])
        self.assertIn("SPEC.md", [item["name"] for item in payload["authoritative_documents"]])

    def test_active_writer_resume_conflict_is_deferred_not_treated_as_peer_host_failure(self) -> None:
        classified = web_bridge.classify_native_resume_failure(
            1,
            "",
            "failed to initialize thread persistence: thread-store conflict: thread abc already has an active writer",
        )
        self.assertEqual(classified["state"], "RESUME_DEFERRED_ACTIVE_WRITER")
        self.assertEqual(classified["failure_class"], "active_writer_present")
        self.assertFalse(classified["fallback_eligible"])
        self.assertTrue(classified["pending_control_event"])

    def test_thread_schema_incompatibility_is_recoverable_target_failure(self) -> None:
        classified = web_bridge.classify_native_resume_failure(
            1,
            "",
            "Error: thread/resume failed: failed to deserialize stored thread item "
            "fco_123: unknown variant `functionCallOutput`, expected one of `userMessage`, "
            "`agentMessage` at line 1 column 28",
        )
        self.assertEqual(classified["state"], "RESUME_TARGET_INCOMPATIBLE")
        self.assertEqual(classified["failure_class"], "target_schema_incompatible")
        self.assertTrue(classified["replacement_eligible"])
        self.assertTrue(classified["pending_control_event"])

    def test_auto_native_stop_recovers_incompatible_target_without_user_turn(self) -> None:
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"; repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = root / "registry.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {"controller-1": {"desktop_codex": ["desktop-bad"]}},
                "__controller_targets__": {
                    "controller-1": {"desktop_codex": {
                        "status": "active", "session_id": "desktop-bad", "generation": 1,
                    }}
                },
                "__controller_execution_ownership__": {
                    "controller-1": {
                        "active_host": "desktop_codex",
                        "execution_target_session_id": "desktop-bad",
                        "generation": 1,
                    }
                },
            }), encoding="utf-8")
            state = root / "auto-stop.json"
            state.write_text(json.dumps({
                "receipt_id": "pending-schema-1", "session_id": "controller-1",
                "repo": str(repo.resolve()), "state": "RESUME_PENDING", "pending_control_event": True,
            }), encoding="utf-8")
            incompatible = {
                "operation": "native_resume", "result": "FAILED", "state": "RESUME_TARGET_INCOMPATIBLE",
                "pending_control_event": True, "returncode": 1, "stdout_tail": "",
                "stderr_tail": "failed to deserialize stored thread item fco_1: unknown variant `functionCallOutput`",
                "failure_class": "target_schema_incompatible", "replacement_eligible": True,
                "execution_target_session_id": "desktop-bad", "target_generation": 1,
            }
            recovered = {
                "operation": "native_target_recovery", "result": "CONFIRMED", "state": "RESUME_SUCCEEDED",
                "pending_control_event": True, "returncode": 0, "stdout_tail": "continued", "stderr_tail": "",
                "execution_target_session_id": "desktop-good", "target_generation": 2,
                "replacement_execution_target_session_id": "desktop-good",
                "ownership_generation": 2,
            }

            def recover_and_rotate(**_kwargs):
                saved_registry = json.loads(registry.read_text(encoding="utf-8"))
                saved_registry["__controller_sessions__"]["controller-1"]["desktop_codex"].append(
                    "desktop-good"
                )
                saved_registry["__controller_targets__"]["controller-1"]["desktop_codex"] = {
                    "status": "active", "session_id": "desktop-good", "generation": 2,
                }
                saved_registry["__controller_execution_ownership__"]["controller-1"] = {
                    "active_host": "desktop_codex",
                    "execution_target_session_id": "desktop-good",
                    "generation": 2,
                }
                registry.write_text(json.dumps(saved_registry), encoding="utf-8")
                return recovered

            with patch.object(web_bridge, "_load_lifecycle_state", side_effect=[{
                "pending_control_event": True,
                "requires_user": False,
                "controller_host": "desktop_codex",
            }, {
                "pending_control_event": False,
                "requires_user": False,
                "controller_host": "desktop_codex",
            }]), patch.object(web_bridge, "execute_native_resume", return_value=incompatible), patch.object(
                web_bridge, "recover_incompatible_native_target", side_effect=recover_and_rotate
            ) as recover, patch.object(web_bridge, "schedule_auto_native_stop") as schedule:
                code = web_bridge.run_auto_native_stop(
                    session_id="controller-1", repo=repo, receipt_id="pending-schema-1",
                    registry=registry, codex="/opt/homebrew/bin/codex", delay_seconds=0,
                    state_path=state, runtime_path="/opt/homebrew/bin:/usr/bin:/bin",
                )
            self.assertEqual(code, 0)
            recover.assert_called_once()
            self.assertEqual(recover.call_args.kwargs["expected_ownership_generation"], 1)
            schedule.assert_not_called()
            saved = json.loads(state.read_text())
            self.assertEqual(saved["state"], "CONTINUATION_CLOSED")
            self.assertEqual(saved["execution_target_session_id"], "desktop-good")
            self.assertEqual(saved["target_generation"], 2)
            self.assertFalse(saved["pending_control_event"])

    def test_desktop_target_replacement_uses_requested_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = root / "registry.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {
                    "controller-1": {"desktop_codex": ["desktop-bad"]}
                },
                "__controller_targets__": {
                    "controller-1": {"desktop_codex": {
                        "status": "active",
                        "session_id": "desktop-bad",
                        "generation": 1,
                    }}
                },
                "__controller_execution_ownership__": {
                    "controller-1": {
                        "active_host": "desktop_codex",
                        "execution_target_session_id": "desktop-bad",
                        "generation": 1,
                    }
                },
            }), encoding="utf-8")

            receipt = web_bridge.replace_desktop_execution_target(
                controller_id="controller-1",
                desktop_session_id="desktop-good",
                repo=repo,
                expected_generation=1,
                expected_ownership_generation=1,
                registry=registry,
            )

            saved = json.loads(registry.read_text(encoding="utf-8"))
            target = saved["__controller_targets__"]["controller-1"]["desktop_codex"]
            ownership = saved["__controller_execution_ownership__"]["controller-1"]
            self.assertEqual(target["session_id"], "desktop-good")
            self.assertEqual(target["generation"], 2)
            self.assertEqual(ownership["execution_target_session_id"], "desktop-good")
            self.assertEqual(ownership["generation"], 2)
            self.assertEqual(receipt["execution_target_session_id"], "desktop-good")
            self.assertEqual(receipt["generation"], 2)
            self.assertEqual(receipt["ownership_generation"], 2)

    def test_recover_incompatible_target_replaces_only_execution_target_then_resumes(self) -> None:
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"; repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = root / "registry.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {"controller-1": {"desktop_codex": ["desktop-bad"]}},
                "__controller_targets__": {
                    "controller-1": {"desktop_codex": {
                        "status": "active", "session_id": "desktop-bad", "generation": 1,
                    }}
                },
            }), encoding="utf-8")
            codex = root / "codex"
            codex.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = \"--version\" ]; then echo codex-test; exit 0; fi\n"
                "printf '%s\n' '{\"type\":\"thread.started\",\"thread_id\":\"desktop-good\"}'\n"
                "exit 0\n",
                encoding="utf-8",
            )
            codex.chmod(0o755)
            replacement_receipt = {
                "controller_id": "controller-1", "execution_target_session_id": "desktop-good",
                "status": "active", "generation": 2, "ownership_generation": 2,
            }
            resumed = {
                "operation": "native_resume", "result": "CONFIRMED", "state": "RESUME_SUCCEEDED",
                "pending_control_event": True, "returncode": 0, "stdout_tail": "step2", "stderr_tail": "",
                "controller_id": "controller-1", "execution_target_session_id": "desktop-good",
                "target_generation": 2,
            }
            with patch.object(web_bridge, "replace_desktop_execution_target", return_value=replacement_receipt) as replace, patch.object(
                web_bridge, "execute_native_resume", return_value=resumed
            ) as resume:
                result = web_bridge.recover_incompatible_native_target(
                    session_id="controller-1", repo=repo, registry=registry, codex=str(codex),
                    failed_target_session_id="desktop-bad", expected_generation=1,
                    expected_ownership_generation=1,
                    runtime_path="/usr/bin:/bin", terminal_receipts=["terminal.json"],
                    next_action="execute step 2",
                )
            self.assertEqual(result["result"], "CONFIRMED")
            self.assertEqual(result["execution_target_session_id"], "desktop-good")
            self.assertEqual(result["target_generation"], 2)
            replace.assert_called_once_with(
                controller_id="controller-1", desktop_session_id="desktop-good", repo=repo,
                expected_generation=1, expected_ownership_generation=1, registry=registry,
            )
            resume.assert_called_once()
            self.assertEqual(resume.call_args.kwargs["session_id"], "controller-1")
            self.assertEqual(resume.call_args.kwargs["next_action"], "execute step 2")
            self.assertEqual(result["ownership_generation"], 2)

    def test_active_writer_message_without_thread_store_prefix_is_still_deferred(self) -> None:
        classified = web_bridge.classify_native_resume_failure(
            1, "", "thread abc already has an active writer"
        )
        self.assertEqual(classified["state"], "RESUME_DEFERRED_ACTIVE_WRITER")
        self.assertEqual(classified["failure_class"], "active_writer_present")
        self.assertFalse(classified["fallback_eligible"])

    def test_session_start_cli_emits_restore_payload_for_unique_registered_controller(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
            (repo / "TASK_LEDGER.md").write_text("task ledger\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {"controller-1": {"web": ["web-session-1"]}},
            }), encoding="utf-8")
            _provision_verified_current_web_target(registry, web_session_id="web-session-1")
            result = subprocess.run(
                ["/usr/bin/python3", str(BRIDGE), "session-start", "--repo", str(repo), "--registry", str(registry),
                 "--web-session-id", "web-session-1"],
                text=True, capture_output=True, check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["controller_id"], "controller-1")
        self.assertEqual(payload["controller_id"], "controller-1")
        self.assertEqual(payload["controller_session_id"], "controller-1")
        self.assertEqual(payload["web_session_id"], "web-session-1")
        self.assertEqual(payload["restore_order"][-1], "git_runtime")


class ControllerHostTrackingTests(WebLifecycleBridgeTests):
    def test_web_bridge_marks_translated_events_as_web_host(self) -> None:
        receipt = {
            "receiptId": "host-web-1", "childTool": "shell_command", "state": "succeeded",
            "rootLabel": str(Path.home() / "Documents" / "SelfAlone"),
            "targetLabel": "git status --short",
            "detail": "命令：git status --short · 工作目录：~/Documents/SelfAlone\n\n命令输出：\n",
        }
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path.home() / "Documents" / "SelfAlone"
            registry = Path(tmp) / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {"controller-1": {"web": ["web-session-1"]}},
            }), encoding="utf-8")
            _provision_verified_current_web_target(registry, web_session_id="web-session-1")
            result = self.run_bridge(
                "translate-receipt", "--session-id", "controller-1", "--repo", str(repo),
                "--registry", str(registry), "--web-session-id", "web-session-1",
                stdin=json.dumps(receipt),
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        event = json.loads(result.stdout)
        self.assertEqual(event["controller_host"], "web")

class DesktopWebLifecycleParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        scripts_dir = str(ROOT / "scripts")
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        spec = importlib.util.spec_from_file_location("task8_lifecycle_hook", ROOT / "scripts" / "lifecycle_hook.py")
        assert spec is not None and spec.loader is not None
        cls.lifecycle = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.lifecycle)

    def stop_result(self, snapshot: dict, triggers: list[str], *, web: bool) -> dict:
        event = {
            "hook_event_name": "Stop",
            "session_id": "controller-1",
            "turn_id": "web-resume" if web else "desktop-native",
        }
        output, _ = self.lifecycle.evaluate_event(
            event, snapshot=snapshot,
            prior_state={"pending_control_event": bool(triggers), "triggers": triggers, "stop_continuations": 1, "snapshot": snapshot},
        )
        return output

    def test_desktop_and_web_resume_share_stop_yield_decision_for_runnable_and_candidate_cases(self) -> None:
        cases = [
            ({"head":"h","ledger_sha256":"l","worktree_status_sha256":"s","ready_ids":["READY-1"],"runnable_ids":["READY-1"],"candidate_revisions":[]}, ["READY:READY-1"]),
            ({"head":"h","ledger_sha256":"l","worktree_status_sha256":"s","ready_ids":[],"runnable_ids":["PENDING-RUNNABLE"],"candidate_revisions":[]}, ["RUNNABLE:PENDING-RUNNABLE"]),
            ({"head":"h","ledger_sha256":"l","worktree_status_sha256":"s","ready_ids":[],"runnable_ids":[],"candidate_revisions":["candidate-1"]}, ["CANDIDATE:candidate-1"]),
        ]
        for snapshot, triggers in cases:
            with self.subTest(snapshot=snapshot):
                self.assertEqual(self.stop_result(snapshot, triggers, web=False), self.stop_result(snapshot, triggers, web=True))

    def test_desktop_and_web_resume_both_allow_true_quiescent_snapshot(self) -> None:
        snapshot = {"head":"h","ledger_sha256":"l","worktree_status_sha256":"s","ready_ids":[],"runnable_ids":[],"candidate_revisions":[]}
        self.assertEqual(self.stop_result(snapshot, [], web=False), self.stop_result(snapshot, [], web=True))
        self.assertEqual(self.stop_result(snapshot, [], web=True), {})

    def test_desktop_and_web_hosts_use_the_same_dispatch_resolver_for_safe_and_unsafe_fallback(self) -> None:
        adapter = ROOT / "scripts" / "run_external_agent.mjs"
        def resolve(*extra: str) -> dict:
            result = subprocess.run([
                "node", str(adapter), "--resolve-route", "--engine", "grok-build", "--category", "backend",
                "--failure-class", "provider_unavailable", "--work-type", "implementation", "--complexity", "normal",
                "--controller-host", "web", *extra,
            ], text=True, capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)

        desktop_safe = resolve()
        web_safe = resolve()
        self.assertEqual(desktop_safe, web_safe)
        self.assertEqual(desktop_safe["decision"], "fallback")
        self.assertEqual(desktop_safe["model"], "gpt-5.6-terra")

        desktop_unsafe = resolve("--partial-write-possible")
        web_unsafe = resolve("--partial-write-possible")
        self.assertEqual(desktop_unsafe, web_unsafe)
        self.assertEqual(desktop_unsafe, {"decision": "blocked", "reason": "partial_write_possible"})

    def test_web_adapter_reuses_shared_dispatch_and_lifecycle_instead_of_defining_web_policy(self) -> None:
        source = BRIDGE.read_text(encoding="utf-8")
        routing = (ROOT / "references" / "agent-model-routing.md").read_text(encoding="utf-8")
        self.assertIn("control_event_guard", source)
        self.assertIn("same current-snapshot", routing)
        self.assertNotIn("WEB_READY", source)
        self.assertNotIn("web_runnable", source)

class WebHostNativeWakeIsolationTests(unittest.TestCase):
    def test_web_current_host_wake_never_calls_desktop_native_resume_without_web_adapter(self) -> None:
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); repo = root / "repo"; repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({"controller-1": str(repo.resolve())}), encoding="utf-8")
            receipt_path = root / "wake.json"
            state = {"pending_control_event": True, "controller_host": "web", "wake_generation": 1}
            missing_verifier = root / "missing-host-verifiers.json"
            with patch.object(
                web_bridge,
                "DEFAULT_PEER_ATTESTATION_VERIFIER_CONFIG",
                missing_verifier,
                create=True,
            ), patch.object(
                web_bridge,
                "execute_native_resume",
                side_effect=AssertionError("web wake must not invoke desktop codex"),
            ):
                receipt = web_bridge.wake_existing_controller(
                    lifecycle_state=state,
                    session_id="controller-1",
                    repo=repo,
                    registry=registry,
                    codex="/opt/homebrew/bin/codex",
                    receipt_path=receipt_path,
                    host_facts={"controller_host": "web", "resume_actionable": True},
                )
            self.assertEqual(receipt["selected_host"], "web")
            self.assertEqual(receipt["result"], "DEFERRED")
            self.assertEqual(
                receipt["error_code"],
                "WEB_HOST_ATTESTATION_VERIFIER_UNAVAILABLE",
            )

    def test_web_current_host_wake_refuses_unregistered_supplied_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); repo = root / "repo"; repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({"controller-1": str(repo.resolve())}), encoding="utf-8")
            receipt_path = root / "wake.json"
            calls = []
            def web_resume(**kwargs):
                calls.append(kwargs)
                return {"operation":"web_resume","result":"CONFIRMED","state":"RESUME_CONFIRMED","returncode":0}
            missing_verifier = root / "missing-host-verifiers.json"
            from unittest.mock import patch
            with patch.object(
                web_bridge,
                "DEFAULT_PEER_ATTESTATION_VERIFIER_CONFIG",
                missing_verifier,
                create=True,
            ):
                receipt = web_bridge.wake_existing_controller(
                    lifecycle_state={"pending_control_event": True, "controller_host": "web", "wake_generation": 1},
                    session_id="controller-1", repo=repo, registry=registry, codex="codex",
                    receipt_path=receipt_path,
                    host_facts={"controller_host": "web", "resume_actionable": True},
                    resume_adapters={"web": web_resume},
                )
            self.assertEqual(calls, [])
            self.assertEqual(receipt["result"], "DEFERRED")
            self.assertEqual(
                receipt["error_code"],
                "WEB_HOST_ATTESTATION_VERIFIER_UNAVAILABLE",
            )

    def test_registered_current_web_adapter_is_fenced_and_host_attested(self) -> None:
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {"controller-1": {"web": ["web-current"]}},
                "__controller_targets__": {"controller-1": {"web": {
                    "status": "active",
                    "session_id": "web-current",
                    "generation": 4,
                }}},
                "__controller_execution_ownership__": {"controller-1": {
                    "active_host": "web",
                    "execution_target_session_id": "web-current",
                    "generation": 8,
                }},
            }), encoding="utf-8")
            adapter_calls: list[dict] = []
            verifier_calls: list[dict] = []

            def web_resume(**kwargs: object) -> dict:
                adapter_calls.append(dict(kwargs))
                attestation = kwargs["host_origin_attestation"]
                return {
                    "operation": "web_resume",
                    "result": "CONFIRMED",
                    "state": "RESUME_CONFIRMED",
                    "returncode": 0,
                    "execution_target_session_id": kwargs["execution_target_session_id"],
                    "target_generation": kwargs["target_generation"],
                    "ownership_generation": 8,
                    "target_mode": "explicit_current",
                    "host_execution_receipt": {
                        "call_receipt": attestation["call_receipt"],
                        "submitted": True,
                    },
                }

            def verify_web_attestation(**kwargs: object) -> dict:
                verifier_calls.append(dict(kwargs))
                return {
                    "origin_host": "chatgpt_web",
                    "origin_conversation_id": kwargs["expected_target_session_id"],
                    "origin_attested": True,
                    "call_receipt": "host-call-1",
                }

            with patch.object(
                web_bridge,
                "_registered_peer_attestation_verifier",
                return_value=verify_web_attestation,
            ):
                receipt = web_bridge.wake_existing_controller(
                    lifecycle_state={
                        "pending_control_event": True,
                        "controller_host": "web",
                        "wake_generation": 1,
                    },
                    session_id="controller-1",
                    repo=repo,
                    registry=registry,
                    codex="codex",
                    receipt_path=root / "wake.json",
                    host_facts={"controller_host": "web", "resume_actionable": True},
                    resume_adapters={"web": web_resume},
                )

            self.assertEqual(receipt["result"], "CONFIRMED")
            self.assertEqual(receipt["ownership_generation"], 8)
            self.assertEqual(len(adapter_calls), 1)
            self.assertEqual(adapter_calls[0]["execution_target_session_id"], "web-current")
            self.assertEqual(adapter_calls[0]["target_generation"], 4)
            self.assertEqual(len(verifier_calls), 1)
            self.assertEqual(verifier_calls[0]["phase"], "pre_delivery")
            self.assertEqual(verifier_calls[0]["expected_target_session_id"], "web-current")
            self.assertEqual(verifier_calls[0]["expected_target_generation"], 4)
            self.assertEqual(
                adapter_calls[0]["host_origin_attestation"]["call_receipt"],
                "host-call-1",
            )

    def test_registered_external_web_host_submit_adapter_is_used_without_caller_injection(self) -> None:
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); repo = root / "repo"; repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {"controller-1": {"web": ["web-current"]}},
                "__controller_targets__": {"controller-1": {"web": {
                    "status": "active", "session_id": "web-current", "generation": 4,
                    "target_mode": "explicit_current",
                }}},
                "__controller_execution_ownership__": {"controller-1": {
                    "active_host": "web", "execution_target_session_id": "web-current", "generation": 8,
                }},
            }), encoding="utf-8")
            calls = []
            def verifier(**kwargs):
                return {
                    "origin_host": "chatgpt_web",
                    "origin_conversation_id": kwargs["expected_target_session_id"],
                    "origin_attested": True,
                    "call_receipt": "host-call-external",
                }
            def submit_reentry(**kwargs):
                calls.append(dict(kwargs))
                return {
                    "operation": "web_reentry", "result": "CONFIRMED", "state": "WEB_REENTRY_SUBMITTED",
                    "returncode": 0, "execution_target_session_id": kwargs["execution_target_session_id"],
                    "target_generation": kwargs["target_generation"], "ownership_generation": kwargs["ownership_generation"],
                    "target_mode": kwargs["target_mode"], "delivery_authorization": "host_attested",
                    "host_attested": True, "strong_web_identity_established": True,
                    "host_execution_receipt": {
                        "call_receipt": kwargs["host_origin_attestation"]["call_receipt"],
                        "reentry_receipt": {"receipt_id": "wr_external"},
                    },
                }
            verifier.submit_reentry = submit_reentry
            with patch.object(web_bridge, "_registered_peer_attestation_verifier", return_value=verifier), patch.object(
                web_bridge, "execute_web_reentry", side_effect=AssertionError("legacy browser adapter must not run")
            ):
                receipt = web_bridge.wake_existing_controller(
                    lifecycle_state={"pending_control_event": True, "controller_host": "web", "wake_generation": 2},
                    session_id="controller-1", repo=repo, registry=registry, codex="codex",
                    receipt_path=root / "wake.json",
                    host_facts={"controller_host": "web", "resume_actionable": True},
                )
            self.assertEqual(receipt["result"], "CONFIRMED")
            self.assertTrue(receipt["host_attested"])
            self.assertTrue(receipt["strong_web_identity_established"])
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["execution_target_session_id"], "web-current")
            self.assertEqual(calls[0]["target_generation"], 4)
            self.assertEqual(calls[0]["ownership_generation"], 8)

    def test_current_web_adapter_is_not_called_when_pre_delivery_attestation_rejects(self) -> None:
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {"controller-1": {"web": ["web-current"]}},
                "__controller_targets__": {"controller-1": {"web": {
                    "status": "active",
                    "session_id": "web-current",
                    "generation": 4,
                }}},
                "__controller_execution_ownership__": {"controller-1": {
                    "active_host": "web",
                    "execution_target_session_id": "web-current",
                    "generation": 8,
                }},
            }), encoding="utf-8")
            adapter_calls: list[dict] = []

            with patch.object(
                web_bridge,
                "_registered_peer_attestation_verifier",
                return_value=lambda **_kwargs: False,
            ):
                receipt = web_bridge.wake_existing_controller(
                    lifecycle_state={
                        "pending_control_event": True,
                        "controller_host": "web",
                        "wake_generation": 1,
                    },
                    session_id="controller-1",
                    repo=repo,
                    registry=registry,
                    codex="codex",
                    receipt_path=root / "wake.json",
                    host_facts={"controller_host": "web", "resume_actionable": True},
                    resume_adapters={
                        "web": lambda **kwargs: adapter_calls.append(dict(kwargs))
                        or {"result": "CONFIRMED"}
                    },
                )

            self.assertEqual(receipt["result"], "FAILED")
            self.assertEqual(receipt["error_code"], "WEB_HOST_ATTESTATION_INVALID")
            self.assertEqual(adapter_calls, [])

    def test_current_web_adapter_receipt_must_correlate_origin_call_receipt(self) -> None:
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {"controller-1": {"web": ["web-current"]}},
                "__controller_targets__": {"controller-1": {"web": {
                    "status": "active", "session_id": "web-current", "generation": 4,
                }}},
                "__controller_execution_ownership__": {"controller-1": {
                    "active_host": "web",
                    "execution_target_session_id": "web-current",
                    "generation": 8,
                }},
            }), encoding="utf-8")

            def adapter(**kwargs: object) -> dict:
                return {
                    "operation": "web_resume",
                    "result": "CONFIRMED",
                    "state": "RESUME_CONFIRMED",
                    "returncode": 0,
                    "execution_target_session_id": kwargs["execution_target_session_id"],
                    "target_generation": kwargs["target_generation"],
                    "ownership_generation": kwargs["ownership_generation"],
                    "target_mode": kwargs["target_mode"],
                    "host_execution_receipt": {
                        "call_receipt": "different-host-call",
                        "submitted": True,
                    },
                }

            verifier = lambda **_kwargs: {
                "origin_host": "chatgpt_web",
                "origin_conversation_id": "web-current",
                "origin_attested": True,
                "call_receipt": "host-call-1",
            }
            with patch.object(
                web_bridge, "_registered_peer_attestation_verifier", return_value=verifier
            ):
                receipt = web_bridge.wake_existing_controller(
                    lifecycle_state={
                        "pending_control_event": True,
                        "controller_host": "web",
                        "wake_generation": 1,
                    },
                    session_id="controller-1",
                    repo=repo,
                    registry=registry,
                    codex="codex",
                    receipt_path=root / "wake.json",
                    host_facts={"controller_host": "web", "resume_actionable": True},
                    resume_adapters={"web": adapter},
                )

            self.assertEqual(receipt["result"], "FAILED")
            self.assertEqual(receipt["error_code"], "WEB_HOST_ATTESTATION_INVALID")
            self.assertTrue(receipt["pending_control_event"])

    def test_current_web_adapter_is_not_called_for_malformed_origin_attestation(self) -> None:
        from unittest.mock import patch

        malformed = (
            None,
            {},
            {"origin_host": "chatgpt_web", "origin_conversation_id": "web-current", "origin_attested": False, "call_receipt": "r1"},
            {"origin_host": "chatgpt_web", "origin_conversation_id": "web-other", "origin_attested": True, "call_receipt": "r1"},
            {"origin_host": "browser_tab", "origin_conversation_id": "web-current", "origin_attested": True, "call_receipt": "r1"},
            {"origin_host": "chatgpt_web", "origin_conversation_id": "web-current", "origin_attested": True, "call_receipt": ""},
        )
        for attestation in malformed:
            with self.subTest(attestation=attestation), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                repo = root / "repo"
                repo.mkdir()
                subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
                registry = root / "controllers.json"
                registry.write_text(json.dumps({
                    "controller-1": str(repo.resolve()),
                    "__controller_sessions__": {"controller-1": {"web": ["web-current"]}},
                    "__controller_targets__": {"controller-1": {"web": {
                        "status": "active", "session_id": "web-current", "generation": 4,
                    }}},
                    "__controller_execution_ownership__": {"controller-1": {
                        "active_host": "web", "execution_target_session_id": "web-current", "generation": 8,
                    }},
                }), encoding="utf-8")
                adapter_calls: list[dict] = []
                with patch.object(
                    web_bridge,
                    "_registered_peer_attestation_verifier",
                    return_value=lambda **_kwargs: attestation,
                ):
                    receipt = web_bridge.wake_existing_controller(
                        lifecycle_state={"pending_control_event": True, "controller_host": "web", "wake_generation": 1},
                        session_id="controller-1", repo=repo, registry=registry, codex="codex",
                        receipt_path=root / "wake.json",
                        host_facts={"controller_host": "web", "resume_actionable": True},
                        resume_adapters={"web": lambda **kwargs: adapter_calls.append(dict(kwargs)) or {"result": "CONFIRMED"}},
                    )
                self.assertEqual(receipt["result"], "FAILED")
                self.assertEqual(adapter_calls, [])

    def test_registered_current_web_adapter_without_ownership_is_never_called(self) -> None:
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {"controller-1": {"web": ["web-current"]}},
                "__controller_targets__": {"controller-1": {"web": {
                    "status": "active",
                    "session_id": "web-current",
                    "generation": 4,
                }}},
            }), encoding="utf-8")
            adapter_calls: list[dict] = []

            def web_resume(**kwargs: object) -> dict:
                adapter_calls.append(dict(kwargs))
                return {"result": "CONFIRMED"}

            with patch.object(
                web_bridge,
                "_registered_peer_attestation_verifier",
                return_value=lambda **_kwargs: True,
            ):
                receipt = web_bridge.wake_existing_controller(
                    lifecycle_state={
                        "pending_control_event": True,
                        "controller_host": "web",
                        "wake_generation": 1,
                    },
                    session_id="controller-1",
                    repo=repo,
                    registry=registry,
                    codex="codex",
                    receipt_path=root / "wake.json",
                    host_facts={"controller_host": "web", "resume_actionable": True},
                    resume_adapters={"web": web_resume},
                )

            self.assertEqual(receipt["result"], "FAILED")
            self.assertEqual(receipt["error_code"], "WEB_HOST_ATTESTATION_INVALID")
            self.assertEqual(adapter_calls, [])

    def test_registered_current_web_adapter_rejects_legacy_browser_identity_before_call(self) -> None:
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {"controller-1": {"web": ["web-current"]}},
                "__controller_targets__": {"controller-1": {"web": {
                    "status": "active",
                    "session_id": "web-current",
                    "generation": 4,
                    "provenance": "host_attested_same_controller_recovery",
                }}},
                "__controller_execution_ownership__": {"controller-1": {
                    "active_host": "web",
                    "execution_target_session_id": "web-current",
                    "generation": 8,
                }},
            }), encoding="utf-8")
            adapter_calls: list[dict] = []

            with patch.object(
                web_bridge,
                "_registered_peer_attestation_verifier",
                return_value=lambda **_kwargs: True,
            ):
                receipt = web_bridge.wake_existing_controller(
                    lifecycle_state={
                        "pending_control_event": True,
                        "controller_host": "web",
                        "wake_generation": 1,
                    },
                    session_id="controller-1",
                    repo=repo,
                    registry=registry,
                    codex="codex",
                    receipt_path=root / "wake.json",
                    host_facts={"controller_host": "web", "resume_actionable": True},
                    resume_adapters={
                        "web": lambda **kwargs: adapter_calls.append(dict(kwargs))
                        or {"result": "CONFIRMED"}
                    },
                )

            self.assertEqual(receipt["result"], "FAILED")
            self.assertEqual(receipt["error_code"], "WEB_HOST_ATTESTATION_INVALID")
            self.assertEqual(adapter_calls, [])

    def test_auto_stop_for_web_host_never_calls_desktop_native_resume(self) -> None:
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); repo = root / "repo"; repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({"controller-1": str(repo.resolve())}), encoding="utf-8")
            state_path = root / "auto.json"
            state_path.write_text(json.dumps({"receipt_id":"r1","session_id":"controller-1","repo":str(repo.resolve()),"state":"RESUME_PENDING"}), encoding="utf-8")
            lifecycle = {"pending_control_event": True, "controller_host":"web", "requires_user":False, "wake_generation":1}
            with patch.object(web_bridge, "_load_lifecycle_state", return_value=lifecycle), patch.object(
                web_bridge, "execute_native_resume", side_effect=AssertionError("auto-stop web host must not invoke desktop codex")
            ):
                rc = web_bridge.run_auto_native_stop(
                    session_id="controller-1", repo=repo, receipt_id="r1", registry=registry,
                    codex="codex", delay_seconds=0, state_path=state_path,
                )
            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(rc, 78)
            self.assertEqual(saved["state"], "WEB_REENTRY_IDENTITY_UNAVAILABLE")
            self.assertEqual(saved["error_code"], "WEB_REENTRY_IDENTITY_UNAVAILABLE")
            self.assertEqual(
                saved["last_lifecycle_fingerprint"],
                web_bridge._wake_event_fingerprint(lifecycle),
            )
            self.assertEqual(saved["blocked_registry_sha256"], web_bridge._file_sha256(registry))
            self.assertFalse(web_bridge.continuation_supervisor_needs_bootstrap(
                lifecycle,
                saved,
                current_registry_sha256=web_bridge._file_sha256(registry),
            ))

    def test_stale_web_host_with_only_desktop_current_target_resumes_same_controller_desktop(self) -> None:
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); repo = root / "repo"; repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {
                    "controller-1": {
                        "web": ["web-old", "web-older"],
                        "desktop_codex": ["desktop-current"],
                    }
                },
                "__controller_targets__": {
                    "controller-1": {
                        "desktop_codex": {
                            "status": "active",
                            "session_id": "desktop-current",
                            "generation": 2,
                        }
                    }
                },
            }), encoding="utf-8")
            state_path = root / "auto.json"
            state_path.write_text(json.dumps({
                "receipt_id": "r1",
                "session_id": "controller-1",
                "repo": str(repo.resolve()),
                "state": "RESUME_PENDING",
            }), encoding="utf-8")
            lifecycle = {
                "pending_control_event": True,
                "controller_host": "web",
                "requires_user": False,
                "wake_generation": 9,
            }
            confirmed = {
                "operation": "native_resume",
                "result": "CONFIRMED",
                "state": "RESUME_CONFIRMED",
                "returncode": 0,
                "pending_control_event": True,
                "execution_target_session_id": "desktop-current",
                "target_generation": 2,
                "target_mode": "explicit_current",
            }
            with patch.object(web_bridge, "_load_lifecycle_state", return_value=lifecycle), patch.object(
                web_bridge, "execute_native_resume", return_value=confirmed
            ) as native_resume, patch.object(
                web_bridge, "execute_web_reentry", side_effect=AssertionError("stale Web host must not bypass canonical desktop current target")
            ), patch.object(web_bridge, "_rearm_auto_native_stop") as rearm:
                rc = web_bridge.run_auto_native_stop(
                    session_id="controller-1", repo=repo, receipt_id="r1", registry=registry,
                    codex="codex", delay_seconds=0, state_path=state_path,
                )
            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(rc, 0)
            self.assertTrue(native_resume.called)
            self.assertTrue(rearm.called)
            self.assertEqual(saved["state"], "RESUME_REARMED")
            self.assertEqual(saved["execution_target_session_id"], "desktop-current")
            self.assertEqual(saved["target_generation"], 2)

class ControllerHostResolutionIsolationTests(unittest.TestCase):
    def test_missing_lifecycle_host_resolves_unique_desktop_binding(self) -> None:
        registry = {
            "__controller_sessions__": {"controller-1": {"desktop_codex": ["desktop-1"], "web": []}}
        }
        self.assertEqual(web_bridge.resolve_controller_host({}, {}, registry, "controller-1"), "desktop_codex")

    def test_missing_lifecycle_host_with_both_bindings_does_not_assume_desktop(self) -> None:
        registry = {
            "__controller_sessions__": {"controller-1": {"desktop_codex": ["desktop-1"], "web": ["web-1"]}}
        }
        self.assertEqual(web_bridge.resolve_controller_host({}, {}, registry, "controller-1"), "web")

    def test_stale_web_lifecycle_host_yields_to_the_only_explicit_current_target(self) -> None:
        registry = {
            "__controller_sessions__": {
                "controller-1": {
                    "desktop_codex": ["desktop-current"],
                    "web": ["web-old", "web-older"],
                }
            },
            "__controller_targets__": {
                "controller-1": {
                    "desktop_codex": {
                        "status": "active",
                        "session_id": "desktop-current",
                        "generation": 2,
                    }
                }
            },
        }

        self.assertEqual(
            web_bridge.resolve_controller_host(
                {"controller_host": "web"}, {}, registry, "controller-1"
            ),
            "desktop_codex",
        )

    def test_canonical_web_ownership_overrides_stale_desktop_lifecycle_hint(self) -> None:
        registry = {
            "__controller_sessions__": {
                "controller-1": {
                    "desktop_codex": ["desktop-current"],
                    "web": ["web-current"],
                }
            },
            "__controller_targets__": {
                "controller-1": {
                    "desktop_codex": {"status": "active", "session_id": "desktop-current", "generation": 2},
                    "web": {"status": "active", "session_id": "web-current", "generation": 4},
                }
            },
            "__controller_execution_ownership__": {
                "controller-1": {
                    "active_host": "web",
                    "execution_target_session_id": "web-current",
                    "generation": 8,
                }
            },
        }
        self.assertEqual(
            web_bridge.resolve_controller_host(
                {"controller_host": "desktop_codex"}, {}, registry, "controller-1"
            ),
            "web",
        )

    def test_canonical_desktop_ownership_overrides_stale_web_lifecycle_hint(self) -> None:
        registry = {
            "__controller_sessions__": {
                "controller-1": {
                    "desktop_codex": ["desktop-current"],
                    "web": ["web-current"],
                }
            },
            "__controller_targets__": {
                "controller-1": {
                    "desktop_codex": {"status": "active", "session_id": "desktop-current", "generation": 2},
                    "web": {"status": "active", "session_id": "web-current", "generation": 4},
                }
            },
            "__controller_execution_ownership__": {
                "controller-1": {
                    "active_host": "desktop_codex",
                    "execution_target_session_id": "desktop-current",
                    "generation": 9,
                }
            },
        }
        self.assertEqual(
            web_bridge.resolve_controller_host(
                {"controller_host": "web"}, {}, registry, "controller-1"
            ),
            "desktop_codex",
        )

    def test_explicit_current_web_target_preserves_web_lifecycle_host(self) -> None:
        registry = {
            "__controller_sessions__": {
                "controller-1": {
                    "desktop_codex": ["desktop-current"],
                    "web": ["web-current", "web-old"],
                }
            },
            "__controller_targets__": {
                "controller-1": {
                    "desktop_codex": {
                        "status": "active",
                        "session_id": "desktop-current",
                        "generation": 2,
                    },
                    "web": {
                        "status": "active",
                        "session_id": "web-current",
                        "generation": 4,
                    },
                }
            },
        }

        self.assertEqual(
            web_bridge.resolve_controller_host(
                {"controller_host": "web"}, {}, registry, "controller-1"
            ),
            "web",
        )


class WebLocalReentryIntegrationTests(unittest.TestCase):
    def make_repo(self, root: Path) -> tuple[Path, Path, Path]:
        repo = root / "repo"; repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
        registry = root / "controllers.json"
        registry.write_text(json.dumps({
            "controller-1": str(repo.resolve()),
            "__controller_sessions__": {"controller-1": {"web": ["web-current"]}},
        }), encoding="utf-8")
        state_path = root / "auto.json"
        return repo, registry, state_path

    def test_builtin_web_reentry_without_registered_origin_verifier_never_calls_browser_adapter(self) -> None:
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            repo, registry, _ = self.make_repo(Path(tmp))
            payload = json.loads(registry.read_text(encoding="utf-8"))
            payload["__controller_targets__"] = {"controller-1": {"web": {
                "status": "active", "session_id": "web-current", "generation": 4,
            }}}
            payload["__controller_execution_ownership__"] = {"controller-1": {
                "active_host": "web",
                "execution_target_session_id": "web-current",
                "generation": 8,
            }}
            registry.write_text(json.dumps(payload), encoding="utf-8")
            receipt_path = Path(tmp) / "wake.json"
            with patch.object(
                web_bridge,
                "execute_web_reentry",
                side_effect=AssertionError("unattested built-in Web adapter must not run"),
            ) as reentry, patch.object(
                web_bridge, "execute_native_resume", side_effect=AssertionError("web wake must not invoke desktop Codex")
            ), patch.object(
                web_bridge,
                "DEFAULT_PEER_ATTESTATION_VERIFIER_CONFIG",
                Path(tmp) / "missing-host-verifiers.json",
                create=True,
            ):
                receipt = web_bridge.wake_existing_controller(
                    lifecycle_state={"pending_control_event": True, "controller_host": "web", "wake_generation": 4},
                    session_id="controller-1", repo=repo, registry=registry, codex="codex",
                    receipt_path=receipt_path,
                    host_facts={"controller_host": "web", "resume_actionable": True},
                )
            self.assertEqual(receipt["result"], "DEFERRED")
            self.assertEqual(receipt["selected_host"], "web")
            self.assertEqual(
                receipt["error_code"],
                "WEB_HOST_ATTESTATION_VERIFIER_UNAVAILABLE",
            )
            self.assertTrue(receipt["pending_control_event"])
            reentry.assert_not_called()

    def test_local_web_wake_rejects_receipt_for_noncanonical_target(self) -> None:
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, registry, _ = self.make_repo(root)
            payload = json.loads(registry.read_text(encoding="utf-8"))
            payload["__controller_targets__"] = {
                "controller-1": {
                    "web": {
                        "status": "active",
                        "session_id": "web-current",
                        "generation": 4,
                    }
                }
            }
            payload["__controller_execution_ownership__"] = {
                "controller-1": {
                    "active_host": "web",
                    "execution_target_session_id": "web-current",
                    "generation": 8,
                }
            }
            registry.write_text(json.dumps(payload), encoding="utf-8")
            wrong = {
                "operation": "web_reentry",
                "result": "CONFIRMED",
                "state": "WEB_REENTRY_SUBMITTED",
                "returncode": 0,
                "execution_target_session_id": "web-wrong",
                "target_generation": 4,
                "ownership_generation": 8,
                "target_mode": "explicit_current",
            }

            with patch.object(
                web_bridge, "_registered_peer_attestation_verifier",
                return_value=lambda **_kwargs: True,
            ), patch.object(web_bridge, "execute_web_reentry", return_value=wrong):
                receipt = web_bridge.wake_existing_controller(
                    lifecycle_state={
                        "pending_control_event": True,
                        "controller_host": "web",
                        "wake_generation": 4,
                    },
                    session_id="controller-1",
                    repo=repo,
                    registry=registry,
                    codex="codex",
                    receipt_path=root / "wake.json",
                    host_facts={"controller_host": "web", "resume_actionable": True},
                )

            self.assertEqual(receipt["result"], "FAILED")
            self.assertEqual(
                receipt["error_code"], "CONTROLLER_TARGET_RECEIPT_MISMATCH"
            )

    def test_web_supervisor_rejects_confirmed_receipt_for_noncanonical_target(self) -> None:
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, registry, state_path = self.make_repo(root)
            payload = json.loads(registry.read_text(encoding="utf-8"))
            payload["__controller_targets__"] = {"controller-1": {"web": {
                "status": "active", "session_id": "web-current", "generation": 4,
            }}}
            payload["__controller_execution_ownership__"] = {"controller-1": {
                "active_host": "web",
                "execution_target_session_id": "web-current",
                "generation": 8,
            }}
            registry.write_text(json.dumps(payload), encoding="utf-8")
            state_path.write_text(json.dumps({
                "receipt_id": "bootstrap:9",
                "session_id": "controller-1",
                "repo": str(repo.resolve()),
                "state": "RESUME_PENDING",
                "pending_control_event": True,
            }), encoding="utf-8")
            lifecycle = {
                "pending_control_event": True,
                "requires_user": False,
                "controller_host": "web",
                "wake_generation": 9,
            }
            wrong = {
                "operation": "web_reentry",
                "result": "CONFIRMED",
                "state": "WEB_REENTRY_SUBMITTED",
                "returncode": 0,
                "execution_target_session_id": "web-wrong",
                "target_generation": 4,
                "ownership_generation": 8,
                "target_mode": "explicit_current",
            }

            with patch.object(
                web_bridge, "_load_lifecycle_state", return_value=lifecycle
            ), patch.object(
                web_bridge, "execute_web_reentry", return_value=wrong
            ), patch.object(web_bridge, "_rearm_auto_native_stop") as rearm:
                code = web_bridge.run_auto_native_stop(
                    session_id="controller-1",
                    repo=repo,
                    receipt_id="bootstrap:9",
                    registry=registry,
                    codex="codex",
                    delay_seconds=0,
                    state_path=state_path,
                )

            self.assertEqual(code, 78)
            rearm.assert_not_called()
            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["state"], "WEB_REENTRY_TARGET_RECEIPT_MISMATCH")
            self.assertTrue(saved["pending_control_event"])

    def test_direct_wake_rejects_confirmed_web_result_after_desktop_handoff(self) -> None:
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); repo, registry, _state_path = self.make_repo(root)
            payload = json.loads(registry.read_text())
            payload["__controller_sessions__"]["controller-1"]["desktop_codex"] = ["desktop-current"]
            payload["__controller_targets__"] = {"controller-1": {
                "web": {"status":"active","session_id":"web-current","generation":4},
                "desktop_codex": {"status":"active","session_id":"desktop-current","generation":2},
            }}
            payload["__controller_execution_ownership__"] = {"controller-1": {
                "active_host":"web","execution_target_session_id":"web-current","generation":8,
            }}
            registry.write_text(json.dumps(payload), encoding="utf-8")
            receipt_path = root / "wake.json"

            def web_then_handoff(**_kwargs):
                changed = json.loads(registry.read_text())
                changed["__controller_execution_ownership__"]["controller-1"] = {
                    "active_host":"desktop_codex",
                    "execution_target_session_id":"desktop-current",
                    "generation":9,
                }
                registry.write_text(json.dumps(changed), encoding="utf-8")
                return {
                    "operation":"web_reentry","result":"CONFIRMED","state":"WEB_REENTRY_SUBMITTED",
                    "returncode":0,"execution_target_session_id":"web-current",
                    "target_generation":4,"target_mode":"explicit_current",
                }

            with patch.object(
                web_bridge, "_registered_peer_attestation_verifier",
                return_value=lambda **_kwargs: True,
            ), patch.object(web_bridge, "execute_web_reentry", side_effect=web_then_handoff), patch.object(
                web_bridge, "execute_native_resume",
                side_effect=AssertionError("Web-owned direct wake must not invoke desktop Codex")
            ):
                receipt = web_bridge.wake_existing_controller(
                    lifecycle_state={"pending_control_event":True,"controller_host":"web","wake_generation":8},
                    session_id="controller-1", repo=repo, registry=registry, codex="codex",
                    receipt_path=receipt_path,
                    host_facts={"controller_host":"web","resume_actionable":True},
                )

            self.assertEqual(receipt["result"], "DEFERRED")
            self.assertEqual(receipt["error_code"], "CONTROLLER_HOST_OWNERSHIP_SUPERSEDED")
            self.assertEqual(receipt["selected_host"], "web")

    def test_detached_supervisor_submits_web_reentry_then_rearms_observer(self) -> None:
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            repo, registry, state_path = self.make_repo(Path(tmp))
            payload = json.loads(registry.read_text(encoding="utf-8"))
            payload["__controller_targets__"] = {"controller-1": {"web": {
                "status": "active", "session_id": "web-current", "generation": 4,
            }}}
            payload["__controller_execution_ownership__"] = {"controller-1": {
                "active_host": "web",
                "execution_target_session_id": "web-current",
                "generation": 8,
            }}
            registry.write_text(json.dumps(payload), encoding="utf-8")
            state_path.write_text(json.dumps({
                "receipt_id": "web-r1", "session_id": "controller-1", "repo": str(repo.resolve()),
                "state": "RESUME_PENDING", "pending_control_event": True,
            }), encoding="utf-8")
            lifecycle = {
                "pending_control_event": True, "requires_user": False, "controller_host": "web",
                "wake_generation": 8, "triggers": ["READY:F1"],
                "snapshot": {"head":"h1","ledger_sha256":"l1","worktree_status_sha256":"w1","ready_ids":["F1"],"runnable_ids":["F1"],"candidate_revisions":[]},
            }
            confirmed = {
                "operation": "web_reentry", "result": "CONFIRMED", "state": "WEB_REENTRY_SUBMITTED",
                "returncode": 0, "execution_target_session_id": "web-current",
                "target_generation": 4, "ownership_generation": 8,
                "target_mode": "explicit_current",
            }
            with patch.object(web_bridge, "_load_lifecycle_state", return_value=lifecycle), patch.object(
                web_bridge, "execute_web_reentry", return_value=confirmed
            ) as reentry, patch.object(web_bridge, "schedule_auto_native_stop") as schedule, patch.object(
                web_bridge, "execute_native_resume", side_effect=AssertionError("web supervisor must not invoke desktop Codex")
            ):
                code = web_bridge.run_auto_native_stop(
                    session_id="controller-1", repo=repo, receipt_id="web-r1", registry=registry,
                    codex="codex", delay_seconds=0, state_path=state_path,
                )
            self.assertEqual(code, 0)
            reentry.assert_called_once()
            schedule.assert_not_called()
            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["state"], "WAITING_FOR_CONTROLLER_PROGRESS")
            self.assertEqual(saved["last_lifecycle_fingerprint"], web_bridge._wake_event_fingerprint(lifecycle))
            self.assertEqual(saved["continuation_count"], 1)
            self.assertEqual(saved["delivery_terminal_receipt_id"], "web-r1")
            self.assertEqual(saved["delivery_terminal_outcome"], "submit_confirmed")

    def test_detached_supervisor_uses_registered_host_submit_adapter_for_strong_web_target(self) -> None:
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            repo, registry, state_path = self.make_repo(Path(tmp))
            payload = json.loads(registry.read_text(encoding="utf-8"))
            payload["__controller_targets__"] = {"controller-1": {"web": {
                "status": "active",
                "session_id": "web-current",
                "generation": 4,
                "provenance": "host_attested_same_controller_recovery",
                "binding_mode": "resume_only",
                "identity_proof": "host_attested_origin",
            }}}
            payload["__controller_execution_ownership__"] = {"controller-1": {
                "active_host": "web",
                "execution_target_session_id": "web-current",
                "generation": 8,
                "provenance": "web_entry",
            }}
            registry.write_text(json.dumps(payload), encoding="utf-8")
            state_path.write_text(json.dumps({
                "receipt_id": "web-host-r1",
                "session_id": "controller-1",
                "repo": str(repo.resolve()),
                "state": "RESUME_PENDING",
                "pending_control_event": True,
            }), encoding="utf-8")
            lifecycle = {
                "pending_control_event": True,
                "requires_user": False,
                "controller_host": "web",
                "wake_generation": 8,
                "triggers": ["RUNTIME_CONTINUATION_DEBT"],
            }
            calls = []
            def verifier(**kwargs):
                calls.append(("verify", kwargs))
                return {
                    "origin_host": "chatgpt_web",
                    "origin_conversation_id": "web-current",
                    "origin_attested": True,
                    "call_receipt": "hr-1",
                }
            def submit_reentry(**kwargs):
                calls.append(("submit", kwargs))
                return {
                    "operation": "web_reentry",
                    "result": "CONFIRMED",
                    "state": "WEB_REENTRY_SUBMITTED",
                    "returncode": 0,
                    "execution_target_session_id": "web-current",
                    "target_generation": 4,
                    "ownership_generation": 8,
                    "target_mode": "explicit_current",
                    "delivery_authorization": "host_attested",
                    "host_attested": True,
                    "strong_web_identity_established": True,
                    "host_execution_receipt": {
                        "call_receipt": "hr-1",
                        "reentry_receipt": {"receipt_id": "wr-1"},
                    },
                }
            verifier.submit_reentry = submit_reentry
            with patch.object(web_bridge, "_load_lifecycle_state", return_value=lifecycle), patch.object(
                web_bridge, "_registered_peer_attestation_verifier", return_value=verifier
            ), patch.object(
                web_bridge, "execute_web_reentry",
                side_effect=AssertionError("strong Host auto-stop must not use legacy browser reentry")
            ), patch.object(web_bridge, "schedule_auto_native_stop") as schedule:
                code = web_bridge.run_auto_native_stop(
                    session_id="controller-1",
                    repo=repo,
                    receipt_id="web-host-r1",
                    registry=registry,
                    codex="codex",
                    delay_seconds=0,
                    state_path=state_path,
                )
            self.assertEqual(code, 0)
            self.assertEqual([kind for kind, _ in calls], ["verify", "submit"])
            schedule.assert_not_called()
            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["state"], "WAITING_FOR_CONTROLLER_PROGRESS")
            self.assertTrue(saved["host_attested"])
            self.assertTrue(saved["strong_web_identity_established"])
            self.assertEqual(saved["delivery_terminal_receipt_id"], "web-host-r1")
            self.assertEqual(saved["delivery_terminal_outcome"], "submit_confirmed")

    def test_detached_supervisor_retries_transient_registered_host_attestation_failure(self) -> None:
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            repo, registry, state_path = self.make_repo(Path(tmp))
            payload = json.loads(registry.read_text(encoding="utf-8"))
            payload["__controller_targets__"] = {"controller-1": {"web": {
                "status": "active",
                "session_id": "web-current",
                "generation": 4,
                "provenance": "host_attested_same_controller_recovery",
                "binding_mode": "resume_only",
                "identity_proof": "host_attested_origin",
            }}}
            payload["__controller_execution_ownership__"] = {"controller-1": {
                "active_host": "web",
                "execution_target_session_id": "web-current",
                "generation": 8,
                "provenance": "web_entry",
            }}
            registry.write_text(json.dumps(payload), encoding="utf-8")
            state_path.write_text(json.dumps({
                "receipt_id": "web-host-transient",
                "session_id": "controller-1",
                "repo": str(repo.resolve()),
                "state": "RESUME_PENDING",
                "pending_control_event": True,
            }), encoding="utf-8")
            lifecycle = {
                "pending_control_event": True,
                "requires_user": False,
                "controller_host": "web",
                "wake_generation": 8,
            }
            def verifier(**_kwargs):
                raise web_bridge.PeerHostTransientUnavailable(
                    "registered Host verifier temporarily unavailable: exact ChatGPT conversation target is unavailable"
                )
            verifier.submit_reentry = lambda **_kwargs: (_ for _ in ()).throw(
                AssertionError("submit must not run without attestation")
            )
            with patch.object(web_bridge, "_load_lifecycle_state", return_value=lifecycle), patch.object(
                web_bridge, "_registered_peer_attestation_verifier", return_value=verifier
            ), patch.object(
                web_bridge, "execute_web_reentry",
                side_effect=AssertionError("strong Host auto-stop must not use legacy browser reentry")
            ), patch.object(web_bridge, "schedule_auto_native_stop") as schedule:
                code = web_bridge.run_auto_native_stop(
                    session_id="controller-1",
                    repo=repo,
                    receipt_id="web-host-transient",
                    registry=registry,
                    codex="codex",
                    delay_seconds=0,
                    state_path=state_path,
                )
            self.assertEqual(code, 0)
            schedule.assert_called_once()
            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["state"], "WEB_REENTRY_PENDING")
            self.assertEqual(saved["failure_class"], "web_reentry_unavailable")
            self.assertEqual(saved["error_code"], "WEB_HOST_TEMPORARILY_UNAVAILABLE")
            self.assertEqual(saved["retry_count"], 1)

    def test_detached_supervisor_does_not_retry_registered_host_identity_mismatch(self) -> None:
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            repo, registry, state_path = self.make_repo(Path(tmp))
            payload = json.loads(registry.read_text(encoding="utf-8"))
            payload["__controller_targets__"] = {"controller-1": {"web": {
                "status": "active",
                "session_id": "web-current",
                "generation": 4,
                "provenance": "host_attested_same_controller_recovery",
                "binding_mode": "resume_only",
                "identity_proof": "host_attested_origin",
            }}}
            payload["__controller_execution_ownership__"] = {"controller-1": {
                "active_host": "web",
                "execution_target_session_id": "web-current",
                "generation": 8,
                "provenance": "web_entry",
            }}
            registry.write_text(json.dumps(payload), encoding="utf-8")
            state_path.write_text(json.dumps({
                "receipt_id": "web-host-invalid",
                "session_id": "controller-1",
                "repo": str(repo.resolve()),
                "state": "RESUME_PENDING",
                "pending_control_event": True,
            }), encoding="utf-8")
            lifecycle = {
                "pending_control_event": True,
                "requires_user": False,
                "controller_host": "web",
                "wake_generation": 8,
            }
            def verifier(**_kwargs):
                raise PermissionError("registered Host verifier bundle member hash mismatch")
            verifier.submit_reentry = lambda **_kwargs: (_ for _ in ()).throw(
                AssertionError("submit must not run with invalid verifier")
            )
            with patch.object(web_bridge, "_load_lifecycle_state", return_value=lifecycle), patch.object(
                web_bridge, "_registered_peer_attestation_verifier", return_value=verifier
            ), patch.object(
                web_bridge, "execute_web_reentry",
                side_effect=AssertionError("strong Host auto-stop must not use legacy browser reentry")
            ), patch.object(web_bridge, "schedule_auto_native_stop") as schedule:
                code = web_bridge.run_auto_native_stop(
                    session_id="controller-1",
                    repo=repo,
                    receipt_id="web-host-invalid",
                    registry=registry,
                    codex="codex",
                    delay_seconds=0,
                    state_path=state_path,
                )
            self.assertEqual(code, 78)
            schedule.assert_not_called()
            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["state"], "WEB_REENTRY_IDENTITY_UNAVAILABLE")
            self.assertEqual(saved["failure_class"], "web_reentry_identity_unavailable")
            self.assertEqual(saved["error_code"], "WEB_HOST_ATTESTATION_INVALID")

    def test_web_result_cannot_commit_or_rearm_after_desktop_handoff(self) -> None:
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); repo, registry, state_path = self.make_repo(root)
            payload = json.loads(registry.read_text())
            payload["__controller_sessions__"]["controller-1"]["desktop_codex"] = ["desktop-current"]
            payload["__controller_targets__"] = {"controller-1": {
                "web": {"status":"active","session_id":"web-current","generation":4},
                "desktop_codex": {"status":"active","session_id":"desktop-current","generation":2},
            }}
            payload["__controller_execution_ownership__"] = {"controller-1": {
                "active_host":"web","execution_target_session_id":"web-current","generation":8,
            }}
            registry.write_text(json.dumps(payload), encoding="utf-8")
            state_path.write_text(json.dumps({
                "receipt_id":"web-handoff","session_id":"controller-1","repo":str(repo.resolve()),
                "state":"RESUME_PENDING","pending_control_event":True,
            }), encoding="utf-8")
            lifecycle = {"pending_control_event":True,"requires_user":False,"controller_host":"web","wake_generation":8}

            def web_then_handoff(**_kwargs):
                changed = json.loads(registry.read_text())
                changed["__controller_execution_ownership__"]["controller-1"] = {
                    "active_host":"desktop_codex",
                    "execution_target_session_id":"desktop-current",
                    "generation":9,
                }
                registry.write_text(json.dumps(changed), encoding="utf-8")
                return {
                    "operation":"web_reentry","result":"CONFIRMED","state":"WEB_REENTRY_SUBMITTED",
                    "returncode":0,"execution_target_session_id":"web-current",
                    "target_generation":4,"target_mode":"explicit_current",
                }

            with patch.object(web_bridge, "_load_lifecycle_state", return_value=lifecycle), patch.object(
                web_bridge, "execute_web_reentry", side_effect=web_then_handoff
            ), patch.object(
                web_bridge, "_rearm_auto_native_stop",
                side_effect=AssertionError("stale Web ownership must not rearm")
            ), patch.object(
                web_bridge, "execute_native_resume",
                side_effect=AssertionError("original Web attempt must not call desktop")
            ):
                code = web_bridge.run_auto_native_stop(
                    session_id="controller-1", repo=repo, receipt_id="web-handoff", registry=registry,
                    codex="codex", delay_seconds=0, state_path=state_path,
                )

            self.assertEqual(code, 0)
            saved = json.loads(state_path.read_text())
            self.assertEqual(saved["state"], "WEB_REENTRY_SUPERSEDED_HOST_HANDOFF")
            self.assertEqual(saved["failure_class"], "host_ownership_superseded")
            self.assertTrue(saved["pending_control_event"])

    def test_desktop_result_cannot_persist_or_rearm_after_web_handoff(self) -> None:
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); repo, registry, state_path = self.make_repo(root)
            payload = json.loads(registry.read_text())
            payload["__controller_sessions__"]["controller-1"]["desktop_codex"] = ["desktop-current"]
            payload["__controller_targets__"] = {"controller-1": {
                "web": {"status":"active","session_id":"web-current","generation":4},
                "desktop_codex": {"status":"active","session_id":"desktop-current","generation":2},
            }}
            payload["__controller_execution_ownership__"] = {"controller-1": {
                "active_host":"desktop_codex","execution_target_session_id":"desktop-current","generation":8,
            }}
            registry.write_text(json.dumps(payload), encoding="utf-8")
            state_path.write_text(json.dumps({
                "receipt_id":"desktop-handoff","session_id":"controller-1","repo":str(repo.resolve()),
                "state":"RESUME_PENDING","pending_control_event":True,
            }), encoding="utf-8")
            lifecycle = {"pending_control_event":True,"requires_user":False,"controller_host":"desktop_codex","wake_generation":8}

            def native_then_handoff(**_kwargs):
                changed = json.loads(registry.read_text())
                changed["__controller_execution_ownership__"]["controller-1"] = {
                    "active_host":"web",
                    "execution_target_session_id":"web-current",
                    "generation":9,
                }
                registry.write_text(json.dumps(changed), encoding="utf-8")
                return {
                    "operation":"native_resume","result":"CONFIRMED","state":"RESUME_SUCCEEDED",
                    "pending_control_event":True,"returncode":0,"stdout_tail":"done","stderr_tail":"",
                    "controller_id":"controller-1","execution_target_session_id":"desktop-current",
                    "target_generation":2,"target_mode":"explicit_current",
                }

            with patch.object(web_bridge, "_load_lifecycle_state", return_value=lifecycle), patch.object(
                web_bridge, "execute_native_resume", side_effect=native_then_handoff
            ), patch.object(
                web_bridge, "persist_confirmed_auto_native_wake",
                side_effect=AssertionError("stale desktop ownership must not persist confirmed wake")
            ), patch.object(
                web_bridge, "_rearm_auto_native_stop",
                side_effect=AssertionError("stale desktop ownership must not rearm")
            ):
                code = web_bridge.run_auto_native_stop(
                    session_id="controller-1", repo=repo, receipt_id="desktop-handoff", registry=registry,
                    codex="codex", delay_seconds=0, state_path=state_path,
                )

            self.assertEqual(code, 0)
            saved = json.loads(state_path.read_text())
            self.assertEqual(saved["state"], "RESUME_SUPERSEDED_HOST_HANDOFF")
            self.assertEqual(saved["failure_class"], "host_ownership_superseded")
            self.assertTrue(saved["pending_control_event"])

    def test_detached_supervisor_defers_while_web_response_is_active_and_retries_without_counting_progress(self) -> None:
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            repo, registry, state_path = self.make_repo(Path(tmp))
            state_path.write_text(json.dumps({
                "receipt_id": "web-r2", "session_id": "controller-1", "repo": str(repo.resolve()),
                "state": "WEB_REENTRY_SUBMITTED", "pending_control_event": True,
                "continuation_count": 1, "unchanged_continuation_count": 0,
                "last_lifecycle_fingerprint": "fp-old",
            }), encoding="utf-8")
            lifecycle = {"pending_control_event": True, "requires_user": False, "controller_host": "web", "wake_generation": 8}
            deferred = {
                "operation": "web_reentry", "result": "DEFERRED", "state": "WEB_REENTRY_DEFERRED_ACTIVE",
                "returncode": 0, "failure_class": "web_host_active",
                "execution_target_session_id": "web-current", "target_generation": 0, "target_mode": "web_lease",
            }
            with patch.object(web_bridge, "_load_lifecycle_state", return_value=lifecycle), patch.object(
                web_bridge, "execute_web_reentry", return_value=deferred
            ), patch.object(web_bridge, "schedule_auto_native_stop") as schedule, patch.object(
                web_bridge, "execute_native_resume", side_effect=AssertionError("web supervisor must not invoke desktop Codex")
            ):
                code = web_bridge.run_auto_native_stop(
                    session_id="controller-1", repo=repo, receipt_id="web-r2", registry=registry,
                    codex="codex", delay_seconds=0, state_path=state_path,
                )
            self.assertEqual(code, 0)
            schedule.assert_called_once()
            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["state"], "WEB_REENTRY_DEFERRED_ACTIVE")
            self.assertEqual(saved["continuation_count"], 1)
            self.assertEqual(saved["unchanged_continuation_count"], 0)


class WebReentryDebounceTests(WebLocalReentryIntegrationTests):
    def test_web_confirmed_wake_debounces_against_current_web_lease_not_desktop_target(self) -> None:
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); repo, registry, _ = self.make_repo(root)
            registry_payload = json.loads(registry.read_text())
            registry_payload["__controller_sessions__"]["controller-1"]["desktop_codex"] = ["desktop-current"]
            registry_payload["__controller_targets__"] = {"controller-1":{
                "desktop_codex":{"status":"active","session_id":"desktop-current","generation":3},
                "web":{"status":"active","session_id":"web-current","generation":4},
            }}
            registry_payload["__controller_execution_ownership__"] = {"controller-1":{
                "active_host":"web",
                "execution_target_session_id":"web-current",
                "generation":8,
            }}
            registry.write_text(json.dumps(registry_payload), encoding="utf-8")
            lease = root / "leases.json"
            lease.write_text(json.dumps({"schema_version":1,"leases":{"controller-1":{
                "repo":str(repo.resolve()),"controller_id":"controller-1","web_session_id":"web-current",
                "authorized_at_unix":1,"expires_at_unix":4102444800,"provenance":"manual_user_authorized","mode":"resume_only"
            }}}), encoding="utf-8")
            lifecycle = {"pending_control_event":True,"controller_host":"web","wake_generation":4,"triggers":["READY:F1"]}
            receipt_path = root / "wake.json"
            receipt_path.write_text(json.dumps({
                "schema_version":1,
                "canonical_common_dir":str(web_bridge._git_common_dir(repo)),
                "controller_id":"controller-1",
                "event_fingerprint":web_bridge._wake_event_fingerprint(lifecycle),
                "result":"CONFIRMED","selected_host":"web",
                "execution_target_session_id":"web-current","target_generation":4,"ownership_generation":8,
                "target_mode":"explicit_current",
                "pending_control_event":True,
            }), encoding="utf-8")
            with patch.object(web_bridge, "DEFAULT_MANUAL_WEB_LEASES", lease), patch.object(
                web_bridge, "execute_web_reentry", side_effect=AssertionError("debounced Web wake must not resubmit")
            ):
                result = web_bridge.dispatch_pending_lifecycle_wake(
                    lifecycle_state=lifecycle, session_id="controller-1", repo=repo, registry=registry,
                    codex="codex", receipt_path=receipt_path,
                    host_facts={"controller_host":"web","resume_actionable":True},
                )
            self.assertTrue(result.get("debounced"))
            self.assertEqual(result["execution_target_session_id"], "web-current")

    def test_web_confirmed_wake_is_not_debounced_after_same_target_ownership_reclaim(self) -> None:
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, registry, _ = self.make_repo(root)
            registry_payload = json.loads(registry.read_text(encoding="utf-8"))
            registry_payload["__controller_targets__"] = {"controller-1": {"web": {
                "status": "active",
                "session_id": "web-current",
                "generation": 4,
            }}}
            registry_payload["__controller_execution_ownership__"] = {"controller-1": {
                "active_host": "web",
                "execution_target_session_id": "web-current",
                "generation": 9,
            }}
            registry.write_text(json.dumps(registry_payload), encoding="utf-8")
            lease = root / "leases.json"
            lease.write_text(json.dumps({"schema_version": 1, "leases": {"controller-1": {
                "repo": str(repo.resolve()),
                "controller_id": "controller-1",
                "web_session_id": "web-current",
                "authorized_at_unix": 1,
                "expires_at_unix": 4102444800,
                "provenance": "manual_user_authorized",
                "mode": "resume_only",
            }}}), encoding="utf-8")
            lifecycle = {
                "pending_control_event": True,
                "controller_host": "web",
                "wake_generation": 4,
                "triggers": ["READY:F1"],
            }
            receipt_path = root / "wake.json"
            receipt_path.write_text(json.dumps({
                "schema_version": 1,
                "canonical_common_dir": str(web_bridge._git_common_dir(repo)),
                "controller_id": "controller-1",
                "event_fingerprint": web_bridge._wake_event_fingerprint(lifecycle),
                "result": "CONFIRMED",
                "selected_host": "web",
                "execution_target_session_id": "web-current",
                "target_generation": 4,
                "ownership_generation": 8,
                "target_mode": "explicit_current",
                "pending_control_event": True,
            }), encoding="utf-8")
            fresh = {
                "result": "DEFERRED",
                "error_code": "FRESH_WAKE_REQUIRED",
                "ownership_generation": 9,
            }
            with patch.object(web_bridge, "DEFAULT_MANUAL_WEB_LEASES", lease), patch.object(
                web_bridge, "wake_existing_controller", return_value=fresh
            ) as wake:
                result = web_bridge.dispatch_pending_lifecycle_wake(
                    lifecycle_state=lifecycle,
                    session_id="controller-1",
                    repo=repo,
                    registry=registry,
                    codex="codex",
                    receipt_path=receipt_path,
                    host_facts={"controller_host": "web", "resume_actionable": True},
                )

            wake.assert_called_once()
            self.assertFalse(result.get("debounced", False))
            self.assertEqual(result["ownership_generation"], 9)


class WebReentryApprovalSupervisorTests(WebLocalReentryIntegrationTests):
    def test_waiting_local_approval_is_persisted_and_retried_without_desktop_fallback(self) -> None:
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            repo, registry, state_path = self.make_repo(Path(tmp))
            state_path.write_text(json.dumps({
                "receipt_id":"approval-r1","session_id":"controller-1","repo":str(repo.resolve()),
                "state":"RESUME_PENDING","pending_control_event":True,
            }), encoding="utf-8")
            lifecycle={"pending_control_event":True,"requires_user":False,"controller_host":"web","wake_generation":11}
            waiting={
                "operation":"web_reentry","result":"DEFERRED","state":"WEB_REENTRY_WAITING_LOCAL_APPROVAL",
                "returncode":0,"failure_class":"local_approval_required","approval_id":"approval-1",
                "approval_expires_at_unix":4102444800,"execution_target_session_id":"web-current","target_generation":0,"target_mode":"web_lease",
            }
            with patch.object(web_bridge,"_load_lifecycle_state",return_value=lifecycle), patch.object(
                web_bridge,"execute_web_reentry",return_value=waiting
            ) as reentry, patch.object(web_bridge,"schedule_auto_native_stop") as schedule, patch.object(
                web_bridge,"execute_native_resume",side_effect=AssertionError("approval wait must never use desktop Codex")
            ):
                code=web_bridge.run_auto_native_stop(
                    session_id="controller-1",repo=repo,receipt_id="approval-r1",registry=registry,
                    codex="codex",delay_seconds=0,state_path=state_path,
                )
            self.assertEqual(code,0)
            schedule.assert_called_once()
            saved=json.loads(state_path.read_text())
            self.assertEqual(saved["state"],"WEB_REENTRY_WAITING_LOCAL_APPROVAL")
            self.assertEqual(saved["approval_id"],"approval-1")
            self.assertEqual(saved["approval_retry_count"],1)
            self.assertEqual(reentry.call_args.kwargs.get("approval_id"),None)


def _manual_fenced_direct_wake_uses_builtin_adapter_without_peer_verifier(self):
    from unittest.mock import patch
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        repo, registry, _state_path = self.make_repo(root)
        payload = json.loads(registry.read_text(encoding="utf-8"))
        payload["__controller_targets__"] = {"controller-1": {"web": {
            "status": "active", "session_id": "web-current", "generation": 4,
            "provenance": "manual_user_authorized", "binding_mode": "temporary",
            "host_attested": False,
        }}}
        payload["__controller_execution_ownership__"] = {"controller-1": {
            "active_host": "web", "execution_target_session_id": "web-current",
            "generation": 4, "provenance": "manual_user_authorized",
        }}
        registry.write_text(json.dumps(payload), encoding="utf-8")
        receipt_path = root / "wake.json"
        confirmed = {
            "operation": "web_reentry", "result": "CONFIRMED",
            "state": "WEB_REENTRY_MANUAL_FENCED_SUBMITTED", "returncode": 0,
            "execution_target_session_id": "web-current", "target_generation": 4,
            "ownership_generation": 4, "target_mode": "explicit_current",
            "delivery_authorization": "manual_fenced", "host_attested": False,
            "strong_web_identity_established": False,
            "host_execution_receipt": {
                "host": "web", "web_session_id": "web-current", "tab_id": "tab-1",
                "source": "ai_bridge_browser", "submitted": True,
                "authorization": "manual_fenced", "host_attested": False,
            },
        }
        with patch.object(web_bridge, "_registered_peer_attestation_verifier", return_value=None), patch.object(
            web_bridge, "execute_web_reentry", return_value=confirmed
        ) as reentry, patch.object(
            web_bridge, "execute_native_resume", side_effect=AssertionError("web wake must not invoke desktop Codex")
        ):
            receipt = web_bridge.wake_existing_controller(
                lifecycle_state={"pending_control_event": True, "controller_host": "web", "wake_generation": 4},
                session_id="controller-1", repo=repo, registry=registry, codex="codex",
                receipt_path=receipt_path,
                host_facts={"controller_host": "web", "resume_actionable": True},
            )
    self.assertEqual(receipt["result"], "CONFIRMED")
    self.assertEqual(receipt["delivery_authorization"], "manual_fenced")
    self.assertFalse(receipt["host_attested"])
    self.assertFalse(receipt["strong_web_identity_established"])
    reentry.assert_called_once()


def _manual_fenced_supervisor_persists_unverified_delivery_evidence(self):
    from unittest.mock import patch
    with tempfile.TemporaryDirectory() as tmp:
        repo, registry, state_path = self.make_repo(Path(tmp))
        payload = json.loads(registry.read_text(encoding="utf-8"))
        payload["__controller_targets__"] = {"controller-1": {"web": {
            "status": "active", "session_id": "web-current", "generation": 4,
            "provenance": "manual_user_authorized", "binding_mode": "temporary",
            "host_attested": False,
        }}}
        payload["__controller_execution_ownership__"] = {"controller-1": {
            "active_host": "web", "execution_target_session_id": "web-current",
            "generation": 4, "provenance": "manual_user_authorized",
        }}
        registry.write_text(json.dumps(payload), encoding="utf-8")
        state_path.write_text(json.dumps({
            "receipt_id": "manual-r1", "session_id": "controller-1", "repo": str(repo.resolve()),
            "state": "RESUME_PENDING", "pending_control_event": True,
        }), encoding="utf-8")
        lifecycle = {
            "pending_control_event": True, "requires_user": False, "controller_host": "web",
            "wake_generation": 8, "triggers": ["READY:F1"],
            "snapshot": {"head":"h1","ledger_sha256":"l1","worktree_status_sha256":"w1","ready_ids":["F1"],"runnable_ids":["F1"],"candidate_revisions":[]},
        }
        confirmed = {
            "operation": "web_reentry", "result": "CONFIRMED",
            "state": "WEB_REENTRY_MANUAL_FENCED_SUBMITTED", "returncode": 0,
            "execution_target_session_id": "web-current", "target_generation": 4,
            "ownership_generation": 4, "target_mode": "explicit_current",
            "delivery_authorization": "manual_fenced", "host_attested": False,
            "strong_web_identity_established": False,
        }
        with patch.object(web_bridge, "_load_lifecycle_state", return_value=lifecycle), patch.object(
            web_bridge, "execute_web_reentry", return_value=confirmed
        ), patch.object(web_bridge, "schedule_auto_native_stop") as schedule, patch.object(
            web_bridge, "execute_native_resume", side_effect=AssertionError("web supervisor must not invoke desktop Codex")
        ):
            code = web_bridge.run_auto_native_stop(
                session_id="controller-1", repo=repo, receipt_id="manual-r1", registry=registry,
                codex="codex", delay_seconds=0, state_path=state_path,
            )
        saved = json.loads(state_path.read_text(encoding="utf-8"))
    self.assertEqual(code, 0)
    self.assertEqual(saved["state"], "WAITING_FOR_CONTROLLER_PROGRESS")
    self.assertEqual(saved["delivery_authorization"], "manual_fenced")
    self.assertFalse(saved["host_attested"])
    self.assertFalse(saved["strong_web_identity_established"])
    self.assertEqual(saved["target_generation"], 4)
    self.assertEqual(saved["ownership_generation"], 4)
    schedule.assert_not_called()


WebLocalReentryIntegrationTests.test_manual_fenced_direct_wake_uses_builtin_adapter_without_peer_verifier = _manual_fenced_direct_wake_uses_builtin_adapter_without_peer_verifier
WebLocalReentryIntegrationTests.test_manual_fenced_supervisor_persists_unverified_delivery_evidence = _manual_fenced_supervisor_persists_unverified_delivery_evidence

def _manual_fenced_direct_wake_passes_bridge_verifier_into_builtin_adapter(self):
    from unittest.mock import patch
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        repo, registry, _state_path = self.make_repo(root)
        payload = json.loads(registry.read_text(encoding="utf-8"))
        payload["__controller_targets__"] = {"controller-1": {"web": {
            "status": "active", "session_id": "web-current", "generation": 4,
            "provenance": "manual_user_authorized", "binding_mode": "temporary", "host_attested": False,
        }}}
        payload["__controller_execution_ownership__"] = {"controller-1": {
            "active_host": "web", "execution_target_session_id": "web-current", "generation": 4,
        }}
        registry.write_text(json.dumps(payload), encoding="utf-8")
        verifier = lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("reject"))
        seen = []
        def builtin(**kwargs):
            seen.append(kwargs.get("origin_verifier", "missing"))
            return {"operation":"web_reentry","result":"DEFERRED","state":"WEB_REENTRY_IDENTITY_UNAVAILABLE","returncode":78,"error_code":"WEB_HOST_ATTESTATION_INVALID"}
        with patch.object(web_bridge, "_registered_peer_attestation_verifier", return_value=verifier), patch.object(
            web_bridge, "execute_web_reentry", side_effect=builtin
        ):
            receipt = web_bridge.wake_existing_controller(
                lifecycle_state={"pending_control_event":True,"controller_host":"web","wake_generation":4},
                session_id="controller-1", repo=repo, registry=registry, codex="codex",
                receipt_path=root/"wake.json", host_facts={"controller_host":"web","resume_actionable":True},
            )
    self.assertEqual(seen, [verifier])
    self.assertEqual(receipt["result"], "DEFERRED")
    self.assertEqual(receipt["error_code"], "WEB_HOST_ATTESTATION_INVALID")


def _manual_fenced_supervisor_passes_bridge_verifier_into_builtin_adapter(self):
    from unittest.mock import patch
    with tempfile.TemporaryDirectory() as tmp:
        repo, registry, state_path = self.make_repo(Path(tmp))
        payload = json.loads(registry.read_text(encoding="utf-8"))
        payload["__controller_targets__"] = {"controller-1": {"web": {
            "status":"active","session_id":"web-current","generation":4,
            "provenance":"manual_user_authorized","binding_mode":"temporary","host_attested":False,
        }}}
        payload["__controller_execution_ownership__"] = {"controller-1": {
            "active_host":"web","execution_target_session_id":"web-current","generation":4,
        }}
        registry.write_text(json.dumps(payload), encoding="utf-8")
        state_path.write_text(json.dumps({
            "receipt_id":"manual-v1","session_id":"controller-1","repo":str(repo.resolve()),
            "state":"RESUME_PENDING","pending_control_event":True,
        }), encoding="utf-8")
        lifecycle={"pending_control_event":True,"requires_user":False,"controller_host":"web","wake_generation":8}
        verifier=lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("reject"))
        seen=[]
        def builtin(**kwargs):
            seen.append(kwargs.get("origin_verifier", "missing"))
            return {"operation":"web_reentry","result":"DEFERRED","state":"WEB_REENTRY_IDENTITY_UNAVAILABLE","returncode":78,"failure_class":"web_reentry_identity_unavailable","error_code":"WEB_HOST_ATTESTATION_INVALID"}
        with patch.object(web_bridge, "_registered_peer_attestation_verifier", return_value=verifier), patch.object(
            web_bridge, "_load_lifecycle_state", return_value=lifecycle
        ), patch.object(web_bridge, "execute_web_reentry", side_effect=builtin), patch.object(
            web_bridge, "schedule_auto_native_stop"
        ):
            web_bridge.run_auto_native_stop(
                session_id="controller-1", repo=repo, receipt_id="manual-v1", registry=registry,
                codex="codex", delay_seconds=0, state_path=state_path,
            )
    self.assertEqual(seen, [verifier])

WebLocalReentryIntegrationTests.test_manual_fenced_direct_wake_passes_bridge_verifier_into_builtin_adapter = _manual_fenced_direct_wake_passes_bridge_verifier_into_builtin_adapter
WebLocalReentryIntegrationTests.test_manual_fenced_supervisor_passes_bridge_verifier_into_builtin_adapter = _manual_fenced_supervisor_passes_bridge_verifier_into_builtin_adapter


def _manual_fenced_confirmed_waits_for_progress_without_resubmit(self):
    from unittest.mock import patch
    with tempfile.TemporaryDirectory() as tmp:
        repo, registry, state_path = self.make_repo(Path(tmp))
        payload = json.loads(registry.read_text(encoding="utf-8"))
        payload["__controller_targets__"] = {"controller-1": {"web": {
            "status": "active", "session_id": "web-current", "generation": 4,
            "provenance": "manual_user_authorized", "binding_mode": "temporary",
            "host_attested": False,
        }}}
        payload["__controller_execution_ownership__"] = {"controller-1": {
            "active_host": "web", "execution_target_session_id": "web-current",
            "generation": 4, "provenance": "manual_user_authorized",
        }}
        registry.write_text(json.dumps(payload), encoding="utf-8")
        state_path.write_text(json.dumps({
            "receipt_id": "manual-once", "session_id": "controller-1", "repo": str(repo.resolve()),
            "state": "RESUME_PENDING", "pending_control_event": True,
        }), encoding="utf-8")
        lifecycle = {
            "pending_control_event": True, "requires_user": False, "controller_host": "web",
            "wake_generation": 8, "triggers": ["READY:F1"],
            "snapshot": {"head":"h1","ledger_sha256":"l1","worktree_status_sha256":"w1","ready_ids":["F1"],"runnable_ids":["F1"],"candidate_revisions":[]},
        }
        confirmed = {
            "operation": "web_reentry", "result": "CONFIRMED",
            "state": "WEB_REENTRY_MANUAL_FENCED_SUBMITTED", "returncode": 0,
            "execution_target_session_id": "web-current", "target_generation": 4,
            "ownership_generation": 4, "target_mode": "explicit_current",
            "delivery_authorization": "manual_fenced", "host_attested": False,
            "strong_web_identity_established": False,
        }
        with patch.object(web_bridge, "_load_lifecycle_state", return_value=lifecycle), patch.object(
            web_bridge, "execute_web_reentry", return_value=confirmed
        ) as reentry, patch.object(web_bridge, "schedule_auto_native_stop") as schedule:
            code = web_bridge.run_auto_native_stop(
                session_id="controller-1", repo=repo, receipt_id="manual-once", registry=registry,
                codex="codex", delay_seconds=0, state_path=state_path,
            )
        saved = json.loads(state_path.read_text(encoding="utf-8"))
        waiting_controller_fence = web_bridge._controller_web_wait_fence(
            registry=registry, controller_id="controller-1"
        )
    self.assertEqual(code, 0)
    reentry.assert_called_once()
    schedule.assert_not_called()
    self.assertEqual(saved["state"], "WAITING_FOR_CONTROLLER_PROGRESS")
    self.assertEqual(saved["delivery_authorization"], "manual_fenced")
    self.assertFalse(saved["host_attested"])
    self.assertFalse(saved["strong_web_identity_established"])
    self.assertEqual(saved["continuation_count"], 1)
    self.assertEqual(saved["last_lifecycle_fingerprint"], web_bridge._wake_event_fingerprint(lifecycle))
    self.assertEqual(saved["waiting_controller_fence"], waiting_controller_fence)
    self.assertNotIn("waiting_registry_sha256", saved)


def _waiting_for_controller_progress_does_not_bootstrap_until_machine_facts_change(self):
    lifecycle = {
        "pending_control_event": True, "requires_user": False, "wake_generation": 8,
        "triggers": ["READY:F1"],
        "snapshot": {"head":"h1","ledger_sha256":"l1","worktree_status_sha256":"w1","ready_ids":["F1"],"runnable_ids":["F1"],"candidate_revisions":[]},
    }
    fingerprint = web_bridge._wake_event_fingerprint(lifecycle)
    fence = {
        "execution_target_session_id": "web-current",
        "target_generation": 4,
        "ownership_generation": 4,
        "target_provenance": "manual_user_authorized",
        "target_binding_mode": "temporary",
        "target_host_attested": False,
        "ownership_provenance": "manual_user_authorized",
    }
    waiting = {
        "state": "WAITING_FOR_CONTROLLER_PROGRESS",
        "pending_control_event": True,
        "last_lifecycle_fingerprint": fingerprint,
        "waiting_controller_fence": dict(fence),
        "delivery_authorization": "manual_fenced",
    }
    # Whole-registry changes are irrelevant when this Controller's target/ownership facts did not change.
    self.assertFalse(web_bridge.continuation_supervisor_needs_bootstrap(
        lifecycle, waiting,
        current_registry_sha256="completely-different-registry-hash",
        current_controller_wait_fence=dict(fence),
    ))
    changed_lifecycle = json.loads(json.dumps(lifecycle))
    changed_lifecycle["triggers"].append("LEDGER_CHANGED")
    self.assertTrue(web_bridge.continuation_supervisor_needs_bootstrap(
        changed_lifecycle, waiting,
        current_registry_sha256="another-registry-hash",
        current_controller_wait_fence=dict(fence),
    ))
    changed_fence = dict(fence)
    changed_fence["target_generation"] = 5
    changed_fence["ownership_generation"] = 5
    self.assertTrue(web_bridge.continuation_supervisor_needs_bootstrap(
        lifecycle, waiting,
        current_registry_sha256="yet-another-registry-hash",
        current_controller_wait_fence=changed_fence,
    ))
    # Invalid/missing current target facts fail closed instead of re-sending an already-confirmed wake.
    self.assertFalse(web_bridge.continuation_supervisor_needs_bootstrap(
        lifecycle, waiting,
        current_registry_sha256="registry-hash",
        current_controller_wait_fence=None,
    ))


WebLocalReentryIntegrationTests.test_manual_fenced_confirmed_waits_for_progress_without_resubmit = _manual_fenced_confirmed_waits_for_progress_without_resubmit
WebLocalReentryIntegrationTests.test_waiting_for_controller_progress_does_not_bootstrap_until_machine_facts_change = _waiting_for_controller_progress_does_not_bootstrap_until_machine_facts_change

# Hardened Web Controller recovery contract overrides.
def _hardened_recovery_current_manual_fixture(root: Path, *, with_desktop: bool = False):
    repo = root / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    registry = root / "controllers.json"
    sessions = {"web": ["web-old", "web-current"]}
    targets = {
        "web": {
            "status": "active",
            "session_id": "web-current",
            "generation": 3,
            "provenance": "manual_user_authorized",
            "binding_mode": "temporary",
            "host_attested": False,
        }
    }
    ownership = {
        "active_host": "web",
        "execution_target_session_id": "web-current",
        "generation": 3,
        "provenance": "manual_user_authorized",
    }
    if with_desktop:
        sessions["desktop_codex"] = ["desktop-current"]
        targets["desktop_codex"] = {
            "status": "active",
            "session_id": "desktop-current",
            "generation": 7,
        }
        ownership = {
            "active_host": "desktop_codex",
            "execution_target_session_id": "desktop-current",
            "generation": 7,
            "provenance": "desktop_entry",
        }
    registry.write_text(json.dumps({
        "controller-1": str(repo.resolve()),
        "__controller_sessions__": {"controller-1": sessions},
        "__controller_targets__": {"controller-1": targets},
        "__controller_execution_ownership__": {"controller-1": ownership},
    }), encoding="utf-8")
    return repo, registry


def _hardened_recovery_upgrades_current_target_in_place(self):
    from unittest.mock import patch
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        repo, registry = _hardened_recovery_current_manual_fixture(root)
        with patch.object(web_bridge, "_registered_peer_attestation_verifier", return_value=lambda **_kwargs: True):
            recovered = web_bridge.recover_same_controller_web_session(
                repo=repo, web_session_id="web-current", registry_path=registry,
                host_identity_receipt={"attested": True},
            )
        saved = json.loads(registry.read_text())
    self.assertEqual(recovered["result"], "RECOVERED")
    self.assertEqual(recovered["state"], "VERIFIED")
    self.assertEqual(recovered["controller_id"], "controller-1")
    self.assertEqual(recovered["execution_target_session_id"], "web-current")
    self.assertEqual(saved["__controller_targets__"]["controller-1"]["web"]["session_id"], "web-current")
    self.assertEqual(saved["__controller_targets__"]["controller-1"]["web"]["generation"], 4)
    self.assertEqual(saved["__controller_targets__"]["controller-1"]["web"]["provenance"], "host_attested_same_controller_recovery")
    self.assertEqual(saved["__controller_targets__"]["controller-1"]["web"]["identity_proof"], "host_attested_origin")


def _hardened_recovery_rejects_generation_change(self):
    from unittest.mock import patch
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        repo, registry = _hardened_recovery_current_manual_fixture(root)
        def verifier(**kwargs):
            self.assertEqual(kwargs.get("expected_target_session_id"), "web-current")
            self.assertEqual(kwargs.get("expected_target_generation"), 3)
            payload = json.loads(registry.read_text())
            payload["__controller_targets__"]["controller-1"]["web"]["generation"] = 4
            registry.write_text(json.dumps(payload), encoding="utf-8")
            return True
        with patch.object(web_bridge, "_registered_peer_attestation_verifier", return_value=verifier):
            with self.assertRaisesRegex(PermissionError, "generation.*changed|stale.*generation"):
                web_bridge.recover_same_controller_web_session(
                    repo=repo, web_session_id="web-current", registry_path=registry,
                    host_identity_receipt={"attested": True},
                )


def _hardened_recovery_without_verifier_preserves_state(self):
    from unittest.mock import patch
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        repo, registry = _hardened_recovery_current_manual_fixture(root)
        before = registry.read_text()
        with patch.object(web_bridge, "_registered_peer_attestation_verifier", return_value=None):
            result = web_bridge.recover_same_controller_web_session(
                repo=repo, web_session_id="web-current", registry_path=registry,
                host_identity_receipt=None,
            )
        after = registry.read_text()
    self.assertEqual(result["result"], "DEFERRED")
    self.assertEqual(result["reason"], "HOST_IDENTITY_UNAVAILABLE")
    self.assertEqual(before, after)


def _hardened_recovery_verifier_exception_preserves_state(self):
    from unittest.mock import patch
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        repo, registry = _hardened_recovery_current_manual_fixture(root)
        before = registry.read_text()
        def unavailable(**_kwargs):
            raise RuntimeError("verifier temporarily unavailable")
        with patch.object(web_bridge, "_registered_peer_attestation_verifier", return_value=unavailable):
            result = web_bridge.recover_same_controller_web_session(
                repo=repo, web_session_id="web-current", registry_path=registry,
                host_identity_receipt={"attested": True},
            )
        after = registry.read_text()
    self.assertEqual(result["result"], "DEFERRED")
    self.assertEqual(result["reason"], "HOST_IDENTITY_VERIFIER_UNAVAILABLE")
    self.assertEqual(before, after)


def _hardened_recovery_idempotent_current_target(self):
    from unittest.mock import patch
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        repo, registry = _hardened_recovery_current_manual_fixture(root)
        with patch.object(web_bridge, "_registered_peer_attestation_verifier", return_value=lambda **_kwargs: True):
            first = web_bridge.recover_same_controller_web_session(
                repo=repo, web_session_id="web-current", registry_path=registry,
                host_identity_receipt={"attested": True},
            )
            second = web_bridge.recover_same_controller_web_session(
                repo=repo, web_session_id="web-current", registry_path=registry,
                host_identity_receipt={"attested": True},
            )
    self.assertEqual(first["result"], "RECOVERED")
    self.assertEqual(second["result"], "ALREADY_VERIFIED")


def _hardened_recovery_preserves_desktop_target(self):
    from unittest.mock import patch
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        repo, registry = _hardened_recovery_current_manual_fixture(root, with_desktop=True)
        with patch.object(web_bridge, "_registered_peer_attestation_verifier", return_value=lambda **_kwargs: True):
            recovered = web_bridge.recover_same_controller_web_session(
                repo=repo, web_session_id="web-current", registry_path=registry,
                host_identity_receipt={"attested": True},
            )
        saved = json.loads(registry.read_text())
    self.assertEqual(recovered["target_generation"], 4)
    self.assertEqual(saved["__controller_targets__"]["controller-1"]["desktop_codex"]["session_id"], "desktop-current")
    self.assertEqual(saved["__controller_targets__"]["controller-1"]["desktop_codex"]["generation"], 7)
    self.assertEqual(saved["__controller_targets__"]["controller-1"]["web"]["session_id"], "web-current")


def _hardened_recovery_aligns_existing_lease_to_current_target(self):
    from unittest.mock import patch
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        repo, registry = _hardened_recovery_current_manual_fixture(root)
        lease = root / "manual-web-leases.json"
        lease.write_text(json.dumps({
            "schema_version": 1,
            "leases": {"controller-1": {
                "repo": str(repo.resolve()), "controller_id": "controller-1",
                "web_session_id": "web-old", "authorized_at_unix": 100,
                "expires_at_unix": 4102444800,
                "provenance": "manual_user_authorized", "mode": "resume_only",
            }},
        }), encoding="utf-8")
        with patch.object(web_bridge, "DEFAULT_MANUAL_WEB_LEASES", lease), patch.object(
            web_bridge, "_registered_peer_attestation_verifier", return_value=lambda **_kwargs: True
        ):
            recovered = web_bridge.recover_same_controller_web_session(
                repo=repo, web_session_id="web-current", registry_path=registry,
                host_identity_receipt={"attested": True},
            )
        rec = json.loads(lease.read_text())["leases"]["controller-1"]
    self.assertFalse(recovered["resume_lease_rotated"])
    self.assertEqual(rec["web_session_id"], "web-old")
    self.assertEqual(rec["authorized_at_unix"], 100)
    self.assertEqual(rec["expires_at_unix"], 4102444800)


def _hardened_recovery_no_prior_lease(self):
    from unittest.mock import patch
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        repo, registry = _hardened_recovery_current_manual_fixture(root)
        lease = root / "manual-web-leases.json"
        with patch.object(web_bridge, "DEFAULT_MANUAL_WEB_LEASES", lease), patch.object(
            web_bridge, "_registered_peer_attestation_verifier", return_value=lambda **_kwargs: True
        ):
            recovered = web_bridge.recover_same_controller_web_session(
                repo=repo, web_session_id="web-current", registry_path=registry,
                host_identity_receipt={"attested": True},
            )
    self.assertEqual(recovered["result"], "RECOVERED")
    self.assertFalse(recovered["resume_lease_rotated"])
    self.assertFalse(lease.exists())


def _historical_alias_cannot_recover_even_with_trusted_verifier(self):
    from unittest.mock import patch
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        repo, registry = _hardened_recovery_current_manual_fixture(root)
        before = registry.read_text()
        verifier = patch.object(web_bridge, "_registered_peer_attestation_verifier", return_value=lambda **_kwargs: True)
        with verifier:
            with self.assertRaisesRegex(PermissionError, "cannot replace the canonical current target"):
                web_bridge.recover_same_controller_web_session(
                    repo=repo, web_session_id="web-old", registry_path=registry,
                    host_identity_receipt={"attested": True},
                )
        after = registry.read_text()
    self.assertEqual(before, after)


def _unbound_chat_cannot_recover_even_with_trusted_verifier(self):
    from unittest.mock import patch
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        repo, registry = _hardened_recovery_current_manual_fixture(root)
        before = registry.read_text()
        with patch.object(web_bridge, "_registered_peer_attestation_verifier", return_value=lambda **_kwargs: True):
            with self.assertRaisesRegex(PermissionError, "cannot replace the canonical current target"):
                web_bridge.recover_same_controller_web_session(
                    repo=repo, web_session_id="ordinary-project-chat", registry_path=registry,
                    host_identity_receipt={"attested": True},
                )
        after = registry.read_text()
    self.assertEqual(before, after)


def _expired_manual_lease_retargets_without_renewal(self):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        repo = root / "repo"; repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
        registry = root / "controllers.json"
        registry.write_text(json.dumps({
            "controller-1": str(repo.resolve()),
            "__controller_sessions__": {"controller-1": {"web": ["web-old", "web-new"]}},
            "__controller_targets__": {"controller-1": {"web": {
                "status": "active", "session_id": "web-old", "generation": 2,
                "provenance": "manual_user_authorized", "binding_mode": "temporary", "host_attested": False,
            }}},
            "__controller_execution_ownership__": {"controller-1": {
                "active_host": "web", "execution_target_session_id": "web-old", "generation": 2,
                "provenance": "manual_user_authorized",
            }},
        }), encoding="utf-8")
        lease = root / "manual-web-leases.json"
        lease.write_text(json.dumps({
            "schema_version": 1,
            "leases": {"controller-1": {
                "repo": str(repo.resolve()), "controller_id": "controller-1",
                "web_session_id": "web-old", "authorized_at_unix": 10,
                "expires_at_unix": 20, "suspended_reason": "old-stop",
                "provenance": "manual_user_authorized", "mode": "resume_only",
            }},
        }), encoding="utf-8")
        result = web_bridge.replace_web_session(
            repo=repo, controller_id="controller-1", web_session_id="web-new",
            expected_generation=2, expected_ownership_generation=2,
            registry_path=registry, lease_path=lease,
        )
        rec = json.loads(lease.read_text())["leases"]["controller-1"]
    self.assertTrue(result["resume_lease_rotated"])
    self.assertEqual(rec["web_session_id"], "web-new")
    self.assertEqual(rec["authorized_at_unix"], 10)
    self.assertEqual(rec["expires_at_unix"], 20)
    self.assertEqual(rec["suspended_reason"], "old-stop")


WebLifecycleBridgeTests.test_same_controller_web_recovery_rebinds_trusted_new_session_without_new_controller = _hardened_recovery_upgrades_current_target_in_place
WebLifecycleBridgeTests.test_same_controller_web_recovery_rejects_attestation_if_target_generation_changes_before_lock = _hardened_recovery_rejects_generation_change
WebLifecycleBridgeTests.test_same_controller_web_recovery_without_host_verifier_preserves_existing_controller_and_state = _hardened_recovery_without_verifier_preserves_state
WebLifecycleBridgeTests.test_same_controller_web_recovery_verifier_exception_degrades_without_revoking_controller = _hardened_recovery_verifier_exception_preserves_state
WebLifecycleBridgeTests.test_same_controller_web_recovery_is_idempotent_after_user_reconfirms_ownership = _hardened_recovery_idempotent_current_target
WebLifecycleBridgeTests.test_web_recovery_preserves_desktop_target_and_only_advances_web_generation = _hardened_recovery_preserves_desktop_target
WebLifecycleBridgeTests.test_same_controller_web_recovery_does_not_rotate_manual_resume_lease = _hardened_recovery_aligns_existing_lease_to_current_target
WebLifecycleBridgeTests.test_same_controller_web_recovery_does_not_create_resume_lease_without_prior_authorization = _hardened_recovery_no_prior_lease
WebLifecycleBridgeTests.test_historical_alias_cannot_recover_even_with_trusted_verifier = _historical_alias_cannot_recover_even_with_trusted_verifier
WebLifecycleBridgeTests.test_unbound_chat_cannot_recover_even_with_trusted_verifier = _unbound_chat_cannot_recover_even_with_trusted_verifier
WebLifecycleBridgeTests.test_expired_manual_lease_retargets_without_renewal = _expired_manual_lease_retargets_without_renewal
# Retain legacy release-gate method names but bind them to the hardened contract.
WebLifecycleBridgeTests.test_browser_tab_receipt_cannot_recover_an_unverified_web_session = _unbound_chat_cannot_recover_even_with_trusted_verifier
WebLifecycleBridgeTests.test_same_controller_web_recovery_cannot_replace_different_host_attested_current_target = _historical_alias_cannot_recover_even_with_trusted_verifier
# The old test name asserted a now-forbidden recovery-driven lease rotation.
if hasattr(WebLifecycleBridgeTests, "test_same_controller_web_recovery_rotates_existing_resume_only_lease_to_new_verified_target"):
    delattr(WebLifecycleBridgeTests, "test_same_controller_web_recovery_rotates_existing_resume_only_lease_to_new_verified_target")


def _session_start_requires_explicit_verified_current_target(self):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        repo = root / "repo"; repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
        (repo / "AGENTS.md").write_text("rules\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True)
        registry = root / "controllers.json"
        registry.write_text(json.dumps({
            "controller-1": str(repo.resolve()),
            "__controller_sessions__": {"controller-1": {"web": ["web-session-1"]}},
            "__controller_targets__": {"controller-1": {"web": {
                "status": "active", "session_id": "web-session-1", "generation": 1,
                "provenance": "host_attested_same_controller_recovery", "binding_mode": "resume_only",
                "identity_proof": "host_attested_origin",
            }}},
            "__controller_execution_ownership__": {"controller-1": {
                "active_host": "web", "execution_target_session_id": "web-session-1", "generation": 1,
                "provenance": "web_entry",
            }},
        }), encoding="utf-8")
        result = self.run_bridge(
            "session-start", "--repo", str(repo), "--registry", str(registry),
            "--web-session-id", "web-session-1",
        )
    self.assertEqual(result.returncode, 0, result.stderr)
    payload = json.loads(result.stdout)
    self.assertEqual(payload["web_session_id"], "web-session-1")


def _session_start_foreign_or_alias_never_becomes_current(self):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        repo = root / "repo"; repo.mkdir()
        other = root / "other"; other.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
        registry = root / "controllers.json"
        registry.write_text(json.dumps({
            "controller-1": str(repo.resolve()), "controller-2": str(other.resolve()),
            "__controller_sessions__": {
                "controller-1": {"web": ["web-current", "web-old"]},
                "controller-2": {"web": ["web-other"]},
            },
            "__controller_targets__": {"controller-1": {"web": {
                "status": "active", "session_id": "web-current", "generation": 2,
                "provenance": "manual_user_authorized", "binding_mode": "temporary", "host_attested": False,
            }}},
            "__controller_execution_ownership__": {"controller-1": {
                "active_host": "web", "execution_target_session_id": "web-current", "generation": 2,
                "provenance": "manual_user_authorized",
            }},
        }), encoding="utf-8")
        for sid in ("web-old", "web-other"):
            result = self.run_bridge(
                "session-start", "--repo", str(repo), "--registry", str(registry),
                "--web-session-id", sid,
            )
            self.assertEqual(result.returncode, 78)
        saved = json.loads(registry.read_text())
    self.assertEqual(saved["__controller_targets__"]["controller-1"]["web"]["session_id"], "web-current")
    self.assertEqual(saved["__controller_targets__"]["controller-1"]["web"]["generation"], 2)


WebControllerSessionIdentityTests.test_session_start_accepts_only_bound_web_controller_session = _session_start_requires_explicit_verified_current_target
WebControllerSessionIdentityTests.test_session_start_refuses_web_session_bound_to_another_controller = _session_start_foreign_or_alias_never_becomes_current


def _nonretryable_web_failure_same_event_and_fence_stays_quiet(self):
    lifecycle = {
        "pending_control_event": True,
        "requires_user": False,
        "wake_generation": 12,
        "triggers": ["rule_update_pending:rev-x"],
        "snapshot": {"head":"h","ledger_sha256":"l","worktree_status_sha256":"w","ready_ids":[],"runnable_ids":[],"candidate_revisions":[]},
    }
    fingerprint = web_bridge._wake_event_fingerprint(lifecycle)
    fence = {
        "execution_target_session_id": "web-current",
        "target_generation": 4,
        "ownership_generation": 8,
        "target_provenance": "host_attested_same_controller_recovery",
        "target_binding_mode": "resume_only",
        "target_host_attested": None,
        "ownership_provenance": "web_entry",
    }
    for state_name, failure_class in (
        ("WEB_REENTRY_FAILED_BEFORE_DISPATCH", "web_reentry_failed_before_dispatch"),
        ("WEB_REENTRY_RESULT_UNKNOWN", "web_reentry_result_unknown"),
    ):
        supervisor = {
            "state": state_name,
            "failure_class": failure_class,
            "pending_control_event": True,
            "last_lifecycle_fingerprint": fingerprint,
            "blocked_controller_fence": dict(fence),
        }
        self.assertFalse(web_bridge.continuation_supervisor_needs_bootstrap(
            lifecycle, supervisor, current_controller_wait_fence=dict(fence)
        ))
        changed_lifecycle = json.loads(json.dumps(lifecycle))
        changed_lifecycle["triggers"].append("NEW_EVENT")
        self.assertTrue(web_bridge.continuation_supervisor_needs_bootstrap(
            changed_lifecycle, supervisor, current_controller_wait_fence=dict(fence)
        ))
        changed_fence = dict(fence)
        changed_fence["target_generation"] = 5
        changed_fence["ownership_generation"] = 9
        self.assertTrue(web_bridge.continuation_supervisor_needs_bootstrap(
            lifecycle, supervisor, current_controller_wait_fence=changed_fence
        ))
        self.assertFalse(web_bridge.continuation_supervisor_needs_bootstrap(
            lifecycle, supervisor, current_controller_wait_fence=None
        ))


def _registered_host_nonretryable_failure_persists_quiet_fence(self):
    from unittest.mock import patch
    with tempfile.TemporaryDirectory() as tmp:
        repo, registry, state_path = self.make_repo(Path(tmp))
        payload = json.loads(registry.read_text(encoding="utf-8"))
        payload["__controller_targets__"] = {"controller-1": {"web": {
            "status": "active", "session_id": "web-current", "generation": 4,
            "provenance": "host_attested_same_controller_recovery",
            "binding_mode": "resume_only", "identity_proof": "host_attested_origin",
        }}}
        payload["__controller_execution_ownership__"] = {"controller-1": {
            "active_host": "web", "execution_target_session_id": "web-current",
            "generation": 8, "provenance": "web_entry",
        }}
        registry.write_text(json.dumps(payload), encoding="utf-8")
        state_path.write_text(json.dumps({
            "receipt_id":"host-fail","session_id":"controller-1","repo":str(repo.resolve()),
            "state":"RESUME_PENDING","pending_control_event":True,
        }), encoding="utf-8")
        lifecycle = {
            "pending_control_event": True, "requires_user": False, "controller_host": "web",
            "wake_generation": 12, "triggers": ["rule_update_pending:rev-x"],
            "snapshot": {"head":"h","ledger_sha256":"l","worktree_status_sha256":"w","ready_ids":[],"runnable_ids":[],"candidate_revisions":[]},
        }
        attempt = {
            "operation":"web_reentry", "result":"FAILED",
            "state":"WEB_REENTRY_FAILED_BEFORE_DISPATCH", "returncode":1,
            "failure_class":"web_reentry_failed_before_dispatch",
            "error_code":"WEB_REENTRY_FAILED_BEFORE_DISPATCH",
            "execution_target_session_id":"web-current", "target_generation":4,
            "ownership_generation":8, "target_mode":"explicit_current",
            "delivery_authorization":"host_attested", "host_attested":True,
            "strong_web_identity_established":True,
        }
        def verifier(**_kwargs):
            return {"call_receipt":"host-call"}
        verifier.submit_reentry = lambda **_kwargs: None
        with patch.object(web_bridge, "_load_lifecycle_state", return_value=lifecycle), patch.object(
            web_bridge, "_registered_peer_attestation_verifier", return_value=verifier
        ), patch.object(
            web_bridge, "_execute_registered_web_host_reentry", return_value=attempt
        ), patch.object(web_bridge, "schedule_auto_native_stop") as schedule:
            code = web_bridge.run_auto_native_stop(
                session_id="controller-1", repo=repo, receipt_id="host-fail", registry=registry,
                codex="codex", delay_seconds=0, state_path=state_path,
            )
        saved = json.loads(state_path.read_text(encoding="utf-8"))
        current_fence = web_bridge._controller_web_wait_fence(
            registry=registry, controller_id="controller-1"
        )
    self.assertEqual(code, 1)
    schedule.assert_not_called()
    self.assertEqual(saved["state"], "WEB_REENTRY_FAILED_BEFORE_DISPATCH")
    self.assertEqual(saved["failure_class"], "web_reentry_failed_before_dispatch")
    self.assertEqual(saved["last_lifecycle_fingerprint"], web_bridge._wake_event_fingerprint(lifecycle))
    self.assertEqual(saved["blocked_controller_fence"], current_fence)
    self.assertFalse(web_bridge.continuation_supervisor_needs_bootstrap(
        lifecycle, saved, current_controller_wait_fence=current_fence
    ))


WebContinuationSupervisorBootstrapTests.test_nonretryable_web_failure_same_event_and_fence_stays_quiet = _nonretryable_web_failure_same_event_and_fence_stays_quiet
WebLocalReentryIntegrationTests.test_registered_host_nonretryable_failure_persists_quiet_fence = _registered_host_nonretryable_failure_persists_quiet_fence


def _registered_host_result_unknown_persists_quiet_fence(self):
    from unittest.mock import patch
    with tempfile.TemporaryDirectory() as tmp:
        repo, registry, state_path = self.make_repo(Path(tmp))
        payload = json.loads(registry.read_text(encoding="utf-8"))
        payload["__controller_targets__"] = {"controller-1": {"web": {
            "status": "active", "session_id": "web-current", "generation": 4,
            "provenance": "host_attested_same_controller_recovery",
            "binding_mode": "resume_only", "identity_proof": "host_attested_origin",
        }}}
        payload["__controller_execution_ownership__"] = {"controller-1": {
            "active_host": "web", "execution_target_session_id": "web-current",
            "generation": 8, "provenance": "web_entry",
        }}
        registry.write_text(json.dumps(payload), encoding="utf-8")
        state_path.write_text(json.dumps({
            "receipt_id":"host-unknown","session_id":"controller-1","repo":str(repo.resolve()),
            "state":"RESUME_PENDING","pending_control_event":True,
        }), encoding="utf-8")
        lifecycle = {
            "pending_control_event": True, "requires_user": False, "controller_host": "web",
            "wake_generation": 13, "triggers": ["terminal_receipt_pending"],
            "snapshot": {"head":"h","ledger_sha256":"l","worktree_status_sha256":"w","ready_ids":[],"runnable_ids":[],"candidate_revisions":[]},
        }
        attempt = {
            "operation":"web_reentry", "result":"BLOCKED",
            "state":"WEB_REENTRY_RESULT_UNKNOWN", "returncode":78,
            "failure_class":"web_reentry_result_unknown",
            "error_code":"WEB_REENTRY_RESULT_UNKNOWN",
            "stderr_tail":"Host dispatch occurred but submit confirmation is unknown; automatic retry is forbidden",
            "execution_target_session_id":"web-current", "target_generation":4,
            "ownership_generation":8, "target_mode":"explicit_current",
            "delivery_authorization":"host_attested", "host_attested":True,
            "strong_web_identity_established":True,
        }
        def verifier(**_kwargs):
            return {"call_receipt":"host-call"}
        verifier.submit_reentry = lambda **_kwargs: None
        with patch.object(web_bridge, "_load_lifecycle_state", return_value=lifecycle), patch.object(
            web_bridge, "_registered_peer_attestation_verifier", return_value=verifier
        ), patch.object(
            web_bridge, "_execute_registered_web_host_reentry", return_value=attempt
        ), patch.object(web_bridge, "schedule_auto_native_stop") as schedule:
            code = web_bridge.run_auto_native_stop(
                session_id="controller-1", repo=repo, receipt_id="host-unknown", registry=registry,
                codex="codex", delay_seconds=0, state_path=state_path,
            )
        saved = json.loads(state_path.read_text(encoding="utf-8"))
        current_fence = web_bridge._controller_web_wait_fence(
            registry=registry, controller_id="controller-1"
        )
    self.assertEqual(code, 78)
    schedule.assert_not_called()
    self.assertEqual(saved["state"], "WEB_REENTRY_RESULT_UNKNOWN")
    self.assertEqual(saved["failure_class"], "web_reentry_result_unknown")
    self.assertEqual(saved["last_lifecycle_fingerprint"], web_bridge._wake_event_fingerprint(lifecycle))
    self.assertEqual(saved["blocked_controller_fence"], current_fence)
    self.assertFalse(web_bridge.continuation_supervisor_needs_bootstrap(
        lifecycle, saved, current_controller_wait_fence=current_fence
    ))


WebLocalReentryIntegrationTests.test_registered_host_result_unknown_persists_quiet_fence = _registered_host_result_unknown_persists_quiet_fence


def _confirmed_web_reentry_clears_stale_nonretryable_block_evidence(self):
    from unittest.mock import patch
    with tempfile.TemporaryDirectory() as tmp:
        repo, registry, state_path = self.make_repo(Path(tmp))
        payload = json.loads(registry.read_text(encoding="utf-8"))
        payload["__controller_targets__"] = {"controller-1": {"web": {
            "status": "active", "session_id": "web-current", "generation": 4,
            "provenance": "manual_user_authorized", "binding_mode": "temporary",
            "host_attested": False,
        }}}
        payload["__controller_execution_ownership__"] = {"controller-1": {
            "active_host": "web", "execution_target_session_id": "web-current",
            "generation": 4, "provenance": "manual_user_authorized",
        }}
        registry.write_text(json.dumps(payload), encoding="utf-8")
        stale_fence = web_bridge._controller_web_wait_fence(
            registry=registry, controller_id="controller-1"
        )
        state_path.write_text(json.dumps({
            "receipt_id": "recover-confirmed", "session_id": "controller-1",
            "repo": str(repo.resolve()), "state": "WEB_REENTRY_FAILED_BEFORE_DISPATCH",
            "pending_control_event": True,
            "failure_class": "web_reentry_failed_before_dispatch",
            "error_code": "WEB_REENTRY_FAILED_BEFORE_DISPATCH",
            "blocked_controller_fence": stale_fence,
            "blocked_since_unix_ms": 123,
        }), encoding="utf-8")
        lifecycle = {
            "pending_control_event": True, "requires_user": False, "controller_host": "web",
            "wake_generation": 14, "triggers": ["NEW_EVENT"],
            "snapshot": {"head":"h2","ledger_sha256":"l2","worktree_status_sha256":"w2","ready_ids":[],"runnable_ids":[],"candidate_revisions":[]},
        }
        confirmed = {
            "operation":"web_reentry", "result":"CONFIRMED",
            "state":"WEB_REENTRY_MANUAL_FENCED_SUBMITTED", "returncode":0,
            "execution_target_session_id":"web-current", "target_generation":4,
            "ownership_generation":4, "target_mode":"explicit_current",
            "delivery_authorization":"manual_fenced", "host_attested":False,
            "strong_web_identity_established":False,
        }
        with patch.object(web_bridge, "_load_lifecycle_state", return_value=lifecycle), patch.object(
            web_bridge, "execute_web_reentry", return_value=confirmed
        ), patch.object(web_bridge, "schedule_auto_native_stop") as schedule:
            code = web_bridge.run_auto_native_stop(
                session_id="controller-1", repo=repo, receipt_id="recover-confirmed",
                registry=registry, codex="codex", delay_seconds=0, state_path=state_path,
            )
        saved = json.loads(state_path.read_text(encoding="utf-8"))
    self.assertEqual(code, 0)
    schedule.assert_not_called()
    self.assertEqual(saved["state"], "WAITING_FOR_CONTROLLER_PROGRESS")
    self.assertNotIn("failure_class", saved)
    self.assertNotIn("error_code", saved)
    self.assertNotIn("blocked_controller_fence", saved)
    self.assertNotIn("blocked_since_unix_ms", saved)


WebLocalReentryIntegrationTests.test_confirmed_web_reentry_clears_stale_nonretryable_block_evidence = _confirmed_web_reentry_clears_stale_nonretryable_block_evidence


def _strong_host_confirmed_submit_waits_without_rearm(self):
    from unittest.mock import patch
    with tempfile.TemporaryDirectory() as tmp:
        repo, registry, state_path = self.make_repo(Path(tmp))
        payload = json.loads(registry.read_text(encoding="utf-8"))
        payload["__controller_targets__"] = {"controller-1": {"web": {
            "status":"active","session_id":"web-current","generation":4,
            "provenance":"host_attested_same_controller_recovery","binding_mode":"resume_only",
            "identity_proof":"host_attested_origin",
        }}}
        payload["__controller_execution_ownership__"] = {"controller-1": {
            "active_host":"web","execution_target_session_id":"web-current",
            "generation":4,"provenance":"web_entry",
        }}
        registry.write_text(json.dumps(payload), encoding="utf-8")
        state_path.write_text(json.dumps({
            "receipt_id":"rule-update:rev-confirmed","session_id":"controller-1",
            "repo":str(repo.resolve()),"state":"RESUME_PENDING","pending_control_event":True,
        }), encoding="utf-8")
        lifecycle = {
            "pending_control_event":True,"requires_user":False,"controller_host":"web",
            "wake_generation":7,"triggers":["rule_update_pending:rev-confirmed"],
            "snapshot":{"head":"h","ledger_sha256":"l","worktree_status_sha256":"w","ready_ids":[],"runnable_ids":[],"candidate_revisions":[],
                "rule_handshake":{"installed_revision":"rev-confirmed","state":"pending_ack","blocking":True}},
        }
        confirmed = {
            "operation":"web_reentry","result":"CONFIRMED","state":"WEB_REENTRY_SUBMITTED","returncode":0,
            "execution_target_session_id":"web-current","target_generation":4,"ownership_generation":4,
            "target_mode":"explicit_current","delivery_authorization":"host_attested",
            "host_attested":True,"strong_web_identity_established":True,
        }
        def verifier(**_kwargs): return {"call_receipt":"host-call"}
        verifier.submit_reentry = lambda **_kwargs: None
        with patch.object(web_bridge,"_load_lifecycle_state",return_value=lifecycle), patch.object(
            web_bridge,"_registered_peer_attestation_verifier",return_value=verifier
        ), patch.object(web_bridge,"_execute_registered_web_host_reentry",return_value=confirmed), patch.object(
            web_bridge,"schedule_auto_native_stop"
        ) as schedule:
            code=web_bridge.run_auto_native_stop(
                session_id="controller-1",repo=repo,receipt_id="rule-update:rev-confirmed",
                registry=registry,codex="codex",delay_seconds=0,state_path=state_path,
            )
        saved=json.loads(state_path.read_text())
    self.assertEqual(code,0)
    schedule.assert_not_called()
    self.assertEqual(saved["state"],"WAITING_FOR_CONTROLLER_PROGRESS")
    self.assertEqual(saved["delivery_terminal_receipt_id"],"rule-update:rev-confirmed")
    self.assertEqual(saved["delivery_terminal_outcome"],"submit_confirmed")


def _terminal_rule_delivery_blocks_bootstrap_across_fingerprint_changes(self):
    base = {
        "pending_control_event":True,"requires_user":False,"wake_generation":7,
        "triggers":["rule_update_pending:rev-confirmed"],
        "snapshot":{"head":"h1","ledger_sha256":"l1","worktree_status_sha256":"w1","ready_ids":[],"runnable_ids":[],"candidate_revisions":[],
            "rule_handshake":{"installed_revision":"rev-confirmed","state":"pending_ack","blocking":True}},
    }
    changed=json.loads(json.dumps(base))
    changed["snapshot"]["head"]="h2"
    changed["triggers"].append("main_head_changed")
    state={
        "state":"WAITING_FOR_CONTROLLER_PROGRESS","receipt_id":"rule-update:rev-confirmed",
        "delivery_terminal_receipt_id":"rule-update:rev-confirmed",
        "delivery_terminal_key":"rule-update:rev-confirmed","delivery_terminal_outcome":"submit_confirmed",
    }
    self.assertFalse(web_bridge.continuation_supervisor_needs_bootstrap(base,state))
    self.assertFalse(web_bridge.continuation_supervisor_needs_bootstrap(changed,state))
    newer=json.loads(json.dumps(changed))
    newer["triggers"]=["rule_update_pending:rev-new"]
    newer["snapshot"]["rule_handshake"]["installed_revision"]="rev-new"
    self.assertTrue(web_bridge.continuation_supervisor_needs_bootstrap(newer,state))


def _same_terminal_receipt_cannot_be_rescheduled(self):
    from unittest.mock import patch
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp); repo=root/"repo"; repo.mkdir(); subprocess.run(["git","init","-q","-b","main",str(repo)],check=True)
        registry=root/"registry.json"; registry.write_text("{}")
        state=root/"auto.json"
        state.write_text(json.dumps({
            "receipt_id":"rule-update:rev1","state":"WEB_REENTRY_RESULT_UNKNOWN",
            "delivery_terminal_receipt_id":"rule-update:rev1","delivery_terminal_key":"rule-update:rev1",
            "delivery_terminal_outcome":"result_unknown","continuation_count":1,"retry_count":0,
        }))
        with patch.object(web_bridge.subprocess,"Popen") as popen:
            same=web_bridge.schedule_auto_native_stop(
                session_id="controller-1",repo=repo,receipt_id="rule-update:rev1",registry=registry,
                codex="codex",delay_seconds=1,state_path=state,
            )
        self.assertFalse(same)
        popen.assert_not_called()
        preserved=json.loads(state.read_text())
        self.assertEqual(preserved["state"],"WEB_REENTRY_RESULT_UNKNOWN")
        self.assertEqual(preserved["delivery_terminal_receipt_id"],"rule-update:rev1")


WebLocalReentryIntegrationTests.test_strong_host_confirmed_submit_waits_without_rearm = _strong_host_confirmed_submit_waits_without_rearm
WebContinuationSupervisorBootstrapTests.test_terminal_rule_delivery_blocks_bootstrap_across_fingerprint_changes = _terminal_rule_delivery_blocks_bootstrap_across_fingerprint_changes
WebAutoStopSupervisorCoalescingTests.test_same_terminal_receipt_cannot_be_rescheduled = _same_terminal_receipt_cannot_be_rescheduled


def _non_rule_delivery_key_uses_wake_generation_with_current_rule_snapshot(self):
    lifecycle = {
        "pending_control_event": True,
        "requires_user": False,
        "wake_generation": 22,
        "triggers": ["READY:F2", "main_worktree_changed"],
        "snapshot": {
            "head": "h2", "ledger_sha256": "l2", "worktree_status_sha256": "w2",
            "ready_ids": ["F2"], "runnable_ids": ["F2"], "candidate_revisions": [],
            "rule_handshake": {
                "installed_revision": "rev-current", "loaded_revision": "rev-current",
                "state": "current", "blocking": False,
            },
        },
    }
    self.assertEqual(web_bridge._lifecycle_delivery_key(lifecycle), "wake-generation:22")
    prior = {
        "state": "WAITING_FOR_CONTROLLER_PROGRESS",
        "receipt_id": "rule-update:rev-current",
        "delivery_terminal_receipt_id": "rule-update:rev-current",
        "delivery_terminal_key": "rule-update:rev-current",
        "delivery_terminal_outcome": "submit_confirmed",
    }
    self.assertTrue(web_bridge.continuation_supervisor_needs_bootstrap(lifecycle, prior))


def _transient_web_reentry_retry_budget_exhausts_without_rearm(self):
    from unittest.mock import patch
    with tempfile.TemporaryDirectory() as tmp:
        repo, registry, state_path = self.make_repo(Path(tmp))
        payload = json.loads(registry.read_text(encoding="utf-8"))
        payload["__controller_targets__"] = {"controller-1": {"web": {
            "status": "active", "session_id": "web-current", "generation": 4,
            "provenance": "host_attested_same_controller_recovery", "binding_mode": "resume_only",
            "identity_proof": "host_attested_origin",
        }}}
        payload["__controller_execution_ownership__"] = {"controller-1": {
            "active_host": "web", "execution_target_session_id": "web-current",
            "generation": 8, "provenance": "web_entry",
        }}
        registry.write_text(json.dumps(payload), encoding="utf-8")
        state_path.write_text(json.dumps({
            "receipt_id": "web-transient-budget", "session_id": "controller-1",
            "repo": str(repo.resolve()), "state": "WEB_REENTRY_PENDING",
            "pending_control_event": True,
            "retry_count": web_bridge.WEB_REENTRY_TRANSIENT_RETRY_LIMIT - 1,
        }), encoding="utf-8")
        lifecycle = {
            "pending_control_event": True, "requires_user": False, "controller_host": "web",
            "wake_generation": 23, "triggers": ["READY:F3"],
            "snapshot": {"head":"h3","ledger_sha256":"l3","worktree_status_sha256":"w3","ready_ids":["F3"],"runnable_ids":["F3"],"candidate_revisions":[]},
        }
        attempt = {
            "operation": "web_reentry", "result": "DEFERRED", "state": "WEB_REENTRY_PENDING",
            "returncode": 78, "failure_class": "web_reentry_unavailable",
            "error_code": "WEB_REENTRY_UNAVAILABLE", "stderr_tail": "temporary route gap",
            "execution_target_session_id": "web-current", "target_generation": 4,
            "ownership_generation": 8, "target_mode": "explicit_current",
            "delivery_authorization": "host_attested", "host_attested": True,
            "strong_web_identity_established": True,
        }
        def verifier(**_kwargs): return {"call_receipt": "host-call"}
        verifier.submit_reentry = lambda **_kwargs: None
        with patch.object(web_bridge, "_load_lifecycle_state", return_value=lifecycle), patch.object(
            web_bridge, "_registered_peer_attestation_verifier", return_value=verifier
        ), patch.object(web_bridge, "_execute_registered_web_host_reentry", return_value=attempt), patch.object(
            web_bridge, "schedule_auto_native_stop"
        ) as schedule:
            code = web_bridge.run_auto_native_stop(
                session_id="controller-1", repo=repo, receipt_id="web-transient-budget",
                registry=registry, codex="codex", delay_seconds=0, state_path=state_path,
            )
        saved = json.loads(state_path.read_text(encoding="utf-8"))
    self.assertEqual(code, 78)
    schedule.assert_not_called()
    self.assertEqual(saved["retry_count"], web_bridge.WEB_REENTRY_TRANSIENT_RETRY_LIMIT)
    self.assertEqual(saved["state"], "WEB_REENTRY_RETRY_EXHAUSTED")
    self.assertEqual(saved["failure_class"], "web_reentry_retry_exhausted")
    self.assertEqual(saved["delivery_terminal_outcome"], "retry_exhausted")
    self.assertEqual(saved["delivery_terminal_key"], "wake-generation:23")


WebContinuationSupervisorBootstrapTests.test_non_rule_delivery_key_uses_wake_generation_with_current_rule_snapshot = _non_rule_delivery_key_uses_wake_generation_with_current_rule_snapshot
WebLocalReentryIntegrationTests.test_transient_web_reentry_retry_budget_exhausts_without_rearm = _transient_web_reentry_retry_budget_exhausts_without_rearm
