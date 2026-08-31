# Adaptive Agent Runtime

让 AI 编程代理把长期项目真正推进到可验证结果，而不是只会写计划、堆测试、维护台账，或在遇到阻塞后停止。

它会根据任务风险选择最小够用的流程，并把需求、设计、开发、测试、真实验收、主线集成、发布门禁和跨会话恢复连接起来。

## 产品身份与兼容性

**Adaptive Agent Runtime** 是新的对外产品名；`adaptive-agent-runtime` 是产品 slug。为保证已有项目、显式调用、Hook、handshake、receipt 与安装路径零中断，底层 Skill ID 暂继续使用稳定兼容标识 `adaptive-delivery`（legacy machine identifier）。这不是第二个产品或第二套规则：机器运行事实仍只使用既有 `.git/adaptive-delivery/` canonical state，不创建双 Skill/runtime。未来只有宿主标准提供可靠 alias/迁移语义时才考虑切换技术 ID。

## 先用一句话理解

`adaptive-delivery` 不是项目管理软件，也不是常驻后台服务。它是一套交付运行合同：

- 小任务直接做，验证后停止。
- 普通功能做成真实用户可走通的闭环。
- 长期项目由一个总控持续负责，用一个台账、一个滚动 Goal 和多个互不冲突的工作包推进。
- Agent、分支、worktree、测试和截图都只是手段；没有进入约定主线并通过真实验收，就不能称为完成。

## 它现在能做什么

| 能力 | 实际作用 |
| --- | --- |
| 项目初始化 | 按项目规模创建必要的产品、设计、技术、台账、Wiki 和长期记忆骨架；已有内容逐项跳过，不覆盖 |
| 开发规划 | 把目标拆成可验收的功能闭环，明确依赖、文件所有权、Agent、验证和停止条件 |
| 持续项目总控 | 保持整个项目的交付责任，用短事件回合处理派发、ACK、候选、验收、集成、阻塞和恢复 |
| 动态多 Agent | 只并行真正互不冲突的工作；Agent 数量从 0 到环境上限动态选择，不为“看起来并行”而分派 |
| 模型与推理路由 | 为每个工作包分别选择模型和推理强度；前端可走 Kimi K3、后端可走 Grok 4.6，通用任务按复杂度选择 Codex 原生模型；外部路线显式使用 OAuth 会员或 API Key，不依赖任务生命周期插件 |
| 长任务恢复 | 从唯一台账、Git、候选、worktree 和有效证据恢复；`RECOVERING` 必须解决问题，不能把冻结当处理 |
| 上下文治理 | 区分当前上下文、Compact、Wiki、长期记忆和任务台账，避免信息混用、遗忘或文档膨胀 |
| 视觉参考治理 | 锁定用户确认的精确参考图，约束设计、开发和验收；禁止用自由发挥、粗糙占位或错误素材替代 |
| 验证与发布门禁 | 按风险选择 H0–H5 证据，绑定候选与环境，复用仍有效的收据，控制付费、外部调用和发布授权 |
| 经验沉淀 | 把有证据、能跨项目复用的经验整理为项目方法或全局 Skill 候选，不把任务流水账当方法论 |

## 什么时候使用

适合：

- 一个功能需要跨多个文件、端或服务才能闭环。
- 项目会持续多天、跨会话或需要多个 Agent 协作。
- 已经出现任务漂移、重复实现、候选堆积、台账落后、Agent 停滞或只做容易小项。
- 涉及认证、费用、外部 API、数据迁移、安装、发布或回滚。
- 需要统一管理视觉参考、真实端截图和 UI/UX 验收。
- 需要建立 Wiki、Raw Sources、长期记忆或 Compact 恢复方式。
- 项目结束后要把证据提炼为可复用经验。

不需要完整编排：

- 普通问答。
- 目标和根因明确的单文件小改。
- 一次性脚本或普通代码审查。
- 不需要跨轮恢复的短讨论。

即使显式调用了 Skill，也会按风险裁剪，不会给小任务强行创建 Goal、台账项或一组 Agent。

## 最常用的指令

以下写法可以直接复制，也可以用意思相同的自然语言。

### 1. 初始化长期项目

