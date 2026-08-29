import unittest

from scripts.controller_state import project_task_state


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

    def test_unknown_legacy_state_fails_closed(self):
        with self.assertRaises(ValueError):
            project_task_state("RUNNING")


if __name__ == "__main__":
    unittest.main()
