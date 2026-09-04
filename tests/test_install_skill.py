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
            "web_agent_health_supervisor.py", "web_agent_events.py", "route_contract.py",
            "control_event_guard.py", "controller_state.py", "assignment_lease_guard.py",
        ):
            script = source / "scripts" / name
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
            for name in ("web_agent_execution.py", "web_reentry_adapter.py"):
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
            (tests_dir / "test_web_reentry_adapter.py").write_text(
                "import unittest\n"
                "class WebReentryContinuationRegressionTests(unittest.TestCase):\n"
                "    def test_transient_web_reentry_failure_rearms_existing_continuation_supervisor(self):\n"
                "        self.assertTrue(True)\n",
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
                "    def test_direct_cycle_persistence_cannot_fabricate_generic_correction_closure(self): self.assertTrue(True)\n",
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
            for name in ("web_agent_execution.py", "web_reentry_adapter.py"):
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
                "    def test_direct_cycle_persistence_cannot_fabricate_generic_correction_closure(self): self.assertTrue(True)\n",
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
            self.assertEqual(set(manifest["capabilities"]), {"core", "desktop_adapter", "web_local_adapter", "web_agent_execution"})
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
            for name in ("web_lifecycle_bridge.py", "lifecycle_hook.py", "controller_scoring_hook.py"):
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
    def test_health_service_plist_is_keepalive_and_runs_installed_health_only_supervisor(self):
        import plistlib
        from scripts.install_skill import install_web_agent_health_service_plist
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            target = root / "adaptive-delivery"
            (target / "scripts").mkdir(parents=True)
            script = target / "scripts" / "web_agent_health_supervisor.py"
            script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            plist = root / "LaunchAgents" / "web-agent-health.plist"
            install_web_agent_health_service_plist(
                plist, target, python_executable="/usr/bin/python3",
                registry_path=root / "controllers.json",
            )
            payload = plistlib.loads(plist.read_bytes())
        self.assertTrue(payload["RunAtLoad"])
        self.assertTrue(payload["KeepAlive"])
        self.assertIn(str(script.resolve()), payload["ProgramArguments"])
        self.assertIn("--registry", payload["ProgramArguments"])
        self.assertNotIn("web_reentry_adapter.py", " ".join(payload["ProgramArguments"]))

    def test_web_agent_execution_capability_requires_matching_health_service(self):
        from scripts.install_skill import (
            detect_host_capabilities, install_web_agent_health_service_plist,
        )
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            target = root / "adaptive-delivery"
            (target / "scripts").mkdir(parents=True)
            script = target / "scripts" / "web_agent_health_supervisor.py"
            script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            plist = root / "health.plist"
            before = detect_host_capabilities(
                skill_root=target,
                ai_bridge_executable=root / "missing-bridge",
                hooks_file=root / "hooks.json", zshenv_file=root / ".zshenv",
                health_service_plist=plist,
            )
            install_web_agent_health_service_plist(
                plist, target, python_executable="/usr/bin/python3",
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
            script = target / "scripts" / "web_agent_health_supervisor.py"
            script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            plist = root / "health.plist"
            source_receipt = root / "event-source.json"
            install_web_agent_health_service_plist(
                plist, target, python_executable="/usr/bin/python3",
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
            (target / "scripts" / "web_agent_health_supervisor.py").write_text(
                "#!/usr/bin/env python3\n", encoding="utf-8"
            )
            plist = root / "LaunchAgents" / "health.plist"
            loaded = []
            report = configure_runtime_services(
                target,
                health_service_plist=plist,
                registry_path=root / "controllers.json",
                python_executable="/usr/bin/python3",
                service_loader=lambda path: loaded.append(path) or {"state": "loaded"},
            )
        self.assertEqual(loaded, [plist.resolve()])
        self.assertEqual(report["state"], "loaded")
        self.assertTrue(report["configured"])
