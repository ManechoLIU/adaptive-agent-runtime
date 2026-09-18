---
name: adaptive-agent-runtime
description: Use when initializing or governing a long-running project, choosing task-ledger granularity, managing AI context, Wiki, memory, Compact, agents, branches, worktrees, Harness or release gates, recovering stalled work, governing visual references, or extracting reusable engineering experience.
---

# Adaptive Agent Runtime

选择能证明当前结果的最小流程。普通问答、单文件简单修改和普通代码审查不加载完整编排；用户显式调用时仍按风险裁剪。

## 选择档位

- **快速档**：目标明确、低风险、小范围。直接执行，做一次定向影响扫描和足以证明结果的检查，然后停止；不强制计划或子 Agent。用户明确认可且能定位精确视觉稿时，即使需要同步少量权威文档也仍属于快速档，具体按 [visual reference governance](references/visual-reference-governance.md) 执行。
- **标准档**：普通功能、UI 或跨文件改动。给出短计划，只协调真实依赖；当前事实变化时同步对应文档。
- **严格档**：认证、费用、外部调用、迁移、安装、部署或发布。读取 [methods](references/methods.md) 的调度升级门和项目安全规则；独立执行主体可用时安排非作者审查，使用相称的 Harness 和授权门。

## 开发授权

- **只制定计划**：输出开发计划和协作方式，不修改代码或分派实施。
- **开始开发**：按已确认计划执行必要工作。
- **继续开发**：从当前任务台账和工作区续接，不重做已完成内容。

## 长期任务模式

出现跨阶段、跨会话、多依赖、多执行主体、高风险副作用、重复候选、持续阻塞或用户要求持续推进时，读取 [long-task governance](references/long-task-governance.md)。用户询问“项目该用什么台账、怎么拆”时也读取它，给出建议、原因、边界和调整触发。该模式使用 `TASK_LEDGER.md` 作为新项目的任务台账；已有项目只有 `PROJECT_STATUS.md` 时继续沿用，禁止再创建第二份台账。

用户已明确要求使用 Goal、要求本 Skill 持续执行长期任务，或项目规则已授权自动 Goal，且运行环境提供 Goal 工具时，按参考中的条件自动创建或切换 Goal；普通问答、轻量任务或仅因 Skill 被隐式选中时不创建 Goal。

## 共享约束

1. 先读取当前事实源、工作区状态和已有实现。
2. 保护用户及其他任务的已有改动。
3. 证据先于完成宣称；证据不足时说明边界。
4. 达到停止条件立即结束，不为完备度扩展范围。
5. heartbeat / PID 只证明存活；只有 Git / 测试 / artifact / blocker 指纹产生新证据才算进展。相同任务合同最多两次 recovery；预算耗尽后同一 Assignment 不得再开新 attempt，必须把策略变化固化为新 Assignment，不能继续同路径重试。
6. 快速档不固定文件清单或工具次数：先按项目文档职责扫描真实影响，只更新受影响事实源；发现跨域变化、冲突或高风险边界时再升级，不能为了快而漏改，也不能因涉及多份文档自动升级。
7. 多 Agent 数量是从 `0` 到运行环境上限的动态选择，不是流程配额。主 Agent 保留范围、共享契约、集成和最终验收；能以现有边界或小规模行为不变拆分隔离的实现优先分派，不默认由主 Agent 包揽全部用户可见模块。
8. **四个控制门**：长期总控只处理 `Dispatch Gate / Delivery Gate / Integration Gate / Stop/Yield Gate`，四门消费**同一份机器投影**，不新增平行台账、Web 专用 READY 或第二状态机。Dispatch 负责 Assignment ACK、规则握手、lineage / recovery budget、项目级 runnable 推导、当前宿主优先与 peer-host 授权 fallback；Delivery 负责 transport 与 delivery 分离及证据 / artifact；Integration 负责 Reviewer、候选、Git ancestry 与 current-main 回归；Stop/Yield 负责 BLOCKED、Goal rollover、idle 与 resume 闭合。细节分别读取 [Agent and model routing](references/agent-model-routing.md)、[Agent delivery contract](references/agent-delivery-contract.md)、[Harness and release](references/harness-and-release.md) 与 [long-task governance](references/long-task-governance.md)。
9. 规则和台账治理按 [long-task governance](references/long-task-governance.md) 做减法：总控只管理 `READY / ACTIVE / VERIFY / BLOCKED / CLOSED` 五个主状态，旧状态仅兼容解析；任务表是项目状态唯一权威，实时容量、health、recovery、RED/GREEN、candidate、Reviewer verdict 和 resume attempt 只进机器证据 / 临时收据。`event_scope_guard.py`、`control_event_guard.py`、`assignment_lease_guard.py`、`ledger_consistency_guard.py` 只执行各自确定性检查，不扩张成新的状态所有者。
10. 持续项目启用 [long-task governance](references/long-task-governance.md) 的生命周期执行器与 Controller Health / Wake Supervisor：每次控制事件和 Stop/Yield 从唯一事实源重算 project-wide runnable；存在 runnable、候选、恢复动作或 Continuation Debt 时禁止 idle/Yield。成功控制收据只授权当前投影 Stop/Yield，后继动作会使授权失效并要求重新收据。Web Bridge 与桌面 Hook 共用同一 Controller、投影和四门；宿主续接只复用 registered controller，失败保持 pending_control_event，绝不创建第二总控；适配器未通过实机验证时保持 degraded。
11. Controller 身份与执行授权分层：controller_id 是长期唯一 ownership，Session/Binding/Authorization 只决定当前入口权限。统一 identity projection 使用 `VERIFIED / DEGRADED / UNVERIFIED / CONFLICTED`；verifier/capability 故障或 `RUNTIME_CONTRACT_DRIFT` 只能使唯一 Controller 进入 DEGRADED，安全 control-plane 工作继续，身份敏感 mutation 单独 fail closed，且绝不创建第二 Controller。直接身份检查统一使用 `scripts/controller_target_guard.py identity`；详细状态与恢复规则按 [long-task governance](references/long-task-governance.md)。


