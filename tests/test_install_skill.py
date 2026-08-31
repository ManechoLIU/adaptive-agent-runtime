import tempfile
import unittest
from pathlib import Path

from scripts.install_skill import detect_host_capabilities


class InstallCapabilityTests(unittest.TestCase):
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
    def make_source(self, root: Path) -> Path:
        import subprocess
        source = root / "source"
        source.mkdir()
        subprocess.run(["git", "-C", str(source), "init", "-b", "main"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(source), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(source), "config", "user.name", "Test"], check=True)
        (source / "SKILL.md").write_text("---\nname: adaptive-delivery\n---\n# Adaptive Agent Runtime\n", encoding="utf-8")
        (source / "scripts").mkdir()
        for name in ("web_lifecycle_bridge.py", "lifecycle_hook.py", "controller_scoring_hook.py"):
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
            self.assertEqual(manifest["skill_id"], "adaptive-delivery")
            self.assertEqual(manifest["previous_revision"], old_revision)
            self.assertIn("capabilities", manifest)
            self.assertEqual(manifest["capabilities"]["core"]["status"], "enabled")
            self.assertFalse((target.parent / "adaptive-agent-runtime").exists())

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
            self.assertEqual(manifest["skill_id"], "adaptive-delivery")
            self.assertIn("capabilities", manifest)
            self.assertEqual(set(manifest["capabilities"]), {"core", "desktop_adapter", "web_local_adapter"})


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
            for event in ("SessionStart", "PostToolUse", "SubagentStop", "UserPromptSubmit"):
                self.assertIn(event, config["hooks"])
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
            self.assertIn("trust", report["desktop_adapter"]["reason"])
            self.assertEqual(report["web_local_adapter"]["status"], "enabled")
            self.assertTrue(hooks.is_file())
            self.assertTrue(zshenv.is_file())
    def test_install_cli_persists_degraded_capabilities_when_adapter_configuration_partially_fails(self):
        import contextlib
        import io
        import json
        from unittest.mock import patch
        from scripts.install_skill import MANIFEST_NAME, main
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = InstallMigrationContractTests().make_source(root)
            target = root / "installed" / "adaptive-delivery"
            codex = root / "codex"; codex.write_text("#!/bin/sh\n", encoding="utf-8"); codex.chmod(0o755)
            bridge = root / "ai-bridge"; bridge.write_text("#!/bin/sh\n", encoding="utf-8"); bridge.chmod(0o755)
            hooks = root / "hooks.json"
            zshenv = root / ".zshenv"
            output = io.StringIO()
            with patch("scripts.install_skill.configure_host_adapters", side_effect=OSError("zshenv write failed")):
                with contextlib.redirect_stdout(output):
                    code = main([
                        "--source", str(source), "--target", str(target),
                        "--summary", "partial adapter failure", "--impact", "none",
                        "--stop-condition", "ready", "--codex", str(codex),
                        "--ai-bridge", str(bridge), "--hooks-file", str(hooks),
                        "--zshenv-file", str(zshenv),
                    ])
            payload = json.loads(output.getvalue().splitlines()[-1])
            persisted = json.loads((target / MANIFEST_NAME).read_text(encoding="utf-8"))

        self.assertEqual(code, 0)
        self.assertEqual(persisted["capabilities"], payload["capabilities"])

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
                    "--stop-condition", "ready", "--codex", str(codex),
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
