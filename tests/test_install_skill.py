import tempfile
import unittest
from pathlib import Path

from scripts.install_skill import detect_host_capabilities


class InstallCapabilityTests(unittest.TestCase):
    def test_desktop_adapter_is_enabled_only_by_an_exact_live_canary_receipt(self):
        import hashlib
        import json
        import subprocess
        from datetime import datetime, timezone

        from scripts.install_skill import install_codex_hooks

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            codex = root / "codex"
            codex.write_text("#!/bin/sh\n", encoding="utf-8")
            codex.chmod(0o755)
            skill_root = root / "adaptive-delivery"
            (skill_root / "scripts").mkdir(parents=True)
            for name in (
                "lifecycle_hook.py",
                "controller_target_guard.py",
                "controller_scoring_hook.py",
                "project_context_guard.py",
                "goal_display_sync.py",
            ):
                script = skill_root / "scripts" / name
                script.write_text(f"#!/usr/bin/env python3\n# {name}\n", encoding="utf-8")
                script.chmod(0o755)
            hooks = root / "hooks.json"
            install_codex_hooks(hooks, skill_root, python_executable="/usr/bin/python3")
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            registry = root / "controllers.json"
            registry_value = {
                "controller-1": str(repo.resolve()),
                "__controller_targets__": {"controller-1": {"desktop_codex": {
                    "status": "active",
                    "session_id": "desktop-current",
                    "generation": 4,
                }}},
                "__controller_execution_ownership__": {"controller-1": {
                    "active_host": "desktop_codex",
                    "execution_target_session_id": "desktop-current",
                    "generation": 7,
                }},
            }
            registry.write_text(json.dumps(registry_value), encoding="utf-8")
            canary = root / "desktop-canary.json"
            receipt = {
                "schema_version": 7,
                "status": "passed",
                "controller_id": "controller-1",
                "controller_session_id": "controller-1",
                "execution_target_session_id": "desktop-current",
                "target_generation": 4,
                "ownership_generation": 7,
                "canonical_repo": str(repo.resolve()),
                "controller_registry_path": str(registry.resolve()),
                "run_id": "0123456789abcdef0123456789abcdef",
                "sequence_index": 8,
                "skill_root": str(skill_root.resolve()),
                "hooks_sha256": hashlib.sha256(hooks.read_bytes()).hexdigest(),
                "lifecycle_sha256": hashlib.sha256(
                    (skill_root / "scripts" / "lifecycle_hook.py").read_bytes()
                ).hexdigest(),
                "controller_target_guard_sha256": hashlib.sha256(
                    (skill_root / "scripts" / "controller_target_guard.py").read_bytes()
                ).hexdigest(),
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "observations": [
                    "pre_tool_allowed",
                    "post_tool_observed",
                    "receipt_latched",
                    "same_turn_continuation_invalidated_receipt",
                    "post_invalidation_tool_observed",
                    "stop_observed",
                    "post_stop_receipt_latched",
                    "post_stop_continuation_invalidated_receipt",
                ],
            }
            canary.write_text(json.dumps(receipt), encoding="utf-8")

            report = detect_host_capabilities(
                codex_executable=codex,
                ai_bridge_executable=root / "missing-ai-bridge",
                hooks_file=hooks,
                zshenv_file=root / ".zshenv",
                skill_root=skill_root,
                desktop_canary_file=canary,
                controller_registry=registry,
            )

            self.assertEqual(report["desktop_adapter"]["status"], "enabled")
            self.assertEqual(
                report["desktop_adapter"]["goal_display_sync"], "configured_unverified"
            )
            self.assertEqual(
                report["web_local_adapter"]["goal_display_sync"],
                "degraded_host_capability_unavailable",
            )

            other_registry = root / "other-controllers.json"
            other_registry.write_text(json.dumps(registry_value), encoding="utf-8")
            wrong_registry = detect_host_capabilities(
                codex_executable=codex,
                ai_bridge_executable=root / "missing-ai-bridge",
                hooks_file=hooks,
                zshenv_file=root / ".zshenv",
                skill_root=skill_root,
                desktop_canary_file=canary,
                controller_registry=other_registry,
            )
            self.assertEqual(wrong_registry["desktop_adapter"]["status"], "degraded")

            boolean_generation_registry = json.loads(json.dumps(registry_value))
            boolean_generation_registry["__controller_targets__"]["controller-1"][
                "desktop_codex"
            ]["generation"] = True
            registry.write_text(
                json.dumps(boolean_generation_registry), encoding="utf-8"
            )
            receipt["target_generation"] = 1
            canary.write_text(json.dumps(receipt), encoding="utf-8")
            boolean_generation = detect_host_capabilities(
                codex_executable=codex,
                ai_bridge_executable=root / "missing-ai-bridge",
                hooks_file=hooks,
                zshenv_file=root / ".zshenv",
                skill_root=skill_root,
                desktop_canary_file=canary,
                controller_registry=registry,
            )
            self.assertEqual(boolean_generation["desktop_adapter"]["status"], "degraded")

            duplicate_registry = json.loads(json.dumps(registry_value))
            duplicate_registry["controller-2"] = str(repo.resolve())
            registry.write_text(json.dumps(duplicate_registry), encoding="utf-8")
            receipt["target_generation"] = 4
            canary.write_text(json.dumps(receipt), encoding="utf-8")
            controller_conflict = detect_host_capabilities(
                codex_executable=codex,
                ai_bridge_executable=root / "missing-ai-bridge",
                hooks_file=hooks,
                zshenv_file=root / ".zshenv",
                skill_root=skill_root,
                desktop_canary_file=canary,
                controller_registry=registry,
            )
            self.assertEqual(controller_conflict["desktop_adapter"]["status"], "degraded")
            registry.write_text(json.dumps(registry_value), encoding="utf-8")

            moved_registry = json.loads(json.dumps(registry_value))
            moved_registry["__controller_execution_ownership__"]["controller-1"] = {
                "active_host": "web",
                "execution_target_session_id": "web-current",
                "generation": 8,
            }
            registry.write_text(json.dumps(moved_registry), encoding="utf-8")
            ownership_moved = detect_host_capabilities(
                codex_executable=codex,
                ai_bridge_executable=root / "missing-ai-bridge",
                hooks_file=hooks,
                zshenv_file=root / ".zshenv",
                skill_root=skill_root,
                desktop_canary_file=canary,
                controller_registry=registry,
            )
            self.assertEqual(ownership_moved["desktop_adapter"]["status"], "degraded")
            registry.write_text(json.dumps(registry_value), encoding="utf-8")

            (skill_root / "scripts" / "controller_target_guard.py").write_text(
                "#!/usr/bin/env python3\n# changed target guard\n", encoding="utf-8"
            )
            stale_guard = detect_host_capabilities(
                codex_executable=codex,
                ai_bridge_executable=root / "missing-ai-bridge",
                hooks_file=hooks,
                zshenv_file=root / ".zshenv",
                skill_root=skill_root,
                desktop_canary_file=canary,
                controller_registry=registry,
            )
            self.assertEqual(stale_guard["desktop_adapter"]["status"], "degraded")

            receipt["controller_target_guard_sha256"] = hashlib.sha256(
                (skill_root / "scripts" / "controller_target_guard.py").read_bytes()
            ).hexdigest()
            canary.write_text(json.dumps(receipt), encoding="utf-8")

            hooks.write_text(hooks.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            stale = detect_host_capabilities(
                codex_executable=codex,
                ai_bridge_executable=root / "missing-ai-bridge",
                hooks_file=hooks,
                zshenv_file=root / ".zshenv",
                skill_root=skill_root,
                desktop_canary_file=canary,
                controller_registry=registry,
            )
            self.assertEqual(stale["desktop_adapter"]["status"], "degraded")
            self.assertIn("canary", stale["desktop_adapter"]["reason"].lower())

    def test_desktop_adapter_rejects_an_expired_canary_receipt(self):
        import hashlib
        import json

        from scripts.install_skill import install_codex_hooks

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            codex = root / "codex"
            codex.write_text("#!/bin/sh\n", encoding="utf-8")
            codex.chmod(0o755)
            skill_root = root / "adaptive-delivery"
            (skill_root / "scripts").mkdir(parents=True)
            for name in (
                "lifecycle_hook.py",
                "controller_target_guard.py",
                "controller_scoring_hook.py",
                "project_context_guard.py",
            ):
                script = skill_root / "scripts" / name
                script.write_text(f"#!/usr/bin/env python3\n# {name}\n", encoding="utf-8")
                script.chmod(0o755)
            hooks = root / "hooks.json"
            install_codex_hooks(hooks, skill_root, python_executable="/usr/bin/python3")
            repo = root / "repo"
            repo.mkdir()
            registry = root / "controllers.json"
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
            canary.write_text(
                json.dumps(
                    {
                        "schema_version": 7,
                        "status": "passed",
                        "controller_id": "controller-1",
                        "controller_session_id": "controller-1",
                        "execution_target_session_id": "desktop-current",
                        "target_generation": 4,
                        "ownership_generation": 7,
                        "canonical_repo": str(repo.resolve()),
                        "controller_registry_path": str(registry.resolve()),
                        "run_id": "0123456789abcdef0123456789abcdef",
                        "sequence_index": 8,
                        "skill_root": str(skill_root.resolve()),
                        "hooks_sha256": hashlib.sha256(hooks.read_bytes()).hexdigest(),
                        "lifecycle_sha256": hashlib.sha256(
                            (skill_root / "scripts" / "lifecycle_hook.py").read_bytes()
                        ).hexdigest(),
                        "controller_target_guard_sha256": hashlib.sha256(
                            (skill_root / "scripts" / "controller_target_guard.py").read_bytes()
                        ).hexdigest(),
                        "completed_at": "2020-01-01T00:00:00+00:00",
                        "observations": [
                                "pre_tool_allowed",
                                "post_tool_observed",
                                "receipt_latched",
                                "same_turn_continuation_invalidated_receipt",
                                "post_invalidation_tool_observed",
                                "stop_observed",
                                "post_stop_receipt_latched",
                                "post_stop_continuation_invalidated_receipt",
                        ],
                    }
                ),
                encoding="utf-8",
            )

            report = detect_host_capabilities(
                codex_executable=codex,
                ai_bridge_executable=root / "missing-ai-bridge",
                hooks_file=hooks,
                zshenv_file=root / ".zshenv",
                skill_root=skill_root,
                desktop_canary_file=canary,
            )

        self.assertEqual(report["desktop_adapter"]["status"], "degraded")

    def test_capability_report_degrades_cleanly_without_ai_bridge(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            codex = root / "codex"
            codex.write_text("#!/bin/sh\n", encoding="utf-8")
            codex.chmod(0o755)
            report = detect_host_capabilities(
                codex_executable=codex,
                ai_bridge_executable=root / "missing-ai-bridge",
                hooks_file=root / "hooks.json",
                zshenv_file=root / ".zshenv",
            )

        self.assertEqual(report["core"]["status"], "enabled")
        self.assertEqual(report["desktop_adapter"]["status"], "degraded")
        self.assertFalse(report["desktop_adapter"]["configured"])
        self.assertIn("not fully configured", report["desktop_adapter"]["reason"])
        self.assertEqual(report["web_local_adapter"]["status"], "degraded")
        self.assertEqual(report["web_local_adapter"]["mode"], "pure_web_file")

    def test_capability_report_rejects_stale_codex_hooks_from_another_install(self):
        import json
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            codex = root / "codex"; codex.write_text("#!/bin/sh\n", encoding="utf-8"); codex.chmod(0o755)
            skill_root = root / "adaptive-delivery"
            (skill_root / "scripts").mkdir(parents=True)
            for name in ("lifecycle_hook.py", "controller_scoring_hook.py"):
                path = skill_root / "scripts" / name
                path.write_text("#!/usr/bin/env python3\n", encoding="utf-8"); path.chmod(0o755)
            hooks = root / "hooks.json"
            hooks.write_text(json.dumps({"hooks": {
                "SessionStart": [{"hooks": [{"type": "command", "command": "/usr/bin/python3 /old/install/scripts/lifecycle_hook.py"}]}],
                "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "/usr/bin/python3 /old/install/scripts/controller_scoring_hook.py"}]}],
            }}), encoding="utf-8")
            report = detect_host_capabilities(
                codex_executable=codex, ai_bridge_executable=root / "missing-bridge",
                hooks_file=hooks, zshenv_file=root / ".zshenv", skill_root=skill_root,
            )

        self.assertFalse(report["desktop_adapter"]["configured"])
        self.assertIn("not fully configured", report["desktop_adapter"]["reason"] )

    def test_capability_report_rejects_stale_web_bridge_block_from_another_install(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            bridge = root / "ai-bridge"
            bridge.write_text("#!/bin/sh\n", encoding="utf-8")
            bridge.chmod(0o755)
            skill_root = root / "adaptive-delivery"
            (skill_root / "scripts").mkdir(parents=True)
            current_script = skill_root / "scripts" / "web_lifecycle_bridge.py"
            current_script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            zshenv = root / ".zshenv"
            zshenv.write_text(
                "# >>> adaptive-delivery web lifecycle bridge >>>\n"
                f'_ad_web_parent="{bridge}"\n'
                '"/usr/bin/python3" "/old/install/scripts/web_lifecycle_bridge.py" post-shell --cwd "$PWD"\n'
                "# <<< adaptive-delivery web lifecycle bridge <<<\n",
                encoding="utf-8",
            )
            report = detect_host_capabilities(
                codex_executable=root / "missing-codex",
                ai_bridge_executable=bridge,
                hooks_file=root / "hooks.json",
                zshenv_file=zshenv,
                skill_root=skill_root,
            )

        self.assertEqual(report["web_local_adapter"]["status"], "degraded")
        self.assertFalse(report["web_local_adapter"]["configured"])

    def test_installer_web_bridge_preserves_shell_and_lifecycle_exit_precedence(self):
        import subprocess
        from scripts.install_skill import _web_zshenv_block

        block = _web_zshenv_block(Path("/tmp/skill"), Path("/tmp/ai-bridge"), "/usr/bin/python3")
        self.assertNotIn("|| true", block)
        self.assertIn("ADAPTIVE_DELIVERY_WEB_SESSION_ID", block)
        self.assertIn("-o comm=", block)
        self.assertNotIn('== *\\"', block)
        self.assertNotIn("resolve-manual-web-session", block)
        self.assertIn('--web-session-id "$_ad_web_session_id"', block)
        function = block.split("  _ad_web_lifecycle_exit() {", 1)[1].split("  }\n  trap", 1)[0]
        function = "_ad_web_lifecycle_exit() {" + function + "}"
        bridge_call = '/usr/bin/python3 /tmp/skill/scripts/web_lifecycle_bridge.py post-shell --cwd "$_ad_web_cwd" --command "$_ad_web_command" --exit-code "$_ad_web_exit_code" --web-session-id "$_ad_web_session_id"'

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

    def test_capability_report_enables_detected_ai_bridge_without_making_it_core_dependency(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            bridge = root / "ai-bridge"
            bridge.write_text("#!/bin/sh\n", encoding="utf-8")
            bridge.chmod(0o755)
            report = detect_host_capabilities(
                codex_executable=root / "missing-codex",
                ai_bridge_executable=bridge,
                hooks_file=root / "hooks.json",
                zshenv_file=root / ".zshenv",
            )

        self.assertEqual(report["core"]["status"], "enabled")
        self.assertEqual(report["desktop_adapter"]["status"], "blocked")
        self.assertEqual(report["web_local_adapter"]["status"], "degraded")
        self.assertEqual(report["web_local_adapter"]["adapter"], "ai-bridge")
        self.assertFalse(report["web_local_adapter"]["configured"])

    def test_identity_capability_report_exposes_runtime_current_entry_host_contract(self):
        import hashlib
        import json
        from scripts.install_skill import (
            MANIFEST_NAME,
            RUNTIME_HOST_TOOL_HOOK_BUNDLE_FILES,
            _installed_controller_identity_capability,
        )
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            scripts = root / "scripts"; scripts.mkdir()
            guard = scripts / "controller_target_guard.py"
            guard.write_text(
                "#!/usr/bin/env python3\nimport json,sys\n"
                "print(json.dumps({'schema_version':1,'canonical_identity_cli':'controller_target_guard.py identity',"
                "'capabilities':['controller_identity_projection','same_controller_recovery','web_session_binding','target_generation_fence','logical_agent_target_resolution','verified_execution_target_fence'],'logical_agent_target_resolution_contract':'logical_agent_target_resolution_v1','verified_execution_target_contract':'verified_execution_target_v1','supported_logical_agent_types':['controller','agent','reviewer','runtime_repair_agent'],'logical_agent_target_resolution_states':['VERIFIED','UNRESOLVED','STALE','CONFLICTED'],'ownership_resolver_scope':'controller_registry_only'}))\n",
                encoding="utf-8",
            )
            guard.chmod(0o755)
            bridge = scripts / "web_lifecycle_bridge.py"
            bridge.write_text(
                "def discover_current_web_entry_for_logical_agent(): pass\n"
                "logical_agent_identity = {}\n"
                "HOST_OPERATION = \"discover_current_entry\"\n"
                "PROVENANCE = \"runtime_host_current_entry_v1\"\n"
                "def verified_web_execution_turn_from_current_entry(): pass\n"
                "verified_execution_turn = {}\n"
                "def runtime_web_turn_for_session_start(): pass\n"
                "def watch_runtime_web_turn_end(): pass\n"
                "runtime_web_turn_lease_v1 = True\n"
                "host_current_entry_unavailable = True\n"
                "runtime_host_verifier_cli_v2 = True\n"
                "verify_tool_pre = True\n"
                "verify_tool_terminal = True\n",
                encoding="utf-8",
            )
            tool_hook = scripts / "runtime_host_tool_hook.py"
            tool_hook.write_text(
                "PROTOCOL = 'runtime_host_tool_hook_v1'\n"
                "def handle_request(): pass\n"
                "def serve_unix_socket(): pass\n",
                encoding="utf-8",
            )
            for relative in RUNTIME_HOST_TOOL_HOOK_BUNDLE_FILES:
                path = root / relative
                if not path.exists():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("# pinned runtime dependency\n", encoding="utf-8")
            revision = "a" * 40
            files = {
                relative: hashlib.sha256((root / relative).read_bytes()).hexdigest()
                for relative in RUNTIME_HOST_TOOL_HOOK_BUNDLE_FILES
            }
            (root / MANIFEST_NAME).write_text(
                json.dumps({"schema_version": 2, "revision": revision, "files": files}),
                encoding="utf-8",
            )
            capability = _installed_controller_identity_capability(root)
        self.assertTrue(capability["runtime_current_entry_discovery_supported"])
        self.assertEqual(capability["current_entry_discovery_contract"], "runtime_host_current_entry_v1")
        self.assertEqual(capability["current_entry_host_operation"], "discover_current_entry")
        self.assertTrue(capability["host_current_entry_required"])
        self.assertTrue(capability["runtime_web_turn_identity_supported"])
        self.assertEqual(capability["verified_execution_turn_contract"], "verified_execution_turn_v1")
        self.assertEqual(capability["host_current_entry_turn_field_optional"], "runtime_invocation_id")
        self.assertTrue(capability["runtime_web_turn_edge_fallback_supported"])
        self.assertFalse(capability["host_schema_change_required_for_trace_rotation"])
        self.assertTrue(capability["machine_turn_end_required_for_trace_rotation"])
        self.assertEqual(capability["host_verifier_protocol"], "runtime_host_verifier_cli_v2")
        self.assertEqual(capability["tool_hook_protocol"], "runtime_host_tool_hook_v1")
        self.assertEqual(capability["tool_hook_path"], str(tool_hook.resolve()))
        self.assertEqual(len(capability["tool_hook_sha256"]), 64)
        self.assertEqual(capability["tool_hook_runtime_revision"], revision)
        self.assertEqual(
            capability["tool_hook_bundle_sha256"],
            {str((root / relative).resolve()): files[relative] for relative in RUNTIME_HOST_TOOL_HOOK_BUNDLE_FILES},
        )
        self.assertEqual(capability["host_attestation"], "external_current_entry_required")
        self.assertEqual(capability["logical_agent_target_resolution_contract"], "logical_agent_target_resolution_v1")
        self.assertEqual(capability["verified_execution_target_contract"], "verified_execution_target_v1")
        self.assertEqual(set(capability["supported_logical_agent_types"]), {"controller", "agent", "reviewer", "runtime_repair_agent"})
        self.assertEqual(set(capability["logical_agent_target_resolution_states"]), {"VERIFIED", "UNRESOLVED", "STALE", "CONFLICTED"})
        self.assertEqual(capability["ownership_resolver_scope"], "controller_registry_only")
        self.assertEqual(capability["automatic_problem_attribution"], "post_migration_enhancement")
        self.assertFalse(capability["strong_web_binding_available"])

    def test_identity_capability_v2_fails_closed_on_untrusted_or_unpinned_bundle(self):
        import hashlib
        import json
        from scripts.install_skill import (
            MANIFEST_NAME,
            RUNTIME_HOST_TOOL_HOOK_BUNDLE_FILES,
            _installed_controller_identity_capability,
        )

        def build(root: Path) -> tuple[Path, Path]:
            scripts = root / "scripts"
            scripts.mkdir()
            guard = scripts / "controller_target_guard.py"
            guard.write_text(
                "#!/usr/bin/env python3\nimport json\n"
                "print(json.dumps({'canonical_identity_cli':'controller_target_guard.py identity',"
                "'capabilities':['controller_identity_projection','same_controller_recovery','web_session_binding','target_generation_fence','logical_agent_target_resolution','verified_execution_target_fence'],"
                "'logical_agent_target_resolution_contract':'logical_agent_target_resolution_v1',"
                "'verified_execution_target_contract':'verified_execution_target_v1',"
                "'supported_logical_agent_types':['controller','agent','reviewer','runtime_repair_agent'],"
                "'logical_agent_target_resolution_states':['VERIFIED','UNRESOLVED','STALE','CONFLICTED']}))\n",
                encoding="utf-8",
            )
            guard.chmod(0o755)
            bridge = scripts / "web_lifecycle_bridge.py"
            bridge.write_text(
                "def discover_current_web_entry_for_logical_agent(): pass\n"
                "logical_agent_identity = {}\nHOST_OPERATION = \"discover_current_entry\"\n"
                "PROVENANCE = 'runtime_host_current_entry_v1'\n"
                "def verified_web_execution_turn_from_current_entry(): pass\n"
                "verified_execution_turn = {}\n"
                "def runtime_web_turn_for_session_start(): pass\n"
                "def watch_runtime_web_turn_end(): pass\n"
                "runtime_web_turn_lease_v1 = True\nhost_current_entry_unavailable = True\n"
                "runtime_host_verifier_cli_v2 = True\nverify_tool_pre = True\nverify_tool_terminal = True\n",
                encoding="utf-8",
            )
            hook = scripts / "runtime_host_tool_hook.py"
            hook.write_text(
                "HOOK_PROTOCOL = 'runtime_host_tool_hook_v1'\n"
                "def handle_request(): pass\ndef serve_unix_socket(): pass\n",
                encoding="utf-8",
            )
            for relative in RUNTIME_HOST_TOOL_HOOK_BUNDLE_FILES:
                path = root / relative
                if not path.exists():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("# pinned runtime dependency\n", encoding="utf-8")
            files = {
                relative: hashlib.sha256((root / relative).read_bytes()).hexdigest()
                for relative in RUNTIME_HOST_TOOL_HOOK_BUNDLE_FILES
            }
            (root / MANIFEST_NAME).write_text(
                json.dumps({"schema_version": 2, "revision": "b" * 40, "files": files}),
                encoding="utf-8",
            )
            return hook, scripts

        for case in ("tamper", "symlink", "writable_parent", "missing"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                hook, scripts = build(root)
                if case == "tamper":
                    hook.write_text(hook.read_text(encoding="utf-8") + "# tampered\n", encoding="utf-8")
                elif case == "symlink":
                    replacement = root / "replacement.py"
                    replacement.write_text(hook.read_text(encoding="utf-8"), encoding="utf-8")
                    hook.unlink()
                    hook.symlink_to(replacement)
                elif case == "writable_parent":
                    scripts.chmod(0o775)
                else:
                    (root / RUNTIME_HOST_TOOL_HOOK_BUNDLE_FILES[-1]).unlink()
                capability = _installed_controller_identity_capability(root)
                self.assertEqual(capability["host_verifier_protocol"], "runtime_host_verifier_cli_v1")
                self.assertIsNone(capability["tool_hook_protocol"])
                self.assertIsNone(capability["tool_hook_bundle_sha256"])

    def test_identity_capability_report_surfaces_missing_installed_guard_as_contract_drift(self):
        from scripts.install_skill import detect_host_capabilities
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "scripts").mkdir()
            report = detect_host_capabilities(
                skill_root=root,
                codex_executable=root / "missing-codex",
                ai_bridge_executable=root / "missing-bridge",
                hooks_file=root / "hooks.json",
                zshenv_file=root / ".zshenv",
                desktop_canary_file=root / "canary.json",
                health_service_plist=root / "health.plist",
                web_event_source_receipt=root / "event-source.json",
            )
        identity = report["controller_identity"]
        self.assertEqual(identity["state"], "RUNTIME_CONTRACT_DRIFT")
        self.assertEqual(identity["status"], "degraded")
        self.assertFalse(identity["configured"])


class InstallMigrationContractTests(unittest.TestCase):
    def test_canonical_release_source_requires_published_main(self):
        import subprocess

        from scripts.install_skill import verify_canonical_release_source

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = self.make_source(root)

            proof = verify_canonical_release_source(source)
            revision = subprocess.check_output(
                ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
            ).strip()
            self.assertEqual(proof["status"], "verified")
            self.assertEqual(proof["branch"], "main")
            self.assertEqual(proof["upstream"], "origin/main")
            self.assertEqual(proof["revision"], revision)
            self.assertEqual(proof["upstream_revision"], revision)

            subprocess.run(
                ["git", "-C", str(source), "checkout", "-b", "feature/not-a-release"],
                check=True,
                capture_output=True,
            )
            with self.assertRaisesRegex(ValueError, "canonical main branch"):
                verify_canonical_release_source(source)

            subprocess.run(
                ["git", "-C", str(source), "checkout", "main"],
                check=True,
                capture_output=True,
            )
            (source / "SKILL.md").write_text(
                "---\nname: adaptive-agent-runtime\n---\n# unpublished\n", encoding="utf-8"
            )
            subprocess.run(["git", "-C", str(source), "add", "SKILL.md"], check=True)
            subprocess.run(
                ["git", "-C", str(source), "commit", "-m", "unpublished"],
                check=True,
                capture_output=True,
            )
            with self.assertRaisesRegex(ValueError, "must equal published upstream"):
                verify_canonical_release_source(source)

    def test_cli_reports_default_target_conflict_without_traceback(self):
        import contextlib
        import io
        from unittest.mock import patch
        from scripts.install_skill import main

        output = io.StringIO()
        with patch("scripts.install_skill.default_install_target", side_effect=ValueError("multiple skill installs detected")):
            with contextlib.redirect_stdout(output):
                code = main([
                    "--source", "/tmp/source", "--summary", "rename conflict",
                    "--impact", "none", "--stop-condition", "choose one install",
                    "--no-configure-host-adapters",
                ])

        self.assertEqual(code, 1)
        self.assertIn("multiple skill installs", output.getvalue())

    def test_default_target_uses_new_name_but_reuses_an_existing_legacy_install(self):
        from scripts.install_skill import default_install_target
        with tempfile.TemporaryDirectory() as d:
            skills_root = Path(d) / "skills"

            self.assertEqual(default_install_target(skills_root), skills_root / "adaptive-agent-runtime")

            legacy = skills_root / "adaptive-delivery"
            legacy.mkdir(parents=True)
            self.assertEqual(default_install_target(skills_root), legacy)

            current = skills_root / "adaptive-agent-runtime"
            current.mkdir()
            with self.assertRaisesRegex(ValueError, "multiple skill installs"):
                default_install_target(skills_root)

    def test_install_blocks_when_new_and_legacy_skill_directories_both_exist(self):
        from scripts.install_skill import install_skill
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = self.make_source(root)
            skills_root = root / "installed"
            legacy = skills_root / "adaptive-delivery"
            current = skills_root / "adaptive-agent-runtime"
            legacy.mkdir(parents=True)
            current.mkdir()

            with self.assertRaisesRegex(ValueError, "multiple skill installs"):
                install_skill(
                    source, current, summary="rename conflict", impact="none",
                    stop_condition="choose one canonical install",
                )

    def test_install_blocks_explicit_new_target_beside_existing_legacy_install(self):
        from scripts.install_skill import install_skill
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = self.make_source(root)
            skills_root = root / "installed"
            legacy = skills_root / "adaptive-delivery"
            current = skills_root / "adaptive-agent-runtime"
            legacy.mkdir(parents=True)

            with self.assertRaisesRegex(ValueError, "multiple skill installs"):
                install_skill(
                    source, current, summary="rename conflict", impact="none",
                    stop_condition="reuse legacy install",
                )

    def make_source(self, root: Path) -> Path:
        import subprocess
        source = root / "source"
        source.mkdir()
        subprocess.run(["git", "-C", str(source), "init", "-b", "main"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(source), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(source), "config", "user.name", "Test"], check=True)
        (source / "SKILL.md").write_text("---\nname: adaptive-agent-runtime\n---\n# Adaptive Agent Runtime\n", encoding="utf-8")
        (source / "scripts").mkdir()
        for name in (
            "web_lifecycle_bridge.py", "lifecycle_hook.py", "controller_scoring_hook.py",
            "web_agent_health_supervisor.py", "controller_runtime_supervisor.py",
            "web_agent_events.py", "route_contract.py", "reviewer_supervisor.py",
            "control_event_guard.py", "event_scope_guard.py", "controller_state.py", "controller_target_guard.py", "agent_target_resolution.py", "assignment_lease_guard.py", "assignment_runtime.py",
            "controller_scoring_guard.py", "project_context_guard.py", "rule_handshake.py", "evaluation_transaction.py",
        ):
            script = source / "scripts" / name
            if name == "controller_target_guard.py":
                script.write_text(
                    "#!/usr/bin/env python3\n"
                    "import json, sys\n"
                    "if len(sys.argv) > 1 and sys.argv[1] == 'capabilities':\n"
                    "    print(json.dumps({'schema_version': 1, 'canonical_identity_cli': 'controller_target_guard.py identity', 'capabilities': ['controller_identity_projection', 'same_controller_recovery', 'web_session_binding', 'target_generation_fence', 'logical_agent_target_resolution', 'verified_execution_target_fence'], 'logical_agent_target_resolution_contract': 'logical_agent_target_resolution_v1', 'verified_execution_target_contract': 'verified_execution_target_v1', 'supported_logical_agent_types': ['controller', 'agent', 'reviewer', 'runtime_repair_agent'], 'logical_agent_target_resolution_states': ['VERIFIED', 'UNRESOLVED', 'STALE', 'CONFLICTED'], 'ownership_resolver_scope': 'controller_registry_only'}))\n"
                    "    raise SystemExit(0)\n"
                    "raise SystemExit(0)\n",
                    encoding="utf-8",
                )
            else:
                script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            script.chmod(0o755)
        subprocess.run(["git", "-C", str(source), "add", "."], check=True)
        subprocess.run(["git", "-C", str(source), "commit", "-m", "initial"], check=True, capture_output=True)
        remote = root / "source-origin.git"
        subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(source), "remote", "add", "origin", str(remote)], check=True)
        subprocess.run(
            ["git", "-C", str(source), "push", "-u", "origin", "main"],
            check=True,
            capture_output=True,
        )
        return source

    def test_existing_legacy_manifest_upgrades_in_place_with_new_product_metadata(self):
        import json
        import subprocess
        from scripts.install_skill import MANIFEST_NAME, install_skill
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = self.make_source(root)
            old_revision = subprocess.run(
                ["git", "-C", str(source), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
            ).stdout.strip()
            target = root / "installed" / "adaptive-delivery"
            target.mkdir(parents=True)
            (target / MANIFEST_NAME).write_text(json.dumps({
                "schema_version": 1, "revision": old_revision, "files": {}
            }), encoding="utf-8")
            manifest = install_skill(
                source, target, summary="rename metadata", impact="none", stop_condition="continue compatible"
            )

            self.assertEqual(target.name, "adaptive-delivery")
            self.assertEqual(manifest["product_name"], "Adaptive Agent Runtime")
            self.assertEqual(manifest["product_slug"], "adaptive-agent-runtime")
            self.assertEqual(manifest["skill_id"], "adaptive-agent-runtime")
            self.assertEqual(manifest["previous_revision"], old_revision)
            self.assertIn("capabilities", manifest)
            self.assertEqual(manifest["capabilities"]["core"]["status"], "enabled")
            self.assertFalse((target.parent / "adaptive-agent-runtime").exists())

    def test_install_rejects_previous_revision_override_of_manifest_truth(self):
        import json
        import subprocess
        from scripts.install_skill import MANIFEST_NAME, install_skill

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = self.make_source(root)
            installed_revision = subprocess.check_output(
                ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
            ).strip()
            (source / "SKILL.md").write_text(
                "---\nname: adaptive-agent-runtime\n---\n# Adaptive Agent Runtime\n# next\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "-C", str(source), "add", "."], check=True)
            subprocess.run(["git", "-C", str(source), "commit", "-m", "next"], check=True, capture_output=True)
            candidate = subprocess.check_output(
                ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
            ).strip()
            target = root / "installed" / "adaptive-delivery"
            target.mkdir(parents=True)
            (target / MANIFEST_NAME).write_text(json.dumps({
                "schema_version": 1,
                "revision": installed_revision,
                "files": {},
            }), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "previous revision override"):
                install_skill(
                    source,
                    target,
                    summary="must trust installed manifest",
                    impact="none",
                    stop_condition="manifest revision is authoritative",
                    previous_revision=candidate,
                )

    def test_install_blocks_upgrade_when_installed_revision_is_absent_from_candidate_history(self):
        import json
        from scripts.install_skill import MANIFEST_NAME, install_skill

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = self.make_source(root)
            target = root / "installed" / "adaptive-delivery"
            target.mkdir(parents=True)
            missing_revision = "a" * 40
            (target / MANIFEST_NAME).write_text(
                json.dumps({"schema_version": 1, "revision": missing_revision, "files": {}}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "absent from candidate source history"):
                install_skill(
                    source,
                    target,
                    summary="must not forget installed hotfix lineage",
                    impact="none",
                    stop_condition="lineage preserved",
                )

    def test_install_blocks_non_ancestor_runtime_upgrade(self):
        import subprocess
        from scripts.install_skill import install_skill

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = self.make_source(root)
            base = subprocess.check_output(
                ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
            ).strip()
            subprocess.run(["git", "-C", str(source), "checkout", "-b", "hotfix"], check=True, capture_output=True)
            (source / "scripts" / "web_lifecycle_bridge.py").write_text(
                "#!/usr/bin/env python3\n# installed hotfix\n", encoding="utf-8"
            )
            subprocess.run(["git", "-C", str(source), "add", "."], check=True)
            subprocess.run(["git", "-C", str(source), "commit", "-m", "hotfix"], check=True, capture_output=True)
            hotfix = subprocess.check_output(
                ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
            ).strip()
            target = root / "installed" / "adaptive-delivery"
            first = install_skill(
                source, target, summary="install hotfix", impact="none", stop_condition="hotfix active"
            )
            self.assertEqual(first["revision"], hotfix)

            subprocess.run(["git", "-C", str(source), "checkout", "-B", "main", base], check=True, capture_output=True)
            (source / "SKILL.md").write_text(
                "---\nname: adaptive-agent-runtime\n---\n# Adaptive Agent Runtime\n# unrelated mainline work\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "-C", str(source), "add", "."], check=True)
            subprocess.run(["git", "-C", str(source), "commit", "-m", "mainline"], check=True, capture_output=True)

            with self.assertRaisesRegex(ValueError, "does not descend from the installed revision"):
                install_skill(
                    source,
                    target,
                    summary="must not replace hotfix from divergent main",
                    impact="none",
                    stop_condition="linear history only",
                )

    def test_install_records_verified_linear_upgrade_lineage(self):
        import subprocess
        from scripts.install_skill import install_skill

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = self.make_source(root)
            target = root / "installed" / "adaptive-delivery"
            first = install_skill(
                source, target, summary="base", impact="none", stop_condition="base installed"
            )
            (source / "SKILL.md").write_text(
                "---\nname: adaptive-agent-runtime\n---\n# Adaptive Agent Runtime\n# next\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "-C", str(source), "add", "."], check=True)
            subprocess.run(["git", "-C", str(source), "commit", "-m", "next"], check=True, capture_output=True)
            second = install_skill(
                source, target, summary="next", impact="none", stop_condition="linear upgrade"
            )

            self.assertEqual(second["previous_revision"], first["revision"])
            self.assertEqual(second["upgrade_lineage"]["status"], "linear")
            self.assertEqual(second["upgrade_lineage"]["previous_revision"], first["revision"])
            self.assertEqual(second["upgrade_lineage"]["revision"], second["revision"])

    def test_full_web_runtime_upgrade_cannot_delete_marker_to_skip_release_gate(self):
        import json
        import subprocess
        from scripts.install_skill import MANIFEST_NAME, install_skill

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = self.make_source(root)
            (source / "scripts" / "web_agent_execution.py").write_text("# web runtime\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(source), "add", "."], check=True)
            subprocess.run(["git", "-C", str(source), "commit", "-m", "full runtime"], check=True, capture_output=True)
            installed_revision = subprocess.check_output(
                ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
            ).strip()
            target = root / "installed" / "adaptive-delivery"
            target.mkdir(parents=True)
            (target / "scripts").mkdir()
            (target / "scripts" / "web_agent_execution.py").write_text("# installed web runtime\n", encoding="utf-8")
            (target / MANIFEST_NAME).write_text(json.dumps({
                "schema_version": 1,
                "revision": installed_revision,
                "files": {"scripts/web_agent_execution.py": "present"},
            }), encoding="utf-8")

            (source / "scripts" / "web_agent_execution.py").unlink()
            subprocess.run(["git", "-C", str(source), "add", "-A"], check=True)
            subprocess.run(["git", "-C", str(source), "commit", "-m", "accidentally remove web runtime"], check=True, capture_output=True)

            with self.assertRaisesRegex(ValueError, "required files are missing"):
                install_skill(
                    source,
                    target,
                    summary="must not skip regression gate",
                    impact="none",
                    stop_condition="full runtime release gate remains mandatory",
                )

    def test_runtime_release_regression_gate_requires_contract_files_for_full_web_runtime(self):
        import subprocess
        from scripts.install_skill import _verify_runtime_release_regressions

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = self.make_source(root)
            (source / "scripts" / "web_agent_execution.py").write_text("# web runtime\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(source), "add", "."], check=True)
            subprocess.run(["git", "-C", str(source), "commit", "-m", "full web runtime marker"], check=True, capture_output=True)
            revision = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()

            with self.assertRaisesRegex(ValueError, "required files are missing"):
                _verify_runtime_release_regressions(source, revision)

    def test_runtime_release_gate_includes_initial_native_supersession_fence(self):
        from scripts.install_skill import RUNTIME_RELEASE_REGRESSION_TESTS

        self.assertIn(
            "tests.test_web_lifecycle_bridge.WebAutoStopSupervisorCoalescingTests.test_stale_supervisor_cannot_native_wake_after_supersession",
            RUNTIME_RELEASE_REGRESSION_TESTS,
        )
        self.assertIn(
            "tests.test_web_lifecycle_bridge.WebAutoStopSupervisorCoalescingTests.test_execute_native_resume_stale_supervisor_token_blocks_process_launch",
            RUNTIME_RELEASE_REGRESSION_TESTS,
        )
        self.assertIn(
            "tests.test_web_lifecycle_bridge.WebAutoStopSupervisorCoalescingTests.test_superseded_supervisor_cannot_start_native_recovery_bootstrap_after_resume",
            RUNTIME_RELEASE_REGRESSION_TESTS,
        )

    def test_runtime_release_gate_tracks_independent_terminal_reconcile_generations(self):
        from scripts.install_skill import RUNTIME_RELEASE_REGRESSION_TESTS

        self.assertIn(
            "tests.test_terminal_continuation.PendingTerminalReconcileTests."
            "test_reconcile_pending_accepts_independent_desktop_target_and_ownership_generations",
            RUNTIME_RELEASE_REGRESSION_TESTS,
        )
        self.assertNotIn(
            "tests.test_terminal_continuation.PendingTerminalReconcileTests."
            "test_reconcile_pending_rejects_target_generation_change_before_publish",
            RUNTIME_RELEASE_REGRESSION_TESTS,
        )

    def test_runtime_release_gate_includes_current_entry_receipt_and_distinct_live_e2e_rearm(self):
        from scripts.install_skill import RUNTIME_RELEASE_REGRESSION_TESTS

        required = {
            "tests.test_web_lifecycle_bridge.WebCurrentEntryDiscoveryTests.test_session_start_auto_discovery_preserves_signed_current_entry_envelope_for_verifier",
            "tests.test_web_lifecycle_bridge.WebCurrentEntryDiscoveryTests.test_session_start_uses_supplied_host_current_entry_without_rediscovery",
            "tests.test_web_lifecycle_bridge.WebCurrentEntryDiscoveryTests.test_session_start_preserves_signed_current_entry_envelope_for_verifier",
            "tests.test_web_lifecycle_bridge.WebLifecycleBridgeTests.test_registered_web_verifier_submit_adapter_ignores_runtime_local_path_kwargs",
            "tests.test_web_lifecycle_bridge.WebLifecycleBridgeTests.test_registered_web_verifier_submit_adapter_maps_controller_active_to_deferred_active",
            "tests.test_web_lifecycle_bridge.WebLifecycleBridgeTests.test_registered_web_verifier_submit_adapter_rejects_controller_active_after_dispatch",
            "tests.test_web_lifecycle_bridge.WebLifecycleBridgeTests.test_registered_web_verifier_submit_adapter_rejects_controller_active_generation_mismatch",
            "tests.test_web_lifecycle_bridge.WebLifecycleBridgeTests.test_registered_web_verifier_submit_adapter_keeps_other_retryable_failures_bounded",
            "tests.test_web_lifecycle_bridge.WebLocalReentryIntegrationTests.test_detached_supervisor_defers_while_web_response_is_active_and_retries_without_counting_progress",
            "tests.test_web_lifecycle_bridge.WebLifecycleBridgeTests.test_registered_v2_identity_evidence_consumes_signed_current_entry_without_reattest",
            "tests.test_web_lifecycle_bridge.WebContinuationSupervisorBootstrapTests.test_ensure_supervisor_uses_new_receipt_for_new_rule_live_e2e_after_old_result_unknown",
        }
        self.assertTrue(required.issubset(set(RUNTIME_RELEASE_REGRESSION_TESTS)))

    def test_runtime_release_gate_includes_host_tool_receipt_closure_contract(self):
        from scripts.install_skill import RUNTIME_RELEASE_REGRESSION_TESTS, RUNTIME_RELEASE_REQUIRED_FILES

        required_tests = {
            "tests.test_controller_target_guard.ControllerTargetGuardTests."
            "test_host_tool_preparation_persists_full_tuple_and_redacts_receipt_material",
            "tests.test_controller_target_guard.ControllerTargetGuardTests."
            "test_host_tool_preparation_rejects_nonce_receipt_and_execution_replays_after_reload",
            "tests.test_runtime_host_tool_hook.RuntimeHostToolHookTests."
            "test_verified_pre_is_prepared_and_dispatches_same_execution_id",
            "tests.test_runtime_host_tool_hook.RuntimeHostToolHookTests."
            "test_pre_requires_verifier_v2_tool_capability",
            "tests.test_runtime_host_tool_hook.RuntimeHostToolHookTests."
            "test_registered_v2_verifier_cli_is_used_across_the_real_process_boundary",
            "tests.test_runtime_host_tool_hook.RuntimeHostToolHookTests."
            "test_terminal_requires_structured_success_and_closes_after_lifecycle_commit",
            "tests.test_runtime_host_tool_hook.RuntimeHostToolHookTests."
            "test_terminal_without_pre_and_generation_rotation_fail_closed",
            "tests.test_runtime_host_tool_hook.RuntimeHostToolHookTests."
            "test_real_unix_server_correlates_request_and_owns_socket_mode",
            "tests.test_runtime_host_tool_hook.RuntimeHostToolHookTests."
            "test_unix_server_rejects_unsafe_paths_and_bad_frames",
            "tests.test_controller_target_guard.ControllerTargetGuardTests."
            "test_host_tool_terminal_retry_is_exact_and_direct_close_is_disabled",
            "tests.test_governance.DurableHostToolReceiptPersistenceTests."
            "test_lifecycle_commit_fsync_failure_keeps_registry_pending_and_exact_retry_is_single_trace",
            "tests.test_governance.DurableHostToolReceiptPersistenceTests."
            "test_verified_terminal_times_out_hung_snapshot_git_without_closed_or_trace",
            "tests.test_governance.GovernanceTests."
            "test_web_stdout_marker_never_closes_without_private_terminal_commit",
            "tests.test_web_lifecycle_bridge.WebLifecycleBridgeTests."
            "test_loaded_verifier_rejects_writable_members_parents_and_replaced_path",
            "tests.test_web_lifecycle_bridge.ControllerWakeSupervisorTests."
            "test_audit_wake_retry_rejects_same_id_receipt_shape_replacement",
        }
        self.assertTrue(required_tests.issubset(set(RUNTIME_RELEASE_REGRESSION_TESTS)))
        self.assertIn("scripts/runtime_host_tool_hook.py", RUNTIME_RELEASE_REQUIRED_FILES)
        self.assertIn("tests/test_runtime_host_tool_hook.py", RUNTIME_RELEASE_REQUIRED_FILES)
        from scripts.install_skill import RUNTIME_HOST_TOOL_HOOK_BUNDLE_FILES
        self.assertTrue(set(RUNTIME_HOST_TOOL_HOOK_BUNDLE_FILES).issubset(RUNTIME_RELEASE_REQUIRED_FILES))

    def test_runtime_release_gate_rejects_missing_host_tool_hook_or_tests(self):
        from unittest.mock import patch

        from scripts.install_skill import (
            RUNTIME_RELEASE_REQUIRED_FILES,
            _verify_runtime_release_regressions,
        )

        for missing in (
            "scripts/runtime_host_tool_hook.py",
            "tests/test_runtime_host_tool_hook.py",
        ):
            with self.subTest(missing=missing):
                entries = [
                    ("100644", "blob", "0" * 40, path)
                    for path in RUNTIME_RELEASE_REQUIRED_FILES
                    if path != missing
                ]
                with patch(
                    "scripts.install_skill._revision_tree_entries",
                    return_value=entries,
                ):
                    with self.assertRaisesRegex(ValueError, missing):
                        _verify_runtime_release_regressions(Path("/unused"), "candidate", required=True)

    def test_runtime_release_gate_includes_host_ownership_and_yield_enforcement_regressions(self):
        from scripts.install_skill import RUNTIME_RELEASE_REGRESSION_TESTS, RUNTIME_RELEASE_REQUIRED_FILES

        required_tests = {
            "tests.test_goal_display_sync.GoalDisplaySyncTests.test_rolled_happy_path_records_exact_host_sequence_and_binding",
            "tests.test_goal_display_sync.GoalDisplaySyncTests.test_successful_rolled_control_receipt_activates_display_sync_debt",
            "tests.test_goal_display_sync.GoalDisplaySyncTests.test_title_failure_recovers_without_recreating_goal",
            "tests.test_goal_display_sync.GoalDisplaySyncTests.test_host_readback_mismatch_retries_only_the_failed_read",
            "tests.test_goal_display_sync.GoalDisplaySyncTests.test_host_readback_rejects_unrelated_objective_and_split_thread_match",
            "tests.test_goal_display_sync.GoalDisplaySyncTests.test_duplicate_rollover_reuses_completed_receipt",
            "tests.test_goal_display_sync.GoalDisplaySyncTests.test_unavailable_host_tool_marks_receipt_degraded",
            "tests.test_goal_display_sync.GoalDisplaySyncTests.test_missing_host_capability_is_degraded_and_exact_target_change_is_fenced",
            "tests.test_desktop_lifecycle_adapter.DesktopOutboundLeaseHookTests.test_managed_controller_rejects_unbounded_dev_commands_before_state_write",
            "tests.test_desktop_lifecycle_adapter.DesktopOutboundLeaseHookTests.test_foreground_command_gate_allows_bounded_work_and_skips_unmanaged_sessions",
            "tests.test_desktop_lifecycle_adapter.DesktopOutboundLeaseHookTests.test_controller_without_explicit_surface_uses_its_registered_canonical_repo",
            "tests.test_desktop_lifecycle_adapter.DesktopOutboundLeaseHookTests.test_explicit_controller_surface_rejects_another_checkout",
            "tests.test_desktop_lifecycle_adapter.DesktopOutboundLeaseHookTests.test_run_hook_reuses_one_project_snapshot_for_management_fence",
            "tests.test_desktop_lifecycle_adapter.DesktopOutboundLeaseHookTests.test_current_desktop_user_prompt_persists_confirmed_native_wake",
            "tests.test_governance.GovernanceTests.test_candidate_inventory_batches_ancestry_for_multiple_worktrees",
            "tests.test_controller_target_guard.ControllerTargetGuardTests.test_claim_controller_host_desktop_after_web_increments_one_cross_host_generation",
            "tests.test_web_lifecycle_bridge.WebLocalReentryIntegrationTests.test_direct_wake_rejects_confirmed_web_result_after_desktop_handoff",
            "tests.test_web_lifecycle_bridge.WebLocalReentryIntegrationTests.test_desktop_result_cannot_persist_or_rearm_after_web_handoff",
            "tests.test_governance.GovernanceTests.test_control_loop_stop_rejection_reopens_pending_event_even_if_prior_state_was_closed",
            "tests.test_web_lifecycle_bridge.WebLifecycleBridgeTests.test_dispatch_event_result_treats_decision_block_as_logical_yield_rejection",
            "tests.test_web_agent_health_supervisor.WebAgentHealthSupervisorTests.test_health_tick_reopens_persisted_non_user_next_action_without_stop_callback",
            "tests.test_terminal_continuation.TerminalContinuationTests.test_terminal_receipt_persists_before_desktop_runtime_is_needed",
            "tests.test_web_lifecycle_bridge.WebLifecycleBridgeTests.test_production_bridge_has_no_trusted_web_attestation_verifier",
            "tests.test_web_lifecycle_bridge.WebLifecycleBridgeTests.test_registered_web_verifier_timeout_covers_product_host_request_budget",
            "tests.test_web_lifecycle_bridge.WebLifecycleBridgeTests.test_registered_web_verifier_classifies_frame_tree_timeout_as_transient",
            "tests.test_web_lifecycle_bridge.WebLifecycleBridgeTests.test_registered_web_verifier_classifies_exact_target_ambiguous_as_transient",
            "tests.test_web_lifecycle_bridge.WebLifecycleBridgeTests.test_registered_web_verifier_loads_pinned_external_runtime_host_cli",
            "tests.test_web_lifecycle_bridge.WebLifecycleBridgeTests.test_registered_web_verifier_exposes_pinned_host_submit_adapter",
            "tests.test_web_lifecycle_bridge.WebLifecycleBridgeTests.test_registered_web_verifier_rechecks_bundle_before_each_execution",
            "tests.test_web_lifecycle_bridge.WebHostNativeWakeIsolationTests.test_registered_external_web_host_submit_adapter_is_used_without_caller_injection",
            "tests.test_web_lifecycle_bridge.WebLocalReentryIntegrationTests.test_detached_supervisor_uses_registered_host_submit_adapter_for_strong_web_target",
            "tests.test_web_lifecycle_bridge.WebLocalReentryIntegrationTests.test_strong_host_confirmed_submit_waits_without_rearm",
            "tests.test_web_lifecycle_bridge.WebContinuationSupervisorBootstrapTests.test_retry_exhausted_rearms_after_host_delivery_fingerprint_change",
            "tests.test_web_lifecycle_bridge.WebContinuationSupervisorBootstrapTests.test_retry_exhausted_rearms_after_controller_fence_change_same_host_fingerprint",
            "tests.test_web_lifecycle_bridge.WebContinuationSupervisorBootstrapTests.test_ensure_supervisor_uses_new_receipt_after_controller_fence_change",
            "tests.test_web_lifecycle_bridge.WebLocalReentryIntegrationTests.test_retry_exhausted_persists_host_delivery_fingerprint",
            "tests.test_web_lifecycle_bridge.WebContinuationSupervisorBootstrapTests.test_confirmed_or_result_unknown_never_rearm_for_host_fingerprint_change",
            "tests.test_web_lifecycle_bridge.WebContinuationSupervisorBootstrapTests.test_terminal_rule_delivery_blocks_bootstrap_across_fingerprint_changes",
            "tests.test_web_lifecycle_bridge.WebContinuationSupervisorBootstrapTests.test_non_rule_delivery_key_uses_wake_generation_with_current_rule_snapshot",
            "tests.test_web_lifecycle_bridge.WebLocalReentryIntegrationTests.test_transient_web_reentry_retry_budget_exhausts_without_rearm",
            "tests.test_web_lifecycle_bridge.WebAutoStopSupervisorCoalescingTests.test_same_terminal_receipt_cannot_be_rescheduled",
            "tests.test_web_lifecycle_bridge.WebLocalReentryIntegrationTests.test_detached_supervisor_retries_transient_registered_host_attestation_failure",
            "tests.test_web_lifecycle_bridge.WebLifecycleBridgeTests.test_malformed_registered_web_verifier_config_fails_closed_without_manual_fallback",
            "tests.test_web_lifecycle_bridge.WebLifecycleBridgeTests.test_browser_tab_receipt_cannot_recover_an_unverified_web_session",
            "tests.test_web_lifecycle_bridge.WebLifecycleBridgeTests.test_manual_web_mutations_cannot_downgrade_host_attested_current_target",
            "tests.test_web_lifecycle_bridge.WebLifecycleBridgeTests.test_same_controller_web_recovery_cannot_replace_different_host_attested_current_target",
            "tests.test_web_lifecycle_bridge.StrongWebSuccessorHandoffTests.test_authorize_web_successor_records_only_fenced_fresh_session",
            "tests.test_web_lifecycle_bridge.StrongWebSuccessorHandoffTests.test_authorize_web_successor_cli_does_not_rotate_target",
            "tests.test_web_lifecycle_bridge.StrongWebSuccessorHandoffTests.test_authorized_strong_web_successor_rotates_target_and_ownership_once",
            "tests.test_web_lifecycle_bridge.StrongWebSuccessorHandoffTests.test_strong_web_successor_expired_authorization_does_not_call_verifier",
            "tests.test_web_lifecycle_bridge.StrongWebSuccessorHandoffTests.test_strong_web_successor_rechecks_target_generation_after_attestation",
            "tests.test_web_lifecycle_bridge.WebLifecycleBridgeTests.test_legacy_quarantined_target_keeps_manual_replacement_exit",
            "tests.test_web_lifecycle_bridge.WebLifecycleBridgeTests.test_same_controller_web_recovery_does_not_rotate_manual_resume_lease",
            "tests.test_web_lifecycle_bridge.WebLifecycleBridgeTests.test_session_start_verified_target_does_not_rotate_manual_resume_lease",
            "tests.test_web_lifecycle_bridge.WebLifecycleBridgeTests.test_historical_alias_cannot_recover_even_with_trusted_verifier",
            "tests.test_web_lifecycle_bridge.WebLifecycleBridgeTests.test_unbound_chat_cannot_recover_even_with_trusted_verifier",
            "tests.test_web_lifecycle_bridge.WebLifecycleBridgeTests.test_zshenv_exit_bridge_executes_and_preserves_exit_precedence",
            "tests.test_web_lifecycle_bridge.WebLifecycleComputerLeaseTests.test_audit_once_never_uses_manual_resume_lease_as_caller_identity",
            "tests.test_web_lifecycle_bridge.WebLifecycleAuditTests.test_auto_native_stop_confirms_host_observed_canonical_target_already_foreground",
            "tests.test_web_lifecycle_bridge.WebLifecycleAuditTests.test_mocked_active_writer_without_host_observation_still_rearms",
            "tests.test_web_lifecycle_bridge.WebLifecycleAuditTests.test_serialized_active_writer_claim_cannot_confirm_already_foreground",
            "tests.test_web_lifecycle_bridge.WebLifecycleAuditTests.test_execute_native_resume_marks_host_observed_active_writer_process_locally",
            "tests.test_web_lifecycle_bridge.WebLifecycleAuditTests.test_auto_native_stop_yields_external_wait_when_desktop_host_reload_is_required",
            "tests.test_web_lifecycle_bridge.WebLifecycleAuditTests.test_desktop_host_reload_gate_requires_exact_armed_zero_sequence_canary",
            "tests.test_web_lifecycle_bridge.WebLifecycleAuditTests.test_desktop_codex_resolution_prefers_the_app_bundled_runtime",
            "tests.test_web_lifecycle_bridge.WebLifecycleAuditTests.test_desktop_codex_resolution_rejects_an_invalid_explicit_override",
            "tests.test_web_lifecycle_bridge.WebLifecycleAuditTests.test_rule_wake_uses_desktop_host_adapter_without_resolving_cli",
            "tests.test_web_lifecycle_bridge.WebLifecycleAuditTests.test_codex_app_server_turn_uses_official_protocol_and_waits_for_completion",
            "tests.test_web_lifecycle_bridge.WebLifecycleAuditTests.test_codex_app_server_active_writer_fails_before_turn_submit",
            "tests.test_web_lifecycle_bridge.WebLifecycleAuditTests.test_codex_app_server_turn_start_response_timeout_is_result_unknown",
            "tests.test_web_lifecycle_bridge.WebLifecycleAuditTests.test_codex_app_server_eof_after_turn_start_confirmation_is_result_unknown",
            "tests.test_web_lifecycle_bridge.WebLifecycleAuditTests.test_desktop_host_resume_uses_app_server_under_target_and_ownership_fence",
            "tests.test_web_lifecycle_bridge.WebLifecycleAuditTests.test_desktop_host_resume_missing_app_server_fails_closed_without_cli_fallback",
            "tests.test_web_lifecycle_bridge.WebLifecycleAuditTests.test_desktop_host_resume_web_ownership_never_starts_app_server",
            "tests.test_web_lifecycle_bridge.WebLifecycleAuditTests.test_execute_native_resume_without_explicit_cli_uses_desktop_host_adapter",
            "tests.test_web_lifecycle_bridge.WebLifecycleAuditTests.test_rule_wake_does_not_require_desktop_runtime_for_web_target",
            "tests.test_web_lifecycle_bridge.WebAutoStopSupervisorCoalescingTests.test_host_neutral_supervisor_omits_missing_desktop_codex_argument",
            "tests.test_web_agent_health_supervisor.WebAgentHealthSupervisorTests.test_rule_wake_defers_host_runtime_resolution_until_target_is_known",
            "tests.test_web_agent_health_supervisor.WebAgentHealthSupervisorTests.test_global_health_cycle_isolates_one_repo_git_failure",
            "tests.test_install_skill.InstallCapabilityTests.test_installer_web_bridge_preserves_shell_and_lifecycle_exit_precedence",
            "tests.test_install_skill.ProjectContextHookInstallationTests.test_runtime_hooks_keep_trust_stable_legacy_indices",
            "tests.test_install_skill.ProjectContextHookInstallationTests.test_shifted_runtime_hook_groups_migrate_back_without_moving_user_groups",
            "tests.test_install_skill.HostAdapterInstallationTests.test_configure_host_adapters_can_update_codex_hooks_without_touching_ai_bridge",
            "tests.test_install_skill.HostAdapterInstallationTests.test_install_cli_skip_ai_bridge_never_rolls_back_concurrent_zshenv_update",
            "tests.test_install_skill.WebAgentHealthServiceInstallationTests.test_runtime_service_retires_legacy_per_controller_web_audit_after_new_service_load",
            "tests.test_install_skill.WebAgentHealthServiceInstallationTests.test_runtime_service_load_failure_preserves_legacy_web_audit",
            "tests.test_web_reentry_adapter.WebReentryAdapterTests.test_resolve_reentry_session_strong_host_target_does_not_require_manual_lease",
            "tests.test_web_reentry_adapter.WebReentryAdapterTests.test_resolve_reentry_session_strong_host_target_requires_matching_web_ownership",
            "tests.test_web_reentry_adapter.WebReentryAdapterTests.test_reentry_without_canonical_web_ownership_never_calls_browser",
            "tests.test_web_reentry_adapter.WebReentryAdapterTests.test_reentry_without_explicit_canonical_web_target_never_calls_browser",
            "tests.test_web_reentry_adapter.WebReentryAdapterTests.test_reentry_without_registered_host_origin_verifier_never_calls_browser",
            "tests.test_web_reentry_adapter.WebReentryAdapterTests.test_legacy_browser_attested_target_is_quarantined_before_browser_use",
            "tests.test_web_reentry_adapter.WebReentryAdapterTests.test_submit_holds_registry_fence_against_target_rotation",
            "tests.test_web_reentry_adapter.WebReentryAdapterTests.test_target_generation_change_before_submit_never_types",
            "tests.test_web_lifecycle_bridge.WebLocalReentryIntegrationTests.test_web_supervisor_rejects_confirmed_receipt_for_noncanonical_target",
            "tests.test_web_lifecycle_bridge.WebLocalReentryIntegrationTests.test_builtin_web_reentry_without_registered_origin_verifier_never_calls_browser_adapter",
            "tests.test_web_reentry_adapter.ManualFencedWebReentryTests.test_missing_host_verifier_allows_only_manual_fenced_exact_current_target",
            "tests.test_web_reentry_adapter.ManualFencedWebReentryTests.test_manual_fenced_reentry_rejects_generation_or_lease_mismatch_before_browser",
            "tests.test_web_reentry_adapter.ManualFencedWebReentryTests.test_invalid_registered_host_verifier_never_falls_back_to_manual_fenced_delivery",
            "tests.test_web_reentry_adapter.ManualFencedWebReentryTests.test_manual_fenced_submit_holds_registry_and_lease_fences",
            "tests.test_web_lifecycle_bridge.WebLocalReentryIntegrationTests.test_manual_fenced_direct_wake_uses_builtin_adapter_without_peer_verifier",
            "tests.test_web_lifecycle_bridge.WebLocalReentryIntegrationTests.test_manual_fenced_supervisor_persists_unverified_delivery_evidence",
            "tests.test_web_reentry_adapter.ManualFencedWebReentryTests.test_explicit_bridge_verifier_rejection_never_falls_back_when_module_verifier_missing",
            "tests.test_web_lifecycle_bridge.WebLocalReentryIntegrationTests.test_manual_fenced_direct_wake_passes_bridge_verifier_into_builtin_adapter",
            "tests.test_web_lifecycle_bridge.WebLocalReentryIntegrationTests.test_manual_fenced_supervisor_passes_bridge_verifier_into_builtin_adapter",
            "tests.test_web_lifecycle_bridge.WebLocalReentryIntegrationTests.test_manual_fenced_confirmed_waits_for_progress_without_resubmit",
            "tests.test_web_lifecycle_bridge.WebLocalReentryIntegrationTests.test_waiting_for_controller_progress_does_not_bootstrap_until_machine_facts_change",
            "tests.test_web_lifecycle_bridge.WebContinuationSupervisorBootstrapTests.test_identity_blocked_same_event_and_registry_are_not_bootstrapped_again",
            "tests.test_web_lifecycle_bridge.WebContinuationSupervisorBootstrapTests.test_identity_blocked_event_retries_after_registry_changes",
            "tests.test_web_lifecycle_bridge.WebContinuationSupervisorBootstrapTests.test_nonretryable_web_failure_same_event_and_fence_stays_quiet",
            "tests.test_web_lifecycle_bridge.WebLocalReentryIntegrationTests.test_registered_host_nonretryable_failure_persists_quiet_fence",
            "tests.test_web_lifecycle_bridge.WebLocalReentryIntegrationTests.test_registered_host_result_unknown_persists_quiet_fence",
            "tests.test_web_lifecycle_bridge.WebLocalReentryIntegrationTests.test_confirmed_web_reentry_clears_stale_nonretryable_block_evidence",
            "tests.test_web_lifecycle_bridge.WebHostNativeWakeIsolationTests.test_registered_current_web_adapter_is_fenced_and_host_attested",
            "tests.test_web_lifecycle_bridge.WebHostNativeWakeIsolationTests.test_current_web_adapter_is_not_called_when_pre_delivery_attestation_rejects",
            "tests.test_web_lifecycle_bridge.WebHostNativeWakeIsolationTests.test_current_web_adapter_receipt_must_correlate_origin_call_receipt",
            "tests.test_web_lifecycle_bridge.WebHostNativeWakeIsolationTests.test_current_web_adapter_is_not_called_for_malformed_origin_attestation",
            "tests.test_web_lifecycle_bridge.WebHostNativeWakeIsolationTests.test_registered_current_web_adapter_without_ownership_is_never_called",
            "tests.test_web_lifecycle_bridge.WebReentryDebounceTests.test_web_confirmed_wake_is_not_debounced_after_same_target_ownership_reclaim",
            "tests.test_rule_handshake.RuleHandshakeTests.test_live_e2e_rejects_stale_ownership_generation_before_acceptance",
            "tests.test_rule_handshake.RuleHandshakeTests.test_fake_project_chat_cannot_ack_by_claiming_logical_controller_id",
            "tests.test_rule_handshake.RuleHandshakeTests.test_current_web_target_ack_records_exact_source_and_generations",
            "tests.test_rule_handshake.RuleHandshakeTests.test_fake_project_chat_cannot_accept_or_defer_live_e2e",
            "tests.test_rule_handshake.RuleHandshakeTests.test_ack_revalidates_source_fence_immediately_before_persist",
            "tests.test_rule_handshake.RuleHandshakeTests.test_defer_revalidates_source_fence_immediately_before_persist",
            "tests.test_rule_handshake.RuleHandshakeTests.test_accept_revalidates_source_fence_before_freezing_evidence",
            "tests.test_governance.ControllerActionSourcePromptTests.test_rule_ack_prompt_carries_logical_controller_and_actual_execution_source",
            "tests.test_governance.ControllerActionSourcePromptTests.test_live_e2e_accept_prompt_carries_actual_execution_source",
            "tests.test_governance.ControllerActionSourcePromptTests.test_web_bridge_event_uses_actual_web_conversation_as_controller_action_source",
            "tests.test_web_reentry_adapter.AiBridgeMcpDiscoveryTests.test_discovery_selects_only_live_loopback_endpoint_and_accepts_url_prefix",
            "tests.test_assignment_runtime.ExternalFailureEvidencePersistenceTests.test_terminal_persists_external_failure_class_retry_safety_and_details",
        }
        self.assertTrue(required_tests.issubset(set(RUNTIME_RELEASE_REGRESSION_TESTS)))
        self.assertIn("scripts/controller_runtime_supervisor.py", RUNTIME_RELEASE_REQUIRED_FILES)
        self.assertIn("scripts/goal_display_sync.py", RUNTIME_RELEASE_REQUIRED_FILES)
        self.assertIn("tests/test_goal_display_sync.py", RUNTIME_RELEASE_REQUIRED_FILES)
        self.assertIn("tests/test_web_agent_health_supervisor.py", RUNTIME_RELEASE_REQUIRED_FILES)
        self.assertIn("tests/test_terminal_continuation.py", RUNTIME_RELEASE_REQUIRED_FILES)
        self.assertIn("scripts/terminal_continuation.py", RUNTIME_RELEASE_REQUIRED_FILES)
        self.assertIn("scripts/assignment_runtime.py", RUNTIME_RELEASE_REQUIRED_FILES)
        self.assertIn("tests/test_assignment_runtime.py", RUNTIME_RELEASE_REQUIRED_FILES)
        self.assertIn("scripts/agent_target_resolution.py", RUNTIME_RELEASE_REQUIRED_FILES)
        self.assertIn("tests/test_agent_target_resolution.py", RUNTIME_RELEASE_REQUIRED_FILES)
        self.assertIn(
            "tests.test_terminal_continuation.PendingTerminalReconcileTests.test_reconcile_pending_classifies_legacy_assignment_without_weakening_current_lease_checks",
            RUNTIME_RELEASE_REGRESSION_TESTS,
        )

    def test_runtime_release_regression_gate_runs_required_tests_from_immutable_revision(self):
        import subprocess
        from scripts.install_skill import (
            _verify_runtime_release_regressions,
            RUNTIME_RELEASE_REGRESSION_TESTS,
            RUNTIME_RELEASE_NODE_REGRESSION_TESTS,
        )

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = self.make_source(root)
            for name in (
                "web_agent_execution.py", "web_reentry_adapter.py", "terminal_continuation.py",
                "goal_display_sync.py", "runtime_host_tool_hook.py", "controller_health.py",
                "controller_self_check.py", "ledger_consistency_guard.py", "lint_governance.py",
                "preblock_guard.py", "project_state.py",
            ):
                (source / "scripts" / name).write_text("# runtime\n", encoding="utf-8")
            (source / "scripts" / "run_external_agent.mjs").write_text(
                "export const marker = 'external-agent-routing';\n",
                encoding="utf-8",
            )
            tests_dir = source / "tests"
            tests_dir.mkdir()
            (tests_dir / "external-agent-routing.test.mjs").write_text(
                "import test from 'node:test';\n"
                "import assert from 'node:assert/strict';\n"
                "test('heterogeneous frontend and backend tasks stay on Kimi and Grok canonical executors', () => { assert.equal(1, 1); });\n"
                "test('assignment-bound execute rejects CLI route mismatch before provider spawn', () => { assert.equal(1, 1); });\n"
                "test('assignment-bound safe fallback requires canonical prior terminal before provider spawn', () => { assert.equal(1, 1); });\n"
                "test('assignment-bound external start persists exact canonical route contract', () => { assert.equal(1, 1); });\n"
                "test('short assignment-bound execution reconciles final Git progress before terminal', () => { assert.equal(1, 1); });\n"
                "test('fresh legacy v1 assignment ACK cannot launch external provider', () => { assert.equal(1, 1); });\n"
                "test('Grok execution transports prompts through a private prompt file and removes it', () => { assert.equal(1, 1); });\n"
                "test('oversized Grok reviewer prompt fails before provider spawn with sharding evidence', () => { assert.equal(1, 1); });\n"
                "test('Grok launch deadline classifies cli_launch_timeout and terminates an unconfirmed process group', () => { assert.equal(1, 1); });\n"
                "test('Grok post-launch child error preserves provider boundary evidence', () => { assert.equal(1, 1); });\n"
                "test('Grok first-output deadline starts after launch confirmation', () => { assert.equal(1, 1); });\n"
                "test('Grok first-output timeout terminates a silent provider attempt', () => { assert.equal(1, 1); });\n"
                "test('Grok generation stall timeout terminates after structured output stops', () => { assert.equal(1, 1); });\n"
                "test('Grok normal leader exit reaps surviving process-group descendants before success', () => { assert.equal(1, 1); });\n"
                "test('Grok absolute deadline kills the entire provider process group', () => { assert.equal(1, 1); });\n"
                "test('Grok side-effect timeout crosses provider boundary as result_unknown and disables retry', () => { assert.equal(1, 1); });\n"
                "test('Grok stall timeout persists structured canonical terminal classification', () => { assert.equal(1, 1); });\n"
                "test('Grok failed attempt uses 0600 prompt file and removes it', () => { assert.equal(1, 1); });\n"
                "test('oversized non-reviewer Grok prompt fails before spawn without sharding', () => { assert.equal(1, 1); });\n"
                "test('Grok stderr and assignment heartbeat do not satisfy first stdout progress', () => { assert.equal(1, 1); });\n"
                "test('Grok unstructured stdout does not satisfy structured first-output progress', () => { assert.equal(1, 1); });\n"
                "test('Grok 1.0.13 streaming text output satisfies first-output progress then stalls', () => { assert.equal(1, 1); });\n"
                "test('Grok 1.0.13 streaming thought tool-call and tool-update events count as model progress', () => { assert.equal(1, 1); });\n"
                "test('Grok 1.0.13 bare type and metadata events cannot spoof model progress', () => { assert.equal(1, 1); });\n"
                "test('Grok malformed stdout after one structured event does not prevent generation stall', () => { assert.equal(1, 1); });\n"
                "test('Grok structured metadata stdout does not satisfy model first-output progress', () => { assert.equal(1, 1); });\n"
                "test('Grok metadata after agent activity does not prevent generation stall', () => { assert.equal(1, 1); });\n"
                "test('Grok empty tool-call updates do not count as provider progress', () => { assert.equal(1, 1); });\n"
                "test('Grok misleading type or event fields do not count as ACP model progress', () => { assert.equal(1, 1); });\n"
                "test('ordinary Grok provider exit and invalid delivery persist durable failure classification', () => { assert.equal(1, 1); });\n"
                "test('Grok delivery validator binds synthesis evidence to exact assigned shard receipts', () => { assert.equal(1, 1); });\n"
                "test('Grok delivery validator requires explicit reviewer phase', () => { assert.equal(1, 1); });\n"
                "test('Grok reviewer shard cannot finalize and synthesis binds exact candidate head', () => { assert.equal(1, 1); });\n"
                "test('Grok reviewer requires explicit phase and immutable candidate commit', () => { assert.equal(1, 1); });\n"
                "test('Grok synthesis validates canonical same-candidate shard receipts', () => { assert.equal(1, 1); });\n"
                "test('Grok cleanup uncertainty preserves cleanup failure class while remaining result unknown', () => { assert.equal(1, 1); });\n"
                "test('Grok side-effect provider uncertainty elevates a known timeout to result_unknown', () => { assert.equal(1, 1); });\n"
                "test('Grok cleanup uncertainty is result unknown and not retry safe', () => { assert.equal(1, 1); });\n"
                "test('cleanup uncertainty is fail closed and result unknown', () => { assert.equal(1, 1); });\n"
                "test('Grok payload or data wrappers cannot spoof ACP model progress', () => { assert.equal(1, 1); });\n"
                "test('Grok prompt preparation cleans a temp directory when prompt write fails', () => { assert.equal(1, 1); });\n"
                "test('Grok prompt write plus cleanup failure is fail closed', () => { assert.equal(1, 1); });\n"
                "test('Grok pure-packet Reviewer uses no tools, no planning, structured verdict, and sufficient turn budget', () => { assert.equal(1, 1); });\n"
                "test('Grok Reviewer structured PASS and FAIL are validated independently from process exit', () => { assert.equal(1, 1); });\n"
                "test('Grok Reviewer max turns without verdict is REVIEW_MAX_TURNS, never PASS', () => { assert.equal(1, 1); });\n"
                "test('Grok Reviewer timeout with residual process group is REVIEW_PROCESS_STUCK', () => { assert.equal(1, 1); });\n"
                "test('Grok work_type=review accepts only structured PASS into canonical acceptance', () => { assert.equal(1, 1); });\n"
                "test('Grok work_type=review valid FAIL is terminal findings and is never retried into PASS', () => { assert.equal(1, 1); });\n"
                "test('Grok work_type=review malformed verdict persists REVIEW_OUTPUT_INVALID and no retry', () => { assert.equal(1, 1); });\n"
                "test('Grok Reviewer stall after stdout closes reaps TERM-resistant relay descendants', () => { assert.equal(1, 1); });\n"
                "test('ordinary Grok parent SIGTERM also performs bounded process-group cleanup', () => { assert.equal(1, 1); });\n"
                "test('Grok work_type=review retries transient pre-output failure only once then accepts PASS', () => { assert.equal(1, 1); });\n"
                "test('Grok Reviewer waits for stdio close before classifying final verdict', () => { assert.equal(1, 1); });\n"
                "test('Grok 1.0.13 json-schema envelope validates structuredOutput as Reviewer verdict', () => { assert.equal(1, 1); });\n"
                "test('Grok json-schema envelope rejects conflicting text and structuredOutput', () => { assert.equal(1, 1); });\n"
                "test('Grok Reviewer returns validated verdict after bounded cleanup even if CLI does not exit', () => { assert.equal(1, 1); });\n"
                "test('run_external_agent direct execution survives symlinked filesystem path', () => { assert.equal(1, 1); });\n"
                "test('cleanup failure preserves prior Grok provider exit evidence', () => { assert.equal(1, 1); });\n",
                encoding="utf-8",
            )
            (tests_dir / "__init__.py").write_text("", encoding="utf-8")
            (tests_dir / "test_assignment_runtime.py").write_text(
                "import unittest\n"
                "class ExternalFailureEvidencePersistenceTests(unittest.TestCase):\n"
                "    def test_terminal_persists_external_failure_class_retry_safety_and_details(self): self.assertTrue(True)\n"
                "class ReviewerRuntimeContractTests(unittest.TestCase):\n"
                "    def test_reviewer_terminal_persists_structured_review_status(self): self.assertTrue(True)\n"
                "    def test_reviewer_terminal_rejects_review_status_that_conflicts_with_delivery(self): self.assertTrue(True)\n"
                "    def test_reviewer_infra_status_requires_unresolved_delivery_and_no_verdict(self): self.assertTrue(True)\n",
                encoding="utf-8",
            )
            (tests_dir / "test_reviewer_supervisor.py").write_text(
                "import unittest\n"
                "class ReviewerSupervisorRoutingTests(unittest.TestCase):\n"
                "    def test_web_controller_review_does_not_launch_codex_directly(self): self.assertTrue(True)\n"
                "class ReviewerSupervisorWebHandoffTests(unittest.TestCase):\n"
                "    def test_web_review_emits_canonical_dispatch_request_without_codex(self): self.assertTrue(True)\n"
                "    def test_web_review_finalizes_only_from_canonical_runtime_reviewer_lease(self): self.assertTrue(True)\n",
                encoding="utf-8",
            )
            (tests_dir / "test_web_reentry_adapter.py").write_text(
                "import unittest\n"
                "class WebReentryContinuationRegressionTests(unittest.TestCase):\n"
                "    def test_transient_web_reentry_failure_rearms_existing_continuation_supervisor(self):\n"
                "        self.assertTrue(True)\n"
                "class WebReentryAdapterTests(unittest.TestCase):\n"
                "    def test_resolve_reentry_session_strong_host_target_does_not_require_manual_lease(self): self.assertTrue(True)\n"
                "    def test_resolve_reentry_session_strong_host_target_requires_matching_web_ownership(self): self.assertTrue(True)\n"
                "    def test_reentry_without_canonical_web_ownership_never_calls_browser(self): self.assertTrue(True)\n"
                "    def test_reentry_without_explicit_canonical_web_target_never_calls_browser(self): self.assertTrue(True)\n"
                "    def test_reentry_without_registered_host_origin_verifier_never_calls_browser(self): self.assertTrue(True)\n"
                "    def test_legacy_browser_attested_target_is_quarantined_before_browser_use(self): self.assertTrue(True)\n"
                "    def test_submit_holds_registry_fence_against_target_rotation(self): self.assertTrue(True)\n"
                "    def test_target_generation_change_before_submit_never_types(self): self.assertTrue(True)\n"
                "class ManualFencedWebReentryTests(unittest.TestCase):\n"
                "    def test_missing_host_verifier_allows_only_manual_fenced_exact_current_target(self): self.assertTrue(True)\n"
                "    def test_manual_fenced_reentry_rejects_generation_or_lease_mismatch_before_browser(self): self.assertTrue(True)\n"
                "    def test_invalid_registered_host_verifier_never_falls_back_to_manual_fenced_delivery(self): self.assertTrue(True)\n"
                "    def test_manual_fenced_submit_holds_registry_and_lease_fences(self): self.assertTrue(True)\n"
                "    def test_explicit_bridge_verifier_rejection_never_falls_back_when_module_verifier_missing(self): self.assertTrue(True)\n"
                "class AiBridgeMcpDiscoveryTests(unittest.TestCase):\n"
                "    def test_discovery_selects_only_live_loopback_endpoint_and_accepts_url_prefix(self): self.assertTrue(True)\n",
                encoding="utf-8",
            )
            (tests_dir / "test_web_collaboration_continuation.py").write_text(
                "import unittest\n"
                "class WebCollaborationContinuationRegressionTests(unittest.TestCase):\n"
                "    def test_regression_parent_already_yielded_then_writer_completed_wakes_same_controller_with_next_runnable(self): self.assertTrue(True)\n"
                "    def test_completed_reviewer_uses_same_terminal_continuation_path(self): self.assertTrue(True)\n"
                "    def test_stale_child_is_second_observed_by_existing_audit_and_wakes_same_controller(self): self.assertTrue(True)\n"
                "    def test_duplicate_terminal_observation_after_confirmed_continuation_does_not_wake_twice(self): self.assertTrue(True)\n",
                encoding="utf-8",
            )
            (tests_dir / "test_web_agent_health_supervisor.py").write_text(
                "import unittest\n"
                "class WebAgentHealthSupervisorTests(unittest.TestCase):\n"
                "    def test_global_health_cycle_refreshes_and_schedules_immediate_rule_update_for_registered_controller(self): self.assertTrue(True)\n"
                "    def test_global_health_cycle_does_not_schedule_rule_update_without_explicit_current_target(self): self.assertTrue(True)\n"
                "    def test_health_tick_with_runnable_and_no_child_event_arms_same_controller_without_user_message(self): self.assertTrue(True)\n"
                "    def test_rule_wake_defers_host_runtime_resolution_until_target_is_known(self): self.assertTrue(True)\n"
                "    def test_global_health_cycle_isolates_one_repo_git_failure(self): self.assertTrue(True)\n"
                "    def test_canonical_runnable_reopens_continuation_without_user_message(self): self.assertTrue(True)\n"
                "    def test_no_canonical_work_does_not_reopen_after_observation_only_turn(self): self.assertTrue(True)\n"
                "    def test_health_tick_reopens_persisted_non_user_next_action_without_stop_callback(self): self.assertTrue(True)\n",
                encoding="utf-8",
            )
            (tests_dir / "test_terminal_continuation.py").write_text(
                "import unittest\n"
                "class TerminalContinuationTests(unittest.TestCase):\n"
                "    def test_terminal_receipt_persists_before_desktop_runtime_is_needed(self): self.assertTrue(True)\n"
                "class PendingTerminalReconcileTests(unittest.TestCase):\n"
                "    def test_reconcile_pending_discovers_canonical_receipts_without_receipt_cli_argument(self): self.assertTrue(True)\n"
                "    def test_reconcile_pending_is_idempotent_and_does_not_mutate_lifecycle_or_dispatch_wake(self): self.assertTrue(True)\n"
                "    def test_reconcile_pending_fails_closed_when_canonical_ownership_is_missing_or_mismatched(self): self.assertTrue(True)\n"
                "    def test_reconcile_pending_cli_has_no_receipt_argument_and_never_self_spawns(self): self.assertTrue(True)\n"
                "    def test_reconcile_pending_classifies_legacy_assignment_without_weakening_current_lease_checks(self): self.assertTrue(True)\n"
                "    def test_reconcile_pending_rejects_lifecycle_change_before_publish(self): self.assertTrue(True)\n"
                "    def test_reconcile_pending_accepts_independent_desktop_target_and_ownership_generations(self): self.assertTrue(True)\n"
                "    def test_reconcile_pending_hashes_same_bytes_it_parses(self): self.assertTrue(True)\n"
                "    def test_reconcile_pending_holds_lifecycle_and_registry_fences_through_audit(self): self.assertTrue(True)\n"
                "    def test_reconcile_pending_holds_runtime_assignment_fence_through_audit(self): self.assertTrue(True)\n"
                "    def test_reconcile_pending_fingerprint_is_order_independent(self): self.assertTrue(True)\n"
                "    def test_atomic_audit_writer_handles_concurrent_publication(self): self.assertTrue(True)\n"
                "class ManualControlCycleReconcileTests(unittest.TestCase):\n"
                "    def test_manual_fenced_control_cycle_reconcile_rejects_untrusted_ai_bridge_receipt(self): self.assertTrue(True)\n"
                "    def test_manual_fenced_control_cycle_reconcile_requires_unexpired_matching_manual_lease(self): self.assertTrue(True)\n"
                "    def test_immutable_cycle_evidence_rejects_forged_snapshot_hash(self): self.assertTrue(True)\n"
                "    def test_immutable_cycle_evidence_requires_terminal_debt_event_type(self): self.assertTrue(True)\n"
                "    def test_manual_reconcile_requires_target_lineage_membership(self): self.assertTrue(True)\n"
                "    def test_manual_reconcile_rejects_untrusted_ai_bridge_even_with_later_receipt(self): self.assertTrue(True)\n"
                "    def test_reconcile_control_cycle_cli_accepts_no_receipt_or_web_session_identity_argument(self): self.assertTrue(True)\n"
                "    def test_manual_reconcile_rejects_untrusted_ai_bridge_before_cycle_evidence(self): self.assertTrue(True)\n"
                "    def test_manual_reconcile_rejects_receipt_from_before_current_target_rotation(self): self.assertTrue(True)\n"
                "    def test_manual_reconcile_never_establishes_idempotence_from_untrusted_ai_bridge(self): self.assertTrue(True)\n"
                "    def test_manual_reconcile_rejects_untrusted_ai_bridge_before_event_type(self): self.assertTrue(True)\n"
                "    def test_manual_reconcile_closes_only_terminal_debt_and_preserves_current_nonterminal_triggers(self): self.assertTrue(True)\n",
                encoding="utf-8",
            )
            (tests_dir / "test_project_context_guard.py").write_text(
                "import unittest\n"
                "class ProjectContextGuardTests(unittest.TestCase):\n"
                "    def test_new_session_project_governance_question_requires_initialized_current_rules(self): self.assertTrue(True)\n"
                "    def test_known_projectless_session_allows_project_fact_prompt_with_unknown_context(self): self.assertTrue(True)\n"
                "    def test_existing_scoring_model_request_must_resolve_real_current_definition(self): self.assertTrue(True)\n"
                "    def test_source_change_before_stop_fails_closed_and_refreshes_for_same_turn_correction(self): self.assertTrue(True)\n"
                "    def test_not_found_unknown_token_does_not_authorize_fabricated_definitive_mechanism(self): self.assertTrue(True)\n"
                "    def test_runtime_state_creation_after_prompt_invalidates_fact_receipt_before_stop(self): self.assertTrue(True)\n"
                "    def test_nested_correction_refresh_preserves_full_applicable_agents_scope_chain(self): self.assertTrue(True)\n"
                "    def test_missing_required_identity_capability_reports_contract_drift_without_revoking_controller(self): self.assertTrue(True)\n"
                "    def test_contract_drift_does_not_upgrade_foreign_unverified_session_to_degraded(self): self.assertTrue(True)\n"
                "    def test_project_context_separates_unique_controller_from_unverified_web_session(self): self.assertTrue(True)\n"
                "    def test_project_context_reports_verified_bound_web_session_without_changing_ownership(self): self.assertTrue(True)\n"
                "class ControllerActionSourcePromptTests(unittest.TestCase):\n"
                "    def test_rule_ack_prompt_carries_logical_controller_and_actual_execution_source(self): self.assertTrue(True)\n"
                "    def test_live_e2e_accept_prompt_carries_actual_execution_source(self): self.assertTrue(True)\n"
                "    def test_web_bridge_event_uses_actual_web_conversation_as_controller_action_source(self): self.assertTrue(True)\n",
                encoding="utf-8",
            )
            (tests_dir / "test_rule_handshake.py").write_text(
                "import unittest\n"
                "class RuleHandshakeTests(unittest.TestCase):\n"
                "    def test_critical_live_runtime_update_requires_real_e2e_after_ack_and_ledger_sync(self): self.assertTrue(True)\n"
                "    def test_forged_live_e2e_acceptance_without_machine_evidence_stays_blocking(self): self.assertTrue(True)\n"
                "    def test_real_confirmed_wake_followed_by_closed_cycle_can_finalize_live_e2e(self): self.assertTrue(True)\n"
                "    def test_failed_live_e2e_does_not_freeze_invalid_wake_snapshot(self): self.assertTrue(True)\n"
                "    def test_live_e2e_debt_survives_later_nonimpacting_install_until_accepted(self): self.assertTrue(True)\n"
                "    def test_live_e2e_rejects_confirmed_wake_that_predates_rule_ack(self): self.assertTrue(True)\n"
                "    def test_live_e2e_rejects_stale_ownership_generation_before_acceptance(self): self.assertTrue(True)\n"
                "    def test_fake_project_chat_cannot_ack_by_claiming_logical_controller_id(self): self.assertTrue(True)\n"
                "    def test_current_web_target_ack_records_exact_source_and_generations(self): self.assertTrue(True)\n"
                "    def test_fake_project_chat_cannot_accept_or_defer_live_e2e(self): self.assertTrue(True)\n"
                "    def test_ack_revalidates_source_fence_immediately_before_persist(self): self.assertTrue(True)\n"
                "    def test_defer_revalidates_source_fence_immediately_before_persist(self): self.assertTrue(True)\n"
                "    def test_accept_revalidates_source_fence_before_freezing_evidence(self): self.assertTrue(True)\n",
                encoding="utf-8",
            )
            (tests_dir / "test_controller_target_guard.py").write_text(
                "import unittest\n"
                "class ControllerTargetGuardTests(unittest.TestCase):\n"
                "    def test_identity_projection_marks_unique_controller_with_missing_host_session_as_degraded(self): self.assertTrue(True)\n"
                "    def test_identity_capability_contract_exposes_canonical_projection_and_cli(self): self.assertTrue(True)\n"
                "    def test_identity_projection_keeps_unique_project_controller_when_session_id_unavailable(self): self.assertTrue(True)\n"
                "    def test_identity_projection_verifies_current_desktop_target_without_changing_controller_id(self): self.assertTrue(True)\n"
                "    def test_identity_projection_marks_old_target_stale_but_keeps_project_ownership(self): self.assertTrue(True)\n"
                "    def test_identity_projection_reports_project_controller_conflict_without_silent_selection(self): self.assertTrue(True)\n"
                "    def test_claim_controller_host_desktop_after_web_increments_one_cross_host_generation(self): self.assertTrue(True)\n"
                "    def test_verified_logical_agent_target_projects_controller_and_defers_other_agent_ownership(self): self.assertTrue(True)\n"
                "    def test_host_tool_preparation_persists_full_tuple_and_redacts_receipt_material(self): self.assertTrue(True)\n"
                "    def test_host_tool_preparation_rejects_nonce_receipt_and_execution_replays_after_reload(self): self.assertTrue(True)\n"
                "    def test_host_tool_terminal_retry_is_exact_and_direct_close_is_disabled(self): self.assertTrue(True)\n",
                encoding="utf-8",
            )
            (tests_dir / "test_agent_target_resolution.py").write_text(
                "import unittest\n"
                "class LogicalAgentTargetResolutionTests(unittest.TestCase):\n"
                "    def test_identity_contract_directly_supports_controller_agent_reviewer_and_runtime_repair_agent(self): self.assertTrue(True)\n"
                "    def test_verified_execution_target_is_generic_and_carries_double_generation_fence(self): self.assertTrue(True)\n"
                "    def test_verified_execution_target_rejects_wrong_logical_agent_and_stale_fences(self): self.assertTrue(True)\n"
                "    def test_resolution_status_model_is_generic_and_requires_verified_target_only_for_verified_state(self): self.assertTrue(True)\n"
                "class LogicalAgentExecutionTurnTests(unittest.TestCase):\n"
                "    def test_verified_execution_turn_is_generic_and_stable_across_target_generation_rotation(self): self.assertTrue(True)\n"
                "    def test_verified_execution_turn_rejects_wrong_agent_target_or_invocation(self): self.assertTrue(True)\n",
                encoding="utf-8",
            )
            (tests_dir / "test_runtime_host_tool_hook.py").write_text(
                "import unittest\n"
                "class RuntimeHostToolHookTests(unittest.TestCase):\n"
                "    def test_verified_pre_is_prepared_and_dispatches_same_execution_id(self): self.assertTrue(True)\n"
                "    def test_pre_requires_verifier_v2_tool_capability(self): self.assertTrue(True)\n"
                "    def test_registered_v2_verifier_cli_is_used_across_the_real_process_boundary(self): self.assertTrue(True)\n"
                "    def test_terminal_requires_structured_success_and_closes_after_lifecycle_commit(self): self.assertTrue(True)\n"
                "    def test_terminal_without_pre_and_generation_rotation_fail_closed(self): self.assertTrue(True)\n"
                "    def test_real_unix_server_correlates_request_and_owns_socket_mode(self): self.assertTrue(True)\n"
                "    def test_unix_server_rejects_unsafe_paths_and_bad_frames(self): self.assertTrue(True)\n",
                encoding="utf-8",
            )
            (tests_dir / "test_web_lifecycle_bridge.py").write_text(
                "import unittest\n"
                "class WebLifecycleComputerLeaseTests(unittest.TestCase):\n"
                "    def test_audit_once_never_uses_manual_resume_lease_as_caller_identity(self): self.assertTrue(True)\n"
                "class ControllerWakeSupervisorTests(unittest.TestCase):\n"
                "    def test_audit_wake_retry_rejects_same_id_receipt_shape_replacement(self): self.assertTrue(True)\n"
                "class WebLifecycleAuditTests(unittest.TestCase):\n"
                "    def test_rule_wake_target_resolution_fails_closed_instead_of_falling_back_to_logical_controller(self): self.assertTrue(True)\n"
                "    def test_rule_wake_rejects_explicit_target_without_canonical_execution_ownership(self): self.assertTrue(True)\n"
                "    def test_audit_once_rule_update_uses_guarded_scheduler_and_never_direct_scheduler(self): self.assertTrue(True)\n"
                "    def test_rule_wake_rejects_legacy_recovery_target_without_trusted_host_origin_proof(self): self.assertTrue(True)\n"
                "    def test_auto_native_stop_confirms_host_observed_canonical_target_already_foreground(self): self.assertTrue(True)\n"
                "    def test_mocked_active_writer_without_host_observation_still_rearms(self): self.assertTrue(True)\n"
                "    def test_serialized_active_writer_claim_cannot_confirm_already_foreground(self): self.assertTrue(True)\n"
                "    def test_execute_native_resume_marks_host_observed_active_writer_process_locally(self): self.assertTrue(True)\n"
                "    def test_auto_native_stop_yields_external_wait_when_desktop_host_reload_is_required(self): self.assertTrue(True)\n"
                "    def test_desktop_host_reload_gate_requires_exact_armed_zero_sequence_canary(self): self.assertTrue(True)\n"
                "    def test_desktop_codex_resolution_prefers_the_app_bundled_runtime(self): self.assertTrue(True)\n"
                "    def test_desktop_codex_resolution_rejects_an_invalid_explicit_override(self): self.assertTrue(True)\n"
                "    def test_rule_wake_uses_desktop_host_adapter_without_resolving_cli(self): self.assertTrue(True)\n"
                "    def test_codex_app_server_turn_uses_official_protocol_and_waits_for_completion(self): self.assertTrue(True)\n"
                "    def test_codex_app_server_active_writer_fails_before_turn_submit(self): self.assertTrue(True)\n"
                "    def test_codex_app_server_turn_start_response_timeout_is_result_unknown(self): self.assertTrue(True)\n"
                "    def test_codex_app_server_eof_after_turn_start_confirmation_is_result_unknown(self): self.assertTrue(True)\n"
                "    def test_desktop_host_resume_uses_app_server_under_target_and_ownership_fence(self): self.assertTrue(True)\n"
                "    def test_desktop_host_resume_missing_app_server_fails_closed_without_cli_fallback(self): self.assertTrue(True)\n"
                "    def test_desktop_host_resume_web_ownership_never_starts_app_server(self): self.assertTrue(True)\n"
                "    def test_execute_native_resume_without_explicit_cli_uses_desktop_host_adapter(self): self.assertTrue(True)\n"
                "    def test_rule_wake_does_not_require_desktop_runtime_for_web_target(self): self.assertTrue(True)\n"
                "class WebCurrentEntryDiscoveryTests(unittest.TestCase):\n"
                "    def test_generic_current_entry_discovery_accepts_runtime_repair_agent_verified_target(self): self.assertTrue(True)\n"
                "    def test_session_start_auto_discovers_machine_current_entry_and_allows_controller_actions(self): self.assertTrue(True)\n"
                "    def test_session_start_auto_discovery_preserves_signed_current_entry_envelope_for_verifier(self): self.assertTrue(True)\n"
                "    def test_session_start_uses_supplied_host_current_entry_without_rediscovery(self): self.assertTrue(True)\n"
                "    def test_session_start_preserves_signed_current_entry_envelope_for_verifier(self): self.assertTrue(True)\n"
                "    def test_session_start_without_host_current_entry_fails_closed(self): self.assertTrue(True)\n"
                "    def test_session_start_caller_claim_of_real_canonical_conversation_is_not_current_entry_proof(self): self.assertTrue(True)\n"
                "    def test_session_start_same_conversation_different_browser_target_fails_closed(self): self.assertTrue(True)\n"
                "    def test_session_start_caller_claim_cannot_override_different_machine_current_entry(self): self.assertTrue(True)\n"
                "    def test_session_start_historical_alias_discovered_by_host_is_not_restored_as_current(self): self.assertTrue(True)\n"
                "    def test_session_start_active_tab_drift_cannot_change_discovered_invocation_identity(self): self.assertTrue(True)\n"
                "    def test_session_start_stale_current_entry_ownership_generation_fails_closed(self): self.assertTrue(True)\n"
                "    def test_session_start_stale_current_entry_generation_fails_closed(self): self.assertTrue(True)\n"
                "    def test_session_start_machine_current_successor_rotates_same_controller_only(self): self.assertTrue(True)\n"
                "class WebMachineInvocationTurnBridgeTests(unittest.TestCase):\n"
                "    def test_current_entry_machine_invocation_builds_generic_verified_execution_turn(self): self.assertTrue(True)\n"
                "    def test_same_host_invocation_has_stable_turn_id_and_next_invocation_changes_it(self): self.assertTrue(True)\n"
                "    def test_session_start_same_machine_invocation_preserves_trace_and_next_invocation_resets(self): self.assertTrue(True)\n"
                "    def test_session_start_without_host_invocation_id_recovers_legacy_overflow_with_runtime_lease(self): self.assertTrue(True)\n"
                "    def test_caller_turn_id_cannot_override_host_machine_turn(self): self.assertTrue(True)\n"
                "    def test_current_entry_rejects_oversized_runtime_invocation_id(self): self.assertTrue(True)\n"
                "class RuntimeWebTurnEdgeWatcherTests(unittest.TestCase):\n"
                "    def test_host_unavailable_marks_lease_ended_but_preserves_overflow_until_next_session_start(self): self.assertTrue(True)\n"
                "    def test_active_host_probe_does_not_end_current_runtime_turn(self): self.assertTrue(True)\n"
                "    def test_post_shell_without_host_turn_token_reuses_active_runtime_lease(self): self.assertTrue(True)\n"
                "    def test_host_invocation_token_does_not_orphan_active_fallback_lease(self): self.assertTrue(True)\n"
                "    def test_direct_host_turn_after_ended_fallback_never_leaves_stale_lease(self): self.assertTrue(True)\n"
                "    def test_verified_same_controller_successor_rotates_active_fallback_lease_without_watcher_end(self): self.assertTrue(True)\n"
                "    def test_verified_same_controller_successor_with_inflight_fallback_tool_fails_closed(self): self.assertTrue(True)\n"
                "    def test_successor_rechecks_inflight_under_registry_fence_before_target_rotation(self): self.assertTrue(True)\n"
                "class RuntimeWebTurnEndClassificationTests(unittest.TestCase):\n"
                "    def test_only_explicit_generation_end_errors_count_as_turn_end(self): self.assertTrue(True)\n"
                "    def test_generic_web_host_generation_end_markers_are_explicit_terminal_edges(self): self.assertTrue(True)\n"
                "    def test_missed_generation_end_edge_never_false_resets_next_active_generation(self): self.assertTrue(True)\n"
                "class RuntimeWebTurnStaleFenceWatcherTests(unittest.TestCase):\n"
                "    def test_foreign_current_entry_does_not_end_or_clear_current_lease(self): self.assertTrue(True)\n"
                "    def test_target_generation_change_does_not_end_current_lease(self): self.assertTrue(True)\n"
                "class RuntimeWebTurnWatcherSpawnTests(unittest.TestCase):\n"
                "    def test_detached_web_turn_watcher_does_not_use_unreaped_popen(self): self.assertTrue(True)\n"
                "class WebAutoStopSupervisorCoalescingTests(unittest.TestCase):\n"
                "    def test_host_neutral_supervisor_omits_missing_desktop_codex_argument(self): self.assertTrue(True)\n"
                "class WebLifecycleBridgeTests(unittest.TestCase):\n"
                "    def test_registered_web_verifier_submit_adapter_ignores_runtime_local_path_kwargs(self): self.assertTrue(True)\n"
                "    def test_registered_web_verifier_submit_adapter_maps_controller_active_to_deferred_active(self): self.assertTrue(True)\n"
                "    def test_registered_web_verifier_submit_adapter_rejects_controller_active_after_dispatch(self): self.assertTrue(True)\n"
                "    def test_registered_web_verifier_submit_adapter_rejects_controller_active_generation_mismatch(self): self.assertTrue(True)\n"
                "    def test_registered_web_verifier_submit_adapter_keeps_other_retryable_failures_bounded(self): self.assertTrue(True)\n"
                "    def test_session_start_without_host_session_id_reports_existing_controller_not_new_controller(self): self.assertTrue(True)\n"
                "    def test_session_start_host_attested_recovery_restores_pending_control_loop_same_controller(self): self.assertTrue(True)\n"
                "    def test_same_controller_web_recovery_verifier_exception_degrades_without_revoking_controller(self): self.assertTrue(True)\n"
                "    def test_same_controller_web_recovery_rejects_attestation_if_target_generation_changes_before_lock(self): self.assertTrue(True)\n"
                "    def test_same_controller_web_recovery_is_idempotent_after_user_reconfirms_ownership(self): self.assertTrue(True)\n"
                "    def test_web_recovery_preserves_desktop_target_and_only_advances_web_generation(self): self.assertTrue(True)\n"
                "    def test_dispatch_event_result_treats_decision_block_as_logical_yield_rejection(self): self.assertTrue(True)\n"
                "    def test_production_bridge_has_no_trusted_web_attestation_verifier(self): self.assertTrue(True)\n"
                "    def test_registered_web_verifier_timeout_covers_product_host_request_budget(self): self.assertTrue(True)\n"
                "    def test_registered_web_verifier_classifies_frame_tree_timeout_as_transient(self): self.assertTrue(True)\n"
                "    def test_registered_web_verifier_classifies_exact_target_ambiguous_as_transient(self): self.assertTrue(True)\n"
                "    def test_registered_web_verifier_exposes_pinned_current_entry_discovery(self): self.assertTrue(True)\n"
                "    def test_registered_web_verifier_loads_pinned_external_runtime_host_cli(self): self.assertTrue(True)\n"
                "    def test_registered_web_verifier_exposes_pinned_host_submit_adapter(self): self.assertTrue(True)\n"
                "    def test_registered_web_verifier_rechecks_bundle_before_each_execution(self): self.assertTrue(True)\n"
                "    def test_registered_v2_identity_evidence_consumes_signed_current_entry_without_reattest(self): self.assertTrue(True)\n"
                "    def test_loaded_verifier_rejects_writable_members_parents_and_replaced_path(self): self.assertTrue(True)\n"
                "    def test_malformed_registered_web_verifier_config_fails_closed_without_manual_fallback(self): self.assertTrue(True)\n"
                "    def test_browser_tab_receipt_cannot_recover_an_unverified_web_session(self): self.assertTrue(True)\n"
                "    def test_replace_web_session_bootstrap_rotates_target_and_manual_lease_without_host_attestation(self): self.assertTrue(True)\n"
                "    def test_manual_web_mutations_cannot_downgrade_host_attested_current_target(self): self.assertTrue(True)\n"
                "    def test_same_controller_web_recovery_cannot_replace_different_host_attested_current_target(self): self.assertTrue(True)\n"
                "    def test_legacy_quarantined_target_keeps_manual_replacement_exit(self): self.assertTrue(True)\n"
                "    def test_replace_web_session_rejects_unapproved_session_and_stale_generation(self): self.assertTrue(True)\n"
                "    def test_replace_same_web_target_is_idempotent_and_unbind_tombstones_without_losing_alias_history(self): self.assertTrue(True)\n"
                "    def test_same_controller_web_recovery_does_not_rotate_manual_resume_lease(self): self.assertTrue(True)\n"
                "    def test_session_start_verified_target_does_not_rotate_manual_resume_lease(self): self.assertTrue(True)\n"
                "    def test_historical_alias_cannot_recover_even_with_trusted_verifier(self): self.assertTrue(True)\n"
                "    def test_unbound_chat_cannot_recover_even_with_trusted_verifier(self): self.assertTrue(True)\n"
                "    def test_zshenv_exit_bridge_executes_and_preserves_exit_precedence(self): self.assertTrue(True)\n"
                "class WebAutoStopSupervisorCoalescingTests(unittest.TestCase):\n"
                "    def test_same_terminal_receipt_cannot_be_rescheduled(self): self.assertTrue(True)\n"
                "    def test_host_neutral_supervisor_omits_missing_desktop_codex_argument(self): self.assertTrue(True)\n"
                "    def test_replacement_can_supersede_while_old_supervisor_waits_in_web_reentry(self): self.assertTrue(True)\n"
                "    def test_replacement_can_supersede_while_old_supervisor_waits_in_native_resume(self): self.assertTrue(True)\n"
                "    def test_superseded_supervisor_cannot_start_native_recovery_bootstrap_after_resume(self): self.assertTrue(True)\n"
                "    def test_superseded_supervisor_cannot_start_recovery_bootstrap_between_ownership_check_and_launch(self): self.assertTrue(True)\n"
                "    def test_superseded_supervisor_cannot_replace_native_target_after_recovery_bootstrap(self): self.assertTrue(True)\n"
                "    def test_superseded_supervisor_cannot_launch_recovery_resume_after_target_replacement(self): self.assertTrue(True)\n"
                "    def test_same_receipt_live_supervisor_is_coalesced(self): self.assertTrue(True)\n"
                "    def test_current_token_web_rearm_hands_off_with_force_rearm_proof(self): self.assertTrue(True)\n"
                "    def test_stale_supervisor_token_exits_without_running_impl(self): self.assertTrue(True)\n"
                "    def test_stale_supervisor_cannot_native_wake_after_supersession(self): self.assertTrue(True)\n"
                "    def test_execute_native_resume_stale_supervisor_token_blocks_process_launch(self): self.assertTrue(True)\n"
                "class WebHostNativeWakeIsolationTests(unittest.TestCase):\n"
                "    def test_stale_web_host_with_only_desktop_current_target_resumes_same_controller_desktop(self): self.assertTrue(True)\n"
                "    def test_registered_current_web_adapter_is_fenced_and_host_attested(self): self.assertTrue(True)\n"
                "    def test_registered_external_web_host_submit_adapter_is_used_without_caller_injection(self): self.assertTrue(True)\n"
                "    def test_current_web_adapter_is_not_called_when_pre_delivery_attestation_rejects(self): self.assertTrue(True)\n"
                "    def test_current_web_adapter_receipt_must_correlate_origin_call_receipt(self): self.assertTrue(True)\n"
                "    def test_current_web_adapter_is_not_called_for_malformed_origin_attestation(self): self.assertTrue(True)\n"
                "    def test_registered_current_web_adapter_without_ownership_is_never_called(self): self.assertTrue(True)\n"
                "class StrongWebSuccessorHandoffTests(unittest.TestCase):\n"
                "    def test_authorize_web_successor_records_only_fenced_fresh_session(self): self.assertTrue(True)\n"
                "    def test_authorize_web_successor_cli_does_not_rotate_target(self): self.assertTrue(True)\n"
                "    def test_authorized_strong_web_successor_rotates_target_and_ownership_once(self): self.assertTrue(True)\n"
                "    def test_strong_web_successor_expired_authorization_does_not_call_verifier(self): self.assertTrue(True)\n"
                "    def test_strong_web_successor_rechecks_target_generation_after_attestation(self): self.assertTrue(True)\n"
                "class WebContinuationSupervisorBootstrapTests(unittest.TestCase):\n"
                "    def test_dead_or_untracked_active_supervisor_requires_bootstrap(self): self.assertTrue(True)\n"
                "    def test_live_active_supervisor_does_not_need_duplicate_bootstrap(self): self.assertTrue(True)\n"
                "    def test_identity_blocked_same_event_and_registry_are_not_bootstrapped_again(self): self.assertTrue(True)\n"
                "    def test_identity_blocked_event_retries_after_registry_changes(self): self.assertTrue(True)\n"
                "    def test_nonretryable_web_failure_same_event_and_fence_stays_quiet(self): self.assertTrue(True)\n"
                "    def test_retry_exhausted_rearms_after_host_delivery_fingerprint_change(self): self.assertTrue(True)\n"
                "    def test_retry_exhausted_rearms_after_controller_fence_change_same_host_fingerprint(self): self.assertTrue(True)\n"
                "    def test_ensure_supervisor_uses_new_receipt_after_controller_fence_change(self): self.assertTrue(True)\n"
                "    def test_ensure_supervisor_uses_new_receipt_for_new_rule_live_e2e_after_old_result_unknown(self): self.assertTrue(True)\n"
                "    def test_confirmed_or_result_unknown_never_rearm_for_host_fingerprint_change(self): self.assertTrue(True)\n"
                "    def test_terminal_rule_delivery_blocks_bootstrap_across_fingerprint_changes(self): self.assertTrue(True)\n"
                "    def test_non_rule_delivery_key_uses_wake_generation_with_current_rule_snapshot(self): self.assertTrue(True)\n"
                "class WebLocalReentryIntegrationTests(unittest.TestCase):\n"
                "    def test_detached_supervisor_defers_while_web_response_is_active_and_retries_without_counting_progress(self): self.assertTrue(True)\n"
                "    def test_detached_supervisor_uses_registered_host_submit_adapter_for_strong_web_target(self): self.assertTrue(True)\n"
                "    def test_retry_exhausted_persists_host_delivery_fingerprint(self): self.assertTrue(True)\n"
                "    def test_strong_host_confirmed_submit_waits_without_rearm(self): self.assertTrue(True)\n"
                "    def test_transient_web_reentry_retry_budget_exhausts_without_rearm(self): self.assertTrue(True)\n"
                "    def test_detached_supervisor_retries_transient_registered_host_attestation_failure(self): self.assertTrue(True)\n"
                "    def test_direct_wake_rejects_confirmed_web_result_after_desktop_handoff(self): self.assertTrue(True)\n"
                "    def test_desktop_result_cannot_persist_or_rearm_after_web_handoff(self): self.assertTrue(True)\n"
                "    def test_web_supervisor_rejects_confirmed_receipt_for_noncanonical_target(self): self.assertTrue(True)\n"
                "    def test_builtin_web_reentry_without_registered_origin_verifier_never_calls_browser_adapter(self): self.assertTrue(True)\n"
                "    def test_manual_fenced_direct_wake_uses_builtin_adapter_without_peer_verifier(self): self.assertTrue(True)\n"
                "    def test_manual_fenced_supervisor_persists_unverified_delivery_evidence(self): self.assertTrue(True)\n"
                "    def test_manual_fenced_direct_wake_passes_bridge_verifier_into_builtin_adapter(self): self.assertTrue(True)\n"
                "    def test_manual_fenced_supervisor_passes_bridge_verifier_into_builtin_adapter(self): self.assertTrue(True)\n"
                "    def test_manual_fenced_confirmed_waits_for_progress_without_resubmit(self): self.assertTrue(True)\n"
                "    def test_waiting_for_controller_progress_does_not_bootstrap_until_machine_facts_change(self): self.assertTrue(True)\n"
                "    def test_registered_host_nonretryable_failure_persists_quiet_fence(self): self.assertTrue(True)\n"
                "    def test_registered_host_result_unknown_persists_quiet_fence(self): self.assertTrue(True)\n"
                "    def test_confirmed_web_reentry_clears_stale_nonretryable_block_evidence(self): self.assertTrue(True)\n"
                "class WebReentryDebounceTests(unittest.TestCase):\n"
                "    def test_web_confirmed_wake_is_not_debounced_after_same_target_ownership_reclaim(self): self.assertTrue(True)\n",
                encoding="utf-8",
            )
            (tests_dir / "test_evaluation_transaction.py").write_text(
                "import unittest\n"
                "class EvaluationTransactionTests(unittest.TestCase):\n"
                "    def test_historical_72_8_cannot_satisfy_re_evaluate_current_capability(self): self.assertTrue(True)\n"
                "    def test_read_as_computed_violation_starts_same_flow_correction_with_new_evidence(self): self.assertTrue(True)\n",
                encoding="utf-8",
            )
            (tests_dir / "test_controller_scoring_hook.py").write_text(
                "import unittest\n"
                "class ControllerScoringEvaluationTransactionTests(unittest.TestCase):\n"
                "    def test_current_re_evaluation_does_not_inject_or_accept_historical_score_as_new_result(self): self.assertTrue(True)\n"
                "    def test_computed_current_score_requires_exact_transaction_metadata_and_persists_it(self): self.assertTrue(True)\n"
                "    def test_historical_total_relabelled_computed_without_fresh_dimension_vector_is_blocked(self): self.assertTrue(True)\n"
                "    def test_runtime_rejects_performance_total_that_does_not_match_dimension_vector(self): self.assertTrue(True)\n"
                "    def test_fact_change_before_stop_forces_same_flow_re_evaluation_refresh(self): self.assertTrue(True)\n",
                encoding="utf-8",
            )
            (tests_dir / "test_web_agent_events.py").write_text(
                "import unittest\n"
                "class WebAgentMachineEventSourceTests(unittest.TestCase):\n"
                "    def test_caller_created_file_inside_codex_session_root_cannot_self_attest(self): self.assertTrue(True)\n"
                "    def test_health_supervisor_publishes_diagnostic_source_without_authorizing_it(self): self.assertTrue(True)\n",
                encoding="utf-8",
            )
            (tests_dir / "test_governance.py").write_text(
                "import unittest\n"
                "class GovernanceTests(unittest.TestCase):\n"
                "    def test_web_stdout_marker_never_closes_without_private_terminal_commit(self): self.assertTrue(True)\n"
                "    def test_runtime_terminal_active_row_does_not_consume_dispatch_capacity(self): self.assertTrue(True)\n"
                "    def test_runnable_hard_defer_requires_machine_evidence_and_checkpoint(self): self.assertTrue(True)\n"
                "    def test_local_hard_defer_still_fills_other_nonconflicting_capacity(self): self.assertTrue(True)\n"
                "    def test_identity_degraded_cannot_authorize_stop_while_project_runnable_exists(self): self.assertTrue(True)\n"
                "    def test_pending_live_e2e_allows_safe_control_cycle_but_no_new_assignment(self): self.assertTrue(True)\n"
                "    def test_project_wide_projection_web_active_verify_do_not_starve_mini_runnables(self): self.assertTrue(True)\n"
                "    def test_project_wide_projection_mini_active_does_not_starve_server_or_web(self): self.assertTrue(True)\n"
                "    def test_project_wide_fairness_requires_parallel_dispatch_when_capacity_exists(self): self.assertTrue(True)\n"
                "    def test_pending_dependency_closure_dynamically_enters_project_wide_runnable_projection(self): self.assertTrue(True)\n"
                "    def test_project_wide_fairness_rejects_more_active_dispatches_than_capacity(self): self.assertTrue(True)\n"
                "    def test_pending_parent_partial_dependency_creates_dynamic_runnable_slice(self): self.assertTrue(True)\n"
                "    def test_reviewer_terminal_recomputes_unrelated_project_runnable(self): self.assertTrue(True)\n"
                "    def test_stop_without_current_turn_control_loop_receipt_fails_closed_even_when_idle(self): self.assertTrue(True)\n"
                "    def test_control_loop_stop_rejection_reopens_pending_event_even_if_prior_state_was_closed(self): self.assertTrue(True)\n"
                "    def test_active_writer_does_not_hide_immediate_controller_actions(self): self.assertTrue(True)\n"
                "    def test_control_loop_receipt_rejects_missing_or_reordered_control_steps(self): self.assertTrue(True)\n"
                "    def test_failed_control_cycle_generates_executable_controller_correction(self): self.assertTrue(True)\n"
                "    def test_unfinished_correction_prevents_control_cycle_closure(self): self.assertTrue(True)\n"
                "    def test_same_controller_deviation_fingerprint_escalates_on_recurrence(self): self.assertTrue(True)\n"
                "    def test_direct_cycle_persistence_cannot_fabricate_generic_correction_closure(self): self.assertTrue(True)\n"
                "    def test_reviewer_pass_integration_has_mandatory_verify_converge_recompute_successors(self): self.assertTrue(True)\n"
                "    def test_known_next_action_enters_canonical_controller_action_projection(self): self.assertTrue(True)\n"
                "    def test_continuation_debt_blocks_control_loop_receipt_until_every_action_resolved(self): self.assertTrue(True)\n"
                "    def test_durable_terminal_receipt_enters_debt_once_and_disappears_after_consumption(self): self.assertTrue(True)\n"
                "    def test_hard_blocked_or_deferred_actions_clear_continuation_debt_and_allow_yield(self): self.assertTrue(True)\n"
                "    def test_continuation_debt_fingerprint_escalates_through_existing_recurrence_rules(self): self.assertTrue(True)\n"
                "    def test_event_scope_guard_allows_project_wide_dispatch_across_business_lines(self): self.assertTrue(True)\n"
                "    def test_event_scope_guard_rejects_cross_task_work_without_project_wide_dispatch_proof(self): self.assertTrue(True)\n"
                "    def test_candidate_inventory_batches_ancestry_for_multiple_worktrees(self): self.assertTrue(True)\n"
                "class DurableHostToolReceiptPersistenceTests(unittest.TestCase):\n"
                "    def test_lifecycle_commit_fsync_failure_keeps_registry_pending_and_exact_retry_is_single_trace(self): self.assertTrue(True)\n"
                "    def test_verified_terminal_times_out_hung_snapshot_git_without_closed_or_trace(self): self.assertTrue(True)\n"
                "class RuntimeWebTurnLeaseTests(unittest.TestCase):\n"
                "    def test_legacy_overflow_migrates_once_only_without_inflight(self): self.assertTrue(True)\n"
                "    def test_legacy_overflow_with_inflight_cannot_migrate(self): self.assertTrue(True)\n"
                "    def test_active_lease_repeated_session_start_is_idempotent(self): self.assertTrue(True)\n"
                "    def test_active_lease_cannot_rotate_without_machine_end(self): self.assertTrue(True)\n"
                "    def test_ended_lease_allows_next_generation_and_clears_overflow(self): self.assertTrue(True)\n"
                "    def test_forged_ended_status_without_machine_end_evidence_cannot_rotate(self): self.assertTrue(True)\n"
                "    def test_stale_watcher_cannot_end_newer_lease(self): self.assertTrue(True)\n"
                "class RuntimeWebTurnMachineTraceAcceptanceTests(unittest.TestCase):\n"
                "    def test_recovered_web_turn_produces_machine_trace_and_clean_closed_cycle_evidence(self): self.assertTrue(True)\n"
                "class UnboundWebPostToolIsolationTests(unittest.TestCase):\n"
                "    def test_unverified_web_post_tool_without_turn_id_cannot_mutate_active_turn(self): self.assertTrue(True)\n"
                "class WebMachineTurnLifecycleTests(unittest.TestCase):\n"
                "    def test_new_machine_web_turn_resets_old_trace_overflow(self): self.assertTrue(True)\n"
                "    def test_same_machine_web_turn_does_not_reset_existing_trace_or_overflow(self): self.assertTrue(True)\n"
                "    def test_new_machine_web_turn_with_inflight_tool_fails_closed_without_hiding_old_trace(self): self.assertTrue(True)\n"
                "    def test_unverified_web_session_start_cannot_rotate_turn(self): self.assertTrue(True)\n"
                "    def test_web_post_tool_cannot_start_next_turn_without_session_boundary(self): self.assertTrue(True)\n"
                "    def test_direct_host_turn_cannot_abandon_active_runtime_fallback_lease(self): self.assertTrue(True)\n"
                "    def test_verified_web_event_rejects_stale_target_and_ownership_fences(self): self.assertTrue(True)\n"
                "    def test_verified_web_event_rejects_historical_web_target(self): self.assertTrue(True)\n"
                "    def test_multiple_web_turns_under_limit_do_not_accumulate_overflow_but_single_turn_still_does(self): self.assertTrue(True)\n"
                "class ControllerActionSourcePromptTests(unittest.TestCase):\n"
                "    def test_rule_ack_prompt_carries_logical_controller_and_actual_execution_source(self): self.assertTrue(True)\n"
                "    def test_live_e2e_accept_prompt_carries_actual_execution_source(self): self.assertTrue(True)\n"
                "    def test_web_bridge_event_uses_actual_web_conversation_as_controller_action_source(self): self.assertTrue(True)\n",
                encoding="utf-8",
            )
            (tests_dir / "test_desktop_lifecycle_adapter.py").write_text(
                "import unittest\n"
                "class DesktopLifecycleTurnGateTests(unittest.TestCase):\n"
                "    def test_successful_receipt_is_invalidated_when_same_turn_continuation_executes(self): self.assertTrue(True)\n"
                "    def test_status_query_does_not_clear_existing_controller_continuation(self): self.assertTrue(True)\n"
                "    def test_hard_yield_gate_rejects_declared_next_action_when_work_is_runnable(self): self.assertTrue(True)\n"
                "    def test_hard_yield_gate_does_not_invent_work_from_status_only_message(self): self.assertTrue(True)\n"
                "class DesktopOutboundLeaseHookTests(unittest.TestCase):\n"
                "    def test_managed_controller_rejects_unbounded_dev_commands_before_state_write(self): self.assertTrue(True)\n"
                "    def test_foreground_command_gate_allows_bounded_work_and_skips_unmanaged_sessions(self): self.assertTrue(True)\n"
                "    def test_controller_without_explicit_surface_uses_its_registered_canonical_repo(self): self.assertTrue(True)\n"
                "    def test_explicit_controller_surface_rejects_another_checkout(self): self.assertTrue(True)\n"
                "    def test_run_hook_reuses_one_project_snapshot_for_management_fence(self): self.assertTrue(True)\n"
                "    def test_current_desktop_user_prompt_persists_confirmed_native_wake(self): self.assertTrue(True)\n",
                encoding="utf-8",
            )
            (tests_dir / "test_goal_display_sync.py").write_text(
                "import unittest\n"
                "class GoalDisplaySyncTests(unittest.TestCase):\n"
                "    def test_rolled_happy_path_records_exact_host_sequence_and_binding(self): self.assertTrue(True)\n"
                "    def test_successful_rolled_control_receipt_activates_display_sync_debt(self): self.assertTrue(True)\n"
                "    def test_title_failure_recovers_without_recreating_goal(self): self.assertTrue(True)\n"
                "    def test_host_readback_mismatch_retries_only_the_failed_read(self): self.assertTrue(True)\n"
                "    def test_host_readback_rejects_unrelated_objective_and_split_thread_match(self): self.assertTrue(True)\n"
                "    def test_duplicate_rollover_reuses_completed_receipt(self): self.assertTrue(True)\n"
                "    def test_unavailable_host_tool_marks_receipt_degraded(self): self.assertTrue(True)\n"
                "    def test_missing_host_capability_is_degraded_and_exact_target_change_is_fenced(self): self.assertTrue(True)\n",
                encoding="utf-8",
            )
            (tests_dir / "test_web_agent_execution.py").write_text(
                "import unittest\n"
                "class WebAgentExecutionTests(unittest.TestCase):\n"
                "    def test_direct_start_web_assignment_is_rejected_even_with_forged_readiness_probe(self): self.assertTrue(True)\n"
                "    def test_public_web_adapter_rejects_self_asserted_strong_attestation(self): self.assertTrue(True)\n"
                "    def test_host_started_lease_uses_attested_machine_start_time_not_ingestion_time(self): self.assertTrue(True)\n"
                "    def test_host_started_rejects_unattested_or_mismatched_machine_start_time(self): self.assertTrue(True)\n"
                "class RuntimeOwnedWebRecoveryContractTests(unittest.TestCase):\n"
                "    def test_machine_event_source_public_status_cannot_accept_caller_verifier(self): self.assertTrue(True)\n"
                "    def test_forged_local_machine_event_receipt_cannot_enable_production_prepare(self): self.assertTrue(True)\n"
                "    def test_forged_safe_fallback_fields_without_canonical_prior_terminal_are_rejected(self): self.assertTrue(True)\n"
                "    def test_safe_fallback_policy_must_declare_selected_fallback_route_not_only_origin(self): self.assertTrue(True)\n"
                "    def test_canonical_kimi_terminal_with_declared_route_can_prepare_web_fallback(self): self.assertTrue(True)\n"
                "    def test_direct_internal_observation_chain_cannot_create_lease_without_attested_machine_event(self): self.assertTrue(True)\n"
                "    def test_route_policy_accepts_normal_whitespace_declaration(self): self.assertTrue(True)\n"
                "    def test_route_policy_rejects_prefixed_fields_comments_and_negative_examples(self): self.assertTrue(True)\n"
                "    def test_route_policy_accepts_active_rule_after_inactive_examples(self): self.assertTrue(True)\n"
                "    def test_route_policy_rejects_inline_comments_negation_hyphen_prefix_and_value_only_class_marker(self): self.assertTrue(True)\n"
                "    def test_route_policy_normalizes_unicode_dash_variants_before_authorization(self): self.assertTrue(True)\n"
                "    def test_route_policy_uses_positive_prefix_grammar_not_negative_phrase_allowlist(self): self.assertTrue(True)\n"
                "    def test_route_policy_full_line_grammar_rejects_intervening_and_trailing_semantics(self): self.assertTrue(True)\n"
                "    def test_route_policy_requires_canonical_class_then_single_directive_order(self): self.assertTrue(True)\n"
                "    def test_route_policy_requires_class_marker_as_first_nonspace_token(self): self.assertTrue(True)\n"
                "    def test_route_policy_accepts_explicit_web_class_marker_before_fields(self): self.assertTrue(True)\n"
                "    def test_route_policy_rejects_tilde_fenced_route_examples(self): self.assertTrue(True)\n"
                "class StructuredCollaborationTerminalTests(unittest.TestCase):\n"
                "    def test_public_structured_terminal_ingest_rejects_caller_supplied_observation(self): self.assertTrue(True)\n"
                "    def test_internal_terminal_helper_cannot_accept_fabricated_observation_without_attested_path(self): self.assertTrue(True)\n",
                encoding="utf-8",
            )
            (tests_dir / "test_install_skill.py").write_text(
                "import unittest\n"
                "class InstallCapabilityTests(unittest.TestCase):\n"
                "    def test_identity_capability_report_exposes_runtime_current_entry_host_contract(self): self.assertTrue(True)\n"
                "    def test_installer_web_bridge_preserves_shell_and_lifecycle_exit_precedence(self): self.assertTrue(True)\n"
                "class ProjectContextHookInstallationTests(unittest.TestCase):\n"
                "    def test_runtime_hooks_keep_trust_stable_legacy_indices(self): self.assertTrue(True)\n"
                "    def test_shifted_runtime_hook_groups_migrate_back_without_moving_user_groups(self): self.assertTrue(True)\n"
                "class HostAdapterInstallationTests(unittest.TestCase):\n"
                "    def test_configure_host_adapters_can_update_codex_hooks_without_touching_ai_bridge(self): self.assertTrue(True)\n"
                "    def test_install_cli_skip_ai_bridge_never_rolls_back_concurrent_zshenv_update(self): self.assertTrue(True)\n"
                "class WebAgentHealthServiceInstallationTests(unittest.TestCase):\n"
                "    def test_runtime_service_retires_legacy_per_controller_web_audit_after_new_service_load(self): self.assertTrue(True)\n"
                "    def test_runtime_service_load_failure_preserves_legacy_web_audit(self): self.assertTrue(True)\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "-C", str(source), "add", "."], check=True)
            subprocess.run(["git", "-C", str(source), "commit", "-m", "release regressions"], check=True, capture_output=True)
            revision = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()

            result = _verify_runtime_release_regressions(source, revision)
            self.assertEqual(result["status"], "passed")
            self.assertEqual(tuple(result["tests"]), RUNTIME_RELEASE_REGRESSION_TESTS)
            self.assertEqual(
                tuple(result["node_tests"]), RUNTIME_RELEASE_NODE_REGRESSION_TESTS
            )

    def test_runtime_release_regression_gate_blocks_failing_required_case(self):
        import subprocess
        from scripts.install_skill import _verify_runtime_release_regressions

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = self.make_source(root)
            for name in (
                "web_agent_execution.py", "web_reentry_adapter.py", "terminal_continuation.py",
                "goal_display_sync.py", "runtime_host_tool_hook.py", "controller_health.py",
                "controller_self_check.py", "ledger_consistency_guard.py", "lint_governance.py",
                "preblock_guard.py", "project_state.py",
            ):
                (source / "scripts" / name).write_text("# runtime\n", encoding="utf-8")
            (source / "scripts" / "run_external_agent.mjs").write_text(
                "export const marker = 'external-agent-routing';\n",
                encoding="utf-8",
            )
            tests_dir = source / "tests"
            tests_dir.mkdir()
            (tests_dir / "external-agent-routing.test.mjs").write_text(
                "import test from 'node:test';\n"
                "import assert from 'node:assert/strict';\n"
                "test('heterogeneous frontend and backend tasks stay on Kimi and Grok canonical executors', () => { assert.equal(1, 1); });\n"
                "test('assignment-bound execute rejects CLI route mismatch before provider spawn', () => { assert.equal(1, 1); });\n"
                "test('assignment-bound safe fallback requires canonical prior terminal before provider spawn', () => { assert.equal(1, 1); });\n"
                "test('assignment-bound external start persists exact canonical route contract', () => { assert.equal(1, 1); });\n"
                "test('short assignment-bound execution reconciles final Git progress before terminal', () => { assert.equal(1, 1); });\n"
                "test('fresh legacy v1 assignment ACK cannot launch external provider', () => { assert.equal(1, 1); });\n"
                "test('Grok execution transports prompts through a private prompt file and removes it', () => { assert.equal(1, 1); });\n"
                "test('oversized Grok reviewer prompt fails before provider spawn with sharding evidence', () => { assert.equal(1, 1); });\n"
                "test('Grok first-output timeout terminates a silent provider attempt', () => { assert.equal(1, 1); });\n"
                "test('Grok generation stall timeout terminates after structured output stops', () => { assert.equal(1, 1); });\n"
                "test('Grok absolute deadline kills the entire provider process group', () => { assert.equal(1, 1); });\n"
                "test('Grok stall timeout persists structured canonical terminal classification', () => { assert.equal(1, 1); });\n"
                "test('Grok failed attempt uses 0600 prompt file and removes it', () => { assert.equal(1, 1); });\n"
                "test('oversized non-reviewer Grok prompt fails before spawn without sharding', () => { assert.equal(1, 1); });\n"
                "test('Grok stderr and assignment heartbeat do not satisfy first stdout progress', () => { assert.equal(1, 1); });\n"
                "test('Grok unstructured stdout does not satisfy structured first-output progress', () => { assert.equal(1, 1); });\n"
                "test('Grok 1.0.13 streaming text output satisfies first-output progress then stalls', () => { assert.equal(1, 1); });\n"
                "test('Grok 1.0.13 streaming thought tool-call and tool-update events count as model progress', () => { assert.equal(1, 1); });\n"
                "test('Grok 1.0.13 bare type and metadata events cannot spoof model progress', () => { assert.equal(1, 1); });\n"
                "test('Grok malformed stdout after one structured event does not prevent generation stall', () => { assert.equal(1, 1); });\n"
                "test('Grok structured metadata stdout does not satisfy model first-output progress', () => { assert.equal(1, 1); });\n"
                "test('Grok metadata after agent activity does not prevent generation stall', () => { assert.equal(1, 1); });\n"
                "test('Grok misleading type or event fields do not count as ACP model progress', () => { assert.equal(1, 1); });\n"
                "test('ordinary Grok provider exit and invalid delivery persist durable failure classification', () => { assert.equal(1, 1); });\n"
                "test('Grok reviewer shard cannot finalize and synthesis binds exact candidate head', () => { assert.equal(1, 1); });\n"
                "test('Grok reviewer requires explicit phase and immutable candidate commit', () => { assert.equal(1, 1); });\n"
                "test('Grok synthesis validates canonical same-candidate shard receipts', () => { assert.equal(1, 1); });\n"
                "test('Grok cleanup uncertainty is result unknown and not retry safe', () => { assert.equal(1, 1); });\n"
                "test('cleanup uncertainty is fail closed and result unknown', () => { assert.equal(1, 1); });\n"
                "test('Grok payload or data wrappers cannot spoof ACP model progress', () => { assert.equal(1, 1); });\n"
                "test('Grok prompt preparation cleans a temp directory when prompt write fails', () => { assert.equal(1, 1); });\n"
                "test('Grok prompt write plus cleanup failure is fail closed', () => { assert.equal(1, 1); });\n"
                "test('Grok pure-packet Reviewer uses no tools, no planning, structured verdict, and sufficient turn budget', () => { assert.equal(1, 1); });\n"
                "test('Grok Reviewer structured PASS and FAIL are validated independently from process exit', () => { assert.equal(1, 1); });\n"
                "test('Grok Reviewer max turns without verdict is REVIEW_MAX_TURNS, never PASS', () => { assert.equal(1, 1); });\n"
                "test('Grok Reviewer timeout with residual process group is REVIEW_PROCESS_STUCK', () => { assert.equal(1, 1); });\n"
                "test('Grok work_type=review accepts only structured PASS into canonical acceptance', () => { assert.equal(1, 1); });\n"
                "test('Grok work_type=review valid FAIL is terminal findings and is never retried into PASS', () => { assert.equal(1, 1); });\n"
                "test('Grok work_type=review malformed verdict persists REVIEW_OUTPUT_INVALID and no retry', () => { assert.equal(1, 1); });\n"
                "test('Grok Reviewer stall after stdout closes reaps TERM-resistant relay descendants', () => { assert.equal(1, 1); });\n"
                "test('ordinary Grok parent SIGTERM also performs bounded process-group cleanup', () => { assert.equal(1, 1); });\n"
                "test('Grok work_type=review retries transient pre-output failure only once then accepts PASS', () => { assert.equal(1, 1); });\n"
                "test('Grok Reviewer waits for stdio close before classifying final verdict', () => { assert.equal(1, 1); });\n"
                "test('Grok 1.0.13 json-schema envelope validates structuredOutput as Reviewer verdict', () => { assert.equal(1, 1); });\n"
                "test('Grok json-schema envelope rejects conflicting text and structuredOutput', () => { assert.equal(1, 1); });\n"
                "test('Grok Reviewer returns validated verdict after bounded cleanup even if CLI does not exit', () => { assert.equal(1, 1); });\n"
                "test('run_external_agent direct execution survives symlinked filesystem path', () => { assert.equal(1, 1); });\n"
                "test('cleanup failure preserves prior Grok provider exit evidence', () => { assert.equal(1, 1); });\n",
                encoding="utf-8",
            )
            (tests_dir / "__init__.py").write_text("", encoding="utf-8")
            (tests_dir / "test_assignment_runtime.py").write_text(
                "import unittest\n"
                "class ExternalFailureEvidencePersistenceTests(unittest.TestCase):\n"
                "    def test_terminal_persists_external_failure_class_retry_safety_and_details(self): self.assertTrue(True)\n"
                "class ReviewerRuntimeContractTests(unittest.TestCase):\n"
                "    def test_reviewer_terminal_persists_structured_review_status(self): self.assertTrue(True)\n"
                "    def test_reviewer_terminal_rejects_review_status_that_conflicts_with_delivery(self): self.assertTrue(True)\n"
                "    def test_reviewer_infra_status_requires_unresolved_delivery_and_no_verdict(self): self.assertTrue(True)\n",
                encoding="utf-8",
            )
            (tests_dir / "test_reviewer_supervisor.py").write_text(
                "import unittest\n"
                "class ReviewerSupervisorRoutingTests(unittest.TestCase):\n"
                "    def test_web_controller_review_does_not_launch_codex_directly(self): self.assertTrue(True)\n"
                "class ReviewerSupervisorWebHandoffTests(unittest.TestCase):\n"
                "    def test_web_review_emits_canonical_dispatch_request_without_codex(self): self.assertTrue(True)\n"
                "    def test_web_review_finalizes_only_from_canonical_runtime_reviewer_lease(self): self.assertTrue(True)\n",
                encoding="utf-8",
            )
            (tests_dir / "test_web_reentry_adapter.py").write_text(
                "import unittest\n"
                "class WebReentryContinuationRegressionTests(unittest.TestCase):\n"
                "    def test_transient_web_reentry_failure_rearms_existing_continuation_supervisor(self):\n"
                "        self.fail('regression returned')\n",
                encoding="utf-8",
            )
            (tests_dir / "test_web_collaboration_continuation.py").write_text(
                "import unittest\n"
                "class WebCollaborationContinuationRegressionTests(unittest.TestCase):\n"
                "    def test_regression_parent_already_yielded_then_writer_completed_wakes_same_controller_with_next_runnable(self): self.assertTrue(True)\n"
                "    def test_completed_reviewer_uses_same_terminal_continuation_path(self): self.assertTrue(True)\n"
                "    def test_stale_child_is_second_observed_by_existing_audit_and_wakes_same_controller(self): self.assertTrue(True)\n"
                "    def test_duplicate_terminal_observation_after_confirmed_continuation_does_not_wake_twice(self): self.assertTrue(True)\n",
                encoding="utf-8",
            )
            (tests_dir / "test_web_agent_health_supervisor.py").write_text(
                "import unittest\n"
                "class WebAgentHealthSupervisorTests(unittest.TestCase):\n"
                "    def test_global_health_cycle_refreshes_and_schedules_immediate_rule_update_for_registered_controller(self): self.assertTrue(True)\n"
                "    def test_global_health_cycle_does_not_schedule_rule_update_without_explicit_current_target(self): self.assertTrue(True)\n"
                "    def test_health_tick_with_runnable_and_no_child_event_arms_same_controller_without_user_message(self): self.assertTrue(True)\n"
                "    def test_rule_wake_defers_host_runtime_resolution_until_target_is_known(self): self.assertTrue(True)\n"
                "    def test_global_health_cycle_isolates_one_repo_git_failure(self): self.assertTrue(True)\n"
                "    def test_no_canonical_work_does_not_reopen_after_observation_only_turn(self): self.assertTrue(True)\n",
                encoding="utf-8",
            )
            (tests_dir / "test_terminal_continuation.py").write_text(
                "import unittest\n"
                "class TerminalContinuationTests(unittest.TestCase):\n"
                "    def test_terminal_receipt_persists_before_desktop_runtime_is_needed(self): self.assertTrue(True)\n"
                "class PendingTerminalReconcileTests(unittest.TestCase):\n"
                "    def test_reconcile_pending_discovers_canonical_receipts_without_receipt_cli_argument(self): self.assertTrue(True)\n"
                "    def test_reconcile_pending_is_idempotent_and_does_not_mutate_lifecycle_or_dispatch_wake(self): self.assertTrue(True)\n"
                "    def test_reconcile_pending_fails_closed_when_canonical_ownership_is_missing_or_mismatched(self): self.assertTrue(True)\n"
                "    def test_reconcile_pending_cli_has_no_receipt_argument_and_never_self_spawns(self): self.assertTrue(True)\n"
                "    def test_reconcile_pending_classifies_legacy_assignment_without_weakening_current_lease_checks(self): self.assertTrue(True)\n"
                "    def test_reconcile_pending_rejects_lifecycle_change_before_publish(self): self.assertTrue(True)\n"
                "    def test_reconcile_pending_accepts_independent_desktop_target_and_ownership_generations(self): self.assertTrue(True)\n"
                "    def test_reconcile_pending_hashes_same_bytes_it_parses(self): self.assertTrue(True)\n"
                "    def test_reconcile_pending_holds_lifecycle_and_registry_fences_through_audit(self): self.assertTrue(True)\n"
                "    def test_reconcile_pending_holds_runtime_assignment_fence_through_audit(self): self.assertTrue(True)\n"
                "    def test_reconcile_pending_fingerprint_is_order_independent(self): self.assertTrue(True)\n"
                "    def test_atomic_audit_writer_handles_concurrent_publication(self): self.assertTrue(True)\n"
                "class ManualControlCycleReconcileTests(unittest.TestCase):\n"
                "    def test_manual_fenced_control_cycle_reconcile_rejects_untrusted_ai_bridge_receipt(self): self.assertTrue(True)\n"
                "    def test_manual_fenced_control_cycle_reconcile_requires_unexpired_matching_manual_lease(self): self.assertTrue(True)\n"
                "    def test_immutable_cycle_evidence_rejects_forged_snapshot_hash(self): self.assertTrue(True)\n"
                "    def test_immutable_cycle_evidence_requires_terminal_debt_event_type(self): self.assertTrue(True)\n"
                "    def test_manual_reconcile_requires_target_lineage_membership(self): self.assertTrue(True)\n"
                "    def test_manual_reconcile_rejects_untrusted_ai_bridge_even_with_later_receipt(self): self.assertTrue(True)\n"
                "    def test_reconcile_control_cycle_cli_accepts_no_receipt_or_web_session_identity_argument(self): self.assertTrue(True)\n"
                "    def test_manual_reconcile_rejects_untrusted_ai_bridge_before_cycle_evidence(self): self.assertTrue(True)\n"
                "    def test_manual_reconcile_rejects_receipt_from_before_current_target_rotation(self): self.assertTrue(True)\n"
                "    def test_manual_reconcile_never_establishes_idempotence_from_untrusted_ai_bridge(self): self.assertTrue(True)\n"
                "    def test_manual_reconcile_rejects_untrusted_ai_bridge_before_event_type(self): self.assertTrue(True)\n"
                "    def test_manual_reconcile_closes_only_terminal_debt_and_preserves_current_nonterminal_triggers(self): self.assertTrue(True)\n",
                encoding="utf-8",
            )
            (tests_dir / "test_project_context_guard.py").write_text(
                "import unittest\n"
                "class ProjectContextGuardTests(unittest.TestCase):\n"
                "    def test_new_session_project_governance_question_requires_initialized_current_rules(self): self.assertTrue(True)\n"
                "    def test_known_projectless_session_allows_project_fact_prompt_with_unknown_context(self): self.assertTrue(True)\n"
                "    def test_existing_scoring_model_request_must_resolve_real_current_definition(self): self.assertTrue(True)\n"
                "    def test_source_change_before_stop_fails_closed_and_refreshes_for_same_turn_correction(self): self.assertTrue(True)\n"
                "    def test_not_found_unknown_token_does_not_authorize_fabricated_definitive_mechanism(self): self.assertTrue(True)\n"
                "    def test_runtime_state_creation_after_prompt_invalidates_fact_receipt_before_stop(self): self.assertTrue(True)\n"
                "    def test_nested_correction_refresh_preserves_full_applicable_agents_scope_chain(self): self.assertTrue(True)\n"
                "    def test_project_context_separates_unique_controller_from_unverified_web_session(self): self.assertTrue(True)\n"
                "    def test_project_context_reports_verified_bound_web_session_without_changing_ownership(self): self.assertTrue(True)\n"
                "class ControllerActionSourcePromptTests(unittest.TestCase):\n"
                "    def test_rule_ack_prompt_carries_logical_controller_and_actual_execution_source(self): self.assertTrue(True)\n"
                "    def test_live_e2e_accept_prompt_carries_actual_execution_source(self): self.assertTrue(True)\n"
                "    def test_web_bridge_event_uses_actual_web_conversation_as_controller_action_source(self): self.assertTrue(True)\n",
                encoding="utf-8",
            )
            (tests_dir / "test_rule_handshake.py").write_text(
                "import unittest\n"
                "class RuleHandshakeTests(unittest.TestCase):\n"
                "    def test_critical_live_runtime_update_requires_real_e2e_after_ack_and_ledger_sync(self): self.assertTrue(True)\n"
                "    def test_forged_live_e2e_acceptance_without_machine_evidence_stays_blocking(self): self.assertTrue(True)\n"
                "    def test_real_confirmed_wake_followed_by_closed_cycle_can_finalize_live_e2e(self): self.assertTrue(True)\n"
                "    def test_failed_live_e2e_does_not_freeze_invalid_wake_snapshot(self): self.assertTrue(True)\n"
                "    def test_live_e2e_debt_survives_later_nonimpacting_install_until_accepted(self): self.assertTrue(True)\n"
                "    def test_live_e2e_rejects_confirmed_wake_that_predates_rule_ack(self): self.assertTrue(True)\n"
                "    def test_fake_project_chat_cannot_ack_by_claiming_logical_controller_id(self): self.assertTrue(True)\n"
                "    def test_current_web_target_ack_records_exact_source_and_generations(self): self.assertTrue(True)\n"
                "    def test_fake_project_chat_cannot_accept_or_defer_live_e2e(self): self.assertTrue(True)\n"
                "    def test_ack_revalidates_source_fence_immediately_before_persist(self): self.assertTrue(True)\n"
                "    def test_defer_revalidates_source_fence_immediately_before_persist(self): self.assertTrue(True)\n"
                "    def test_accept_revalidates_source_fence_before_freezing_evidence(self): self.assertTrue(True)\n",
                encoding="utf-8",
            )
            (tests_dir / "test_controller_target_guard.py").write_text(
                "import unittest\n"
                "class ControllerTargetGuardTests(unittest.TestCase):\n"
                "    def test_identity_projection_keeps_unique_project_controller_when_session_id_unavailable(self): self.assertTrue(True)\n"
                "    def test_identity_projection_verifies_current_desktop_target_without_changing_controller_id(self): self.assertTrue(True)\n"
                "    def test_identity_projection_marks_old_target_stale_but_keeps_project_ownership(self): self.assertTrue(True)\n"
                "    def test_identity_projection_reports_project_controller_conflict_without_silent_selection(self): self.assertTrue(True)\n"
                "    def test_host_tool_preparation_persists_full_tuple_and_redacts_receipt_material(self): self.assertTrue(True)\n"
                "    def test_host_tool_preparation_rejects_nonce_receipt_and_execution_replays_after_reload(self): self.assertTrue(True)\n"
                "    def test_host_tool_terminal_retry_is_exact_and_direct_close_is_disabled(self): self.assertTrue(True)\n",
                encoding="utf-8",
            )
            (tests_dir / "test_runtime_host_tool_hook.py").write_text(
                "import unittest\n"
                "class RuntimeHostToolHookTests(unittest.TestCase):\n"
                "    def test_verified_pre_is_prepared_and_dispatches_same_execution_id(self): self.assertTrue(True)\n"
                "    def test_pre_requires_verifier_v2_tool_capability(self): self.assertTrue(True)\n"
                "    def test_registered_v2_verifier_cli_is_used_across_the_real_process_boundary(self): self.assertTrue(True)\n"
                "    def test_terminal_requires_structured_success_and_closes_after_lifecycle_commit(self): self.assertTrue(True)\n"
                "    def test_terminal_without_pre_and_generation_rotation_fail_closed(self): self.assertTrue(True)\n"
                "    def test_real_unix_server_correlates_request_and_owns_socket_mode(self): self.assertTrue(True)\n"
                "    def test_unix_server_rejects_unsafe_paths_and_bad_frames(self): self.assertTrue(True)\n",
                encoding="utf-8",
            )
            (tests_dir / "test_web_lifecycle_bridge.py").write_text(
                "import unittest\n"
                "class WebLifecycleComputerLeaseTests(unittest.TestCase):\n"
                "    def test_audit_once_never_uses_manual_resume_lease_as_caller_identity(self): self.assertTrue(True)\n"
                "class ControllerWakeSupervisorTests(unittest.TestCase):\n"
                "    def test_audit_wake_retry_rejects_same_id_receipt_shape_replacement(self): self.assertTrue(True)\n"
                "class WebLifecycleAuditTests(unittest.TestCase):\n"
                "    def test_rule_wake_target_resolution_fails_closed_instead_of_falling_back_to_logical_controller(self): self.assertTrue(True)\n"
                "    def test_rule_wake_rejects_explicit_target_without_canonical_execution_ownership(self): self.assertTrue(True)\n"
                "    def test_audit_once_rule_update_uses_guarded_scheduler_and_never_direct_scheduler(self): self.assertTrue(True)\n"
                "    def test_rule_wake_rejects_legacy_recovery_target_without_trusted_host_origin_proof(self): self.assertTrue(True)\n"
                "    def test_auto_native_stop_confirms_host_observed_canonical_target_already_foreground(self): self.assertTrue(True)\n"
                "    def test_mocked_active_writer_without_host_observation_still_rearms(self): self.assertTrue(True)\n"
                "    def test_serialized_active_writer_claim_cannot_confirm_already_foreground(self): self.assertTrue(True)\n"
                "    def test_execute_native_resume_marks_host_observed_active_writer_process_locally(self): self.assertTrue(True)\n"
                "    def test_auto_native_stop_yields_external_wait_when_desktop_host_reload_is_required(self): self.assertTrue(True)\n"
                "    def test_desktop_host_reload_gate_requires_exact_armed_zero_sequence_canary(self): self.assertTrue(True)\n"
                "    def test_desktop_codex_resolution_prefers_the_app_bundled_runtime(self): self.assertTrue(True)\n"
                "    def test_desktop_codex_resolution_rejects_an_invalid_explicit_override(self): self.assertTrue(True)\n"
                "    def test_rule_wake_uses_desktop_host_adapter_without_resolving_cli(self): self.assertTrue(True)\n"
                "    def test_codex_app_server_turn_uses_official_protocol_and_waits_for_completion(self): self.assertTrue(True)\n"
                "    def test_codex_app_server_active_writer_fails_before_turn_submit(self): self.assertTrue(True)\n"
                "    def test_codex_app_server_turn_start_response_timeout_is_result_unknown(self): self.assertTrue(True)\n"
                "    def test_codex_app_server_eof_after_turn_start_confirmation_is_result_unknown(self): self.assertTrue(True)\n"
                "    def test_desktop_host_resume_uses_app_server_under_target_and_ownership_fence(self): self.assertTrue(True)\n"
                "    def test_desktop_host_resume_missing_app_server_fails_closed_without_cli_fallback(self): self.assertTrue(True)\n"
                "    def test_desktop_host_resume_web_ownership_never_starts_app_server(self): self.assertTrue(True)\n"
                "    def test_execute_native_resume_without_explicit_cli_uses_desktop_host_adapter(self): self.assertTrue(True)\n"
                "    def test_rule_wake_does_not_require_desktop_runtime_for_web_target(self): self.assertTrue(True)\n"
                "class WebAutoStopSupervisorCoalescingTests(unittest.TestCase):\n"
                "    def test_host_neutral_supervisor_omits_missing_desktop_codex_argument(self): self.assertTrue(True)\n"
                "class WebLifecycleBridgeTests(unittest.TestCase):\n"
                "    def test_loaded_verifier_rejects_writable_members_parents_and_replaced_path(self): self.assertTrue(True)\n"
                "    def test_session_start_without_host_session_id_reports_existing_controller_not_new_controller(self): self.assertTrue(True)\n"
                "    def test_session_start_host_attested_recovery_restores_pending_control_loop_same_controller(self): self.assertTrue(True)\n"
                "    def test_same_controller_web_recovery_is_idempotent_after_user_reconfirms_ownership(self): self.assertTrue(True)\n"
                "    def test_web_recovery_preserves_desktop_target_and_only_advances_web_generation(self): self.assertTrue(True)\n"
                "    def test_replace_web_session_bootstrap_rotates_target_and_manual_lease_without_host_attestation(self): self.assertTrue(True)\n"
                "    def test_manual_web_mutations_cannot_downgrade_host_attested_current_target(self): self.assertTrue(True)\n"
                "    def test_same_controller_web_recovery_cannot_replace_different_host_attested_current_target(self): self.assertTrue(True)\n"
                "    def test_legacy_quarantined_target_keeps_manual_replacement_exit(self): self.assertTrue(True)\n"
                "    def test_replace_web_session_rejects_unapproved_session_and_stale_generation(self): self.assertTrue(True)\n"
                "    def test_replace_same_web_target_is_idempotent_and_unbind_tombstones_without_losing_alias_history(self): self.assertTrue(True)\n"
                "class WebAutoStopSupervisorCoalescingTests(unittest.TestCase):\n"
                "    def test_same_terminal_receipt_cannot_be_rescheduled(self): self.assertTrue(True)\n"
                "    def test_host_neutral_supervisor_omits_missing_desktop_codex_argument(self): self.assertTrue(True)\n"
                "    def test_same_receipt_live_supervisor_is_coalesced(self): self.assertTrue(True)\n"
                "    def test_current_token_web_rearm_hands_off_with_force_rearm_proof(self): self.assertTrue(True)\n"
                "    def test_stale_supervisor_token_exits_without_running_impl(self): self.assertTrue(True)\n"
                "class StrongWebSuccessorHandoffTests(unittest.TestCase):\n"
                "    def test_authorize_web_successor_records_only_fenced_fresh_session(self): self.assertTrue(True)\n"
                "    def test_authorize_web_successor_cli_does_not_rotate_target(self): self.assertTrue(True)\n"
                "    def test_authorized_strong_web_successor_rotates_target_and_ownership_once(self): self.assertTrue(True)\n"
                "    def test_strong_web_successor_expired_authorization_does_not_call_verifier(self): self.assertTrue(True)\n"
                "    def test_strong_web_successor_rechecks_target_generation_after_attestation(self): self.assertTrue(True)\n"
                "class WebContinuationSupervisorBootstrapTests(unittest.TestCase):\n"
                "    def test_dead_or_untracked_active_supervisor_requires_bootstrap(self): self.assertTrue(True)\n"
                "    def test_live_active_supervisor_does_not_need_duplicate_bootstrap(self): self.assertTrue(True)\n"
                "    def test_nonretryable_web_failure_same_event_and_fence_stays_quiet(self): self.assertTrue(True)\n"
                "    def test_retry_exhausted_rearms_after_host_delivery_fingerprint_change(self): self.assertTrue(True)\n"
                "    def test_retry_exhausted_rearms_after_controller_fence_change_same_host_fingerprint(self): self.assertTrue(True)\n"
                "    def test_ensure_supervisor_uses_new_receipt_after_controller_fence_change(self): self.assertTrue(True)\n"
                "    def test_confirmed_or_result_unknown_never_rearm_for_host_fingerprint_change(self): self.assertTrue(True)\n"
                "    def test_terminal_rule_delivery_blocks_bootstrap_across_fingerprint_changes(self): self.assertTrue(True)\n"
                "    def test_non_rule_delivery_key_uses_wake_generation_with_current_rule_snapshot(self): self.assertTrue(True)\n"
                "class WebLocalReentryIntegrationTests(unittest.TestCase):\n"
                "    def test_retry_exhausted_persists_host_delivery_fingerprint(self): self.assertTrue(True)\n"
                "    def test_registered_host_nonretryable_failure_persists_quiet_fence(self): self.assertTrue(True)\n"
                "    def test_registered_host_result_unknown_persists_quiet_fence(self): self.assertTrue(True)\n"
                "    def test_confirmed_web_reentry_clears_stale_nonretryable_block_evidence(self): self.assertTrue(True)\n",
                encoding="utf-8",
            )
            (tests_dir / "test_evaluation_transaction.py").write_text(
                "import unittest\n"
                "class EvaluationTransactionTests(unittest.TestCase):\n"
                "    def test_historical_72_8_cannot_satisfy_re_evaluate_current_capability(self): self.assertTrue(True)\n"
                "    def test_read_as_computed_violation_starts_same_flow_correction_with_new_evidence(self): self.assertTrue(True)\n",
                encoding="utf-8",
            )
            (tests_dir / "test_controller_scoring_hook.py").write_text(
                "import unittest\n"
                "class ControllerScoringEvaluationTransactionTests(unittest.TestCase):\n"
                "    def test_current_re_evaluation_does_not_inject_or_accept_historical_score_as_new_result(self): self.assertTrue(True)\n"
                "    def test_computed_current_score_requires_exact_transaction_metadata_and_persists_it(self): self.assertTrue(True)\n"
                "    def test_historical_total_relabelled_computed_without_fresh_dimension_vector_is_blocked(self): self.assertTrue(True)\n"
                "    def test_runtime_rejects_performance_total_that_does_not_match_dimension_vector(self): self.assertTrue(True)\n"
                "    def test_fact_change_before_stop_forces_same_flow_re_evaluation_refresh(self): self.assertTrue(True)\n",
                encoding="utf-8",
            )
            (tests_dir / "test_web_agent_events.py").write_text(
                "import unittest\n"
                "class WebAgentMachineEventSourceTests(unittest.TestCase):\n"
                "    def test_caller_created_file_inside_codex_session_root_cannot_self_attest(self): self.assertTrue(True)\n"
                "    def test_health_supervisor_publishes_diagnostic_source_without_authorizing_it(self): self.assertTrue(True)\n",
                encoding="utf-8",
            )
            (tests_dir / "test_governance.py").write_text(
                "import unittest\n"
                "class GovernanceTests(unittest.TestCase):\n"
                "    def test_web_stdout_marker_never_closes_without_private_terminal_commit(self): self.assertTrue(True)\n"
                "    def test_project_wide_projection_web_active_verify_do_not_starve_mini_runnables(self): self.assertTrue(True)\n"
                "    def test_project_wide_projection_mini_active_does_not_starve_server_or_web(self): self.assertTrue(True)\n"
                "    def test_project_wide_fairness_requires_parallel_dispatch_when_capacity_exists(self): self.assertTrue(True)\n"
                "    def test_pending_dependency_closure_dynamically_enters_project_wide_runnable_projection(self): self.assertTrue(True)\n"
                "    def test_project_wide_fairness_rejects_more_active_dispatches_than_capacity(self): self.assertTrue(True)\n"
                "    def test_pending_parent_partial_dependency_creates_dynamic_runnable_slice(self): self.assertTrue(True)\n"
                "    def test_reviewer_terminal_recomputes_unrelated_project_runnable(self): self.assertTrue(True)\n"
                "    def test_stop_without_current_turn_control_loop_receipt_fails_closed_even_when_idle(self): self.assertTrue(True)\n"
                "    def test_active_writer_does_not_hide_immediate_controller_actions(self): self.assertTrue(True)\n"
                "    def test_control_loop_receipt_rejects_missing_or_reordered_control_steps(self): self.assertTrue(True)\n"
                "    def test_failed_control_cycle_generates_executable_controller_correction(self): self.assertTrue(True)\n"
                "    def test_unfinished_correction_prevents_control_cycle_closure(self): self.assertTrue(True)\n"
                "    def test_same_controller_deviation_fingerprint_escalates_on_recurrence(self): self.assertTrue(True)\n"
                "    def test_direct_cycle_persistence_cannot_fabricate_generic_correction_closure(self): self.assertTrue(True)\n"
                "    def test_reviewer_pass_integration_has_mandatory_verify_converge_recompute_successors(self): self.assertTrue(True)\n"
                "    def test_known_next_action_enters_canonical_controller_action_projection(self): self.assertTrue(True)\n"
                "    def test_continuation_debt_blocks_control_loop_receipt_until_every_action_resolved(self): self.assertTrue(True)\n"
                "    def test_durable_terminal_receipt_enters_debt_once_and_disappears_after_consumption(self): self.assertTrue(True)\n"
                "    def test_hard_blocked_or_deferred_actions_clear_continuation_debt_and_allow_yield(self): self.assertTrue(True)\n"
                "    def test_continuation_debt_fingerprint_escalates_through_existing_recurrence_rules(self): self.assertTrue(True)\n"
                "    def test_event_scope_guard_allows_project_wide_dispatch_across_business_lines(self): self.assertTrue(True)\n"
                "    def test_event_scope_guard_rejects_cross_task_work_without_project_wide_dispatch_proof(self): self.assertTrue(True)\n"
                "    def test_candidate_inventory_batches_ancestry_for_multiple_worktrees(self): self.assertTrue(True)\n"
                "class DurableHostToolReceiptPersistenceTests(unittest.TestCase):\n"
                "    def test_lifecycle_commit_fsync_failure_keeps_registry_pending_and_exact_retry_is_single_trace(self): self.assertTrue(True)\n"
                "    def test_verified_terminal_times_out_hung_snapshot_git_without_closed_or_trace(self): self.assertTrue(True)\n"
                "class ControllerActionSourcePromptTests(unittest.TestCase):\n"
                "    def test_rule_ack_prompt_carries_logical_controller_and_actual_execution_source(self): self.assertTrue(True)\n"
                "    def test_live_e2e_accept_prompt_carries_actual_execution_source(self): self.assertTrue(True)\n"
                "    def test_web_bridge_event_uses_actual_web_conversation_as_controller_action_source(self): self.assertTrue(True)\n",
                encoding="utf-8",
            )
            (tests_dir / "test_desktop_lifecycle_adapter.py").write_text(
                "import unittest\n"
                "class DesktopLifecycleTurnGateTests(unittest.TestCase):\n"
                "    def test_successful_receipt_is_invalidated_when_same_turn_continuation_executes(self): self.assertTrue(True)\n"
                "    def test_status_query_does_not_clear_existing_controller_continuation(self): self.assertTrue(True)\n"
                "    def test_hard_yield_gate_rejects_declared_next_action_when_work_is_runnable(self): self.assertTrue(True)\n"
                "    def test_hard_yield_gate_does_not_invent_work_from_status_only_message(self): self.assertTrue(True)\n"
                "class DesktopOutboundLeaseHookTests(unittest.TestCase):\n"
                "    def test_managed_controller_rejects_unbounded_dev_commands_before_state_write(self): self.assertTrue(True)\n"
                "    def test_foreground_command_gate_allows_bounded_work_and_skips_unmanaged_sessions(self): self.assertTrue(True)\n"
                "    def test_controller_without_explicit_surface_uses_its_registered_canonical_repo(self): self.assertTrue(True)\n"
                "    def test_explicit_controller_surface_rejects_another_checkout(self): self.assertTrue(True)\n"
                "    def test_run_hook_reuses_one_project_snapshot_for_management_fence(self): self.assertTrue(True)\n"
                "    def test_current_desktop_user_prompt_persists_confirmed_native_wake(self): self.assertTrue(True)\n",
                encoding="utf-8",
            )
            (tests_dir / "test_goal_display_sync.py").write_text(
                "import unittest\n"
                "class GoalDisplaySyncTests(unittest.TestCase):\n"
                "    def test_rolled_happy_path_records_exact_host_sequence_and_binding(self): self.assertTrue(True)\n"
                "    def test_successful_rolled_control_receipt_activates_display_sync_debt(self): self.assertTrue(True)\n"
                "    def test_title_failure_recovers_without_recreating_goal(self): self.assertTrue(True)\n"
                "    def test_host_readback_mismatch_retries_only_the_failed_read(self): self.assertTrue(True)\n"
                "    def test_host_readback_rejects_unrelated_objective_and_split_thread_match(self): self.assertTrue(True)\n"
                "    def test_duplicate_rollover_reuses_completed_receipt(self): self.assertTrue(True)\n"
                "    def test_unavailable_host_tool_marks_receipt_degraded(self): self.assertTrue(True)\n"
                "    def test_missing_host_capability_is_degraded_and_exact_target_change_is_fenced(self): self.assertTrue(True)\n",
                encoding="utf-8",
            )
            (tests_dir / "test_web_agent_execution.py").write_text(
                "import unittest\n"
                "class WebAgentExecutionTests(unittest.TestCase):\n"
                "    def test_direct_start_web_assignment_is_rejected_even_with_forged_readiness_probe(self): self.assertTrue(True)\n"
                "    def test_public_web_adapter_rejects_self_asserted_strong_attestation(self): self.assertTrue(True)\n"
                "    def test_host_started_lease_uses_attested_machine_start_time_not_ingestion_time(self): self.assertTrue(True)\n"
                "    def test_host_started_rejects_unattested_or_mismatched_machine_start_time(self): self.assertTrue(True)\n"
                "class RuntimeOwnedWebRecoveryContractTests(unittest.TestCase):\n"
                "    def test_machine_event_source_public_status_cannot_accept_caller_verifier(self): self.assertTrue(True)\n"
                "    def test_forged_local_machine_event_receipt_cannot_enable_production_prepare(self): self.assertTrue(True)\n"
                "    def test_forged_safe_fallback_fields_without_canonical_prior_terminal_are_rejected(self): self.assertTrue(True)\n"
                "    def test_safe_fallback_policy_must_declare_selected_fallback_route_not_only_origin(self): self.assertTrue(True)\n"
                "    def test_canonical_kimi_terminal_with_declared_route_can_prepare_web_fallback(self): self.assertTrue(True)\n"
                "    def test_direct_internal_observation_chain_cannot_create_lease_without_attested_machine_event(self): self.assertTrue(True)\n"
                "    def test_route_policy_accepts_normal_whitespace_declaration(self): self.assertTrue(True)\n"
                "    def test_route_policy_rejects_prefixed_fields_comments_and_negative_examples(self): self.assertTrue(True)\n"
                "    def test_route_policy_accepts_active_rule_after_inactive_examples(self): self.assertTrue(True)\n"
                "    def test_route_policy_rejects_inline_comments_negation_hyphen_prefix_and_value_only_class_marker(self): self.assertTrue(True)\n"
                "    def test_route_policy_normalizes_unicode_dash_variants_before_authorization(self): self.assertTrue(True)\n"
                "    def test_route_policy_uses_positive_prefix_grammar_not_negative_phrase_allowlist(self): self.assertTrue(True)\n"
                "    def test_route_policy_full_line_grammar_rejects_intervening_and_trailing_semantics(self): self.assertTrue(True)\n"
                "    def test_route_policy_requires_canonical_class_then_single_directive_order(self): self.assertTrue(True)\n"
                "    def test_route_policy_requires_class_marker_as_first_nonspace_token(self): self.assertTrue(True)\n"
                "    def test_route_policy_accepts_explicit_web_class_marker_before_fields(self): self.assertTrue(True)\n"
                "    def test_route_policy_rejects_tilde_fenced_route_examples(self): self.assertTrue(True)\n"
                "class StructuredCollaborationTerminalTests(unittest.TestCase):\n"
                "    def test_public_structured_terminal_ingest_rejects_caller_supplied_observation(self): self.assertTrue(True)\n"
                "    def test_internal_terminal_helper_cannot_accept_fabricated_observation_without_attested_path(self): self.assertTrue(True)\n",
                encoding="utf-8",
            )
            (tests_dir / "test_agent_target_resolution.py").write_text(
                "import unittest\n"
                "class LogicalAgentTargetResolutionTests(unittest.TestCase):\n"
                "    def test_identity_contract_directly_supports_controller_agent_reviewer_and_runtime_repair_agent(self): self.assertTrue(True)\n"
                "    def test_verified_execution_target_is_generic_and_carries_double_generation_fence(self): self.assertTrue(True)\n"
                "    def test_verified_execution_target_rejects_wrong_logical_agent_and_stale_fences(self): self.assertTrue(True)\n",
                encoding="utf-8",
            )
            (tests_dir / "test_install_skill.py").write_text(
                "import unittest\n"
                "class InstallCapabilityTests(unittest.TestCase):\n"
                "    def test_identity_capability_report_exposes_runtime_current_entry_host_contract(self): self.assertTrue(True)\n"
                "    def test_installer_web_bridge_preserves_shell_and_lifecycle_exit_precedence(self): self.assertTrue(True)\n"
                "class ProjectContextHookInstallationTests(unittest.TestCase):\n"
                "    def test_runtime_hooks_keep_trust_stable_legacy_indices(self): self.assertTrue(True)\n"
                "    def test_shifted_runtime_hook_groups_migrate_back_without_moving_user_groups(self): self.assertTrue(True)\n"
                "class HostAdapterInstallationTests(unittest.TestCase):\n"
                "    def test_configure_host_adapters_can_update_codex_hooks_without_touching_ai_bridge(self): self.assertTrue(True)\n"
                "    def test_install_cli_skip_ai_bridge_never_rolls_back_concurrent_zshenv_update(self): self.assertTrue(True)\n"
                "class WebAgentHealthServiceInstallationTests(unittest.TestCase):\n"
                "    def test_runtime_service_retires_legacy_per_controller_web_audit_after_new_service_load(self): self.assertTrue(True)\n"
                "    def test_runtime_service_load_failure_preserves_legacy_web_audit(self): self.assertTrue(True)\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "-C", str(source), "add", "."], check=True)
            subprocess.run(["git", "-C", str(source), "commit", "-m", "failing release regression"], check=True, capture_output=True)
            revision = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()

            with self.assertRaisesRegex(ValueError, "Runtime release regression gate failed"):
                _verify_runtime_release_regressions(source, revision)

    def test_fresh_install_manifest_reports_product_and_host_capabilities_without_new_state_identity(self):
        from scripts.install_skill import install_skill
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = self.make_source(root)
            target = root / "adaptive-delivery"
            manifest = install_skill(
                source, target, summary="fresh install", impact="none", stop_condition="ready"
            )

            self.assertEqual(manifest["product_name"], "Adaptive Agent Runtime")
            self.assertEqual(manifest["skill_id"], "adaptive-agent-runtime")
            self.assertIn("capabilities", manifest)
            self.assertEqual(set(manifest["capabilities"]), {"core", "desktop_adapter", "web_local_adapter", "web_agent_execution", "controller_identity"})
            identity = manifest["capabilities"]["controller_identity"]
            self.assertEqual(identity["status"], "enabled")
            self.assertEqual(identity["canonical_identity_cli"], "controller_target_guard.py identity")
            self.assertIn("controller_identity_projection", identity["capabilities"])
            self.assertIn("same_controller_recovery", identity["capabilities"])
            self.assertFalse(identity["strong_web_binding_available"])
            self.assertEqual(identity["host_attestation"], "unavailable")
            web_execution = manifest["capabilities"]["web_agent_execution"]
            self.assertEqual(web_execution["status"], "host_limited")
            self.assertFalse(web_execution["configured"])
            self.assertEqual(web_execution["health_supervisor"], "not_configured")
            self.assertEqual(web_execution["recovery_mode"], "canonical_progress_health_supervisor")
            self.assertEqual(web_execution["host_terminal"], "unavailable")


    def test_legacy_install_with_incomplete_manifest_drops_obsolete_files_not_in_selected_revision(self):
        import json
        from scripts.install_skill import MANIFEST_NAME, install_skill
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = self.make_source(root)
            target = root / "installed" / "adaptive-delivery"
            (target / "scripts").mkdir(parents=True)
            (target / "scripts" / "obsolete_runtime.py").write_text("dangerous old behavior\n", encoding="utf-8")
            (target / MANIFEST_NAME).write_text(json.dumps({"schema_version": 1, "files": {}}), encoding="utf-8")

            install_skill(source, target, summary="clean legacy install", impact="none", stop_condition="exact revision only")

            self.assertFalse((target / "scripts" / "obsolete_runtime.py").exists())
            self.assertTrue((target / "SKILL.md").is_file())

    def test_install_materializes_recorded_revision_even_if_source_changes_after_head_resolution(self):
        import subprocess
        from unittest.mock import patch
        import scripts.install_skill as installer

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = self.make_source(root)
            target = root / "installed" / "adaptive-delivery"
            original = (source / "SKILL.md").read_text(encoding="utf-8")
            real_git = installer._git
            mutated = False

            def racing_git(repo, *args):
                nonlocal mutated
                value = real_git(repo, *args)
                if args == ("rev-parse", "HEAD") and not mutated:
                    (source / "SKILL.md").write_text(original + "# concurrent mutation\\n", encoding="utf-8")
                    mutated = True
                return value

            with patch("scripts.install_skill._git", side_effect=racing_git):
                manifest = installer.install_skill(
                    source,
                    target,
                    summary="race-safe install",
                    impact="none",
                    stop_condition="installed revision is exact",
                )

            committed = subprocess.check_output(
                ["git", "-C", str(source), "show", f"{manifest['revision']}:SKILL.md"], text=True
            )
            installed = (target / "SKILL.md").read_text(encoding="utf-8")

        self.assertTrue(mutated)
        self.assertEqual(installed, committed)
        self.assertNotIn("concurrent mutation", installed)

class InstallPromotionSafetyTests(unittest.TestCase):
    def test_promote_staged_install_cleans_symlink_backup_without_error(self):
        from scripts.install_skill import _promote_staged_install
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            legacy_real = root / "legacy-real"
            legacy_real.mkdir()
            (legacy_real / "old.txt").write_text("old", encoding="utf-8")
            target = root / "adaptive-delivery"
            target.symlink_to(legacy_real, target_is_directory=True)
            stage = root / "stage"
            stage.mkdir()
            (stage / "new.txt").write_text("new", encoding="utf-8")

            _promote_staged_install(stage, target)

            backups = list(root.glob(".adaptive-delivery.backup-*"))
            self.assertTrue(target.is_dir())
            self.assertFalse(target.is_symlink())
            self.assertEqual((target / "new.txt").read_text(encoding="utf-8"), "new")
            self.assertEqual(backups, [])
            self.assertEqual((legacy_real / "old.txt").read_text(encoding="utf-8"), "old")


class ProjectContextHookInstallationTests(unittest.TestCase):
    def test_runtime_hooks_keep_trust_stable_legacy_indices(self):
        import json
        from scripts.install_skill import install_codex_hooks
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            target = root / "adaptive-delivery"
            (target / "scripts").mkdir(parents=True)
            for name in (
                "project_context_guard.py",
                "lifecycle_hook.py",
                "controller_scoring_hook.py",
                "project_context_guard.py",
            ):
                (target / "scripts" / name).write_text("# hook" + chr(10), encoding="utf-8")
            hooks = root / "hooks.json"
            install_codex_hooks(hooks, target, python_executable="/usr/bin/python3")
            value = json.loads(hooks.read_text(encoding="utf-8"))
            for event in ("SessionStart", "UserPromptSubmit", "Stop"):
                entries = value["hooks"][event]
                self.assertEqual(
                    1,
                    sum("project_context_guard.py" in str(item) for item in entries),
                )
                self.assertIn("lifecycle_hook.py", str(entries[0]))
            self.assertIn(
                "project_context_guard.py", str(value["hooks"]["SessionStart"][1])
            )
            for event in ("UserPromptSubmit", "Stop"):
                self.assertIn(
                    "controller_scoring_hook.py", str(value["hooks"][event][1])
                )
                self.assertIn(
                    "project_context_guard.py", str(value["hooks"][event][2])
                )
            self.assertEqual(
                "startup|resume|clear|compact",
                value["hooks"]["SessionStart"][0]["matcher"],
            )
            self.assertEqual(
                0,
                value["hooks"]["UserPromptSubmit"][2]["hooks"][0]["additionalContextLimit"],
            )

    def test_shifted_runtime_hook_groups_migrate_back_without_moving_user_groups(self):
        import json
        from scripts.install_skill import install_codex_hooks
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            target = root / "adaptive-delivery"
            (target / "scripts").mkdir(parents=True)
            hooks = root / "hooks.json"
            hooks.write_text(json.dumps({"hooks": {
                "UserPromptSubmit": [
                    {"hooks": [{"type": "command", "command": "/old/project_context_guard.py"}]},
                    {"hooks": [{"type": "command", "command": "/old/lifecycle_hook.py"}]},
                    {"hooks": [{"type": "command", "command": "/old/controller_scoring_hook.py"}]},
                    {"hooks": [{"type": "command", "command": "echo keep-user-hook"}]},
                ]
            }}), encoding="utf-8")

            install_codex_hooks(hooks, target, python_executable="/usr/bin/python3")
            entries = json.loads(hooks.read_text(encoding="utf-8"))["hooks"]["UserPromptSubmit"]

        self.assertIn("lifecycle_hook.py", str(entries[0]))
        self.assertIn("controller_scoring_hook.py", str(entries[1]))
        self.assertIn("project_context_guard.py", str(entries[2]))
        self.assertIn("echo keep-user-hook", str(entries[3]))


class HostAdapterInstallationTests(unittest.TestCase):
    def test_codex_hook_install_preserves_existing_hooks_and_is_idempotent(self):
        import json
        from scripts.install_skill import install_codex_hooks
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            target = root / "adaptive-delivery"
            (target / "scripts").mkdir(parents=True)
            hooks = root / "hooks.json"
            hooks.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type":"command","command":"echo keep"}]}]}}), encoding="utf-8")

            first = install_codex_hooks(hooks, target, python_executable="/usr/bin/python3")
            second = install_codex_hooks(hooks, target, python_executable="/usr/bin/python3")

            self.assertEqual(first, second)
            config = json.loads(hooks.read_text(encoding="utf-8"))
            self.assertEqual(len([x for x in config["hooks"]["Stop"] if "echo keep" in str(x)]), 1)
            self.assertEqual(len([x for x in config["hooks"]["Stop"] if "lifecycle_hook.py" in str(x)]), 1)
            self.assertEqual(len([x for x in config["hooks"]["Stop"] if "controller_scoring_hook.py" in str(x)]), 1)
            self.assertEqual(len([x for x in config["hooks"]["Stop"] if "project_context_guard.py" in str(x)]), 1)
            for event in ("SessionStart", "PreToolUse", "PostToolUse", "SubagentStop", "UserPromptSubmit"):
                self.assertIn(event, config["hooks"])
            self.assertEqual(
                sum("lifecycle_hook.py" in str(entry) for entry in config["hooks"]["UserPromptSubmit"]),
                1,
            )
            self.assertIn('"matcher": "*"', json.dumps(config["hooks"]["PreToolUse"]))
            self.assertIn('"matcher": "*"', json.dumps(config["hooks"]["PostToolUse"]))
            for event in (
                "SessionStart", "PreToolUse", "PostToolUse", "SubagentStop", "UserPromptSubmit", "Stop",
            ):
                lifecycle_handlers = [
                    handler
                    for group in config["hooks"][event]
                    for handler in group.get("hooks", [])
                    if "lifecycle_hook.py" in str(handler.get("command", ""))
                ]
                self.assertEqual([20], [handler["timeout"] for handler in lifecycle_handlers])

    def test_codex_hook_install_preserves_other_handlers_inside_same_group(self):
        import json
        from scripts.install_skill import install_codex_hooks
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            target = root / "adaptive-delivery"
            (target / "scripts").mkdir(parents=True)
            hooks = root / "hooks.json"
            mixed = {
                "matcher": "*",
                "hooks": [
                    {"type": "command", "command": "/old/lifecycle_hook.py"},
                    {"type": "command", "command": "echo keep-user-handler"},
                ],
            }
            hooks.write_text(json.dumps({"hooks": {"PostToolUse": [mixed], "Stop": [mixed]}}), encoding="utf-8")

            install_codex_hooks(hooks, target, python_executable="/usr/bin/python3")
            config = json.loads(hooks.read_text(encoding="utf-8"))

        self.assertEqual(sum("echo keep-user-handler" in str(entry) for entry in config["hooks"]["PostToolUse"]), 1)
        self.assertEqual(sum("echo keep-user-handler" in str(entry) for entry in config["hooks"]["Stop"]), 1)
        self.assertEqual(sum("lifecycle_hook.py" in str(entry) for entry in config["hooks"]["PostToolUse"]), 1)

    def test_ai_bridge_zshenv_install_preserves_user_content_and_replaces_legacy_block(self):
        from scripts.install_skill import install_ai_bridge_zshenv
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            target = root / "adaptive-delivery"
            (target / "scripts").mkdir(parents=True)
            zshenv = root / ".zshenv"
            zshenv.write_text("export KEEP_ME=1\n# >>> adaptive-delivery web lifecycle bridge >>>\nold block\n# <<< adaptive-delivery web lifecycle bridge <<<\n", encoding="utf-8")
            bridge = root / "ai-bridge"
            bridge.write_text("#!/bin/sh\n", encoding="utf-8")
            bridge.chmod(0o755)

            install_ai_bridge_zshenv(zshenv, target, bridge, python_executable="/usr/bin/python3")
            once = zshenv.read_text(encoding="utf-8")
            install_ai_bridge_zshenv(zshenv, target, bridge, python_executable="/usr/bin/python3")
            twice = zshenv.read_text(encoding="utf-8")

            self.assertEqual(once, twice)
            self.assertIn("export KEEP_ME=1", twice)
            self.assertEqual(twice.count("# >>> adaptive-delivery web lifecycle bridge >>>"), 1)
            self.assertIn(str((target / "scripts" / "web_lifecycle_bridge.py").resolve()), twice)
            self.assertIn(str(bridge.resolve()), twice)

    def test_ai_bridge_zshenv_quotes_shell_metacharacters_in_paths(self):
        from scripts.install_skill import install_ai_bridge_zshenv
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            target = root / 'skill $(touch SHOULD_NOT_RUN) "quoted"'
            (target / "scripts").mkdir(parents=True)
            (target / "scripts" / "web_lifecycle_bridge.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            bridge = root / 'bridge $(touch ALSO_NOT_RUN)'
            bridge.write_text("#!/bin/sh\n", encoding="utf-8"); bridge.chmod(0o755)
            zshenv = root / ".zshenv"

            install_ai_bridge_zshenv(zshenv, target, bridge, python_executable="/usr/bin/python3")
            text = zshenv.read_text(encoding="utf-8")

        self.assertNotIn('if [[ "$_ad_web_parent" == *"' + str(bridge), text)
        self.assertIn("_ad_web_bridge_executable=", text)
        self.assertIn("post-shell", text)

    def test_configure_host_adapters_reports_actual_degradation_and_installed_web_bridge(self):
        from scripts.install_skill import configure_host_adapters
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            target = root / "adaptive-delivery"
            (target / "scripts").mkdir(parents=True)
            for name in ("web_lifecycle_bridge.py", "lifecycle_hook.py", "controller_scoring_hook.py", "project_context_guard.py"):
                script = target / "scripts" / name
                script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
                script.chmod(0o755)
            codex = root / "codex"; codex.write_text("#!/bin/sh\n", encoding="utf-8"); codex.chmod(0o755)
            bridge = root / "ai-bridge"; bridge.write_text("#!/bin/sh\n", encoding="utf-8"); bridge.chmod(0o755)
            hooks = root / "hooks.json"
            zshenv = root / ".zshenv"

            report = configure_host_adapters(
                target,
                codex_executable=codex,
                ai_bridge_executable=bridge,
                hooks_file=hooks,
                zshenv_file=zshenv,
                python_executable="/usr/bin/python3",
            )

            self.assertEqual(report["desktop_adapter"]["status"], "degraded")
            self.assertIn("canary", report["desktop_adapter"]["reason"].lower())
            self.assertEqual(report["web_local_adapter"]["status"], "enabled")
            self.assertTrue(hooks.is_file())
            self.assertTrue(zshenv.is_file())

    def test_configure_host_adapters_can_update_codex_hooks_without_touching_ai_bridge(self):
        from scripts.install_skill import configure_host_adapters, install_ai_bridge_zshenv
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            target = root / "adaptive-delivery"
            (target / "scripts").mkdir(parents=True)
            for name in ("web_lifecycle_bridge.py", "lifecycle_hook.py", "controller_scoring_hook.py", "project_context_guard.py"):
                script = target / "scripts" / name
                script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
                script.chmod(0o755)
            codex = root / "codex"; codex.write_text("#!/bin/sh\n", encoding="utf-8"); codex.chmod(0o755)
            bridge = root / "ai-bridge"; bridge.write_text("#!/bin/sh\n", encoding="utf-8"); bridge.chmod(0o755)
            hooks = root / "hooks.json"
            zshenv = root / ".zshenv"
            install_ai_bridge_zshenv(zshenv, target, bridge, python_executable="/usr/bin/python3")
            zshenv_before = zshenv.read_bytes()

            report = configure_host_adapters(
                target,
                codex_executable=codex,
                ai_bridge_executable=bridge,
                hooks_file=hooks,
                zshenv_file=zshenv,
                python_executable="/usr/bin/python3",
                configure_ai_bridge=False,
            )

            self.assertTrue(hooks.is_file())
            self.assertEqual(zshenv.read_bytes(), zshenv_before)
            self.assertEqual(report["web_local_adapter"]["status"], "enabled")

    def test_install_cli_rolls_back_target_and_host_files_when_adapter_configuration_partially_fails(self):
        import contextlib
        import io
        from unittest.mock import patch
        from scripts.install_skill import main
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = InstallMigrationContractTests().make_source(root)
            target = root / "installed" / "adaptive-delivery"
            target.mkdir(parents=True)
            (target / "old.txt").write_text("old install", encoding="utf-8")
            codex = root / "codex"; codex.write_text("#!/bin/sh\n", encoding="utf-8"); codex.chmod(0o755)
            bridge = root / "ai-bridge"; bridge.write_text("#!/bin/sh\n", encoding="utf-8"); bridge.chmod(0o755)
            hooks = root / "hooks.json"; hooks.write_text('{"keep":"hooks"}\n', encoding="utf-8")
            zshenv = root / ".zshenv"; zshenv.write_text("export KEEP=1\n", encoding="utf-8")
            output = io.StringIO()

            def partial_failure(*args, **kwargs):
                hooks.write_text('{"mutated":true}\n', encoding="utf-8")
                zshenv.write_text("BROKEN=1\n", encoding="utf-8")
                raise OSError("zshenv write failed after partial mutation")

            with patch("scripts.install_skill.configure_host_adapters", side_effect=partial_failure):
                with contextlib.redirect_stdout(output):
                    code = main([
                        "--source", str(source), "--target", str(target),
                        "--summary", "partial adapter failure", "--impact", "none",
                        "--stop-condition", "ready", "--no-configure-runtime-services",
                        "--codex", str(codex),
                        "--ai-bridge", str(bridge), "--hooks-file", str(hooks),
                        "--zshenv-file", str(zshenv),
                    ])

            self.assertNotEqual(code, 0)
            self.assertEqual((target / "old.txt").read_text(encoding="utf-8"), "old install")
            self.assertFalse((target / "SKILL.md").exists())
            self.assertEqual(hooks.read_text(encoding="utf-8"), '{"keep":"hooks"}\n')
            self.assertEqual(zshenv.read_text(encoding="utf-8"), "export KEEP=1\n")
            self.assertIn("rolled back", output.getvalue().lower())

    def test_install_cli_skip_ai_bridge_never_rolls_back_concurrent_zshenv_update(self):
        import contextlib
        import io
        from unittest.mock import patch
        from scripts.install_skill import main
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = InstallMigrationContractTests().make_source(root)
            target = root / "installed" / "adaptive-delivery"
            target.mkdir(parents=True)
            (target / "old.txt").write_text("old install", encoding="utf-8")
            hooks = root / "hooks.json"; hooks.write_text('{"keep":"hooks"}\n', encoding="utf-8")
            zshenv = root / ".zshenv"; zshenv.write_text("export USER_OLD=1\n", encoding="utf-8")
            output = io.StringIO()

            def desktop_hook_failure(*args, **kwargs):
                self.assertFalse(kwargs["configure_ai_bridge"])
                hooks.write_text('{"mutated":true}\n', encoding="utf-8")
                zshenv.write_text("export USER_NEW=1\n", encoding="utf-8")
                raise OSError("desktop hook failure after concurrent zshenv update")

            with patch("scripts.install_skill.configure_host_adapters", side_effect=desktop_hook_failure):
                with contextlib.redirect_stdout(output):
                    code = main([
                        "--source", str(source), "--target", str(target),
                        "--summary", "desktop only rollback", "--impact", "none",
                        "--stop-condition", "ready", "--no-configure-runtime-services",
                        "--no-configure-ai-bridge", "--hooks-file", str(hooks),
                        "--zshenv-file", str(zshenv),
                    ])

            self.assertNotEqual(code, 0)
            self.assertEqual((target / "old.txt").read_text(encoding="utf-8"), "old install")
            self.assertEqual(hooks.read_text(encoding="utf-8"), '{"keep":"hooks"}\n')
            self.assertEqual(zshenv.read_text(encoding="utf-8"), "export USER_NEW=1\n")

    def test_different_targets_sharing_host_files_use_common_resource_lock(self):
        import subprocess, sys, time
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = InstallMigrationContractTests().make_source(root)
            target1 = root / "one" / "adaptive-delivery"
            target2 = root / "two" / "adaptive-delivery"
            hooks = root / "shared" / "hooks.json"
            zshenv = root / "shared" / ".zshenv"
            hooks.parent.mkdir(parents=True)
            hooks.write_text('{"hooks":{}}\n', encoding="utf-8")
            zshenv.write_text("export KEEP=1\n", encoding="utf-8")
            # Hold the lock for a shared host file. A different target must still be blocked.
            shared_lock = hooks.parent / f".{hooks.name}.adaptive-agent-runtime.lock"
            ready = root / "ready"
            holder = subprocess.Popen([sys.executable, "-c",
                "import fcntl,time,sys,pathlib; p=pathlib.Path(sys.argv[1]); p.parent.mkdir(parents=True,exist_ok=True); f=open(p,'a+'); fcntl.flock(f.fileno(),fcntl.LOCK_EX); pathlib.Path(sys.argv[2]).write_text('1'); time.sleep(2)",
                str(shared_lock), str(ready)])
            for _ in range(100):
                if ready.exists(): break
                time.sleep(0.01)
            installer = Path(__file__).resolve().parents[1] / "scripts" / "install_skill.py"
            result = subprocess.run([sys.executable, str(installer), "--source", str(source), "--target", str(target2),
                "--summary", "shared lock", "--impact", "none", "--stop-condition", "blocked",
                "--no-configure-runtime-services",
                "--hooks-file", str(hooks), "--zshenv-file", str(zshenv),
                "--ai-bridge", str(root / "missing-bridge"), "--codex", str(root / "missing-codex")],
                text=True, capture_output=True)
            holder.wait(timeout=3)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("installer", result.stdout.lower())
            self.assertFalse(target2.exists())
            self.assertEqual(hooks.read_text(encoding="utf-8"), '{"hooks":{}}\n')
            self.assertEqual(zshenv.read_text(encoding="utf-8"), "export KEEP=1\n")

    def test_install_cli_rejects_concurrent_installer_without_mutating_target(self):
        import subprocess, sys, time
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = InstallMigrationContractTests().make_source(root)
            target = root / "installed" / "adaptive-delivery"
            target.mkdir(parents=True)
            (target / "old.txt").write_text("old", encoding="utf-8")
            lock = target.parent / f".{target.name}.install.lock"
            ready = root / "ready"
            holder = subprocess.Popen([sys.executable, "-c",
                "import fcntl,time,sys,pathlib; f=open(sys.argv[1],'a+'); fcntl.flock(f.fileno(),fcntl.LOCK_EX); pathlib.Path(sys.argv[2]).write_text('1'); time.sleep(2.0)",
                str(lock), str(ready)])
            for _ in range(50):
                if ready.exists(): break
                time.sleep(0.01)
            installer = Path(__file__).resolve().parents[1] / "scripts" / "install_skill.py"
            result = subprocess.run([sys.executable, str(installer),
                "--source", str(source), "--target", str(target), "--summary", "concurrent",
                "--impact", "none", "--stop-condition", "blocked", "--no-configure-host-adapters",
                "--no-configure-runtime-services"],
                text=True, capture_output=True)
            holder.wait(timeout=3)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("another installer", result.stdout.lower())
            self.assertEqual((target / "old.txt").read_text(encoding="utf-8"), "old")
            self.assertFalse((target / "SKILL.md").exists())

    def test_install_cli_configures_available_host_adapters_in_one_entrypoint(self):
        import contextlib
        import io
        import json
        from scripts.install_skill import main
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = InstallMigrationContractTests().make_source(root)
            target = root / "installed" / "adaptive-delivery"
            codex = root / "codex"; codex.write_text("#!/bin/sh\n", encoding="utf-8"); codex.chmod(0o755)
            bridge = root / "ai-bridge"; bridge.write_text("#!/bin/sh\n", encoding="utf-8"); bridge.chmod(0o755)
            hooks = root / "hooks.json"
            zshenv = root / ".zshenv"
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main([
                    "--source", str(source), "--target", str(target),
                    "--summary", "productized install", "--impact", "none",
                    "--stop-condition", "ready", "--no-configure-runtime-services",
                    "--codex", str(codex),
                    "--ai-bridge", str(bridge), "--hooks-file", str(hooks),
                    "--zshenv-file", str(zshenv),
                ])
            payload = json.loads(output.getvalue().splitlines()[-1])
            hooks_created = hooks.is_file()
            zshenv_created = zshenv.is_file()

        self.assertEqual(code, 0)
        self.assertEqual(payload["product_name"], "Adaptive Agent Runtime")
        self.assertEqual(payload["capabilities"]["web_local_adapter"]["status"], "enabled")
        self.assertTrue(hooks_created)
        self.assertTrue(zshenv_created)

    def test_install_cli_can_configure_codex_hooks_without_configuring_ai_bridge(self):
        import contextlib
        import io
        from scripts.install_skill import main
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = InstallMigrationContractTests().make_source(root)
            target = root / "installed" / "adaptive-delivery"
            codex = root / "codex"; codex.write_text("#!/bin/sh\n", encoding="utf-8"); codex.chmod(0o755)
            bridge = root / "ai-bridge"; bridge.write_text("#!/bin/sh\n", encoding="utf-8"); bridge.chmod(0o755)
            hooks = root / "hooks.json"
            zshenv = root / ".zshenv"
            zshenv.write_text("export KEEP_UNCHANGED=1\n", encoding="utf-8")
            output = io.StringIO()

            with contextlib.redirect_stdout(output):
                code = main([
                    "--source", str(source), "--target", str(target),
                    "--summary", "desktop hooks only", "--impact", "none",
                    "--stop-condition", "ready", "--no-configure-runtime-services",
                    "--no-configure-ai-bridge", "--codex", str(codex),
                    "--ai-bridge", str(bridge), "--hooks-file", str(hooks),
                    "--zshenv-file", str(zshenv),
                ])

            self.assertEqual(code, 0, output.getvalue())
            self.assertTrue(hooks.is_file())
            self.assertEqual(zshenv.read_text(encoding="utf-8"), "export KEEP_UNCHANGED=1\n")


class WebAgentHealthServiceInstallationTests(unittest.TestCase):
    def test_health_service_plist_is_keepalive_and_runs_host_neutral_controller_supervisor(self):
        import plistlib
        from scripts.install_skill import install_web_agent_health_service_plist
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            target = root / "adaptive-delivery"
            (target / "scripts").mkdir(parents=True)
            script = target / "scripts" / "controller_runtime_supervisor.py"
            script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            script.chmod(0o755)
            plist = root / "LaunchAgents" / "web-agent-health.plist"
            install_web_agent_health_service_plist(
                plist, target,
                registry_path=root / "controllers.json",
            )
            payload = plistlib.loads(plist.read_bytes())
        self.assertTrue(payload["RunAtLoad"])
        self.assertTrue(payload["KeepAlive"])
        self.assertEqual(payload["ProgramArguments"][0], str(script.resolve()))
        self.assertIn("--registry", payload["ProgramArguments"])
        self.assertNotIn("web_reentry_adapter.py", " ".join(payload["ProgramArguments"]))

    def test_desktop_background_continuation_rejects_decoy_or_once_plist(self):
        import json
        import plistlib
        from datetime import datetime, timezone
        from scripts.install_skill import (
            detect_host_capabilities, install_web_agent_health_service_plist,
        )

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            target = root / "adaptive-delivery"
            (target / "scripts").mkdir(parents=True)
            script = target / "scripts" / "controller_runtime_supervisor.py"
            script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            script.chmod(0o755)
            plist = root / "controller-runtime.plist"
            heartbeat = root / "controller-runtime-heartbeat.json"
            install_web_agent_health_service_plist(
                plist, target,
                registry_path=root / "controllers.json",
            )
            heartbeat.write_text(json.dumps({
                "schema_version": 1,
                "state": "ready",
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "pid": 43,
                "supervisor_contract": "host_neutral_controller_runtime_v1",
            }), encoding="utf-8")
            canonical = plistlib.loads(plist.read_bytes())
            installed_script = canonical["ProgramArguments"][0]

            invalid_arguments = (
                [
                    "/bin/false", installed_script,
                    "--registry", str(root / "controllers.json"),
                ],
                [
                    "--registry", str(root / "controllers.json"),
                    installed_script, "--poll-seconds", "15",
                ],
                [*canonical["ProgramArguments"], "--once"],
                [*canonical["ProgramArguments"][:-1], "inf"],
                [*canonical["ProgramArguments"][:-1], "1e309"],
            )
            for arguments in invalid_arguments:
                with self.subTest(arguments=arguments):
                    payload = dict(canonical)
                    payload["ProgramArguments"] = arguments
                    plist.write_bytes(plistlib.dumps(payload))
                    report = detect_host_capabilities(
                        codex_executable=root / "missing-codex",
                        skill_root=target,
                        ai_bridge_executable=root / "missing-ai-bridge",
                        hooks_file=root / "hooks.json",
                        zshenv_file=root / ".zshenv",
                        health_service_plist=plist,
                        runtime_supervisor_heartbeat=heartbeat,
                    )
                    self.assertEqual(
                        report["desktop_adapter"]["background_continuation"],
                        "not_configured",
                    )
                    self.assertFalse(
                        report["desktop_adapter"]["background_continuation_ready"]
                    )

            payload = dict(canonical)
            payload["Program"] = False
            plist.write_bytes(plistlib.dumps(payload))
            malformed_program_report = detect_host_capabilities(
                codex_executable=root / "missing-codex",
                skill_root=target,
                ai_bridge_executable=root / "missing-ai-bridge",
                hooks_file=root / "hooks.json",
                zshenv_file=root / ".zshenv",
                health_service_plist=plist,
                runtime_supervisor_heartbeat=heartbeat,
            )
            self.assertEqual(
                malformed_program_report["desktop_adapter"]["background_continuation"],
                "not_configured",
            )

    def test_desktop_background_continuation_is_reported_without_ai_bridge(self):
        import json
        from datetime import datetime, timedelta, timezone
        from scripts.install_skill import (
            detect_host_capabilities, install_web_agent_health_service_plist,
        )
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            target = root / "adaptive-delivery"
            (target / "scripts").mkdir(parents=True)
            script = target / "scripts" / "controller_runtime_supervisor.py"
            script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            script.chmod(0o755)
            plist = root / "controller-runtime.plist"
            heartbeat = root / "controller-runtime-heartbeat.json"
            install_web_agent_health_service_plist(
                plist, target,
                registry_path=root / "controllers.json",
            )
            report = detect_host_capabilities(
                codex_executable=root / "missing-codex",
                skill_root=target,
                ai_bridge_executable=root / "missing-ai-bridge",
                hooks_file=root / "hooks.json",
                zshenv_file=root / ".zshenv",
                health_service_plist=plist,
                runtime_supervisor_heartbeat=heartbeat,
            )

            self.assertEqual(
                report["desktop_adapter"]["background_continuation"],
                "configured_unverified",
            )
            self.assertTrue(
                report["desktop_adapter"]["continuation_independent_of_ai_bridge"]
            )
            self.assertEqual(report["web_local_adapter"]["status"], "degraded")

            heartbeat.write_text(json.dumps({
                "schema_version": 1,
                "state": "ready",
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "pid": 42,
            }), encoding="utf-8")
            legacy_report = detect_host_capabilities(
                codex_executable=root / "missing-codex",
                skill_root=target,
                ai_bridge_executable=root / "missing-ai-bridge",
                hooks_file=root / "hooks.json",
                zshenv_file=root / ".zshenv",
                health_service_plist=plist,
                runtime_supervisor_heartbeat=heartbeat,
            )
            self.assertEqual(
                legacy_report["desktop_adapter"]["background_continuation"],
                "configured_unverified",
            )
            heartbeat.write_text(json.dumps({
                "schema_version": 1,
                "state": "ready",
                "observed_at": (
                    datetime.now(timezone.utc) - timedelta(seconds=91)
                ).isoformat(),
                "pid": 42,
                "supervisor_contract": "host_neutral_controller_runtime_v1",
            }), encoding="utf-8")
            stale_report = detect_host_capabilities(
                codex_executable=root / "missing-codex",
                skill_root=target,
                ai_bridge_executable=root / "missing-ai-bridge",
                hooks_file=root / "hooks.json",
                zshenv_file=root / ".zshenv",
                health_service_plist=plist,
                runtime_supervisor_heartbeat=heartbeat,
            )
            self.assertEqual(
                stale_report["desktop_adapter"]["background_continuation"],
                "configured_unverified",
            )
            heartbeat.write_text(json.dumps({
                "schema_version": 1,
                "state": "ready",
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "pid": 43,
                "supervisor_contract": "host_neutral_controller_runtime_v1",
            }), encoding="utf-8")
            live_report = detect_host_capabilities(
                codex_executable=root / "missing-codex",
                skill_root=target,
                ai_bridge_executable=root / "missing-ai-bridge",
                hooks_file=root / "hooks.json",
                zshenv_file=root / ".zshenv",
                health_service_plist=plist,
                runtime_supervisor_heartbeat=heartbeat,
            )
            self.assertEqual(
                live_report["desktop_adapter"]["background_continuation"], "ready"
            )

    def test_web_agent_execution_capability_requires_matching_health_service(self):
        from scripts.install_skill import (
            detect_host_capabilities, install_web_agent_health_service_plist,
        )
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            target = root / "adaptive-delivery"
            (target / "scripts").mkdir(parents=True)
            script = target / "scripts" / "controller_runtime_supervisor.py"
            script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            script.chmod(0o755)
            plist = root / "health.plist"
            before = detect_host_capabilities(
                skill_root=target,
                ai_bridge_executable=root / "missing-bridge",
                hooks_file=root / "hooks.json", zshenv_file=root / ".zshenv",
                health_service_plist=plist,
            )
            install_web_agent_health_service_plist(
                plist, target,
                registry_path=root / "controllers.json",
            )
            after = detect_host_capabilities(
                skill_root=target,
                ai_bridge_executable=root / "missing-bridge",
                hooks_file=root / "hooks.json", zshenv_file=root / ".zshenv",
                health_service_plist=plist,
            )
        self.assertFalse(before["web_agent_execution"]["configured"])
        self.assertFalse(after["web_agent_execution"]["configured"])
        self.assertEqual(after["web_agent_execution"]["health_supervisor"], "launchd_keepalive")
        self.assertEqual(after["web_agent_execution"]["continuation"], "existing_web_reentry_supervisor")
        self.assertEqual(after["web_agent_execution"]["structured_terminal"], "unavailable")
        self.assertIn("machine event source", after["web_agent_execution"]["reason"])

    def test_web_agent_execution_capability_requires_both_health_and_machine_event_source(self):
        import json
        from datetime import datetime, timezone
        from scripts.install_skill import (
            detect_host_capabilities, install_web_agent_health_service_plist,
        )
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            target = root / "adaptive-delivery"
            (target / "scripts").mkdir(parents=True)
            script = target / "scripts" / "controller_runtime_supervisor.py"
            script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            script.chmod(0o755)
            plist = root / "health.plist"
            source_receipt = root / "event-source.json"
            install_web_agent_health_service_plist(
                plist, target,
                registry_path=root / "controllers.json",
            )
            source_receipt.write_text(json.dumps({
                "schema_version": 1,
                "state": "ready",
                "source": "chatgpt_subagent_machine_events",
                "events": ["started", "completed", "failed", "interrupted", "cancelled", "disconnected"],
                "observed_at": datetime.now(timezone.utc).isoformat(),
            }), encoding="utf-8")
            report = detect_host_capabilities(
                skill_root=target,
                ai_bridge_executable=root / "missing-bridge",
                hooks_file=root / "hooks.json",
                zshenv_file=root / ".zshenv",
                health_service_plist=plist,
                web_event_source_receipt=source_receipt,
            )["web_agent_execution"]
        self.assertFalse(report["configured"])
        self.assertEqual(report["structured_terminal"], "unavailable")
        self.assertEqual(report["dispatch_interception"], "unavailable_on_chatgpt_web")
        self.assertTrue(any(word in report["reason"].lower() for word in ("trusted", "trustworthy")))
        self.assertEqual(report["continuation"], "existing_web_reentry_supervisor")

    def test_configure_runtime_health_service_activates_keepalive_without_host_adapter_changes(self):
        from scripts.install_skill import configure_runtime_services
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            target = root / "adaptive-delivery"
            (target / "scripts").mkdir(parents=True)
            script = target / "scripts" / "controller_runtime_supervisor.py"
            script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            script.chmod(0o755)
            plist = root / "LaunchAgents" / "health.plist"
            loaded = []
            report = configure_runtime_services(
                target,
                health_service_plist=plist,
                registry_path=root / "controllers.json",
                service_loader=lambda path: loaded.append(path) or {"state": "loaded"},
            )
        self.assertEqual(loaded, [plist.resolve()])
        self.assertEqual(report["state"], "loaded")
        self.assertTrue(report["configured"])

def _runtime_service_retires_legacy_per_controller_web_audit_after_new_service_load(self):
    import plistlib
    from scripts.install_skill import configure_runtime_services
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        target = root / "adaptive-delivery"
        (target / "scripts").mkdir(parents=True)
        script = target / "scripts" / "controller_runtime_supervisor.py"
        script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        script.chmod(0o755)
        launchagents = root / "LaunchAgents"; launchagents.mkdir()
        health = launchagents / "com.openai.adaptive-agent-runtime.web-agent-health.plist"
        legacy_a = launchagents / "ai.openai.adaptive-delivery.web-lifecycle.controller-a.plist"
        legacy_b = launchagents / "com.openai.adaptive-delivery.web-lifecycle.controller-b.plist"
        unrelated = launchagents / "com.example.keep.plist"
        for p in (legacy_a, legacy_b, unrelated):
            p.write_bytes(plistlib.dumps({"Label": p.stem, "ProgramArguments": ["/bin/true"]}))
        loaded=[]; retired=[]
        def loader(path):
            loaded.append(path)
            return {"state":"loaded"}
        def retire(path):
            retired.append(path)
            path.unlink(missing_ok=True)
        report = configure_runtime_services(
            target, health_service_plist=health,
            registry_path=root / "controllers.json",
            service_loader=loader,
            legacy_service_unloader=retire,
        )
        exists = {p.name:p.exists() for p in (health, legacy_a, legacy_b, unrelated)}
    self.assertEqual(loaded, [health.resolve()])
    self.assertEqual(set(retired), {legacy_a.resolve(), legacy_b.resolve()})
    self.assertEqual(set(report["retired_legacy_services"]), {str(legacy_a.resolve()), str(legacy_b.resolve())})
    self.assertTrue(exists[health.name])
    self.assertFalse(exists[legacy_a.name])
    self.assertFalse(exists[legacy_b.name])
    self.assertTrue(exists[unrelated.name])


def _runtime_service_load_failure_preserves_legacy_web_audit(self):
    from scripts.install_skill import configure_runtime_services
    with tempfile.TemporaryDirectory() as d:
        root=Path(d)
        target=root/"adaptive-delivery"; (target/"scripts").mkdir(parents=True)
        script=target/"scripts"/"controller_runtime_supervisor.py"
        script.write_text("#!/usr/bin/env python3\n", encoding="utf-8"); script.chmod(0o755)
        launchagents=root/"LaunchAgents"; launchagents.mkdir()
        health=launchagents/"com.openai.adaptive-agent-runtime.web-agent-health.plist"
        legacy=launchagents/"ai.openai.adaptive-delivery.web-lifecycle.controller-a.plist"
        legacy.write_text("legacy", encoding="utf-8")
        retired=[]
        with self.assertRaisesRegex(OSError, "new service failed"):
            configure_runtime_services(
                target, health_service_plist=health,
                registry_path=root/"controllers.json",
                service_loader=lambda _path: (_ for _ in ()).throw(OSError("new service failed")),
                legacy_service_unloader=lambda path: retired.append(path),
            )
        legacy_exists=legacy.exists()
    self.assertEqual(retired, [])
    self.assertTrue(legacy_exists)


WebAgentHealthServiceInstallationTests.test_runtime_service_retires_legacy_per_controller_web_audit_after_new_service_load = _runtime_service_retires_legacy_per_controller_web_audit_after_new_service_load
WebAgentHealthServiceInstallationTests.test_runtime_service_load_failure_preserves_legacy_web_audit = _runtime_service_load_failure_preserves_legacy_web_audit
