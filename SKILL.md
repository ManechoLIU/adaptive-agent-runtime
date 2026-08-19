---
name: adaptive-delivery
description: Use when initializing a software project, coordinating delivery across multiple stages, resuming work across agent sessions, governing approved visual references, defining Harness or release gates, or extracting reusable engineering experience.
---

# Adaptive Delivery

选择能证明当前结果的最小流程。普通问答、单文件简单修改和普通代码审查不加载完整编排；用户显式调用时仍按风险裁剪。

## 选择档位

- **快速档**：目标明确、低风险、小范围。直接执行，做一个足以证明结果的定向检查，然后停止；不强制计划、子 Agent 或文档更新。
- **标准档**：普通功能、UI 或跨文件改动。给出短计划，只协调真实依赖；当前事实变化时同步对应文档。
- **严格档**：认证、费用、外部调用、迁移、安装、部署或发布。读取项目安全规则，使用相称的 Harness 和授权门。

## 开发授权

- **只制定计划**：输出开发计划和协作方式，不修改代码或分派实施。
- **开始开发**：按已确认计划执行必要工作。
- **继续开发**：从 `PROJECT_STATUS.md` 和当前工作区续接，不重做已完成内容。

## 共享约束

1. 先读取当前事实源、工作区状态和已有实现。
2. 保护用户及其他任务的已有改动。
3. 证据先于完成宣称；证据不足时说明边界。
4. 达到停止条件立即结束，不为完备度扩展范围。
5. 两次有界等待均无新证据时停止轮询，从最近证据点恢复、重新分派或报告阻塞。

## 按需读取

- 选择方法、初始化档案或跨会话续接：读 [methods](references/methods.md)。
- 涉及风险证据、外部服务、本地/Web 发布或恢复：读 [Harness and release](references/harness-and-release.md)。
- 用户明确认可视觉参考，或任务涉及视觉基线：读 [visual reference governance](references/visual-reference-governance.md)。
- 同步权威文档或提炼项目经验：读 [experience catalog](references/experience-catalog.md)。

初始化使用 `scripts/init_project.py`；逐个跳过已有文件，不覆盖或静默合并。全局 Skill 修改、付费调用、Git 写入和公开发布仍需对应授权。
