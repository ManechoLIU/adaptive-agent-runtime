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


if __name__ == "__main__":
    unittest.main()