```text
请使用 adaptive-delivery 初始化当前项目。这个项目会长期开发、跨会话并使用多个 Agent，请建立合适的事实源、唯一台账、Wiki 和长期记忆，但不要覆盖已有文件。
```

### 2. 只制定开发计划

```text
请使用 adaptive-delivery 进入开发阶段，先给出里程碑、功能闭环、依赖、Agent 分工、验收和停止条件；先不修改代码。
```

### 3. 按计划开始开发

```text
请使用 adaptive-delivery 按已确认计划开始开发。能并行的工作包并行，完成后及时验收、合入主线并更新唯一台账。
```

### 4. 从中断处继续

```text
请使用 adaptive-delivery 继续开发。从当前 Git、唯一台账、候选和 worktree 对账后续接，不重做已经有效的成果。
```

### 5. 恢复停滞项目

```text
请使用 adaptive-delivery 检查项目为什么停滞。核对总控、Goal、台账、Agent ACK、候选、主线和真实验收状态；从最近可运行检查点恢复，并继续派发不受阻塞影响的 READY 工作。
```

### 6. 登记并执行视觉参考

```text
请使用 adaptive-delivery 把这张图登记为当前页面的绑定视觉参考，记录精确文件、哈希、适用状态和排除项；后续开发与验收严格按同一参考执行。
```

### 7. 治理项目上下文

```text
请使用 adaptive-delivery 检查当前项目的上下文治理：区分台账、Wiki、Raw Sources、长期记忆和 Compact，删除重复职责，但不要把治理文档当作开发产出。
```

### 8. 配置外部模型路线

```text
请使用 adaptive-delivery 配置 Agent 路由：前端使用 Kimi K3，后端使用 Grok 4.6，其他任务由主 Agent 按复杂度选择 Codex 原生模型；每个工作包都选择最低可靠推理强度。分别让我选择 OAuth 会员或 API Key，先只做不调用模型的预检。真实调用前再次确认，不要静默切换认证、计费路线、模型或推理强度。
```

该能力由 Adaptive Delivery 独立提供，不要求安装 Task Navigator 或 Codex Continuity；但仍需安装对应的官方 Kimi Code CLI 或 Grok Build CLI。API Key 应通过环境变量或系统凭据库提供，不粘贴到对话中。

## 三种执行档位

| 档位 | 适用情况 | 默认做法 |
| --- | --- | --- |
| 快速档 | 明确、低风险、小范围 | 直接执行，做定向影响扫描和足以证明结果的检查，通过即停止 |
| 标准档 | 普通功能、UI、跨文件或存在依赖 | 短计划、必要分工、真实用户闭环、主线集成与验收 |
| 严格档 | 认证、费用、外部调用、迁移、安装、部署或发布 | 范围冻结、风险与授权门、非作者审查、恢复与回滚证据 |

档位按独立工作流选择，不按代码行数选择。一个请求里可以同时有快速、标准和严格工作流；高风险步骤不会拖住与它无依赖的工作。

## 长期项目怎样运行

### 一个总控，一个台账，一个滚动 Goal

- **项目总控**持续拥有整个项目的范围、依赖、调度、共享契约、候选集成和最终验收。
- **唯一任务台账**记录当前目标、拆分、状态、阻塞、下一步、验收和证据；不复制需求正文、视觉规范、技术设计或聊天流水。
- **一个当前 Goal**对应一个可以共同验收的小里程碑，不是整个开放 backlog。里程碑通过后，总控回写台账、重算 `READY`，再进入下一个 Goal。
- **工作包**各自保留目标、边界、负责人、文件范围、验收和证据，所以 Goal 轮换不会让任务目标漂移。

Goal 不是必须由用户每次手动开启。用户明确要求长期持续推进、明确要求使用 Goal，或项目规则已授权自动 Goal，且宿主提供 Goal 工具时，总控会按里程碑自动创建或切换。普通短任务不会创建。

### 总控是事件驱动，不是后台空转

总控不需要一直占用模型。以下事件发生时，它必须醒来处理：

