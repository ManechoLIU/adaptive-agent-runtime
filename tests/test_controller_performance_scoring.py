from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCORING = ROOT / "references" / "controller-performance-scoring.md"


class ControllerPerformanceScoringContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = SCORING.read_text(encoding="utf-8")

    def test_manual_wakeup_and_autonomous_continuation_are_explicit_scoring_signals(self) -> None:
        self.assertIn("人工唤醒", self.text)
        self.assertIn("自主续作", self.text)
        self.assertIn("用户实质接管派发 / 恢复 / 验收", self.text)
        self.assertIn("总分最高 `69`", self.text)
        self.assertIn("总分最高 `59`", self.text)

    def test_visual_baseline_failure_is_a_quality_gate_failure_not_an_ordinary_ui_bug(self) -> None:
        self.assertIn("视觉基线失守", self.text)
        self.assertIn("绑定参考图", self.text)
        self.assertIn("目标端规范", self.text)
        self.assertIn("撤回", self.text)
        self.assertIn("质量门失效", self.text)

    def test_historical_scoring_anchors_to_controller_tenure_boundary(self) -> None:
        self.assertIn("任期起点", self.text)
        self.assertIn("接管 / 换控", self.text)
        self.assertIn("不得把前任", self.text)
        self.assertIn("第一笔漂亮交付", self.text)

    def test_standard_output_requires_autonomy_and_visual_gate_findings(self) -> None:
        self.assertIn("人工介入 / 自主续作结论", self.text)
        self.assertIn("视觉基线核对结论", self.text)


if __name__ == "__main__":
    unittest.main()
