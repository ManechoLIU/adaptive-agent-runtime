from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class ProjectContextGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.repo = root / "project"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)], check=True)
        (self.repo / "AGENTS.md").write_text(
            "# Project Rules\n\nCurrent project rule: FACT_FIRST_V2.\n"
            "Use [Current Score Model](docs/current-score-model.md) for formal scoring.\n",
            encoding="utf-8",
        )
        (self.repo / "SKILL.md").write_text(
            "# Project Skill\n\nProject-specific workflow is CURRENT_PROJECT_SKILL.\n",
            encoding="utf-8",
        )
        (self.repo / "TASK_LEDGER.md").write_text(
            "# Task Ledger\n\n| Task | State |\n|---|---|\n| T-1 | READY |\n",
            encoding="utf-8",
        )
        docs = self.repo / "docs"
        docs.mkdir()
        (docs / "current-score-model.md").write_text(
            "# Current Score Model\n\n评分模型 CURRENT_MODEL_V2，七维，0-100。\n",
            encoding="utf-8",
        )
        (self.repo / "tracked.txt").write_text("tracked\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(
            [
                "git", "-C", str(self.repo),
                "-c", "user.name=Test", "-c", "user.email=test@example.com",
                "commit", "-qm", "init",
            ],
            check=True,
        )
        self.skill = root / "runtime-skill"
        self.skill.mkdir()
        (self.skill / "SKILL.md").write_text(
            "# Adaptive Agent Runtime\n\nRuntime rule: RUNTIME_FACT_FIRST.\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def hook(self):
        from scripts import project_context_guard
        return project_context_guard

    def test_new_session_project_governance_question_requires_initialized_current_rules(self):
        hook = self.hook()
        output, state = hook.evaluate_event(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "reader-session",
                "turn_id": "t1",
                "cwd": str(self.repo),
                "prompt": "这个项目当前治理规则是什么？",
            },
            skill_root=self.skill,
            prior_state={},
        )
        self.assertTrue(state["pending_project_fact_turn"])
        receipt = state["project_context_receipt"]
        self.assertEqual(receipt["state"], "initialized")
        self.assertEqual(receipt["project_root"], str(self.repo.resolve()))
        self.assertEqual(receipt["sources"]["agents"]["status"], "verified")
        self.assertEqual(receipt["sources"]["project_skill"]["status"], "verified")
        self.assertEqual(receipt["sources"]["runtime_skill"]["status"], "verified")
        context = output["hookSpecificOutput"]["additionalContext"]
        self.assertIn("FACT_FIRST_V2", context)
        self.assertIn("CURRENT_PROJECT_SKILL", context)
        self.assertIn("RUNTIME_FACT_FIRST", context)
        self.assertIn("verified_facts=", context)
        self.assertIn("unknown_facts=", context)

    def test_existing_scoring_model_request_must_resolve_real_current_definition(self):
        hook = self.hook()
        output, state = hook.evaluate_event(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "reader-session",
                "turn_id": "score",
                "cwd": str(self.repo),
                "prompt": "按照治理体系现有评分模型评分，不要自己设计模型",
            },
            skill_root=self.skill,
            prior_state={},
        )
        mechanism = state["mechanism_resolution"]
        self.assertEqual(mechanism["state"], "found")
        self.assertEqual(
            Path(mechanism["path"]).resolve(),
            (self.repo / "docs" / "current-score-model.md").resolve(),
        )
        self.assertIn("CURRENT_MODEL_V2", output["hookSpecificOutput"]["additionalContext"])
        self.assertNotIn("approximate", mechanism)

    def test_missing_existing_mechanism_is_not_found_and_stop_blocks_fabrication(self):
        hook = self.hook()
        _, state = hook.evaluate_event(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "reader-session",
                "turn_id": "missing",
                "cwd": str(self.repo),
                "prompt": "按照项目规定的量子审计评分矩阵给我评分",
            },
            skill_root=self.skill,
            prior_state={},
        )
        self.assertEqual(state["mechanism_resolution"]["state"], "not_found")
        blocked, state = hook.evaluate_event(
            {
                "hook_event_name": "Stop",
                "session_id": "reader-session",
                "turn_id": "missing",
                "cwd": str(self.repo),
                "last_assistant_message": "量子审计评分矩阵共五维，我给 88/100。",
            },
            skill_root=self.skill,
            prior_state=state,
        )
        self.assertEqual(blocked["decision"], "block")
        self.assertIn("UNKNOWN", blocked["reason"])
        allowed, state = hook.evaluate_event(
            {
                "hook_event_name": "Stop",
                "session_id": "reader-session",
                "turn_id": "missing",
                "cwd": str(self.repo),
                "last_assistant_message": "UNKNOWN / NOT FOUND：当前权威事实源中没有找到该模型定义。",
            },
            skill_root=self.skill,
            prior_state=state,
        )
        self.assertEqual(allowed, {})
        self.assertFalse(state["pending_project_fact_turn"])

    def test_current_fact_source_overrides_old_model_claim_in_prompt_history(self):
        hook = self.hook()
        output, state = hook.evaluate_event(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "reader-session",
                "turn_id": "history",
                "cwd": str(self.repo),
                "prompt": (
                    "聊天历史里说评分模型是 OLD_MODEL_V1，"
                    "但请按治理体系现有评分模型回答。"
                ),
            },
            skill_root=self.skill,
            prior_state={},
        )
        context = output["hookSpecificOutput"]["additionalContext"]
        self.assertIn("CURRENT_MODEL_V2", context)
        self.assertEqual(state["mechanism_resolution"]["state"], "found")
        self.assertNotEqual(
            state["mechanism_resolution"]["sha256"],
            hashlib.sha256(b"OLD_MODEL_V1").hexdigest(),
        )

    def test_unverified_guess_is_revoked_and_same_turn_auto_initializes_for_correction(self):
        hook = self.hook()
        blocked, state = hook.evaluate_event(
            {
                "hook_event_name": "Stop",
                "session_id": "reader-session",
                "turn_id": "guess",
                "cwd": str(self.repo),
                "last_assistant_message": "当前项目治理规则确定就是 OLD_GUESS。",
            },
            skill_root=self.skill,
            prior_state={},
        )
        self.assertEqual(blocked["decision"], "block")
        self.assertIn("撤销", blocked["reason"])
        self.assertIn("FACT_FIRST_V2", blocked["reason"])
        self.assertTrue(state["correction_required"])
        self.assertEqual(state["project_context_receipt"]["state"], "initialized")

    def test_read_only_governance_or_scoring_query_does_not_require_controller_identity(self):
        hook = self.hook()
        with patch(
            "scripts.controller_target_guard.resolve_execution_target",
            side_effect=AssertionError("read-only query must not enter Controller identity gate"),
        ):
            output, state = hook.evaluate_event(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "external-evaluator",
                    "turn_id": "read-only",
                    "cwd": str(self.repo),
                    "prompt": "按照治理体系现有评分模型评价当前 Controller",
                },
                skill_root=self.skill,
                prior_state={},
            )
        self.assertIn("additionalContext", output["hookSpecificOutput"])
        self.assertFalse(state.get("controller_identity_required", False))

    def test_controller_exclusive_target_guard_remains_strict(self):
        from scripts.controller_target_guard import resolve_execution_target
        registry = Path(self.tmp.name) / "empty-registry.json"
        registry.write_text("{}\n", encoding="utf-8")
        with self.assertRaises(PermissionError):
            resolve_execution_target(
                repo=self.repo,
                host="desktop_codex",
                registry_path=registry,
            )

    def test_session_start_resume_and_compact_all_reinitialize_current_context(self):
        hook = self.hook()
        for index, source in enumerate(("startup", "resume", "compact")):
            with self.subTest(source=source):
                output, state = hook.evaluate_event(
                    {
                        "hook_event_name": "SessionStart",
                        "source": source,
                        "session_id": f"s-{index}",
                        "cwd": str(self.repo),
                    },
                    skill_root=self.skill,
                    prior_state={"project_context_receipt": {"state": "stale"}},
                )
                self.assertEqual(
                    state["project_context_receipt"]["state"],
                    "initialized",
                )
                self.assertIn(
                    "FACT_FIRST_V2",
                    output["hookSpecificOutput"]["additionalContext"],
                )

    def test_source_change_before_stop_fails_closed_and_refreshes_for_same_turn_correction(self):
        hook = self.hook()
        _, state = hook.evaluate_event(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "reader-session",
                "turn_id": "changed",
                "cwd": str(self.repo),
                "prompt": "当前项目规则是什么？",
            },
            skill_root=self.skill,
            prior_state={},
        )
        (self.repo / "AGENTS.md").write_text(
            "# Project Rules\n\nCurrent project rule: FACT_FIRST_V3.\n",
            encoding="utf-8",
        )
        blocked, state = hook.evaluate_event(
            {
                "hook_event_name": "Stop",
                "session_id": "reader-session",
                "turn_id": "changed",
                "cwd": str(self.repo),
                "last_assistant_message": "当前规则是 FACT_FIRST_V2。",
            },
            skill_root=self.skill,
            prior_state=state,
        )
        self.assertEqual(blocked["decision"], "block")
        self.assertIn("FACT_FIRST_V3", blocked["reason"])
        self.assertTrue(state["correction_required"])


class ProjectContextRestoreTests(unittest.TestCase):
    def test_web_restore_payload_includes_project_and_runtime_rule_initialization(self):
        from scripts import web_lifecycle_bridge as bridge
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            (repo / "AGENTS.md").write_text("rules-now\n", encoding="utf-8")
            (repo / "SKILL.md").write_text("project-skill-now\n", encoding="utf-8")
            (repo / "TASK_LEDGER.md").write_text("# Ledger\n", encoding="utf-8")
            (repo / "README.md").write_text("x\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(
                [
                    "git", "-C", str(repo),
                    "-c", "user.name=Test", "-c", "user.email=test@example.com",
                    "commit", "-qm", "init",
                ],
                check=True,
            )
            registry = root / "registry.json"
            registry.write_text(
                json.dumps({"controller-1": str(repo.resolve())}),
                encoding="utf-8",
            )
            payload = bridge.web_session_restore_payload(repo, registry)
            self.assertIn("project_context", payload)
            self.assertEqual(payload["project_context"]["state"], "initialized")
            names = [item["name"] for item in payload["documents"]]
            self.assertIn("AGENTS.md", names)
            self.assertIn("SKILL.md", names)


if __name__ == "__main__":
    unittest.main()
