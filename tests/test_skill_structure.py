from pathlib import Path
import subprocess
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = ROOT / "SKILL.md"
OPENAI_METADATA = ROOT / "agents" / "openai.yaml"
REFERENCE_PATHS = {
    "methods": ROOT / "references" / "methods.md",
    "harness": ROOT / "references" / "harness-and-release.md",
    "visual": ROOT / "references" / "visual-reference-governance.md",
    "experience": ROOT / "references" / "experience-catalog.md",
    "long-task": ROOT / "references" / "long-task-governance.md",
    "context": ROOT / "references" / "context-governance.md",
    "agent-routing": ROOT / "references" / "agent-model-routing.md",
    "agent-delivery": ROOT / "references" / "agent-delivery-contract.md",
    "external-agent": ROOT / "references" / "external-agent-auth.md",
    "controller-scoring": ROOT / "references" / "controller-performance-scoring.md",
}
CORE_REFERENCE_KEYS = ("methods", "harness", "visual", "experience", "long-task", "context")
METHODS = REFERENCE_PATHS["methods"]
VISUAL = REFERENCE_PATHS["visual"]


def read_entrypoint() -> str:
    return ENTRYPOINT.read_text(encoding="utf-8")


def split_frontmatter(markdown: str) -> tuple[str, str]:
    match = re.match(r"\A---\s*\n(.*?)\n---\s*\n(.*)\Z", markdown, re.DOTALL)
    if not match:
        raise AssertionError("SKILL.md must start with YAML frontmatter")
    return match.group(1), match.group(2)


