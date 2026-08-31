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
            )

        self.assertEqual(report["core"]["status"], "enabled")
        self.assertEqual(report["desktop_adapter"]["status"], "degraded")
        self.assertIn("trust", report["desktop_adapter"]["reason"])
        self.assertEqual(report["web_local_adapter"]["status"], "degraded")
        self.assertEqual(report["web_local_adapter"]["mode"], "pure_web_file")

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
            )

        self.assertEqual(report["core"]["status"], "enabled")
        self.assertEqual(report["desktop_adapter"]["status"], "blocked")
        self.assertEqual(report["web_local_adapter"]["status"], "enabled")
        self.assertEqual(report["web_local_adapter"]["adapter"], "ai-bridge")

class InstallMigrationContractTests(unittest.TestCase):
    def make_source(self, root: Path) -> Path:
        import subprocess
        source = root / "source"
        source.mkdir()
        subprocess.run(["git", "-C", str(source), "init", "-b", "main"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(source), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(source), "config", "user.name", "Test"], check=True)
        (source / "SKILL.md").write_text("---\nname: adaptive-delivery\n---\n# Adaptive Agent Runtime\n", encoding="utf-8")
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