## 续接与执行预算

- 跨会话续接优先使用当前任务台账、有效交接摘要和工作区事实；摘要完整时不重读旧会话，缺少关键事实时只追溯能解除当前阻塞的内容。
- SessionStart 注入本轮工作集，不预灌全文台账或完整 Skill：能定位唯一 ACTIVE / RECOVERING / VERIFY 项时给该项必读字段（含边界与下一步）；否则给开放项投影。下一步只认台账字段。
- 项目事实提问只附被问到的源；必读正文超限则标不完整并省略，禁止静默截断。包里没有的判断当不存在。
- 预检只覆盖仓库、范围、现有改动和最近证据；完成这些核对后进入首个受控修改、验证或可交付分析，不为固定次数继续读资料。
- 连续读取、等待或工具往返没有产生新证据时，从最近有效检查点收缩范围、重排、交接或报告阻塞；时间与次数只是重新判断粒度的信号，不是跨项目硬上限。

## 按需读取

- 选择方法、标准/严格档调度、初始化档案或跨会话续接：读 [methods](references/methods.md)；多问题、多 Agent 或共享事实源任务使用其中的“质量保真加速协议”。
- 子 Agent 分派、前后端模型选择、模型降级或并发配置：读 [Agent and model routing](references/agent-model-routing.md) 与 [Agent delivery contract](references/agent-delivery-contract.md)。需要配置、登录、预检或执行 Kimi/Grok 外部 Agent 时，再读 [External Agent authentication and execution](references/external-agent-auth.md)；该流程由 Adaptive Agent Runtime 直接执行，不依赖任务生命周期插件。
- 长期项目、Goal、台账粒度、候选分支或共享环境治理：读 [long-task governance](references/long-task-governance.md)。
- 用户要求审计或评分项目总控履职时：使用 [controller performance scoring](references/controller-performance-scoring.md)。正式七维原始分固定为**0–100**，输出必须分开报告近期能力、风险状态与依据、约束分，并附同 Controller、同模型的已核验单回合最佳/最差；无样本写 `UNKNOWN`。有 `UserPromptSubmit + Stop` 的宿主启用 `controller_scoring_hook.py`；不提供 UserPromptSubmit / Stop 的宿主执行 `record-read` 与带完整三层参数的 `finalize-score`，未通过禁止输出分数。比较先用 `latest-score`；单回合诊断不能覆盖正式评分。重大事故的 `49` 分上限不按时间消失，仅在纠正验收、控制面对齐和后续 L3/L4 闭环均有证据后解除，历史最差回合仍保留。
- **Controller Self-Check**：按 [long-task governance](references/long-task-governance.md) 固定循环；Stop/Yield收据不完整或correction未闭合即fail-closed；禁止第二scheduler、台账、Controller。
- 上下文、Compact、Raw Sources、Wiki、长期记忆、知识目录初始化或资料摄取：读 [context governance](references/context-governance.md)。
- 涉及风险证据、外部服务、本地/Web 发布或恢复：读 [Harness and release](references/harness-and-release.md)。
- 用户明确认可视觉参考，或任务涉及视觉基线：读 [visual reference governance](references/visual-reference-governance.md)。
- 同步权威文档或提炼项目经验：读 [experience catalog](references/experience-catalog.md)。

初始化使用 `scripts/init_project.py`；完整上下文治理选 `durable`，并创建可查询的知识工作区和项目级工作流入口。脚本逐个跳过已有内容，不覆盖或静默合并。全局 Skill 修改、付费调用、Git 写入和公开发布仍需对应授权。