- 新任务或用户反馈到达。
- Agent 返回 ACK、候选或失败。
- 依赖、规则、主线或验收状态变化。
- 候选需要审查、合入、回归或真实验收。
- 工作包阻塞、恢复或改派。

每次只闭合一组因果相关动作，然后结束本轮等待下一事件。是否能顺手追加，不看动作多少：只有同一工作包、同一候选且为保持当前状态一致、安全、可恢复所必需的动作可以留下；另一任务、新实现或未来输入必须排到下一事件。但只要当前事件还有可立即执行的审查、集成、验收、ACK 追问、恢复或台账同步，就不能提前结束。

### 候选不能无限堆积

每条顺序集成流的未处理候选 WIP 上限为 1：

1. Writer 形成范围清楚的候选。
2. 总控审查实际 diff 和证据。
3. 及时顺序合入项目约定主线。
4. 在主线做相称回归和真实业务 Case。
5. 立即回写台账并释放下一个候选。

这可以防止“开发了很久，但用户一直看不到 main 上的新结果”。

生命周期门会直接扫描 Git worktree 中尚未进入主线的候选。每个 live candidate 必须明确进入审查、集成、返工、排队、外部阻塞或被替代之一；若代码已被主线等价吸收但 worktree 仍需保留验收现场，使用 `absorbed` 并绑定 absorbing main revision 与保留原因；若失败候选只为恢复取证保留，使用 `parked` 并记录 reason code、wake condition 与保留原因。`absorbed / parked` 会写入本机候选生命周期状态并退出 live candidate WIP；只要该 worktree `HEAD` 再变化，就自动重新进入 live 队列。排队只允许容量或顺序集成两种原因且必须写明下一检查点。同一集成流有 live candidate 时不能继续创建下一个 Writer，但 Desktop、Mini、Server 等互不冲突的其他流仍可并行。这样候选积压不再依赖总控“记得处理”，保留验收/恢复现场也不会反复制造假候选。

如果下游页面缺少一个尚未冻结的共享字段或语义，总控不会让下游 Agent 猜着开发，也不会只写“等待输入”后空转。缺失合同本身会成为上游工作包；它进入主线并验证通过后，同一事件立即释放下游实现。

### 阻塞一个包，不阻塞整个项目

- 工作包遇到问题时进入 `RECOVERING`，记录负责人、根因假设、恢复动作和检查点，并继续诊断、恢复原 Agent、补派或接管。
- 外部等待只阻塞依赖它的工作包；其他 `READY` 工作继续。
- 当前 Goal 和优先级只决定先后，不能把不服务当前 Goal 的开放工作排除出项目级 `READY`；P2、另一端或后续里程碑只要能独立推进，就仍是存活反例。
- 只有整个项目确实没有任何内部恢复动作、无可执行 `READY`、无活动任务或待处理候选，且所有开放路径都等待同一个外部条件时，才把系统 Goal 标为 blocked。
- 总控在调用系统 blocked 前必须用唯一台账路径运行 `scripts/preblock_guard.py` 做一次临时项目级反例扫描；门禁会比对台账开放项，不能靠漏填 P2 或后续任务绕过。这不会新增治理文档，只阻止“当前包卡住 = 整个项目停工”的错误升级。

## 多 Agent 如何协作

多 Agent 不是固定配额，也不是越多越快。

总控会根据这些条件决定使用 0 到环境上限个 Agent：

- 工作包之间没有直接顺序依赖。
- 文件所有权互斥；同一文件同一时刻只有一个写入者。
- 数据库、端口、GUI、模拟器、产物目录等共享环境已经隔离或登记。
- 并行带来的收益大于交接、审查和集成成本。

Writer 开始前必须回传可送达总控的 ACK，包括工作树、分支、`HEAD/status`、精确文件范围、首个复现或 RED、验证和停止条件。任务创建成功、消息已发送、工作树 clean 或 Agent 显示 running，都不等于开发已经开始。Agent 可以复用，但每次必须换新的 Assignment：上一 Assignment 结束 / 冻结并释放文件和 worktree，新 Assignment 再 ACK 后才能写。若握手迟延时已经产生边界清楚的 RED WIP，总控先冻结并指定唯一恢复人，再在完整 ACK 后从原检查点恢复；不粗暴丢弃有效修改，也不叠加多个 Writer。

