from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

UTC = timezone.utc
T0 = datetime(2026, 9, 4, 10, 0, 0, tzinfo=UTC)


class StepClock:
    def __init__(self, *values):
        self.values = list(values)
        self.index = 0

    def __call__(self):
        value = self.values[self.index]
        self.index += 1
        return value


class EvaluationTransactionTests(unittest.TestCase):
    def module(self):
        from scripts import evaluation_transaction
        return evaluation_transaction

    def model(self, sha="model-v2"):
        return {
            "state": "found",
            "path": "/runtime/current-model.md",
            "sha256": sha,
        }

    def test_historical_72_8_cannot_satisfy_re_evaluate_current_capability(self):
        et = self.module()
        calls = []
        tx = et.begin_evaluation(
            prompt="按照现有模型重新评估当前 Controller 的现在真实能力",
            subject={"kind": "controller", "id": "controller-1"},
            model=self.model(),
            evidence_provider=lambda: calls.append("refresh") or {"control_events": ["new-event"], "ledger": "READY"},
            historical_refs=[{"score": 72.8, "recorded_at": "2026-09-04T09:59:59+00:00"}],
            clock=StepClock(T0, T0 + timedelta(seconds=1)),
        )
        self.assertEqual("COMPUTE", tx["intent"])
        self.assertEqual(["refresh"], calls)
        self.assertEqual("READ", tx["historical_refs"][0]["provenance"])
        errors = et.validate_completion(
            tx,
            result={"performance_score": 72.8},
            provenance={"performance_score": "READ"},
            core_fields=("performance_score",),
        )
        self.assertTrue(any("COMPUTED" in error for error in errors))

    def test_latest_record_query_may_be_read(self):
        et = self.module()
        calls = []
        tx = et.begin_evaluation(
            prompt="系统最新记录的 Controller 分数是多少？",
            subject={"kind": "controller", "id": "controller-1"},
            model=self.model(),
            evidence_provider=lambda: calls.append("refresh") or {},
            historical_refs=[{"score": 72.8}],
            clock=StepClock(T0),
        )
        self.assertEqual("READ", tx["intent"])
        self.assertEqual([], calls)
        self.assertEqual(
            [],
            et.validate_completion(
                tx,
                result={"recorded_score": 72.8},
                provenance={"recorded_score": "READ"},
                core_fields=("recorded_score",),
            ),
        )

    def test_use_existing_model_and_recalculate_requires_computed(self):
        et = self.module()
        tx = et.begin_evaluation(
            prompt="按照这个现有模型重新算一遍",
            subject={"kind": "project", "id": "p1"},
            model=self.model(),
            evidence_provider=lambda: {"state": "current"},
            clock=StepClock(T0, T0 + timedelta(seconds=1)),
        )
        self.assertEqual("COMPUTE", tx["intent"])
        self.assertTrue(tx["evidence_snapshot_sha256"])
        self.assertEqual(
            [],
            et.validate_completion(
                tx,
                result={"decision": "PASS"},
                provenance={"decision": "COMPUTED"},
                core_fields=("decision",),
            ),
        )

    def test_just_generated_history_still_cannot_replace_re_evaluation(self):
        et = self.module()
        tx = et.begin_evaluation(
            prompt="刚刚虽然评过，但现在请重新评分",
            subject={"kind": "controller", "id": "controller-1"},
            model=self.model(),
            evidence_provider=lambda: {"current": "facts-v2"},
            historical_refs=[{"score": 88.0, "recorded_at": T0.isoformat()}],
            clock=StepClock(T0, T0 + timedelta(microseconds=1)),
        )
        self.assertEqual("COMPUTE", tx["intent"])
        self.assertNotEqual(
            tx["evaluation_begun_at"],
            tx["evidence_cutoff_at"],
        )
        self.assertEqual("READ", tx["historical_refs"][0]["provenance"])

    def test_current_fact_change_changes_evidence_snapshot(self):
        et = self.module()
        first = et.begin_evaluation(
            prompt="重新评估当前状态",
            subject={"kind": "project", "id": "p1"},
            model=self.model(),
            evidence_provider=lambda: {"ledger": "READY"},
            clock=StepClock(T0, T0 + timedelta(seconds=1)),
        )
        second = et.begin_evaluation(
            prompt="重新评估当前状态",
            subject={"kind": "project", "id": "p1"},
            model=self.model(),
            evidence_provider=lambda: {"ledger": "ACTIVE"},
            clock=StepClock(T0 + timedelta(seconds=2), T0 + timedelta(seconds=3)),
        )
        self.assertNotEqual(
            first["evidence_snapshot_sha256"],
            second["evidence_snapshot_sha256"],
        )

    def test_historical_cutoff_is_never_reused_as_current_cutoff(self):
        et = self.module()
        old_cutoff = T0 - timedelta(hours=1)
        tx = et.begin_evaluation(
            prompt="重新评估现在能力",
            subject={"kind": "controller", "id": "controller-1"},
            model=self.model(),
            evidence_provider=lambda: {"current": True},
            historical_refs=[{"score": 70.0, "evidence_cutoff_at": old_cutoff.isoformat()}],
            clock=StepClock(T0, T0 + timedelta(seconds=2)),
        )
        self.assertEqual((T0 + timedelta(seconds=2)).isoformat(), tx["evidence_cutoff_at"])
        self.assertNotEqual(old_cutoff.isoformat(), tx["evidence_cutoff_at"])

    def test_performance_and_risk_constrained_layers_are_separate(self):
        et = self.module()
        result = et.validate_scoring_layers(
            performance_score=80.0,
            governance_risk_status="RED",
            risk_constrained_score=49.0,
            active_cap=49.0,
        )
        self.assertEqual(80.0, result["capability"]["value"])
        self.assertEqual("COMPUTED", result["capability"]["provenance"])
        self.assertEqual(49.0, result["risk_constrained"]["value"])
        self.assertEqual("DERIVED", result["risk_constrained"]["provenance"])

    def test_old_model_history_is_ineligible_for_current_model_extrema(self):
        et = self.module()
        records = [
            {"record_kind": "cycle", "cycle_id": "old-best", "terminal_status": "CLOSED", "score": 99.0, "model_sha256": "old"},
            {"record_kind": "cycle", "cycle_id": "new-best", "terminal_status": "CLOSED", "score": 82.0, "model_sha256": "model-v2"},
            {"record_kind": "cycle", "cycle_id": "new-worst", "terminal_status": "FAILED", "score": 41.0, "model_sha256": "model-v2"},
        ]
        extremes = et.current_model_extrema(records, model_sha256="model-v2")
        self.assertEqual("new-best", extremes["best"]["cycle_id"])
        self.assertEqual("new-worst", extremes["worst"]["cycle_id"])

    def test_no_current_model_extrema_returns_unknown(self):
        et = self.module()
        extremes = et.current_model_extrema(
            [{"record_kind": "cycle", "cycle_id": "old", "terminal_status": "CLOSED", "score": 90.0, "model_sha256": "old"}],
            model_sha256="model-v2",
        )
        self.assertEqual("UNKNOWN", extremes["best"])
        self.assertEqual("UNKNOWN", extremes["worst"])

    def test_read_as_computed_violation_starts_same_flow_correction_with_new_evidence(self):
        et = self.module()
        original = et.begin_evaluation(
            prompt="按照现有模型重新评估当前能力",
            subject={"kind": "controller", "id": "controller-1"},
            model=self.model(),
            evidence_provider=lambda: {"revision": 1},
            clock=StepClock(T0, T0 + timedelta(seconds=1)),
        )
        errors = et.validate_completion(
            original,
            result={"performance_score": 72.8},
            provenance={"performance_score": "READ"},
            core_fields=("performance_score",),
        )
        self.assertTrue(errors)
        corrected = et.correct_evaluation(
            original,
            reason="core_result_provenance_READ",
            evidence_provider=lambda: {"revision": 2},
            clock=StepClock(T0 + timedelta(seconds=2), T0 + timedelta(seconds=3)),
        )
        self.assertEqual(original["evaluation_id"], corrected["supersedes_evaluation_id"])
        self.assertEqual("COMPUTE", corrected["intent"])
        self.assertNotEqual(
            original["evidence_snapshot_sha256"],
            corrected["evidence_snapshot_sha256"],
        )
        self.assertGreater(
            datetime.fromisoformat(corrected["evidence_cutoff_at"]),
            datetime.fromisoformat(original["evidence_cutoff_at"]),
        )


if __name__ == "__main__":
    unittest.main()
