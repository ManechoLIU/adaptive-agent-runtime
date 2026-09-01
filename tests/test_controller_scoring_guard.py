from pathlib import Path
import importlib.util
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
            guard.record_model_read(repo, skill_root=ROOT)
            target = guard.receipt_path(repo)
            value = json.loads(target.read_text(encoding="utf-8"))
            value["read_at"] = (datetime.now(timezone.utc) - timedelta(seconds=guard.RECEIPT_MAX_AGE_SECONDS + 1)).isoformat()
            target.write_text(json.dumps(value), encoding="utf-8")
            self.assertTrue(any("expired" in error for error in guard.score_guard_errors(repo, skill_root=ROOT)))

            guard.record_model_read(repo, skill_root=ROOT)
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
            guard.record_model_read(repo, skill_root=ROOT)
            record = guard.finalize_score(
                repo, skill_root=ROOT, controller_session_id="controller-1", turn_id="web-turn-1",
                score=86.0, window_summary="最近两个闭合控制事件 + 24h 异常", message_sha256="abc",
            )
            self.assertEqual(86.0, record["score"])
            self.assertFalse(guard.receipt_path(repo).exists())
            self.assertEqual(86.0, guard.latest_score_history(repo, controller_session_id="controller-1")["score"])

    def test_finalize_score_fails_closed_without_valid_guard_and_writes_no_history(self):
        guard = load_module()
        import subprocess
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            with self.assertRaisesRegex(ValueError, "score-guard"):
                guard.finalize_score(
                    repo, skill_root=ROOT, controller_session_id="controller-1", turn_id="web-turn-1",
                    score=86.0, window_summary=None, message_sha256=None,
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
            guard.append_score_history(repo, {"controller_session_id": "c1", "record_kind": "cycle", "cycle_id": "good", "terminal_status": "CLOSED", "score": 94.0, "model_sha256": model, "evidence_summary": "closed", "message_sha256": "a" * 64})
            guard.append_score_history(repo, {"controller_session_id": "c1", "record_kind": "cycle", "cycle_id": "bad", "terminal_status": "FAILED", "score": 52.0, "model_sha256": model, "evidence_summary": "failed", "message_sha256": "b" * 64})
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
            guard.append_score_history(repo, {"controller_session_id": "c1", "record_kind": "cycle", "cycle_id": "valid", "terminal_status": "BLOCKED", "score": 70.0, "model_sha256": model, "evidence_summary": "blocked", "message_sha256": "e" * 64})
            extremes = guard.cycle_score_extremes(repo, controller_session_id="c1", model_sha256=model)
            self.assertEqual("valid", extremes["best"]["cycle_id"])
            self.assertEqual("valid", extremes["worst"]["cycle_id"])

    def test_finalize_cycle_score_requires_terminal_cycle_and_keeps_formal_history_clean(self):
        guard = load_module()
        import subprocess
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            guard.record_model_read(repo, skill_root=ROOT)
            with self.assertRaisesRegex(ValueError, "terminal"):
                guard.finalize_cycle_score(repo, skill_root=ROOT, controller_session_id="c1", turn_id="t1", cycle_id="cycle-1", terminal_status="ACTIVE", score=80.0, evidence_summary="still running", message_sha256="x")
            guard.record_model_read(repo, skill_root=ROOT)
            record = guard.finalize_cycle_score(repo, skill_root=ROOT, controller_session_id="c1", turn_id="t2", cycle_id="cycle-1", terminal_status="CLOSED", score=82.0, evidence_summary="closed with reviewer", message_sha256="a" * 64)
            self.assertEqual("cycle", record["record_kind"])
            self.assertIsNone(guard.latest_score_history(repo, controller_session_id="c1"))

    def test_cycle_extremes_cli_reports_best_and_worst_for_current_model(self):
        guard = load_module()
        import json, subprocess
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            model = guard.scoring_model_sha256(ROOT)
            guard.append_score_history(repo, {"controller_session_id": "c1", "record_kind": "cycle", "cycle_id": "best", "terminal_status": "CLOSED", "score": 93.0, "model_sha256": model, "evidence_summary": "best", "message_sha256": "f" * 64})
            guard.append_score_history(repo, {"controller_session_id": "c1", "record_kind": "cycle", "cycle_id": "worst", "terminal_status": "FAILED", "score": 61.0, "model_sha256": model, "evidence_summary": "worst", "message_sha256": "1" * 64})
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


    def test_finalize_cycle_score_rejects_empty_controller_session(self):
        guard = load_module()
        import subprocess
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            guard.record_model_read(repo, skill_root=ROOT)
            with self.assertRaisesRegex(ValueError, "controller"):
                guard.finalize_cycle_score(
                    repo, skill_root=ROOT, controller_session_id="", turn_id="t1",
                    cycle_id="cycle-1", terminal_status="CLOSED", score=80.0,
                    evidence_summary="reviewer pass", message_sha256="a" * 64,
                )

    def test_finalize_cycle_score_rejects_missing_evidence_summary(self):
        guard = load_module()
        import subprocess
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            guard.record_model_read(repo, skill_root=ROOT)
            with self.assertRaisesRegex(ValueError, "evidence"):
                guard.finalize_cycle_score(
                    repo, skill_root=ROOT, controller_session_id="c1", turn_id="t1",
                    cycle_id="cycle-1", terminal_status="CLOSED", score=80.0,
                    evidence_summary=" ", message_sha256="a" * 64,
                )

    def test_finalize_cycle_score_rejects_invalid_message_reference(self):
        guard = load_module()
        import subprocess
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            guard.record_model_read(repo, skill_root=ROOT)
            with self.assertRaisesRegex(ValueError, "message"):
                guard.finalize_cycle_score(
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
            guard.append_score_history(repo, valid)
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