主 Agent 不重复实现已经分派的内容；它保留共享接缝、冲突、集成和最终验收。高风险边界、纵向里程碑或发布候选在条件允许时安排非作者审查，但 Reviewer 不能成为第一个真正打开页面或运行功能的人。

## 台账怎么写才不拖慢开发

台账粒度会动态调整，不是固定模板：

- 探索阶段按未知问题或场景拆。
- 建设阶段按可独立验收的功能闭环拆。
- 验收阶段按真实业务 Case 收口。
- 认证、费用、迁移、生产写入和发布偏细。
- 低风险机械步骤没有独立验收价值时合并。

一个可执行工作包至少说明：目标、非目标、依赖、预计读写范围、状态、验收标准、验证方式、证据位置、下一步、负责人和暂停条件。完成历史可以压缩，但仍有恢复、依赖或验收价值的任务项不能删除。

当台账维护成本开始超过执行收益时，合并机械项、压缩完成历史和过程收据；不能停止登记真实任务拆分、状态、阻塞和证据，也不能另建第二台账。

推荐的最小运行形态与减法触发条件见 [长任务治理](references/long-task-governance.md)：每个任务 ID 只出现一次，历史过程由 Git 和既有证据承担。

## 五层上下文治理

| 层 | 保存什么 | 不保存什么 |
| --- | --- | --- |
| 当前上下文 | 最新要求、本轮目标、临时判断和工具结果 | 长期事实 |
| Compact | 上下文压缩或中断时的临时恢复现场 | 项目文件、完成证据 |
| Wiki | 多份资料编译后的可查询知识；Raw Sources 是其不可变原始资料层 | 当前任务状态、产品规则副本 |
| 长期记忆 | 跨会话稳定、可复用、有证据和失效条件的结论 | 当前进度、密钥、临时假设 |
| 任务台账 | 当前目标、拆分、状态、阻塞、下一步、验收和证据 | 需求正文、视觉规范、聊天流水 |

发生冲突时，优先级为：用户最新要求 → 当前权威事实源 → 当前台账项 → Wiki / 长期记忆 → Compact、聊天和工具缓存。

## 视觉任务如何避免自由发挥

视觉参考分为探索稿、认可参考图、运行时资产和已替代四种状态。用户明确认可精确图片后，Skill 会：

1. 固定文件、尺寸、哈希、适用页面/状态和排除项。
2. 把采用点转成可执行视觉规范。
3. 要求设计、开发、测试和验收使用同一基线。
4. 新组件从语义最接近的已确认组件派生；无法可靠派生时登记设计或资产阻塞，不能用粗糙 CSS、伪元素或通用组件冒充。
5. 首个可运行界面先由 Writer 在真实端自验，再交 UI/UX EARLY；冻结候选后做 FINAL。

截图存在、文件名写着 `final`、测试绿灯或代码已提交，都不等于视觉通过。验收必须绑定同状态、规定视口、浏览器缩放、DPR、运行 revision 和实际截图内容。

## 验证与真实完成

| 层级 | 证明内容 |
| --- | --- |
| H0 | 格式、Lint、类型、局部构建 |
| H1 | 单元行为与共享契约 |
| H2 | 服务、数据库和适配器集成 |
| H3 | 真实用户入口到服务和持久化结果 |
| H4 | 经授权的真实外部服务 |
| H5 | 正式产物、安装/部署、迁移、冒烟和回滚 |

完成证据必须绑定候选内容和适用环境。同一候选、环境和检查已有有效收据时可以复用；代码、契约、环境或验收策略变化后，只让受影响的收据失效。

共享数据合同还要检查真实规模和跨层单位：多记录场景是否产生 `N+1`，数据库字符长度、运行时字符串偏移、字节、时间、金额和百分比是否使用同一语义。单条 fixture、类型绿灯或接口能返回值，都不能替代这类审查。

`DONE` 至少要求：验收标准满足、实际 diff 已审查、证据已回写、候选已进入项目承诺的主线或交付位置。测试数量、静态页面、HTTP 200、旧截图、文件存在或 Agent 报告不能替代真实结果。