class SkillStructureTests(unittest.TestCase):
    def test_packaged_project_workflow_template_is_not_discoverable_as_a_skill(self):
        tracked = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "*SKILL.md"],
            check=True, capture_output=True, text=True,
        ).stdout.splitlines()
        discoverable = sorted(Path(path) for path in tracked if Path(path).name == "SKILL.md")

        self.assertEqual([Path("SKILL.md")], discoverable)
        self.assertTrue((ROOT / "assets" / "templates" / "PROJECT_SKILL.md").is_file())

    def test_required_skill_files_exist(self):
        missing = [str(path.relative_to(ROOT)) for path in [ENTRYPOINT, *REFERENCE_PATHS.values()] if not path.is_file()]
        self.assertEqual([], missing, f"required Skill files are missing: {missing}")

    def test_rule_and_runtime_handshake_scripts_are_packaged(self):
        required = [
            ROOT / "scripts" / "project_state.py",
            ROOT / "scripts" / "install_skill.py",
            ROOT / "scripts" / "rule_handshake.py",
            ROOT / "scripts" / "assignment_runtime.py",
        ]
        self.assertEqual([], [str(path.relative_to(ROOT)) for path in required if not path.is_file()])

    def test_entrypoint_frontmatter_has_trigger_only_metadata(self):
        frontmatter, _ = split_frontmatter(read_entrypoint())
        fields = dict(re.findall(r"(?m)^([A-Za-z][\w-]*):\s*(.+?)\s*$", frontmatter))

        self.assertEqual("adaptive-agent-runtime", fields.get("name"))
        description = fields.get("description", "").strip().strip('"')
        self.assertRegex(description, r"^Use when\b")
        self.assertLessEqual(len(frontmatter), 1024)
        self.assertLessEqual(len(description), 500)


    def test_product_identity_documents_legacy_alias_without_second_state_root(self):
        entrypoint = read_entrypoint()
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        project_state = (ROOT / "scripts" / "project_state.py").read_text(encoding="utf-8")

        self.assertIn("# Adaptive Agent Runtime", entrypoint)
        self.assertIn("adaptive-delivery", readme)
        self.assertIn("legacy machine identifier", readme.casefold())
        self.assertIn(' / "adaptive-delivery"', project_state)
        self.assertNotIn(' / "adaptive-agent-runtime"', project_state)

    def test_openai_metadata_exposes_runtime_name_and_new_invocation(self):
        metadata = OPENAI_METADATA.read_text(encoding="utf-8")

        self.assertIn('display_name: "Adaptive Agent Runtime"', metadata)
        self.assertIn('$adaptive-agent-runtime', metadata)
        self.assertNotIn('$adaptive-delivery', metadata)

    def test_readme_uses_new_public_skill_id(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn('$adaptive-agent-runtime', readme)
        self.assertNotIn('$adaptive-delivery', readme)

    def test_project_templates_use_new_public_skill_id(self):
        templates = (
            "AGENTS.md", "TASK_LEDGER.md", "PROJECT_STATUS.md",
            "MEMORY.md", "PROJECT_SKILL.md", "WIKI_INDEX.md",
        )
        for name in templates:
            text = (ROOT / "assets" / "templates" / name).read_text(encoding="utf-8")
            self.assertIn("adaptive-agent-runtime", text, name)
            self.assertNotIn("`adaptive-delivery` Skill", text, name)

    def test_entrypoint_is_short(self):
        _, body = split_frontmatter(read_entrypoint())
        self.assertLessEqual(len(body.split()), 350)

    def test_core_runtime_instructions_are_vendor_neutral(self):
        core = "\n".join(
            [read_entrypoint(), *[
                REFERENCE_PATHS[key].read_text(encoding="utf-8")
                for key in CORE_REFERENCE_KEYS
            ]]
        ).casefold()
        vendor_names = ("codex", "chatgpt", "claude code", "gemini cli", "cursor")

        self.assertEqual([], [name for name in vendor_names if name in core])

    def test_local_reference_links_resolve(self):
        _, body = split_frontmatter(read_entrypoint())
        links = set(re.findall(r"\[[^\]]+\]\((references/[^)\s]+)\)", body))
        expected = {f"references/{path.name}" for path in REFERENCE_PATHS.values()}

        self.assertEqual(expected, links)
        self.assertTrue(all((ROOT / link).is_file() for link in links))

    def test_entrypoint_excludes_low_scope_work_from_full_orchestration(self):
        _, body = split_frontmatter(read_entrypoint())
        normalized = body.casefold()

        self.assertRegex(normalized, r"普通问答|ordinary\s+questions?|q\s*&\s*a")
        self.assertRegex(normalized, r"单文件[^\n]{0,20}(简单|小修)|single[- ]file[^\n]{0,20}simple")
        self.assertRegex(normalized, r"普通(?:代码)?审查|ordinary\s+(?:code\s+)?reviews?")

    def test_development_kickoff_has_authority_and_collaboration_contract(self):
        entrypoint = read_entrypoint()
        methods = METHODS.read_text(encoding="utf-8")

        for phrase in ("只制定计划", "开始开发", "继续开发"):
            self.assertIn(phrase, entrypoint)
        for field in (
            "里程碑",
            "验收",
            "工作包",
            "依赖",
            "负责人",
            "文件所有权",
            "停止条件",
            "主 Agent",
            "唯一任务台账",
        ):
            self.assertIn(field, methods)

    def test_long_task_governance_separates_runtime_context_from_project_files(self):
        long_task = REFERENCE_PATHS["long-task"].read_text(encoding="utf-8")

        for phrase in (
            "当前上下文",
            "Compact / 交接摘要",
            "TASK_LEDGER.md",
            "MEMORY.md",
            "WIKI_INDEX.md",
            "项目级 `SKILL.md`",
        ):
            self.assertIn(phrase, long_task)

    def test_ledger_granularity_advice_uses_five_variables_and_explains_the_recommendation(self):
        long_task = REFERENCE_PATHS["long-task"].read_text(encoding="utf-8")

        for phrase in (
            "失败代价",
            "需求清晰度",
            "项目或资料质量",
            "执行者熟悉度",
            "人工校准意愿",
            "推荐粒度",
            "不推荐其他粒度的原因",
            "必须细拆",
            "可以粗拆",
            "调整触发",
            "人类确认点",
        ):
            self.assertIn(phrase, long_task)

    def test_context_governance_covers_priority_knowledge_compilation_and_memory_hygiene(self):
        context = REFERENCE_PATHS["context"].read_text(encoding="utf-8")

        for phrase in (
            "当前上下文",
            "Compact",
            "Raw Sources",
            "Wiki",
            "长期记忆",
            "任务台账",
            "证据链",
            "Ingest",
            "Query",
            "Lint",
            "raw_sources/",
            "wiki/",
            "logs/ingestion/",
            "以后还会复用吗",
            "有证据来源吗",
            "会不会污染后续判断",
        ):
            self.assertIn(phrase, context)

    def test_durable_initialization_guidance_matches_context_governance(self):
        methods = METHODS.read_text(encoding="utf-8")

        for phrase in (
            "项目级 `SKILL.md`",
            "raw_sources/",
            "wiki/",
            "logs/ingestion/",
            "目录骨架",
        ):
            self.assertIn(phrase, methods)

    def test_task_ledger_template_is_an_instance_not_a_method_manual(self):
        for name in ("TASK_LEDGER.md", "PROJECT_STATUS.md"):
            ledger = (ROOT / "assets" / "templates" / name).read_text(
                encoding="utf-8"
            )

            self.assertNotIn("## 使用规则", ledger)
            self.assertNotIn("阶段 / 粒度", ledger)
            self.assertNotIn("动态混合粒度", ledger)
            self.assertIn("当前目标", ledger)
            self.assertIn("任务拆分", ledger)
            self.assertIn("证据 / 下一步", ledger)

    def test_instance_ledgers_preserve_the_selected_granularity(self):
        long_task = REFERENCE_PATHS["long-task"].read_text(encoding="utf-8")

        self.assertIn("项目已选择的实际粒度", long_task)
        self.assertIn("已完成台账项及其证据", long_task)

    def test_long_task_governance_scopes_candidate_and_shared_environment_reconciliation(self):
        long_task = REFERENCE_PATHS["long-task"].read_text(encoding="utf-8")

        for phrase in (
            "与当前工作包相关的 worktree",
            "未合并提交",
            "只隔离文件，不自动隔离运行环境",
            "发现重叠候选时先登记依赖与冲突",
            "未集成到主线前保持 `VERIFY`",
        ):
            self.assertIn(phrase, long_task)

    def test_long_task_controller_closes_atomic_events_before_yielding(self):
        long_task = REFERENCE_PATHS["long-task"].read_text(encoding="utf-8")

        for phrase in (
            "原子控制事务",
            "当前 Goal",
            "活动工作包",
            "READY",
            "阻塞",
            "下一可见检查点",
            "短事件回合",
            "立即 yield",
        ):
            self.assertIn(phrase, long_task)

    def test_long_task_governance_prefers_subtraction_and_business_closure(self):
        entrypoint = read_entrypoint()
        long_task = REFERENCE_PATHS["long-task"].read_text(encoding="utf-8")

        self.assertIn("规则和台账治理按", entrypoint)
        self.assertIn("不新增平行台账", entrypoint)
        for phrase in (
            "每个任务 ID 只出现一次",
            "业务功能纵向切片",
            "可观察的用户能力",
            "scripts/control_event_guard.py",
            "scripts/event_scope_guard.py",
            "scripts/assignment_lease_guard.py",
            "scripts/ledger_consistency_guard.py",
            "scripts/lifecycle_hook.py",
            "scripts/controller_target_guard.py",
            "不另建评分表",
        ):
            self.assertIn(phrase, long_task)

    def test_controller_lifecycle_hook_is_documented_as_runtime_enforcement(self):
        entrypoint = read_entrypoint()
        long_task = REFERENCE_PATHS["long-task"].read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("生命周期执行器", entrypoint)
        for phrase in (
            "SessionStart",
            "PostToolUse",
            "SubagentStop",
            "Stop",
            "registered controller",
            "control_event_guard.py",
        ):
            self.assertIn(phrase, long_task)
        self.assertIn("--register-controller", readme)
        self.assertIn("/hooks", readme)

    def test_controller_health_wake_contract_is_documented_without_second_identity(self):
        entrypoint = read_entrypoint()
        long_task = REFERENCE_PATHS["long-task"].read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        combined = "\n".join((entrypoint, long_task, readme))

        for phrase in (
            "Controller Health",
            "Wake Supervisor",
            "DEAD",
            "DEFERRED",
            "Git common-dir",
            "pending_control_event",
            "same-controller continuation",
            "wake success does not clear pending",
        ):
            self.assertIn(phrase, combined)
        self.assertIn("Adaptive Agent Runtime", combined)
        self.assertIn("public Skill ID", combined)
        self.assertIn("adaptive-agent-runtime", combined)
        self.assertIn("no second controller", combined)
        self.assertIn("no mutable health ledger", combined)
        self.assertIn("controller worktree events require binding proof", combined)
        self.assertIn("explicit Resume/Replace handling", combined)

    def test_controller_scoring_machine_gate_is_packaged_and_documented(self):
        entrypoint = read_entrypoint()
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertTrue((ROOT / "scripts" / "controller_scoring_hook.py").is_file())
        for phrase in ("UserPromptSubmit", "controller_scoring_hook.py", "score-guard", "未通过", "禁止输出分数"):
            self.assertIn(phrase, entrypoint + "\n" + readme)
        self.assertIn("不提供 UserPromptSubmit / Stop", entrypoint)
        self.assertIn("不能声称机器门已生效", readme)

    def test_long_task_governance_bounds_event_append_and_agent_reuse(self):
        long_task = REFERENCE_PATHS["long-task"].read_text(encoding="utf-8")

        for phrase in (
            "短事件按因果边界",
            "QUEUE_NEXT_EVENT",
            "若延后动作不会造成不一致、不安全或不可恢复",
            "Agent 实例和本次 Assignment 是两个身份",
            "Reviewer 改为同候选 Writer 后失去该 revision 的非作者资格",
            "任务表是状态唯一权威",
        ):
            self.assertIn(phrase, long_task)

    def test_long_task_rule_updates_use_machine_handshake_and_common_runtime_state(self):
        long_task = REFERENCE_PATHS["long-task"].read_text(encoding="utf-8")
        for phrase in (
            "scripts/install_skill.py",
            "scripts/rule_handshake.py ack",
            "rule_update_pending:<revision>",
            "git rev-parse --git-common-dir",
            "runtime-assignments.json",
            "spawn 前 fail closed",
        ):
            self.assertIn(phrase, long_task)

    def test_long_task_controller_uses_evidence_before_interrupt_or_replacement(self):
        long_task = REFERENCE_PATHS["long-task"].read_text(encoding="utf-8")

        for phrase in (
            "消息送达",
            "任务活动",
            "工具事件",
            "工作树状态",
            "clean",
            "loaded ACK",
            "连续两次",
            "更换总控",
            "唯一常驻验收入口",
        ):
            self.assertIn(phrase, long_task)

    def test_long_task_controller_turns_missing_inputs_into_upstream_work(self):
        long_task = REFERENCE_PATHS["long-task"].read_text(encoding="utf-8")

        for phrase in (
            "缺失输入拆成能独立验收的上游工作包",
            "明确下游释放条件",
            "契约冻结前禁止下游猜测",
            "补齐 ACK 后从原 checkpoint 恢复",
            "不因握手缺失自动丢弃有效 WIP",
        ):
            self.assertIn(phrase, long_task)

    def test_shared_contract_review_checks_scale_and_unit_semantics(self):
        harness = REFERENCE_PATHS["harness"].read_text(encoding="utf-8")

        for phrase in (
            "多记录或批量场景",
            "N+1",
            "单位一致性",
            "UTF-16 code unit",
            "权威持久化或冻结合同",
            "独立工作包登记",
        ):
            self.assertIn(phrase, harness)

    def test_mixed_requests_route_each_independent_workflow_by_its_own_risk(self):
        methods = METHODS.read_text(encoding="utf-8")

        for phrase in ("工作流 / 档位 / 证据 / 授权门", "不使用单一最高值", "共同关键路径"):
            self.assertIn(phrase, methods)

    def test_controller_governance_collapses_to_exactly_four_named_gates(self):
        entrypoint = read_entrypoint()
        long_task = REFERENCE_PATHS["long-task"].read_text(encoding="utf-8")
        routing = REFERENCE_PATHS["agent-routing"].read_text(encoding="utf-8")
        delivery = REFERENCE_PATHS["agent-delivery"].read_text(encoding="utf-8")
        scenarios = (ROOT / "tests" / "behavioral-scenarios.md").read_text(encoding="utf-8")

        self.assertIn("四个控制门", entrypoint)
        for gate in ("Dispatch Gate", "Delivery Gate", "Integration Gate", "Stop/Yield Gate"):
            self.assertIn(gate, entrypoint)
        self.assertNotIn("三个临时机器门", scenarios)
        self.assertIn("Dispatch Gate", routing)
        self.assertIn("Delivery Gate", delivery)
        self.assertIn("Integration Gate", delivery)
        self.assertIn("Stop/Yield Gate", long_task)
        self.assertIn("同一份机器投影", entrypoint)

    def test_routing_documents_dual_host_fallback_without_cross_host_guessing(self):
        routing = REFERENCE_PATHS["agent-routing"].read_text(encoding="utf-8")
        long_task = REFERENCE_PATHS["long-task"].read_text(encoding="utf-8")
        delivery = REFERENCE_PATHS["agent-delivery"].read_text(encoding="utf-8")
        for phrase in ("双宿主兜底", "controller_host=web|desktop_codex", "peer_host_available", "peer_host_unavailable", "同一模型档位"):
            self.assertIn(phrase, routing)
        self.assertIn("controller_host", long_task)
        self.assertIn("host_fallback_level", delivery)
        self.assertIn("Moving Web→Desktop or Desktop→Web never resets", delivery)

    def test_external_agent_routes_pin_the_selected_reasoning_effort(self):
        routing = REFERENCE_PATHS["agent-routing"].read_text(encoding="utf-8")
        external = REFERENCE_PATHS["external-agent"].read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        for effort in ("`low`", "`medium`", "`high`", "`xhigh`", "`max`"):
            self.assertIn(effort, routing)
        self.assertIn("不得省略 `reasoning_effort`", routing)
        self.assertIn("为每个工作包分别选择模型和推理强度", readme)

        routed_commands = [
            line for line in external.splitlines()
            if line.startswith("node ") and (" --check " in line or " --execute " in line)
        ]
        self.assertEqual(8, len(routed_commands))
        for command in routed_commands:
            self.assertIn("--reasoning-effort <selected-effort>", command)

    def test_quality_preserving_acceleration_keeps_evidence_and_removes_duplicate_work(self):
        methods = METHODS.read_text(encoding="utf-8")

        for phrase in (
            "一次读取、持续复用",
            "分工保持互斥",
            "草案后定向审查",
            "局部失效与恢复",
            "单次等待不超过 60 秒",
            "不能删除以下质量门",
        ):
            self.assertIn(phrase, methods)

    def test_approved_visual_reference_uses_the_fast_path_without_losing_impact_scan(self):
        entrypoint = read_entrypoint()
        visual = VISUAL.read_text(encoding="utf-8")

        self.assertIn("定向影响扫描", entrypoint)
        for phrase in ("已确认参考图快速路径", "强制使用快速档", "不减少必要影响扫描"):
            self.assertIn(phrase, visual)

    def test_binding_visual_reference_is_a_hard_gate_not_inspiration(self):
        visual = VISUAL.read_text(encoding="utf-8")

        for phrase in (
            "绑定视觉基线",
            "不得另起一套视觉系统",
            "语义最接近的已确认组件",
            "同状态",
            "浏览器缩放",
            "截图存在不等于视觉通过",
            "保持 `VERIFY`",
            "visual_evidence_guard.py",
        ):
            self.assertIn(phrase, visual)

    def test_scoring_docs_expose_fixed_raw_scale_and_single_cycle_diagnostics(self):
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for text in (skill, readme):
            self.assertIn("0–100", text)
            self.assertIn("单回合", text)
            self.assertIn("最佳", text)
            self.assertIn("最差", text)


if __name__ == "__main__":
    unittest.main()
