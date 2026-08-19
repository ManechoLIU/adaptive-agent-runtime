from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = ROOT / "SKILL.md"
REFERENCE_PATHS = {
    "methods": ROOT / "references" / "methods.md",
    "harness": ROOT / "references" / "harness-and-release.md",
    "visual": ROOT / "references" / "visual-reference-governance.md",
    "experience": ROOT / "references" / "experience-catalog.md",
}
METHODS = REFERENCE_PATHS["methods"]


def read_entrypoint() -> str:
    return ENTRYPOINT.read_text(encoding="utf-8")


def split_frontmatter(markdown: str) -> tuple[str, str]:
    match = re.match(r"\A---\s*\n(.*?)\n---\s*\n(.*)\Z", markdown, re.DOTALL)
    if not match:
        raise AssertionError("SKILL.md must start with YAML frontmatter")
    return match.group(1), match.group(2)


class SkillStructureTests(unittest.TestCase):
    def test_required_skill_files_exist(self):
        missing = [str(path.relative_to(ROOT)) for path in [ENTRYPOINT, *REFERENCE_PATHS.values()] if not path.is_file()]
        self.assertEqual([], missing, f"required Skill files are missing: {missing}")

    def test_entrypoint_frontmatter_has_trigger_only_metadata(self):
        frontmatter, _ = split_frontmatter(read_entrypoint())
        fields = dict(re.findall(r"(?m)^([A-Za-z][\w-]*):\s*(.+?)\s*$", frontmatter))

        self.assertEqual("adaptive-delivery", fields.get("name"))
        description = fields.get("description", "").strip().strip('"')
        self.assertRegex(description, r"^Use when\b")
        self.assertLessEqual(len(frontmatter), 1024)
        self.assertLessEqual(len(description), 500)

    def test_entrypoint_is_short(self):
        _, body = split_frontmatter(read_entrypoint())
        self.assertLessEqual(len(body.split()), 350)

    def test_core_runtime_instructions_are_vendor_neutral(self):
        core = "\n".join(
            [read_entrypoint(), *[path.read_text(encoding="utf-8") for path in REFERENCE_PATHS.values()]]
        ).casefold()
        vendor_names = ("codex", "chatgpt", "claude code", "gemini cli", "cursor")

        self.assertEqual([], [name for name in vendor_names if name in core])

    def test_four_local_reference_links_resolve(self):
        _, body = split_frontmatter(read_entrypoint())
        links = set(re.findall(r"\[[^\]]+\]\((references/[^)\s]+)\)", body))
        expected = {f"references/{path.name}" for path in REFERENCE_PATHS.values()}

        self.assertEqual(expected, links)
        self.assertTrue(all((ROOT / link).is_file() for link in links))

    def test_entrypoint_excludes_low_scope_work_from_full_orchestration(self):
        _, body = split_frontmatter(read_entrypoint())
        normalized = body.casefold()

        self.assertRegex(normalized, r"普通问答|ordinary\s+questions?|q\s*&\s*a")
        self.assertRegex(normalized, r"单文件[^\n]{0,20}(简单|小修)|single[- ]file[^\n]{0,20}simple")
        self.assertRegex(normalized, r"普通(?:代码)?审查|ordinary\s+(?:code\s+)?reviews?")

    def test_development_kickoff_has_authority_and_collaboration_contract(self):
        entrypoint = read_entrypoint()
        methods = METHODS.read_text(encoding="utf-8")

        for phrase in ("只制定计划", "开始开发", "继续开发"):
            self.assertIn(phrase, entrypoint)
        for field in (
            "里程碑",
            "验收",
            "工作包",
            "依赖",
            "负责人",
            "文件所有权",
            "停止条件",
            "主 Agent",
            "PROJECT_STATUS.md",
        ):
            self.assertIn(field, methods)


if __name__ == "__main__":
    unittest.main()
