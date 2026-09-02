import unittest

from scripts.controller_state import derive_runnable_tasks, project_task_state


class ControllerStateProjectionTests(unittest.TestCase):
    def test_legacy_states_map_to_exactly_five_controller_states(self):
        cases = {
            "PENDING": ("READY", False, None, None),
            "READY": ("READY", True, None, None),
            "ACTIVE": ("ACTIVE", None, "normal", None),
            "RECOVERING": ("ACTIVE", None, "recovering", None),
            "VERIFY": ("VERIFY", None, None, None),
            "BLOCKED": ("BLOCKED", None, None, None),
            "DONE": ("CLOSED", None, None, "done"),
            "SUPERSEDED": ("CLOSED", None, None, "superseded"),
        }
        for legacy, expected in cases.items():
            with self.subTest(legacy=legacy):
                result = project_task_state(legacy)
                self.assertEqual(
                    (result["main_state"], result["dispatchable"], result["health"], result["closure_reason"]),
                    expected,
                )
                self.assertIn(result["main_state"], {"READY", "ACTIVE", "VERIFY", "BLOCKED", "CLOSED"})

    def test_runtime_health_overrides_active_projection_without_new_main_state(self):
        stale = project_task_state("ACTIVE", runtime={"state": "progress_stale", "reason": "progress_deadline_exceeded"})
        recovering = project_task_state("ACTIVE", runtime={"state": "unhealthy", "reason": "lease_expired"})
        exhausted = project_task_state("ACTIVE", runtime={"state": "budget_exhausted", "reason": "recovery_budget_exhausted"})
        self.assertEqual((stale["main_state"], stale["health"]), ("ACTIVE", "stale"))
        self.assertEqual((recovering["main_state"], recovering["health"]), ("ACTIVE", "recovering"))
        self.assertEqual((exhausted["main_state"], exhausted["health"]), ("ACTIVE", "budget_exhausted"))

    def test_verify_accepts_named_machine_gate_without_status_inflation(self):
        result = project_task_state("VERIFY", verification_gate="review")
        self.assertEqual(result["main_state"], "VERIFY")
        self.assertEqual(result["verification_gate"], "review")


    def test_pending_task_becomes_runnable_when_declared_dependencies_are_closed(self):
        records = [
            {"id": "A", "status": "DONE", "next_action": "closed", "row": "A | DONE | closed"},
            {"id": "B", "status": "PENDING", "next_action": "依赖 A；随后实现 Web handoff", "row": "B | PENDING | 依赖 A；随后实现 Web handoff"},
        ]
        result = derive_runnable_tasks(records)
        self.assertEqual(result["runnable_task_ids"], ["B"])
        self.assertEqual(result["exclusions"], {})

    def test_pending_task_stays_nonrunnable_for_open_dependency_or_external_gate(self):
        records = [
            {"id": "A", "status": "BLOCKED", "next_action": "等待授权", "row": "A | BLOCKED | 等待授权"},
            {"id": "B", "status": "PENDING", "next_action": "依赖 A 后继续", "row": "B | PENDING | 依赖 A 后继续"},
            {"id": "C", "status": "PENDING", "next_action": "真实 AppID / 域名授权后验收", "row": "C | PENDING | 真实 AppID / 域名授权后验收"},
        ]
        result = derive_runnable_tasks(records)
        self.assertEqual(result["runnable_task_ids"], [])
        self.assertIn("open_dependencies:A", result["exclusions"]["B"])
        self.assertIn("external_or_environment_gate", result["exclusions"]["C"])

    def test_pending_with_shorthand_dependency_gate_is_not_guessed_runnable(self):
        records = [
            {"id": "M1-F5-C", "status": "PENDING", "next_action": "依赖 B；完成后生成模板", "row": "M1-F5-C | PENDING | 依赖 B"},
            {"id": "M1-F4-C-WEB-HANDOFF", "status": "PENDING", "next_action": "仅在 SERVER-GATE candidate 集成后转 READY", "row": "M1-F4-C-WEB-HANDOFF | PENDING | 仅在 SERVER-GATE candidate 集成后转 READY"},
        ]
        result = derive_runnable_tasks(records)
        self.assertEqual(result["runnable_task_ids"], [])
        self.assertIn("unresolved_dependency_gate", result["exclusions"]["M1-F5-C"])
        self.assertIn("unresolved_dependency_gate", result["exclusions"]["M1-F4-C-WEB-HANDOFF"])

    def test_pending_with_unresolved_task_shorthand_is_not_guessed_runnable(self):
        records = [{"id": "M1-F5", "status": "PENDING", "next_action": "F4-V1、对话选择、阅读与外部生成边界", "row": "M1-F5 | PENDING"}]
        result = derive_runnable_tasks(records)
        self.assertEqual(result["runnable_task_ids"], [])
        self.assertIn("unresolved_dependency_gate", result["exclusions"]["M1-F5"])

    def test_pending_without_unresolved_dependency_or_gate_is_derived_runnable(self):
        records = [
            {"id": "A", "status": "BLOCKED", "next_action": "等待 credential", "row": "A | BLOCKED | 等待 credential"},
            {"id": "B", "status": "PENDING", "next_action": "实现独立文档校验", "row": "B | PENDING | 实现独立文档校验"},
        ]
        result = derive_runnable_tasks(records)
        self.assertEqual(result["runnable_task_ids"], ["B"])

    def test_unknown_legacy_state_fails_closed(self):
        with self.assertRaises(ValueError):
            project_task_state("RUNNING")


if __name__ == "__main__":
    unittest.main()
