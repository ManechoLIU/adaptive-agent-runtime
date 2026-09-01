from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "controller_scoring_hook.py"


def load_module():
    spec = importlib.util.spec_from_file_location("controller_scoring_hook", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class ControllerScoringHookTests(unittest.TestCase):
    def test_user_prompt_submit_auto_injects_exact_scoring_model_and_marks_pending(self):
        hook = load_module()
        output, state = hook.evaluate_event(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "s1",
                "prompt": "给我下现在总控履职评分",
            },
            skill_root=ROOT,
            prior_state={},
        )
        context = output["hookSpecificOutput"]["additionalContext"]
        self.assertEqual("UserPromptSubmit", output["hookSpecificOutput"]["hookEventName"])
        self.assertIn("# Controller Performance Scoring", context)
        self.assertIn("七维评分模型", context)
        self.assertIn(hook.scoring_model_sha256(ROOT), context)
        self.assertTrue(state["pending_scoring"])
        self.assertEqual(hook.scoring_model_sha256(ROOT), state["model_sha256"])

    def test_non_scoring_prompt_is_ignored(self):
        hook = load_module()
        output, state = hook.evaluate_event(
            {"hook_event_name": "UserPromptSubmit", "session_id": "s1", "prompt": "现在项目进度怎么样"},
            skill_root=ROOT,
            prior_state={},
        )
        self.assertEqual({}, output)
        self.assertFalse(state.get("pending_scoring", False))

    def test_common_scoring_phrasings_activate_gate(self):
        hook = load_module()
        prompts = [
            "给总控评分",
            "评估下总控履职能力，给出具体评分",
            "现在总控多少分",
            "审计一下项目总控履职",
            "比较近期总控表现",
            "检查总控是不是假繁荣",
            "score the controller performance",
            "Audit the controller's recent performance",
            "Evaluate the orchestrator's recent performance",
            "评估一下总控最近表现",
            "评价一下总控最近履职情况",
        ]
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                self.assertTrue(hook.is_controller_scoring_request(prompt))

    def test_unrelated_controller_audits_do_not_activate_scoring_gate(self):
        hook = load_module()
        prompts = [
            "审计 controller_scoring_hook.py 的安全性",
            "项目总控要求审计第三方依赖",
            "请审计 Orchestrator API 的权限边界",
        ]
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                self.assertFalse(hook.is_controller_scoring_request(prompt))

    def test_stop_allows_and_clears_pending_state_when_exact_model_is_unchanged(self):
        hook = load_module()
        _, state = hook.evaluate_event(
            {"hook_event_name": "UserPromptSubmit", "session_id": "s1", "cwd": str(ROOT), "prompt": "给总控评分"},
            skill_root=ROOT, prior_state={},
        )
        output, state = hook.evaluate_event(
            {"hook_event_name": "Stop", "session_id": "s1", "cwd": str(ROOT), "last_assistant_message": "总控评分：82/100。"},
            skill_root=ROOT, prior_state=state,
        )
        self.assertEqual({}, output)
        self.assertFalse(state["pending_scoring"])

    def test_stop_blocks_if_installed_scoring_model_changed_after_injection(self):
        hook = load_module()
        import subprocess
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as sd:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            skill = Path(sd)
            (skill / "references").mkdir()
            model = skill / "references" / "controller-performance-scoring.md"
            model.write_text("model v1\n", encoding="utf-8")
            _, state = hook.evaluate_event(
                {"hook_event_name": "UserPromptSubmit", "session_id": "s1", "cwd": str(repo), "prompt": "给总控评分"},
                skill_root=skill, prior_state={},
            )
            model.write_text("model v2\n", encoding="utf-8")
            output, state = hook.evaluate_event(
                {"hook_event_name": "Stop", "session_id": "s1", "cwd": str(repo), "last_assistant_message": "总控评分：80/100"},
                skill_root=skill, prior_state=state,
            )
            self.assertEqual("block", output["decision"])
            self.assertIn("重新提交", output["reason"])
            self.assertNotIn("model v2", output["reason"])
            self.assertFalse(state["pending_scoring"])
            self.assertTrue(state["reinject_required"])
            self.assertNotEqual(hook.scoring_model_sha256(skill), state["model_sha256"])

            exit_output, state = hook.evaluate_event(
                {"hook_event_name": "Stop", "session_id": "s1", "cwd": str(repo), "last_assistant_message": "评分模型已变化，请重新提交评分请求。"},
                skill_root=skill, prior_state=state,
            )
            self.assertEqual({}, exit_output)
            self.assertTrue(state["reinject_required"])

            _, refreshed = hook.evaluate_event(
                {"hook_event_name": "UserPromptSubmit", "session_id": "s1", "cwd": str(repo), "prompt": "给总控评分"},
                skill_root=skill, prior_state=state,
            )
            self.assertFalse(refreshed.get("reinject_required", False))
            self.assertEqual(hook.scoring_model_sha256(skill), refreshed["model_sha256"])
            allowed, refreshed = hook.evaluate_event(
                {"hook_event_name": "Stop", "session_id": "s1", "cwd": str(repo), "last_assistant_message": "总控评分：80/100"},
                skill_root=skill, prior_state=refreshed,
            )
            self.assertEqual({}, allowed)
            self.assertFalse(refreshed["pending_scoring"])


    def test_scoring_state_is_bound_to_turn_id_and_stale_turn_is_cleared(self):
        hook = load_module()
        _, state = hook.evaluate_event(
            {"hook_event_name": "UserPromptSubmit", "session_id": "s1", "turn_id": "score-turn", "cwd": str(ROOT), "prompt": "给总控评分"},
            skill_root=ROOT, prior_state={},
        )
        self.assertEqual("score-turn", state["turn_id"])
        output, state = hook.evaluate_event(
            {"hook_event_name": "UserPromptSubmit", "session_id": "s1", "turn_id": "next-turn", "cwd": str(ROOT), "prompt": "现在项目进度怎么样"},
            skill_root=ROOT, prior_state=state,
        )
        self.assertEqual({}, output)
        self.assertFalse(state.get("pending_scoring", False))
        self.assertEqual("next-turn", state.get("turn_id"))
        output, _ = hook.evaluate_event(
            {"hook_event_name": "Stop", "session_id": "s1", "turn_id": "next-turn", "cwd": str(ROOT), "last_assistant_message": "Overall: 82/100"},
            skill_root=ROOT, prior_state=state,
        )
        self.assertEqual({}, output)

    def test_stop_blocks_when_receipt_validation_raises(self):
        hook = load_module()
        from unittest.mock import patch
        _, state = hook.evaluate_event(
            {"hook_event_name": "UserPromptSubmit", "session_id": "s1", "turn_id": "t1", "cwd": str(ROOT), "prompt": "给总控评分"},
            skill_root=ROOT, prior_state={},
        )
        with patch.object(hook, "finalize_score", side_effect=ValueError("receipt vanished")):
            output, state = hook.evaluate_event(
                {"hook_event_name": "Stop", "session_id": "s1", "turn_id": "t1", "cwd": str(ROOT), "last_assistant_message": "当前总控履职评分：82/100。"},
                skill_root=ROOT, prior_state=state,
            )
        self.assertEqual("block", output["decision"])
        self.assertIn("score-guard", output["reason"])
        self.assertTrue(state["pending_scoring"])

    def test_run_hook_blocks_when_scoring_state_cannot_be_persisted(self):
        import json
        import os
        import subprocess
        event = {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "s1",
            "cwd": str(ROOT),
            "prompt": "给总控评分",
        }
        env = dict(os.environ)
        env["AD_SCORING_STATE_DIR"] = "/dev/null"
        completed = subprocess.run(
            ["python3", str(SCRIPT)],
            input=json.dumps(event, ensure_ascii=False),
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(0, completed.returncode)
        output = json.loads(completed.stdout)
        self.assertEqual("block", output["decision"])
        self.assertIn("persist", output["reason"])

    def test_run_hook_blocks_if_successful_scoring_stop_cannot_persist_clear_state(self):
        import io
        import json
        from contextlib import redirect_stdout
        from unittest.mock import patch
        hook = load_module()
        _, prior = hook.evaluate_event(
            {"hook_event_name": "UserPromptSubmit", "session_id": "s1", "cwd": str(ROOT), "prompt": "给总控评分"},
            skill_root=ROOT, prior_state={},
        )
        event = {
            "hook_event_name": "Stop",
            "session_id": "s1",
            "cwd": str(ROOT),
            "last_assistant_message": "总分：82/100。",
        }
        stdout = io.StringIO()
        with patch.object(hook, "_read_state", return_value=prior), \
             patch.object(hook, "_write_state", side_effect=OSError("disk full")), \
             patch.object(hook.sys, "stdin", io.StringIO(json.dumps(event, ensure_ascii=False))), \
             redirect_stdout(stdout):
            self.assertEqual(0, hook.run_hook())
        output = json.loads(stdout.getvalue())
        self.assertEqual("block", output["decision"])
        self.assertIn("persist", output["reason"])

    def test_successful_formal_score_appends_machine_history_after_guard_passes(self):
        import json, subprocess
        hook = load_module()
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            _, state = hook.evaluate_event(
                {"hook_event_name": "UserPromptSubmit", "session_id": "controller-1", "turn_id": "turn-1", "cwd": str(repo), "prompt": "给总控评分"},
                skill_root=ROOT, prior_state={},
            )
            output, state = hook.evaluate_event(
                {"hook_event_name": "Stop", "session_id": "controller-1", "turn_id": "turn-1", "cwd": str(repo),
                 "last_assistant_message": "当前总控履职评分：86/100。\n评估窗口：最近 24 小时内最多 5 个有效控制事件 + 当前重大未闭环异常单独检查。"},
                skill_root=ROOT, prior_state=state,
            )
            self.assertEqual({}, output)
            history = hook.latest_score_history(repo, controller_session_id="controller-1")
            self.assertEqual(86.0, history["score"])
            self.assertEqual("controller-1", history["controller_session_id"])
            self.assertEqual("turn-1", history["turn_id"])
            self.assertEqual(hook.scoring_model_sha256(ROOT), history["model_sha256"])
            self.assertIn("最近 24 小时内最多 5 个有效控制事件", history["window_summary"])
            self.assertNotIn("当前总控履职评分", history.get("window_summary", ""))

    def test_failed_score_guard_does_not_pollute_score_history(self):
        import subprocess
        hook = load_module()
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            _, state = hook.evaluate_event(
                {"hook_event_name": "UserPromptSubmit", "session_id": "controller-1", "turn_id": "turn-1", "cwd": str(repo), "prompt": "给总控评分"},
                skill_root=ROOT, prior_state={},
            )
            Path(state["receipt_path"]).unlink()
            output, _ = hook.evaluate_event(
                {"hook_event_name": "Stop", "session_id": "controller-1", "turn_id": "turn-1", "cwd": str(repo),
                 "last_assistant_message": "当前总控履职评分：86/100。"},
                skill_root=ROOT, prior_state=state,
            )
            self.assertEqual("block", output["decision"])
            self.assertIsNone(hook.latest_score_history(repo, controller_session_id="controller-1"))

    def test_install_hooks_preserves_existing_stop_and_adds_scoring_handlers_idempotently(self):
        hook = load_module()
        with tempfile.TemporaryDirectory() as td:
            config = Path(td) / "hooks.json"
            config.write_text('{"hooks":{"Stop":[{"hooks":[{"type":"command","command":"python3 lifecycle_hook.py"}]}]}}', encoding="utf-8")
            hook.install_hooks(config, script_path=SCRIPT, python_executable="/usr/bin/python3")
            hook.install_hooks(config, script_path=SCRIPT, python_executable="/usr/bin/python3")
            import json
            value = json.loads(config.read_text(encoding="utf-8"))
            self.assertEqual(1, len(value["hooks"]["UserPromptSubmit"]))
            scoring_stop = [group for group in value["hooks"]["Stop"] if "controller_scoring_hook.py" in str(group)]
            self.assertEqual(1, len(scoring_stop))
            self.assertTrue(any("lifecycle_hook.py" in str(group) for group in value["hooks"]["Stop"]))
            handler = value["hooks"]["UserPromptSubmit"][0]["hooks"][0]
            self.assertEqual(0, handler["additionalContextLimit"])

    def test_single_cycle_score_is_persisted_as_cycle_not_formal_history(self):
        import subprocess
        hook = load_module()
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            _, state = hook.evaluate_event(
                {"hook_event_name": "UserPromptSubmit", "session_id": "controller-1", "turn_id": "turn-cycle", "cwd": str(repo), "prompt": "给总控这个单回合闭环评分"},
                skill_root=ROOT, prior_state={},
            )
            output, state = hook.evaluate_event(
                {"hook_event_name": "Stop", "session_id": "controller-1", "turn_id": "turn-cycle", "cwd": str(repo),
                 "last_assistant_message": "单回合诊断评分：91/100。\n控制回合：M1-F4-C-SERVER-GATE\n回合终态：CLOSED\n证据摘要：reviewer pass then main。"},
                skill_root=ROOT, prior_state=state,
            )
            self.assertEqual({}, output)
            self.assertIsNone(hook.latest_score_history(repo, controller_session_id="controller-1"))
            extremes = hook.cycle_score_extremes(repo, controller_session_id="controller-1", model_sha256=hook.scoring_model_sha256(ROOT))
            self.assertEqual(91.0, extremes["best"]["score"])
            self.assertEqual("M1-F4-C-SERVER-GATE", extremes["best"]["cycle_id"])


class ControllerScoringOutputGateTests(unittest.TestCase):
    def test_output_detector_requires_scoring_semantics(self):
        hook = load_module()
        self.assertFalse(hook.looks_like_controller_score_output("总分：82/100。"))
        self.assertTrue(hook.looks_like_controller_score_output("当前总控履职评分：82/100。"))
        self.assertTrue(hook.looks_like_controller_score_output("Controller performance score: 82/100."))
        self.assertFalse(hook.looks_like_controller_score_output("Benchmark score: 82/100."))
        self.assertFalse(hook.looks_like_controller_score_output("controller failed in 50/100 requests"))

    def test_stop_blocks_scoring_output_when_no_scoring_state_exists(self):
        hook = load_module()
        output, _ = hook.evaluate_event(
            {
                "hook_event_name": "Stop",
                "session_id": "s1",
                "cwd": str(ROOT),
                "last_assistant_message": "当前总控履职评分：82/100。",
            },
            skill_root=ROOT,
            prior_state={},
        )
        self.assertEqual("block", output["decision"])
        self.assertIn("score-guard", output["reason"])

    def test_user_prompt_submit_records_shared_git_receipt_and_stop_requires_it(self):
        import json, subprocess
        hook = load_module()
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            linked = Path(td) / "linked"
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
            (repo / "x").write_text("x", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "x"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"], check=True)
            subprocess.run(["git", "-C", str(repo), "worktree", "add", "-qb", "linked", str(linked)], check=True)
            _, state = hook.evaluate_event(
                {"hook_event_name": "UserPromptSubmit", "session_id": "s1", "cwd": str(linked), "prompt": "给总控评分"},
                skill_root=ROOT,
                prior_state={},
            )
            self.assertTrue(Path(state["receipt_path"]).is_file())
            Path(state["receipt_path"]).unlink()
            output, state2 = hook.evaluate_event(
                {"hook_event_name": "Stop", "session_id": "s1", "cwd": str(linked), "last_assistant_message": "总控评分：82/100"},
                skill_root=ROOT,
                prior_state=state,
            )
            self.assertEqual("block", output["decision"])
            self.assertIn("score-guard", output["reason"])
            self.assertTrue(state2["pending_scoring"])


    def test_output_detector_recognizes_standalone_cycle_score(self):
        hook = load_module()
        self.assertTrue(hook.looks_like_controller_score_output("单回合诊断评分：91/100"))

    def test_stop_blocks_cycle_score_when_no_scoring_state_exists(self):
        hook = load_module()
        output, _ = hook.evaluate_event(
            {
                "hook_event_name": "Stop",
                "session_id": "controller-1",
                "turn_id": "turn-cycle",
                "cwd": str(ROOT),
                "last_assistant_message": "单回合诊断评分：91/100\n控制回合：cycle-1\n回合终态：CLOSED\n证据摘要：reviewed and integrated",
            },
            skill_root=ROOT,
            prior_state={},
        )
        self.assertEqual("block", output["decision"])
        self.assertIn("score-guard", output["reason"])


    def test_cycle_request_blocks_formal_score_output_mode_mismatch(self):
        import subprocess
        hook = load_module()
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            _, state = hook.evaluate_event(
                {"hook_event_name": "UserPromptSubmit", "session_id": "controller-1", "turn_id": "turn-cycle", "cwd": str(repo), "prompt": "给总控这个单回合闭环评分"},
                skill_root=ROOT, prior_state={},
            )
            output, _ = hook.evaluate_event(
                {"hook_event_name": "Stop", "session_id": "controller-1", "turn_id": "turn-cycle", "cwd": str(repo),
                 "last_assistant_message": "当前总控履职评分：91/100。"},
                skill_root=ROOT, prior_state=state,
            )
            self.assertEqual("block", output.get("decision"))
            self.assertIn("mode", output.get("reason", ""))
            self.assertIsNone(hook.latest_score_history(repo, controller_session_id="controller-1"))

    def test_cycle_diagnostic_language_triggers_scoring_gate(self):
        hook = load_module()
        self.assertTrue(hook.is_controller_scoring_request("分析总控的最佳闭环和最差闭环"))
        self.assertTrue(hook.is_controller_scoring_request("做一次总控单回合诊断"))

    def test_formal_request_blocks_cycle_score_when_regexes_overlap(self):
        import subprocess
        hook = load_module()
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            _, state = hook.evaluate_event(
                {"hook_event_name": "UserPromptSubmit", "session_id": "controller-1", "turn_id": "turn-formal-overlap", "cwd": str(repo), "prompt": "给总控正式评分"},
                skill_root=ROOT, prior_state={},
            )
            output, _ = hook.evaluate_event(
                {"hook_event_name": "Stop", "session_id": "controller-1", "turn_id": "turn-formal-overlap", "cwd": str(repo),
                 "last_assistant_message": "总控单回合诊断评分：91/100。\n控制回合：cycle-1\n回合终态：CLOSED\n证据摘要：reviewed"},
                skill_root=ROOT, prior_state=state,
            )
            self.assertEqual("block", output.get("decision"))
            self.assertIn("mode", output.get("reason", ""))
            self.assertIsNone(hook.latest_score_history(repo, controller_session_id="controller-1"))

    def test_cycle_request_blocks_distinct_formal_score_in_same_response(self):
        import subprocess
        hook = load_module()
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            _, state = hook.evaluate_event(
                {"hook_event_name": "UserPromptSubmit", "session_id": "controller-1", "turn_id": "turn-cycle-mixed", "cwd": str(repo), "prompt": "给总控做单回合诊断评分"},
                skill_root=ROOT, prior_state={},
            )
            output, _ = hook.evaluate_event(
                {"hook_event_name": "Stop", "session_id": "controller-1", "turn_id": "turn-cycle-mixed", "cwd": str(repo),
                 "last_assistant_message": "单回合诊断评分：91/100。\n当前总控履职评分：82/100。\n控制回合：cycle-1\n回合终态：CLOSED\n证据摘要：reviewed"},
                skill_root=ROOT, prior_state=state,
            )
            self.assertEqual("block", output.get("decision"))
            self.assertIn("mode", output.get("reason", ""))
            self.assertEqual(
                {"best": None, "worst": None},
                hook.cycle_score_extremes(repo, controller_session_id="controller-1", model_sha256=hook.scoring_model_sha256(ROOT)),
            )

    def test_cycle_request_blocks_shared_controller_subject_with_second_formal_score(self):
        import subprocess
        hook = load_module()
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            _, state = hook.evaluate_event(
                {"hook_event_name": "UserPromptSubmit", "session_id": "controller-1", "turn_id": "turn-cycle-shared-subject", "cwd": str(repo), "prompt": "给总控做单回合诊断评分"},
                skill_root=ROOT, prior_state={},
            )
            output, _ = hook.evaluate_event(
                {"hook_event_name": "Stop", "session_id": "controller-1", "turn_id": "turn-cycle-shared-subject", "cwd": str(repo),
                 "last_assistant_message": "总控：单回合诊断评分：91/100，正式履职评分：82/100。\n控制回合：cycle-1\n回合终态：CLOSED\n证据摘要：reviewed"},
                skill_root=ROOT, prior_state=state,
            )
            self.assertEqual("block", output.get("decision"))
            self.assertIn("mode", output.get("reason", ""))
            self.assertEqual(
                {"best": None, "worst": None},
                hook.cycle_score_extremes(repo, controller_session_id="controller-1", model_sha256=hook.scoring_model_sha256(ROOT)),
            )

    def test_cycle_request_allows_single_cycle_score_even_when_regexes_overlap(self):
        import subprocess
        hook = load_module()
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            _, state = hook.evaluate_event(
                {"hook_event_name": "UserPromptSubmit", "session_id": "controller-1", "turn_id": "turn-cycle-overlap", "cwd": str(repo), "prompt": "给总控做单回合诊断评分"},
                skill_root=ROOT, prior_state={},
            )
            output, _ = hook.evaluate_event(
                {"hook_event_name": "Stop", "session_id": "controller-1", "turn_id": "turn-cycle-overlap", "cwd": str(repo),
                 "last_assistant_message": "总控单回合诊断评分：91/100。\n控制回合：cycle-1\n回合终态：CLOSED\n证据摘要：reviewed"},
                skill_root=ROOT, prior_state=state,
            )
            self.assertEqual({}, output)

    def test_formal_request_blocks_cycle_score_output_mode_mismatch(self):
        import subprocess
        hook = load_module()
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            _, state = hook.evaluate_event(
                {"hook_event_name": "UserPromptSubmit", "session_id": "controller-1", "turn_id": "turn-formal", "cwd": str(repo), "prompt": "给总控正式评分"},
                skill_root=ROOT, prior_state={},
            )
            output, _ = hook.evaluate_event(
                {"hook_event_name": "Stop", "session_id": "controller-1", "turn_id": "turn-formal", "cwd": str(repo),
                 "last_assistant_message": "单回合诊断评分：91/100。\n控制回合：cycle-1\n回合终态：CLOSED\n证据摘要：reviewed"},
                skill_root=ROOT, prior_state=state,
            )
            self.assertEqual("block", output.get("decision"))
            self.assertIn("mode", output.get("reason", ""))
            self.assertEqual(
                {"best": None, "worst": None},
                hook.cycle_score_extremes(repo, controller_session_id="controller-1", model_sha256=hook.scoring_model_sha256(ROOT)),
            )


if __name__ == "__main__":
    unittest.main()