## 自带工具

### 初始化项目

```bash
python3 scripts/init_project.py /path/to/project --profile collaborative
python3 scripts/init_project.py /path/to/project --profile durable
python3 scripts/init_project.py /path/to/project --profile core
```

- `collaborative`：六个协作文档。
- `durable`：增加 Wiki、Raw Sources、长期记忆和项目级 Skill 骨架。
- `core`：只创建四个核心文档。
- `--without-design`：项目确实不需要视觉文档时使用。

脚本逐项跳过已有文件，不覆盖、不静默合并。

### 检查治理结构

```bash
python3 scripts/lint_governance.py --strict /path/to/project
```

检查唯一台账、重复任务 ID、下一检查点、阻塞、规则版本和文档链接。任务表是状态唯一权威；顶部不再重复维护“当前活动项”，旧项目若保留该字段则必须与 `ACTIVE / RECOVERING` 完全一致。它只发现结构问题，不能代替总控判断与真实开发。

既有项目先不加 `--strict` 查看迁移警告，完成一次范围单一的台账瘦身并对账后再启用严格门；迁移本身不得暂停无冲突开发。

每次控制事件结束前都用临时 JSON 做轻量收口；存在多个 `READY`、必需 Reviewer 或规则更新时一并声明：

```bash
python3 scripts/control_event_guard.py \
  --ledger /path/to/project/TASK_LEDGER.md \
  --repo /path/to/project \
  --require-review UX-EARLY \
  --rule-revision abc123 --affected-task writer-1 \
  control-event.json
```

该 JSON 只在当前事件中使用，不进入项目台账。除当前台账 SHA-256、派发前可用槽位、全部 `READY` 决定和显式声明的 Reviewer / 规则 ACK 外，还要带本轮 `event_contract`、`event_actions`、`terminal_receipt_issued=true`、全部未合入 worktree 的 `candidate_packages` 决定与本轮 `new_assignments`。脚本会复核整条动作链是否仍属于同一主任务和候选 revision，并直接用 Git 对账候选是否遗漏；延后 `READY` 必须使用结构化 `reason_code`，不能用“下一事件”或“稍后处理”留下空槽。门禁通过后删除临时输入，不新增治理文档。

### 让控制事件自动触发，而不是等总控想起来

`scripts/lifecycle_hook.py` 把持续总控的控制事件接到 Codex 生命周期：

- `SessionStart`：读取 canonical `main` 与唯一台账基线，并立即暴露台账一致性错误；
- `PostToolUse`：主线、工作区、台账、`READY` 或任一 worktree 未合入候选改变后立即给总控追加控制上下文；
- `SubagentStop`：把子 Agent 完成登记为待审候选事件；
- `Stop`：仍有待处理事件且没有通过的控制收据时，只允许一次受控续作；若快照没有真实变化，第二次 Stop 直接 fail closed，避免把长回合伪装成推进。

先把唯一总控登记到本机状态；临时 Writer / Reviewer 不登记：

```bash
python3 ~/.agents/skills/adaptive-delivery/scripts/lifecycle_hook.py \
  --register-controller <controller-session-id> /path/to/canonical/main
```

然后在 `~/.codex/hooks.json` 合并四个 command handler，命令都指向安装副本的 `scripts/lifecycle_hook.py`：`SessionStart`、`PostToolUse`（matcher `*`）、`SubagentStop` 和 `Stop`。Codex 的非托管 Hook 还必须在 CLI 的 `/hooks` 中审核并信任精确定义；未显示 Active 就不能声称自动门禁已生效。

总控履职评分另有独立的机器门。安装副本就绪后执行：

```bash
python3 ~/.agents/skills/adaptive-delivery/scripts/controller_scoring_hook.py --install-hooks
```

