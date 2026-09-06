from __future__ import annotations

import hashlib
import json
import importlib.util
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(name, SKILL_ROOT / relative_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {relative_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


init_project = load_module("init_project", "scripts/init_project.py")
lint_governance = load_module("lint_governance", "scripts/lint_governance.py")
preblock_guard = load_module("preblock_guard", "scripts/preblock_guard.py")
control_event_guard = load_module(
    "control_event_guard", "scripts/control_event_guard.py"
)
event_scope_guard = load_module("event_scope_guard", "scripts/event_scope_guard.py")
assignment_lease_guard = load_module(
    "assignment_lease_guard", "scripts/assignment_lease_guard.py"
)
ledger_consistency_guard = load_module(
    "ledger_consistency_guard", "scripts/ledger_consistency_guard.py"
)
lifecycle_hook = load_module("lifecycle_hook", "scripts/lifecycle_hook.py")


class GovernanceTests(unittest.TestCase):
    def complete_event_receipt(self) -> dict[str, object]:
        return {
            "event_contract": {
                "event_id": "control-event-1",
                "event_type": "dispatch",
                "primary_task": "CONTROL-WAVE-1",
                "candidate_revision": "ledger-abc123",
                "allowed_actions": ["ledger_sync"],
                "allowed_files": [],
                "terminal_receipt": "control event synchronized",
            },
            "event_actions": [
                {
                    "action": "ledger_sync",
                    "primary_task": "CONTROL-WAVE-1",
                    "candidate_revision": "ledger-abc123",
                    "files": [],
                    "required_to_close_current_state": True,
                }
            ],
            "terminal_receipt_issued": True,
        }

    def route_policy_source(self) -> dict[str, str]:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        policy = Path(directory.name) / "AGENTS.md"
        policy.write_text(
            "后端默认 provider=grok-build、model=grok-4.6、auth_mode=oauth。\n"
            "前端默认 provider=kimi-code、model=kimi-k3、auth_mode=api。\n",
            encoding="utf-8",
        )
        return {
            "path": str(policy),
            "sha256": hashlib.sha256(policy.read_bytes()).hexdigest(),
        }

    def delegated_assignment(
        self,
        task_id: str,
        integration_flow: str,
        *,
        policy_class: str = "backend",
    ) -> dict[str, object]:
        return {
            "task_id": task_id,
            "integration_flow": integration_flow,
            "execution_mode": "delegated",
            "owned_files": [f"owned/{task_id}.ts"],
            "route": {
                "decision": "default",
                "policy_class": policy_class,
                "provider": "grok-build" if policy_class == "backend" else "kimi-code",
                "model": "grok-4.6" if policy_class == "backend" else "kimi-k3",
                "auth_mode": "oauth" if policy_class == "backend" else "api",
                "policy_source": self.route_policy_source(),
            },
        }

    def test_lifecycle_hook_surfaces_ready_work_after_tool_use(self) -> None:
        snapshot = {
            "head": "abc123",
            "ledger_sha256": "ledger-1",
            "worktree_status_sha256": "status-1",
            "ready_ids": ["WEB-READY", "MINI-READY"],
        }

        output, next_state = lifecycle_hook.evaluate_event(
            {
                "hook_event_name": "PostToolUse",
                "session_id": "session-1",
                "turn_id": "turn-1",
                "tool_name": "Bash",
                "tool_input": {"command": "git status --short"},
                "tool_response": {"output": ""},
            },
            snapshot=snapshot,
            prior_state=None,
        )

        self.assertEqual(
            output["hookSpecificOutput"]["hookEventName"], "PostToolUse"
        )
        self.assertIn("WEB-READY", output["hookSpecificOutput"]["additionalContext"])
        self.assertIn("MINI-READY", output["hookSpecificOutput"]["additionalContext"])
        self.assertTrue(next_state["pending_control_event"])

    def test_project_snapshot_joins_runtime_liveness_for_active_ledger_task(self) -> None:
        import json
        import subprocess
        from datetime import datetime, timedelta, timezone
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            subprocess.run(["git", "-C", str(root), "init", "-b", "main"], check=True, capture_output=True)
            (root/".git"/"adaptive-delivery").mkdir(parents=True)
            (root/"TASK_LEDGER.md").write_text("| ID | 状态 | 负责人 | 下一步 |\n|---|---|---|---|\n| `T1` | `ACTIVE` | grok | y |\n",encoding="utf-8")
            now=datetime.now(timezone.utc)
            lease={"assignment_id":"a1","task_id":"T1","agent_id":"grok","provider":"grok","session_id":"s1","worktree":"/tmp/wt","lease_expires_at":(now-timedelta(minutes=1)).isoformat(),"progress_deadline_at":(now+timedelta(minutes=10)).isoformat(),"terminal_state":None}
            (root/".git"/"adaptive-delivery"/"runtime-assignments.json").write_text(json.dumps({"schema_version":1,"leases":{"a1":lease}}))
            def fake_git(_root,*args):
                if args==("rev-parse","--show-toplevel"): return str(root)
                if args==("rev-parse","--git-common-dir"): return str(root / ".git")
                if args==("worktree","list","--porcelain"): return f"worktree {root}\nHEAD abc\nbranch refs/heads/main\n"
                if args==("branch","--show-current"): return "main"
                if args==("status","--porcelain=v1","--untracked-files=no"): return ""
                if args==("rev-parse","HEAD"): return "abc"
                raise AssertionError(args)
            with patch.object(lifecycle_hook,"run_git",side_effect=fake_git), patch("control_event_guard.unmerged_worktree_candidates",return_value={}):
                snap=lifecycle_hook.project_snapshot(root)
            self.assertEqual(snap["assignment_liveness"]["T1"]["state"],"unhealthy")
            self.assertEqual(snap["assignment_liveness"]["T1"]["reason"],"lease_expired")

    def lifecycle_worktree_fixture(self) -> tuple[Path, Path, Path, Path]:
        import subprocess

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        main = Path(directory.name) / "main"
        controller_worktree = Path(directory.name) / "controller-worktree"
        writer_worktree = Path(directory.name) / "writer-worktree"
        registry = Path(directory.name) / "controllers.json"
        main.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=main, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "tests@example.com"], cwd=main, check=True)
        subprocess.run(["git", "config", "user.name", "Tests"], cwd=main, check=True)
        (main / "TASK_LEDGER.md").write_text(
            "| ID | 状态 | 负责人 | 下一步 |\n|---|---|---|---|\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "add", "TASK_LEDGER.md"], cwd=main, check=True)
        subprocess.run(["git", "commit", "-m", "base"], cwd=main, check=True, capture_output=True)
        for branch, worktree in (
            ("controller-surface", controller_worktree),
            ("writer-surface", writer_worktree),
        ):
            subprocess.run(
                ["git", "worktree", "add", "-b", branch, str(worktree)],
                cwd=main,
                check=True,
                capture_output=True,
            )
        return main, controller_worktree, writer_worktree, registry

    def test_lifecycle_unbound_writer_worktree_rejects_registered_session(self) -> None:
        from unittest.mock import patch

        main, _controller_worktree, writer_worktree, registry = self.lifecycle_worktree_fixture()
        with patch.object(lifecycle_hook, "REGISTRY_PATH", registry):
            lifecycle_hook.register_controller("controller-1", main)
            self.assertFalse(
                lifecycle_hook.controller_event_is_managed(
                    {"session_id": "controller-1", "controller_host": "web"},
                    writer_worktree,
                    main,
                )
            )

    def test_lifecycle_registering_linked_worktree_binds_only_that_surface(self) -> None:
        from unittest.mock import patch

        main, controller_worktree, writer_worktree, registry = self.lifecycle_worktree_fixture()
        with patch.object(lifecycle_hook, "REGISTRY_PATH", registry):
            lifecycle_hook.register_controller("controller-1", controller_worktree)
            main_snapshot = lifecycle_hook.project_snapshot(main)
            controller_snapshot = lifecycle_hook.project_snapshot(controller_worktree)

            self.assertIsNotNone(main_snapshot)
            self.assertIsNotNone(controller_snapshot)
            self.assertEqual(main_snapshot["git_common_dir"], controller_snapshot["git_common_dir"])
            self.assertEqual(lifecycle_hook.registered_root("controller-1"), main.resolve())
            self.assertTrue(
                lifecycle_hook.controller_event_is_managed(
                    {"session_id": "controller-1", "controller_host": "web"},
                    controller_worktree,
                    main,
                )
            )
            self.assertFalse(
                lifecycle_hook.controller_event_is_managed(
                    {"session_id": "controller-1", "controller_host": "web"},
                    writer_worktree,
                    main,
                )
            )
            self.assertFalse(
                lifecycle_hook.controller_event_is_managed(
                    {"session_id": "controller-1", "controller_host": "web"},
                    main,
                    main,
                )
            )

    def test_lifecycle_controller_registration_uses_shared_registry_lock(self) -> None:
        from unittest.mock import patch

        main, _controller_worktree, _writer_worktree, registry = self.lifecycle_worktree_fixture()
        with patch.object(lifecycle_hook, "REGISTRY_PATH", registry):
            lifecycle_hook.register_controller("controller-1", main)

        self.assertTrue(registry.with_suffix(registry.suffix + ".lock").exists())

    def test_lifecycle_controller_registration_preserves_existing_web_session_bindings(self) -> None:
        from unittest.mock import patch

        main, _controller_worktree, _writer_worktree, registry = self.lifecycle_worktree_fixture()
        registry.write_text(json.dumps({
            "__controller_sessions__": {"controller-1": {"web": ["web-session-1"]}}
        }), encoding="utf-8")
        with patch.object(lifecycle_hook, "REGISTRY_PATH", registry):
            lifecycle_hook.register_controller("controller-1", main)

        saved = lifecycle_hook.load_json(registry)
        self.assertEqual(saved["__controller_sessions__"]["controller-1"]["web"], ["web-session-1"])
        self.assertEqual(saved["controller-1"], str(main.resolve()))

    def test_lifecycle_binds_desktop_entry_to_registered_controller_without_creating_another_controller(self) -> None:
        from unittest.mock import patch

        main, _controller_worktree, _writer_worktree, registry = self.lifecycle_worktree_fixture()
        registry.write_text(json.dumps({
            "__controller_sessions__": {"controller-1": {"web": ["web-session-1"]}}
        }), encoding="utf-8")
        with patch.object(lifecycle_hook, "REGISTRY_PATH", registry):
            lifecycle_hook.register_controller("controller-1", main)
            lifecycle_hook.bind_desktop_session(
                controller_id="controller-1",
                desktop_session_id="desktop-entry-2",
                repo=main,
            )
            lifecycle_hook.bind_desktop_session(
                controller_id="controller-1",
                desktop_session_id="desktop-entry-2",
                repo=main,
            )

            self.assertIsNone(
                lifecycle_hook.registered_controller_id("desktop-entry-2")
            )
            self.assertIsNone(lifecycle_hook.registered_root("desktop-entry-2"))

        saved = lifecycle_hook.load_json(registry)
        self.assertEqual(saved["__controller_sessions__"]["controller-1"]["web"], ["web-session-1"])
        self.assertEqual(
            saved["__controller_sessions__"]["controller-1"]["desktop_codex"],
            ["desktop-entry-2"],
        )
        self.assertNotIn("desktop-entry-2", {
            key for key, value in saved.items() if isinstance(value, str)
        })

    def test_desktop_aliases_are_inert_until_one_is_explicitly_replaced_as_current(self) -> None:
        from unittest.mock import patch

        main, _controller_worktree, _writer_worktree, registry = self.lifecycle_worktree_fixture()
        with patch.object(lifecycle_hook, "REGISTRY_PATH", registry):
            lifecycle_hook.register_controller("controller-1", main)
            lifecycle_hook.bind_desktop_session(
                controller_id="controller-1",
                desktop_session_id="desktop-entry-2",
                repo=main,
            )

            self.assertIsNone(lifecycle_hook.registered_controller_id("controller-1"))
            self.assertIsNone(lifecycle_hook.registered_controller_id("desktop-entry-2"))

            receipt = lifecycle_hook.replace_desktop_session(
                controller_id="controller-1",
                desktop_session_id="desktop-entry-2",
                repo=main,
                expected_generation=0,
            )

            self.assertIsNone(lifecycle_hook.registered_controller_id("controller-1"))
            self.assertEqual(
                lifecycle_hook.registered_controller_id("desktop-entry-2"),
                "controller-1",
            )

        self.assertEqual(receipt["status"], "active")
        self.assertEqual(receipt["generation"], 1)
        saved = lifecycle_hook.load_json(registry)
        self.assertEqual(
            saved["__controller_sessions__"]["controller-1"]["desktop_codex"],
            ["desktop-entry-2"],
        )
        self.assertEqual(
            saved["__controller_targets__"]["controller-1"]["desktop_codex"],
            {
                "status": "active",
                "session_id": "desktop-entry-2",
                "generation": 1,
            },
        )

    def test_replacing_desktop_target_deactivates_old_alias_and_advances_generation(self) -> None:
        from unittest.mock import patch

        main, _controller_worktree, _writer_worktree, registry = self.lifecycle_worktree_fixture()
        with patch.object(lifecycle_hook, "REGISTRY_PATH", registry):
            lifecycle_hook.register_controller("controller-1", main)
            lifecycle_hook.replace_desktop_session(
                controller_id="controller-1",
                desktop_session_id="desktop-entry-old",
                repo=main,
                expected_generation=0,
            )
            lifecycle_hook.bind_desktop_session(
                controller_id="controller-1",
                desktop_session_id="desktop-entry-current",
                repo=main,
            )
            receipt = lifecycle_hook.replace_desktop_session(
                controller_id="controller-1",
                desktop_session_id="desktop-entry-current",
                repo=main,
                expected_generation=1,
            )

            self.assertIsNone(lifecycle_hook.registered_controller_id("controller-1"))
            self.assertIsNone(lifecycle_hook.registered_controller_id("desktop-entry-old"))
            self.assertEqual(
                lifecycle_hook.registered_controller_id("desktop-entry-current"),
                "controller-1",
            )

        self.assertEqual(receipt["generation"], 2)
        saved = lifecycle_hook.load_json(registry)
        self.assertEqual(
            saved["__controller_sessions__"]["controller-1"]["desktop_codex"],
            ["desktop-entry-old", "desktop-entry-current"],
        )

    def test_replace_and_unbind_fail_closed_when_generation_is_stale(self) -> None:
        from unittest.mock import patch

        main, _controller_worktree, _writer_worktree, registry = self.lifecycle_worktree_fixture()
        with patch.object(lifecycle_hook, "REGISTRY_PATH", registry):
            lifecycle_hook.register_controller("controller-1", main)
            lifecycle_hook.replace_desktop_session(
                controller_id="controller-1",
                desktop_session_id="desktop-current",
                repo=main,
                expected_generation=0,
            )

            with self.assertRaisesRegex(PermissionError, "expected_generation"):
                lifecycle_hook.replace_desktop_session(
                    controller_id="controller-1",
                    desktop_session_id="desktop-next",
                    repo=main,
                    expected_generation=0,
                )
            with self.assertRaisesRegex(PermissionError, "expected_generation"):
                lifecycle_hook.unbind_desktop_session(
                    controller_id="controller-1",
                    desktop_session_id="desktop-current",
                    repo=main,
                    expected_generation=0,
                )

        saved = lifecycle_hook.load_json(registry)
        self.assertEqual(
            saved["__controller_targets__"]["controller-1"]["desktop_codex"],
            {"status": "active", "session_id": "desktop-current", "generation": 1},
        )

    def test_unbinding_current_desktop_target_does_not_reactivate_canonical_session(self) -> None:
        from unittest.mock import patch

        main, _controller_worktree, _writer_worktree, registry = self.lifecycle_worktree_fixture()
        with patch.object(lifecycle_hook, "REGISTRY_PATH", registry):
            lifecycle_hook.register_controller("controller-1", main)
            lifecycle_hook.replace_desktop_session(
                controller_id="controller-1",
                desktop_session_id="desktop-entry-current",
                repo=main,
                expected_generation=0,
            )
            receipt = lifecycle_hook.unbind_desktop_session(
                controller_id="controller-1",
                desktop_session_id="desktop-entry-current",
                repo=main,
                expected_generation=1,
            )

            self.assertIsNone(lifecycle_hook.registered_controller_id("controller-1"))
            self.assertIsNone(lifecycle_hook.registered_controller_id("desktop-entry-current"))

        self.assertEqual(receipt["status"], "unbound")
        self.assertEqual(receipt["generation"], 2)
        saved = lifecycle_hook.load_json(registry)
        self.assertEqual(
            saved["__controller_targets__"]["controller-1"]["desktop_codex"],
            {"status": "unbound", "session_id": None, "generation": 2},
        )

    def test_lifecycle_refuses_desktop_entry_owned_by_another_controller(self) -> None:
        from unittest.mock import patch

        first, _controller_worktree, _writer_worktree, registry = self.lifecycle_worktree_fixture()
        with tempfile.TemporaryDirectory() as directory:
            second = Path(directory) / "second"
            second.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=second, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "tests@example.com"], cwd=second, check=True)
            subprocess.run(["git", "config", "user.name", "Tests"], cwd=second, check=True)
            (second / "TASK_LEDGER.md").write_text(
                "| ID | 状态 | 负责人 | 下一步 |\n|---|---|---|---|\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "add", "TASK_LEDGER.md"], cwd=second, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=second, check=True, capture_output=True)

            with patch.object(lifecycle_hook, "REGISTRY_PATH", registry):
                lifecycle_hook.register_controller("controller-1", first)
                lifecycle_hook.register_controller("controller-2", second)
                lifecycle_hook.bind_desktop_session(
                    controller_id="controller-1",
                    desktop_session_id="desktop-shared",
                    repo=first,
                )

                with self.assertRaisesRegex(PermissionError, "another Controller"):
                    lifecycle_hook.bind_desktop_session(
                        controller_id="controller-2",
                        desktop_session_id="desktop-shared",
                        repo=second,
                    )

    def test_lifecycle_refuses_promoting_bound_desktop_entry_to_second_controller(self) -> None:
        from unittest.mock import patch

        first, _controller_worktree, _writer_worktree, registry = self.lifecycle_worktree_fixture()
        with tempfile.TemporaryDirectory() as directory:
            second = Path(directory) / "second"
            second.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=second, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "tests@example.com"], cwd=second, check=True)
            subprocess.run(["git", "config", "user.name", "Tests"], cwd=second, check=True)
            (second / "TASK_LEDGER.md").write_text(
                "| ID | 状态 | 负责人 | 下一步 |\n|---|---|---|---|\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "add", "TASK_LEDGER.md"], cwd=second, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=second, check=True, capture_output=True)

            with patch.object(lifecycle_hook, "REGISTRY_PATH", registry):
                lifecycle_hook.register_controller("controller-1", first)
                lifecycle_hook.bind_desktop_session(
                    controller_id="controller-1",
                    desktop_session_id="desktop-shared",
                    repo=first,
                )

                with self.assertRaisesRegex(ValueError, "already bound"):
                    lifecycle_hook.register_controller("desktop-shared", second)

    def test_replace_refuses_desktop_target_active_for_another_controller_even_if_alias_index_is_missing(self) -> None:
        from unittest.mock import patch

        first, _controller_worktree, _writer_worktree, registry = self.lifecycle_worktree_fixture()
        with tempfile.TemporaryDirectory() as directory:
            second = Path(directory) / "second"
            second.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=second, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "tests@example.com"], cwd=second, check=True)
            subprocess.run(["git", "config", "user.name", "Tests"], cwd=second, check=True)
            (second / "TASK_LEDGER.md").write_text(
                "| ID | 状态 | 负责人 | 下一步 |\n|---|---|---|---|\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "add", "TASK_LEDGER.md"], cwd=second, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=second, check=True, capture_output=True)

            with patch.object(lifecycle_hook, "REGISTRY_PATH", registry):
                lifecycle_hook.register_controller("controller-1", first)
                lifecycle_hook.register_controller("controller-2", second)
                value = lifecycle_hook.load_json(registry)
                value["__controller_targets__"] = {
                    "controller-2": {
                        "desktop_codex": {
                            "status": "active",
                            "session_id": "desktop-shared",
                            "generation": 1,
                        }
                    }
                }
                lifecycle_hook.write_json(registry, value)

                with self.assertRaisesRegex(PermissionError, "another Controller"):
                    lifecycle_hook.replace_desktop_session(
                        controller_id="controller-1",
                        desktop_session_id="desktop-shared",
                        repo=first,
                        expected_generation=0,
                    )

    def test_lifecycle_desktop_entry_uses_canonical_controller_state(self) -> None:
        from unittest.mock import patch

        main, _controller_worktree, _writer_worktree, registry = self.lifecycle_worktree_fixture()
        state_root = main.parent / "lifecycle-state"
        with patch.object(lifecycle_hook, "REGISTRY_PATH", registry), patch.object(
            lifecycle_hook, "STATE_ROOT", state_root
        ):
            lifecycle_hook.register_controller("controller-1", main)
            lifecycle_hook.replace_desktop_session(
                controller_id="controller-1",
                desktop_session_id="desktop-entry-2",
                repo=main,
                expected_generation=0,
            )
            event = {
                "hook_event_name": "SessionStart",
                "session_id": "desktop-entry-2",
                "cwd": str(main),
            }
            with patch("sys.stdin", io.StringIO(json.dumps(event))), patch(
                "sys.stdout", new_callable=io.StringIO
            ):
                self.assertEqual(lifecycle_hook.run_hook(), 0)
            controller_state = lifecycle_hook.state_path("controller-1")
            alias_state = lifecycle_hook.state_path("desktop-entry-2")

        self.assertTrue(controller_state.is_file())
        self.assertFalse(alias_state.exists())
        saved = lifecycle_hook.load_json(controller_state)
        self.assertEqual(saved["session_id"], "controller-1")
        self.assertEqual(saved["source_session_id"], "desktop-entry-2")
        self.assertEqual(saved["controller_host"], "desktop_codex")

    def test_lifecycle_pretool_rejects_message_or_navigation_to_retired_task(self) -> None:
        from unittest.mock import patch

        main, _controller_worktree, _writer_worktree, registry = self.lifecycle_worktree_fixture()
        state_root = main.parent / "lifecycle-state"
        with patch.object(lifecycle_hook, "REGISTRY_PATH", registry), patch.object(
            lifecycle_hook, "STATE_ROOT", state_root
        ):
            lifecycle_hook.register_controller("controller-old", main)
            lifecycle_hook.replace_desktop_session(
                controller_id="controller-old",
                desktop_session_id="desktop-current",
                repo=main,
                expected_generation=0,
            )
            for tool_name, target_key in (
                ("mcp__codex_app__send_message_to_thread", "threadId"),
                ("mcp__codex_app__navigate_to_codex_page", "threadId"),
            ):
                event = {
                    "hook_event_name": "PreToolUse",
                    "session_id": "desktop-current",
                    "turn_id": "turn-1",
                    "tool_use_id": f"tool-{tool_name}",
                    "tool_name": tool_name,
                    "tool_input": {target_key: "controller-old"},
                    "cwd": str(main),
                }
                with self.subTest(tool_name=tool_name), patch(
                    "sys.stdin", io.StringIO(json.dumps(event))
                ), patch("sys.stdout", new_callable=io.StringIO) as output:
                    self.assertEqual(lifecycle_hook.run_hook(), 0)
                    denial = json.loads(output.getvalue())
                    self.assertEqual(
                        denial["hookSpecificOutput"]["permissionDecision"], "deny"
                    )
                    self.assertIn(
                        "current desktop_codex target desktop-current",
                        denial["hookSpecificOutput"]["permissionDecisionReason"],
                    )

            missing_target = {
                "hook_event_name": "PreToolUse",
                "session_id": "desktop-current",
                "turn_id": "turn-1",
                "tool_use_id": "tool-missing-target",
                "tool_name": "mcp__codex_app__send_message_to_thread",
                "tool_input": {},
                "cwd": str(main),
            }
            with patch("sys.stdin", io.StringIO(json.dumps(missing_target))), patch(
                "sys.stdout", new_callable=io.StringIO
            ) as output:
                self.assertEqual(lifecycle_hook.run_hook(), 0)
                denial = json.loads(output.getvalue())
                self.assertIn(
                    "explicit thread target",
                    denial["hookSpecificOutput"]["permissionDecisionReason"],
                )

            current_target = {
                "hook_event_name": "PreToolUse",
                "session_id": "desktop-current",
                "turn_id": "turn-1",
                "tool_use_id": "tool-current-target",
                "tool_name": "mcp__codex_app__send_message_to_thread",
                "tool_input": {"threadId": "desktop-current"},
                "cwd": str(main),
            }
            with patch("sys.stdin", io.StringIO(json.dumps(current_target))), patch(
                "sys.stdout", new_callable=io.StringIO
            ) as output:
                self.assertEqual(lifecycle_hook.run_hook(), 0)
                self.assertEqual(output.getvalue(), "")
            saved = lifecycle_hook.load_json(lifecycle_hook.state_path("controller-old"))
            self.assertIn("tool-current-target", saved["inflight_tool_use_ids"])

    def test_lifecycle_bind_desktop_session_cli_reports_canonical_controller(self) -> None:
        from unittest.mock import patch

        main, _controller_worktree, _writer_worktree, registry = self.lifecycle_worktree_fixture()
        with patch.object(lifecycle_hook, "REGISTRY_PATH", registry):
            lifecycle_hook.register_controller("controller-1", main)
            with patch("sys.stdout", new_callable=io.StringIO) as output:
                result = lifecycle_hook.main([
                    "--bind-desktop-session",
                    "controller-1",
                    "desktop-entry-2",
                    str(main),
                ])

        self.assertEqual(result, 0)
        self.assertEqual(
            json.loads(output.getvalue()),
            {
                "controller_id": "controller-1",
                "controller_session_id": "controller-1",
                "desktop_session_id": "desktop-entry-2",
                "event_source": "desktop_codex",
            },
        )

    def test_lifecycle_serializes_state_updates_from_multiple_desktop_entries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "controller.json"
            helper = root / "update_state.py"
            helper.write_text(
                """
import importlib.util
import json
import sys
import time
from pathlib import Path

skill_root = Path(sys.argv[1])
sys.path.insert(0, str(skill_root / "scripts"))
spec = importlib.util.spec_from_file_location("isolated_lifecycle_hook", skill_root / "scripts" / "lifecycle_hook.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

def slow_evaluate(event, *, snapshot, prior_state):
    state = dict(prior_state or {})
    triggers = list(state.get("triggers", []))
    time.sleep(0.25)
    triggers.append(event["trigger"])
    state["triggers"] = sorted(set(triggers))
    return {}, state

module.evaluate_event = slow_evaluate
module.persist_event_state(Path(sys.argv[2]), {"trigger": sys.argv[3]}, {})
""".strip()
                + "\n",
                encoding="utf-8",
            )
            processes = [
                subprocess.Popen(
                    [sys.executable, str(helper), str(SKILL_ROOT), str(state), trigger],
                    cwd=SKILL_ROOT,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for trigger in ("entry-a", "entry-b")
            ]
            results = [process.communicate(timeout=10) for process in processes]

            self.assertEqual([process.returncode for process in processes], [0, 0], results)
            self.assertEqual(
                json.loads(state.read_text(encoding="utf-8"))["triggers"],
                ["entry-a", "entry-b"],
            )

    def test_lifecycle_rejects_second_controller_session_for_canonical_project(self) -> None:
        from unittest.mock import patch

        main, _controller_worktree, _writer_worktree, registry = self.lifecycle_worktree_fixture()
        with patch.object(lifecycle_hook, "REGISTRY_PATH", registry):
            lifecycle_hook.register_controller("controller-1", main)
            original_registry = lifecycle_hook.load_json(registry)

            with self.assertRaises(ValueError):
                lifecycle_hook.register_controller("controller-2", main)

            self.assertEqual(lifecycle_hook.load_json(registry), original_registry)
            self.assertEqual(lifecycle_hook.registered_root("controller-1"), main.resolve())
            self.assertIsNone(lifecycle_hook.registered_root("controller-2"))

    def test_project_snapshot_exposes_five_state_task_projection(self) -> None:
        import subprocess
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            subprocess.run(["git", "-C", str(root), "init", "-b", "main"], check=True, capture_output=True)
            (root / ".git" / "adaptive-delivery").mkdir(parents=True)
            (root / "TASK_LEDGER.md").write_text(
                """# Ledger

- 当前 Goal：`T3` active work
- 下一可见检查点：`T3` checkpoint
- 当前阻塞：none
- 规则版本：test

| ID | 状态 | 负责人 | 下一步 |
|---|---|---|---|
| `T1` | `PENDING` | 待分配 | wait |
| `T2` | `READY` | 待分配 | dispatch |
| `T3` | `ACTIVE` | Agent A | execute |
| `T4` | `RECOVERING` | Agent B Assignment T4-RECOVERY-01 delivered ACK | 恢复动作：继续 checkpoint 测试 PASS |
| `T5` | `VERIFY` | reviewer | review |
| `T6` | `BLOCKED` | controller | external |
| `T7` | `DONE` | controller | none |
| `T8` | `SUPERSEDED` | controller | none |
""",
                encoding="utf-8",
            )
            def fake_git(_root, *args):
                if args == ("rev-parse", "--show-toplevel"): return str(root)
                if args == ("rev-parse", "--git-common-dir"): return str(root / ".git")
                if args == ("worktree", "list", "--porcelain"): return f"worktree {root}\nHEAD abc\nbranch refs/heads/main\n"
                if args == ("branch", "--show-current"): return "main"
                if args == ("status", "--porcelain=v1", "--untracked-files=no"): return ""
                if args == ("rev-parse", "HEAD"): return "abc"
                raise AssertionError(args)
            with patch.object(lifecycle_hook, "run_git", side_effect=fake_git), patch(
                "control_event_guard.unmerged_worktree_candidates", return_value={}
            ):
                snap = lifecycle_hook.project_snapshot(root)
            projection = snap["task_projection"]
            self.assertEqual(projection["T1"]["main_state"], "READY")
            self.assertFalse(projection["T1"]["dispatchable"])
            self.assertEqual(projection["T2"]["main_state"], "READY")
            self.assertTrue(projection["T2"]["dispatchable"])
            self.assertEqual((projection["T3"]["main_state"], projection["T3"]["health"]), ("ACTIVE", "recovering"))
            self.assertEqual((projection["T4"]["main_state"], projection["T4"]["health"]), ("ACTIVE", "recovering"))
            self.assertEqual(projection["T5"]["main_state"], "VERIFY")
            self.assertEqual(projection["T6"]["main_state"], "BLOCKED")
            self.assertEqual((projection["T7"]["main_state"], projection["T7"]["closure_reason"]), ("CLOSED", "done"))
            self.assertEqual((projection["T8"]["main_state"], projection["T8"]["closure_reason"]), ("CLOSED", "superseded"))

    def test_lifecycle_rule_update_is_persistent_and_exact_until_ack_and_ledger_sync(self) -> None:
        base = {
            "head": "abc", "ledger_sha256": "l1", "worktree_status_sha256": "s1",
            "ready_ids": [], "candidate_revisions": [], "ledger_errors": [], "assignment_liveness": {},
        }
        pending = {**base, "rule_handshake": {
            "state": "pending_ack", "blocking": True, "installed_revision": "rev-new",
            "summary": "canonical runtime", "impact": "live_assignments",
            "stop_condition": "ACK and ledger sync", "changed_files": ["scripts/assignment_runtime.py"],
        }}
        first, state = lifecycle_hook.evaluate_event(
            {"hook_event_name": "SessionStart", "session_id": "controller-1"}, snapshot=pending, prior_state=None
        )
        self.assertIn("rule_update_pending:rev-new", state["triggers"])
        context = first["hookSpecificOutput"]["additionalContext"]
        self.assertIn("rev-new", context)
        self.assertIn("canonical runtime", context)
        self.assertIn("rule_handshake.py", context)
        self.assertIn('" ack', context)
        expected_handshake = Path(lifecycle_hook.__file__).resolve().parent / "rule_handshake.py"
        self.assertIn(str(expected_handshake), context)
        self.assertNotIn("~/.agents/skills/adaptive-delivery", context)

        receipt_event = {
            "hook_event_name": "PostToolUse", "session_id": "controller-1",
            "tool_input": {"command": "python3 scripts/control_event_guard.py snapshot --ledger TASK_LEDGER.md"},
            "tool_response": {"exit_code": 0, "output": "control-event: allowed"},
        }
        _, still_pending = lifecycle_hook.evaluate_event(receipt_event, snapshot=pending, prior_state=state)
        self.assertTrue(still_pending["pending_control_event"])
        self.assertIn("rule_update_pending:rev-new", still_pending["triggers"])

        stale = {**base, "ledger_sha256": "l2", "rule_handshake": {
            **pending["rule_handshake"], "state": "ledger_stale", "loaded_revision": "rev-new",
        }}
        _, stale_state = lifecycle_hook.evaluate_event(
            {"hook_event_name": "PostToolUse", "session_id": "controller-1", "tool_input": {}, "tool_response": {}},
            snapshot=stale, prior_state=still_pending,
        )
        self.assertNotIn("rule_update_pending:rev-new", stale_state["triggers"])
        self.assertIn("rule_ledger_stale:rev-new", stale_state["triggers"])

        current = {**base, "ledger_sha256": "l3", "rule_handshake": {
            **pending["rule_handshake"], "state": "current", "blocking": False, "loaded_revision": "rev-new",
        }}
        _, current_state = lifecycle_hook.evaluate_event(
            {"hook_event_name": "PostToolUse", "session_id": "controller-1", "tool_input": {}, "tool_response": {}},
            snapshot=current, prior_state=stale_state,
        )
        self.assertFalse(any(t.startswith("rule_update_pending:") or t.startswith("rule_ledger_stale:") for t in current_state["triggers"]))

    def test_rule_wake_policy_is_immediate_only_for_critical_change_hitting_live_assignment(self) -> None:
        handshake = {
            "state": "pending_ack", "impact": "live_assignments",
            "changed_files": ["scripts/run_external_agent.mjs"],
        }
        policy = lifecycle_hook.derive_rule_wake_policy(
            handshake,
            assignment_liveness={"T1": {"ledger_state": "ACTIVE", "state": "healthy"}},
        )
        self.assertEqual(policy, "immediate")

    def test_rule_wake_policy_defers_live_impact_until_current_event_boundary(self) -> None:
        handshake = {
            "state": "pending_ack", "impact": "live_assignments",
            "changed_files": ["references/context-governance.md"],
        }
        policy = lifecycle_hook.derive_rule_wake_policy(
            handshake, assignment_liveness={}
        )
        self.assertEqual(policy, "after_event")

    def test_rule_wake_policy_uses_next_natural_turn_for_nonimpacting_update(self) -> None:
        handshake = {
            "state": "pending_ack", "impact": "none",
            "changed_files": ["references/context-governance.md"],
        }
        policy = lifecycle_hook.derive_rule_wake_policy(
            handshake, assignment_liveness={}
        )
        self.assertEqual(policy, "next_turn")

    def test_after_event_rule_update_allows_current_control_receipt_without_new_assignment(self) -> None:
        status = {
            "state": "pending_ack", "blocking": True, "impact": "live_assignments",
            "installed_revision": "rev-new", "changed_files": ["references/context-governance.md"],
        }
        self.assertEqual(
            control_event_guard.canonical_rule_handshake_errors(
                Path("."), Path("TASK_LEDGER.md"),
                snapshot={"assignment_liveness": {}, "new_assignments": []},
                handshake_evaluator=lambda *_args, **_kwargs: status,
            ),
            [],
        )

    def test_pending_live_e2e_allows_safe_control_cycle_but_no_new_assignment(self) -> None:
        status = {
            "state": "pending_live_e2e", "blocking": True, "impact": "live_assignments",
            "installed_revision": "rev-new", "changed_files": ["scripts/web_lifecycle_bridge.py"],
        }
        self.assertEqual(
            lifecycle_hook.derive_rule_wake_policy(status, assignment_liveness={}),
            "after_event",
        )
        self.assertEqual(
            control_event_guard.canonical_rule_handshake_errors(
                Path("."), Path("TASK_LEDGER.md"),
                snapshot={"assignment_liveness": {}, "new_assignments": []},
                handshake_evaluator=lambda *_args, **_kwargs: status,
            ),
            [],
        )
        errors = control_event_guard.canonical_rule_handshake_errors(
            Path("."), Path("TASK_LEDGER.md"),
            snapshot={"assignment_liveness": {}, "new_assignments": [{"task_id": "T2"}]},
            handshake_evaluator=lambda *_args, **_kwargs: status,
        )
        self.assertIn("rule handshake pending_live_e2e for installed revision rev-new", errors)

    def test_immediate_rule_update_still_blocks_current_control_receipt(self) -> None:
        status = {
            "state": "pending_ack", "blocking": True, "impact": "live_assignments",
            "installed_revision": "rev-new", "changed_files": ["scripts/run_external_agent.mjs"],
        }
        errors = control_event_guard.canonical_rule_handshake_errors(
            Path("."), Path("TASK_LEDGER.md"),
            snapshot={
                "assignment_liveness": {"T1": {"ledger_state": "ACTIVE", "state": "healthy"}},
                "new_assignments": [],
            },
            handshake_evaluator=lambda *_args, **_kwargs: status,
        )
        self.assertIn("rule handshake pending_ack for installed revision rev-new", errors)

    def test_next_turn_rule_update_does_not_interrupt_existing_post_tool_event(self) -> None:
        snapshot = {
            "head": "abc", "ledger_sha256": "l", "worktree_status_sha256": "s",
            "ready_ids": [], "runnable_ids": [], "candidate_revisions": [], "ledger_errors": [],
            "assignment_liveness": {},
            "rule_handshake": {
                "state": "pending_ack", "blocking": False, "installed_revision": "rev-docs",
                "impact": "none", "changed_files": ["references/context-governance.md"],
            },
        }
        output, state = lifecycle_hook.evaluate_event(
            {"hook_event_name": "PostToolUse", "session_id": "controller-1", "tool_input": {}, "tool_response": {}},
            snapshot=snapshot,
            prior_state={
                "snapshot": {**snapshot, "rule_handshake": {"state": "current", "blocking": False}},
                "pending_control_event": False, "triggers": [], "stop_continuations": 0,
            },
        )
        self.assertEqual(output, {})
        self.assertEqual(state["rule_wake_policy"], "next_turn")
        self.assertFalse(state["pending_control_event"])

    def test_after_event_control_receipt_clears_old_event_but_keeps_rule_wake_pending(self) -> None:
        snapshot = {
            "head": "abc", "ledger_sha256": "l", "worktree_status_sha256": "s",
            "ready_ids": [], "runnable_ids": [], "candidate_revisions": [], "ledger_errors": [],
            "assignment_liveness": {},
            "rule_handshake": {
                "state": "pending_ack", "blocking": True, "installed_revision": "rev-new",
                "impact": "live_assignments", "changed_files": ["references/context-governance.md"],
            },
        }
        event = {
            "hook_event_name": "PostToolUse", "session_id": "controller-1",
            "tool_input": {"command": f"{sys.executable} {SKILL_ROOT / 'scripts' / 'control_event_guard.py'} event.json --ledger TASK_LEDGER.md --repo ."},
            "tool_response": {"exit_code": 0, "output": "control-event: allowed"},
        }
        output, state = lifecycle_hook.evaluate_event(
            event, snapshot=snapshot,
            prior_state={
                "snapshot": snapshot, "pending_control_event": True,
                "triggers": ["candidate_queue_changed", "rule_update_pending:rev-new"],
                "stop_continuations": 1,
            },
        )
        self.assertEqual(output, {})
        self.assertTrue(state["pending_control_event"])
        self.assertEqual(state["triggers"], ["rule_update_pending:rev-new"])
        self.assertEqual(state["rule_wake_policy"], "after_event")

    def test_lifecycle_rule_install_integrity_error_is_blocking(self) -> None:
        snapshot = {
            "head": "abc", "ledger_sha256": "l", "worktree_status_sha256": "s",
            "ready_ids": [], "candidate_revisions": [], "ledger_errors": [], "assignment_liveness": {},
            "rule_handshake": {"state": "integrity_error", "blocking": True, "installed_revision": "rev-bad", "errors": ["hash mismatch"]},
        }
        triggers = lifecycle_hook.lifecycle_triggers(snapshot, None)
        self.assertIn("rule_install_integrity_error:rev-bad", triggers)

    def test_lifecycle_wake_generation_increments_after_closed_event_reoccurs(self) -> None:
        snapshot = {
            "root": "/tmp/project",
            "head": "h1",
            "ledger_sha256": "l1",
            "worktree_status_sha256": "s1",
            "ready_ids": [],
            "runnable_ids": [],
            "candidate_revisions": [],
            "assignment_liveness": {},
            "rule_handshake": {"state": "current", "installed_revision": "rev-1"},
        }
        _, first = lifecycle_hook.evaluate_event(
            {"hook_event_name": "SubagentStop", "session_id": "controller-1", "agent_id": "writer-1"},
            snapshot=snapshot,
            prior_state=None,
        )
        self.assertTrue(first["pending_control_event"])
        self.assertEqual(first["wake_generation"], 1)

        _, closed = lifecycle_hook.evaluate_event(
            {
                "hook_event_name": "PostToolUse",
                "session_id": "controller-1",
                "tool_input": {"command": f"{sys.executable} {SKILL_ROOT / 'scripts' / 'control_event_guard.py'} receipt.json --ledger TASK_LEDGER.md --repo ."},
                "tool_response": {"output": "control-event: allowed", "exit_code": 0},
            },
            snapshot=snapshot,
            prior_state=first,
        )
        self.assertFalse(closed["pending_control_event"])
        self.assertEqual(closed["wake_generation"], 1)

        _, second = lifecycle_hook.evaluate_event(
            {"hook_event_name": "SubagentStop", "session_id": "controller-1", "agent_id": "writer-1"},
            snapshot=snapshot,
            prior_state=closed,
        )
        self.assertTrue(second["pending_control_event"])
        self.assertEqual(second["triggers"], first["triggers"])
        self.assertEqual(second["wake_generation"], 2)

    def test_subagent_stop_preserves_terminal_receipt_path_for_controller_resume(self) -> None:
        snapshot = {
            "root": "/tmp/project", "head": "h1", "ledger_sha256": "l1",
            "worktree_status_sha256": "s1", "ready_ids": [], "runnable_ids": [],
            "candidate_revisions": [], "assignment_liveness": {},
            "rule_handshake": {"state": "current", "installed_revision": "rev-1"},
        }
        receipt = "/tmp/reviewer-terminal.json"
        _, state = lifecycle_hook.evaluate_event(
            {
                "hook_event_name": "SubagentStop", "session_id": "controller-1",
                "agent_id": "reviewer-1", "terminal_receipt": receipt,
            },
            snapshot=snapshot, prior_state=None,
        )
        self.assertEqual(state["pending_terminal_receipts"], [receipt])

    def test_successful_control_receipt_clears_consumed_terminal_receipts(self) -> None:
        snapshot = {
            "root": "/tmp/project", "head": "h1", "ledger_sha256": "l1",
            "worktree_status_sha256": "s1", "ready_ids": [], "runnable_ids": [],
            "candidate_revisions": [], "assignment_liveness": {},
            "rule_handshake": {"state": "current", "installed_revision": "rev-1"},
        }
        _, pending = lifecycle_hook.evaluate_event(
            {
                "hook_event_name": "SubagentStop", "session_id": "controller-1",
                "agent_id": "reviewer-1", "terminal_receipt": "/tmp/reviewer-terminal.json",
            }, snapshot=snapshot, prior_state=None,
        )
        _, closed = lifecycle_hook.evaluate_event(
            {
                "hook_event_name": "PostToolUse", "session_id": "controller-1",
                "tool_input": {"command": f"{sys.executable} {SKILL_ROOT / 'scripts' / 'control_event_guard.py'} receipt.json --ledger TASK_LEDGER.md --repo ."},
                "tool_response": {"output": "control-event: allowed", "exit_code": 0},
            }, snapshot=snapshot, prior_state=pending,
        )
        self.assertEqual(closed.get("pending_terminal_receipts", []), [])

    def test_session_start_keeps_unconsumed_terminal_receipt_pending(self) -> None:
        receipt = "/tmp/reviewer-terminal.json"
        snapshot = {
            "root": "/tmp/project", "head": "h1", "ledger_sha256": "l1", "worktree_status_sha256": "s1",
            "ready_ids": [], "runnable_ids": [], "candidate_revisions": [], "assignment_liveness": {},
            "rule_handshake": {"state": "current", "installed_revision": "rev-1"},
        }
        prior = {
            "pending_control_event": True, "triggers": ["subagent_stopped:reviewer-1"],
            "pending_terminal_receipts": [receipt], "wake_generation": 1, "controller_host": "web",
        }
        output, state = lifecycle_hook.evaluate_event(
            {"hook_event_name": "SessionStart", "session_id": "controller-1", "controller_host": "web"},
            snapshot=snapshot, prior_state=prior,
        )
        self.assertTrue(state["pending_control_event"])
        self.assertEqual(state["pending_terminal_receipts"], [receipt])
        self.assertIn(receipt, str(output))

    def test_lifecycle_hook_surfaces_unhealthy_active_runtime_without_git_change(self) -> None:
        snapshot = {
            "head": "abc123", "ledger_sha256": "ledger-1", "worktree_status_sha256": "status-1",
            "ready_ids": [], "candidate_revisions": [], "ledger_errors": [],
            "assignment_liveness": {"F1": {"ledger_state": "ACTIVE", "state": "unhealthy", "reason": "lease_expired"}},
            "stale_active_ids": ["F1"], "progress_stale_ids": [], "terminal_active_ids": [],
        }
        prior={"snapshot":dict(snapshot),"pending_control_event":False,"triggers":[],"stop_continuations":0}
        output, state=lifecycle_hook.evaluate_event({"hook_event_name":"PostToolUse","session_id":"s","tool_input":{},"tool_response":{}},snapshot=snapshot,prior_state=prior)
        self.assertTrue(state["pending_control_event"]); self.assertIn("active_lease_expired:F1", state["triggers"]); self.assertIn("active_lease_expired:F1", str(output))

    def test_control_event_guard_blocks_canonical_pending_rule_handshake(self) -> None:
        status = {
            "state": "pending_ack", "blocking": True, "installed_revision": "rev-new"
        }
        errors = control_event_guard.canonical_rule_handshake_errors(
            Path("."), Path("TASK_LEDGER.md"),
            handshake_evaluator=lambda *_args, **_kwargs: status,
        )
        self.assertIn("rule handshake pending_ack for installed revision rev-new", errors)

    def test_control_event_guard_blocks_stale_active_runtime(self) -> None:
        snapshot=self.complete_event_receipt(); snapshot.update({"ledger_sha256":"x","available_slots":0,"ready_packages":[],"assignment_liveness":{"F1":{"ledger_state":"ACTIVE","state":"unhealthy","reason":"lease_expired"}}})
        errors=control_event_guard.validate_snapshot(snapshot)
        self.assertIn("ACTIVE runtime unhealthy: F1 (lease_expired)", errors)

    def test_control_event_guard_requires_every_work_in_flight_runtime(self) -> None:
        snapshot = self.complete_event_receipt()
        snapshot.update(
            {
                "ledger_sha256": "x",
                "available_slots": 0,
                "capacity_projection": {
                    "source": "host_runtime",
                    "evidence": "receipt:host-runtime/capacity-exact",
                    "total_slots": 2,
                    "occupied_task_ids": ["F1", "F2"],
                },
                "ready_packages": [],
                "assignment_liveness": {
                    "F1": {
                        "ledger_state": "ACTIVE",
                        "state": "healthy",
                        "reason": "lease_current",
                    }
                },
            }
        )

        errors = control_event_guard.validate_snapshot(
            snapshot,
            ledger_work_in_flight={"F1": "ACTIVE", "F2": "RECOVERING"},
        )

        self.assertIn("assignment_liveness omitted RECOVERING task: F2", errors)

    def test_control_event_guard_accepts_exact_work_in_flight_runtime(self) -> None:
        snapshot = self.complete_event_receipt()
        snapshot.update(
            {
                "ledger_sha256": "x",
                "available_slots": 0,
                "capacity_projection": {
                    "source": "host_runtime",
                    "evidence": "receipt:host-runtime/capacity-exact",
                    "total_slots": 2,
                    "occupied_task_ids": ["F1", "F2"],
                },
                "ready_packages": [],
                "assignment_liveness": {
                    "F1": {
                        "ledger_state": "ACTIVE",
                        "state": "healthy",
                        "reason": "lease_current",
                    },
                    "F2": {
                        "ledger_state": "RECOVERING",
                        "state": "healthy",
                        "reason": "lease_current",
                    },
                },
            }
        )

        self.assertEqual(
            control_event_guard.validate_snapshot(
                snapshot,
                ledger_work_in_flight={"F1": "ACTIVE", "F2": "RECOVERING"},
            ),
            [],
        )
    def test_runtime_reconciliation_keeps_checkpoint_governance_lightweight(self) -> None:
        long_task = (SKILL_ROOT / "references" / "long-task-governance.md").read_text(encoding="utf-8")
        delivery = (SKILL_ROOT / "references" / "agent-delivery-contract.md").read_text(encoding="utf-8")
        for phrase in (
            "普通短任务不强制 checkpoint",
            "不新增总控人工必填字段",
            "不新增第二套状态机",
            "最近已验收 checkpoint",
        ):
            self.assertIn(phrase, long_task)
        self.assertIn("checkpoint 只作为恢复锚点", delivery)

    def test_lifecycle_clears_stale_recovery_trigger_after_task_returns_ready(self) -> None:
        prior_snapshot = {
            "head": "abc", "ledger_sha256": "ledger-old", "worktree_status_sha256": "status",
            "ready_ids": [], "candidate_revisions": [], "ledger_errors": [],
            "assignment_liveness": {
                "F1": {"ledger_state": "RECOVERING", "state": "terminal", "reason": "terminal:failed"}
            },
        }
        prior_state = {
            "snapshot": prior_snapshot, "pending_control_event": True,
            "triggers": ["recovery_stalled:F1"], "stop_continuations": 0,
        }
        current_snapshot = {
            "head": "abc", "ledger_sha256": "ledger-new", "worktree_status_sha256": "status",
            "ready_ids": ["F1"], "candidate_revisions": [], "ledger_errors": [],
            "assignment_liveness": {},
        }
        _, state = lifecycle_hook.evaluate_event(
            {"hook_event_name": "PostToolUse", "session_id": "s", "tool_input": {}, "tool_response": {}},
            snapshot=current_snapshot, prior_state=prior_state,
        )
        self.assertNotIn("recovery_stalled:F1", state["triggers"])
        self.assertIn("READY:F1", state["triggers"])

    def test_lifecycle_current_snapshot_drops_ready_trigger_after_task_blocks(self) -> None:
        prior_snapshot = {
            "head": "abc", "ledger_sha256": "ledger-old", "worktree_status_sha256": "status",
            "ready_ids": ["F1"], "candidate_revisions": [], "ledger_errors": [], "assignment_liveness": {},
        }
        prior_state = {
            "snapshot": prior_snapshot, "pending_control_event": True,
            "triggers": ["READY:F1"], "stop_continuations": 0,
        }
        current_snapshot = {
            "head": "abc", "ledger_sha256": "ledger-new", "worktree_status_sha256": "status",
            "ready_ids": [], "candidate_revisions": [], "ledger_errors": [],
            "assignment_liveness": {"F1": {"ledger_state": "BLOCKED", "state": "terminal", "reason": "external blocker"}},
        }
        output, state = lifecycle_hook.evaluate_event(
            {"hook_event_name": "PostToolUse", "session_id": "s", "tool_input": {}, "tool_response": {}},
            snapshot=current_snapshot, prior_state=prior_state,
        )
        self.assertNotIn("READY:F1", state["triggers"])
        self.assertNotIn("READY 派发", str(output))

    def test_lifecycle_current_snapshot_drops_terminal_trigger_after_task_blocks(self) -> None:
        prior_snapshot = {
            "head": "abc", "ledger_sha256": "ledger-old", "worktree_status_sha256": "status",
            "ready_ids": [], "candidate_revisions": [], "ledger_errors": [],
            "assignment_liveness": {"F1": {"ledger_state": "ACTIVE", "state": "terminal", "reason": "terminal:failed"}},
        }
        prior_state = {
            "snapshot": prior_snapshot, "pending_control_event": True,
            "triggers": ["agent_session_terminal:F1"], "stop_continuations": 0,
        }
        current_snapshot = {
            "head": "abc", "ledger_sha256": "ledger-new", "worktree_status_sha256": "status",
            "ready_ids": [], "candidate_revisions": [], "ledger_errors": [],
            "assignment_liveness": {"F1": {"ledger_state": "BLOCKED", "state": "terminal", "reason": "external blocker"}},
        }
        _, state = lifecycle_hook.evaluate_event(
            {"hook_event_name": "PostToolUse", "session_id": "s", "tool_input": {}, "tool_response": {}},
            snapshot=current_snapshot, prior_state=prior_state,
        )
        self.assertNotIn("agent_session_terminal:F1", state["triggers"])

    def test_lifecycle_keeps_terminal_trigger_while_task_is_still_active(self) -> None:
        snapshot = {
            "head": "abc", "ledger_sha256": "ledger", "worktree_status_sha256": "status",
            "ready_ids": [], "candidate_revisions": [], "ledger_errors": [],
            "assignment_liveness": {
                "F1": {"ledger_state": "ACTIVE", "state": "terminal", "reason": "terminal:failed"}
            },
        }
        triggers = lifecycle_hook.lifecycle_triggers(snapshot, None)
        self.assertIn("agent_session_terminal:F1", triggers)

    def test_lifecycle_surfaces_recovery_budget_exhaustion_as_control_trigger(self) -> None:
        snapshot = {
            "head": "abc", "ledger_sha256": "ledger", "worktree_status_sha256": "status",
            "ready_ids": [], "candidate_revisions": [], "ledger_errors": [],
            "assignment_liveness": {
                "F1": {"ledger_state": "ACTIVE", "state": "budget_exhausted", "reason": "recovery_budget_exhausted"}
            },
        }
        triggers = lifecycle_hook.lifecycle_triggers(snapshot, None)
        self.assertIn("recovery_budget_exhausted:F1", triggers)

    def test_lifecycle_hook_surfaces_invalid_ledger_at_session_start(self) -> None:
        output, next_state = lifecycle_hook.evaluate_event(
            {
                "hook_event_name": "SessionStart",
                "session_id": "session-1",
                "turn_id": "turn-1",
            },
            snapshot={
                "head": "abc123",
                "ledger_sha256": "ledger-1",
                "worktree_status_sha256": "status-1",
                "ready_ids": [],
                "candidate_revisions": [],
                "ledger_errors": [
                    "M2-F2 next action references undeclared task ID M2-F2-DEV-WEREAD-QA-01"
                ],
            },
            prior_state=None,
        )

        context = output["hookSpecificOutput"]["additionalContext"]
        self.assertIn("LEDGER_INVALID", context)
        self.assertTrue(next_state["pending_control_event"])

    def test_lifecycle_hook_surfaces_unmerged_candidate(self) -> None:
        snapshot = {
            "head": "abc123",
            "ledger_sha256": "ledger-1",
            "worktree_status_sha256": "status-1",
            "ready_ids": [],
            "candidate_revisions": ["candidate-123"],
        }

        output, next_state = lifecycle_hook.evaluate_event(
            {
                "hook_event_name": "PostToolUse",
                "session_id": "session-1",
                "turn_id": "turn-1",
                "tool_name": "Bash",
                "tool_input": {"command": "git status --short"},
                "tool_response": {"output": ""},
            },
            snapshot=snapshot,
            prior_state=None,
        )

        self.assertIn("candidate-123", output["hookSpecificOutput"]["additionalContext"])
        self.assertTrue(next_state["pending_control_event"])

    def test_lifecycle_hook_stop_continues_until_ready_is_dispatched(self) -> None:
        output, next_state = lifecycle_hook.evaluate_event(
            {
                "hook_event_name": "Stop",
                "session_id": "session-1",
                "turn_id": "turn-1",
                "stop_hook_active": False,
            },
            snapshot={
                "head": "abc123",
                "ledger_sha256": "ledger-1",
                "worktree_status_sha256": "status-1",
                "ready_ids": ["WEB-READY"],
            },
            prior_state={
                "pending_control_event": True,
                "triggers": ["READY:WEB-READY"],
            },
        )

        self.assertEqual(output["decision"], "block")
        self.assertIn("WEB-READY", output["reason"])
        self.assertTrue(next_state["pending_control_event"])

    def test_lifecycle_hook_repeated_stop_with_ready_stays_blocked_until_dispatch_or_blocked(self) -> None:
        snapshot = {
            "head": "abc123",
            "ledger_sha256": "ledger-1",
            "worktree_status_sha256": "status-1",
            "ready_ids": ["SERVER-GATE"],
            "candidate_revisions": [],
            "rule_handshake": {"state": "current", "blocking": False, "installed_revision": "rev-current"},
        }
        prior = {
            "pending_control_event": True,
            "triggers": ["READY:SERVER-GATE", "ledger_changed"],
            "stop_continuations": 1,
            "snapshot": snapshot,
        }

        output, next_state = lifecycle_hook.evaluate_event(
            {
                "hook_event_name": "Stop",
                "session_id": "session-1",
                "turn_id": "turn-2",
                "stop_hook_active": False,
            },
            snapshot=snapshot,
            prior_state=prior,
        )

        self.assertEqual(output["decision"], "block")
        self.assertIn("SERVER-GATE", output["reason"])
        self.assertIn("BLOCKED", output["reason"])
        self.assertTrue(next_state["pending_control_event"])

    def test_lifecycle_hook_repeated_stop_without_progress_stays_blocked_while_pending(self) -> None:
        snapshot = {
            "head": "abc123",
            "ledger_sha256": "ledger-1",
            "worktree_status_sha256": "status-1",
            "ready_ids": [],
            "candidate_revisions": ["candidate-123"],
        }
        first_output, first_state = lifecycle_hook.evaluate_event(
            {
                "hook_event_name": "Stop",
                "session_id": "session-1",
                "turn_id": "turn-1",
            },
            snapshot=snapshot,
            prior_state={
                "pending_control_event": True,
                "triggers": ["CANDIDATE:candidate-123"],
            },
        )
        self.assertEqual(first_output["decision"], "block")

        second_output, second_state = lifecycle_hook.evaluate_event(
            {
                "hook_event_name": "Stop",
                "session_id": "session-1",
                "turn_id": "turn-1",
            },
            snapshot=snapshot,
            prior_state=first_state,
        )

        self.assertEqual(second_output["decision"], "block")
        self.assertIn("candidate-123", second_output["reason"])
        self.assertEqual(second_state["stop_continuations"], 2)

    def test_stop_gate_docs_do_not_describe_second_stop_escape(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        for relative in ("README.md", "references/long-task-governance.md"):
            text = (repo_root / relative).read_text(encoding="utf-8")
            self.assertNotIn("第二次 Stop", text, relative)
            self.assertNotIn("首次 Stop 只允许一次受控续作", text, relative)
            self.assertIn("重复 Stop", text, relative)

    def test_explicit_non_user_next_action_survives_tool_use_and_blocks_stop(self) -> None:
        snapshot = {
            "head": "abc123", "ledger_sha256": "ledger-1", "worktree_status_sha256": "status-1",
            "ready_ids": [], "runnable_ids": [], "candidate_revisions": [], "ledger_errors": [],
            "assignment_liveness": {}, "rule_handshake": {"state": "current", "blocking": False},
        }
        post_output, state = lifecycle_hook.evaluate_event(
            {
                "hook_event_name": "PostToolUse", "session_id": "controller-1",
                "controller_host": "web", "tool_name": "AI-Bridge.computer",
                "tool_input": {"detail": "电脑操作：click · 应用 Google Chrome"},
                "tool_response": {"state": "succeeded"},
                "next_action": "read the claimed task result and continue the workflow",
                "requires_user": False,
            },
            snapshot=snapshot, prior_state={"pending_control_event": False, "triggers": [], "stop_continuations": 0},
        )
        self.assertTrue(state["pending_control_event"])
        self.assertEqual(state["next_action"], "read the claimed task result and continue the workflow")
        self.assertFalse(state["requires_user"])
        self.assertIn("read the claimed task result", post_output["hookSpecificOutput"]["additionalContext"])

        stop_output, stopped = lifecycle_hook.evaluate_event(
            {"hook_event_name": "Stop", "session_id": "controller-1", "controller_host": "web"},
            snapshot=snapshot, prior_state=state,
        )
        self.assertEqual(stop_output["decision"], "block")
        self.assertTrue(stopped["pending_control_event"])

    def test_explicit_user_required_next_action_releases_continuation_only_pending_state(self) -> None:
        snapshot = {
            "head": "abc123", "ledger_sha256": "ledger-1", "worktree_status_sha256": "status-1",
            "ready_ids": [], "runnable_ids": [], "candidate_revisions": [], "ledger_errors": [],
            "assignment_liveness": {}, "rule_handshake": {"state": "current", "blocking": False},
        }
        prior = {
            "pending_control_event": True, "triggers": ["next_action_pending"],
            "next_action": "read result", "requires_user": False, "stop_continuations": 0,
            "snapshot": snapshot,
        }
        output, state = lifecycle_hook.evaluate_event(
            {
                "hook_event_name": "PostToolUse", "session_id": "controller-1",
                "next_action": "user must choose which account to use", "requires_user": True,
                "tool_name": "Bash", "tool_input": {}, "tool_response": {"exit_code": 0},
            },
            snapshot=snapshot, prior_state=prior,
        )
        self.assertEqual(output, {})
        self.assertFalse(state["pending_control_event"])
        self.assertEqual(state["next_action"], "user must choose which account to use")
        self.assertTrue(state["requires_user"])


    def test_lifecycle_hook_snapshot_progress_resets_stop_continuation(self) -> None:
        output, next_state = lifecycle_hook.evaluate_event(
            {
                "hook_event_name": "PostToolUse",
                "session_id": "session-1",
                "turn_id": "turn-1",
                "tool_name": "Bash",
                "tool_input": {"command": "git status --short"},
                "tool_response": {"output": ""},
            },
            snapshot={
                "head": "abc123",
                "ledger_sha256": "ledger-2",
                "worktree_status_sha256": "status-1",
                "ready_ids": ["WEB-READY"],
                "candidate_revisions": [],
            },
            prior_state={
                "pending_control_event": True,
                "triggers": ["READY:WEB-READY"],
                "stop_continuations": 1,
                "snapshot": {
                    "head": "abc123",
                    "ledger_sha256": "ledger-1",
                    "worktree_status_sha256": "status-1",
                    "ready_ids": ["WEB-READY"],
                    "candidate_revisions": [],
                },
            },
        )

        self.assertEqual(output["hookSpecificOutput"]["hookEventName"], "PostToolUse")
        self.assertEqual(next_state["stop_continuations"], 0)

    def test_lifecycle_hook_successful_guard_receipt_clears_pending_event(self) -> None:
        output, next_state = lifecycle_hook.evaluate_event(
            {
                "hook_event_name": "PostToolUse",
                "session_id": "session-1",
                "turn_id": "turn-1",
                "tool_name": "Bash",
                "tool_input": {
                    "command": f"{sys.executable} {SKILL_ROOT / 'scripts' / 'control_event_guard.py'} receipt.json --ledger TASK_LEDGER.md"
                },
                "tool_response": {"output": "control-event: allowed", "exit_code": 0},
            },
            snapshot={
                "head": "abc123",
                "ledger_sha256": "ledger-2",
                "worktree_status_sha256": "status-2",
                "ready_ids": [],
            },
            prior_state={
                "pending_control_event": True,
                "triggers": ["ledger_changed"],
            },
        )

        self.assertEqual(output, {})
        self.assertFalse(next_state["pending_control_event"])
        self.assertEqual(next_state["triggers"], [])

    def test_lifecycle_hook_does_not_retrigger_unchanged_candidate_after_receipt(self) -> None:
        snapshot = {
            "head": "abc123",
            "ledger_sha256": "ledger-2",
            "worktree_status_sha256": "status-2",
            "ready_ids": [],
            "candidate_revisions": ["candidate-123"],
            "ledger_errors": [],
            "assignment_liveness": {},
        }
        receipt_output, receipt_state = lifecycle_hook.evaluate_event(
            {
                "hook_event_name": "PostToolUse",
                "session_id": "session-1",
                "turn_id": "turn-1",
                "tool_name": "Bash",
                "tool_input": {
                    "command": (
                        f"{sys.executable} {SKILL_ROOT / 'scripts' / 'control_event_guard.py'} receipt.json "
                        "--ledger TASK_LEDGER.md --repo ."
                    )
                },
                "tool_response": {"output": "control-event: allowed", "exit_code": 0},
            },
            snapshot=snapshot,
            prior_state={
                "snapshot": dict(snapshot),
                "pending_control_event": True,
                "triggers": ["CANDIDATE:candidate-123"],
            },
        )
        self.assertEqual(receipt_output, {})
        self.assertFalse(receipt_state["pending_control_event"])

        next_output, next_state = lifecycle_hook.evaluate_event(
            {
                "hook_event_name": "PostToolUse",
                "session_id": "session-1",
                "turn_id": "turn-1",
                "tool_name": "Bash",
                "tool_input": {"command": "git status --short"},
                "tool_response": {"output": "", "exit_code": 0},
            },
            snapshot=snapshot,
            prior_state=receipt_state,
        )

        self.assertEqual(next_output, {})
        self.assertFalse(next_state["pending_control_event"])
        self.assertEqual(next_state["triggers"], [])

    def test_lifecycle_hook_ignores_projects_without_a_canonical_ledger(self) -> None:
        output, next_state = lifecycle_hook.evaluate_event(
            {
                "hook_event_name": "Stop",
                "session_id": "session-1",
                "turn_id": "turn-1",
            },
            snapshot=None,
            prior_state={"pending_control_event": True},
        )

        self.assertEqual(output, {})
        self.assertEqual(next_state, {})

    def test_event_scope_guard_allows_only_causally_required_same_candidate_action(self) -> None:
        contract = {
            "event_id": "event-1",
            "event_type": "candidate integration",
            "primary_task": "F1",
            "candidate_revision": "abc123",
            "allowed_actions": ["review", "integrate", "main_regression", "ledger_sync"],
            "allowed_files": ["app/a.ts", "TASK_LEDGER.md"],
            "terminal_receipt": "main regression and ledger sync",
        }
        proposed = {
            "action": "main_regression",
            "primary_task": "F1",
            "candidate_revision": "abc123",
            "files": [],
            "required_to_close_current_state": True,
        }

        self.assertEqual(
            event_scope_guard.classify_append(contract, proposed),
            ("SAME_EVENT", []),
        )
    def test_event_scope_guard_allows_project_wide_dispatch_across_business_lines(self) -> None:
        contract = {
            "event_id": "review-web-1",
            "event_type": "review_terminal",
            "primary_task": "WEB-1",
            "candidate_revision": "web-candidate",
            "terminal_receipt": "receipt:web-review",
            "allowed_actions": ["consume_review", "dispatch"],
            "allowed_files": [],
        }
        proposed = {
            "action": "dispatch",
            "primary_task": "MINI-READY",
            "candidate_revision": "project-wide",
            "files": [],
            "required_to_close_current_state": True,
            "starts_new_implementation": False,
            "waits_for_future_input": False,
            "project_wide_scheduler_action": True,
            "derived_from_project_projection": True,
        }
        self.assertEqual(
            event_scope_guard.classify_append(contract, proposed),
            ("SAME_EVENT", []),
        )

    def test_event_scope_guard_rejects_cross_task_work_without_project_wide_dispatch_proof(self) -> None:
        contract = {
            "event_id": "review-web-1",
            "event_type": "review_terminal",
            "primary_task": "WEB-1",
            "candidate_revision": "web-candidate",
            "terminal_receipt": "receipt:web-review",
            "allowed_actions": ["consume_review", "dispatch"],
            "allowed_files": [],
        }
        base = {
            "action": "dispatch",
            "primary_task": "MINI-READY",
            "candidate_revision": "project-wide",
            "files": [],
            "required_to_close_current_state": True,
            "starts_new_implementation": False,
            "waits_for_future_input": False,
        }
        decision, reasons = event_scope_guard.classify_append(contract, base)
        self.assertEqual(decision, "QUEUE_NEXT_EVENT")
        self.assertIn("different primary task", reasons)

        implementation = {
            **base,
            "project_wide_scheduler_action": True,
            "derived_from_project_projection": True,
            "starts_new_implementation": True,
        }
        decision, reasons = event_scope_guard.classify_append(contract, implementation)
        self.assertEqual(decision, "QUEUE_NEXT_EVENT")
        self.assertIn("new implementation belongs to a new event", reasons)


    def test_event_scope_guard_queues_unrelated_or_future_work(self) -> None:
        contract = {
            "event_id": "event-1",
            "event_type": "candidate integration",
            "primary_task": "F1",
            "candidate_revision": "abc123",
            "allowed_actions": ["review", "integrate", "ledger_sync"],
            "allowed_files": ["app/a.ts", "TASK_LEDGER.md"],
            "terminal_receipt": "integration verdict",
        }
        proposed = {
            "action": "implement",
            "primary_task": "F2",
            "candidate_revision": "def456",
            "files": ["app/b.ts"],
            "required_to_close_current_state": False,
            "starts_new_implementation": True,
            "waits_for_future_input": True,
        }

        decision, reasons = event_scope_guard.classify_append(contract, proposed)

        self.assertEqual(decision, "QUEUE_NEXT_EVENT")
        self.assertIn("different primary task", reasons)
        self.assertIn("new implementation belongs to a new event", reasons)
        self.assertIn("future input must trigger a new event", reasons)

    def test_assignment_lease_rejects_writes_before_complete_ack(self) -> None:
        errors = assignment_lease_guard.validate_assignment(
            {
                "assignment_id": "F1-writer-1",
                "agent_id": "/root/writer",
                "state": "RESERVED",
                "observed_modified_files": ["app/a.ts"],
            }
        )

        self.assertIn(
            "RESERVED assignment cannot modify files before delivered ACK", errors
        )

    def test_assignment_lease_allows_active_writer_with_complete_ack(self) -> None:
        errors = assignment_lease_guard.validate_assignment(
            {
                "assignment_id": "F1-writer-1",
                "agent_id": "/root/writer",
                "state": "ACTIVE",
                "primary_goal": "close F1 safely",
                "success_criteria": ["targeted test green"],
                "owned_scope": ["app/a.ts"],
                "forbidden_scope": [],
                "parallelizable": True,
                "role": "writer",
                "observed_modified_files": ["app/a.ts"],
                "ack": {
                    "repository_root": "/repo",
                    "branch": "codex/f1",
                    "head": "abc123",
                    "status": "clean at ACK",
                    "owned_files": ["app/a.ts"],
                    "first_red": "test_f1 fails",
                    "stop_condition": "candidate commit and receipt",
                },
            }
        )

        self.assertEqual(errors, [])

    def test_assignment_contract_requires_one_goal_and_parallel_decision(self) -> None:
        assignment = {
            "assignment_id": "F1-writer-1", "task_id": "F1", "agent_id": "/root/writer",
            "state": "ACKED", "observed_modified_files": [],
            "ack": {"repository_root": "/repo", "branch": "codex/f1", "head": "abc123",
                    "status": "clean at ACK", "owned_files": ["app/a.ts"],
                    "first_red": "test_f1 fails", "stop_condition": "candidate commit and receipt"},
        }
        errors = assignment_lease_guard.validate_assignment(assignment)
        self.assertIn("primary_goal is required", errors)
        self.assertIn("success_criteria must contain unique non-empty items", errors)
        self.assertIn("owned_scope must contain unique non-empty items", errors)
        self.assertIn("forbidden_scope must be a list", errors)
        self.assertIn("parallelizable must be true or false", errors)

    def test_assignment_contract_accepts_single_goal_and_parallel_reason(self) -> None:
        assignment = {
            "assignment_id": "F1-writer-1", "task_id": "F1", "agent_id": "/root/writer",
            "state": "ACKED", "primary_goal": "close F1 migration guard",
            "success_criteria": ["targeted test green", "scope-only diff"],
            "owned_scope": ["app/a.ts"], "forbidden_scope": ["app/b.ts"],
            "parallelizable": False, "dependency_reason": "shares migration contract with F0",
            "observed_modified_files": [],
            "ack": {"repository_root": "/repo", "branch": "codex/f1", "head": "abc123",
                    "status": "clean at ACK", "owned_files": ["app/a.ts"],
                    "first_red": "test_f1 fails", "stop_condition": "candidate commit and receipt"},
        }
        self.assertEqual(assignment_lease_guard.validate_assignment(assignment), [])

    def test_runtime_aware_active_requires_current_matching_lease(self) -> None:
        assignment = {
            "assignment_id": "F1-writer-1", "task_id": "F1", "agent_id": "/root/writer",
            "state": "ACTIVE", "worktree": "/tmp/wt", "observed_modified_files": [],
            "primary_goal": "close F1 safely", "success_criteria": ["targeted test green"],
            "owned_scope": ["app/a.ts"], "forbidden_scope": [], "parallelizable": True,
            "ack": {"repository_root": "/repo", "branch": "codex/f1", "head": "abc123",
                    "status": "clean at ACK", "owned_files": ["app/a.ts"],
                    "first_red": "test_f1 fails", "stop_condition": "candidate commit and receipt"},
        }
        errors = assignment_lease_guard.validate_assignment(assignment, runtime_state={"schema_version": 1, "leases": {}})
        self.assertIn("ACTIVE requires current runtime lease", errors)

    def test_runtime_aware_active_accepts_healthy_matching_lease(self) -> None:
        from datetime import datetime, timedelta, timezone
        now=datetime(2026,8,29,10,0,tzinfo=timezone.utc)
        assignment = {
            "assignment_id": "F1-writer-1", "task_id": "F1", "agent_id": "/root/writer",
            "state": "ACTIVE", "worktree": "/tmp/wt", "observed_modified_files": [],
            "primary_goal": "close F1 safely", "success_criteria": ["targeted test green"],
            "owned_scope": ["app/a.ts"], "forbidden_scope": [], "parallelizable": True,
            "ack": {"repository_root": "/repo", "branch": "codex/f1", "head": "abc123",
                    "status": "clean at ACK", "owned_files": ["app/a.ts"],
                    "first_red": "test_f1 fails", "stop_condition": "candidate commit and receipt"},
        }
        lease={"assignment_id":"F1-writer-1","task_id":"F1","agent_id":"/root/writer",
               "provider":"grok","session_id":"s1","worktree":"/tmp/wt",
               "lease_expires_at":(now+timedelta(minutes=20)).isoformat(),
               "progress_deadline_at":(now+timedelta(minutes=30)).isoformat(),"terminal_state":None}
        errors=assignment_lease_guard.validate_assignment(assignment, runtime_state={"schema_version":1,"leases":{"F1-writer-1":lease}}, now=now)
        self.assertEqual(errors, [])

    def test_assignment_lease_exact_launch_expectations_fail_closed(self) -> None:
        assignment = {
            "assignment_id": "F1-writer-1", "task_id": "F1", "agent_id": "writer",
            "state": "ACKED", "primary_goal": "close F1",
            "success_criteria": ["green"], "owned_scope": ["app/a.ts"],
            "forbidden_scope": [], "parallelizable": True, "observed_modified_files": [],
            "ack": {
                "repository_root": "/repo", "branch": "codex/f1", "head": "abc123",
                "status": "clean", "owned_files": ["app/a.ts"], "first_red": "red",
                "stop_condition": "candidate",
            },
        }
        errors = assignment_lease_guard.validate_assignment(
            assignment,
            expected_assignment_id="F1-writer-2",
            expected_task_id="F2",
            expected_agent_id="reviewer",
            expected_repository_root="/other",
            expected_branch="main",
            expected_head="def456",
        )
        self.assertIn("assignment_id does not match launch contract", errors)
        self.assertIn("task_id does not match launch contract", errors)
        self.assertIn("agent_id does not match launch contract", errors)
        self.assertIn("ack.repository_root does not match launch repository", errors)
        self.assertIn("ack.branch does not match launch branch", errors)
        self.assertIn("ack.head does not match launch revision", errors)

    def test_assignment_lease_exact_launch_expectations_accept_matching_ack(self) -> None:
        assignment = {
            "assignment_id": "F1-writer-1", "task_id": "F1", "agent_id": "writer",
            "state": "ACKED", "primary_goal": "close F1",
            "success_criteria": ["green"], "owned_scope": ["app/a.ts"],
            "forbidden_scope": [], "parallelizable": True, "observed_modified_files": [],
            "ack": {
                "repository_root": "/repo", "branch": "codex/f1", "head": "abc123",
                "status": "clean", "owned_files": ["app/a.ts"], "first_red": "red",
                "stop_condition": "candidate",
            },
        }
        self.assertEqual(assignment_lease_guard.validate_assignment(
            assignment,
            expected_assignment_id="F1-writer-1", expected_task_id="F1", expected_agent_id="writer",
            expected_repository_root="/repo", expected_branch="codex/f1", expected_head="abc123",
        ), [])

    def test_assignment_lease_reuse_requires_release_and_new_ack(self) -> None:
        errors = assignment_lease_guard.validate_assignment(
            {
                "assignment_id": "F2-writer-2",
                "agent_id": "/root/reused",
                "state": "ACTIVE",
                "observed_modified_files": [],
                "previous_assignment": {
                    "state": "ACTIVE",
                    "files_released": False,
                    "worktree_released": False,
                },
            }
        )

        self.assertTrue(any("complete delivered ACK" in error for error in errors))
        self.assertIn(
            "reused agent requires previous assignment to be FROZEN or TERMINAL",
            errors,
        )

    def test_control_event_guard_cli_rejects_invalid_ledger_contract(self) -> None:
        import json

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = root / "TASK_LEDGER.md"
            ledger.write_text(
                """# Ledger

- 当前 Goal：`M2-F2` 完成微信读书内部闭环
- 下一可见检查点：`M2-F2` 形成真实端证据
- 当前阻塞：无
- 规则版本：abc123

| ID | 状态 / 负责人 | 证据 / 下一步 |
| --- | --- | --- |
| M2-F2 | `ACTIVE` / 项目总控 | 下一步派发 `M2-F2-DEV-WEREAD-QA-01` 后继续 |
""",
                encoding="utf-8",
            )
            snapshot = self.complete_event_receipt()
            snapshot.update(
                {
                    "ledger_sha256": control_event_guard.ledger_sha256(ledger),
                    "available_slots": 0,
                    "ready_packages": [],
                    "required_reviews": [],
                }
            )
            receipt = root / "receipt.json"
            receipt.write_text(json.dumps(snapshot), encoding="utf-8")

            result = control_event_guard.main(
                [str(receipt), "--ledger", str(ledger)]
            )

            self.assertEqual(result, 1)

    def test_control_event_guard_requires_goal_rollover_on_goal_close(self) -> None:
        snapshot = {
            **self.complete_event_receipt(),
            "ledger_sha256": "abc123",
            "available_slots": 0,
            "ready_packages": [],
            "event_contract": {
                "event_id": "close-goal-1",
                "event_type": "Goal closure",
                "primary_task": "M2-F6",
                "candidate_revision": "ledger-abc123",
                "allowed_actions": ["ledger_sync"],
                "allowed_files": ["TASK_LEDGER.md"],
                "terminal_receipt": "M2-F6 Goal closed",
            },
        }

        errors = control_event_guard.validate_snapshot(
            snapshot,
            ledger_ready_ids=set(),
            ledger_open_ids={"M2-F6", "M1-F4-B"},
            ledger_goal_ids={"M2-F6"},
        )

        self.assertTrue(any("goal_rollover" in error for error in errors))

    def test_control_event_guard_accepts_goal_rollover_to_new_open_goal(self) -> None:
        snapshot = {
            **self.complete_event_receipt(),
            "ledger_sha256": "abc123",
            "available_slots": 0,
            "ready_packages": [],
            "event_contract": {
                "event_id": "close-goal-2",
                "event_type": "里程碑收口",
                "primary_task": "M2-F6",
                "candidate_revision": "ledger-abc123",
                "allowed_actions": ["ledger_sync"],
                "allowed_files": ["TASK_LEDGER.md"],
                "terminal_receipt": "M2-F6 Goal 已完成并关闭",
            },
            "event_actions": [{
                "action": "ledger_sync",
                "primary_task": "M2-F6",
                "candidate_revision": "ledger-abc123",
                "files": ["TASK_LEDGER.md"],
                "required_to_close_current_state": True,
            }],
            "goal_rollover": {
                "status": "rolled",
                "closed_goal_id": "M2-F6",
                "current_goal_id": "M1-F4-B",
                "project_recomputed": True,
            },
        }

        errors = control_event_guard.validate_snapshot(
            snapshot,
            ledger_ready_ids=set(),
            ledger_open_ids={"M2-F6", "M1-F4-B"},
            ledger_goal_ids={"M1-F4-B"},
        )

        self.assertEqual(errors, [])

    def test_control_event_guard_rejects_unproven_project_block_on_goal_close(self) -> None:
        snapshot = {
            **self.complete_event_receipt(),
            "ledger_sha256": "abc123",
            "available_slots": 0,
            "ready_packages": [],
            "event_contract": {
                "event_id": "close-goal-blocked",
                "event_type": "Goal closure",
                "primary_task": "M2-F6",
                "candidate_revision": "ledger-abc123",
                "allowed_actions": ["ledger_sync"],
                "allowed_files": ["TASK_LEDGER.md"],
                "terminal_receipt": "M2-F6 Goal closed",
            },
            "event_actions": [{
                "action": "ledger_sync",
                "primary_task": "M2-F6",
                "candidate_revision": "ledger-abc123",
                "files": ["TASK_LEDGER.md"],
                "required_to_close_current_state": True,
            }],
            "goal_rollover": {
                "status": "project_blocked",
                "closed_goal_id": "M2-F6",
                "project_recomputed": True,
                "blocked_scan": {
                    "project_scope_scan": True,
                    "ledger_revision": "abc123",
                    "open_packages": [{
                        "id": "M1-F4-B",
                        "state": "READY",
                        "can_progress": True,
                        "reason": "still executable",
                        "external_condition_id": "",
                    }],
                    "live_tasks": 0,
                    "pending_candidates": 0,
                    "controller_actions": 0,
                },
            },
        }

        errors = control_event_guard.validate_snapshot(
            snapshot,
            ledger_ready_ids=set(),
            ledger_open_ids={"M1-F4-B"},
            ledger_goal_ids=set(),
        )

        self.assertTrue(any("goal_rollover blocked scan" in error for error in errors))
        self.assertTrue(any("can still make progress" in error for error in errors))

    def test_control_event_guard_requires_every_ready_decision(self) -> None:
        snapshot = {
            **self.complete_event_receipt(),
            "ledger_sha256": "abc123",
            "available_slots": 1,
            "ready_packages": [
                {
                    "id": "F1",
                    "decision": "active",
                    "task_id": "task-1",
                    "delivered_ack": True,
                }
            ],
        }

        errors = control_event_guard.validate_snapshot(
            snapshot, ledger_ready_ids={"F1", "F2"}
        )

        self.assertIn("control event omitted READY packages: F2", errors)

    def test_control_event_guard_checks_reviewer_and_rule_acks(self) -> None:
        snapshot = {
            **self.complete_event_receipt(),
            "ledger_sha256": "abc123",
            "available_slots": 0,
            "ready_packages": [],
            "required_reviews": [
                {"id": "R1", "task_id": "review-1", "delivered_ack": False}
            ],
            "rule_update": {
                "revision": "def456",
                "affected_tasks": ["writer-1", "writer-2"],
                "acknowledged_tasks": ["writer-1"],
            },
        }

        errors = control_event_guard.validate_snapshot(
            snapshot,
            ledger_ready_ids=set(),
            required_review_ids={"R1"},
            expected_rule_revision="def456",
            affected_task_ids={"writer-1", "writer-2"},
        )

        self.assertIn("required review R1 requires delivered_ack=true", errors)
        self.assertIn("rule update missing loaded ACK: writer-2", errors)

    def test_control_event_guard_requires_tdd_causal_evidence_for_risk_scoped_review(self) -> None:
        snapshot = {
            **self.complete_event_receipt(), "ledger_sha256": "abc123", "available_slots": 0,
            "ready_packages": [],
            "required_reviews": [{"id": "R1", "task_id": "review-1", "delivered_ack": True, "tdd_required": True}],
        }
        errors = control_event_guard.validate_snapshot(snapshot, ledger_ready_ids=set(), required_review_ids={"R1"})
        self.assertIn("required review R1 requires red_evidence", errors)
        self.assertIn("required review R1 requires candidate_revision", errors)
        self.assertIn("required review R1 requires green_evidence", errors)
        self.assertIn("required review R1 requires red_green_same_case=true", errors)
        self.assertIn("required review R1 requires reviewer_counterexample", errors)
        self.assertIn("required review R1 requires verdict PASS or FAIL", errors)

    def test_control_event_guard_accepts_complete_tdd_causal_review(self) -> None:
        snapshot = {
            **self.complete_event_receipt(), "ledger_sha256": "abc123", "available_slots": 0,
            "ready_packages": [],
            "required_reviews": [{
                "id": "R1", "task_id": "review-1", "delivered_ack": True, "tdd_required": True,
                "red_evidence": "pre-fix test X fails with expected assertion", "candidate_revision": "deadbeef",
                "green_evidence": "candidate test X passes", "red_green_same_case": True,
                "reviewer_counterexample": "boundary X+1 remains rejected", "verdict": "PASS",
            }],
        }
        self.assertEqual(control_event_guard.validate_snapshot(snapshot, ledger_ready_ids=set(), required_review_ids={"R1"}), [])

    def test_review_pass_cannot_close_while_candidate_is_still_only_in_review(self) -> None:
        snapshot = {
            **self.complete_event_receipt(), "ledger_sha256": "abc", "available_slots": 0, "ready_packages": [],
            "required_reviews": [{"id": "R1", "task_id": "review-1", "delivered_ack": True,
                                  "candidate_revision": "candidate-1", "verdict": "PASS"}],
            "candidate_packages": [{"revision": "candidate-1", "worktree": "/repo/wt",
                                     "task_id": "F1", "integration_flow": "server-main",
                                     "decision": "review", "review_task_id": "review-1", "delivered_ack": True}],
            "new_assignments": [],
        }
        errors = control_event_guard.validate_snapshot(
            snapshot, ledger_ready_ids=set(), expected_candidates={"/repo/wt": "candidate-1"}
        )
        self.assertIn("review PASS for candidate-1 requires completed integration or ordered integration queue", errors)

    def test_review_fail_requires_same_candidate_rework_disposition(self) -> None:
        snapshot = {
            **self.complete_event_receipt(), "ledger_sha256": "abc", "available_slots": 0, "ready_packages": [],
            "required_reviews": [{"id": "R1", "task_id": "review-1", "delivered_ack": True,
                                  "candidate_revision": "candidate-1", "verdict": "FAIL"}],
            "candidate_packages": [{"revision": "candidate-1", "worktree": "/repo/wt",
                                     "task_id": "F1", "integration_flow": "server-main",
                                     "decision": "review", "review_task_id": "review-1", "delivered_ack": True}],
            "new_assignments": [],
        }
        errors = control_event_guard.validate_snapshot(
            snapshot, ledger_ready_ids=set(), expected_candidates={"/repo/wt": "candidate-1"}
        )
        self.assertIn("review FAIL for candidate-1 requires rework disposition", errors)

    def test_completed_integration_requires_exact_main_revision_and_regression_evidence(self) -> None:
        base_candidate = {
            "revision": "candidate-1", "worktree": "/repo/wt", "task_id": "F1",
            "integration_flow": "server-main", "decision": "integrate",
            "controller_event_id": "event-1", "integrated_this_event": True,
        }
        for missing_field in ("main_revision", "regression_evidence"):
            candidate = {**base_candidate, "main_revision": "main-2", "regression_evidence": "tests green"}
            candidate.pop(missing_field)
            snapshot = {
                **self.complete_event_receipt(), "ledger_sha256": "abc", "available_slots": 0, "ready_packages": [],
                "required_reviews": [{"id": "R1", "task_id": "review-1", "delivered_ack": True,
                                      "candidate_revision": "candidate-1", "verdict": "PASS"}],
                "candidate_packages": [candidate], "new_assignments": [],
            }
            errors = control_event_guard.validate_snapshot(
                snapshot, ledger_ready_ids=set(), expected_candidates={}, expected_main_revision="main-2"
            )
            self.assertIn(f"candidate-1 integrate requires {missing_field}", errors)

    def test_integrated_candidate_revisions_are_derived_from_git_ancestry(self) -> None:
        from types import SimpleNamespace
        from unittest.mock import patch
        snapshot = {"candidate_packages": [
            {"revision": "good", "decision": "integrate"},
            {"revision": "bad", "decision": "integrate"},
            {"revision": "queued", "decision": "queued"},
        ]}
        def fake_git(_root, *args, **kwargs):
            self.assertEqual(args[:2], ("merge-base", "--is-ancestor"))
            return SimpleNamespace(returncode=0 if args[2] == "good" else 1)
        with patch.object(control_event_guard, "run_git", side_effect=fake_git):
            revisions = control_event_guard.integrated_candidate_revisions(Path("/repo"), snapshot, "main-2")
        self.assertEqual(revisions, {"good"})

    def test_completed_integration_requires_candidate_to_be_ancestor_of_current_main(self) -> None:
        snapshot = {
            **self.complete_event_receipt(), "ledger_sha256": "abc", "available_slots": 0, "ready_packages": [],
            "required_reviews": [{"id": "R1", "task_id": "review-1", "delivered_ack": True,
                                  "candidate_revision": "candidate-1", "verdict": "PASS"}],
            "candidate_packages": [{
                "revision": "candidate-1", "worktree": "/repo/wt", "task_id": "F1",
                "integration_flow": "server-main", "decision": "integrate",
                "controller_event_id": "event-1", "integrated_this_event": True,
                "main_revision": "main-2", "regression_evidence": "current-main regression PASS",
            }],
            "new_assignments": [],
        }
        errors = control_event_guard.validate_snapshot(
            snapshot, ledger_ready_ids=set(), expected_candidates={}, expected_main_revision="main-2",
            expected_integrated_revisions=set(),
        )
        self.assertIn("candidate-1 integrate requires candidate revision to be an ancestor of current main", errors)

    def test_review_pass_accepts_completed_integration_with_current_main_regression(self) -> None:
        snapshot = {
            **self.complete_event_receipt(), "ledger_sha256": "abc", "available_slots": 0, "ready_packages": [],
            "required_reviews": [{"id": "R1", "task_id": "review-1", "delivered_ack": True,
                                  "candidate_revision": "candidate-1", "verdict": "PASS"}],
            "candidate_packages": [{
                "revision": "candidate-1", "worktree": "/repo/wt", "task_id": "F1",
                "integration_flow": "server-main", "decision": "integrate",
                "controller_event_id": "event-1", "integrated_this_event": True,
                "main_revision": "main-2", "regression_evidence": "current-main targeted 16/16 PASS",
                "current_main_verified": True,
                "current_main_verification_evidence": "receipt:current-main-verify",
                "fact_converged": True,
                "fact_convergence_evidence": "receipt:fact-convergence",
                "post_integration_recomputed": True,
                "post_integration_recompute_evidence": "receipt:project-recompute",
            }],
            "new_assignments": [],
        }
        self.assertEqual(control_event_guard.validate_snapshot(
            snapshot, ledger_ready_ids=set(), expected_candidates={}, expected_main_revision="main-2",
            expected_integrated_revisions={"candidate-1"},
        ), [])

    def test_review_pass_accepts_ordered_integration_queue(self) -> None:
        snapshot = {
            **self.complete_event_receipt(), "ledger_sha256": "abc", "available_slots": 0, "ready_packages": [],
            "required_reviews": [{"id": "R1", "task_id": "review-1", "delivered_ack": True,
                                  "candidate_revision": "candidate-1", "verdict": "PASS"}],
            "candidate_packages": [{
                "revision": "candidate-1", "worktree": "/repo/wt", "task_id": "F1",
                "integration_flow": "server-main", "decision": "queued",
                "reason_code": "ordered_integration", "next_checkpoint": "after preceding candidate integrates",
            }],
            "new_assignments": [],
        }
        self.assertEqual(control_event_guard.validate_snapshot(
            snapshot, ledger_ready_ids=set(), expected_candidates={"/repo/wt": "candidate-1"}
        ), [])

    def test_review_fail_accepts_acknowledged_rework(self) -> None:
        snapshot = {
            **self.complete_event_receipt(), "ledger_sha256": "abc", "available_slots": 0, "ready_packages": [],
            "required_reviews": [{"id": "R1", "task_id": "review-1", "delivered_ack": True,
                                  "candidate_revision": "candidate-1", "verdict": "FAIL"}],
            "candidate_packages": [{
                "revision": "candidate-1", "worktree": "/repo/wt", "task_id": "F1",
                "integration_flow": "server-main", "decision": "rework",
                "writer_task_id": "F1-REWORK", "delivered_ack": True,
            }],
            "new_assignments": [],
        }
        self.assertEqual(control_event_guard.validate_snapshot(
            snapshot, ledger_ready_ids=set(), expected_candidates={"/repo/wt": "candidate-1"}
        ), [])

    def test_runnable_hard_defer_requires_machine_evidence_and_checkpoint(self) -> None:
        snapshot = {
            **self.complete_event_receipt(),
            "ledger_sha256": "abc123",
            "available_slots": 1,
            "ready_packages": [{
                "id": "F1",
                "decision": "deferred",
                "reason": "writer owns the same output files",
                "reason_code": "file_conflict",
            }],
        }
        errors = control_event_guard.validate_snapshot(
            snapshot, ledger_ready_ids={"F1"}, derived_runnable_ids={"F1"}
        )
        self.assertTrue(any("requires traceable evidence" in error for error in errors), errors)
        self.assertTrue(any("requires next_checkpoint" in error for error in errors), errors)

        snapshot["ready_packages"][0].update({
            "evidence": "receipt:file-lease-conflict",
            "next_checkpoint": "conflicting assignment terminal receipt",
        })
        self.assertEqual(
            control_event_guard.validate_snapshot(
                snapshot, ledger_ready_ids={"F1"}, derived_runnable_ids={"F1"}
            ),
            [],
        )

    def test_control_event_guard_allows_complete_event(self) -> None:
        snapshot = {
            **self.complete_event_receipt(),
            "ledger_sha256": "abc123",
            "available_slots": 1,
            "ready_packages": [
                {
                    "id": "F1",
                    "decision": "active",
                    "task_id": "task-1",
                    "delivered_ack": True,
                },
                {
                    "id": "F2",
                    "decision": "deferred",
                    "reason": "shares the same output directory as F1",
                    "reason_code": "file_conflict",
                    "evidence": "receipt:file-conflict-f1-f2",
                    "next_checkpoint": "F1 assignment terminal receipt",
                },
            ],
            "required_reviews": [
                {"id": "R1", "task_id": "review-1", "delivered_ack": True}
            ],
            "rule_update": {
                "revision": "def456",
                "affected_tasks": ["writer-1"],
                "acknowledged_tasks": ["writer-1"],
            },
        }

        self.assertEqual(
            control_event_guard.validate_snapshot(
                snapshot,
                ledger_ready_ids={"F1", "F2"},
                expected_ledger_sha256="abc123",
                required_review_ids={"R1"},
                expected_rule_revision="def456",
                affected_task_ids={"writer-1"},
            ),
            [],
        )

    def test_control_event_guard_persists_immutable_machine_cycle_evidence(self) -> None:
        import json

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
            (root / "TASK_LEDGER.md").write_text("# ledger\n", encoding="utf-8")
            subprocess.run(["git", "add", "TASK_LEDGER.md"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=root, check=True, capture_output=True)
            revision = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=root, check=True,
                capture_output=True, text=True,
            ).stdout.strip()
            snapshot = self.complete_event_receipt()
            receipt, receipt_path = control_event_guard.persist_controller_cycle_evidence(
                root,
                snapshot,
                controller_id="controller-1",
                ledger_sha256="ledger-sha",
                main_revision=revision,
                terminal_status="CLOSED",
                validation_errors=[],
            )

            self.assertEqual("controller_cycle_evidence", receipt["record_kind"])
            self.assertEqual("control-event-1", receipt["evidence_id"])
            self.assertEqual("control-event-1", receipt["cycle_id"])
            self.assertEqual("controller-1", receipt["controller_id"])
            self.assertEqual("CLOSED", receipt["terminal_status"])
            self.assertEqual("control event synchronized", receipt["evidence_summary"])
            self.assertEqual(receipt, json.loads(receipt_path.read_text(encoding="utf-8")))

            repeated, repeated_path = control_event_guard.persist_controller_cycle_evidence(
                root,
                snapshot,
                controller_id="controller-1",
                ledger_sha256="ledger-sha",
                main_revision=revision,
                terminal_status="CLOSED",
                validation_errors=[],
            )
            self.assertEqual(receipt, repeated)
            self.assertEqual(receipt_path, repeated_path)

    def test_control_event_guard_refuses_conflicting_cycle_evidence_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
            snapshot = self.complete_event_receipt()
            control_event_guard.persist_controller_cycle_evidence(
                root,
                snapshot,
                controller_id="controller-1",
                ledger_sha256="ledger-sha",
                main_revision="main-1",
                terminal_status="FAILED",
                validation_errors=["candidate review missing"],
            )
            changed = self.complete_event_receipt()
            changed["event_contract"]["terminal_receipt"] = "rewritten receipt"

            with self.assertRaisesRegex(ValueError, "immutable"):
                control_event_guard.persist_controller_cycle_evidence(
                    root,
                    changed,
                    controller_id="controller-1",
                    ledger_sha256="ledger-sha",
                    main_revision="main-1",
                    terminal_status="CLOSED",
                    validation_errors=[],
                )

    def test_control_event_guard_rejects_unproven_risk_clearance_markers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
            incident = self.complete_event_receipt()
            incident["event_contract"]["event_id"] = "incident-1"
            control_event_guard.persist_controller_cycle_evidence(
                root,
                incident,
                controller_id="controller-1",
                ledger_sha256="ledger-sha",
                main_revision="main-1",
                terminal_status="FAILED",
                validation_errors=["candidate review missing"],
            )

            alignment = self.complete_event_receipt()
            alignment["event_contract"].update({
                "event_id": "alignment-1",
                "alignment_for_incident": "incident-1",
            })
            with self.assertRaisesRegex(ValueError, "alignment.*rule ACK"):
                control_event_guard.persist_controller_cycle_evidence(
                    root,
                    alignment,
                    controller_id="controller-1",
                    ledger_sha256="ledger-sha",
                    main_revision="main-1",
                    terminal_status="CLOSED",
                    validation_errors=[],
                )
            alignment["rule_update"] = {
                "revision": "rule-2",
                "affected_tasks": ["writer-1"],
                "acknowledged_tasks": ["writer-1"],
            }
            alignment_receipt, _ = control_event_guard.persist_controller_cycle_evidence(
                root,
                alignment,
                controller_id="controller-1",
                ledger_sha256="ledger-sha",
                main_revision="main-1",
                terminal_status="CLOSED",
                validation_errors=[],
            )
            self.assertEqual("incident-1", alignment_receipt["alignment_for_incident"])

            closure = self.complete_event_receipt()
            closure["event_contract"].update({
                "event_id": "closure-1",
                "post_incident_closure_for": "incident-1",
            })
            with self.assertRaisesRegex(ValueError, "post-incident closure.*L3 or L4"):
                control_event_guard.persist_controller_cycle_evidence(
                    root,
                    closure,
                    controller_id="controller-1",
                    ledger_sha256="ledger-sha",
                    main_revision="main-1",
                    terminal_status="CLOSED",
                    validation_errors=[],
                )
    def test_control_event_guard_classifies_integrated_review_fail_as_major_incident(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
            snapshot = self.complete_event_receipt()
            receipt, _ = control_event_guard.persist_controller_cycle_evidence(
                root,
                snapshot,
                controller_id="controller-1",
                ledger_sha256="ledger-sha",
                main_revision="main-1",
                terminal_status="FAILED",
                validation_errors=["review FAIL for candidate-1 requires rework disposition"],
                integrated_revisions={"candidate-1"},
            )
            self.assertEqual("major", receipt["governance_incident_severity"])

    def test_control_event_guard_rejects_empty_correction_and_unbound_post_closure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
            incident = self.complete_event_receipt()
            incident["event_contract"]["event_id"] = "incident-1"
            control_event_guard.persist_controller_cycle_evidence(
                root,
                incident,
                controller_id="controller-1",
                ledger_sha256="ledger-sha",
                main_revision="main-1",
                terminal_status="FAILED",
                validation_errors=["review FAIL for candidate-1 requires rework disposition"],
                integrated_revisions={"candidate-1"},
            )

            correction = self.complete_event_receipt()
            correction["event_contract"].update({
                "event_id": "correction-1",
                "corrects_incident": "incident-1",
            })
            with self.assertRaisesRegex(ValueError, "correction clearance.*integrated correction"):
                control_event_guard.persist_controller_cycle_evidence(
                    root,
                    correction,
                    controller_id="controller-1",
                    ledger_sha256="ledger-sha",
                    main_revision="main-2",
                    terminal_status="CLOSED",
                    validation_errors=[],
                )

            post_closure = self.complete_event_receipt()
            post_closure["event_contract"].update({
                "event_id": "post-1",
                "post_incident_closure_for": "incident-1",
            })
            with self.assertRaisesRegex(ValueError, "post-incident closure.*correction evidence"):
                control_event_guard.persist_controller_cycle_evidence(
                    root,
                    post_closure,
                    controller_id="controller-1",
                    ledger_sha256="ledger-sha",
                    main_revision="main-3",
                    terminal_status="CLOSED",
                    validation_errors=[],
                    integrated_revisions={"unrelated-candidate"},
                )

    def test_control_event_guard_accepts_evidence_bound_correction_and_post_closure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
            incident = self.complete_event_receipt()
            incident["event_contract"]["event_id"] = "incident-1"
            control_event_guard.persist_controller_cycle_evidence(
                root,
                incident,
                controller_id="controller-1",
                ledger_sha256="ledger-1",
                main_revision="main-1",
                terminal_status="FAILED",
                validation_errors=["review FAIL for bad-revision requires rework disposition"],
                integrated_revisions={"bad-revision"},
            )

            correction = self.complete_event_receipt()
            correction["event_contract"].update({
                "event_id": "correction-1",
                "corrects_incident": "incident-1",
                "correction_revision": "fixed-revision",
            })
            correction["candidate_packages"] = [{
                "revision": "fixed-revision",
                "task_id": "FIX-1",
                "review_task_id": "REVIEW-1",
                "decision": "integrate",
                "integrated_this_event": True,
                "main_revision": "main-2",
                "regression_evidence": "current-main regression PASS",
                "acceptance_evidence": "real environment acceptance PASS",
                "author_task_id": "FIX-1",
            }]
            correction["required_reviews"] = [{
                "id": "review-1",
                "task_id": "REVIEW-1",
                "delivered_ack": True,
                "candidate_revision": "fixed-revision",
                "verdict": "PASS",
            }]
            correction_receipt, _ = control_event_guard.persist_controller_cycle_evidence(
                root,
                correction,
                controller_id="controller-1",
                ledger_sha256="ledger-2",
                main_revision="main-2",
                terminal_status="CLOSED",
                validation_errors=[],
                integrated_revisions={"fixed-revision"},
                ledger_open_ids=set(),
                ledger_task_states={"FIX-1": "DONE", "REVIEW-1": "DONE"},
            )
            self.assertEqual("fixed-revision", correction_receipt["correction_revision"])

            post = self.complete_event_receipt()
            post["event_contract"].update({
                "event_id": "post-1",
                "post_incident_closure_for": "incident-1",
                "depends_on_correction_evidence_id": "correction-1",
            })
            post_receipt, _ = control_event_guard.persist_controller_cycle_evidence(
                root,
                post,
                controller_id="controller-1",
                ledger_sha256="ledger-3",
                main_revision="main-3",
                terminal_status="CLOSED",
                validation_errors=[],
                integrated_revisions={"post-revision"},
            )
            self.assertEqual("L3", post_receipt["outcome_level"])
            self.assertEqual(
                "correction-1", post_receipt["depends_on_correction_evidence_id"]
            )

    def test_control_event_guard_rejects_correction_with_unregistered_task_identities(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
            incident = self.complete_event_receipt()
            incident["event_contract"]["event_id"] = "incident-1"
            control_event_guard.persist_controller_cycle_evidence(
                root,
                incident,
                controller_id="controller-1",
                ledger_sha256="ledger-1",
                main_revision="main-1",
                terminal_status="FAILED",
                validation_errors=["review FAIL for bad-revision requires rework disposition"],
                integrated_revisions={"bad-revision"},
            )

            correction = self.complete_event_receipt()
            correction["event_contract"].update({
                "event_id": "correction-1",
                "corrects_incident": "incident-1",
                "correction_revision": "fixed-revision",
            })
            correction["candidate_packages"] = [{
                "revision": "fixed-revision",
                "task_id": "NONEXISTENT-LEDGER-TASK",
                "review_task_id": "NONEXISTENT-REVIEW-TASK",
                "decision": "integrate",
                "integrated_this_event": True,
                "main_revision": "main-2",
                "regression_evidence": "PASS",
                "acceptance_evidence": "PASS",
                "author_task_id": "NONEXISTENT-LEDGER-TASK",
            }]
            correction["required_reviews"] = [{
                "id": "review-1",
                "task_id": "NONEXISTENT-REVIEW-TASK",
                "delivered_ack": True,
                "candidate_revision": "fixed-revision",
                "verdict": "PASS",
            }]

            correction["candidate_packages"][0]["author_task_id"] = "FAKE-AUTHOR"
            with self.assertRaisesRegex(ValueError, "author task bound"):
                control_event_guard.persist_controller_cycle_evidence(
                    root,
                    correction,
                    controller_id="controller-1",
                    ledger_sha256="ledger-2",
                    main_revision="main-2",
                    terminal_status="CLOSED",
                    validation_errors=[],
                    integrated_revisions={"fixed-revision"},
                    ledger_open_ids=set(),
                    ledger_task_states={"FIX-1": "DONE", "REVIEW-1": "DONE"},
                )
            correction["candidate_packages"][0]["author_task_id"] = (
                "NONEXISTENT-LEDGER-TASK"
            )
            with self.assertRaisesRegex(ValueError, "current ledger"):
                control_event_guard.persist_controller_cycle_evidence(
                    root,
                    correction,
                    controller_id="controller-1",
                    ledger_sha256="ledger-2",
                    main_revision="main-2",
                    terminal_status="CLOSED",
                    validation_errors=[],
                    integrated_revisions={"fixed-revision"},
                    ledger_open_ids=set(),
                    ledger_task_states={"FIX-1": "DONE", "REVIEW-1": "DONE"},
                )

    def test_control_event_guard_fails_closed_when_configured_upstream_is_unresolvable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "config", "branch.main.remote", "origin"], cwd=root, check=True)
            subprocess.run(["git", "config", "branch.main.merge", "refs/heads/main"], cwd=root, check=True)
            incident = self.complete_event_receipt()
            incident["event_contract"]["event_id"] = "incident-1"
            control_event_guard.persist_controller_cycle_evidence(
                root,
                incident,
                controller_id="controller-1",
                ledger_sha256="ledger-1",
                main_revision="main-1",
                terminal_status="FAILED",
                validation_errors=["review FAIL for bad-revision requires rework disposition"],
                integrated_revisions={"bad-revision"},
            )
            correction = self.complete_event_receipt()
            correction["event_contract"].update({
                "event_id": "correction-1",
                "corrects_incident": "incident-1",
                "correction_revision": "fixed-revision",
            })
            correction["candidate_packages"] = [{
                "revision": "fixed-revision",
                "task_id": "FIX-1",
                "review_task_id": "REVIEW-1",
                "decision": "integrate",
                "integrated_this_event": True,
                "main_revision": "main-2",
                "regression_evidence": "PASS",
                "acceptance_evidence": "PASS",
                "author_task_id": "FIX-1",
            }]
            correction["required_reviews"] = [{
                "id": "review-1",
                "task_id": "REVIEW-1",
                "delivered_ack": True,
                "candidate_revision": "fixed-revision",
                "verdict": "PASS",
            }]

            with self.assertRaisesRegex(ValueError, "tracked remote.*unavailable"):
                control_event_guard.persist_controller_cycle_evidence(
                    root,
                    correction,
                    controller_id="controller-1",
                    ledger_sha256="ledger-2",
                    main_revision="main-2",
                    terminal_status="CLOSED",
                    validation_errors=[],
                    integrated_revisions={"fixed-revision"},
                    ledger_open_ids=set(),
                    ledger_task_states={"FIX-1": "DONE", "REVIEW-1": "DONE"},
                )

    def test_control_event_guard_rejects_post_closure_bound_to_legacy_weak_correction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
            incident = self.complete_event_receipt()
            incident["event_contract"]["event_id"] = "incident-1"
            control_event_guard.persist_controller_cycle_evidence(
                root,
                incident,
                controller_id="controller-1",
                ledger_sha256="ledger-1",
                main_revision="main-1",
                terminal_status="FAILED",
                validation_errors=["review FAIL for bad-revision requires rework disposition"],
                integrated_revisions={"bad-revision"},
            )
            legacy_path = control_event_guard.controller_cycle_evidence_path(
                root, "legacy-correction"
            )
            legacy_path.parent.mkdir(parents=True, exist_ok=True)
            legacy_path.write_text(json.dumps({
                "schema_version": 1,
                "record_kind": "controller_cycle_evidence",
                "evidence_id": "legacy-correction",
                "controller_id": "controller-1",
                "cycle_id": "legacy-correction",
                "terminal_status": "CLOSED",
                "corrects_incident": "incident-1",
                "outcome_level": "L3",
                "recorded_at": "2026-09-02T00:01:00+00:00",
            }), encoding="utf-8")
            post = self.complete_event_receipt()
            post["event_contract"].update({
                "event_id": "post-1",
                "post_incident_closure_for": "incident-1",
                "depends_on_correction_evidence_id": "legacy-correction",
            })

            with self.assertRaisesRegex(ValueError, "strong correction"):
                control_event_guard.persist_controller_cycle_evidence(
                    root,
                    post,
                    controller_id="controller-1",
                    ledger_sha256="ledger-2",
                    main_revision="main-2",
                    terminal_status="CLOSED",
                    validation_errors=[],
                    integrated_revisions={"post-revision"},
                )

    def test_registered_control_event_main_writes_machine_cycle_evidence(self) -> None:
        import json
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
            ledger = root / "TASK_LEDGER.md"
            ledger.write_text(
                (SKILL_ROOT / "assets" / "templates" / "TASK_LEDGER.md").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            subprocess.run(["git", "add", "TASK_LEDGER.md"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=root, check=True, capture_output=True)
            snapshot = {
                **self.complete_event_receipt(),
                "ledger_sha256": control_event_guard.ledger_sha256(ledger),
                "available_slots": 1,
                "capacity_projection": {
                    "source": "host_runtime",
                    "evidence": "receipt:host-runtime/capacity-main",
                    "total_slots": 1,
                    "occupied_task_ids": [],
                },
                "ready_packages": [{
                    "id": "INIT-01",
                    "decision": "active",
                    "task_id": "INIT-01-WRITER",
                    "delivered_ack": True,
                }],
                "required_reviews": [],
                "candidate_packages": [],
                "new_assignments": [],
                "controller_actions": [],
                "correction_actions": [],
                "control_loop_receipt": {
                    "scope": "project_wide",
                    "ledger_sha256": control_event_guard.ledger_sha256(ledger),
                    "runnable_ids": ["INIT-01"],
                    "candidate_revisions": [],
                    "controller_action_ids": [],
                    "correction_fingerprints": [],
                    "completed_steps": list(control_event_guard.CONTROL_LOOP_STEPS),
                    "recomputed_after_actions": True,
                },
                "machine_trace": {
                    "turn_id": "turn-1",
                    "tool_use_ids": ["tool-1"],
                    "trace_sha256": "trace-1",
                },
            }
            receipt_file = root / "control-receipt.json"
            receipt_file.write_text(json.dumps(snapshot), encoding="utf-8")
            with patch.object(
                control_event_guard,
                "resolve_controller_trace_session",
                return_value="controller-1",
            ), patch.object(
                control_event_guard,
                "observed_machine_trace",
                return_value=snapshot["machine_trace"],
            ):
                result = control_event_guard.main([
                    str(receipt_file),
                    "--ledger", str(ledger),
                    "--repo", str(root),
                    "--controller-session", "controller-1",
                ])

            self.assertEqual(0, result)
            evidence_path = control_event_guard.controller_cycle_evidence_path(
                root, "control-event-1"
            )
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            self.assertEqual("controller-1", evidence["controller_id"])
            self.assertEqual("CLOSED", evidence["terminal_status"])

    def test_control_event_guard_rejects_fabricated_available_slots(self) -> None:
        snapshot = {
            **self.complete_event_receipt(),
            "ledger_sha256": "abc123",
            "available_slots": 0,
            "capacity_projection": {
                "source": "host_runtime",
                "total_slots": 8,
                "occupied_task_ids": [],
            },
            "ready_packages": [],
        }

        errors = control_event_guard.validate_snapshot(
            snapshot, ledger_ready_ids=set(), ledger_work_in_flight={}
        )

        self.assertTrue(any("available_slots" in error and "machine projection" in error for error in errors))

    def test_control_event_guard_rejects_unrouted_delegated_assignment(self) -> None:
        snapshot = {
            **self.complete_event_receipt(),
            "candidate_packages": [],
            "new_assignments": [{
                "task_id": "SERVER-1",
                "integration_flow": "server-main",
                "execution_mode": "delegated",
                "owned_files": ["apps/server/src/runtime.ts"],
            }],
        }

        errors = control_event_guard.validate_candidate_queue(
            snapshot, expected_candidates={}
        )

        self.assertTrue(any("SERVER-1 delegated assignment requires route" in error for error in errors))

    def test_frontend_owned_files_cannot_claim_backend_route(self) -> None:
        assignment = self.delegated_assignment("WEB-ROUTE-1", "web-main", policy_class="backend")
        assignment["owned_files"] = ["apps/web/src/runtime.ts"]
        snapshot = {
            **self.complete_event_receipt(),
            "candidate_packages": [],
            "new_assignments": [assignment],
        }

        errors = control_event_guard.validate_candidate_queue(snapshot, expected_candidates={})

        self.assertTrue(
            any("WEB-ROUTE-1 route policy_class backend conflicts with derived frontend" in error for error in errors),
            errors,
        )

    def test_backend_owned_files_cannot_claim_frontend_route(self) -> None:
        assignment = self.delegated_assignment("SERVER-ROUTE-1", "server-main", policy_class="frontend")
        assignment["owned_files"] = ["apps/server/src/runtime.ts"]
        snapshot = {
            **self.complete_event_receipt(),
            "candidate_packages": [],
            "new_assignments": [assignment],
        }

        errors = control_event_guard.validate_candidate_queue(snapshot, expected_candidates={})

        self.assertTrue(
            any("SERVER-ROUTE-1 route policy_class frontend conflicts with derived backend" in error for error in errors),
            errors,
        )

    def test_mixed_frontend_backend_owned_files_require_split_before_dispatch(self) -> None:
        assignment = self.delegated_assignment("MIXED-ROUTE-1", "mixed-main", policy_class="frontend")
        assignment["owned_files"] = [
            "apps/web/src/runtime.ts",
            "apps/server/src/runtime.ts",
        ]
        snapshot = {
            **self.complete_event_receipt(),
            "candidate_packages": [],
            "new_assignments": [assignment],
        }

        errors = control_event_guard.validate_candidate_queue(snapshot, expected_candidates={})

        self.assertTrue(
            any("MIXED-ROUTE-1 owned_files span multiple route classes" in error for error in errors),
            errors,
        )

    def test_control_event_guard_rejects_controller_self_write_without_exception(self) -> None:
        snapshot = {
            **self.complete_event_receipt(),
            "candidate_packages": [],
            "new_assignments": [{
                "task_id": "SERVER-1",
                "integration_flow": "server-main",
                "execution_mode": "controller",
                "owned_files": ["apps/server/src/runtime.ts"],
                "route": {
                    "decision": "controller_exception",
                    "policy_class": "backend",
                    "provider": "codex-native",
                    "model": "current",
                    "auth_mode": "host",
                    "policy_source": {"path": "AGENTS.md", "sha256": "abc123"},
                },
            }],
        }

        errors = control_event_guard.validate_candidate_queue(
            snapshot, expected_candidates={}
        )

        self.assertTrue(any("SERVER-1 controller execution requires controller_exception" in error for error in errors))

    def test_control_event_guard_rejects_unproven_safe_fallback(self) -> None:
        snapshot = {
            **self.complete_event_receipt(),
            "candidate_packages": [],
            "new_assignments": [{
                "task_id": "SERVER-1",
                "integration_flow": "server-main",
                "execution_mode": "delegated",
                "owned_files": ["apps/server/src/runtime.ts"],
                "route": {
                    "decision": "safe_fallback",
                    "policy_class": "backend",
                    "provider": "codex-native",
                    "model": "gpt-5.6-terra",
                    "auth_mode": "host",
                    "policy_source": {"path": "AGENTS.md", "sha256": "abc123"},
                    "fallback_from": {
                        "provider": "grok-build",
                        "model": "grok-4.6",
                        "auth_mode": "oauth",
                    },
                },
            }],
        }

        errors = control_event_guard.validate_candidate_queue(
            snapshot, expected_candidates={}
        )

        self.assertTrue(any("SERVER-1 safe fallback requires" in error for error in errors))

    def test_control_event_guard_requires_traceable_capacity_evidence(self) -> None:
        snapshot = {
            **self.complete_event_receipt(),
            "ledger_sha256": "abc123",
            "available_slots": 8,
            "capacity_projection": {
                "source": "host_runtime",
                "total_slots": 8,
                "occupied_task_ids": [],
            },
            "ready_packages": [],
        }

        errors = control_event_guard.validate_snapshot(
            snapshot, ledger_ready_ids=set(), ledger_work_in_flight={}
        )

        self.assertIn("capacity_projection requires traceable evidence", errors)

    def test_control_event_guard_rejects_route_not_declared_by_policy_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            policy = Path(directory) / "AGENTS.md"
            policy.write_text(
                "后端默认 provider=grok-build、model=grok-4.6、auth_mode=oauth。\n",
                encoding="utf-8",
            )
            source = {
                "path": str(policy),
                "sha256": hashlib.sha256(policy.read_bytes()).hexdigest(),
            }
            snapshot = {
                **self.complete_event_receipt(),
                "candidate_packages": [],
                "new_assignments": [{
                    "task_id": "SERVER-1",
                    "integration_flow": "server-main",
                    "execution_mode": "delegated",
                    "owned_files": ["apps/server/src/runtime.ts"],
                    "route": {
                        "decision": "default",
                        "policy_class": "backend",
                        "provider": "kimi-code",
                        "model": "kimi-k3",
                        "auth_mode": "api",
                        "policy_source": source,
                    },
                }],
            }

            errors = control_event_guard.validate_candidate_queue(
                snapshot, expected_candidates={}
            )

        self.assertTrue(any("SERVER-1 route is not declared by policy source" in error for error in errors))

    def test_control_event_guard_accepts_machine_capacity_projection(self) -> None:
        snapshot = {
            **self.complete_event_receipt(),
            "ledger_sha256": "abc123",
            "available_slots": 7,
            "capacity_projection": {
                "source": "host_runtime",
                "evidence": "receipt:host-runtime/capacity-1",
                "total_slots": 8,
                "occupied_task_ids": ["SERVER-ACTIVE"],
            },
            "assignment_liveness": {
                "SERVER-ACTIVE": {
                    "ledger_state": "ACTIVE",
                    "state": "healthy",
                    "reason": "live lease",
                }
            },
            "ready_packages": [],
        }

        self.assertEqual(
            control_event_guard.validate_snapshot(
                snapshot,
                ledger_ready_ids=set(),
                ledger_work_in_flight={"SERVER-ACTIVE": "ACTIVE"},
            ),
            [],
        )

    def test_control_event_guard_accepts_declared_default_route(self) -> None:
        snapshot = {
            **self.complete_event_receipt(),
            "candidate_packages": [],
            "new_assignments": [self.delegated_assignment("SERVER-1", "server-main")],
        }

        self.assertEqual(
            control_event_guard.validate_candidate_queue(snapshot, expected_candidates={}),
            [],
        )

    def test_control_event_guard_accepts_proven_safe_fallback(self) -> None:
        from scripts.assignment_runtime import apply_runtime_receipt
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            policy = Path(directory) / "AGENTS.md"
            policy.write_text(
                "后端默认 provider=grok-build、model=grok-4.6、auth_mode=oauth。" + chr(10)
                + "后端fallback provider=codex-native、model=gpt-5.6-terra、auth_mode=host。" + chr(10),
                encoding="utf-8",
            )
            policy_source = {
                "path": str(policy.resolve()),
                "sha256": hashlib.sha256(policy.read_bytes()).hexdigest(),
            }
            started = {
                "event_type": "assignment_started",
                "assignment_id": "SERVER-1-GROK-PRIOR",
                "task_id": "SERVER-1",
                "agent_id": "grok-prior",
                "provider": "grok-build",
                "model": "grok-4.6",
                "agent_type": "external-grok",
                "session_id": "grok-prior-session",
                "worktree": str(repo),
                "issued_at": "2026-09-04T00:00:00+00:00",
                "attempt": 1,
                "lease_id": "SERVER-1-GROK-PRIOR:attempt:1",
                "event_seq": 1,
                "receipt_id": "SERVER-1-GROK-PRIOR:1:1",
                "assignment_contract_version": 2,
                "side_effect": False,
                "primary_goal": "attempt preferred backend route",
                "success_criteria": ["produce bounded result"],
                "owned_scope": ["apps/server/src/runtime.ts"],
                "strategy": "external-preferred",
                "auth_mode": "oauth",
                "policy_class": "backend",
                "route_decision": "default",
                "route_contract": {
                    "decision": "default",
                    "policy_class": "backend",
                    "provider": "grok-build",
                    "model": "grok-4.6",
                    "auth_mode": "oauth",
                    "policy_source": policy_source,
                },
            }
            apply_runtime_receipt(repo, started)
            terminal = {
                "event_type": "assignment_terminal",
                "assignment_id": "SERVER-1-GROK-PRIOR",
                "task_id": "SERVER-1",
                "agent_id": "grok-prior",
                "provider": "grok-build",
                "model": "grok-4.6",
                "agent_type": "external-grok",
                "session_id": "grok-prior-session",
                "worktree": str(repo),
                "issued_at": "2026-09-04T00:01:00+00:00",
                "attempt": 1,
                "lease_id": "SERVER-1-GROK-PRIOR:attempt:1",
                "event_seq": 2,
                "receipt_id": "SERVER-1-GROK-PRIOR:1:2",
                "terminal_state": "failed",
                "transport_outcome": "failed",
                "delivery_outcome": "unresolved",
                "summary": "preferred backend provider unavailable",
                "evidence": ["receipt:grok/terminal-safe-failure"],
                "artifacts": [],
                "next_action": "use declared safe fallback",
                "retry_class": "provider_exit",
                "side_effect": False,
                "result_unknown": False,
            }
            apply_runtime_receipt(repo, terminal)
            assignment = self.delegated_assignment("SERVER-1", "server-main")
            assignment["route"] = {
                "decision": "safe_fallback",
                "policy_class": "backend",
                "provider": "codex-native",
                "model": "gpt-5.6-terra",
                "auth_mode": "host",
                "policy_source": policy_source,
                "fallback_from": {
                    "provider": "grok-build",
                    "model": "grok-4.6",
                    "auth_mode": "oauth",
                },
                "prior_assignment_id": "SERVER-1-GROK-PRIOR",
                "failure_evidence": "receipt:grok/terminal-safe-failure",
            }
            snapshot = {
                **self.complete_event_receipt(),
                "candidate_packages": [],
                "new_assignments": [assignment],
            }
            self.assertEqual(
                control_event_guard.validate_candidate_queue(
                    snapshot, expected_candidates={}, runtime_repo=repo
                ),
                [],
            )


    def test_control_event_guard_accepts_bounded_controller_exception(self) -> None:
        snapshot = {
            **self.complete_event_receipt(),
            "candidate_packages": [],
            "new_assignments": [{
                "task_id": "SERVER-1",
                "integration_flow": "server-main",
                "execution_mode": "controller",
                "owned_files": ["apps/server/src/runtime.ts"],
                "route": {
                    "decision": "controller_exception",
                    "policy_class": "backend",
                    "provider": "codex-native",
                    "model": "current",
                    "auth_mode": "host",
                    "policy_source": self.route_policy_source(),
                    "default_route": {
                        "provider": "grok-build",
                        "model": "grok-4.6",
                        "auth_mode": "oauth",
                    },
                },
                "controller_exception": {
                    "reason_code": "low_risk_tiny_change",
                    "reason": "one bounded compatibility edit",
                    "stop_condition": "stop after the targeted test passes",
                },
            }],
        }

        self.assertEqual(
            control_event_guard.validate_candidate_queue(snapshot, expected_candidates={}),
            [],
        )

    def test_control_event_guard_rejects_stale_ledger_and_omitted_expectations(self) -> None:
        snapshot = {
            **self.complete_event_receipt(),
            "ledger_sha256": "stale",
            "available_slots": 0,
            "ready_packages": [],
        }

        errors = control_event_guard.validate_snapshot(
            snapshot,
            ledger_ready_ids=set(),
            expected_ledger_sha256="current",
            required_review_ids={"UX-EARLY"},
            expected_rule_revision="rule-2",
            affected_task_ids={"writer-1"},
        )

        self.assertIn("ledger_sha256 does not match the current ledger", errors)
        self.assertIn("control event omitted required reviews: UX-EARLY", errors)
        self.assertIn("control event omitted the declared rule update", errors)

    def test_control_event_guard_rejects_multi_chain_event_journal(self) -> None:
        snapshot = {
            "ledger_sha256": "abc123",
            "available_slots": 0,
            "ready_packages": [],
            "event_contract": {
                "event_id": "event-1",
                "event_type": "candidate integration",
                "primary_task": "F1",
                "candidate_revision": "abc123",
                "allowed_actions": ["review", "integrate", "ledger_sync"],
                "allowed_files": ["app/a.ts", "TASK_LEDGER.md"],
                "terminal_receipt": "F1 integrated and ledger synchronized",
            },
            "event_actions": [
                {
                    "action": "integrate",
                    "primary_task": "F1",
                    "candidate_revision": "abc123",
                    "files": ["app/a.ts"],
                    "required_to_close_current_state": True,
                },
                {
                    "action": "integrate",
                    "primary_task": "F2",
                    "candidate_revision": "def456",
                    "files": ["app/b.ts"],
                    "required_to_close_current_state": True,
                },
            ],
            "terminal_receipt_issued": True,
        }

        errors = control_event_guard.validate_snapshot(
            snapshot, ledger_ready_ids=set()
        )

        self.assertTrue(any("event_actions[1]" in error for error in errors))
        self.assertTrue(any("different primary task" in error for error in errors))

    def test_control_event_guard_rejects_vague_deferral_with_idle_slot(self) -> None:
        snapshot = {
            "ledger_sha256": "abc123",
            "available_slots": 2,
            "ready_packages": [
                {
                    "id": "F1",
                    "decision": "deferred",
                    "reason": "handle in the next event",
                }
            ],
            "event_contract": {
                "event_id": "event-1",
                "event_type": "dispatch",
                "primary_task": "F1",
                "candidate_revision": "ledger-abc123",
                "allowed_actions": ["dispatch", "ledger_sync"],
                "allowed_files": ["TASK_LEDGER.md"],
                "terminal_receipt": "all READY packages have exact decisions",
            },
            "event_actions": [
                {
                    "action": "ledger_sync",
                    "primary_task": "F1",
                    "candidate_revision": "ledger-abc123",
                    "files": ["TASK_LEDGER.md"],
                    "required_to_close_current_state": True,
                }
            ],
            "terminal_receipt_issued": True,
        }

        errors = control_event_guard.validate_snapshot(
            snapshot, ledger_ready_ids={"F1"}
        )

        self.assertTrue(any("reason_code" in error for error in errors))
        self.assertTrue(any("idle dispatch capacity" in error for error in errors))

    def test_control_event_guard_absorbed_candidate_releases_flow_wip(self) -> None:
        snapshot = {
            **self.complete_event_receipt(),
            "ledger_sha256": "abc123",
            "available_slots": 1,
            "ready_packages": [],
            "candidate_packages": [{
                "revision": "candidate-1",
                "worktree": "/repo/worktree-1",
                "task_id": "WEB-1",
                "integration_flow": "web-main",
                "decision": "absorbed",
                "absorbing_revision": "main-2",
                "retention_reason": "retain QA evidence",
            }],
            "new_assignments": [self.delegated_assignment("WEB-2", "web-main", policy_class="frontend")],
        }

        self.assertEqual(
            control_event_guard.validate_snapshot(
                snapshot,
                ledger_ready_ids=set(),
                expected_candidates={"/repo/worktree-1": "candidate-1"},
            ),
            [],
        )

    def test_control_event_guard_parked_candidate_requires_recovery_metadata(self) -> None:
        snapshot = {
            **self.complete_event_receipt(),
            "ledger_sha256": "abc123",
            "available_slots": 0,
            "ready_packages": [],
            "candidate_packages": [{
                "revision": "candidate-1",
                "worktree": "/repo/worktree-1",
                "task_id": "SERVER-1",
                "integration_flow": "server-main",
                "decision": "parked",
            }],
            "new_assignments": [],
        }

        errors = control_event_guard.validate_snapshot(
            snapshot,
            ledger_ready_ids=set(),
            expected_candidates={"/repo/worktree-1": "candidate-1"},
        )

        self.assertTrue(any("parked requires reason_code" in error for error in errors))
        self.assertTrue(any("parked requires wake_condition" in error for error in errors))
        self.assertTrue(any("parked requires retention_reason" in error for error in errors))

    def test_candidate_inventory_excludes_absorbed_retained_worktree(self) -> None:
        import subprocess

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            worktree = Path(directory) / "candidate"
            state_dir = Path(directory) / "state"
            root.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
            (root / "base.txt").write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "add", "base.txt"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "worktree", "add", "-b", "candidate", str(worktree)], cwd=root, check=True, capture_output=True)
            (worktree / "candidate.txt").write_text("candidate\n", encoding="utf-8")
            subprocess.run(["git", "add", "candidate.txt"], cwd=worktree, check=True)
            subprocess.run(["git", "commit", "-m", "candidate"], cwd=worktree, check=True, capture_output=True)
            revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=worktree, check=True, capture_output=True, text=True).stdout.strip()
            main_revision = subprocess.run(["git", "rev-parse", "main"], cwd=root, check=True, capture_output=True, text=True).stdout.strip()

            control_event_guard.record_candidate_lifecycle(
                root,
                [{
                    "revision": revision,
                    "worktree": str(worktree),
                    "decision": "absorbed",
                    "absorbing_revision": main_revision,
                    "retention_reason": "keep real-device QA evidence",
                }],
                state_dir=state_dir,
            )

            self.assertEqual(
                control_event_guard.unmerged_worktree_candidates(root, state_dir=state_dir),
                {},
            )
            inventory = control_event_guard.worktree_candidate_inventory(root, state_dir=state_dir)
            self.assertEqual(inventory["retained"][str(worktree.resolve())]["state"], "absorbed")

    def test_candidate_inventory_reactivates_when_retained_worktree_head_changes(self) -> None:
        import subprocess

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            worktree = Path(directory) / "candidate"
            state_dir = Path(directory) / "state"
            root.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
            (root / "base.txt").write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "add", "base.txt"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "worktree", "add", "-b", "candidate", str(worktree)], cwd=root, check=True, capture_output=True)
            (worktree / "candidate.txt").write_text("v1\n", encoding="utf-8")
            subprocess.run(["git", "add", "candidate.txt"], cwd=worktree, check=True)
            subprocess.run(["git", "commit", "-m", "v1"], cwd=worktree, check=True, capture_output=True)
            old_revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=worktree, check=True, capture_output=True, text=True).stdout.strip()
            main_revision = subprocess.run(["git", "rev-parse", "main"], cwd=root, check=True, capture_output=True, text=True).stdout.strip()
            control_event_guard.record_candidate_lifecycle(root, [{
                "revision": old_revision,
                "worktree": str(worktree),
                "decision": "absorbed",
                "absorbing_revision": main_revision,
                "retention_reason": "keep QA evidence",
            }], state_dir=state_dir)
            (worktree / "candidate.txt").write_text("v2\n", encoding="utf-8")
            subprocess.run(["git", "add", "candidate.txt"], cwd=worktree, check=True)
            subprocess.run(["git", "commit", "-m", "v2"], cwd=worktree, check=True, capture_output=True)
            new_revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=worktree, check=True, capture_output=True, text=True).stdout.strip()

            self.assertEqual(
                control_event_guard.unmerged_worktree_candidates(root, state_dir=state_dir),
                {str(worktree.resolve()): new_revision},
            )

    def test_control_event_guard_cli_persists_retained_candidate_state(self) -> None:
        import json
        import subprocess

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            worktree = Path(directory) / "candidate"
            state_dir = Path(directory) / "state"
            root.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
            ledger = root / "TASK_LEDGER.md"
            ledger.write_text(
                (SKILL_ROOT / "assets" / "templates" / "TASK_LEDGER.md").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            subprocess.run(["git", "add", "TASK_LEDGER.md"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "worktree", "add", "-b", "candidate", str(worktree)], cwd=root, check=True, capture_output=True)
            (worktree / "candidate.txt").write_text("candidate\n", encoding="utf-8")
            subprocess.run(["git", "add", "candidate.txt"], cwd=worktree, check=True)
            subprocess.run(["git", "commit", "-m", "candidate"], cwd=worktree, check=True, capture_output=True)
            revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=worktree, check=True, capture_output=True, text=True).stdout.strip()
            main_revision = subprocess.run(["git", "rev-parse", "main"], cwd=root, check=True, capture_output=True, text=True).stdout.strip()
            snapshot = {
                **self.complete_event_receipt(),
                "ledger_sha256": control_event_guard.ledger_sha256(ledger),
                "available_slots": 1,
                "capacity_projection": {
                    "source": "host_runtime",
                    "evidence": "receipt:host-runtime/capacity-cli",
                    "total_slots": 1,
                    "occupied_task_ids": [],
                },
                "ready_packages": [{"id": "INIT-01", "decision": "active", "task_id": "INIT-01-WRITER", "delivered_ack": True}],
                "candidate_packages": [{
                    "revision": revision,
                    "worktree": str(worktree.resolve()),
                    "task_id": "OLD-CANDIDATE",
                    "integration_flow": "mini-main",
                    "decision": "absorbed",
                    "absorbing_revision": main_revision,
                    "retention_reason": "retain QA evidence",
                }],
                "new_assignments": [],
            }
            receipt = root / "receipt.json"
            receipt.write_text(json.dumps(snapshot), encoding="utf-8")
            previous = control_event_guard.CANDIDATE_STATE_ROOT
            control_event_guard.CANDIDATE_STATE_ROOT = state_dir
            try:
                result = control_event_guard.main([str(receipt), "--ledger", str(ledger), "--repo", str(root)])
                self.assertEqual(result, 0)
                self.assertEqual(control_event_guard.unmerged_worktree_candidates(root, state_dir=state_dir), {})
            finally:
                control_event_guard.CANDIDATE_STATE_ROOT = previous

    def test_control_event_guard_requires_every_live_candidate_decision(self) -> None:
        snapshot = {
            **self.complete_event_receipt(),
            "ledger_sha256": "abc123",
            "available_slots": 1,
            "ready_packages": [],
            "candidate_packages": [],
            "new_assignments": [],
        }

        errors = control_event_guard.validate_snapshot(
            snapshot,
            ledger_ready_ids=set(),
            expected_candidates={"/repo/worktree-1": "candidate-1"},
        )

        self.assertIn("control event omitted unmerged candidates: candidate-1", errors)

    def test_control_event_guard_blocks_same_flow_writer_behind_candidate(self) -> None:
        snapshot = {
            **self.complete_event_receipt(),
            "ledger_sha256": "abc123",
            "available_slots": 2,
            "ready_packages": [],
            "candidate_packages": [
                {
                    "revision": "candidate-1",
                    "worktree": "/repo/worktree-1",
                    "task_id": "WEB-1",
                    "integration_flow": "web-main",
                    "decision": "review",
                    "review_task_id": "review-web-1",
                    "delivered_ack": True,
                }
            ],
            "new_assignments": [
                {"task_id": "WEB-2", "integration_flow": "web-main"}
            ],
        }

        errors = control_event_guard.validate_snapshot(
            snapshot,
            ledger_ready_ids=set(),
            expected_candidates={"/repo/worktree-1": "candidate-1"},
        )

        self.assertTrue(any("WEB-2 cannot start" in error for error in errors))

    def test_control_event_guard_accepts_parallel_flow_behind_candidate(self) -> None:
        snapshot = {
            **self.complete_event_receipt(),
            "ledger_sha256": "abc123",
            "available_slots": 2,
            "ready_packages": [],
            "candidate_packages": [
                {
                    "revision": "candidate-1",
                    "worktree": "/repo/worktree-1",
                    "task_id": "WEB-1",
                    "integration_flow": "web-main",
                    "decision": "integrate",
                    "controller_event_id": "integrate-web-1",
                    "integrated_this_event": True,
                    "main_revision": "main-after-web-1",
                    "regression_evidence": "current-main targeted regression PASS",
                "current_main_verified": True,
                "current_main_verification_evidence": "receipt:current-main-verify",
                "fact_converged": True,
                "fact_convergence_evidence": "receipt:fact-convergence",
                "post_integration_recomputed": True,
                "post_integration_recompute_evidence": "receipt:project-recompute",
                }
            ],
            "new_assignments": [
                self.delegated_assignment("MINI-1", "mini-main", policy_class="frontend")
            ],
        }

        self.assertEqual(
            control_event_guard.validate_snapshot(
                snapshot,
                ledger_ready_ids=set(),
                expected_candidates={"/repo/worktree-1": "candidate-1"},
            ),
            [],
        )

    def test_control_event_guard_requires_exact_checkpoint_for_queued_candidate(self) -> None:
        snapshot = {
            **self.complete_event_receipt(),
            "ledger_sha256": "abc123",
            "available_slots": 0,
            "ready_packages": [],
            "candidate_packages": [
                {
                    "revision": "candidate-1",
                    "worktree": "/repo/worktree-1",
                    "task_id": "SERVER-1",
                    "integration_flow": "server-main",
                    "decision": "queued",
                    "reason_code": "capacity",
                }
            ],
            "new_assignments": [],
        }

        errors = control_event_guard.validate_snapshot(
            snapshot,
            ledger_ready_ids=set(),
            expected_candidates={"/repo/worktree-1": "candidate-1"},
        )

        self.assertIn("candidate-1 queued requires next_checkpoint", errors)


    def test_control_event_guard_requires_derived_runnable_pending_package(self) -> None:
        snapshot = {
            **self.complete_event_receipt(),
            "ledger_sha256": "abc123",
            "available_slots": 1,
            "ready_packages": [],
        }
        errors = control_event_guard.validate_snapshot(
            snapshot, ledger_ready_ids=set(), derived_runnable_ids={"PENDING-RUNNABLE"}
        )
        self.assertTrue(any("derived runnable" in error and "PENDING-RUNNABLE" in error for error in errors))

    def test_control_event_guard_accepts_derived_runnable_pending_package_decision(self) -> None:
        snapshot = {
            **self.complete_event_receipt(),
            "ledger_sha256": "abc123",
            "available_slots": 1,
            "ready_packages": [{"id": "PENDING-RUNNABLE", "decision": "active", "task_id": "assignment-1", "delivered_ack": True}],
        }
        errors = control_event_guard.validate_snapshot(
            snapshot, ledger_ready_ids=set(), derived_runnable_ids={"PENDING-RUNNABLE"}
        )
        self.assertEqual(errors, [])

    def _fairness_ledger(self, rows: list[tuple[str, str, str]]) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        ledger = Path(directory.name) / "TASK_LEDGER.md"
        q = chr(96)
        body = chr(10).join(
            f"| {q}{task_id}{q} | {q}{status}{q} | owner | {next_action} |"
            for task_id, status, next_action in rows
        )
        ledger.write_text(
            chr(10).join([
                "# Ledger",
                "",
                "- 当前 Goal：project-wide fairness",
                "- 下一可见检查点：project-wide dispatch",
                "- 当前阻塞：none",
                "- 规则版本：test",
                "",
                "| ID | 状态 | 负责人 | 下一步 |",
                "|---|---|---|---|",
                body,
                "",
            ]),
            encoding="utf-8",
        )
        return ledger
    def test_project_wide_projection_web_active_verify_do_not_starve_mini_runnables(self) -> None:
        ledger = self._fairness_ledger([
            ("WEB-ACTIVE", "ACTIVE", "continue web writer"),
            ("WEB-VERIFY", "VERIFY", "review web candidate"),
            ("MINI-READY", "READY", "dispatch mini writer"),
            ("MINI-PENDING", "PENDING", "implement independent mini slice"),
        ])
        projection = control_event_guard.project_wide_dispatch_projection(ledger)
        self.assertEqual(
            projection["derived_runnable_ids"],
            {"MINI-READY", "MINI-PENDING"},
        )
        snapshot = {
            **self.complete_event_receipt(),
            "ledger_sha256": "abc123",
            "available_slots": 2,
            "ready_packages": [
                {"id": "MINI-READY", "decision": "active", "task_id": "MINI-READY-A1", "delivered_ack": True},
                {"id": "MINI-PENDING", "decision": "active", "task_id": "MINI-PENDING-A1", "delivered_ack": True},
            ],
        }
        errors = control_event_guard.validate_snapshot(
            snapshot,
            ledger_ready_ids=set(projection["ready_ids"]),
            derived_runnable_ids=set(projection["derived_runnable_ids"]),
        )
        self.assertEqual(errors, [])

    def test_project_wide_projection_mini_active_does_not_starve_server_or_web(self) -> None:
        ledger = self._fairness_ledger([
            ("MINI-ACTIVE", "ACTIVE", "continue mini writer"),
            ("SERVER-READY", "READY", "dispatch server writer"),
            ("WEB-PENDING", "PENDING", "implement independent web slice"),
        ])
        projection = control_event_guard.project_wide_dispatch_projection(ledger)
        self.assertEqual(
            projection["derived_runnable_ids"],
            {"SERVER-READY", "WEB-PENDING"},
        )
        snapshot = {
            **self.complete_event_receipt(),
            "ledger_sha256": "abc123",
            "available_slots": 2,
            "ready_packages": [
                {"id": "SERVER-READY", "decision": "active", "task_id": "SERVER-A1", "delivered_ack": True},
            ],
        }
        errors = control_event_guard.validate_snapshot(
            snapshot,
            ledger_ready_ids=set(projection["ready_ids"]),
            derived_runnable_ids=set(projection["derived_runnable_ids"]),
        )
        self.assertTrue(
            any("omitted derived runnable packages" in error and "WEB-PENDING" in error for error in errors),
            errors,
        )

    def test_runtime_terminal_active_row_does_not_consume_dispatch_capacity(self) -> None:
        from unittest.mock import patch

        ledger = self._fairness_ledger([
            ("STALE-ACTIVE", "ACTIVE", "recover stale assignment"),
            ("SERVER-READY", "READY", "dispatch server writer"),
        ])
        projection = control_event_guard.project_wide_dispatch_projection(ledger)
        with patch("scripts.assignment_runtime.load_runtime_state", return_value={
            "leases": {
                "A-OLD": {
                    "task_id": "STALE-ACTIVE",
                    "attempt": 1,
                    "terminal_state": "failed",
                }
            }
        }):
            occupied = control_event_guard.runtime_occupied_task_ids(
                ledger.parent, dict(projection["work_in_flight"])
            )
        self.assertEqual(occupied, set())

        snapshot = {
            **self.complete_event_receipt(),
            "ledger_sha256": "abc123",
            "available_slots": 1,
            "capacity_projection": {
                "source": "host_runtime",
                "evidence": "receipt:capacity-runtime",
                "total_slots": 1,
                "occupied_task_ids": [],
            },
            "ready_packages": [{
                "id": "SERVER-READY",
                "decision": "deferred",
                "reason": "capacity is full",
                "reason_code": "capacity",
            }],
        }
        errors = control_event_guard.validate_snapshot(
            snapshot,
            ledger_ready_ids=set(projection["ready_ids"]),
            derived_runnable_ids=set(projection["derived_runnable_ids"]),
            ledger_work_in_flight=dict(projection["work_in_flight"]),
            expected_runtime_occupied_task_ids=occupied,
        )
        self.assertTrue(
            any("idle dispatch capacity remains" in error for error in errors),
            errors,
        )
        self.assertFalse(
            any("occupied tasks do not match" in error for error in errors),
            errors,
        )

    def test_project_wide_fairness_requires_parallel_dispatch_when_capacity_exists(self) -> None:
        runnable = {"WEB-READY", "MINI-READY", "SERVER-READY"}
        snapshot = {
            **self.complete_event_receipt(),
            "ledger_sha256": "abc123",
            "available_slots": 3,
            "ready_packages": [
                {"id": "WEB-READY", "decision": "active", "task_id": "WEB-A1", "delivered_ack": True},
                {
                    "id": "MINI-READY",
                    "decision": "deferred",
                    "reason": "leave mini for a later controller turn",
                    "reason_code": "capacity",
                },
                {
                    "id": "SERVER-READY",
                    "decision": "deferred",
                    "reason": "leave server for a later controller turn",
                    "reason_code": "capacity",
                },
            ],
        }
        errors = control_event_guard.validate_snapshot(
            snapshot, ledger_ready_ids=runnable, derived_runnable_ids=runnable
        )
        self.assertTrue(
            any("idle dispatch capacity remains for project-wide runnable packages" in error for error in errors),
            errors,
        )

        snapshot["ready_packages"] = [
            {"id": task_id, "decision": "active", "task_id": f"{task_id}-A1", "delivered_ack": True}
            for task_id in sorted(runnable)
        ]
        self.assertEqual(
            control_event_guard.validate_snapshot(
                snapshot, ledger_ready_ids=runnable, derived_runnable_ids=runnable
            ),
            [],
        )

    def test_local_hard_defer_still_fills_other_nonconflicting_capacity(self) -> None:
        runnable = {"WEB-REVIEW-WAIT", "MINI-READY"}
        snapshot = {
            **self.complete_event_receipt(),
            "ledger_sha256": "abc123",
            "available_slots": 2,
            "ready_packages": [{
                "id": "WEB-REVIEW-WAIT",
                "decision": "deferred",
                "reason": "candidate must integrate in review order before this writer can touch shared files",
                "reason_code": "ordered_integration",
                "evidence": "receipt:review-order-web",
                "next_checkpoint": "review integration terminal receipt",
            }, {
                "id": "MINI-READY",
                "decision": "active",
                "task_id": "MINI-READY-A1",
                "delivered_ack": True,
            }],
        }
        self.assertEqual(
            control_event_guard.validate_snapshot(
                snapshot, ledger_ready_ids=runnable, derived_runnable_ids=runnable
            ),
            [],
        )

        snapshot["ready_packages"][1] = {
            "id": "MINI-READY",
            "decision": "deferred",
            "reason": "leave mini idle while web waits",
            "reason_code": "capacity",
        }
        errors = control_event_guard.validate_snapshot(
            snapshot, ledger_ready_ids=runnable, derived_runnable_ids=runnable
        )
        self.assertTrue(
            any("idle dispatch capacity remains" in error and "MINI-READY" in error for error in errors),
            errors,
        )

    def test_pending_dependency_closure_dynamically_enters_project_wide_runnable_projection(self) -> None:
        ledger = self._fairness_ledger([
            ("WEB-BASE", "ACTIVE", "finish base contract"),
            ("MINI-SPLIT", "PENDING", "after WEB-BASE implement mini split"),
            ("SERVER-READY", "READY", "dispatch server writer"),
        ])
        before = control_event_guard.project_wide_dispatch_projection(ledger)
        self.assertEqual(before["derived_runnable_ids"], {"SERVER-READY"})
        self.assertIn("MINI-SPLIT", before["runnable_exclusions"])

        q = chr(96)
        ledger.write_text(
            ledger.read_text(encoding="utf-8").replace(
                f"| {q}WEB-BASE{q} | {q}ACTIVE{q} |",
                f"| {q}WEB-BASE{q} | {q}DONE{q} |",
            ),
            encoding="utf-8",
        )
        after = control_event_guard.project_wide_dispatch_projection(ledger)
        self.assertEqual(after["derived_runnable_ids"], {"MINI-SPLIT", "SERVER-READY"})

        prior_snapshot = {
            "head": "h1", "ledger_sha256": "l1", "worktree_status_sha256": "s1",
            "ready_ids": ["SERVER-READY"], "runnable_ids": ["SERVER-READY"],
            "candidate_revisions": [], "assignment_liveness": {},
        }
        next_snapshot = {
            **prior_snapshot,
            "ledger_sha256": "l2",
            "runnable_ids": ["MINI-SPLIT", "SERVER-READY"],
        }
        triggers = lifecycle_hook.lifecycle_triggers(
            next_snapshot, {"snapshot": prior_snapshot}
        )
        self.assertIn("RUNNABLE:MINI-SPLIT", triggers)

    def test_project_wide_fairness_rejects_more_active_dispatches_than_capacity(self) -> None:
        runnable = {"WEB-READY", "MINI-READY"}
        snapshot = {
            **self.complete_event_receipt(),
            "ledger_sha256": "abc123",
            "available_slots": 1,
            "ready_packages": [
                {"id": "WEB-READY", "decision": "active", "task_id": "WEB-A1", "delivered_ack": True},
                {"id": "MINI-READY", "decision": "active", "task_id": "MINI-A1", "delivered_ack": True},
            ],
        }
        errors = control_event_guard.validate_snapshot(
            snapshot, ledger_ready_ids=runnable, derived_runnable_ids=runnable
        )
        self.assertIn("active dispatch decisions exceed available capacity: 2 > 1", errors)

    def test_stop_without_current_turn_control_loop_receipt_fails_closed_even_when_idle(self) -> None:
        snapshot = {
            "head": "abc",
            "ledger_sha256": "ledger",
            "worktree_status_sha256": "status",
            "ready_ids": [],
            "runnable_ids": [],
            "candidate_revisions": [],
            "ledger_errors": [],
            "assignment_liveness": {},
            "controller_corrections": [],
            "control_loop_required": True,
            "rule_handshake": {"state": "current", "blocking": False},
        }
        output, _ = lifecycle_hook.evaluate_event(
            {
                "hook_event_name": "Stop",
                "session_id": "controller-1",
                "turn_id": "turn-1",
            },
            snapshot=snapshot,
            prior_state={
                "active_turn_id": "turn-1",
                "pending_control_event": False,
                "must_yield": False,
                "snapshot": snapshot,
            },
        )
        self.assertEqual(output.get("decision"), "block")
        self.assertIn("control", str(output.get("reason", "")).lower())
        self.assertIn("receipt", str(output.get("reason", "")).lower())

    def test_failed_control_cycle_generates_executable_controller_correction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
            snapshot = self.complete_event_receipt()
            snapshot["event_contract"]["event_id"] = "deviation-1"
            receipt, _ = control_event_guard.persist_controller_cycle_evidence(
                root,
                snapshot,
                controller_id="controller-1",
                ledger_sha256="ledger-1",
                main_revision="main-1",
                terminal_status="FAILED",
                validation_errors=[
                    "control event omitted derived runnable packages: MINI-READY"
                ],
            )
            deviations = receipt["controller_deviations"]
            self.assertEqual(len(deviations), 1)
            deviation = deviations[0]
            self.assertEqual(deviation["deviation_code"], "project_runnable_omission")
            self.assertEqual(deviation["responsibility"], "controller")
            self.assertEqual(deviation["level"], "L3")
            self.assertTrue(deviation["correction"]["mandatory"])
            self.assertTrue(deviation["correction"]["executable"])
            self.assertEqual(
                deviation["correction"]["projection"],
                "canonical_project_control",
            )
            self.assertTrue(deviation["fingerprint"])

    def test_direct_cycle_persistence_cannot_fabricate_generic_correction_closure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
            incident = self.complete_event_receipt()
            incident["event_contract"]["event_id"] = "generic-deviation-1"
            failed, _ = control_event_guard.persist_controller_cycle_evidence(
                root,
                incident,
                controller_id="controller-1",
                ledger_sha256="ledger-1",
                main_revision="main-1",
                terminal_status="FAILED",
                validation_errors=["control event omitted derived runnable packages: MINI-READY"],
            )
            correction = failed["controller_deviations"][0]
            forged = self.complete_event_receipt()
            forged["event_contract"]["event_id"] = "forged-close-1"
            forged["correction_actions"] = [{
                "fingerprint": correction["fingerprint"],
                "decision": "corrected",
                "action": correction["correction"]["action"],
                "executed_by": "controller-1",
                "verified_by": "controller-1",
            }]
            with self.assertRaisesRegex(ValueError, "generic correction closure failed"):
                control_event_guard.persist_controller_cycle_evidence(
                    root,
                    forged,
                    controller_id="controller-1",
                    ledger_sha256="ledger-2",
                    main_revision="main-2",
                    terminal_status="CLOSED",
                    validation_errors=[],
                )
            self.assertEqual(
                [item["fingerprint"] for item in control_event_guard.open_controller_corrections(root, "controller-1")],
                [correction["fingerprint"]],
            )

    def test_same_controller_deviation_fingerprint_escalates_on_recurrence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
            for index in (1, 2):
                snapshot = self.complete_event_receipt()
                snapshot["event_contract"]["event_id"] = f"deviation-{index}"
                receipt, _ = control_event_guard.persist_controller_cycle_evidence(
                    root,
                    snapshot,
                    controller_id="controller-1",
                    ledger_sha256=f"ledger-{index}",
                    main_revision=f"main-{index}",
                    terminal_status="FAILED",
                    validation_errors=[
                        "control event omitted derived runnable packages: MINI-READY"
                    ],
                )
                if index == 1:
                    first = receipt["controller_deviations"][0]
                else:
                    second = receipt["controller_deviations"][0]
            self.assertEqual(first["fingerprint"], second["fingerprint"])
            self.assertEqual(first["level"], "L3")
            self.assertEqual(second["level"], "L4")
            self.assertEqual(second["recurrence_count"], 2)

    def test_continuation_debt_fingerprint_escalates_through_existing_recurrence_rules(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
            recorded = []
            for index in (1, 2):
                snapshot = self.complete_event_receipt()
                snapshot["event_contract"]["event_id"] = f"continuation-debt-{index}"
                receipt, _ = control_event_guard.persist_controller_cycle_evidence(
                    root,
                    snapshot,
                    controller_id="controller-1",
                    ledger_sha256=f"ledger-{index}",
                    main_revision=f"main-{index}",
                    terminal_status="FAILED",
                    validation_errors=[
                        "continuation debt remains open: integration:deadbeef"
                    ],
                )
                recorded.append(receipt["controller_deviations"][0])
            first, second = recorded
            self.assertEqual(first["deviation_code"], "CONTINUATION_DEBT_NOT_CLEARED")
            self.assertEqual(first["fingerprint"], second["fingerprint"])
            self.assertEqual(first["level"], "L3")
            self.assertEqual(second["level"], "L4")
            self.assertEqual(second["recurrence_count"], 2)
            self.assertTrue(
                second["correction"]["requires_unique_controller_handoff"]
            )

    def test_open_correction_enters_lifecycle_projection_and_blocks_stop(self) -> None:
        snapshot = {
            "head": "abc",
            "ledger_sha256": "ledger",
            "worktree_status_sha256": "status",
            "ready_ids": [],
            "runnable_ids": [],
            "candidate_revisions": [],
            "ledger_errors": [],
            "assignment_liveness": {},
            "controller_corrections": [
                {
                    "fingerprint": "fp-1",
                    "level": "L2",
                    "deviation_code": "unconsumed_candidate",
                    "correction": {
                        "mandatory": True,
                        "executable": True,
                        "action": "consume_candidate_and_recompute",
                    },
                }
            ],
            "rule_handshake": {"state": "current", "blocking": False},
        }
        triggers = lifecycle_hook.lifecycle_triggers(snapshot, None)
        self.assertIn("CORRECTION:fp-1", triggers)
        output, _ = lifecycle_hook.evaluate_event(
            {"hook_event_name": "Stop", "session_id": "controller-1", "turn_id": "turn-1"},
            snapshot=snapshot,
            prior_state={
                "active_turn_id": "turn-1",
                "pending_control_event": True,
                "triggers": ["CORRECTION:fp-1"],
                "snapshot": snapshot,
            },
        )
        self.assertEqual(output.get("decision"), "block")
        self.assertIn("CORRECTION:fp-1", str(output.get("reason", "")))

    def test_control_loop_receipt_requires_project_wide_projection_and_all_controller_actions(self) -> None:
        snapshot = {
            **self.complete_event_receipt(),
            "ledger_sha256": "ledger-1",
            "available_slots": 1,
            "ready_packages": [
                {
                    "id": "MINI-READY",
                    "decision": "active",
                    "task_id": "MINI-READY-A1",
                    "delivered_ack": True,
                }
            ],
            "controller_actions": [],
        }
        errors = control_event_guard.validate_snapshot(
            snapshot,
            ledger_ready_ids={"MINI-READY"},
            derived_runnable_ids={"MINI-READY"},
            expected_ledger_sha256="ledger-1",
            expected_candidate_revisions={"cand-1"},
            expected_controller_action_ids={"candidate:cand-1", "recovery:SERVER-ACTIVE"},
            expected_corrections=[],
            require_control_loop_receipt=True,
        )
        self.assertTrue(any("control_loop_receipt" in error for error in errors), errors)
        self.assertTrue(any("controller action" in error for error in errors), errors)

    def test_live_candidate_with_stale_matching_lease_requires_control_plane_reconcile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
            common = Path(subprocess.run(
                ["git", "rev-parse", "--git-common-dir"], cwd=root, check=True, capture_output=True, text=True
            ).stdout.strip())
            if not common.is_absolute():
                common = (root / common).resolve()
            state_dir = common / "adaptive-delivery"
            state_dir.mkdir(parents=True, exist_ok=True)
            runtime = {
                "schema_version": 2,
                "lineages": {},
                "leases": {
                    "assignment-1": {
                        "assignment_id": "assignment-1",
                        "task_id": "TASK-1",
                        "worktree": "/tmp/candidate",
                        "attempt": 1,
                        "terminal_state": None,
                        "baseline_head": "base",
                        "last_observed_head": "base",
                        "candidate_revision": None,
                        "last_heartbeat_at": "2099-01-01T00:00:00+00:00",
                        "lease_expires_at": "2099-01-01T01:00:00+00:00",
                        "last_progress_at": "2099-01-01T00:00:00+00:00",
                        "progress_deadline_at": "2099-01-01T01:00:00+00:00",
                    }
                },
            }
            (state_dir / "runtime-assignments.json").write_text(json.dumps(runtime), encoding="utf-8")
            actions = control_event_guard.canonical_controller_action_projection(
                root,
                controller_id="controller-1",
                candidates={"/tmp/candidate": "candidate-abc"},
                required_review_ids=set(),
                work_in_flight={"TASK-1": "ACTIVE"},
                corrections=[],
            )
            action_id = "control_plane_reconcile:assignment-1"
            self.assertIn(action_id, actions)
            self.assertEqual(actions[action_id]["expected_candidate_revision"], "candidate-abc")
            self.assertEqual(actions[action_id]["observed_runtime_head"], "base")

            runtime["leases"]["assignment-1"]["last_observed_head"] = "candidate-abc"
            runtime["leases"]["assignment-1"]["candidate_revision"] = "candidate-abc"
            (state_dir / "runtime-assignments.json").write_text(json.dumps(runtime), encoding="utf-8")
            actions = control_event_guard.canonical_controller_action_projection(
                root,
                controller_id="controller-1",
                candidates={"/tmp/candidate": "candidate-abc"},
                required_review_ids=set(),
                work_in_flight={"TASK-1": "ACTIVE"},
                corrections=[],
            )
            self.assertNotIn(action_id, actions)

    def test_active_writer_does_not_hide_immediate_controller_actions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
            correction = {
                "fingerprint": "fp-live",
                "level": "L2",
                "deviation_code": "unconsumed_candidate",
                "correction": {
                    "mandatory": True,
                    "executable": True,
                    "action": "consume_candidate_integration_acceptance_and_recompute",
                },
            }
            actions = control_event_guard.canonical_controller_action_projection(
                root,
                controller_id="controller-1",
                candidates={"/tmp/candidate": "candidate-abc"},
                required_review_ids={"review-1"},
                work_in_flight={"WEB-ACTIVE": "ACTIVE"},
                corrections=[correction],
            )
            self.assertEqual(
                set(actions),
                {
                    "candidate:candidate-abc",
                    "review:review-1",
                    "recovery:WEB-ACTIVE",
                    "correction:fp-live",
                },
            )
            snapshot = {
                **self.complete_event_receipt(),
                "ledger_sha256": "ledger-live",
                "available_slots": 0,
                "ready_packages": [],
                "controller_actions": [],
                "correction_actions": [],
                "control_loop_receipt": {
                    "scope": "project_wide",
                    "ledger_sha256": "ledger-live",
                    "runnable_ids": [],
                    "candidate_revisions": ["candidate-abc"],
                    "controller_action_ids": sorted(actions),
                    "correction_fingerprints": ["fp-live"],
                    "completed_steps": list(control_event_guard.CONTROL_LOOP_STEPS),
                    "recomputed_after_actions": True,
                },
            }
            errors = control_event_guard.validate_snapshot(
                snapshot,
                ledger_ready_ids=set(),
                derived_runnable_ids=set(),
                expected_ledger_sha256="ledger-live",
                expected_candidate_revisions={"candidate-abc"},
                expected_controller_action_ids=set(actions),
                expected_corrections=[correction],
                require_control_loop_receipt=True,
            )
            self.assertTrue(any("omitted controller actions" in error for error in errors), errors)
            self.assertTrue(any("mandatory correction" in error for error in errors), errors)

    def test_control_loop_receipt_rejects_missing_or_reordered_control_steps(self) -> None:
        snapshot = {
            **self.complete_event_receipt(),
            "ledger_sha256": "ledger-1",
            "available_slots": 0,
            "ready_packages": [],
            "controller_actions": [],
            "correction_actions": [],
            "control_loop_receipt": {
                "scope": "project_wide",
                "ledger_sha256": "ledger-1",
                "runnable_ids": [],
                "candidate_revisions": [],
                "controller_action_ids": [],
                "correction_fingerprints": [],
                "completed_steps": list(reversed(control_event_guard.CONTROL_LOOP_STEPS)),
                "recomputed_after_actions": True,
            },
        }
        errors = control_event_guard.validate_snapshot(
            snapshot,
            ledger_ready_ids=set(),
            derived_runnable_ids=set(),
            expected_ledger_sha256="ledger-1",
            expected_candidate_revisions=set(),
            expected_controller_action_ids=set(),
            expected_corrections=[],
            require_control_loop_receipt=True,
        )
        self.assertTrue(any("completed_steps" in error for error in errors), errors)

    def test_unfinished_correction_prevents_control_cycle_closure(self) -> None:
        correction = {
            "fingerprint": "fp-correction",
            "level": "L2",
            "deviation_code": "unconsumed_reviewer",
            "responsibility": "controller",
            "correction": {
                "mandatory": True,
                "executable": True,
                "action": "consume_reviewer_and_recompute",
            },
        }
        snapshot = {
            **self.complete_event_receipt(),
            "ledger_sha256": "ledger-1",
            "available_slots": 0,
            "ready_packages": [],
            "controller_actions": [],
            "control_loop_receipt": {
                "scope": "project_wide",
                "ledger_sha256": "ledger-1",
                "runnable_ids": [],
                "candidate_revisions": [],
                "controller_action_ids": ["correction:fp-correction"],
                "correction_fingerprints": ["fp-correction"],
                "recomputed_after_actions": True,
            },
            "correction_actions": [],
        }
        errors = control_event_guard.validate_snapshot(
            snapshot,
            ledger_ready_ids=set(),
            derived_runnable_ids=set(),
            expected_ledger_sha256="ledger-1",
            expected_candidate_revisions=set(),
            expected_controller_action_ids={"correction:fp-correction"},
            expected_corrections=[correction],
            require_control_loop_receipt=True,
        )
        self.assertTrue(
            any("mandatory correction" in error and "fp-correction" in error for error in errors),
            errors,
        )

    def test_reviewer_terminal_recomputes_unrelated_project_runnable(self) -> None:
        snapshot = {
            "head": "abc",
            "ledger_sha256": "ledger",
            "worktree_status_sha256": "status",
            "ready_ids": ["MINI-READY"],
            "runnable_ids": ["MINI-READY"],
            "candidate_revisions": ["deadbeef"],
            "ledger_errors": [],
            "assignment_liveness": {},
            "controller_corrections": [],
            "rule_handshake": {"state": "current", "blocking": False},
        }
        _, state = lifecycle_hook.evaluate_event(
            {
                "hook_event_name": "SubagentStop",
                "session_id": "controller-1",
                "agent_id": "web-reviewer-1",
                "terminal_receipt": "/tmp/reviewer-terminal.json",
            },
            snapshot=snapshot,
            prior_state={"pending_control_event": False, "snapshot": snapshot},
        )
        self.assertIn("READY:MINI-READY", state["triggers"])
        self.assertIn("CANDIDATE:deadbeef", state["triggers"])
        self.assertIn("subagent_stopped:web-reviewer-1", state["triggers"])

    def test_pending_parent_partial_dependency_creates_dynamic_runnable_slice(self) -> None:
        from scripts.controller_state import derive_runnable_tasks
        records = [
            {"id": "WEB-BASE", "status": "DONE", "next_action": "closed"},
            {"id": "SERVER-BASE", "status": "ACTIVE", "next_action": "continue"},
            {
                "id": "MINI-PARENT",
                "status": "PENDING",
                "next_action": (
                    "after WEB-BASE implement mini shell; "
                    "after SERVER-BASE wire server synchronization"
                ),
            },
        ]
        projection = derive_runnable_tasks(records)
        self.assertIn("MINI-PARENT", projection["runnable_task_ids"])
        slices = projection["derived_slices"]["MINI-PARENT"]
        self.assertEqual(len(slices), 1)
        self.assertIn("mini shell", slices[0]["next_action"])
        self.assertEqual(slices[0]["closed_dependencies"], ["WEB-BASE"])
        self.assertEqual(slices[0]["open_dependencies"], [])

    def test_successful_control_receipt_closes_the_event_and_latches_yield(self) -> None:
        snapshot = {
            "head": "abc", "ledger_sha256": "ledger", "worktree_status_sha256": "status",
            "ready_ids": [], "runnable_ids": ["PENDING-RUNNABLE"], "candidate_revisions": [],
            "rule_handshake": {"state": "current", "blocking": False},
        }
        event = {
            "hook_event_name": "PostToolUse", "session_id": "s", "turn_id": "turn-1",
            "tool_input": {"command": f"{sys.executable} {SKILL_ROOT / 'scripts' / 'control_event_guard.py'} receipt --ledger TASK_LEDGER.md"},
            "tool_response": {"exit_code": 0, "stdout": "control-event: allowed"},
        }
        _, state = lifecycle_hook.evaluate_event(
            event, snapshot=snapshot,
            prior_state={"pending_control_event": True, "triggers": ["RUNNABLE:PENDING-RUNNABLE"], "snapshot": snapshot},
        )
        self.assertFalse(state["pending_control_event"])
        self.assertEqual(state["triggers"], [])
        self.assertTrue(state["must_yield"])
        self.assertEqual(state["receipt_turn_id"], "turn-1")

    def test_reviewer_pass_integration_has_mandatory_verify_converge_recompute_successors(self) -> None:
        snapshot = {
            "candidate_packages": [{
                "revision": "deadbeef",
                "worktree": "/tmp/candidate",
                "task_id": "WEB-TASK",
                "integration_flow": "web",
                "decision": "integrate",
                "controller_event_id": "event-1",
                "integrated_this_event": True,
                "main_revision": "main-after",
                "regression_evidence": "receipt:integration-regression",
            }],
            "required_reviews": [{
                "id": "review-web",
                "task_id": "WEB-TASK",
                "delivered_ack": True,
                "verdict": "PASS",
                "candidate_revision": "deadbeef",
            }],
        }
        errors = control_event_guard.validate_mandatory_continuations(
            snapshot,
            ledger_task_states={"WEB-TASK": "VERIFY"},
        )
        self.assertTrue(any("current-main verification" in error for error in errors))

        candidate = snapshot["candidate_packages"][0]
        candidate.update({
            "current_main_verified": True,
            "current_main_verification_evidence": "receipt:main-verify",
        })
        errors = control_event_guard.validate_mandatory_continuations(
            snapshot,
            ledger_task_states={"WEB-TASK": "VERIFY"},
        )
        self.assertTrue(any("FACT_PROJECTION_DRIFT" in error for error in errors))

        candidate.update({
            "fact_converged": True,
            "fact_convergence_evidence": "receipt:ledger-converged",
        })
        errors = control_event_guard.validate_mandatory_continuations(
            snapshot,
            ledger_task_states={"WEB-TASK": "CLOSED"},
        )
        self.assertTrue(any("post-integration project-wide recompute" in error for error in errors))

        candidate.update({
            "post_integration_recomputed": True,
            "post_integration_recompute_evidence": "receipt:project-recompute",
        })
        self.assertEqual(
            control_event_guard.validate_mandatory_continuations(
                snapshot,
                ledger_task_states={"WEB-TASK": "CLOSED"},
            ),
            [],
        )

    def test_known_next_action_enters_canonical_controller_action_projection(self) -> None:
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            with patch.object(
                control_event_guard,
                "_controller_lifecycle_state",
                return_value={
                    "next_action": "integrate reviewed candidate",
                    "requires_user": False,
                },
            ):
                actions = control_event_guard.canonical_controller_action_projection(
                    repo,
                    controller_id="controller-1",
                    candidates={},
                    required_review_ids=set(),
                    work_in_flight={},
                    corrections=[],
                    snapshot={},
                )
            self.assertTrue(
                any(item.get("type") == "known_next_action" for item in actions.values())
            )

    def test_continuation_debt_blocks_control_loop_receipt_until_every_action_resolved(self) -> None:
        snapshot = {
            "control_loop_receipt": {
                "scope": "project_wide",
                "completed_steps": list(control_event_guard.CONTROL_LOOP_STEPS),
                "ledger_sha256": "ledger",
                "runnable_ids": [],
                "candidate_revisions": [],
                "controller_action_ids": ["known_next_action:abc"],
                "correction_fingerprints": [],
                "continuation_debt_ids": ["known_next_action:abc"],
                "open_continuation_debt_ids": ["known_next_action:abc"],
                "recomputed_after_actions": True,
            },
            "controller_actions": [],
            "correction_actions": [],
        }
        errors = control_event_guard.validate_control_loop_receipt(
            snapshot,
            expected_ledger_sha256="ledger",
            expected_runnable_ids=set(),
            expected_candidate_revisions=set(),
            expected_controller_action_ids={"known_next_action:abc"},
            expected_corrections=[],
        )
        self.assertTrue(any("continuation debt" in error.lower() for error in errors))

        snapshot["controller_actions"] = [{
            "id": "known_next_action:abc",
            "decision": "executed",
            "evidence": "receipt:next-action",
        }]
        snapshot["control_loop_receipt"]["open_continuation_debt_ids"] = []
        self.assertEqual(
            control_event_guard.validate_control_loop_receipt(
                snapshot,
                expected_ledger_sha256="ledger",
                expected_runnable_ids=set(),
                expected_candidate_revisions=set(),
                expected_controller_action_ids={"known_next_action:abc"},
                expected_corrections=[],
            ),
            [],
        )

    def test_durable_terminal_receipt_enters_debt_once_and_disappears_after_consumption(self) -> None:
        from unittest.mock import patch

        terminal_path = "/tmp/durable-terminal-verdict.json"
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            with patch.object(
                control_event_guard,
                "_controller_lifecycle_state",
                return_value={
                    "pending_terminal_receipts": [terminal_path],
                    "requires_user": False,
                },
            ):
                first = control_event_guard.canonical_controller_action_projection(
                    repo,
                    controller_id="controller-1",
                    candidates={},
                    required_review_ids=set(),
                    work_in_flight={},
                    corrections=[],
                    snapshot={},
                )
            terminal_ids = [
                action_id
                for action_id, action in first.items()
                if action.get("type") == "terminal_receipt"
            ]
            self.assertEqual(len(terminal_ids), 1)

            action_id = terminal_ids[0]
            receipt_snapshot = {
                "control_loop_receipt": {
                    "scope": "project_wide",
                    "completed_steps": list(control_event_guard.CONTROL_LOOP_STEPS),
                    "ledger_sha256": "ledger",
                    "runnable_ids": [],
                    "candidate_revisions": [],
                    "controller_action_ids": [action_id],
                    "correction_fingerprints": [],
                    "continuation_debt_ids": [action_id],
                    "open_continuation_debt_ids": [],
                    "recomputed_after_actions": True,
                },
                "controller_actions": [{
                    "id": action_id,
                    "decision": "executed",
                    "evidence": "receipt:durable-terminal-consumed",
                }],
                "correction_actions": [],
            }
            self.assertEqual(
                control_event_guard.validate_control_loop_receipt(
                    receipt_snapshot,
                    expected_ledger_sha256="ledger",
                    expected_runnable_ids=set(),
                    expected_candidate_revisions=set(),
                    expected_controller_action_ids={action_id},
                    expected_corrections=[],
                ),
                [],
            )

            with patch.object(
                control_event_guard,
                "_controller_lifecycle_state",
                return_value={"pending_terminal_receipts": [], "requires_user": False},
            ):
                second = control_event_guard.canonical_controller_action_projection(
                    repo,
                    controller_id="controller-1",
                    candidates={},
                    required_review_ids=set(),
                    work_in_flight={},
                    corrections=[],
                    snapshot={},
                )
            self.assertNotIn(action_id, second)

    def test_hard_blocked_or_deferred_actions_clear_continuation_debt_and_allow_yield(self) -> None:
        action_ids = {"integration:abc", "recovery:T1"}
        snapshot = {
            "control_loop_receipt": {
                "scope": "project_wide",
                "completed_steps": list(control_event_guard.CONTROL_LOOP_STEPS),
                "ledger_sha256": "ledger",
                "runnable_ids": [],
                "candidate_revisions": [],
                "controller_action_ids": sorted(action_ids),
                "correction_fingerprints": [],
                "continuation_debt_ids": sorted(action_ids),
                "open_continuation_debt_ids": [],
                "recomputed_after_actions": True,
            },
            "controller_actions": [{
                "id": "integration:abc",
                "decision": "blocked",
                "reason_code": "shared_environment",
                "reason": "integration environment is exclusively occupied",
                "evidence": "receipt:integration-lock",
            }, {
                "id": "recovery:T1",
                "decision": "deferred",
                "reason_code": "external_blocker",
                "reason": "provider recovery endpoint is unavailable",
                "evidence": "receipt:provider-outage",
                "next_checkpoint": "provider availability change",
            }],
            "correction_actions": [],
        }
        self.assertEqual(
            control_event_guard.validate_control_loop_receipt(
                snapshot,
                expected_ledger_sha256="ledger",
                expected_runnable_ids=set(),
                expected_candidate_revisions=set(),
                expected_controller_action_ids=action_ids,
                expected_corrections=[],
            ),
            [],
        )

    def test_scenario_a_reviewer_pass_derives_all_successors_then_releases_next_ready(self) -> None:
        review = {
            "id": "review-web",
            "task_id": "WEB-1",
            "delivered_ack": True,
            "verdict": "PASS",
            "candidate_revision": "candidate-web",
        }
        candidate = {
            "revision": "candidate-web",
            "worktree": "/tmp/web-candidate",
            "task_id": "WEB-1",
            "integration_flow": "web-main",
            "decision": "review",
        }
        snapshot = {"required_reviews": [review], "candidate_packages": [candidate]}

        actions = control_event_guard.mandatory_continuation_projection(snapshot)
        self.assertEqual(actions["integration:candidate-web"]["type"], "integration")

        candidate.update({
            "decision": "integrate",
            "integrated_this_event": True,
            "main_revision": "main-after-web",
            "regression_evidence": "receipt:integration-regression",
        })
        actions = control_event_guard.mandatory_continuation_projection(snapshot)
        self.assertIn("current_main_verify:candidate-web", actions)

        candidate.update({
            "current_main_verified": True,
            "current_main_verification_evidence": "receipt:main-verify",
        })
        actions = control_event_guard.mandatory_continuation_projection(snapshot)
        self.assertIn("fact_convergence:WEB-1", actions)

        candidate.update({
            "fact_converged": True,
            "fact_convergence_evidence": "receipt:fact-convergence",
        })
        actions = control_event_guard.mandatory_continuation_projection(snapshot)
        self.assertIn("post_integration_recompute:candidate-web", actions)

        candidate.update({
            "post_integration_recomputed": True,
            "post_integration_recompute_evidence": "receipt:project-recompute",
        })
        self.assertEqual(
            control_event_guard.mandatory_continuation_projection(
                snapshot,
                ledger_task_states={"WEB-1": "CLOSED"},
            ),
            {},
        )

        ledger = self._fairness_ledger([
            ("WEB-1", "CLOSED", "done"),
            ("MINI-READY", "READY", "dispatch mini writer"),
        ])
        projection = control_event_guard.project_wide_dispatch_projection(ledger)
        self.assertIn("MINI-READY", projection["derived_runnable_ids"])

    def test_scenario_b_reviewer_fail_creates_rework_debt_and_blocks_yield_until_consumed(self) -> None:
        snapshot = {
            "required_reviews": [{
                "id": "review-web",
                "task_id": "WEB-1",
                "delivered_ack": True,
                "verdict": "FAIL",
                "candidate_revision": "candidate-web",
            }],
            "candidate_packages": [{
                "revision": "candidate-web",
                "worktree": "/tmp/web-candidate",
                "task_id": "WEB-1",
                "integration_flow": "web-main",
                "decision": "review",
            }],
        }
        actions = control_event_guard.mandatory_continuation_projection(snapshot)
        action_id = "rework:candidate-web"
        self.assertEqual(actions[action_id]["type"], "rework")

        receipt_snapshot = {
            "control_loop_receipt": {
                "scope": "project_wide",
                "completed_steps": list(control_event_guard.CONTROL_LOOP_STEPS),
                "ledger_sha256": "ledger",
                "runnable_ids": [],
                "candidate_revisions": [],
                "controller_action_ids": [action_id],
                "correction_fingerprints": [],
                "continuation_debt_ids": [action_id],
                "open_continuation_debt_ids": [action_id],
                "recomputed_after_actions": True,
            },
            "controller_actions": [],
            "correction_actions": [],
        }
        errors = control_event_guard.validate_control_loop_receipt(
            receipt_snapshot,
            expected_ledger_sha256="ledger",
            expected_runnable_ids=set(),
            expected_candidate_revisions=set(),
            expected_controller_action_ids={action_id},
            expected_corrections=[],
        )
        self.assertTrue(any("continuation debt remains open" in error for error in errors))

    def test_preblock_guard_rejects_derived_runnable_pending_package(self) -> None:
        snapshot = {
            "project_scope_scan": True,
            "ledger_revision": "abc123",
            "open_packages": [{
                "id": "BLOCKED-A", "state": "BLOCKED", "can_progress": False,
                "reason": "waiting for credential", "external_condition_id": "credential",
            }, {
                "id": "PENDING-RUNNABLE", "state": "PENDING", "can_progress": False,
                "reason": "not promoted yet", "external_condition_id": "credential",
            }],
            "live_tasks": 0, "pending_candidates": 0, "controller_actions": 0,
        }
        errors = preblock_guard.validate_snapshot(
            snapshot, ledger_package_ids={"BLOCKED-A", "PENDING-RUNNABLE"},
            derived_runnable_ids={"PENDING-RUNNABLE"},
        )
        self.assertTrue(any("derived runnable" in error and "PENDING-RUNNABLE" in error for error in errors))

    def test_lifecycle_stop_blocks_on_derived_runnable_even_when_ready_is_empty(self) -> None:
        snapshot = {
            "head": "abc123", "ledger_sha256": "ledger-1", "worktree_status_sha256": "status-1",
            "ready_ids": [], "runnable_ids": ["PENDING-RUNNABLE"], "candidate_revisions": [],
        }
        output, state = lifecycle_hook.evaluate_event(
            {"hook_event_name": "Stop", "session_id": "session-1", "turn_id": "turn-1"},
            snapshot=snapshot,
            prior_state={"pending_control_event": True, "triggers": ["RUNNABLE:PENDING-RUNNABLE"], "stop_continuations": 1},
        )
        self.assertEqual(output["decision"], "block")
        self.assertIn("PENDING-RUNNABLE", output["reason"])
        self.assertTrue(state["pending_control_event"])

    def test_identity_degraded_cannot_authorize_stop_while_project_runnable_exists(self) -> None:
        snapshot = {
            "head": "abc123",
            "ledger_sha256": "ledger-1",
            "worktree_status_sha256": "status-1",
            "ready_ids": [],
            "runnable_ids": ["MINI-READY"],
            "candidate_revisions": [],
            "identity_state": "DEGRADED",
            "runtime_contract_state": "RUNTIME_CONTRACT_DRIFT",
            "project_controller_state": {
                "project_controller": "EXISTING",
                "controller_id": "controller-1",
                "uniqueness": "UNIQUE",
            },
            "safe_control_actions_allowed": True,
            "controller_actions_allowed": False,
        }
        output, state = lifecycle_hook.evaluate_event(
            {"hook_event_name": "Stop", "session_id": "controller-1", "turn_id": "turn-1"},
            snapshot=snapshot,
            prior_state={
                "pending_control_event": True,
                "triggers": ["RUNNABLE:MINI-READY"],
                "stop_continuations": 0,
            },
        )
        self.assertEqual(output["decision"], "block")
        self.assertIn("MINI-READY", output["reason"])
        self.assertTrue(state["pending_control_event"])

    def test_preblock_guard_rejects_ready_work_outside_current_goal(self) -> None:
        snapshot = {
            "project_scope_scan": True,
            "ledger_revision": "abc123",
            "open_packages": [
                {
                    "id": "CURRENT-GOAL",
                    "state": "BLOCKED",
                    "can_progress": False,
                    "reason": "GUI unavailable",
                    "external_condition_id": "gui-session",
                },
                {
                    "id": "P2-OTHER-END",
                    "state": "READY",
                    "can_progress": True,
                    "reason": "independent package",
                    "external_condition_id": "",
                },
            ],
            "live_tasks": 0,
            "pending_candidates": 0,
            "controller_actions": 0,
        }

        errors = preblock_guard.validate_snapshot(
            snapshot, ledger_package_ids={"CURRENT-GOAL", "P2-OTHER-END"}
        )

        self.assertTrue(any("P2-OTHER-END can still make progress" in e for e in errors))
        self.assertTrue(any("P2-OTHER-END is still READY" in e for e in errors))

    def test_preblock_guard_rejects_active_work_and_controller_actions(self) -> None:
        snapshot = {
            "project_scope_scan": True,
            "ledger_revision": "abc123",
            "open_packages": [
                {
                    "id": "F1",
                    "state": "ACTIVE",
                    "can_progress": False,
                    "reason": "writer still running",
                    "external_condition_id": "writer-result",
                }
            ],
            "live_tasks": 1,
            "pending_candidates": 0,
            "controller_actions": 1,
        }

        errors = preblock_guard.validate_snapshot(snapshot, ledger_package_ids={"F1"})

        self.assertTrue(any("F1 is still ACTIVE" in e for e in errors))
        self.assertTrue(any("live_tasks must be zero" in e for e in errors))
        self.assertTrue(any("controller_actions must be zero" in e for e in errors))

    def test_preblock_guard_allows_single_shared_external_blocker(self) -> None:
        snapshot = {
            "project_scope_scan": True,
            "ledger_revision": "abc123",
            "open_packages": [
                {
                    "id": "F1",
                    "state": "BLOCKED",
                    "can_progress": False,
                    "reason": "waiting for the same production credential",
                    "external_condition_id": "production-credential",
                },
                {
                    "id": "F2",
                    "state": "BLOCKED",
                    "can_progress": False,
                    "reason": "waiting for the same production credential",
                    "external_condition_id": "production-credential",
                },
            ],
            "live_tasks": 0,
            "pending_candidates": 0,
            "controller_actions": 0,
        }

        self.assertEqual(
            preblock_guard.validate_snapshot(snapshot, ledger_package_ids={"F1", "F2"}),
            [],
        )

    def test_preblock_guard_rejects_omitted_open_ledger_package(self) -> None:
        snapshot = {
            "project_scope_scan": True,
            "ledger_revision": "abc123",
            "open_packages": [
                {
                    "id": "F1",
                    "state": "BLOCKED",
                    "can_progress": False,
                    "reason": "waiting for credential",
                    "external_condition_id": "credential",
                }
            ],
            "live_tasks": 0,
            "pending_candidates": 0,
            "controller_actions": 0,
        }

        errors = preblock_guard.validate_snapshot(
            snapshot, ledger_package_ids={"F1", "P2-READY"}
        )

        self.assertIn("scan omitted ledger packages: P2-READY", errors)

    def test_durable_profile_creates_nine_project_documents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = init_project.initialize_project(root, profile="durable")

            self.assertEqual(
                set(report.created),
                {
                    "AGENTS.md",
                    "TASK_LEDGER.md",
                    "MEMORY.md",
                    "WIKI_INDEX.md",
                    "SKILL.md",
                    "SPEC.md",
                    "DESIGN.md",
                    "TECHNICAL.md",
                    "EVOLUTION.md",
                },
            )
            errors, warnings = lint_governance.lint_project(root, strict=True)
            self.assertEqual(errors, [])
            self.assertEqual(warnings, [])

    def test_durable_profile_initializes_queryable_knowledge_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            report = init_project.initialize_project(root, profile="durable")

            self.assertEqual(
                set(report.created_directories),
                {
                    "raw_sources",
                    "wiki",
                    "logs",
                    "logs/ingestion",
                },
            )
            self.assertTrue((root / "raw_sources").is_dir())
            self.assertTrue((root / "wiki").is_dir())
            self.assertTrue((root / "logs" / "ingestion").is_dir())

    def test_existing_legacy_ledger_prevents_second_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "PROJECT_STATUS.md").write_text("# Existing\n", encoding="utf-8")

            report = init_project.initialize_project(root, profile="collaborative")

            self.assertFalse((root / "TASK_LEDGER.md").exists())
            self.assertIn(
                "TASK_LEDGER.md (using existing PROJECT_STATUS.md)", report.skipped
            )

    def test_linter_rejects_two_ledgers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "TASK_LEDGER.md").write_text("# New\n", encoding="utf-8")
            (root / "PROJECT_STATUS.md").write_text("# Old\n", encoding="utf-8")

            errors, _ = lint_governance.lint_project(root)

            self.assertTrue(any("both TASK_LEDGER" in error for error in errors))

    def test_linter_accepts_parallel_active_rows_when_pointer_matches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "TASK_LEDGER.md").write_text(
                """# Ledger

- 当前活动项：F1、F2

| ID | 状态 | 负责人 | 文件或范围 | 下一步动作 |
| --- | --- | --- | --- | --- |
| F1 | `ACTIVE` | Agent A | app/auth/** | 完成后进入 F3 |
| F2 | `ACTIVE` | Agent B | app/reader/** | 完成后进入 F4 |
""",
                encoding="utf-8",
            )

            errors, _ = lint_governance.lint_project(root)

            self.assertEqual(errors, [])

    def test_linter_rejects_activity_pointer_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "TASK_LEDGER.md").write_text(
                """# Ledger

- 当前活动项：F1

| ID | 状态 |
| --- | --- |
| F1 | `ACTIVE` |
| F2 | `ACTIVE` |
""",
                encoding="utf-8",
            )

            errors, _ = lint_governance.lint_project(root)

            self.assertTrue(any("current activity pointer does not match" in error for error in errors))

    def test_linter_allows_omitting_derived_activity_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "TASK_LEDGER.md").write_text(
                """# Ledger

| ID | 状态 / 负责人 |
| --- | --- |
| F1 | `ACTIVE` / Agent A |
""",
                encoding="utf-8",
            )

            errors, warnings = lint_governance.lint_project(root)

            self.assertEqual(errors, [])
            self.assertFalse(any("current activity" in warning for warning in warnings))

    def test_ledger_consistency_uses_task_table_as_canonical_state(self) -> None:
        text = """# Ledger

- 当前 Goal：`F1` 完成真实登录闭环
- 下一可见检查点：`F1` 真实浏览器登录恢复通过
- 当前阻塞：无
- 规则版本：abc123

| ID | 状态 / 负责人 | 证据 / 下一步 |
| --- | --- | --- |
| F1 | `ACTIVE` / Agent A | 完成登录恢复 Case |
| F2 | `RECOVERING` / Agent B Assignment F2-RECOVERY-01 | delivered ACK；修复 RED 后在 checkpoint 复审 |
"""

        self.assertEqual(ledger_consistency_guard.validate_ledger(text), [])

    def test_ledger_consistency_rejects_implicit_next_task_not_in_task_table(self) -> None:
        text = """# Ledger

- 当前 Goal：`M2-F2` 完成微信读书内部闭环
- 下一可见检查点：`M2-F2` 形成真实端证据
- 当前阻塞：无
- 规则版本：abc123

| ID | 状态 / 负责人 | 证据 / 下一步 |
| --- | --- | --- |
| M2-F2 | `ACTIVE` / 项目总控 | 下一步派发 `M2-F2-DEV-WEREAD-QA-01` 后继续 |
"""

        errors = ledger_consistency_guard.validate_ledger(text)

        self.assertTrue(
            any("undeclared task ID M2-F2-DEV-WEREAD-QA-01" in error for error in errors)
        )

    def test_ledger_consistency_allows_assignment_id_without_extra_task_row(self) -> None:
        text = """# Ledger

- 当前 Goal：`M1-F4-C-SERVER-GATE` 完成后端门禁
- 下一可见检查点：`M1-F4-C-SERVER-GATE` 的 Assignment `M1-F4-C-SERVER-GATE-B-01` 返回候选
- 当前阻塞：无
- 规则版本：abc123

| ID | 状态 / 负责人 | 证据 / 下一步 |
| --- | --- | --- |
| M1-F4-C-SERVER-GATE | `ACTIVE` / Agent B | 当前 Assignment `M1-F4-C-SERVER-GATE-B-01` delivered ACK；等待候选 |
"""

        self.assertEqual(ledger_consistency_guard.validate_ledger(text), [])

    def test_ledger_consistency_assignment_exemption_requires_declared_parent(self) -> None:
        text = """# Ledger

- 当前 Goal：`M1-F4-C-SERVER-GATE` 完成后端门禁
- 下一可见检查点：`M1-F4-C-SERVER-GATE` 等待 Assignment `M9-UNKNOWN-B-01`
- 当前阻塞：无
- 规则版本：abc123

| ID | 状态 / 负责人 | 证据 / 下一步 |
| --- | --- | --- |
| M1-F4-C-SERVER-GATE | `ACTIVE` / Agent B | 等待外部执行 |
"""

        errors = ledger_consistency_guard.validate_ledger(text)
        self.assertTrue(any("undeclared task ID M9-UNKNOWN-B-01" in error for error in errors))

    def test_ledger_consistency_rejects_implicit_task_in_visible_checkpoint(self) -> None:
        text = """# Ledger

- 当前 Goal：`M2-F2` 完成微信读书内部闭环
- 下一可见检查点：`M2-F2` 的 `M2-F2-DEV-WEREAD-QA-01` 先形成候选
- 当前阻塞：无
- 规则版本：abc123

| ID | 状态 / 负责人 | 证据 / 下一步 |
| --- | --- | --- |
| M2-F2 | `ACTIVE` / 项目总控 | 完成当前父任务 |
"""

        errors = ledger_consistency_guard.validate_ledger(text)

        self.assertTrue(
            any("checkpoint references undeclared task ID M2-F2-DEV-WEREAD-QA-01" in error for error in errors)
        )

    def test_ledger_consistency_does_not_treat_crypto_algorithm_as_task_id(self) -> None:
        text = """# Ledger

- 当前 Goal：`M1-F3A-A` 完成加密闭环
- 下一可见检查点：`M1-F3A-A` 完成验证
- 当前阻塞：无
- 规则版本：abc123

| ID | 状态 / 负责人 | 证据 / 下一步 |
| --- | --- | --- |
| M1-F3A-A | `ACTIVE` / Agent A | 使用 AES-256-GCM 完成加密验证 |
"""

        errors = ledger_consistency_guard.validate_ledger(text)

        self.assertFalse(any("AES-256-GCM" in error for error in errors))

    def test_ledger_consistency_recovering_requires_real_execution_binding(self) -> None:
        text = """# Ledger

- 当前 Goal：`M1-F4-B` 修复后端闭环
- 下一可见检查点：`M1-F4-B` 恢复执行
- 当前阻塞：无
- 规则版本：abc123

| ID | 状态 / 负责人 | 证据 / 下一步 |
| --- | --- | --- |
| M1-F4-B | `RECOVERING` / 项目总控 | 后续只在形成新的可验证执行路由后再恢复 |
"""

        errors = ledger_consistency_guard.validate_ledger(text)

        self.assertIn(
            "M1-F4-B RECOVERING requires a delivered assignment ACK or a verifiable recovery action",
            errors,
        )

    def test_ledger_consistency_allows_recovering_with_delivered_assignment_ack(self) -> None:
        text = """# Ledger

- 当前 Goal：`M2-F2` 完成微信读书内部闭环
- 下一可见检查点：`M2-F2` 候选进入代码门
- 当前阻塞：无
- 规则版本：abc123

| ID | 状态 / 负责人 | 固定边界与当前证据 | 依赖、阻塞与下一步 |
| --- | --- | --- | --- |
| M2-F2 | `RECOVERING` / 外部 Kimi `M2-F2-DEV-WEREAD-QA-01 / kimi-k3 / api / high` | Writer 已完整 delivered ACK，lease guard PASS | 恢复动作：只实现 develop-only port，候选先过非作者代码门 |
"""

        self.assertEqual(ledger_consistency_guard.validate_ledger(text), [])

    def test_ledger_consistency_rejects_stale_pointer_and_unmapped_goal(self) -> None:
        text = """# Ledger

- 当前 Goal：旧目标
- 当前活动项：F1
- 下一可见检查点：稍后看看
- 当前阻塞：无
- 规则版本：abc123

| ID | 状态 / 负责人 | 证据 / 下一步 |
| --- | --- | --- |
| F1 | `ACTIVE` / Agent A | 完成登录恢复 Case |
| F2 | `RECOVERING` / 待分配 | 等待 |
"""

        errors = ledger_consistency_guard.validate_ledger(text)

        self.assertIn(
            "current activity pointer does not match ACTIVE/RECOVERING task rows",
            errors,
        )
        self.assertIn("current Goal must reference at least one open task ID", errors)
        self.assertIn("next visible checkpoint must reference at least one open task ID", errors)
        self.assertIn("F2 RECOVERING requires a unique owner", errors)

    def test_ledger_consistency_does_not_match_task_id_prefixes(self) -> None:
        text = """# Ledger

- 当前 Goal：`F10` 另一个任务
- 下一可见检查点：`F10` 稍后
- 当前阻塞：无
- 规则版本：abc123

| ID | 状态 / 负责人 | 证据 / 下一步 |
| --- | --- | --- |
| F1 | `READY` / 主 Agent | 执行 F1 |
"""

        errors = ledger_consistency_guard.validate_ledger(text)

        self.assertIn("current Goal must reference at least one open task ID", errors)

    def test_ledger_consistency_rejects_duplicated_runtime_capacity_pointer(self) -> None:
        text = """# Ledger

- 当前 Goal：`F1` 完成登录闭环
- 下一可见检查点：`F1` 真实浏览器通过
- 当前阻塞：无
- 规则版本：abc123
- 容量 / READY：1/4，三个空槽

| ID | 状态 / 负责人 | 证据 / 下一步 |
| --- | --- | --- |
| F1 | `ACTIVE` / Agent A | 完成登录恢复 Case |
"""

        errors = ledger_consistency_guard.validate_ledger(text)

        self.assertTrue(any("runtime capacity" in error for error in errors))

    def test_task_parser_ignores_status_words_in_evidence_and_non_task_tables(self) -> None:
        text = """# Ledger

| ID | 状态 / owner | 证据 / 下一步 |
| --- | --- | --- |
| F1 | `VERIFY` | 旧收据写 READY，但本项仍待复验 |

| 证据 | 结论 |
| --- | --- |
| screenshot | READY FOR EARLY |
"""

        self.assertEqual(lint_governance.task_rows(text), [("F1", "VERIFY")])

    def test_task_parser_normalizes_code_span_id_with_summary_label(self) -> None:
        text = """| 功能组 | 汇总状态 |
| --- | --- |
| `M2` 微信小程序 | `READY` |
"""

        self.assertEqual(lint_governance.task_rows(text), [("M2", "READY")])

    def test_task_record_prefers_next_step_over_evidence_column(self) -> None:
        text = """| ID | 状态 / owner | 固定边界与当前证据 | 依赖、阻塞与下一步 |
| --- | --- | --- | --- |
| F1 | `RECOVERING` / Agent A | 旧候选失败 | 修复后在 checkpoint 复审 |
"""

        self.assertEqual(
            lint_governance.task_records(text)[0]["next_action"],
            "修复后在 checkpoint 复审",
        )

    def test_legacy_project_status_template_passes_strict_lint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = (
                SKILL_ROOT / "assets" / "templates" / "PROJECT_STATUS.md"
            ).read_text(encoding="utf-8")
            (root / "PROJECT_STATUS.md").write_text(template, encoding="utf-8")

            errors, warnings = lint_governance.lint_project(root, strict=True)

            self.assertEqual(errors, [])
            self.assertEqual(warnings, [])

    def test_linter_rejects_duplicate_task_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "TASK_LEDGER.md").write_text(
                """# Ledger

- 当前活动项：无
- 协调下一动作：选择 F1

| ID | 状态 |
| --- | --- |
| F1 | `READY` |

| ID | 状态 |
| --- | --- |
| F1 | `DONE` |
""",
                encoding="utf-8",
            )

            errors, _ = lint_governance.lint_project(root, strict=True)

            self.assertTrue(any("repeats task IDs" in error for error in errors))

    def test_linter_warns_before_strict_duplicate_id_migration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "TASK_LEDGER.md").write_text(
                """# Ledger

- 当前活动项：无

| ID | 状态 |
| --- | --- |
| F1 | `READY` |

| ID | 状态 |
| --- | --- |
| F1 | `DONE` |
""",
                encoding="utf-8",
            )

            errors, warnings = lint_governance.lint_project(root)

            self.assertEqual(errors, [])
            self.assertTrue(any("repeats task IDs" in warning for warning in warnings))


