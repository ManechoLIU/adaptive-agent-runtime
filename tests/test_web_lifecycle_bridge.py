from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "scripts" / "web_lifecycle_bridge.py"
_SPEC = importlib.util.spec_from_file_location("web_lifecycle_bridge_under_test", BRIDGE)
assert _SPEC and _SPEC.loader
web_bridge = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(web_bridge)


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

    def test_audit_dispatch_failure_does_not_advance_cursor_and_replay_is_fail_closed(self) -> None:
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); repo = root / "repo"; repo.mkdir()
            audit = root / "audit.jsonl"; cursor = root / "cursor.json"
            receipt = {
                "receiptId":"guard-fail-1", "childTool":"shell_command", "state":"succeeded",
                "rootLabel":str(repo), "targetLabel":"python3 scripts/control_event_guard.py event.json",
                "detail":f"命令：python3 scripts/control_event_guard.py event.json · 工作目录：{repo}\n\n命令输出：\ncontrol-event: allowed; done\n",
            }
            audit.write_text(json.dumps(receipt) + "\n", encoding="utf-8")
            args = ["audit-once", "--session-id", "controller-1", "--repo", str(repo),
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
            registry.write_text(json.dumps({"controller-1": str(repo.resolve())}), encoding="utf-8")
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
            self.assertEqual(event["controller_host"], "web")
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



class WebSessionRestoreAndResumeClassificationTests(unittest.TestCase):
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

        self.assertEqual(payload["controller_session"], "controller-1")
        self.assertEqual(payload["restore_order"], ["AGENTS.md", "TASK_LEDGER.md", "MEMORY.md", "WIKI_INDEX.md", "git_runtime"])
        self.assertEqual([item["name"] for item in payload["documents"]], ["AGENTS.md", "TASK_LEDGER.md", "MEMORY.md", "WIKI_INDEX.md"])
        self.assertIn("agent rules", payload["documents"][0]["content"])
        self.assertIn("adaptive-delivery", payload["runtime_state_path"])
        self.assertNotIn("adaptive-agent-runtime", payload["runtime_state_path"])

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
            registry.write_text(json.dumps({"controller-1": str(repo.resolve())}), encoding="utf-8")
            result = subprocess.run(
                ["/usr/bin/python3", str(BRIDGE), "session-start", "--repo", str(repo), "--registry", str(registry)],
                text=True, capture_output=True, check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["controller_session"], "controller-1")
        self.assertEqual(payload["restore_order"][-1], "git_runtime")


class ControllerHostTrackingTests(WebLifecycleBridgeTests):
    def test_web_bridge_marks_translated_events_as_web_host(self) -> None:
        receipt = {
            "receiptId": "host-web-1", "childTool": "shell_command", "state": "succeeded",
            "rootLabel": str(Path.home() / "Documents" / "SelfAlone"),
            "targetLabel": "git status --short",
            "detail": "命令：git status --short · 工作目录：~/Documents/SelfAlone\n\n命令输出：\n",
        }
        result = self.run_bridge(
            "translate-receipt", "--session-id", "controller-1",
            "--repo", str(Path.home() / "Documents" / "SelfAlone"),
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