该命令在保留现有 Hook 的前提下增加 `UserPromptSubmit` 与 `Stop`：评分/履职审计请求进入时，`UserPromptSubmit` 自动把**当前安装的** `references/controller-performance-scoring.md` 正文和 SHA-256 完整注入模型上下文；`Stop` 在最终回复前复核同一精确模型。若模型中途变化，`Stop` 必须 fail closed，且不得把受长度限制的 Stop 文本冒充“完整重载”；必须重新提交评分/审计请求，由新的 `UserPromptSubmit` 完整注入当前版本后才能输出评分。评分门状态无法持久化时同样 fail closed。Hook 的模型根固定为其自身安装目录，调用方不能用 `--skill-root` 切换到另一份评分表。仍须在 Codex `/hooks` 中确认这两个 handler 为 Active / trusted；未激活时不能声称机器门已生效。

不提供 `UserPromptSubmit / Stop` 的宿主（包括无法暴露最终文本生命周期事件的 Web 宿主）无法由本机 Codex Hook 强制拦截最终回复。此时只能走 `controller_scoring_guard.py record-read --repo <project>` → `score-guard --repo <project>` 的 fail-closed 路径；任一步未通过就禁止输出分数，不能把这条兼容路径描述成宿主级自动拦截。

ChatGPT Web + AI-Bridge 需要额外的 Web bridge，因为 Web 工具调用不会天然触发 Codex Hook。`scripts/web_lifecycle_bridge.py` 可把**唯一 registered controller** 项目下由 AI-Bridge 启动的 shell 调用桥接为 `PostToolUse`，并从 AI-Bridge 审计收据确认 `control_event_guard.py` 的真实 `control-event: allowed` 输出。未登记项目静默跳过；同一 repo 若登记多个 controller 则拒绝映射；没有 repo 归属的 `computer` 事件不自动猜测归属。Web 平台没有可验证的“最终文字回复已结束”本地事件，因此 bridge 不伪造这个信号：在真实 allowed 控制收据后做短 debounce，并可自动调用 `native-stop`，复用原 controller session 走 Codex 原生生命周期；更新的收据会使旧延迟 Stop 失效，不会 fork 第二 controller。

`computer` 收据本身没有 repo / Web session ID，因此只允许**短租约归属**：先在 canonical repo 内运行 `python3 scripts/web_lifecycle_bridge.py arm-computer --cwd <repo>`，默认只授权接下来 90 秒内 1 次 GUI 收据，可显式设置 `--ttl-seconds 5..300` 与 `--uses 1..8`；租约过期或耗尽即删除，租约外 `computer` 静默忽略。需要硬 Stop 时使用 `python3 scripts/web_lifecycle_bridge.py native-stop --session-id <registered-controller> --repo <repo>`；命令先核对 registry 精确匹配，再通过 `codex exec resume` 续接**同一 thread**，不会 fork 或创建第二 controller。常驻 `audit-once` 可加 `--auto-native-stop --auto-stop-delay-seconds 5`：每个真实 `control-event: allowed` 收据会安排一次延迟 native Stop，同 session 的更新收据会覆盖旧计划。Goal 收口事件同时必须在临时控制 JSON 中声明 `goal_rollover`，否则 `control_event_guard.py` 直接拒绝收据。

Hook 不会替总控盲目派发。它负责自动发现和阻止漏处理；文件冲突、共享环境、优先级与 `spawn_agent` 仍由总控判断。存在未合入候选时，只有带 `--repo` 且完整声明候选处置的 `control_event_guard.py` 收据才能清除本次自动控制事件。

### 防止短事件继续无边界加任务

```bash
python3 scripts/event_scope_guard.py event-append.json
```

临时 JSON 包含 `event_contract` 和 `proposed_action`。合同固定事件 ID / 类型、主任务、候选 revision、允许动作 / 文件和终止收据；每次想把新动作塞入当前回合前运行。输出 `SAME_EVENT` 才能继续，`QUEUE_NEXT_EVENT` 表示先结束当前事件，等待下一次输入或另开回合。文件用完即删，不写进台账。

### 防止 Agent 复用和 ACK 越界

```bash
python3 scripts/assignment_lease_guard.py assignment.json
```

每次 Assignment 都有独立 ID。`RESERVED` 阶段禁止写文件；`ACKED / ACTIVE / CANDIDATE` 必须带完整 ACK，修改只能落在 owned files；复用同一 Agent 时，上一 Assignment 必须已经冻结 / 结束并释放文件和 worktree。临时 JSON 不进项目提交。

