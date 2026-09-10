from __future__ import annotations

import unittest

from scripts import agent_target_resolution as resolution


class LogicalAgentTargetResolutionTests(unittest.TestCase):
    def test_identity_contract_directly_supports_controller_agent_reviewer_and_runtime_repair_agent(self) -> None:
        for agent_type in ("controller", "agent", "reviewer", "runtime_repair_agent"):
            identity = resolution.logical_agent_identity(agent_type=agent_type, agent_id=f"{agent_type}-1")
            self.assertEqual(identity["agent_type"], agent_type)
            self.assertEqual(identity["schema_version"], 1)

    def test_verified_execution_target_is_generic_and_carries_double_generation_fence(self) -> None:
        for agent_type in ("agent", "reviewer", "runtime_repair_agent"):
            identity = resolution.logical_agent_identity(agent_type=agent_type, agent_id=f"{agent_type}-7")
            target = resolution.verified_execution_target(
                logical_agent=identity,
                host="web",
                execution_target_session_id=f"web-{agent_type}-current",
                target_generation=4,
                ownership_generation=9,
                provenance="test_projection",
            )
            self.assertEqual(target["logical_agent_identity"], identity)
            self.assertEqual(target["target_generation"], 4)
            self.assertEqual(target["ownership_generation"], 9)
            self.assertEqual(target["state"], "VERIFIED")

    def test_resolution_status_model_is_generic_and_requires_verified_target_only_for_verified_state(self) -> None:
        for agent_type in ("controller", "agent", "reviewer", "runtime_repair_agent"):
            identity = resolution.logical_agent_identity(agent_type=agent_type, agent_id=f"{agent_type}-9")
            unresolved = resolution.execution_target_resolution(
                logical_agent=identity, state="UNRESOLVED", reason="OWNERSHIP_RESOLVER_REQUIRED"
            )
            self.assertEqual(unresolved["contract"], "logical_agent_target_resolution_v1")
            self.assertEqual(unresolved["state"], "UNRESOLVED")
            self.assertNotIn("verified_execution_target", unresolved)
            with self.assertRaisesRegex(PermissionError, "UNRESOLVED.*OWNERSHIP_RESOLVER_REQUIRED"):
                resolution.require_verified_execution_target_from_resolution(unresolved)

        controller = resolution.logical_agent_identity(agent_type="controller", agent_id="controller-1")
        target = resolution.verified_execution_target(
            logical_agent=controller, host="web", execution_target_session_id="web-current",
            target_generation=4, ownership_generation=9, provenance="test_projection",
        )
        verified = resolution.execution_target_resolution(
            logical_agent=controller, state="VERIFIED", reason="CURRENT_EXECUTION_TARGET",
            verified_target=target,
        )
        required = resolution.require_verified_execution_target_from_resolution(verified)
        self.assertEqual(required["execution_target_session_id"], "web-current")
        self.assertEqual(required["target_generation"], 4)
        self.assertEqual(required["ownership_generation"], 9)

    def test_resolution_status_model_declares_stale_and_conflicted_without_verified_target(self) -> None:
        identity = resolution.logical_agent_identity(agent_type="reviewer", agent_id="reviewer-2")
        for state in ("STALE", "CONFLICTED"):
            value = resolution.execution_target_resolution(
                logical_agent=identity, state=state, reason=f"{state}_TEST"
            )
            self.assertEqual(value["state"], state)
            self.assertNotIn("verified_execution_target", value)

    def test_verified_execution_target_rejects_wrong_logical_agent_and_stale_fences(self) -> None:
        controller = resolution.logical_agent_identity(agent_type="controller", agent_id="controller-1")
        reviewer = resolution.logical_agent_identity(agent_type="reviewer", agent_id="reviewer-1")
        target = resolution.verified_execution_target(
            logical_agent=controller,
            host="web",
            execution_target_session_id="web-current",
            target_generation=4,
            ownership_generation=9,
            provenance="test_projection",
        )
        with self.assertRaisesRegex(PermissionError, "different logical Agent"):
            resolution.normalize_verified_execution_target(target, expected_logical_agent=reviewer)
        with self.assertRaisesRegex(PermissionError, "session mismatch"):
            resolution.normalize_verified_execution_target(
                target, expected_execution_target_session_id="web-other"
            )
        with self.assertRaisesRegex(PermissionError, "target generation is stale"):
            resolution.normalize_verified_execution_target(target, expected_target_generation=3)
        with self.assertRaisesRegex(PermissionError, "ownership generation is stale"):
            resolution.normalize_verified_execution_target(target, expected_ownership_generation=8)


if __name__ == "__main__":
    unittest.main()

class LogicalAgentExecutionTurnTests(unittest.TestCase):
    def test_verified_execution_turn_is_generic_and_stable_across_target_generation_rotation(self) -> None:
        for agent_type in ("controller", "agent", "reviewer", "runtime_repair_agent"):
            identity = resolution.logical_agent_identity(agent_type=agent_type, agent_id=f"{agent_type}-turn")
            before = resolution.verified_execution_target(
                logical_agent=identity, host="web", execution_target_session_id="web-current",
                target_generation=7, ownership_generation=7, provenance="test",
            )
            after = resolution.verified_execution_target(
                logical_agent=identity, host="web", execution_target_session_id="web-current",
                target_generation=8, ownership_generation=8, provenance="test",
            )
            turn_a = resolution.verified_execution_turn(
                verified_target=before, runtime_invocation_id="machine-generation-A",
                provenance="runtime_host_current_entry_v1",
            )
            turn_b = resolution.verified_execution_turn(
                verified_target=after, runtime_invocation_id="machine-generation-A",
                provenance="runtime_host_current_entry_v1",
            )
            self.assertEqual(turn_a["contract"], "verified_execution_turn_v1")
            self.assertEqual(turn_a["turn_id"], turn_b["turn_id"])
            self.assertEqual(turn_b["target_generation"], 8)
            self.assertEqual(turn_b["ownership_generation"], 8)
            next_turn = resolution.verified_execution_turn(
                verified_target=after, runtime_invocation_id="machine-generation-B",
                provenance="runtime_host_current_entry_v1",
            )
            self.assertNotEqual(turn_a["turn_id"], next_turn["turn_id"])

    def test_verified_execution_turn_rejects_wrong_agent_target_or_invocation(self) -> None:
        controller = resolution.logical_agent_identity(agent_type="controller", agent_id="controller-1")
        reviewer = resolution.logical_agent_identity(agent_type="reviewer", agent_id="reviewer-1")
        target = resolution.verified_execution_target(
            logical_agent=controller, host="web", execution_target_session_id="web-current",
            target_generation=8, ownership_generation=8, provenance="test",
        )
        turn = resolution.verified_execution_turn(
            verified_target=target, runtime_invocation_id="machine-generation-A",
            provenance="runtime_host_current_entry_v1",
        )
        with self.assertRaisesRegex(PermissionError, "different logical Agent"):
            resolution.normalize_verified_execution_turn(turn, expected_logical_agent=reviewer)
        with self.assertRaisesRegex(PermissionError, "target generation is stale"):
            resolution.normalize_verified_execution_turn(turn, expected_target_generation=7)
        with self.assertRaisesRegex(PermissionError, "runtime invocation"):
            resolution.normalize_verified_execution_turn(turn, expected_runtime_invocation_id="machine-generation-B")
