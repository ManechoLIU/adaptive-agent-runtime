# Adaptive Delivery

让 AI 编程代理按项目风险选择最小够用的工程方法，并把需求、设计、开发、测试、验收和发布连接成可验证的交付过程。

## 适合什么场景

- 初始化会持续开发、跨多个 Agent 会话的产品项目。
- 从需求讨论一路推进到设计、实现、验收和上线。
- 治理所有角色共同使用的 UI 视觉参考图。
- 为本地应用、Web 或双端产品选择相称的验证与发布门禁。
- 把经过验证的项目经验提炼成可复用方法。

它不替代测试框架、CI/CD、项目管理工具或发布平台，也不会让 AI 编程代理自动共享旧会话。普通问答、单文件简单修改和普通代码审查不会因此套上完整流程。

## 兼容性

- 兼容 Agent Skills 的宿主可以直接安装并自动或显式调用。
- 其他 AI 编程代理可以按自然语言要求读取 `SKILL.md` 后使用同一方法。
- 自动发现、调用语法、安装目录和全局指令文件由宿主决定。

运行时核心只有一份，不为不同平台复制规则。`AGENTS.md` 是默认的项目协作入口；若宿主使用其他入口文件，应链接到现有权威文档，不要复制内容。

## 安装

通用方式是把整个仓库克隆或安装到宿主官方文档指定的 Skills 目录；只复制 `SKILL.md` 会漏掉 references、scripts 和 templates。

### Codex