### 防止台账和真实任务状态漂移

```bash
python3 scripts/ledger_consistency_guard.py /path/to/project/TASK_LEDGER.md
```

任务表是状态唯一权威。门禁检查 Goal / 下一检查点是否指向开放任务；“下一可见检查点”或任务“下一步”里出现的任务式 ID 必须先有显式任务行；`ACTIVE / RECOVERING` 必须有负责人，且 `RECOVERING` 必须绑定 delivered Assignment ACK 或已完成的可验证恢复动作；控制收据还必须枚举全部 `ACTIVE / RECOVERING` 的真实运行租约，不能省略或错配。旧式顶部活动指针仍须与任务表一致。`control_event_guard.py` 现在也会内联执行这组一致性检查，不能靠事件收据绕过。实时槽位和 Writer / Reviewer 数量只能出现在临时控制收据，不能在台账顶部复制。新项目直接启用；旧项目先完成一次不阻塞开发的范围单一迁移，再把它放到台账提交和总控 yield 前。

### 复用验证收据

```bash
python3 scripts/verification_guard.py run /path/to/project \
  --check-id full-suite -- your-test-command
```

收据保存在目标仓库 `.git/adaptive-delivery/`，不进入提交，也不保存命令输出或密钥。确需对同一快照重跑时，使用受支持的 `--force-reason`。

### 检查视觉验收收据

```bash
python3 scripts/visual_evidence_guard.py path/to/visual-receipt.json
```

检查绑定参考、哈希、状态矩阵、视口、缩放、截图尺寸、运行时资产和组件派生关系。机械检查通过仍不等于图片内容审美或视觉一致性已经通过，非作者仍要打开真实候选和参考图复核。

## 初始化后各文件负责什么

| 文件或目录 | 唯一职责 |
| --- | --- |
| `AGENTS.md` | 新会话启动、事实源路由和协作边界 |
| `TASK_LEDGER.md` | 唯一执行控制面 |
| `SPEC.md` | 产品行为和验收 |
| `DESIGN.md` | 视觉、交互和认可参考图 |
| `TECHNICAL.md` | 架构、数据、接口、Harness、发布和恢复 |
| `EVOLUTION.md` | 有证据且会改变未来行为的项目经验 |
| `WIKI_INDEX.md` / `wiki/` | 多资料的结构化查询入口和编译知识 |
| `raw_sources/` | Wiki 的不可变原始资料层 |
| `MEMORY.md` | 稳定、可复用、可验证的长期结论 |
| 项目级 `SKILL.md` | 本项目反复使用的固定风格、专业流程和引用 |

已有项目只有 `PROJECT_STATUS.md` 时可以继续把它作为唯一台账；不得同时维护 `PROJECT_STATUS.md` 和 `TASK_LEDGER.md` 两套控制面。

## 授权边界

“开始开发”只授权项目范围内必要实现，不自动授权：

- 付费调用或超出已确认预算的重试。
- 读取、输出或保存密钥。
- 远端 push、PR、发布、部署或账号操作。
- 数据删除、迁移、覆盖或其他难恢复动作。
- 把参考图直接作为运行时资产。

这些动作分别在实际发生前确认。未获授权的高风险步骤停在门前，不阻塞其他不依赖它的工作。

### Web Adapter 与无 AI-Bridge 降级

Adaptive Agent Runtime 的 Core 不依赖 AI-Bridge。检测到 AI-Bridge 时，Web Adapter 可以把明确归属到唯一 registered controller repo 的本地工具事件桥接进同一生命周期，并用 `scripts/web_lifecycle_bridge.py session-start --repo <repo>` 生成 `AGENTS → TASK_LEDGER/PROJECT_STATUS → MEMORY → WIKI_INDEX → Git/runtime` 的恢复载荷。没有 AI-Bridge 或其他本地桥时，Web 进入 `pure_web_file` 降级模式：仍可按 Core 规则工作，但不能声称看得到本地 Git/runtime、自动唤醒本地 controller 或执行本地 shell。

同一 controller thread resume 若返回 `active writer` 冲突，会保持 pending 并标记为 deferred；它不等同于宿主失效，因此不会为了“恢复”再创建第二 controller。

