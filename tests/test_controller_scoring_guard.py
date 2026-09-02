from pathlib import Path
import hashlib
import importlib.util
import json
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "controller_scoring_guard.py"


def load_module():
    spec = importlib.util.spec_from_file_location("controller_scoring_guard", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


def append_attested_cycle(guard, repo: Path, record: dict, **receipt_overrides):
    cycle_id = str(record["cycle_id"])
    evidence_id = str(receipt_overrides.pop("evidence_id", f"evidence-{cycle_id}"))
    receipt = {
        "schema_version": 1,
        "record_kind": "controller_cycle_evidence",
        "evidence_id": evidence_id,
        "controller_id": record["controller_session_id"],
        "cycle_id": cycle_id,
        "terminal_status": record["terminal_status"],
        "evidence_summary": record["evidence_summary"],
        "outcome_level": "L3",
        "recorded_at": record.get("recorded_at", "2026-09-02T00:00:00+00:00"),
        **receipt_overrides,
    }
    target = guard._cycle_evidence_path(repo, evidence_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    content = (json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    target.write_bytes(content)
    return guard.append_score_history(
        repo,
        {
            **record,
            "evidence_id": evidence_id,
            "evidence_sha256": hashlib.sha256(content).hexdigest(),
        },
    )


class ControllerScoringGuardTests(unittest.TestCase):
    def test_scoring_is_blocked_without_fresh_model_read_receipt(self):
        guard = load_module()
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            import subprocess
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            errors = guard.score_guard_errors(repo, skill_root=ROOT)
        self.assertTrue(any("scoring model read receipt" in error for error in errors))

    def test_record_read_then_guard_passes_for_exact_model_digest(self):
        guard = load_module()
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            import subprocess
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            receipt = guard.record_model_read(repo, skill_root=ROOT)
            self.assertEqual(guard.scoring_model_sha256(ROOT), receipt["model_sha256"])
            self.assertEqual([], guard.score_guard_errors(repo, skill_root=ROOT))

    def test_registered_repo_manual_scoring_requires_stable_logical_controller_id(self):
        guard = load_module()
        import subprocess
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            registry = Path(td) / "controllers.json"
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            registry.write_text(json.dumps({
                "logical-controller": str(repo),
                "__controller_sessions__": {
                    "logical-controller": {"desktop_codex": ["source-session"]}
                },
            }), encoding="utf-8")
            guard.CONTROLLER_REGISTRY_PATH = registry

            with self.assertRaisesRegex(ValueError, "stable logical controller"):
                guard.record_model_read(
                    repo,
                    skill_root=ROOT,
                    controller_session_id="source-session",
                )
            receipt = guard.record_model_read(
                repo,
                skill_root=ROOT,
                controller_session_id="logical-controller",
            )
            self.assertEqual("logical-controller", receipt["controller_session_id"])

    def test_model_change_invalidates_old_receipt(self):
        guard = load_module()
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as sd:
            repo = Path(td)
            import subprocess
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            skill = Path(sd)
            (skill / "references").mkdir()
            model = skill / "references" / "controller-performance-scoring.md"
            model.write_text("version one\n", encoding="utf-8")
            guard.record_model_read(repo, skill_root=skill)
            model.write_text("version two\n", encoding="utf-8")
            errors = guard.score_guard_errors(repo, skill_root=skill)
        self.assertTrue(any("stale" in error for error in errors))

    def test_linked_worktrees_share_one_scoring_receipt_store(self):
        guard = load_module()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "repo"
            linked = Path(td) / "linked"
            import subprocess
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
            (root / "x").write_text("x", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "x"], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-qm", "init"], check=True)
            subprocess.run(["git", "-C", str(root), "worktree", "add", "-qb", "linked", str(linked)], check=True)
            guard.record_model_read(root, skill_root=ROOT)
            self.assertEqual(guard.receipt_path(root), guard.receipt_path(linked))
            self.assertEqual([], guard.score_guard_errors(linked, skill_root=ROOT))

    def test_record_read_cli_prints_model_content_before_receipt(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            import subprocess
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            completed = subprocess.run([
                "python3", str(SCRIPT), "record-read", "--repo", str(repo)
            ], check=True, capture_output=True, text=True)
            self.assertIn("# Controller Performance Scoring", completed.stdout)
            self.assertIn("七维评分模型", completed.stdout)
            self.assertIn('"model_sha256"', completed.stdout)


    def test_score_guard_rejects_expired_receipt_and_successful_cli_guard_consumes_it(self):
        guard = load_module()
        import json, subprocess
        from datetime import datetime, timedelta, timezone
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            guard.record_model_read(repo, skill_root=ROOT, controller_session_id="controller-1")
            target = guard.receipt_path(repo)
            value = json.loads(target.read_text(encoding="utf-8"))
            value["read_at"] = (datetime.now(timezone.utc) - timedelta(seconds=guard.RECEIPT_MAX_AGE_SECONDS + 1)).isoformat()
            target.write_text(json.dumps(value), encoding="utf-8")
            self.assertTrue(any("expired" in error for error in guard.score_guard_errors(repo, skill_root=ROOT)))

            guard.record_model_read(repo, skill_root=ROOT, controller_session_id="controller-1")
            self.assertEqual([], guard.consume_score_guard(repo, skill_root=ROOT))
            self.assertFalse(target.exists())
            self.assertTrue(guard.score_guard_errors(repo, skill_root=ROOT))

    def test_read_and_record_uses_the_same_model_bytes_for_output_and_receipt(self):
        guard = load_module()
        import subprocess
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as sd:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            skill = Path(sd)
            (skill / "references").mkdir()
            model = skill / "references" / "controller-performance-scoring.md"
            model.write_text("one exact model\n", encoding="utf-8")
            content, receipt = guard.read_and_record_model(repo, skill_root=skill)
            self.assertEqual(b"one exact model\n", content)
            import hashlib
            self.assertEqual(hashlib.sha256(content).hexdigest(), receipt["model_sha256"])

    def test_score_history_is_shared_by_git_common_dir_and_latest_is_controller_scoped(self):
        guard = load_module()
        import subprocess
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "repo"
            linked = Path(td) / "linked"
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
            (root / "x").write_text("x", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "x"], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-qm", "init"], check=True)
            subprocess.run(["git", "-C", str(root), "worktree", "add", "-qb", "linked", str(linked)], check=True)
            guard.append_score_history(root, {"controller_session_id": "c1", "score": 81.0, "recorded_at": "2026-08-31T10:00:00+00:00"})
            guard.append_score_history(linked, {"controller_session_id": "c2", "score": 90.0, "recorded_at": "2026-08-31T10:01:00+00:00"})
            guard.append_score_history(linked, {"controller_session_id": "c1", "score": 86.0, "recorded_at": "2026-08-31T10:02:00+00:00"})
            self.assertEqual(guard.score_history_path(root), guard.score_history_path(linked))
            self.assertEqual(86.0, guard.latest_score_history(root, controller_session_id="c1")["score"])
            self.assertEqual(90.0, guard.latest_score_history(linked, controller_session_id="c2")["score"])
            self.assertIsNone(guard.latest_score_history(root, controller_session_id="missing"))

    def test_finalize_score_consumes_guard_and_records_history_for_manual_web_path(self):
        guard = load_module()
        import subprocess
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            guard.record_model_read(repo, skill_root=ROOT, controller_session_id="controller-1")
            record = guard.finalize_score(
                repo, skill_root=ROOT, controller_session_id="controller-1", turn_id="web-turn-1",
                score=86.0, performance_score=86.0, governance_risk_status="GREEN",
                risk_summary="no unresolved governance incident",
                window_summary="最近两个闭合控制事件 + 24h 异常", message_sha256="abc",
            )
            self.assertEqual(86.0, record["score"])
            self.assertFalse(guard.receipt_path(repo).exists())
            self.assertEqual(86.0, guard.latest_score_history(repo, controller_session_id="controller-1")["score"])

    def test_finalize_score_records_three_layer_report_and_current_cycle_extremes(self):
        guard = load_module()
        import subprocess
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            model = guard.scoring_model_sha256(ROOT)
            append_attested_cycle(guard, repo, {
                "controller_session_id": "controller-1", "record_kind": "cycle",
                "cycle_id": "best-cycle", "terminal_status": "CLOSED", "score": 90.0,
                "model_sha256": model, "evidence_summary": "closed cleanly", "message_sha256": "a" * 64,
            })
            append_attested_cycle(guard, repo, {
                "controller_session_id": "controller-1", "record_kind": "cycle",
                "cycle_id": "worst-cycle", "terminal_status": "FAILED", "score": 49.0,
                "model_sha256": model, "evidence_summary": "gate bypassed", "message_sha256": "b" * 64,
            })
            guard.record_model_read(repo, skill_root=ROOT, controller_session_id="controller-1")
            record = guard.finalize_score(
                repo, skill_root=ROOT, controller_session_id="controller-1", turn_id="web-turn-1",
                score=49.0, performance_score=77.4, governance_risk_status="RED",
                risk_summary="major incident remains open",
                window_summary="最近 24 小时内最多 5 个有效控制事件", message_sha256="c" * 64,
            )
            self.assertEqual(77.4, record["performance_score"])
            self.assertEqual(49.0, record["risk_constrained_score"])
            self.assertEqual("RED", record["governance_risk_status"])
            self.assertEqual("best-cycle", record["cycle_extremes"]["best"]["cycle_id"])
            self.assertEqual("worst-cycle", record["cycle_extremes"]["worst"]["cycle_id"])

    def test_finalize_score_rejects_constraint_above_performance(self):
        guard = load_module()
        import subprocess
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            guard.record_model_read(repo, skill_root=ROOT)
            with self.assertRaisesRegex(ValueError, "risk-constrained score"):
                guard.finalize_score(
                    repo, skill_root=ROOT, controller_session_id="controller-1", turn_id="web-turn-1",
                    score=81.0, performance_score=80.0, governance_risk_status="GREEN",
                    risk_summary="no open governance incident", window_summary="24h", message_sha256="d" * 64,
                )
            self.assertIsNone(guard.latest_score_history(repo, controller_session_id="controller-1"))

    def test_finalize_score_enforces_machine_risk_projection_and_red_cap(self):
        guard = load_module()
        import subprocess
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            guard.append_score_history(repo, {
                "controller_session_id": "controller-1", "record_kind": "cycle",
                "cycle_id": "major-incident", "terminal_status": "FAILED", "score": 49.0,
                "model_sha256": "legacy-model", "evidence_summary": "candidate bypassed review",
                "message_sha256": "a" * 64,
            })
            guard.record_model_read(
                repo, skill_root=ROOT, controller_session_id="controller-1"
            )
            with self.assertRaisesRegex(ValueError, "machine governance risk projection"):
                guard.finalize_score(
                    repo, skill_root=ROOT, controller_session_id="controller-1", turn_id="t1",
                    score=95.0, performance_score=95.0, governance_risk_status="GREEN",
                    risk_summary="claimed clean", window_summary="24h", message_sha256="b" * 64,
                )

    def test_machine_failed_cycle_evidence_creates_red_risk_without_self_scored_history(self):
        guard = load_module()
        import subprocess
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            target = guard._cycle_evidence_path(repo, "failed-event-1")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps({
                "schema_version": 1,
                "record_kind": "controller_cycle_evidence",
                "evidence_id": "failed-event-1",
                "controller_id": "controller-1",
                "cycle_id": "failed-event-1",
                "terminal_status": "FAILED",
                "evidence_summary": "candidate review missing",
                "outcome_level": "L0",
                "governance_incident_severity": "major",
                "recorded_at": "2026-09-02T00:00:00+00:00",
            }), encoding="utf-8")

            projection = guard.governance_risk_projection(
                repo, controller_session_id="controller-1"
            )
            self.assertEqual("RED", projection["status"])
            self.assertEqual(49, projection["active_cap"])
            self.assertEqual("failed-event-1", projection["incident_states"][0]["incident_cycle_id"])

            guard.record_model_read(
                repo, skill_root=ROOT, controller_session_id="controller-1"
            )
            with self.assertRaisesRegex(ValueError, "49"):
                guard.finalize_score(
                    repo, skill_root=ROOT, controller_session_id="controller-1", turn_id="t2",
                    score=90.0, performance_score=95.0, governance_risk_status="RED",
                    risk_summary="incident remains open", window_summary="24h", message_sha256="c" * 64,
                )

    def test_blocked_guard_attempt_without_major_incident_does_not_create_49_debt(self):
        guard = load_module()
        import subprocess
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            target = guard._cycle_evidence_path(repo, "blocked-draft")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps({
                "schema_version": 1,
                "record_kind": "controller_cycle_evidence",
                "evidence_id": "blocked-draft",
                "controller_id": "controller-1",
                "cycle_id": "blocked-draft",
                "terminal_status": "FAILED",
                "evidence_summary": "draft omitted a READY package",
                "outcome_level": "L0",
                "governance_incident_severity": "none",
                "recorded_at": "2026-09-02T00:00:00+00:00",
            }), encoding="utf-8")

            projection = guard.governance_risk_projection(
                repo, controller_session_id="controller-1"
            )
            self.assertEqual("GREEN", projection["status"])
            self.assertIsNone(projection["active_cap"])

    def test_risk_projection_clears_cap_only_after_correction_alignment_and_l3_closure(self):
        guard = load_module()
        import subprocess
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)

            def write_receipt(evidence_id: str, recorded_at: str, **values):
                target = guard._cycle_evidence_path(repo, evidence_id)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps({
                    "schema_version": 1,
                    "record_kind": "controller_cycle_evidence",
                    "evidence_id": evidence_id,
                    "controller_id": "controller-1",
                    "cycle_id": evidence_id,
                    "terminal_status": values.pop("terminal_status", "CLOSED"),
                    "evidence_summary": values.pop("evidence_summary", evidence_id),
                    "outcome_level": values.pop("outcome_level", "L2"),
                    "recorded_at": recorded_at,
                    **values,
                }, sort_keys=True), encoding="utf-8")

            write_receipt(
                "incident-1", "2026-09-02T00:00:00+00:00",
                terminal_status="FAILED", outcome_level="L0",
                governance_incident_severity="major",
            )
            self.assertEqual("RED", guard.governance_risk_projection(
                repo, controller_session_id="controller-1"
            )["status"])

            write_receipt(
                "correction-1", "2026-09-02T00:01:00+00:00",
                corrects_incident="incident-1",
            )
            self.assertEqual("AMBER", guard.governance_risk_projection(
                repo, controller_session_id="controller-1"
            )["status"])

            write_receipt(
                "alignment-1", "2026-09-02T00:02:00+00:00",
                alignment_for_incident="incident-1",
            )
            self.assertEqual("AMBER", guard.governance_risk_projection(
                repo, controller_session_id="controller-1"
            )["status"])

            write_receipt(
                "closure-1", "2026-09-02T00:03:00+00:00",
                post_incident_closure_for="incident-1", outcome_level="L3",
            )
            projection = guard.governance_risk_projection(
                repo, controller_session_id="controller-1"
            )
            self.assertEqual("GREEN", projection["status"])
            self.assertIsNone(projection["active_cap"])

    def test_finalize_score_fails_closed_without_valid_guard_and_writes_no_history(self):
        guard = load_module()
        import subprocess
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            with self.assertRaisesRegex(ValueError, "score-guard"):
                guard.finalize_score(
                    repo, skill_root=ROOT, controller_session_id="controller-1", turn_id="web-turn-1",
                    score=86.0, performance_score=86.0, governance_risk_status="GREEN",
                    risk_summary="no unresolved governance incident", window_summary=None, message_sha256=None,
                )
            self.assertIsNone(guard.latest_score_history(repo, controller_session_id="controller-1"))

    def test_latest_score_cli_returns_unknown_when_no_controller_record_exists(self):
        import subprocess
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            completed = subprocess.run([
                "python3", str(SCRIPT), "latest-score", "--repo", str(repo), "--controller-session", "c1"
            ], check=True, capture_output=True, text=True)
            self.assertEqual("UNKNOWN", completed.stdout.strip())

    def test_finalize_score_cli_persists_three_layer_report(self):
        import json
        import subprocess
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(
                ["python3", str(SCRIPT), "record-read", "--repo", str(repo), "--controller-session", "controller-1"],
                check=True, capture_output=True, text=True,
            )
            completed = subprocess.run([
                "python3", str(SCRIPT), "finalize-score", "--repo", str(repo),
                "--controller-session", "controller-1", "--score", "77.4",
                "--performance-score", "77.4", "--governance-risk-status", "GREEN",
                "--risk-summary", "no recorded machine governance incident",
                "--window-summary", "24h",
            ], check=True, capture_output=True, text=True)
            record = json.loads(completed.stdout)
            self.assertEqual(77.4, record["performance_score"])
            self.assertEqual(77.4, record["risk_constrained_score"])
            self.assertEqual("GREEN", record["governance_risk_status"])
            self.assertEqual({"best": None, "worst": None}, record["cycle_extremes"])

    def test_cli_rejects_skill_root_override(self):
        import subprocess
        completed = subprocess.run([
            "python3", str(SCRIPT), "score-guard", "--repo", str(ROOT), "--skill-root", "/tmp/alternate"
        ], capture_output=True, text=True)
        self.assertNotEqual(0, completed.returncode)
        self.assertIn("unrecognized arguments", completed.stderr)

    def test_cycle_history_extremes_are_separate_from_formal_latest_score(self):
        guard = load_module()
        import subprocess
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            model = guard.scoring_model_sha256(ROOT)
            guard.append_score_history(repo, {"controller_session_id": "c1", "record_kind": "formal", "score": 88.0, "model_sha256": model})
            append_attested_cycle(guard, repo, {"controller_session_id": "c1", "record_kind": "cycle", "cycle_id": "good", "terminal_status": "CLOSED", "score": 94.0, "model_sha256": model, "evidence_summary": "closed", "message_sha256": "a" * 64})
            append_attested_cycle(guard, repo, {"controller_session_id": "c1", "record_kind": "cycle", "cycle_id": "bad", "terminal_status": "FAILED", "score": 52.0, "model_sha256": model, "evidence_summary": "failed", "message_sha256": "b" * 64})
            self.assertEqual(88.0, guard.latest_score_history(repo, controller_session_id="c1")["score"])
            extremes = guard.cycle_score_extremes(repo, controller_session_id="c1", model_sha256=model)
            self.assertEqual("good", extremes["best"]["cycle_id"])
            self.assertEqual("bad", extremes["worst"]["cycle_id"])

    def test_cycle_extremes_ignore_nonterminal_and_other_model_records(self):
        guard = load_module()
        import subprocess
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            model = guard.scoring_model_sha256(ROOT)
            guard.append_score_history(repo, {"controller_session_id": "c1", "record_kind": "cycle", "cycle_id": "active", "terminal_status": "ACTIVE", "score": 5.0, "model_sha256": model, "evidence_summary": "active", "message_sha256": "c" * 64})
            guard.append_score_history(repo, {"controller_session_id": "c1", "record_kind": "cycle", "cycle_id": "old-model", "terminal_status": "CLOSED", "score": 99.0, "model_sha256": "other", "evidence_summary": "old", "message_sha256": "d" * 64})
            append_attested_cycle(guard, repo, {"controller_session_id": "c1", "record_kind": "cycle", "cycle_id": "valid", "terminal_status": "BLOCKED", "score": 70.0, "model_sha256": model, "evidence_summary": "blocked", "message_sha256": "e" * 64})
            extremes = guard.cycle_score_extremes(repo, controller_session_id="c1", model_sha256=model)
            self.assertEqual("valid", extremes["best"]["cycle_id"])
            self.assertEqual("valid", extremes["worst"]["cycle_id"])

    def test_finalize_cycle_candidate_requires_terminal_claim_and_stays_out_of_extremes(self):
        guard = load_module()
        import subprocess
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            guard.record_model_read(repo, skill_root=ROOT)
            with self.assertRaisesRegex(ValueError, "terminal"):
                guard.finalize_cycle_candidate(repo, skill_root=ROOT, controller_session_id="c1", turn_id="t1", cycle_id="cycle-1", terminal_status="ACTIVE", score=80.0, evidence_summary="still running", message_sha256="x")
            guard.record_model_read(repo, skill_root=ROOT)
            record = guard.finalize_cycle_candidate(repo, skill_root=ROOT, controller_session_id="c1", turn_id="t2", cycle_id="cycle-1", terminal_status="CLOSED", score=82.0, evidence_summary="closed with reviewer", message_sha256="a" * 64)
            self.assertEqual("cycle_candidate", record["record_kind"])
            self.assertIsNone(guard.latest_score_history(repo, controller_session_id="c1"))
            self.assertEqual(
                {"best": None, "worst": None},
                guard.cycle_score_extremes(repo, controller_session_id="c1", model_sha256=guard.scoring_model_sha256(ROOT)),
            )

    def test_finalize_attested_cycle_score_requires_exact_machine_receipt(self):
        guard = load_module()
        import subprocess
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            receipt = {
                "schema_version": 1,
                "record_kind": "controller_cycle_evidence",
                "evidence_id": "event-1",
                "controller_id": "c1",
                "cycle_id": "event-1",
                "terminal_status": "CLOSED",
                "evidence_summary": "candidate reviewed and integrated",
                "outcome_level": "L3",
                "recorded_at": "2026-09-02T00:00:00+00:00",
            }
            target = guard._cycle_evidence_path(repo, "event-1")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(receipt), encoding="utf-8")
            guard.record_model_read(repo, skill_root=ROOT, controller_session_id="c1")
            record = guard.finalize_attested_cycle_score(
                repo,
                skill_root=ROOT,
                controller_session_id="c1",
                turn_id="t1",
                cycle_id="event-1",
                terminal_status="CLOSED",
                score=91.0,
                evidence_summary="candidate reviewed and integrated",
                evidence_id="event-1",
                message_sha256="a" * 64,
            )
            self.assertEqual("cycle", record["record_kind"])
            extremes = guard.cycle_score_extremes(
                repo,
                controller_session_id="c1",
                model_sha256=guard.scoring_model_sha256(ROOT),
            )
            self.assertEqual("event-1", extremes["best"]["cycle_id"])

            guard.record_model_read(repo, skill_root=ROOT, controller_session_id="c1")
            with self.assertRaisesRegex(ValueError, "does not match"):
                guard.finalize_attested_cycle_score(
                    repo,
                    skill_root=ROOT,
                    controller_session_id="c1",
                    turn_id="t2",
                    cycle_id="event-1",
                    terminal_status="FAILED",
                    score=20.0,
                    evidence_summary="fabricated",
                    evidence_id="event-1",
                    message_sha256="b" * 64,
                )

    def test_cycle_extremes_cli_reports_best_and_worst_for_current_model(self):
        guard = load_module()
        import json, subprocess
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            model = guard.scoring_model_sha256(ROOT)
            append_attested_cycle(guard, repo, {"controller_session_id": "c1", "record_kind": "cycle", "cycle_id": "best", "terminal_status": "CLOSED", "score": 93.0, "model_sha256": model, "evidence_summary": "best", "message_sha256": "f" * 64})
            append_attested_cycle(guard, repo, {"controller_session_id": "c1", "record_kind": "cycle", "cycle_id": "worst", "terminal_status": "FAILED", "score": 61.0, "model_sha256": model, "evidence_summary": "worst", "message_sha256": "1" * 64})
            guard.record_model_read(repo, skill_root=ROOT)
            completed = subprocess.run(["python3", str(SCRIPT), "cycle-extremes", "--repo", str(repo), "--controller-session", "c1"], check=True, capture_output=True, text=True)
            payload = json.loads(completed.stdout)
            self.assertEqual("best", payload["best"]["cycle_id"])
            self.assertEqual("worst", payload["worst"]["cycle_id"])


    def test_cycle_extremes_cli_fails_closed_without_exact_model_receipt(self):
        guard = load_module()
        import subprocess
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            model = guard.scoring_model_sha256(ROOT)
            guard.append_score_history(repo, {"controller_session_id": "c1", "record_kind": "cycle", "cycle_id": "best", "terminal_status": "CLOSED", "score": 93.0, "model_sha256": model, "evidence_summary": "best", "message_sha256": "f" * 64})
            completed = subprocess.run(["python3", str(SCRIPT), "cycle-extremes", "--repo", str(repo), "--controller-session", "c1"], check=False, capture_output=True, text=True)
            self.assertNotEqual(0, completed.returncode)
            self.assertNotIn('"score": 93.0', completed.stdout)
            self.assertIn("controller scoring blocked", completed.stderr)


    def test_finalize_cycle_candidate_rejects_empty_controller_session(self):
        guard = load_module()
        import subprocess
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            guard.record_model_read(repo, skill_root=ROOT)
            with self.assertRaisesRegex(ValueError, "controller"):
                guard.finalize_cycle_candidate(
                    repo, skill_root=ROOT, controller_session_id="", turn_id="t1",
                    cycle_id="cycle-1", terminal_status="CLOSED", score=80.0,
                    evidence_summary="reviewer pass", message_sha256="a" * 64,
                )

    def test_finalize_cycle_candidate_rejects_missing_evidence_summary(self):
        guard = load_module()
        import subprocess
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            guard.record_model_read(repo, skill_root=ROOT)
            with self.assertRaisesRegex(ValueError, "evidence"):
                guard.finalize_cycle_candidate(
                    repo, skill_root=ROOT, controller_session_id="c1", turn_id="t1",
                    cycle_id="cycle-1", terminal_status="CLOSED", score=80.0,
                    evidence_summary=" ", message_sha256="a" * 64,
                )

    def test_finalize_cycle_candidate_rejects_invalid_message_reference(self):
        guard = load_module()
        import subprocess
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            guard.record_model_read(repo, skill_root=ROOT)
            with self.assertRaisesRegex(ValueError, "message"):
                guard.finalize_cycle_candidate(
                    repo, skill_root=ROOT, controller_session_id="c1", turn_id="t1",
                    cycle_id="cycle-1", terminal_status="CLOSED", score=80.0,
                    evidence_summary="reviewer pass", message_sha256="short",
                )

    def test_cycle_extremes_ignore_malformed_cycle_records(self):
        guard = load_module()
        import subprocess
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            model = guard.scoring_model_sha256(ROOT)
            valid = {
                "controller_session_id": "c1", "record_kind": "cycle", "cycle_id": "valid",
                "terminal_status": "CLOSED", "score": 80.0, "model_sha256": model,
                "evidence_summary": "reviewed and integrated", "message_sha256": "a" * 64,
            }
            append_attested_cycle(guard, repo, valid)
            for record in (
                {**valid, "cycle_id": "", "score": 100.0},
                {**valid, "cycle_id": "no-evidence", "score": 99.0, "evidence_summary": ""},
                {**valid, "cycle_id": "no-message", "score": 1.0, "message_sha256": None},
                {**valid, "cycle_id": "too-high", "score": 101.0},
                {**valid, "cycle_id": "too-low", "score": -1.0},
            ):
                guard.append_score_history(repo, record)
            extremes = guard.cycle_score_extremes(repo, controller_session_id="c1", model_sha256=model)
            self.assertEqual("valid", extremes["best"]["cycle_id"])
            self.assertEqual("valid", extremes["worst"]["cycle_id"])

    def test_cycle_extremes_refuse_blank_controller_identity(self):
        guard = load_module()
        import subprocess
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            model = guard.scoring_model_sha256(ROOT)
            guard.append_score_history(repo, {
                "controller_session_id": "", "record_kind": "cycle", "cycle_id": "anonymous",
                "terminal_status": "CLOSED", "score": 90.0, "model_sha256": model,
                "evidence_summary": "anonymous", "message_sha256": "a" * 64,
            })
            self.assertEqual(
                {"best": None, "worst": None},
                guard.cycle_score_extremes(repo, controller_session_id="", model_sha256=model),
            )


if __name__ == "__main__":
    unittest.main()
