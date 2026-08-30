from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "scripts" / "web_lifecycle_bridge.py"


class WebLifecycleBridgeTests(unittest.TestCase):
    def run_bridge(self, *args: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["/usr/bin/python3", str(BRIDGE), *args],
            input=stdin,
            text=True,
            capture_output=True,
            check=False,
        )

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

        result = self.run_bridge(
            "translate-receipt",
            "--session-id",
            "controller-1",
            "--repo",
            str(Path.home() / "Documents" / "SelfAlone"),
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

        result = self.run_bridge(
            "translate-receipt",
            "--session-id",
            "controller-1",
            "--repo",
            str(Path.home() / "Documents" / "SelfAlone"),
            stdin=json.dumps(receipt),
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_post_shell_resolves_the_single_registered_controller_for_repo(self) -> None:
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
                "--cwd",
                str(repo),
                "--command",
                "git status --short",
                "--exit-code",
                "0",
                "--registry",
                str(registry),
                "--capture-event",
                str(capture),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            event = json.loads(capture.read_text(encoding="utf-8"))
            self.assertEqual(event["session_id"], "controller-1")
            self.assertEqual(event["cwd"], str(repo.resolve()))
            self.assertEqual(event["tool_response"]["exit_code"], 0)

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
            ["/usr/bin/python3", str(BRIDGE), *args],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_audit_once_captures_successful_control_guard_receipt_and_advances_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            audit = tmp_path / "audit.jsonl"
            receipt = {
                "receiptId": "guard-1",
                "childTool": "shell_command",
                "state": "succeeded",
                "rootLabel": str(repo),
                "targetLabel": "python3 scripts/control_event_guard.py event.json",
                "detail": (
                    "命令：python3 scripts/control_event_guard.py event.json"
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

    def test_audit_once_schedules_native_stop_after_allowed_guard_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = tmp_path / "controllers.json"
            registry.write_text(json.dumps({"controller-1": str(repo.resolve())}), encoding="utf-8")
            audit = tmp_path / "audit.jsonl"
            receipt = {
                "receiptId": "guard-auto-stop-1",
                "childTool": "shell_command",
                "state": "succeeded",
                "rootLabel": str(repo),
                "targetLabel": "python3 scripts/control_event_guard.py event.json --repo .",
                "detail": (
                    "命令：python3 scripts/control_event_guard.py event.json --repo ."
                    f" · 工作目录：{repo}\n\n命令输出：\n"
                    "control-event: allowed; declared decisions are complete\n"
                ),
            }
            audit.write_text(json.dumps(receipt) + "\n", encoding="utf-8")
            cursor = tmp_path / "cursor.json"
            capture = tmp_path / "auto-stop.json"

            result = self.run_bridge(
                "audit-once",
                "--session-id", "controller-1",
                "--repo", str(repo),
                "--audit-log", str(audit),
                "--cursor", str(cursor),
                "--registry", str(registry),
                "--auto-native-stop",
                "--auto-stop-delay-seconds", "5",
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

    def test_audit_once_ignores_computer_without_valid_lease(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
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
                "--audit-log", str(audit), "--cursor", str(cursor),
                "--computer-lease", str(lease), "--capture-events", str(capture),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(capture.exists())

    def test_audit_once_consumes_one_computer_event_from_valid_lease(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            audit = tmp_path / "audit.jsonl"
            audit.write_text(json.dumps({
                "receiptId":"computer-2", "childTool":"computer", "state":"succeeded",
                "targetLabel":"Google Chrome", "detail":"电脑操作：click · 应用 Google Chrome"
            }) + "\n", encoding="utf-8")
            cursor = tmp_path / "cursor.json"
            capture = tmp_path / "events.jsonl"
            lease = tmp_path / "lease.json"
            lease.write_text(json.dumps({
                "session_id":"controller-1", "repo":str(repo.resolve()),
                "expires_at_unix_ms":4102444800000, "remaining_uses":1
            }), encoding="utf-8")

            result = self.run_bridge(
                "audit-once", "--session-id", "controller-1", "--repo", str(repo),
                "--audit-log", str(audit), "--cursor", str(cursor),
                "--computer-lease", str(lease), "--capture-events", str(capture),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            event = json.loads(capture.read_text().splitlines()[0])
            self.assertEqual(event["tool_name"], "AI-Bridge.computer")
            self.assertIn("click", event["tool_input"]["detail"])
            self.assertFalse(lease.exists())

    def test_arm_computer_resolves_registered_controller_and_writes_bounded_lease(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = tmp_path / "controllers.json"
            registry.write_text(json.dumps({"controller-1":str(repo.resolve())}), encoding="utf-8")
            lease = tmp_path / "lease.json"

            result = self.run_bridge(
                "arm-computer", "--cwd", str(repo), "--registry", str(registry),
                "--lease", str(lease), "--ttl-seconds", "90", "--uses", "2",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            value=json.loads(lease.read_text())
            self.assertEqual(value["session_id"], "controller-1")
            self.assertEqual(value["remaining_uses"], 2)
            self.assertGreater(value["expires_at_unix_ms"], value["issued_at_unix_ms"])


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
                argv[:6],
                ["/opt/homebrew/bin/codex", "exec", "-C", str(repo.resolve()), "resume", "controller-1"],
            )
            self.assertNotIn("fork", argv)

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
        registry.write_text(json.dumps({"controller-1": str(repo.resolve())}), encoding="utf-8")
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

    def test_auto_native_stop_success_confirms_same_controller_resume(self) -> None:
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
            self.assertFalse(saved["pending_control_event"])
            self.assertIn("resume controller-1", marker.read_text())

    def test_detached_scheduler_does_not_discard_stderr_to_devnull(self) -> None:
        source = BRIDGE.read_text(encoding="utf-8")
        self.assertNotIn("stderr=subprocess.DEVNULL", source)
        self.assertNotIn("stdout=subprocess.DEVNULL", source)
        self.assertIn("rotate_launcher_log", source)
        self.assertIn(".launcher.log", source)
        self.assertIn("stderr_tail", source)
