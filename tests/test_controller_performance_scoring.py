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

    def test_scoring_requires_explicit_responsibility_attribution(self) -> None:
        for marker in ("controller-caused", "governance-caused", "external-caused", "mixed"):
            self.assertIn(marker, self.text)
        self.assertIn("责任归因", self.text)
        self.assertIn("不得把治理基础设施导致的停滞直接记为总控失职", self.text)

    def test_difficult_environment_is_not_an_automatic_bonus(self) -> None:
        self.assertIn("困难环境本身不加分", self.text)
        self.assertIn("不得设置困难系数", self.text)

    def test_adaptation_credit_requires_verifiable_autonomous_recovery(self) -> None:
        self.assertIn("适应能力", self.text)
        self.assertIn("主动 reconcile", self.text)
        self.assertIn("不需要用户实质接管", self.text)
        self.assertIn("异常恢复与任务流转", self.text)
        self.assertIn("调度与执行效率", self.text)
        self.assertIn("控制面一致性与可审计性", self.text)

    def test_historical_score_comparison_fails_closed_without_verifiable_previous_score(self) -> None:
        self.assertIn("最近一次有效评分", self.text)
        self.assertIn("不得根据模型记忆", self.text)
        self.assertIn("UNKNOWN", self.text)
        self.assertIn("上升 / 下降", self.text)

    def test_standard_output_requires_attribution_conclusion(self) -> None:
        self.assertIn("责任归因结论", self.text)


if __name__ == "__main__":
    unittest.main()
