import unittest

from scripts.controller_health import derive_controller_health, decide_controller_wake


class ControllerHealthTests(unittest.TestCase):
    def test_active_writer_is_active_or_deferred_without_fallback(self):
        health = derive_controller_health({
            "registered_controller": "controller-1",
            "pending_control_event": True,
            "controller_host": "web",
            "active_writer": True,
            "resume_state": "RESUME_DEFERRED_ACTIVE_WRITER",
            "peer_host_available": True,
        })
        self.assertEqual(health["state"], "DEFERRED")
        self.assertEqual(decide_controller_wake(health)["decision"], "DEFER")
        self.assertIsNone(decide_controller_wake(health)["selected_host"])

    def test_eligible_terminal_host_failure_requires_peer_fallback(self):
        health = derive_controller_health({
            "registered_controller": "controller-1",
            "pending_control_event": True,
            "controller_host": "web",
            "active_writer": False,
            "resume_state": "RESUME_FAILED",
            "failure_class": "quota_exhausted",
            "fallback_eligible": True,
            "peer_host_available": True,
            "fallback_safe": True,
        })
        self.assertEqual(health["state"], "FALLBACK_NEEDED")
        decision = decide_controller_wake(health)
        self.assertEqual(decision["decision"], "FALLBACK_PEER_HOST")
        self.assertEqual(decision["selected_host"], "desktop_codex")

    def test_no_safe_continuation_is_dead_but_never_replace(self):
        health = derive_controller_health({
            "registered_controller": "controller-1",
            "pending_control_event": True,
            "controller_host": "web",
            "active_writer": False,
            "resume_state": "RESUME_FAILED",
            "failure_class": "runtime_unavailable",
            "fallback_eligible": True,
            "peer_host_available": False,
            "fallback_safe": True,
            "failure_conclusive": True,
        })
        self.assertEqual(health["state"], "DEAD")
        self.assertEqual(decide_controller_wake(health)["decision"], "DEAD_BLOCK")

    def test_ambiguous_or_unsafe_failures_defer_without_peer_fallback(self):
        unsafe_cases = (
            {"failure_class": "resume_failed"},
            {"failure_class": "timeout"},
            {"failure_class": "quota_exhausted", "unknown_side_effect": True},
            {"failure_class": "quota_exhausted", "partial_write": True},
        )
        for unsafe_case in unsafe_cases:
            with self.subTest(**unsafe_case):
                health = derive_controller_health({
                    "registered_controller": "controller-1",
                    "pending_control_event": True,
                    "controller_host": "web",
                    "active_writer": False,
                    "resume_state": "RESUME_FAILED",
                    "fallback_eligible": True,
                    "peer_host_available": True,
                    "fallback_safe": True,
                    **unsafe_case,
                })
                decision = decide_controller_wake(health)
                self.assertEqual(health["state"], "DEGRADED")
                self.assertEqual(decision["decision"], "DEFER")
                self.assertIsNone(decision["selected_host"])

if __name__ == "__main__":
    unittest.main()