if __name__ == "__main__":
    unittest.main()


class ControllerSelfCheckLifecycleTests(unittest.TestCase):
    def test_quiescent_session_start_still_injects_non_numeric_controller_self_check(self) -> None:
        snapshot = {"head":"h","ledger_sha256":"l","worktree_status_sha256":"s","ready_ids":[],"runnable_ids":[],"candidate_revisions":[],"rule_handshake":{}}
        output, state = lifecycle_hook.evaluate_event(
            {"hook_event_name":"SessionStart","session_id":"controller-1"}, snapshot=snapshot, prior_state=None
        )
        context = output["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Controller Self-Check", context)
        self.assertIn("关键路径与优先级", context)
        self.assertNotIn("25%", context)
        self.assertNotIn("当前得分", context)
        self.assertFalse(state["pending_control_event"])


class ControllerHostLifecycleTests(unittest.TestCase):
    def test_native_lifecycle_defaults_current_host_to_desktop_codex(self) -> None:
        snapshot = {"head":"h","ledger_sha256":"l","worktree_status_sha256":"s","ready_ids":[],"runnable_ids":[],"candidate_revisions":[],"rule_handshake":{}}
        _, state = lifecycle_hook.evaluate_event(
            {"hook_event_name":"SessionStart","session_id":"controller-1"}, snapshot=snapshot, prior_state=None
        )
        self.assertEqual(state["controller_host"], "desktop_codex")

    def test_explicit_web_event_updates_same_controller_host(self) -> None:
        snapshot = {"head":"h","ledger_sha256":"l","worktree_status_sha256":"s","ready_ids":[],"runnable_ids":[],"candidate_revisions":[],"rule_handshake":{}}
        _, state = lifecycle_hook.evaluate_event(
            {"hook_event_name":"PostToolUse","session_id":"controller-1","controller_host":"web"}, snapshot=snapshot, prior_state={"controller_host":"desktop_codex"}
        )
        self.assertEqual(state["controller_host"], "web")