[OpenAI 官方文档](https://developers.openai.com/codex/skills/)确认 Codex 从 `~/.agents/skills` 和项目 `.agents/skills` 发现 Skill，也可以让 `$skill-installer` 从 GitHub 安装：

```text
请使用 skill-installer 从 https://github.com/ManechoLIU/adaptive-delivery 安装 adaptive-delivery
```

手动安装：

```bash
git clone https://github.com/ManechoLIU/adaptive-delivery ~/.agents/skills/adaptive-delivery
```

Codex 可按描述自动选择，也可显式写 `$adaptive-delivery`。

### Claude Code

[Claude Code 官方文档](https://code.claude.com/docs/en/slash-commands)确认个人 Skill 位于 `~/.claude/skills/<name>`，可自动选择或用 `/name` 调用：

```bash
git clone https://github.com/ManechoLIU/adaptive-delivery ~/.claude/skills/adaptive-delivery
```

### Gemini CLI

[Gemini CLI 官方文档](https://geminicli.com/docs/cli/using-agent-skills/)提供直接安装命令，并支持 `~/.agents/skills`：

```bash
gemini skills install https://github.com/ManechoLIU/adaptive-delivery
```

### Cursor

[Cursor 官方文档](https://cursor.com/docs/skills)确认可从 GitHub 导入，也会发现 `~/.agents/skills`、`~/.cursor/skills` 及项目级对应目录。可在 **Customize → Rules → Add Rule → Remote Rule (Github)** 输入本仓库 URL。

安装后直接用自然语言描述任务即可；固定前缀只是各宿主的快捷调用方式，不是 Skill 的通用语法。

## 会自动识别什么

Skill 结合用户最新指令与项目当前事实判断动作，不依赖逐字匹配：

| 你可以这样说 | 默认理解 |
| --- | --- |
| “先别动代码，先给我开发计划和分工” | 只规划，不修改或分派实施 |
| “计划没问题，开始做吧” | 在已确认计划范围内开始开发 |
| “接着上次没做完的继续” | 对账状态账本和工作区，从现有停点续接 |
| “你看着办吧” | 写入范围不明确，先读取事实并确认，不擅自执行 |

识别“当前情况”时优先核对唯一任务台账、Git 根目录、`HEAD`、未提交改动和相关权威文档；新项目使用 `TASK_LEDGER.md`，已有项目可继续使用 `PROJECT_STATUS.md`，两者不得并存为双控制面。旧聊天只能提供线索，不能覆盖当前文件。项目写入授权也不能自动扩展为付费调用、提交、推送、部署或发布授权。

## 最快开始

普通协作型产品默认初始化六个顶层文件：

```text
请使用 adaptive-delivery 初始化当前项目，按协作型产品处理。
```

| 文件 | 唯一职责 |
| --- | --- |
| `AGENTS.md` | 新会话启动和事实源路由 |
| `TASK_LEDGER.md` | 唯一任务台账和执行控制面 |
| `SPEC.md` | 产品行为与验收 |
| `DESIGN.md` | 视觉、交互与认可参考图 |
| `TECHNICAL.md` | 架构、Harness、发布与恢复 |
| `EVOLUTION.md` | 有证据的可复用项目经验 |

长期、跨会话、多资料或需要 Goal 恢复的项目使用 durable 档位：

```bash
python3 scripts/init_project.py /path/to/project --profile durable
```

它会在上述六份文件之外增加 `MEMORY.md` 和 `WIKI_INDEX.md`。当前上下文和 Compact 不创建项目文件，也不会预先创建空的 `raw_sources/`、`wiki/` 或 `logs/` 目录。

只需要四个核心文档时：

```bash
python3 scripts/init_project.py /path/to/project --profile core
```

脚本逐个跳过已有文件，不覆盖、不静默合并，也不会在没有认可图片时创建空的视觉目录。

## 三档裁剪

- **快速档**：明确、低风险、小范围；直接完成并用最小证据验证。
- **标准档**：普通功能、UI 或跨文件工作；短计划、必要协调、真实闭环。
- **严格档**：认证、费用、外部调用、迁移或发布；完整风险门禁和授权边界。

混合请求先按可独立交付的工作流分别选档。一个付费或外部调用步骤只升级依赖它的工作流，不会把无依赖的只读核查和低风险修改整体升级；多问题任务复用同一事实源证据，并把独立审查集中在高风险结论。

## 验证证据复用

完成宣称必须绑定当前候选和适用环境的验证证据。同一候选、环境与命令的有效收据可以复用；项目验收策略、问题调查或环境漂移要求新证据时应重跑并记录原因。Git 项目可用机械守卫执行检查：

```bash
python3 scripts/verification_guard.py run /path/to/project \
  --check-id full-suite -- your-test-command
```

守卫把收据保存在目标仓库的 `.git/adaptive-delivery/`，不会进入提交。确需在同一快照重跑时，`--force-reason` 接受 `acceptance-policy`、`investigation`、`output-unavailable`、`receipt-unverifiable` 或 `user-requested`。

## 使用场景总览

| 你的情况 | Skill 会怎么处理 |
| --- | --- |
| 新建长期产品项目 | 按项目形态初始化常用事实源；已有文档逐个跳过，不覆盖 |
| 需求或架构还不确定 | 先用可丢弃原型或 Spike 回答关键未知，不直接铺开开发 |
| 准备进入开发阶段 | 按实际项目给出里程碑、工作包、依赖、分工、文件所有权、验证和停止条件 |
| 已确认计划，准备开工 | 只执行计划内必要工作，由主 Agent 调度、集成和最终验收 |
| 新会话继续旧项目 | 对账唯一任务台账、Git、全部相关候选和未提交改动，承接而不是重做 |
| 长任务、多个 worktree 或反复跑偏 | 启用长期任务模式；按授权自动 Goal，并先对账候选分支与共享环境副作用 |
| 普通功能或 UI 开发 | 走标准档，以真实用户路径完成最小纵向闭环 |
| 登记已明确认可的视觉参考图 | 走快速档，锁定精确图片并只同步真实受影响的事实源；不重新讨论、生成或实现 |
| 探索或讨论尚未认可的视觉方向 | 保留为候选，不提前升级成有效参考或视觉规则 |
| 修复根因明确的小 Bug | 走快速档：回归、最小修复、定向验证，通过即停止 |
| Bug 根因不明或跨模块 | 先诊断根因，再决定是否拆包、独立审查或升级验证 |
| 测试、验收或发布候选 | 按风险选择最小证据集；不拿测试数量或静态页面冒充真实完成 |
| 付费 API、认证、迁移或外部服务 | 走严格档，先完成免费检查，在费用和副作用授权门前停止 |
| 本地应用、Web 或双端上线 | 分别选择 `local`、`web` 或 `dual` 发布画像和恢复门禁 |
| 事故或中断任务恢复 | 两次有界等待均无新证据时停止轮询，从最近证据点恢复、重新分派或报告阻塞 |
| 项目经验值得复用 | 先留在项目并附证据；跨项目成立且获授权后再更新全局 Skill |

普通问答、普通代码审查、一次性脚本和根因明确的单文件小修改不会自动套用完整编排；仍会遵守范围、证据和授权边界。

## 进入开发阶段

```text
请使用 adaptive-delivery 进入开发阶段，先制定开发计划和协作方式，不执行。
请使用 adaptive-delivery 按已确认计划开始开发。
请使用 adaptive-delivery 继续开发。
```

三条指令分别表示“只规划”“按计划执行”“从当前状态续接”。开发计划按项目实际情况确定工作包、依赖、Agent 分工、文件所有权、验证、验收和停止条件，不强制所有项目使用同一种协作模式。

这些是便于复制的明确写法，不是必须逐字输入的口令。在 Codex 中可把“请使用 adaptive-delivery”替换为 `$adaptive-delivery`，Claude Code 和 Cursor 则使用各自的 `/adaptive-delivery`。自然语言意图明确时直接识别；涉及修改但授权含糊时，停在只读对账和确认，不替用户猜测。

## 常见例子

### 从零做 Web 产品

问题：需求、设计和开发容易各自推进。Skill 选择标准档，把首个核心流程做成真实纵向切片，并用唯一任务台账保持新会话对齐。结果：每一步都有当前事实、下一步和验收证据。

### 本地桌面应用

问题：浏览器里能运行不等于安装包可交付。Skill 在发布阶段选择 `local` 门禁，检查正式产物、隔离启动、升级、数据和恢复。结果：完成结论对应真实安装环境。

### 修复一个明确 Bug

问题：小 Bug 容易被扩展成重构。Skill 走快速档，只做回归、最小修复和定向验证，证据通过立即停止。结果：更快且不夹带无关改动。

### 接入付费图片 API

问题：真实验证可能产生费用。Skill 走严格档，未确认服务、模型、次数、尺寸、输出位置和预算前停在授权门；先完成不收费验证。结果：不会用“试一下”制造意外账单。

### 认可一张有瑕疵的 UI 参考图

问题：整体方向可用，但 Logo 和间距不能照搬。Skill 保存精确图片，在 `DESIGN.md` 记录采用点与排除项，并同步当前状态；不擅自重新生成或立即实现 UI。结果：设计、开发、测试和验收使用同一视觉基线。

## 自我进化

项目事实变化时，只更新负责该事实的文档。单项目 workaround 先留在项目；只有证据、适用与不适用条件和跨项目价值明确后，才形成全局 Skill 候选。修改全局 Skill、提交、推送或公开发布仍需用户确认。

Skill 不是后台服务。跨会话一致性来自项目中的 `AGENTS.md`、唯一任务台账和权威文档；长期项目再使用 `MEMORY.md` 与 `WIKI_INDEX.md`。当前上下文和 Compact 不落成项目文件，也不自动读取旧聊天；宿主专用入口文件只负责引导到这些事实源。

## 贡献

欢迎提交能减少遗漏、返工或过度流程的场景。请同时说明：适用条件、失败证据、最小修正和不应影响的控制场景。

## License

[MIT](LICENSE)