## 安装

安装整个仓库，不要只复制 `SKILL.md`；否则会缺少 references、scripts 和测试。推荐使用同一个安装入口完成 Core 与当前机器可用 Host Adapter 的配置：

```bash
python3 scripts/install_skill.py \
  --source /path/to/adaptive-delivery-source \
  --summary "Install Adaptive Agent Runtime" \
  --impact none \
  --stop-condition "installation verified"
```

安装器保持技术 Skill ID 与机器状态路径 `adaptive-delivery` 不变，但 manifest 对外记录 `Adaptive Agent Runtime / adaptive-agent-runtime`。检测到 Codex 时会幂等合并 lifecycle + scoring hooks；检测到 AI-Bridge 时会幂等安装 Web shell bridge；已有 Hook 和 `.zshenv` 其他内容不会被覆盖。Codex 非托管 Hook 的 **Active / trusted** 仍必须由宿主自身确认，因此即使配置已写入，能力报告在无法机器验证 trust 时仍为 `degraded`，不会虚报完全启用。没有 AI-Bridge 时安装本身仍成功，Web 本地能力明确降级为 `pure_web_file`。如只需要复制 Core、明确不希望安装器修改宿主配置，可加 `--no-configure-host-adapters`。

### Codex

```text
请使用 skill-installer 从 https://github.com/ManechoLIU/adaptive-delivery 安装 adaptive-delivery
```

或手动安装：

```bash
git clone https://github.com/ManechoLIU/adaptive-delivery ~/.agents/skills/adaptive-delivery
```

Codex 可以按描述自动选择，也可以显式写 `$adaptive-delivery`。

### Claude Code

```bash
git clone https://github.com/ManechoLIU/adaptive-delivery ~/.claude/skills/adaptive-delivery
```

### Gemini CLI

```bash
gemini skills install https://github.com/ManechoLIU/adaptive-delivery
```

### Cursor

可在 **Customize → Rules → Add Rule → Remote Rule (Github)** 输入：

```text
https://github.com/ManechoLIU/adaptive-delivery
```

各宿主的发现路径和调用语法不同，但 Skill 的项目事实源、授权和完成规则保持一致。

## 常见疑问

### 总控 idle 是不是停止工作？

不一定。正常 idle 表示当前没有立即可执行动作，等待候选、ACK、用户反馈或外部状态变化。若仍有审查、集成、验收、恢复或可派发 `READY`，此时 idle 就是调度失职。

### Agent 越多越快吗？

不是。文件、数据库、GUI 或依赖冲突会让过度并行变成重复实现和集成负担。Skill 追求的是关键路径吞吐，不是 Agent 数量。

### 台账是不是固定不变？

不是。它会随风险、返工面、熟悉度和维护成本拆细或合并，但必须持续记录真实工作包和状态。

### Compact 能代替台账吗？

不能。Compact 是临时恢复摘要；台账是项目当前控制面。Compact 恢复后还要用 Git、台账和权威事实源校验。

### Skill 能自动读取旧会话吗？

不能保证。跨会话一致性依赖当前项目文件、Git 和明确交接；旧聊天只作为低优先级线索。

### 通过测试就算完成吗？

不一定。用户可见功能通常还需要当前主线上的真实入口、持久化、失败恢复、设备或浏览器状态，以及必要的外部服务或发布证据。

## 目录

```text
adaptive-delivery/
|-- SKILL.md
|-- README.md
|-- references/
|-- scripts/
`-- tests/
```

- `SKILL.md`：Agent 使用的精简入口和路由。
- `README.md`：给人看的完整使用指南。
- `references/`：按场景加载的详细治理规则。
- `scripts/`：可重复的初始化和机械守卫。
- `tests/`：结构、脚本和关键行为回归。

## 贡献与演进

欢迎提交能够减少遗漏、返工、停滞或形式主义的改进。请同时说明适用条件、失败证据、最小修正和不应影响的场景。

单项目经验不会自动升级为全局规则。只有已经去除项目隐私和供应商特例、具有跨场景价值、写明适用与不适用条件并获得授权后，才更新全局 Skill。

## License

[MIT](LICENSE)
