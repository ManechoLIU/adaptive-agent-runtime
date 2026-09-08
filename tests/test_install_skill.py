import tempfile
import unittest
from pathlib import Path

from scripts.install_skill import detect_host_capabilities


class InstallCapabilityTests(unittest.TestCase):
    def test_desktop_adapter_is_enabled_only_by_an_exact_live_canary_receipt(self):
        import hashlib
        import json
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
            canary = root / "desktop-canary.json"
            receipt = {
                "schema_version": 3,
                "status": "passed",
                "controller_session_id": "controller-1",
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
                    "session_started",
                    "pre_tool_allowed",
                    "post_tool_observed",
                    "receipt_latched",
                    "same_turn_denied",
                    "stop_observed",
                    "next_turn_allowed",
                    "subagent_stop_observed",
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
            )

            self.assertEqual(report["desktop_adapter"]["status"], "enabled")
            self.assertEqual(
                report["desktop_adapter"]["goal_display_sync"], "configured_unverified"
            )
            self.assertEqual(
                report["web_local_adapter"]["goal_display_sync"],
                "degraded_host_capability_unavailable",
            )

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
            canary = root / "desktop-canary.json"
            canary.write_text(
                json.dumps(
                    {
                        "schema_version": 3,
                        "status": "passed",
                        "controller_session_id": "controller-1",
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
                                "session_started",
                                "pre_tool_allowed",
                                "post_tool_observed",
                                "receipt_latched",
                                "same_turn_denied",
                                "stop_observed",
                                "next_turn_allowed",
                                "subagent_stop_observed",
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
        self.assertIn("resolve-manual-web-session --cwd \"$PWD\"", block)
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
            "control_event_guard.py", "event_scope_guard.py", "controller_state.py", "controller_target_guard.py", "assignment_lease_guard.py",
            "controller_scoring_guard.py", "project_context_guard.py", "rule_handshake.py", "evaluation_transaction.py",
        ):
            script = source / "scripts" / name
            if name == "controller_target_guard.py":
                script.write_text(
                    "#!/usr/bin/env python3\n"
                    "import json, sys\n"
                    "if len(sys.argv) > 1 and sys.argv[1] == 'capabilities':\n"
                    "    print(json.dumps({'schema_version': 1, 'canonical_identity_cli': 'controller_target_guard.py identity', 'capabilities': ['controller_identity_projection', 'same_controller_recovery', 'web_session_binding', 'target_generation_fence']}))\n"
                    "    raise SystemExit(0)\n"
                    "raise SystemExit(0)\n",
                    encoding="utf-8",
                )
            else:
                script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            script.chmod(0o755)
        subprocess.run(["git", "-C", str(source), "add", "."], check=True)
        subprocess.run(["git", "-C", str(source), "commit", "-m", "initial"], check=True, capture_output=True)
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

    def test_runtime_release_gate_includes_host_ownership_and_yield_enforcement_regressions(self):
        from scripts.install_skill import RUNTIME_RELEASE_REGRESSION_TESTS, RUNTIME_RELEASE_REQUIRED_FILES

        required_tests = {
            "tests.test_goal_display_sync.GoalDisplaySyncTests.test_rolled_happy_path_records_exact_host_sequence_and_binding",
            "tests.test_goal_display_sync.GoalDisplaySyncTests.test_successful_rolled_control_receipt_activates_display_sync_debt",
            "tests.test_goal_display_sync.GoalDisplaySyncTests.test_title_failure_recovers_without_recreating_goal",
            "tests.test_goal_display_sync.GoalDisplaySyncTests.test_host_readback_mismatch_retries_only_the_failed_read",
            "tests.test_goal_display_sync.GoalDisplaySyncTests.test_duplicate_rollover_reuses_completed_receipt",
            "tests.test_goal_display_sync.GoalDisplaySyncTests.test_unavailable_host_tool_marks_receipt_degraded",
            "tests.test_goal_display_sync.GoalDisplaySyncTests.test_missing_host_capability_is_degraded_and_exact_target_change_is_fenced",
            "tests.test_desktop_lifecycle_adapter.DesktopOutboundLeaseHookTests.test_managed_controller_rejects_unbounded_dev_commands_before_state_write",
            "tests.test_desktop_lifecycle_adapter.DesktopOutboundLeaseHookTests.test_foreground_command_gate_allows_bounded_work_and_skips_unmanaged_sessions",
            "tests.test_controller_target_guard.ControllerTargetGuardTests.test_claim_controller_host_desktop_after_web_increments_one_cross_host_generation",
            "tests.test_web_lifecycle_bridge.WebLocalReentryIntegrationTests.test_direct_wake_rejects_confirmed_web_result_after_desktop_handoff",
            "tests.test_web_lifecycle_bridge.WebLocalReentryIntegrationTests.test_desktop_result_cannot_persist_or_rearm_after_web_handoff",
            "tests.test_governance.GovernanceTests.test_control_loop_stop_rejection_reopens_pending_event_even_if_prior_state_was_closed",
            "tests.test_web_lifecycle_bridge.WebLifecycleBridgeTests.test_dispatch_event_result_treats_decision_block_as_logical_yield_rejection",
            "tests.test_web_agent_health_supervisor.WebAgentHealthSupervisorTests.test_health_tick_reopens_persisted_non_user_next_action_without_stop_callback",
            "tests.test_web_lifecycle_bridge.WebLifecycleBridgeTests.test_production_bridge_has_no_trusted_web_attestation_verifier",
            "tests.test_web_lifecycle_bridge.WebLifecycleBridgeTests.test_registered_web_verifier_loads_pinned_external_runtime_host_cli",
            "tests.test_web_lifecycle_bridge.WebLifecycleBridgeTests.test_registered_web_verifier_exposes_pinned_host_submit_adapter",
            "tests.test_web_lifecycle_bridge.WebLifecycleBridgeTests.test_registered_web_verifier_rechecks_bundle_before_each_execution",
            "tests.test_web_lifecycle_bridge.WebHostNativeWakeIsolationTests.test_registered_external_web_host_submit_adapter_is_used_without_caller_injection",
            "tests.test_web_lifecycle_bridge.WebLifecycleBridgeTests.test_malformed_registered_web_verifier_config_fails_closed_without_manual_fallback",
            "tests.test_web_lifecycle_bridge.WebLifecycleBridgeTests.test_browser_tab_receipt_cannot_recover_an_unverified_web_session",
            "tests.test_web_lifecycle_bridge.WebLifecycleBridgeTests.test_manual_web_mutations_cannot_downgrade_host_attested_current_target",
            "tests.test_web_lifecycle_bridge.WebLifecycleBridgeTests.test_same_controller_web_recovery_cannot_replace_different_host_attested_current_target",
            "tests.test_web_lifecycle_bridge.WebLifecycleBridgeTests.test_legacy_quarantined_target_keeps_trusted_host_recovery_exit",
            "tests.test_web_lifecycle_bridge.WebLifecycleBridgeTests.test_legacy_quarantined_target_keeps_manual_replacement_exit",
            "tests.test_web_lifecycle_bridge.WebLifecycleBridgeTests.test_same_controller_web_recovery_rotates_existing_resume_only_lease_to_new_verified_target",
            "tests.test_web_lifecycle_bridge.WebLifecycleBridgeTests.test_session_start_verified_target_rotates_existing_resume_lease_without_new_ownership_claim",
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
            "tests.test_web_lifecycle_bridge.WebHostNativeWakeIsolationTests.test_registered_current_web_adapter_is_fenced_and_host_attested",
            "tests.test_web_lifecycle_bridge.WebHostNativeWakeIsolationTests.test_current_web_adapter_is_not_called_when_pre_delivery_attestation_rejects",
            "tests.test_web_lifecycle_bridge.WebHostNativeWakeIsolationTests.test_current_web_adapter_receipt_must_correlate_origin_call_receipt",
            "tests.test_web_lifecycle_bridge.WebHostNativeWakeIsolationTests.test_current_web_adapter_is_not_called_for_malformed_origin_attestation",
            "tests.test_web_lifecycle_bridge.WebHostNativeWakeIsolationTests.test_registered_current_web_adapter_without_ownership_is_never_called",
            "tests.test_web_lifecycle_bridge.WebReentryDebounceTests.test_web_confirmed_wake_is_not_debounced_after_same_target_ownership_reclaim",
            "tests.test_rule_handshake.RuleHandshakeTests.test_live_e2e_rejects_stale_ownership_generation_before_acceptance",
            "tests.test_web_reentry_adapter.AiBridgeMcpDiscoveryTests.test_discovery_selects_only_live_loopback_endpoint_and_accepts_url_prefix",
        }
        self.assertTrue(required_tests.issubset(set(RUNTIME_RELEASE_REGRESSION_TESTS)))
        self.assertIn("scripts/controller_runtime_supervisor.py", RUNTIME_RELEASE_REQUIRED_FILES)
        self.assertIn("scripts/goal_display_sync.py", RUNTIME_RELEASE_REQUIRED_FILES)
        self.assertIn("tests/test_goal_display_sync.py", RUNTIME_RELEASE_REQUIRED_FILES)
        self.assertIn("tests/test_web_agent_health_supervisor.py", RUNTIME_RELEASE_REQUIRED_FILES)
        self.assertIn("tests/test_terminal_continuation.py", RUNTIME_RELEASE_REQUIRED_FILES)
        self.assertIn("scripts/terminal_continuation.py", RUNTIME_RELEASE_REQUIRED_FILES)
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
            for name in ("web_agent_execution.py", "web_reentry_adapter.py", "terminal_continuation.py", "goal_display_sync.py"):
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
                "test('fresh legacy v1 assignment ACK cannot launch external provider', () => { assert.equal(1, 1); });\n",
                encoding="utf-8",
            )
            (tests_dir / "__init__.py").write_text("", encoding="utf-8")
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
                "    def test_canonical_runnable_reopens_continuation_without_user_message(self): self.assertTrue(True)\n"
                "    def test_no_canonical_work_does_not_reopen_after_observation_only_turn(self): self.assertTrue(True)\n"
                "    def test_health_tick_reopens_persisted_non_user_next_action_without_stop_callback(self): self.assertTrue(True)\n",
                encoding="utf-8",
            )
            (tests_dir / "test_terminal_continuation.py").write_text(
                "import unittest\n"
                "class PendingTerminalReconcileTests(unittest.TestCase):\n"
                "    def test_reconcile_pending_discovers_canonical_receipts_without_receipt_cli_argument(self): self.assertTrue(True)\n"
                "    def test_reconcile_pending_is_idempotent_and_does_not_mutate_lifecycle_or_dispatch_wake(self): self.assertTrue(True)\n"
                "    def test_reconcile_pending_fails_closed_when_canonical_ownership_is_missing_or_mismatched(self): self.assertTrue(True)\n"
                "    def test_reconcile_pending_cli_has_no_receipt_argument_and_never_self_spawns(self): self.assertTrue(True)\n"
                "    def test_reconcile_pending_classifies_legacy_assignment_without_weakening_current_lease_checks(self): self.assertTrue(True)\n"
                "    def test_reconcile_pending_rejects_lifecycle_change_before_publish(self): self.assertTrue(True)\n"
                "    def test_reconcile_pending_rejects_target_generation_change_before_publish(self): self.assertTrue(True)\n"
                "    def test_reconcile_pending_hashes_same_bytes_it_parses(self): self.assertTrue(True)\n"
                "    def test_reconcile_pending_holds_lifecycle_and_registry_fences_through_audit(self): self.assertTrue(True)\n"
                "    def test_reconcile_pending_holds_runtime_assignment_fence_through_audit(self): self.assertTrue(True)\n"
                "    def test_reconcile_pending_fingerprint_is_order_independent(self): self.assertTrue(True)\n"
                "    def test_atomic_audit_writer_handles_concurrent_publication(self): self.assertTrue(True)\n"
                "class ManualControlCycleReconcileTests(unittest.TestCase):\n"
                "    def test_manual_fenced_control_cycle_reconcile_closes_only_reconciled_terminal_debt_without_verifying_web_identity(self): self.assertTrue(True)\n"
                "    def test_manual_fenced_control_cycle_reconcile_requires_unexpired_matching_manual_lease(self): self.assertTrue(True)\n"
                "    def test_manual_reconcile_requires_target_lineage_membership(self): self.assertTrue(True)\n"
                "    def test_manual_reconcile_skips_later_unrelated_allowed_receipt(self): self.assertTrue(True)\n"
                "    def test_reconcile_control_cycle_cli_accepts_no_receipt_or_web_session_identity_argument(self): self.assertTrue(True)\n"
                "    def test_manual_reconcile_rejects_forged_immutable_cycle_evidence(self): self.assertTrue(True)\n"
                "    def test_manual_reconcile_rejects_receipt_from_before_current_target_rotation(self): self.assertTrue(True)\n"
                "    def test_manual_reconcile_is_idempotent_after_durable_lifecycle_closure(self): self.assertTrue(True)\n"
                "    def test_manual_reconcile_rejects_non_terminal_debt_closed_cycle_even_with_matching_hash(self): self.assertTrue(True)\n"
                "    def test_manual_reconcile_closes_only_terminal_debt_and_preserves_current_nonterminal_triggers(self): self.assertTrue(True)\n",
                encoding="utf-8",
            )
            (tests_dir / "test_project_context_guard.py").write_text(
                "import unittest\n"
                "class ProjectContextGuardTests(unittest.TestCase):\n"
                "    def test_new_session_project_governance_question_requires_initialized_current_rules(self): self.assertTrue(True)\n"
                "    def test_existing_scoring_model_request_must_resolve_real_current_definition(self): self.assertTrue(True)\n"
                "    def test_source_change_before_stop_fails_closed_and_refreshes_for_same_turn_correction(self): self.assertTrue(True)\n"
                "    def test_not_found_unknown_token_does_not_authorize_fabricated_definitive_mechanism(self): self.assertTrue(True)\n"
                "    def test_runtime_state_creation_after_prompt_invalidates_fact_receipt_before_stop(self): self.assertTrue(True)\n"
                "    def test_nested_correction_refresh_preserves_full_applicable_agents_scope_chain(self): self.assertTrue(True)\n"
                "    def test_missing_required_identity_capability_reports_contract_drift_without_revoking_controller(self): self.assertTrue(True)\n"
                "    def test_contract_drift_does_not_upgrade_foreign_unverified_session_to_degraded(self): self.assertTrue(True)\n"
                "    def test_project_context_separates_unique_controller_from_unverified_web_session(self): self.assertTrue(True)\n"
                "    def test_project_context_reports_verified_bound_web_session_without_changing_ownership(self): self.assertTrue(True)\n",
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
                "    def test_live_e2e_rejects_stale_ownership_generation_before_acceptance(self): self.assertTrue(True)\n",
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
                "    def test_claim_controller_host_desktop_after_web_increments_one_cross_host_generation(self): self.assertTrue(True)\n",
                encoding="utf-8",
            )
            (tests_dir / "test_web_lifecycle_bridge.py").write_text(
                "import unittest\n"
                "class WebLifecycleAuditTests(unittest.TestCase):\n"
                "    def test_rule_wake_target_resolution_fails_closed_instead_of_falling_back_to_logical_controller(self): self.assertTrue(True)\n"
                "    def test_rule_wake_rejects_explicit_target_without_canonical_execution_ownership(self): self.assertTrue(True)\n"
                "    def test_audit_once_rule_update_uses_guarded_scheduler_and_never_direct_scheduler(self): self.assertTrue(True)\n"
                "    def test_rule_wake_rejects_legacy_recovery_target_without_trusted_host_origin_proof(self): self.assertTrue(True)\n"
                "class WebLifecycleBridgeTests(unittest.TestCase):\n"
                "    def test_session_start_without_host_session_id_reports_existing_controller_not_new_controller(self): self.assertTrue(True)\n"
                "    def test_session_start_host_attested_recovery_restores_pending_control_loop_same_controller(self): self.assertTrue(True)\n"
                "    def test_same_controller_web_recovery_verifier_exception_degrades_without_revoking_controller(self): self.assertTrue(True)\n"
                "    def test_same_controller_web_recovery_rejects_attestation_if_target_generation_changes_before_lock(self): self.assertTrue(True)\n"
                "    def test_same_controller_web_recovery_is_idempotent_after_user_reconfirms_ownership(self): self.assertTrue(True)\n"
                "    def test_web_recovery_preserves_desktop_target_and_only_advances_web_generation(self): self.assertTrue(True)\n"
                "    def test_dispatch_event_result_treats_decision_block_as_logical_yield_rejection(self): self.assertTrue(True)\n"
                "    def test_production_bridge_has_no_trusted_web_attestation_verifier(self): self.assertTrue(True)\n"
                "    def test_registered_web_verifier_loads_pinned_external_runtime_host_cli(self): self.assertTrue(True)\n"
                "    def test_registered_web_verifier_exposes_pinned_host_submit_adapter(self): self.assertTrue(True)\n"
                "    def test_registered_web_verifier_rechecks_bundle_before_each_execution(self): self.assertTrue(True)\n"
                "    def test_malformed_registered_web_verifier_config_fails_closed_without_manual_fallback(self): self.assertTrue(True)\n"
                "    def test_browser_tab_receipt_cannot_recover_an_unverified_web_session(self): self.assertTrue(True)\n"
                "    def test_replace_web_session_bootstrap_rotates_target_and_manual_lease_without_host_attestation(self): self.assertTrue(True)\n"
                "    def test_manual_web_mutations_cannot_downgrade_host_attested_current_target(self): self.assertTrue(True)\n"
                "    def test_same_controller_web_recovery_cannot_replace_different_host_attested_current_target(self): self.assertTrue(True)\n"
                "    def test_legacy_quarantined_target_keeps_trusted_host_recovery_exit(self): self.assertTrue(True)\n"
                "    def test_legacy_quarantined_target_keeps_manual_replacement_exit(self): self.assertTrue(True)\n"
                "    def test_replace_web_session_rejects_unapproved_session_and_stale_generation(self): self.assertTrue(True)\n"
                "    def test_replace_same_web_target_is_idempotent_and_unbind_tombstones_without_losing_alias_history(self): self.assertTrue(True)\n"
                "    def test_same_controller_web_recovery_rotates_existing_resume_only_lease_to_new_verified_target(self): self.assertTrue(True)\n"
                "    def test_session_start_verified_target_rotates_existing_resume_lease_without_new_ownership_claim(self): self.assertTrue(True)\n"
                "class WebAutoStopSupervisorCoalescingTests(unittest.TestCase):\n"
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
                "class WebContinuationSupervisorBootstrapTests(unittest.TestCase):\n"
                "    def test_dead_or_untracked_active_supervisor_requires_bootstrap(self): self.assertTrue(True)\n"
                "    def test_live_active_supervisor_does_not_need_duplicate_bootstrap(self): self.assertTrue(True)\n"
                "    def test_identity_blocked_same_event_and_registry_are_not_bootstrapped_again(self): self.assertTrue(True)\n"
                "    def test_identity_blocked_event_retries_after_registry_changes(self): self.assertTrue(True)\n"
                "class WebLocalReentryIntegrationTests(unittest.TestCase):\n"
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
                "    def test_event_scope_guard_rejects_cross_task_work_without_project_wide_dispatch_proof(self): self.assertTrue(True)\n",
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
                "    def test_foreground_command_gate_allows_bounded_work_and_skips_unmanaged_sessions(self): self.assertTrue(True)\n",
                encoding="utf-8",
            )
            (tests_dir / "test_goal_display_sync.py").write_text(
                "import unittest\n"
                "class GoalDisplaySyncTests(unittest.TestCase):\n"
                "    def test_rolled_happy_path_records_exact_host_sequence_and_binding(self): self.assertTrue(True)\n"
                "    def test_successful_rolled_control_receipt_activates_display_sync_debt(self): self.assertTrue(True)\n"
                "    def test_title_failure_recovers_without_recreating_goal(self): self.assertTrue(True)\n"
                "    def test_host_readback_mismatch_retries_only_the_failed_read(self): self.assertTrue(True)\n"
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
            for name in ("web_agent_execution.py", "web_reentry_adapter.py", "terminal_continuation.py", "goal_display_sync.py"):
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
                "test('fresh legacy v1 assignment ACK cannot launch external provider', () => { assert.equal(1, 1); });\n",
                encoding="utf-8",
            )
            (tests_dir / "__init__.py").write_text("", encoding="utf-8")
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
                "    def test_no_canonical_work_does_not_reopen_after_observation_only_turn(self): self.assertTrue(True)\n",
                encoding="utf-8",
            )
            (tests_dir / "test_terminal_continuation.py").write_text(
                "import unittest\n"
                "class PendingTerminalReconcileTests(unittest.TestCase):\n"
                "    def test_reconcile_pending_discovers_canonical_receipts_without_receipt_cli_argument(self): self.assertTrue(True)\n"
                "    def test_reconcile_pending_is_idempotent_and_does_not_mutate_lifecycle_or_dispatch_wake(self): self.assertTrue(True)\n"
                "    def test_reconcile_pending_fails_closed_when_canonical_ownership_is_missing_or_mismatched(self): self.assertTrue(True)\n"
                "    def test_reconcile_pending_cli_has_no_receipt_argument_and_never_self_spawns(self): self.assertTrue(True)\n"
                "    def test_reconcile_pending_classifies_legacy_assignment_without_weakening_current_lease_checks(self): self.assertTrue(True)\n"
                "    def test_reconcile_pending_rejects_lifecycle_change_before_publish(self): self.assertTrue(True)\n"
                "    def test_reconcile_pending_rejects_target_generation_change_before_publish(self): self.assertTrue(True)\n"
                "    def test_reconcile_pending_hashes_same_bytes_it_parses(self): self.assertTrue(True)\n"
                "    def test_reconcile_pending_holds_lifecycle_and_registry_fences_through_audit(self): self.assertTrue(True)\n"
                "    def test_reconcile_pending_holds_runtime_assignment_fence_through_audit(self): self.assertTrue(True)\n"
                "    def test_reconcile_pending_fingerprint_is_order_independent(self): self.assertTrue(True)\n"
                "    def test_atomic_audit_writer_handles_concurrent_publication(self): self.assertTrue(True)\n"
                "class ManualControlCycleReconcileTests(unittest.TestCase):\n"
                "    def test_manual_fenced_control_cycle_reconcile_closes_only_reconciled_terminal_debt_without_verifying_web_identity(self): self.assertTrue(True)\n"
                "    def test_manual_fenced_control_cycle_reconcile_requires_unexpired_matching_manual_lease(self): self.assertTrue(True)\n"
                "    def test_manual_reconcile_requires_target_lineage_membership(self): self.assertTrue(True)\n"
                "    def test_manual_reconcile_skips_later_unrelated_allowed_receipt(self): self.assertTrue(True)\n"
                "    def test_reconcile_control_cycle_cli_accepts_no_receipt_or_web_session_identity_argument(self): self.assertTrue(True)\n"
                "    def test_manual_reconcile_rejects_forged_immutable_cycle_evidence(self): self.assertTrue(True)\n"
                "    def test_manual_reconcile_rejects_receipt_from_before_current_target_rotation(self): self.assertTrue(True)\n"
                "    def test_manual_reconcile_is_idempotent_after_durable_lifecycle_closure(self): self.assertTrue(True)\n"
                "    def test_manual_reconcile_rejects_non_terminal_debt_closed_cycle_even_with_matching_hash(self): self.assertTrue(True)\n"
                "    def test_manual_reconcile_closes_only_terminal_debt_and_preserves_current_nonterminal_triggers(self): self.assertTrue(True)\n",
                encoding="utf-8",
            )
            (tests_dir / "test_project_context_guard.py").write_text(
                "import unittest\n"
                "class ProjectContextGuardTests(unittest.TestCase):\n"
                "    def test_new_session_project_governance_question_requires_initialized_current_rules(self): self.assertTrue(True)\n"
                "    def test_existing_scoring_model_request_must_resolve_real_current_definition(self): self.assertTrue(True)\n"
                "    def test_source_change_before_stop_fails_closed_and_refreshes_for_same_turn_correction(self): self.assertTrue(True)\n"
                "    def test_not_found_unknown_token_does_not_authorize_fabricated_definitive_mechanism(self): self.assertTrue(True)\n"
                "    def test_runtime_state_creation_after_prompt_invalidates_fact_receipt_before_stop(self): self.assertTrue(True)\n"
                "    def test_nested_correction_refresh_preserves_full_applicable_agents_scope_chain(self): self.assertTrue(True)\n"
                "    def test_project_context_separates_unique_controller_from_unverified_web_session(self): self.assertTrue(True)\n"
                "    def test_project_context_reports_verified_bound_web_session_without_changing_ownership(self): self.assertTrue(True)\n",
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
                "    def test_live_e2e_rejects_confirmed_wake_that_predates_rule_ack(self): self.assertTrue(True)\n",
                encoding="utf-8",
            )
            (tests_dir / "test_controller_target_guard.py").write_text(
                "import unittest\n"
                "class ControllerTargetGuardTests(unittest.TestCase):\n"
                "    def test_identity_projection_keeps_unique_project_controller_when_session_id_unavailable(self): self.assertTrue(True)\n"
                "    def test_identity_projection_verifies_current_desktop_target_without_changing_controller_id(self): self.assertTrue(True)\n"
                "    def test_identity_projection_marks_old_target_stale_but_keeps_project_ownership(self): self.assertTrue(True)\n"
                "    def test_identity_projection_reports_project_controller_conflict_without_silent_selection(self): self.assertTrue(True)\n",
                encoding="utf-8",
            )
            (tests_dir / "test_web_lifecycle_bridge.py").write_text(
                "import unittest\n"
                "class WebLifecycleAuditTests(unittest.TestCase):\n"
                "    def test_rule_wake_target_resolution_fails_closed_instead_of_falling_back_to_logical_controller(self): self.assertTrue(True)\n"
                "    def test_rule_wake_rejects_explicit_target_without_canonical_execution_ownership(self): self.assertTrue(True)\n"
                "    def test_audit_once_rule_update_uses_guarded_scheduler_and_never_direct_scheduler(self): self.assertTrue(True)\n"
                "    def test_rule_wake_rejects_legacy_recovery_target_without_trusted_host_origin_proof(self): self.assertTrue(True)\n"
                "class WebLifecycleBridgeTests(unittest.TestCase):\n"
                "    def test_session_start_without_host_session_id_reports_existing_controller_not_new_controller(self): self.assertTrue(True)\n"
                "    def test_session_start_host_attested_recovery_restores_pending_control_loop_same_controller(self): self.assertTrue(True)\n"
                "    def test_same_controller_web_recovery_is_idempotent_after_user_reconfirms_ownership(self): self.assertTrue(True)\n"
                "    def test_web_recovery_preserves_desktop_target_and_only_advances_web_generation(self): self.assertTrue(True)\n"
                "    def test_replace_web_session_bootstrap_rotates_target_and_manual_lease_without_host_attestation(self): self.assertTrue(True)\n"
                "    def test_manual_web_mutations_cannot_downgrade_host_attested_current_target(self): self.assertTrue(True)\n"
                "    def test_same_controller_web_recovery_cannot_replace_different_host_attested_current_target(self): self.assertTrue(True)\n"
                "    def test_legacy_quarantined_target_keeps_trusted_host_recovery_exit(self): self.assertTrue(True)\n"
                "    def test_legacy_quarantined_target_keeps_manual_replacement_exit(self): self.assertTrue(True)\n"
                "    def test_replace_web_session_rejects_unapproved_session_and_stale_generation(self): self.assertTrue(True)\n"
                "    def test_replace_same_web_target_is_idempotent_and_unbind_tombstones_without_losing_alias_history(self): self.assertTrue(True)\n"
                "class WebAutoStopSupervisorCoalescingTests(unittest.TestCase):\n"
                "    def test_same_receipt_live_supervisor_is_coalesced(self): self.assertTrue(True)\n"
                "    def test_current_token_web_rearm_hands_off_with_force_rearm_proof(self): self.assertTrue(True)\n"
                "    def test_stale_supervisor_token_exits_without_running_impl(self): self.assertTrue(True)\n"
                "class WebContinuationSupervisorBootstrapTests(unittest.TestCase):\n"
                "    def test_dead_or_untracked_active_supervisor_requires_bootstrap(self): self.assertTrue(True)\n"
                "    def test_live_active_supervisor_does_not_need_duplicate_bootstrap(self): self.assertTrue(True)\n",
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
                "    def test_event_scope_guard_rejects_cross_task_work_without_project_wide_dispatch_proof(self): self.assertTrue(True)\n",
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
                "    def test_foreground_command_gate_allows_bounded_work_and_skips_unmanaged_sessions(self): self.assertTrue(True)\n",
                encoding="utf-8",
            )
            (tests_dir / "test_goal_display_sync.py").write_text(
                "import unittest\n"
                "class GoalDisplaySyncTests(unittest.TestCase):\n"
                "    def test_rolled_happy_path_records_exact_host_sequence_and_binding(self): self.assertTrue(True)\n"
                "    def test_successful_rolled_control_receipt_activates_display_sync_debt(self): self.assertTrue(True)\n"
                "    def test_title_failure_recovers_without_recreating_goal(self): self.assertTrue(True)\n"
                "    def test_host_readback_mismatch_retries_only_the_failed_read(self): self.assertTrue(True)\n"
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
    def test_project_context_hooks_are_installed_before_lifecycle_and_scoring(self):
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
                self.assertIn("project_context_guard.py", str(entries[0]))
            self.assertLess(
                next(i for i,x in enumerate(value["hooks"]["UserPromptSubmit"]) if "project_context_guard.py" in str(x)),
                next(i for i,x in enumerate(value["hooks"]["UserPromptSubmit"]) if "controller_scoring_hook.py" in str(x)),
            )
            self.assertEqual(
                "startup|resume|clear|compact",
                value["hooks"]["SessionStart"][0]["matcher"],
            )
            self.assertEqual(
                0,
                value["hooks"]["UserPromptSubmit"][0]["hooks"][0]["additionalContextLimit"],
            )


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
