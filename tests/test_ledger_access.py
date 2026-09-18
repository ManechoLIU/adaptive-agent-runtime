from __future__ import annotations

import hashlib
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {relative_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


lint_governance = load_module("lint_governance", "scripts/lint_governance.py")
ledger_consistency_guard = load_module(
    "ledger_consistency_guard", "scripts/ledger_consistency_guard.py"
)
controller_state = load_module("controller_state", "scripts/controller_state.py")
ledger_access = load_module("ledger_access", "scripts/ledger_access.py")


VALID_LEDGER = """# 任务台账

## 当前目标

- 当前 Goal：F1 交付最小台账读写闭环
- 下一可见检查点：F1 结构化投影可查询
- 当前阻塞：无
- 规则版本：runtime-rev-1

## 任务拆分

| ID | 状态 / 负责人 | 目标与边界 | 依赖 / 阻塞 | 验收与验证 | 证据 / 下一步 |
| --- | --- | --- | --- | --- | --- |
| F1 | READY / 主 Agent | 提供结构化读取与防旧写 | 无 | inspect 输出稳定 JSON；stale write 被拒绝 | tests；完成实现 |
| F2 | BLOCKED / Reviewer | 独立复核 | 依赖 F1 | F1 完成后复核 | 等待 F1 |
"""


class LedgerAccessTests(unittest.TestCase):
    def write_ledger(self, root: Path, text: str = VALID_LEDGER) -> Path:
        ledger = root / "TASK_LEDGER.md"
        ledger.write_text(text, encoding="utf-8")
        return ledger

    def test_projection_reuses_canonical_parser_and_exposes_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = self.write_ledger(Path(directory))
            projection = ledger_access.project_ledger(ledger)

            expected_sha = hashlib.sha256(ledger.read_bytes()).hexdigest()
            self.assertEqual(projection["schema_version"], 1)
            self.assertEqual(projection["ledger_sha256"], expected_sha)
            self.assertEqual(projection["ledger_revision"], expected_sha)
            self.assertTrue(projection["valid"])
            self.assertEqual(projection["current_goal"], "F1 交付最小台账读写闭环")
            self.assertEqual(projection["ready_ids"], ["F1"])
            self.assertEqual(projection["open_ids"], ["F1", "F2"])
            self.assertEqual(projection["runnable_ids"], ["F1"])

            work = {item["id"]: item for item in projection["work_items"]}
            self.assertEqual(work["F1"]["status"], "READY")
            self.assertEqual(work["F1"]["owner"], "主 Agent")
            self.assertEqual(work["F1"]["scope"], "提供结构化读取与防旧写")
            self.assertEqual(work["F1"]["dependencies_blockers"], "无")
            self.assertEqual(
                work["F1"]["acceptance"], "inspect 输出稳定 JSON；stale write 被拒绝"
            )
            self.assertEqual(work["F1"]["evidence"], "tests；完成实现")
            self.assertEqual(work["F1"]["next_action"], "tests；完成实现")

    def test_guarded_apply_updates_only_from_exact_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = self.write_ledger(Path(directory))
            expected_sha = ledger_access.ledger_sha256(ledger)
            replacement = VALID_LEDGER.replace("READY / 主 Agent", "ACTIVE / 主 Agent")

            receipt = ledger_access.apply_ledger(
                ledger,
                expected_sha256=expected_sha,
                replacement_text=replacement,
            )

            self.assertEqual(receipt["previous_sha256"], expected_sha)
            self.assertNotEqual(receipt["ledger_sha256"], expected_sha)
            self.assertIn("ACTIVE / 主 Agent", ledger.read_text(encoding="utf-8"))

    def test_guarded_apply_rejects_stale_revision_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = self.write_ledger(Path(directory))
            stale_sha = ledger_access.ledger_sha256(ledger)
            newer = VALID_LEDGER.replace("runtime-rev-1", "runtime-rev-2")
            ledger.write_text(newer, encoding="utf-8")
            before = ledger.read_bytes()

            replacement = newer.replace("READY / 主 Agent", "ACTIVE / 主 Agent")
            with self.assertRaisesRegex(ledger_access.StaleLedgerError, "stale ledger"):
                ledger_access.apply_ledger(
                    ledger,
                    expected_sha256=stale_sha,
                    replacement_text=replacement,
                )

            self.assertEqual(ledger.read_bytes(), before)

    def test_guarded_apply_rejects_invalid_replacement_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = self.write_ledger(Path(directory))
            expected_sha = ledger_access.ledger_sha256(ledger)
            before = ledger.read_bytes()
            invalid = VALID_LEDGER.replace("- 当前 Goal：F1 交付最小台账读写闭环\n", "")

            with self.assertRaisesRegex(ValueError, "replacement ledger is invalid"):
                ledger_access.apply_ledger(
                    ledger,
                    expected_sha256=expected_sha,
                    replacement_text=invalid,
                )

            self.assertEqual(ledger.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
