from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(name, SKILL_ROOT / relative_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {relative_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


init_project = load_module("init_project", "scripts/init_project.py")
lint_governance = load_module("lint_governance", "scripts/lint_governance.py")
preblock_guard = load_module("preblock_guard", "scripts/preblock_guard.py")
control_event_guard = load_module(
    "control_event_guard", "scripts/control_event_guard.py"
)


class GovernanceTests(unittest.TestCase):
    def test_control_event_guard_requires_every_ready_decision(self) -> None:
        snapshot = {
            "ledger_sha256": "abc123",
            "available_slots": 1,
            "ready_packages": [
                {
                    "id": "F1",
                    "decision": "active",
                    "task_id": "task-1",
                    "delivered_ack": True,
                }
            ],
        }

        errors = control_event_guard.validate_snapshot(
            snapshot, ledger_ready_ids={"F1", "F2"}
        )

        self.assertIn("control event omitted READY packages: F2", errors)

    def test_control_event_guard_checks_reviewer_and_rule_acks(self) -> None:
        snapshot = {
            "ledger_sha256": "abc123",
            "available_slots": 0,
            "ready_packages": [],
            "required_reviews": [
                {"id": "R1", "task_id": "review-1", "delivered_ack": False}
            ],
            "rule_update": {
                "revision": "def456",
                "affected_tasks": ["writer-1", "writer-2"],
                "acknowledged_tasks": ["writer-1"],
            },
        }

        errors = control_event_guard.validate_snapshot(
            snapshot,
            ledger_ready_ids=set(),
            required_review_ids={"R1"},
            expected_rule_revision="def456",
            affected_task_ids={"writer-1", "writer-2"},
        )

        self.assertIn("required review R1 requires delivered_ack=true", errors)
        self.assertIn("rule update missing loaded ACK: writer-2", errors)

    def test_control_event_guard_allows_complete_event(self) -> None:
        snapshot = {
            "ledger_sha256": "abc123",
            "available_slots": 1,
            "ready_packages": [
                {
                    "id": "F1",
                    "decision": "active",
                    "task_id": "task-1",
                    "delivered_ack": True,
                },
                {
                    "id": "F2",
                    "decision": "deferred",
                    "reason": "shares the same output directory as F1",
                },
            ],
            "required_reviews": [
                {"id": "R1", "task_id": "review-1", "delivered_ack": True}
            ],
            "rule_update": {
                "revision": "def456",
                "affected_tasks": ["writer-1"],
                "acknowledged_tasks": ["writer-1"],
            },
        }

        self.assertEqual(
            control_event_guard.validate_snapshot(
                snapshot,
                ledger_ready_ids={"F1", "F2"},
                expected_ledger_sha256="abc123",
                required_review_ids={"R1"},
                expected_rule_revision="def456",
                affected_task_ids={"writer-1"},
            ),
            [],
        )

    def test_control_event_guard_rejects_stale_ledger_and_omitted_expectations(self) -> None:
        snapshot = {
            "ledger_sha256": "stale",
            "available_slots": 0,
            "ready_packages": [],
        }

        errors = control_event_guard.validate_snapshot(
            snapshot,
            ledger_ready_ids=set(),
            expected_ledger_sha256="current",
            required_review_ids={"UX-EARLY"},
            expected_rule_revision="rule-2",
            affected_task_ids={"writer-1"},
        )

        self.assertIn("ledger_sha256 does not match the current ledger", errors)
        self.assertIn("control event omitted required reviews: UX-EARLY", errors)
        self.assertIn("control event omitted the declared rule update", errors)

    def test_preblock_guard_rejects_ready_work_outside_current_goal(self) -> None:
        snapshot = {
            "project_scope_scan": True,
            "ledger_revision": "abc123",
            "open_packages": [
                {
                    "id": "CURRENT-GOAL",
                    "state": "BLOCKED",
                    "can_progress": False,
                    "reason": "GUI unavailable",
                    "external_condition_id": "gui-session",
                },
                {
                    "id": "P2-OTHER-END",
                    "state": "READY",
                    "can_progress": True,
                    "reason": "independent package",
                    "external_condition_id": "",
                },
            ],
            "live_tasks": 0,
            "pending_candidates": 0,
            "controller_actions": 0,
        }

        errors = preblock_guard.validate_snapshot(
            snapshot, ledger_package_ids={"CURRENT-GOAL", "P2-OTHER-END"}
        )

        self.assertTrue(any("P2-OTHER-END can still make progress" in e for e in errors))
        self.assertTrue(any("P2-OTHER-END is still READY" in e for e in errors))

    def test_preblock_guard_rejects_active_work_and_controller_actions(self) -> None:
        snapshot = {
            "project_scope_scan": True,
            "ledger_revision": "abc123",
            "open_packages": [
                {
                    "id": "F1",
                    "state": "ACTIVE",
                    "can_progress": False,
                    "reason": "writer still running",
                    "external_condition_id": "writer-result",
                }
            ],
            "live_tasks": 1,
            "pending_candidates": 0,
            "controller_actions": 1,
        }

        errors = preblock_guard.validate_snapshot(snapshot, ledger_package_ids={"F1"})

        self.assertTrue(any("F1 is still ACTIVE" in e for e in errors))
        self.assertTrue(any("live_tasks must be zero" in e for e in errors))
        self.assertTrue(any("controller_actions must be zero" in e for e in errors))

    def test_preblock_guard_allows_single_shared_external_blocker(self) -> None:
        snapshot = {
            "project_scope_scan": True,
            "ledger_revision": "abc123",
            "open_packages": [
                {
                    "id": "F1",
                    "state": "BLOCKED",
                    "can_progress": False,
                    "reason": "waiting for the same production credential",
                    "external_condition_id": "production-credential",
                },
                {
                    "id": "F2",
                    "state": "BLOCKED",
                    "can_progress": False,
                    "reason": "waiting for the same production credential",
                    "external_condition_id": "production-credential",
                },
            ],
            "live_tasks": 0,
            "pending_candidates": 0,
            "controller_actions": 0,
        }

        self.assertEqual(
            preblock_guard.validate_snapshot(snapshot, ledger_package_ids={"F1", "F2"}),
            [],
        )

    def test_preblock_guard_rejects_omitted_open_ledger_package(self) -> None:
        snapshot = {
            "project_scope_scan": True,
            "ledger_revision": "abc123",
            "open_packages": [
                {
                    "id": "F1",
                    "state": "BLOCKED",
                    "can_progress": False,
                    "reason": "waiting for credential",
                    "external_condition_id": "credential",
                }
            ],
            "live_tasks": 0,
            "pending_candidates": 0,
            "controller_actions": 0,
        }

        errors = preblock_guard.validate_snapshot(
            snapshot, ledger_package_ids={"F1", "P2-READY"}
        )

        self.assertIn("scan omitted ledger packages: P2-READY", errors)

    def test_durable_profile_creates_nine_project_documents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = init_project.initialize_project(root, profile="durable")

            self.assertEqual(
                set(report.created),
                {
                    "AGENTS.md",
                    "TASK_LEDGER.md",
                    "MEMORY.md",
                    "WIKI_INDEX.md",
                    "SKILL.md",
                    "SPEC.md",
                    "DESIGN.md",
                    "TECHNICAL.md",
                    "EVOLUTION.md",
                },
            )
            errors, warnings = lint_governance.lint_project(root, strict=True)
            self.assertEqual(errors, [])
            self.assertEqual(warnings, [])

    def test_durable_profile_initializes_queryable_knowledge_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            report = init_project.initialize_project(root, profile="durable")

            self.assertEqual(
                set(report.created_directories),
                {
                    "raw_sources",
                    "wiki",
                    "logs",
                    "logs/ingestion",
                },
            )
            self.assertTrue((root / "raw_sources").is_dir())
            self.assertTrue((root / "wiki").is_dir())
            self.assertTrue((root / "logs" / "ingestion").is_dir())

    def test_existing_legacy_ledger_prevents_second_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "PROJECT_STATUS.md").write_text("# Existing\n", encoding="utf-8")

            report = init_project.initialize_project(root, profile="collaborative")

            self.assertFalse((root / "TASK_LEDGER.md").exists())
            self.assertIn(
                "TASK_LEDGER.md (using existing PROJECT_STATUS.md)", report.skipped
            )

    def test_linter_rejects_two_ledgers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "TASK_LEDGER.md").write_text("# New\n", encoding="utf-8")
            (root / "PROJECT_STATUS.md").write_text("# Old\n", encoding="utf-8")

            errors, _ = lint_governance.lint_project(root)

            self.assertTrue(any("both TASK_LEDGER" in error for error in errors))

    def test_linter_accepts_parallel_active_rows_when_pointer_matches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "TASK_LEDGER.md").write_text(
                """# Ledger

- 当前活动项：F1、F2

| ID | 状态 | 负责人 | 文件或范围 | 下一步动作 |
| --- | --- | --- | --- | --- |
| F1 | `ACTIVE` | Agent A | app/auth/** | 完成后进入 F3 |
| F2 | `ACTIVE` | Agent B | app/reader/** | 完成后进入 F4 |
""",
                encoding="utf-8",
            )

            errors, _ = lint_governance.lint_project(root)

            self.assertEqual(errors, [])

    def test_linter_rejects_activity_pointer_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "TASK_LEDGER.md").write_text(
                """# Ledger

- 当前活动项：F1

| ID | 状态 |
| --- | --- |
| F1 | `ACTIVE` |
| F2 | `ACTIVE` |
""",
                encoding="utf-8",
            )

            errors, _ = lint_governance.lint_project(root)

            self.assertTrue(any("current activity pointer does not match" in error for error in errors))

    def test_task_parser_ignores_status_words_in_evidence_and_non_task_tables(self) -> None:
        text = """# Ledger

| ID | 状态 / owner | 证据 / 下一步 |
| --- | --- | --- |
| F1 | `VERIFY` | 旧收据写 READY，但本项仍待复验 |

| 证据 | 结论 |
| --- | --- |
| screenshot | READY FOR EARLY |
"""

        self.assertEqual(lint_governance.task_rows(text), [("F1", "VERIFY")])

    def test_task_parser_normalizes_code_span_id_with_summary_label(self) -> None:
        text = """| 功能组 | 汇总状态 |
| --- | --- |
| `M2` 微信小程序 | `READY` |
"""

        self.assertEqual(lint_governance.task_rows(text), [("M2", "READY")])

    def test_legacy_project_status_template_passes_strict_lint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = (
                SKILL_ROOT / "assets" / "templates" / "PROJECT_STATUS.md"
            ).read_text(encoding="utf-8")
            (root / "PROJECT_STATUS.md").write_text(template, encoding="utf-8")

            errors, warnings = lint_governance.lint_project(root, strict=True)

            self.assertEqual(errors, [])
            self.assertEqual(warnings, [])

    def test_linter_rejects_duplicate_task_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "TASK_LEDGER.md").write_text(
                """# Ledger

- 当前活动项：无
- 协调下一动作：选择 F1

| ID | 状态 |
| --- | --- |
| F1 | `READY` |

| ID | 状态 |
| --- | --- |
| F1 | `DONE` |
""",
                encoding="utf-8",
            )

            errors, _ = lint_governance.lint_project(root, strict=True)

            self.assertTrue(any("repeats task IDs" in error for error in errors))

    def test_linter_warns_before_strict_duplicate_id_migration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "TASK_LEDGER.md").write_text(
                """# Ledger

- 当前活动项：无

| ID | 状态 |
| --- | --- |
| F1 | `READY` |

| ID | 状态 |
| --- | --- |
| F1 | `DONE` |
""",
                encoding="utf-8",
            )

            errors, warnings = lint_governance.lint_project(root)

            self.assertEqual(errors, [])
            self.assertTrue(any("repeats task IDs" in warning for warning in warnings))


if __name__ == "__main__":
    unittest.main()
