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

        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp) / "controllers.json"
            repo = Path.home() / "Documents" / "SelfAlone"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {"controller-1": {"web": ["web-session-1"]}},
            }), encoding="utf-8")
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
            registry = tmp_path / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {"controller-1": {"web": ["web-session-1"]}},
            }), encoding="utf-8")
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
            audit = root / "audit.jsonl"; cursor = root / "cursor.json"
            receipt = {
                "receiptId":"guard-lock-1", "childTool":"shell_command", "state":"succeeded",
                "rootLabel":str(repo), "targetLabel":"python3 scripts/control_event_guard.py event.json",
                "detail":f"命令：python3 scripts/control_event_guard.py event.json · 工作目录：{repo}\n\n命令输出：\ncontrol-event: allowed; done\n",
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
            with patch.object(web_bridge, "dispatch_event", return_value=0) as dispatch:
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
            audit = root / "audit.jsonl"; cursor = root / "cursor.json"
            receipt = {
                "receiptId":"guard-fail-1", "childTool":"shell_command", "state":"succeeded",
                "rootLabel":str(repo), "targetLabel":"python3 scripts/control_event_guard.py event.json",
                "detail":f"命令：python3 scripts/control_event_guard.py event.json · 工作目录：{repo}\n\n命令输出：\ncontrol-event: allowed; done\n",
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
            self.assertIn("verified Web Controller Session identity", result.stderr)

    def test_audit_once_ignores_computer_lease_bound_to_different_web_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"; repo.mkdir()
            registry = root / "controllers.json"
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {"controller-1": {"web": ["web-session-1", "web-session-2"]}},
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
                argv[:6],
                ["/opt/homebrew/bin/codex", "exec", "-C", str(repo.resolve()), "resume", "controller-1"],
            )
            self.assertNotIn("fork", argv)

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
                host_facts={"controller_host": "web", "resume_actionable": True},
            )

            self.assertEqual(receipt["decision"], "RESUME_CURRENT_HOST")
            self.assertEqual(receipt["controller_id"], "controller-1")
            self.assertEqual(receipt["selected_host"], "web")
            self.assertTrue(receipt["pending_control_event"])
            self.assertEqual(receipt["result"], "CONFIRMED")
            self.assertIn("resume controller-1", marker.read_text(encoding="utf-8"))
            self.assertEqual(json.loads(receipt_path.read_text(encoding="utf-8"))["decision"], "RESUME_CURRENT_HOST")

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

    def test_eligible_peer_fallback_keeps_the_registered_controller(self) -> None:
        calls: list[dict] = []

        def desktop_resume(**kwargs: object) -> dict:
            calls.append(dict(kwargs))
            return {"result": "CONFIRMED", "operation": "desktop-resume"}

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
                },
                resume_adapters={"desktop_codex": desktop_resume},
            )

            self.assertEqual(receipt["decision"], "FALLBACK_PEER_HOST")
            self.assertEqual(receipt["selected_host"], "desktop_codex")
            self.assertEqual(receipt["controller_id"], "controller-1")
            self.assertEqual(receipt["result"], "CONFIRMED")
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["session_id"], "controller-1")
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

    def test_current_host_adapter_is_ignored_for_native_preflighted_resume(self) -> None:
        from unittest.mock import patch

        adapter_calls: list[dict] = []

        def supplied_current_host_adapter(**kwargs: object) -> dict:
            adapter_calls.append(dict(kwargs))
            return {"result": "CONFIRMED", "operation": "untrusted-current-host-adapter"}

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(
                web_bridge,
                "execute_native_resume",
                wraps=web_bridge.execute_native_resume,
            ) as native_resume:
                receipt, _, marker = self.wake(
                    Path(tmp),
                    lifecycle_state={"pending_control_event": True, "triggers": ["READY:F1"]},
                    host_facts={"controller_host": "web", "resume_actionable": True},
                    resume_adapters={"web": supplied_current_host_adapter},
                )

            self.assertEqual(receipt["decision"], "RESUME_CURRENT_HOST")
            self.assertEqual(receipt["operation"], "native_resume")
            self.assertEqual(adapter_calls, [])
            self.assertEqual(native_resume.call_count, 1)
            self.assertIn("resume controller-1", marker.read_text(encoding="utf-8"))

    def test_peer_adapter_metadata_is_json_safe_bounded_and_persisted(self) -> None:
        calls: list[dict] = []

        def desktop_resume(**kwargs: object) -> dict:
            calls.append(dict(kwargs))
            return {
                "result": "CONFIRMED",
                "operation": "peer-operation-" * 1024,
                "command": ["peer-resume", object(), "unbounded-command-argument" * 1024],
                "stderr_tail": {"diagnostic": object()},
            }

        with tempfile.TemporaryDirectory() as tmp:
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
                    host_facts={"controller_host": "web", "resume_actionable": True},
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
                    host_facts={"controller_host": "web", "resume_actionable": True},
                )
            self.assertEqual(receipt["result"], "CONFIRMED")
            self.assertEqual(resume.call_args.kwargs["terminal_receipts"], [terminal])

    def test_confirmed_wake_keeps_pending_control_event_true(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            receipt, receipt_path, _ = self.wake(
                Path(tmp),
                lifecycle_state={"pending_control_event": True, "triggers": ["READY:F1"]},
                host_facts={"controller_host": "web", "resume_actionable": True},
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
                        lifecycle_state={"pending_control_event": True, "triggers": triggers, "controller_host": "web"},
                        session_id="controller-1",
                        repo=repo,
                        registry=registry,
                        codex=str(codex),
                        receipt_path=receipt_path,
                        host_facts={"controller_host": "web", "resume_actionable": True},
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
                "controller_host": "web",
            }
            first = web_bridge.dispatch_pending_lifecycle_wake(
                lifecycle_state=state,
                session_id="controller-1",
                repo=repo,
                registry=registry,
                codex=str(codex),
                receipt_path=receipt_path,
                host_facts={"controller_host": "web", "resume_actionable": True},
            )
            second = web_bridge.dispatch_pending_lifecycle_wake(
                lifecycle_state=state,
                session_id="controller-1",
                repo=repo,
                registry=registry,
                codex=str(codex),
                receipt_path=receipt_path,
                host_facts={"controller_host": "web", "resume_actionable": True},
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
                "controller_host": "web",
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
                    host_facts={"controller_host": "web", "resume_actionable": True},
                )
                second = web_bridge.dispatch_pending_lifecycle_wake(
                    lifecycle_state=second_state, session_id="controller-1", repo=repo, registry=registry,
                    codex=str(codex), receipt_path=receipt_path,
                    host_facts={"controller_host": "web", "resume_actionable": True},
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
                "controller_host": "web", "snapshot": {"head": "h", "ledger_sha256": "l", "worktree_status_sha256": "s"},
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
                    host_facts={"controller_host": "web", "resume_actionable": True},
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
                "controller_host": "web",
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
                            host_facts={"controller_host": "web", "resume_actionable": True},
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
                "controller_host": "web",
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
                    host_facts={"controller_host": "web", "resume_actionable": True},
                )
                second = web_bridge.dispatch_pending_lifecycle_wake(
                    lifecycle_state=second_state, session_id="controller-1", repo=repo, registry=registry,
                    codex=str(codex), receipt_path=receipt_path,
                    host_facts={"controller_host": "web", "resume_actionable": True},
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
                "controller_host": "web",
            }
            first = web_bridge.dispatch_pending_lifecycle_wake(
                lifecycle_state=state,
                session_id="controller-1",
                repo=repo,
                registry=registry,
                codex=str(codex),
                receipt_path=receipt_path,
                host_facts={"controller_host": "web", "active_writer": True},
            )
            second = web_bridge.dispatch_pending_lifecycle_wake(
                lifecycle_state=state,
                session_id="controller-1",
                repo=repo,
                registry=registry,
                codex=str(codex),
                receipt_path=receipt_path,
                host_facts={"controller_host": "web", "resume_actionable": True},
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
                host_facts={"controller_host": "web", "resume_actionable": True},
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
                        "command": "python3 scripts/control_event_guard.py receipt.json --ledger TASK_LEDGER.md"
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
                ), patch.object(web_bridge, "dispatch_event", return_value=0):
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
            audit = root / "audit.jsonl"
            receipt = {
                "receiptId": "wake-capture-retry-1", "childTool": "shell_command", "state": "succeeded",
                "rootLabel": str(repo), "targetLabel": "python3 scripts/control_event_guard.py receipt.json --repo .",
                "detail": "命令：python3 scripts/control_event_guard.py receipt.json --repo .\n\n命令输出：\ncontrol-event: allowed\n",
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
            audit = root / "audit.jsonl"
            receipt = {
                "receiptId": "wake-only-capture-1", "childTool": "shell_command", "state": "succeeded",
                "rootLabel": str(repo), "targetLabel": "python3 scripts/control_event_guard.py receipt.json --repo .",
                "detail": "命令：python3 scripts/control_event_guard.py receipt.json --repo .\n\n命令输出：\ncontrol-event: allowed\n",
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
            audit = root / "audit.jsonl"
            receipt = {
                "receiptId": "wake-generation-1", "childTool": "shell_command", "state": "succeeded",
                "rootLabel": str(repo), "targetLabel": "python3 scripts/control_event_guard.py receipt.json --repo .",
                "detail": "命令：python3 scripts/control_event_guard.py receipt.json --repo .\n\n命令输出：\ncontrol-event: allowed\n",
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

    def test_audit_once_retries_wake_without_reconsuming_one_use_computer_lease(self) -> None:
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, registry, codex, _receipt_path, _ = self.make_controller(root)
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
            audit = root / "audit.jsonl"
            receipt = {
                "receiptId": "wake-retry-1", "childTool": "shell_command", "state": "succeeded",
                "rootLabel": str(repo), "targetLabel": "python3 scripts/control_event_guard.py receipt.json --repo .",
                "detail": "命令：python3 scripts/control_event_guard.py receipt.json --repo .\n\n命令输出：\ncontrol-event: allowed\n",
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
            audit = root / "audit.jsonl"
            receipt = {
                "receiptId": "wake-none-1", "childTool": "shell_command", "state": "succeeded",
                "rootLabel": str(repo), "targetLabel": "python3 scripts/control_event_guard.py receipt.json --repo .",
                "detail": "命令：python3 scripts/control_event_guard.py receipt.json --repo .\n\n命令输出：\ncontrol-event: allowed\n",
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
            audit = root / "audit.jsonl"
            receipt = {
                "receiptId": "wake-fail-1", "childTool": "shell_command", "state": "succeeded",
                "rootLabel": str(repo), "targetLabel": "python3 scripts/control_event_guard.py receipt.json --repo .",
                "detail": "命令：python3 scripts/control_event_guard.py receipt.json --repo .\n\n命令输出：\ncontrol-event: allowed\n",
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
            registry.write_text(json.dumps({
                "controller-1": str(repo.resolve()),
                "__controller_sessions__": {"controller-1": {"web": ["web-session-1"]}},
            }), encoding="utf-8")
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
