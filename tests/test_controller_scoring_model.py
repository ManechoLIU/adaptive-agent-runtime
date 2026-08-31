import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODEL = ROOT / "references" / "controller-performance-scoring.md"


class ControllerScoringWindowContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = MODEL.read_text(encoding="utf-8")

    def test_default_window_is_24_hours_with_at_most_five_valid_events(self):
        self.assertIn("最近 24 小时", self.text)
        self.assertRegex(self.text, r"最近\s*5\s*个有效控制事件")
        self.assertNotIn("最近 2 个已经形成明确状态收口的控制事件", self.text)

    def test_more_than_five_events_must_select_the_five_most_recent_not_cherry_pick(self):
        self.assertIn("超过 5 个时取时间上最近的 5 个", self.text)
        self.assertIn("不得在 24 小时内挑选更漂亮的 5 个", self.text)

    def test_unresolved_major_anomalies_are_separate_and_not_double_counted(self):
        self.assertIn("当前所有重大未闭环异常", self.text)
        self.assertIn("不得作为额外控制事件重复计分", self.text)

    def test_sparse_24h_sample_must_be_disclosed_not_backfilled_for_convenience(self):
        self.assertIn("样本不足", self.text)
        self.assertIn("不得为了凑满 5 个事件", self.text)

    def test_self_check_does_not_run_formal_window_or_expose_live_score(self):
        self.assertIn("Self-Check", self.text)
        self.assertIn("不得执行完整的 24 小时 / 5 事件正式评分", self.text)
        self.assertIn("不得暴露实时总分", self.text)

    def test_raw_dimension_scores_are_explicitly_zero_to_one_hundred(self):
        self.assertIn("原始分统一采用 0–100 分制", self.text)
        self.assertIn("加权分 = 原始分 × 权重", self.text)
        self.assertIn("96 × 25% = 24.00", self.text)
        self.assertIn("评分维度 | 权重 | 原始分 | 加权分 | 主要判断", self.text)
        self.assertIn("不得改用 10 分制", self.text)

    def test_single_cycle_diagnostics_reuse_rubric_without_changing_formal_score(self):
        self.assertIn("单回合诊断", self.text)
        self.assertIn("最佳闭环回合", self.text)
        self.assertIn("最差闭环回合", self.text)
        self.assertIn("不得反向改变 24 小时正式总分", self.text)
        self.assertIn("尚未终结", self.text)
        self.assertIn("不得进入最佳 / 最差排名", self.text)
        self.assertIn("同一评分模型", self.text)


if __name__ == "__main__":
    unittest.main()
