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
        ]
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                self.assertTrue(hook.is_controller_scoring_request(prompt))

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
            self.assertTrue(state["pending_scoring"])
            self.assertNotEqual(hook.scoring_model_sha256(skill), state["model_sha256"])

            _, refreshed = hook.evaluate_event(
                {"hook_event_name": "UserPromptSubmit", "session_id": "s1", "cwd": str(repo), "prompt": "给总控评分"},
                skill_root=skill, prior_state=state,
            )
            self.assertEqual(hook.scoring_model_sha256(skill), refreshed["model_sha256"])
            allowed, refreshed = hook.evaluate_event(
                {"hook_event_name": "Stop", "session_id": "s1", "cwd": str(repo), "last_assistant_message": "总控评分：80/100"},
                skill_root=skill, prior_state=refreshed,
            )
            self.assertEqual({}, allowed)
            self.assertFalse(refreshed["pending_scoring"])


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


class ControllerScoringOutputGateTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
