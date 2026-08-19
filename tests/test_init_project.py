from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from tempfile import TemporaryDirectory
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = spec_from_file_location("adaptive_delivery_init", ROOT / "scripts" / "init_project.py")
assert SPEC is not None and SPEC.loader is not None
MODULE = module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class InitProjectTests(unittest.TestCase):
    def test_collaborative_profile_creates_exactly_six_documents(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            report = MODULE.initialize_project(root)

            self.assertEqual(
                set(report.created),
                {"AGENTS.md", "PROJECT_STATUS.md", "SPEC.md", "DESIGN.md", "TECHNICAL.md", "EVOLUTION.md"},
            )
            self.assertEqual(report.skipped, ())
            self.assertEqual({path.name for path in root.iterdir()}, set(report.created))

    def test_core_profile_creates_exactly_four_documents(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            report = MODULE.initialize_project(root, profile="core")

            self.assertEqual(set(report.created), {"SPEC.md", "DESIGN.md", "TECHNICAL.md", "EVOLUTION.md"})
            self.assertEqual({path.name for path in root.iterdir()}, set(report.created))

    def test_without_design_removes_design_from_selected_profile(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            report = MODULE.initialize_project(root, include_design=False)

            self.assertNotIn("DESIGN.md", report.created)
            self.assertFalse((root / "DESIGN.md").exists())

    def test_existing_file_is_skipped_byte_for_byte(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            existing = root / "SPEC.md"
            existing.write_bytes(b"user-owned\x00content\n")

            report = MODULE.initialize_project(root)

            self.assertEqual(existing.read_bytes(), b"user-owned\x00content\n")
            self.assertIn("SPEC.md", report.skipped)
            self.assertNotIn("SPEC.md", report.created)

    def test_dangling_symlink_is_rejected_without_writing_outside_root(self):
        with TemporaryDirectory() as directory, TemporaryDirectory() as outside_directory:
            root = Path(directory)
            outside = Path(outside_directory) / "escaped.md"
            (root / "SPEC.md").symlink_to(outside)

            with self.assertRaisesRegex(ValueError, "symbolic link"):
                MODULE.initialize_project(root)

            self.assertFalse(outside.exists())

    def test_existing_directory_at_document_path_is_rejected(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "DESIGN.md").mkdir()

            with self.assertRaisesRegex(ValueError, "regular file"):
                MODULE.initialize_project(root)

    def test_second_run_is_idempotent(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first = MODULE.initialize_project(root)
            second = MODULE.initialize_project(root)

            self.assertEqual(len(first.created), 6)
            self.assertEqual(second.created, ())
            self.assertEqual(set(second.skipped), set(first.created))

    def test_invalid_root_fails_without_creating_it(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "missing"

            with self.assertRaisesRegex(ValueError, "project root"):
                MODULE.initialize_project(root)

            self.assertFalse(root.exists())

    def test_unknown_profile_is_rejected(self):
        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "unknown profile"):
                MODULE.initialize_project(Path(directory), profile="enterprise")

    def test_templates_preserve_status_and_visual_governance(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            MODULE.initialize_project(root)

            status = (root / "PROJECT_STATUS.md").read_text(encoding="utf-8")
            design = (root / "DESIGN.md").read_text(encoding="utf-8")
            agents = (root / "AGENTS.md").read_text(encoding="utf-8")

            self.assertIn("替换", status)
            self.assertIn("不追加", status)
            for heading in ("参考图", "采用点", "不得照搬", "状态"):
                self.assertIn(heading, design)
            self.assertIn("不共享旧聊天", agents)


if __name__ == "__main__":
    unittest.main()
