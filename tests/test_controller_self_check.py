import tempfile
import unittest
from pathlib import Path

from scripts.controller_self_check import render_controller_self_check

ROOT = Path(__file__).resolve().parents[1]


class ControllerSelfCheckTests(unittest.TestCase):
    def test_self_check_is_derived_from_exact_model_without_numeric_scores(self):
        rendered = render_controller_self_check(ROOT)
        for dimension in (
            "有效成果与 Goal 推进",
            "任务拆解与边界设计",
            "关键路径与优先级",
            "调度与执行效率",
            "质量、验收与证据",
            "异常恢复与任务流转",
            "控制面一致性与可审计性",
        ):
            self.assertIn(dimension, rendered)
        self.assertIn("自主续作", rendered)
        self.assertIn("责任归因", rendered)
        self.assertNotIn("25%", rendered)
        self.assertNotIn("15%", rendered)
        self.assertNotIn("10%", rendered)
        self.assertNotIn("总分", rendered)
        self.assertNotIn("当前得分", rendered)

    def test_self_check_fails_closed_when_formal_model_shape_is_incomplete(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            target = root / "references"
            target.mkdir()
            source = (ROOT / "references" / "controller-performance-scoring.md").read_text(encoding="utf-8")
            source = source.replace("| 异常恢复与任务流转 | 10% |", "| OTHER | 10% |", 1)
            (target / "controller-performance-scoring.md").write_text(source, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "seven scoring dimensions"):
                render_controller_self_check(root)
