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
                {"AGENTS.md", "TASK_LEDGER.md", "SPEC.md", "DESIGN.md", "TECHNICAL.md", "EVOLUTION.md"},
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

            status = (root / "TASK_LEDGER.md").read_text(encoding="utf-8")
            design = (root / "DESIGN.md").read_text(encoding="utf-8")
            agents = (root / "AGENTS.md").read_text(encoding="utf-8")

            self.assertIn("替换", status)
            self.assertIn("不追加", status)
            for heading in ("参考图", "采用点", "不得照搬", "状态"):
                self.assertIn(heading, design)
            self.assertIn("不共享旧聊天", agents)

    def test_durable_profile_adds_memory_wiki_and_knowledge_workspace(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            report = MODULE.initialize_project(root, profile="durable")

            self.assertEqual(len(report.created), 9)
            self.assertTrue((root / "MEMORY.md").is_file())
            self.assertTrue((root / "WIKI_INDEX.md").is_file())
            self.assertTrue((root / "SKILL.md").is_file())
            self.assertEqual(
                set(report.created_directories),
                {
                    "raw_sources",
                    "wiki",
                    "logs",
                    "logs/ingestion",
                },
            )
            for relative_path in report.created_directories:
                self.assertTrue((root / relative_path).is_dir())

    def test_durable_profile_installs_the_project_workflow_template_as_skill_md(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = ROOT / "assets" / "templates" / "PROJECT_SKILL.md"

            MODULE.initialize_project(root, profile="durable")

            self.assertTrue(source.is_file())
            self.assertEqual((root / "SKILL.md").read_bytes(), source.read_bytes())

    def test_task_ledger_template_contains_runtime_state_not_methodology(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            MODULE.initialize_project(root, profile="durable")

            ledger = (root / "TASK_LEDGER.md").read_text(encoding="utf-8")

            self.assertNotIn("## 使用规则", ledger)
            self.assertNotIn("阶段 / 粒度", ledger)
            self.assertNotIn("动态混合粒度", ledger)
            for field in (
                "当前目标",
                "任务拆分",
                "状态",
                "阻塞",
                "下一步",
                "验收",
                "证据",
            ):
                self.assertIn(field, ledger)

    def test_durable_profile_is_idempotent_for_documents_and_directories(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first = MODULE.initialize_project(root, profile="durable")
            second = MODULE.initialize_project(root, profile="durable")

            self.assertEqual(len(first.created), 9)
            self.assertEqual(len(first.created_directories), 4)
            self.assertEqual(second.created, ())
            self.assertEqual(second.created_directories, ())
            self.assertEqual(set(second.skipped), set(first.created))
            self.assertEqual(
                set(second.skipped_directories), set(first.created_directories)
            )

    def test_durable_directory_type_conflict_fails_before_writing(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "raw_sources").write_text("user file\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "must be a directory"):
                MODULE.initialize_project(root, profile="durable")

            self.assertEqual(
                {path.name for path in root.iterdir()}, {"raw_sources"}
            )

    def test_existing_project_status_is_the_legacy_ledger_alias(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "PROJECT_STATUS.md"
            legacy.write_text("# user-owned ledger\n", encoding="utf-8")

            report = MODULE.initialize_project(root, profile="durable")

            self.assertFalse((root / "TASK_LEDGER.md").exists())
            self.assertEqual(legacy.read_text(encoding="utf-8"), "# user-owned ledger\n")
            self.assertIn(
                "TASK_LEDGER.md (using existing PROJECT_STATUS.md)", report.skipped
            )

    def test_two_existing_ledgers_are_rejected(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "TASK_LEDGER.md").write_text("# new ledger\n", encoding="utf-8")
            (root / "PROJECT_STATUS.md").write_text("# legacy ledger\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "both task ledgers exist"):
                MODULE.initialize_project(root, profile="durable")


if __name__ == "__main__":
    unittest.main()
