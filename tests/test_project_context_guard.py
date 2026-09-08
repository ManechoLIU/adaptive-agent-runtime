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

    def test_project_context_separates_unique_controller_from_unverified_web_session(self):
        module = self.hook()
        registry = Path(self.tmp.name) / "controllers.json"
        registry.write_text(
            json.dumps({"controller-1": str(self.repo.resolve())}),
            encoding="utf-8",
        )
        receipt = module.initialize_project_context(
            self.repo,
            skill_root=self.skill,
            controller_registry_path=registry,
            controller_host="web",
            source_session_id=None,
        )
        project = receipt["project_controller_state"]
        binding = receipt["session_binding_state"]
        self.assertEqual(project["project_controller"], "EXISTING")
        self.assertEqual(project["controller_id"], "controller-1")
        self.assertEqual(project["uniqueness"], "UNIQUE")
        self.assertEqual(binding["verification"], "UNVERIFIED")
        self.assertEqual(binding["reason"], "HOST_SESSION_ID_UNAVAILABLE")
        self.assertEqual(
            binding["recovery"], "SAME_CONTROLLER_SESSION_RECOVERY_REQUIRED"
        )
        self.assertFalse(receipt["controller_actions_allowed"])
        self.assertFalse(receipt["create_new_controller_allowed"])
        context = module._context_text(receipt)
        self.assertIn('"project_controller": "EXISTING"', context)
        self.assertIn('"verification": "UNVERIFIED"', context)
        self.assertIn("do not create, reappoint, replace", context)

    def test_missing_required_identity_capability_reports_contract_drift_without_revoking_controller(self):
        module = self.hook()
        registry = Path(self.tmp.name) / "controllers.json"
        registry.write_text(json.dumps({"controller-1": str(self.repo.resolve())}), encoding="utf-8")
        (self.repo / "AGENTS.md").write_text(
            "# Project Rules\n\nadaptive_agent_runtime_required_capabilities: controller_identity_projection,missing_identity_v99\n",
            encoding="utf-8",
        )

        receipt = module.initialize_project_context(
            self.repo,
            skill_root=self.skill,
            controller_registry_path=registry,
            controller_host="web",
            source_session_id=None,
        )

        self.assertEqual(receipt["runtime_contract_state"], "RUNTIME_CONTRACT_DRIFT")
        self.assertEqual(receipt["missing_identity_capabilities"], ["missing_identity_v99"])
        self.assertEqual(receipt["identity_state"], "DEGRADED")
        self.assertEqual(receipt["project_controller_state"]["controller_id"], "controller-1")
        self.assertFalse(receipt["create_new_controller_allowed"])
        self.assertFalse(receipt["controller_actions_allowed"])
        context = module._context_text(receipt)
        self.assertIn("identity_state=DEGRADED", context)
        self.assertIn("runtime_contract_state=RUNTIME_CONTRACT_DRIFT", context)
        self.assertIn("safe_control_actions_allowed=true", context)
        self.assertIn("missing_identity_v99", context)

    def test_contract_drift_does_not_upgrade_foreign_unverified_session_to_degraded(self):
        module = self.hook()
        registry = Path(self.tmp.name) / "controllers.json"
        registry.write_text(json.dumps({
            "controller-1": str(self.repo.resolve()),
            "__controller_sessions__": {"controller-1": {"web": ["web-current"]}},
            "__controller_targets__": {"controller-1": {"web": {
                "status": "active", "session_id": "web-current", "generation": 1,
                "provenance": "host_attested_same_controller_recovery",
                "binding_mode": "resume_only", "identity_proof": "host_attested_origin",
            }}},
            "__controller_execution_ownership__": {"controller-1": {
                "active_host": "web", "execution_target_session_id": "web-current",
                "generation": 1, "provenance": "web_entry",
            }},
        }), encoding="utf-8")
        (self.repo / "AGENTS.md").write_text(
            "# Project Rules\n\nadaptive_agent_runtime_required_capabilities: missing_identity_v99\n",
            encoding="utf-8",
        )

        receipt = module.initialize_project_context(
            self.repo, skill_root=self.skill, controller_registry_path=registry,
            controller_host="web", source_session_id="web-foreign",
        )

        self.assertEqual(receipt["runtime_contract_state"], "RUNTIME_CONTRACT_DRIFT")
        self.assertEqual(receipt["session_binding_state"]["verification"], "UNVERIFIED")
        self.assertEqual(receipt["session_binding_state"]["reason"], "SESSION_NOT_BOUND_TO_PROJECT_CONTROLLER")
        self.assertEqual(receipt["identity_state"], "UNVERIFIED")
        self.assertFalse(receipt["safe_control_actions_allowed"])
        self.assertFalse(receipt["controller_actions_allowed"])
        self.assertFalse(receipt["create_new_controller_allowed"])

    def test_legacy_missing_web_controller_identity_cli_is_reported_as_contract_drift(self):
        module = self.hook()
        registry = Path(self.tmp.name) / "controllers.json"
        registry.write_text(json.dumps({"controller-1": str(self.repo.resolve())}), encoding="utf-8")
        (self.repo / "AGENTS.md").write_text(
            "# Project Rules\n\nBefore Controller actions run: python3 scripts/web_lifecycle_bridge.py controller-identity --repo <repo> --web-session-id <trusted-id>\n",
            encoding="utf-8",
        )
        receipt = module.initialize_project_context(
            self.repo, skill_root=self.skill, controller_registry_path=registry,
            controller_host="web", source_session_id=None,
        )
        self.assertEqual(receipt["runtime_contract_state"], "RUNTIME_CONTRACT_DRIFT")
        self.assertIn("legacy_web_lifecycle_controller_identity_cli", receipt["missing_identity_capabilities"])
        self.assertEqual(receipt["canonical_identity_cli"], "controller_target_guard.py identity")
        self.assertEqual(receipt["identity_state"], "DEGRADED")
        self.assertEqual(receipt["project_controller_state"]["controller_id"], "controller-1")
        self.assertFalse(receipt["create_new_controller_allowed"])

    def test_current_identity_capability_contract_reports_current(self):
        module = self.hook()
        registry = Path(self.tmp.name) / "controllers.json"
        registry.write_text(json.dumps({
            "controller-1": str(self.repo.resolve()),
            "__controller_sessions__": {"controller-1": {"web": ["web-current"]}},
            "__controller_targets__": {"controller-1": {"web": {
                "status": "active", "session_id": "web-current", "generation": 1,
                "provenance": "host_attested_same_controller_recovery",
                "binding_mode": "resume_only", "identity_proof": "host_attested_origin",
            }}},
            "__controller_execution_ownership__": {"controller-1": {
                "active_host": "web", "execution_target_session_id": "web-current",
                "generation": 1, "provenance": "web_entry",
            }},
        }), encoding="utf-8")
        (self.repo / "AGENTS.md").write_text(
            "# Project Rules\n\nadaptive_agent_runtime_required_capabilities: controller_identity_projection,web_session_binding,target_generation_fence\n",
            encoding="utf-8",
        )

        receipt = module.initialize_project_context(
            self.repo, skill_root=self.skill, controller_registry_path=registry,
            controller_host="web", source_session_id="web-current",
        )

        self.assertEqual(receipt["runtime_contract_state"], "CURRENT")
        self.assertEqual(receipt["missing_identity_capabilities"], [])
        self.assertEqual(receipt["identity_state"], "VERIFIED")
        self.assertTrue(receipt["controller_actions_allowed"])

    def test_session_start_infers_native_codex_as_desktop_binding(self):
        module = self.hook()
        registry = Path(self.tmp.name) / "controllers.json"
        registry.write_text(json.dumps({
            "controller-1": str(self.repo.resolve()),
            "__controller_sessions__": {
                "controller-1": {"desktop_codex": ["desktop-current"]}
            },
            "__controller_targets__": {
                "controller-1": {
                    "desktop_codex": {
                        "status": "active",
                        "session_id": "desktop-current",
                        "generation": 5,
                    }
                }
            },
        }), encoding="utf-8")
        with patch.object(module.target_guard, "DEFAULT_REGISTRY", registry):
            output, state = module.evaluate_event(
                {
                    "hook_event_name": "SessionStart",
                    "session_id": "desktop-current",
                    "turn_id": "t-desktop",
                    "cwd": str(self.repo),
                },
                skill_root=self.skill,
                prior_state={},
            )
        receipt = state["project_context_receipt"]
        self.assertEqual(receipt["session_binding_state"]["host"], "desktop_codex")
        self.assertEqual(receipt["session_binding_state"]["verification"], "VERIFIED")
        self.assertTrue(receipt["controller_actions_allowed"])
        self.assertIn(
            '"verification": "VERIFIED"',
            output["hookSpecificOutput"]["additionalContext"],
        )

    def test_controller_target_change_invalidates_prior_project_context_identity_receipt(self):
        module = self.hook()
        registry = Path(self.tmp.name) / "controllers.json"
        registry.write_text(json.dumps({
            "controller-1": str(self.repo.resolve()),
            "__controller_sessions__": {
                "controller-1": {"desktop_codex": ["desktop-old", "desktop-new"]}
            },
            "__controller_targets__": {
                "controller-1": {
                    "desktop_codex": {
                        "status": "active",
                        "session_id": "desktop-old",
                        "generation": 1,
                    }
                }
            },
        }), encoding="utf-8")
        receipt = module.initialize_project_context(
            self.repo,
            skill_root=self.skill,
            controller_registry_path=registry,
            controller_host="desktop_codex",
            source_session_id="desktop-old",
        )
        saved = json.loads(registry.read_text(encoding="utf-8"))
        saved["__controller_targets__"]["controller-1"]["desktop_codex"] = {
            "status": "active",
            "session_id": "desktop-new",
            "generation": 2,
        }
        registry.write_text(json.dumps(saved), encoding="utf-8")
        self.assertTrue(module._receipt_source_changed(receipt))

    def test_project_context_reports_verified_bound_web_session_without_changing_ownership(self):
        module = self.hook()
        registry = Path(self.tmp.name) / "controllers.json"
        registry.write_text(
            json.dumps({
                "controller-1": str(self.repo.resolve()),
                "__controller_sessions__": {
                    "controller-1": {"web": ["web-current"]}
                },
                "__controller_targets__": {"controller-1": {"web": {
                    "status": "active", "session_id": "web-current", "generation": 1,
                    "provenance": "host_attested_same_controller_recovery",
                    "binding_mode": "resume_only", "identity_proof": "host_attested_origin",
                }}},
                "__controller_execution_ownership__": {"controller-1": {
                    "active_host": "web", "execution_target_session_id": "web-current",
                    "generation": 1, "provenance": "web_entry",
                }},
            }),
            encoding="utf-8",
        )
        receipt = module.initialize_project_context(
            self.repo,
            skill_root=self.skill,
            controller_registry_path=registry,
            controller_host="web",
            source_session_id="web-current",
        )
        self.assertEqual(
            receipt["project_controller_state"]["controller_id"],
            "controller-1",
        )
        self.assertEqual(
            receipt["session_binding_state"]["verification"],
            "VERIFIED",
        )
        self.assertTrue(receipt["controller_actions_allowed"])
        self.assertFalse(receipt["create_new_controller_allowed"])

    def test_nested_working_directory_loads_all_applicable_agents_in_scope_order(self):
        module = self.hook()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "repo"
            nested = root / "packages" / "web"
            nested.mkdir(parents=True)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / "AGENTS.md").write_text("root-rule" + chr(10), encoding="utf-8")
            (root / "packages" / "AGENTS.md").write_text("package-rule" + chr(10), encoding="utf-8")
            (nested / "AGENTS.md").write_text("web-rule" + chr(10), encoding="utf-8")
            receipt = module.initialize_project_context(nested, skill_root=ROOT)
        agents = receipt["sources"]["agents"]
        self.assertEqual(receipt["state"], "initialized")
        self.assertEqual(
            [Path(item["path"]).parent.name for item in agents["scope_chain"]],
            ["repo", "packages", "web"],
        )
        self.assertLess(agents["content"].index("root-rule"), agents["content"].index("package-rule"))
        self.assertLess(agents["content"].index("package-rule"), agents["content"].index("web-rule"))

    def test_nested_agents_can_initialize_context_when_repo_root_has_no_agents(self):
        module = self.hook()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "repo"
            nested = root / "service"
            nested.mkdir(parents=True)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (nested / "AGENTS.md").write_text("service-rule" + chr(10), encoding="utf-8")
            receipt = module.initialize_project_context(nested, skill_root=ROOT)
        self.assertEqual(receipt["state"], "initialized")
        self.assertEqual(len(receipt["sources"]["agents"]["scope_chain"]), 1)
        self.assertIn("service-rule", receipt["sources"]["agents"]["content"])

    def test_new_nested_agents_scope_invalidates_existing_context_receipt(self):
        module = self.hook()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "repo"
            nested = root / "packages" / "web"
            nested.mkdir(parents=True)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / "AGENTS.md").write_text("root-rule" + chr(10), encoding="utf-8")
            receipt = module.initialize_project_context(nested, skill_root=ROOT)
            self.assertFalse(module._receipt_source_changed(receipt))
            (root / "packages" / "AGENTS.md").write_text("new-package-rule" + chr(10), encoding="utf-8")
            self.assertTrue(module._receipt_source_changed(receipt))

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

    def test_not_found_unknown_token_does_not_authorize_fabricated_definitive_mechanism(self):
        hook = self.hook()
        _, state = hook.evaluate_event(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "reader-session",
                "turn_id": "mixed-unknown",
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
                "turn_id": "mixed-unknown",
                "cwd": str(self.repo),
                "last_assistant_message": (
                    "UNKNOWN / NOT FOUND：当前权威事实源中未找到该机制，"
                    "但确定使用红黄绿三色治理。"
                ),
            },
            skill_root=self.skill,
            prior_state=state,
        )
        self.assertEqual(blocked["decision"], "block")
        self.assertIn("uncertainty-only", blocked["reason"])
        self.assertTrue(state["pending_project_fact_turn"])
        self.assertTrue(state["correction_required"])

    def test_runtime_state_creation_after_prompt_invalidates_fact_receipt_before_stop(self):
        hook = self.hook()
        _, state = hook.evaluate_event(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "reader-session",
                "turn_id": "runtime-created",
                "cwd": str(self.repo),
                "prompt": "当前项目 Runtime 状态是什么？",
            },
            skill_root=self.skill,
            prior_state={},
        )
        receipt = state["project_context_receipt"]
        self.assertEqual(receipt["sources"]["runtime_state"]["status"], "not_found")
        runtime_path = Path(receipt["sources"]["runtime_state"]["path"])
        runtime_path.parent.mkdir(parents=True, exist_ok=True)
        runtime_path.write_text(
            json.dumps({"schema_version": 1, "leases": {"A1": {"terminal_state": None}}}) + chr(10),
            encoding="utf-8",
        )
        self.assertTrue(hook._receipt_source_changed(receipt))
        blocked, state = hook.evaluate_event(
            {
                "hook_event_name": "Stop",
                "session_id": "reader-session",
                "turn_id": "runtime-created",
                "cwd": str(self.repo),
                "last_assistant_message": "当前项目 Runtime 状态为空。",
            },
            skill_root=self.skill,
            prior_state=state,
        )
        self.assertEqual(blocked["decision"], "block")
        self.assertIn("authoritative_project_source_changed_before_stop", blocked["reason"])
        self.assertEqual(
            state["project_context_receipt"]["sources"]["runtime_state"]["status"],
            "verified",
        )
        self.assertTrue(state["pending_project_fact_turn"])
        self.assertTrue(state["correction_required"])

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

    def test_nested_correction_refresh_preserves_full_applicable_agents_scope_chain(self):
        hook = self.hook()
        nested = self.repo / "packages" / "web"
        nested.mkdir(parents=True)
        (self.repo / "packages" / "AGENTS.md").write_text(
            "package-rule" + chr(10),
            encoding="utf-8",
        )
        (nested / "AGENTS.md").write_text(
            "nested-web-rule" + chr(10),
            encoding="utf-8",
        )
        _, state = hook.evaluate_event(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "reader-session",
                "turn_id": "nested-refresh",
                "cwd": str(nested),
                "prompt": "当前项目规则是什么？",
            },
            skill_root=self.skill,
            prior_state={},
        )
        initial = state["project_context_receipt"]
        self.assertEqual(Path(initial["working_directory"]).resolve(), nested.resolve())
        self.assertIn("package-rule", initial["sources"]["agents"]["content"])
        self.assertIn("nested-web-rule", initial["sources"]["agents"]["content"])

        runtime_path = Path(initial["sources"]["runtime_state"]["path"])
        runtime_path.parent.mkdir(parents=True, exist_ok=True)
        runtime_path.write_text(
            json.dumps({"schema_version": 1, "leases": {}}) + chr(10),
            encoding="utf-8",
        )
        blocked, state = hook.evaluate_event(
            {
                "hook_event_name": "Stop",
                "session_id": "reader-session",
                "turn_id": "nested-refresh",
                "cwd": str(self.repo),
                "last_assistant_message": "当前项目规则已确认。",
            },
            skill_root=self.skill,
            prior_state=state,
        )
        self.assertEqual(blocked["decision"], "block")
        refreshed = state["project_context_receipt"]
        self.assertEqual(Path(refreshed["working_directory"]).resolve(), nested.resolve())
        self.assertIn("package-rule", refreshed["sources"]["agents"]["content"])
        self.assertIn("nested-web-rule", refreshed["sources"]["agents"]["content"])
        self.assertEqual(refreshed["sources"]["runtime_state"]["status"], "verified")

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
