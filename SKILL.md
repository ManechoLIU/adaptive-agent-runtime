---
name: adaptive-delivery
description: Use when initializing or running a multi-stage software project, coordinating across agents, branches, worktrees, or sessions, recovering stalled or drifting work, governing approved visual references, defining Harness or release gates, or extracting reusable engineering experience.
---

# Adaptive Delivery

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

出现跨阶段、跨会话、多依赖、多执行主体、高风险副作用、重复候选、持续阻塞或用户要求持续推进时，读取 [long-task governance](references/long-task-governance.md)。该模式使用 `TASK_LEDGER.md` 作为新项目的任务台账；已有项目只有 `PROJECT_STATUS.md` 时继续沿用，禁止再创建第二份台账。

用户已明确要求使用 Goal、要求本 Skill 持续执行长期任务，或项目规则已授权自动 Goal，且运行环境提供 Goal 工具时，按参考中的条件自动创建或切换 Goal；普通问答、轻量任务或仅因 Skill 被隐式选中时不创建 Goal。

## 共享约束

1. 先读取当前事实源、工作区状态和已有实现。
2. 保护用户及其他任务的已有改动。
3. 证据先于完成宣称；证据不足时说明边界。
4. 达到停止条件立即结束，不为完备度扩展范围。
5. 两次有界等待均无新证据时停止轮询，从最近证据点恢复、重新分派或报告阻塞。
6. 快速档不固定文件清单或工具次数：先按项目文档职责扫描真实影响，只更新受影响事实源；发现跨域变化、冲突或高风险边界时再升级，不能为了快而漏改，也不能因涉及多份文档自动升级。
7. 多 Agent 数量是从 `0` 到运行环境上限的动态选择，不是流程配额。主 Agent 保留范围、共享契约、集成和最终验收；能以现有边界或小规模行为不变拆分隔离的实现优先分派，不默认由主 Agent 包揽全部用户可见模块。
8. 完整验证只在实际 diff 审查结束、候选快照冻结后运行一次；同一快照与同一命令已有通过收据时必须复用。重跑前先指出使旧收据失效的内容或环境变化；发布后默认只核对远端落点。具体按 [Harness and release](references/harness-and-release.md) 执行。

## 续接与执行预算

- 跨会话续接优先使用完整交接摘要和当前工作区；摘要已包含目标、决定、改动、证据与剩余步骤时，不再读取旧会话。缺少关键事实时最多做一次定向读取，默认不带完整工具输出；只有具体阻塞无法消解时才补取对应输出。
- 续接预检通常最多三个工具批次，用于固定仓库、范围、现有改动和最近证据；随后必须进入第一个受控修改、验证或可交付分析。仍缺证据时停止扩大读取，只获取能解除当前阻塞的最小信息。
- 同一工作包累计超过十二次工具往返，或连续五分钟没有新增根因、受控差异、测试结果、浏览器证据或明确阻塞时，立即停止重复读取并压缩续接点。若剩余工作无法在当前上下文安全完成则交接新任务；否则只执行一个最接近交付的定向步骤。

## 按需读取

- 选择方法、标准/严格档调度、初始化档案或跨会话续接：读 [methods](references/methods.md)；多问题、多 Agent 或共享事实源任务使用其中的“质量保真加速协议”。
- 长期项目、Goal、任务台账、Compact、项目记忆、Wiki、候选分支或共享环境治理：读 [long-task governance](references/long-task-governance.md)。
- 涉及风险证据、外部服务、本地/Web 发布或恢复：读 [Harness and release](references/harness-and-release.md)。
- 用户明确认可视觉参考，或任务涉及视觉基线：读 [visual reference governance](references/visual-reference-governance.md)。
- 同步权威文档或提炼项目经验：读 [experience catalog](references/experience-catalog.md)。

初始化使用 `scripts/init_project.py`；逐个跳过已有文件，不覆盖或静默合并。全局 Skill 修改、付费调用、Git 写入和公开发布仍需对应授权。