class PendingLifecycleWakeDispatchTests(unittest.TestCase):
    GENERIC_WAKE_ENTRY = "wake_existing_controller"

    def base_snapshot(self, **overrides: object) -> dict[str, object]:
        snapshot: dict[str, object] = {
            "head": "abc123",
            "ledger_sha256": "ledger-1",
            "worktree_status_sha256": "status-1",
            "ready_ids": [],
            "runnable_ids": [],
            "candidate_revisions": [],
            "ledger_errors": [],
            "assignment_liveness": {},
            "rule_handshake": {"state": "current", "blocking": False},
        }
        snapshot.update(overrides)
        return snapshot

    def request_for(
        self,
        snapshot: dict[str, object],
        *,
        prior_state: dict[str, object] | None = None,
        event_name: str = "PostToolUse",
        cwd: str | None = None,
    ) -> tuple[dict[str, object] | None, dict[str, object]]:
        event: dict[str, object] = {
            "hook_event_name": event_name,
            "session_id": "controller-1",
            "controller_host": "web",
            "tool_input": {},
            "tool_response": {},
        }
        if cwd is not None:
            event["cwd"] = cwd
        _, state = lifecycle_hook.evaluate_event(
            event, snapshot=snapshot, prior_state=prior_state
        )
        return lifecycle_hook.pending_wake_request(state), state

    def test_active_lease_expired_uses_generic_wake_entry_point(self) -> None:
        snapshot = self.base_snapshot(
            assignment_liveness={
                "F1": {"ledger_state": "ACTIVE", "state": "unhealthy", "reason": "lease_expired"}
            }
        )
        prior = {
            "snapshot": dict(snapshot),
            "pending_control_event": False,
            "triggers": [],
            "stop_continuations": 0,
        }
        request, state = self.request_for(snapshot, prior_state=prior)

        self.assertTrue(state["pending_control_event"])
        self.assertIn("active_lease_expired:F1", state["triggers"])
        self.assertIsNotNone(request)
        self.assertEqual(request["entry_point"], self.GENERIC_WAKE_ENTRY)
        self.assertEqual(request["event_fingerprint"], lifecycle_hook.pending_event_fingerprint(state))
        self.assertTrue(request["pending_control_event"])

    def test_ready_candidate_rule_and_bound_worktree_share_the_same_wake_entry(self) -> None:
        from unittest.mock import patch

        cases = [
            (self.base_snapshot(ready_ids=["READY-1"], runnable_ids=["READY-1"]), "READY:READY-1"),
            (self.base_snapshot(candidate_revisions=["candidate-123"]), "CANDIDATE:candidate-123"),
            (
                self.base_snapshot(
                    assignment_liveness={
                        "F1": {"ledger_state": "ACTIVE", "state": "healthy", "reason": "lease_current"}
                    },
                    rule_handshake={
                        "state": "pending_ack",
                        "blocking": True,
                        "installed_revision": "rev-goal",
                        "impact": "live_assignments",
                        "changed_files": ["scripts/assignment_runtime.py"],
                    },
                ),
                "rule_update_pending:rev-goal",
            ),
        ]
        entry_points: set[str] = set()
        for snapshot, expected_trigger in cases:
            with self.subTest(trigger=expected_trigger):
                request, state = self.request_for(snapshot)
                self.assertTrue(state["pending_control_event"])
                self.assertIn(expected_trigger, state["triggers"])
                self.assertIsNotNone(request)
                entry_points.add(str(request["entry_point"]))
                self.assertEqual(request["event_fingerprint"], lifecycle_hook.pending_event_fingerprint(state))
        self.assertEqual(entry_points, {self.GENERIC_WAKE_ENTRY})

        main, controller_worktree, writer_worktree, registry = GovernanceTests.lifecycle_worktree_fixture(self)
        with patch.object(lifecycle_hook, "REGISTRY_PATH", registry):
            lifecycle_hook.register_controller("controller-1", controller_worktree)
            changed = self.base_snapshot(ledger_sha256="ledger-bound")
            request, state = self.request_for(
                changed,
                prior_state={
                    "snapshot": self.base_snapshot(),
                    "pending_control_event": False,
                    "triggers": [],
                },
                cwd=str(controller_worktree),
            )
            self.assertTrue(
                lifecycle_hook.controller_event_is_managed(
                    {"session_id": "controller-1", "controller_host": "web"},
                    controller_worktree,
                    main,
                )
            )
            self.assertFalse(
                lifecycle_hook.controller_event_is_managed(
                    {"session_id": "controller-1", "controller_host": "web"},
                    writer_worktree,
                    main,
                )
            )
            self.assertIn("ledger_changed", state["triggers"])
            self.assertEqual(request["entry_point"], self.GENERIC_WAKE_ENTRY)

    def test_unchanged_pending_state_reuses_fingerprint_and_does_not_invent_a_second_policy(self) -> None:
        snapshot = self.base_snapshot(ready_ids=["READY-1"], runnable_ids=["READY-1"])
        first, first_state = self.request_for(snapshot)
        second, second_state = self.request_for(snapshot, prior_state=first_state)

        self.assertEqual(first["entry_point"], self.GENERIC_WAKE_ENTRY)
        self.assertEqual(second["entry_point"], first["entry_point"])
        self.assertEqual(second["event_fingerprint"], first["event_fingerprint"])
        self.assertEqual(second_state["triggers"], first_state["triggers"])
        self.assertTrue(second_state["pending_control_event"])

    def test_cleared_control_event_does_not_request_a_wake(self) -> None:
        snapshot = self.base_snapshot()
        output, next_state = lifecycle_hook.evaluate_event(
            {
                "hook_event_name": "PostToolUse",
                "session_id": "controller-1",
                "tool_input": {
                    "command": f"{sys.executable} {SKILL_ROOT / 'scripts' / 'control_event_guard.py'} receipt.json --ledger TASK_LEDGER.md"
                },
                "tool_response": {"output": "control-event: allowed", "exit_code": 0},
            },
            snapshot=snapshot,
            prior_state={"pending_control_event": True, "triggers": ["READY:READY-1"]},
        )
        self.assertEqual(output, {})
        self.assertFalse(next_state["pending_control_event"])
        self.assertIsNone(lifecycle_hook.pending_wake_request(next_state))

class WebDispatchRuntimeGateTests(unittest.TestCase):
    def test_web_delegated_assignment_requires_machine_verified_bound_runtime_dispatch(self) -> None:
        from scripts.control_event_guard import canonical_web_dispatch_errors
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            snapshot = {
                "new_assignments": [{
                    "task_id": "WEB-1",
                    "assignment_id": "A-WEB-1",
                    "execution_mode": "delegated",
                    "execution_transport": "web",
                    "runtime_dispatch": {
                        "dispatch_id": "missing-ticket",
                        "state": "bound",
                        "conversation_id": "child-1",
                        "lease_id": "lease-1",
                    },
                }]
            }
            errors = canonical_web_dispatch_errors(repo, snapshot)
        self.assertTrue(any("canonical Runtime dispatch" in error for error in errors))
