# Long-task Governance

## 目的与适用边界

本方法用于需求、设计、技术探索、开发、验收、发布和治理等会跨轮次推进、需要证据或必须可恢复的工作。普通问答、一次性讨论、明确且低风险的小改不创建 Goal、台账项或额外治理文件。

“上下文治理”是信息分工方法，不是新的项目文件。完整分层、知识编译和初始化规则见 [context governance](context-governance.md)：

| 层 | 职责 | 是否创建项目文件 |
| --- | --- | --- |
| 当前上下文 | 当前轮最新要求、临时判断和工具结果 | 否 |
| Compact / 交接摘要 | 上下文压缩或任务中断时续接现场 | 否 |
| Wiki | 编译多份资料并提供查询入口 | 需要时使用 `WIKI_INDEX.md` |
| 项目长期记忆 | 保存稳定、可复用、可验证的项目结论 | 长期项目使用 `MEMORY.md` |
| 任务台账 | 控制目标、状态、依赖、证据、阻塞与恢复 | 使用唯一任务台账 |

完整上下文治理初始化会创建项目级 `SKILL.md` 作为固定风格与专业流程入口；它只保留项目特有能力和引用，不复制本 Skill 的通用方法。非 durable 档不默认创建。

## 唯一台账与文件命名

新项目使用 `TASK_LEDGER.md`。已有项目按以下顺序定位：

1. 存在 `TASK_LEDGER.md`：使用它。
2. 仅存在 `PROJECT_STATUS.md`：把它视为旧名称的唯一任务台账，继续沿用。
3. 两者都存在：先对账并确定唯一控制面，不在冲突消解前推进状态。
4. 两者都不存在：只有长期或跨轮次工作才创建 `TASK_LEDGER.md`。

台账不是项目周报、需求文档或会话日志。完成历史由 Git 和交付证据保存；产品、视觉、技术和经验只链接到各自权威文档。

## 各阶段如何使用台账

| 阶段 | 台账项 | 典型证据 |
| --- | --- | --- |
| 需求探索 | 待验证问题、范围与决策门 | 用户确认、调研来源、原型结论 |
| 产品与视觉设计 | 流程、页面范围、候选与确认层级 | 精确稿件、审查记录、可操作原型 |
| 技术探索 | 架构未知、Spike、风险与回退 | 运行结果、接口或数据实验 |
| 建设 | 可独立验收的功能闭环 | 测试、真实入口、持久化结果 |
| 验收与发布 | 真实 Case、回归、授权和恢复 | 浏览器或设备证据、发布候选、回滚验证 |
| 治理与恢复 | 候选对账、阻塞消解、事实源迁移 | Git 图、diff、状态快照、链接检查 |

临时构思和不需要跨轮次恢复的讨论留在当前上下文，不进入台账。

## 粒度与状态

台账粒度是控制不确定性、失败半径和校准成本的旋钮，不是排版格式。建议前判断五个变量：

1. **失败代价**：返工、数据、费用、合规或外部副作用越大，越要细拆。
2. **需求清晰度**：目标或验收越模糊，越先拆成问题、场景或决策门。
3. **项目或资料质量**：结构混乱、事实源冲突或测试薄弱时先缩小范围；结构与 Harness 可靠时可以粗拆。
4. **执行者熟悉度**：对代码、领域和工具越陌生，越需要短反馈周期。
5. **人工校准意愿**：愿意频繁确认可细拆；希望低介入则用较粗的功能闭环，但提高证据门槛。

回答“这个项目该用什么台账、拆多细”时必须输出：**推荐粒度**、**不推荐其他粒度的原因**、**必须细拆**的边界、**可以粗拆**的范围、台账项字段、**调整触发**和**人类确认点**，并给出首版任务拆分。默认采用混合粒度：探索按问题或场景、建设按可独立验收的功能闭环、验收按真实业务 Case；不是给整个项目永久贴一个粒度标签。

- 探索阶段按未知问题或验收场景拆细；建设阶段默认使用功能级闭环；验收阶段按真实业务 Case 收口。
- 权限、资金、认证、迁移、外部写入、生产配置和发布始终偏细；低风险机械任务只有独立验收价值时才单列。
- 大面积改文件、长时间无证据、反复换方案、大规模返工或下一步动作不清楚时立即拆细。
- Agent 频繁等待、台账维护超过执行成本或机械项没有独立验收价值时合并。

台账出现同一任务 ID 多处重复、顶部长期保留旧事故 / 旧 Reviewer / 旧漂移过程，或小状态变化产生大量纯台账提交时，立即做一次范围单一的减法整理：任务表是状态唯一权威；顶部只保留当前 Goal、下一可见检查点、真实阻塞和规则版本。“当前活动项”应删除，确需展示时只能从任务表 `ACTIVE / RECOVERING` 派生且必须完全一致。实时槽位、Writer / Reviewer 数量和本轮 `READY` 决策只写临时控制收据，不进入台账顶部。每个任务 ID 只出现一次；`ACTIVE / RECOVERING` 保留完整恢复字段，`READY / PENDING` 保留一行必要信息，`DONE` 压缩为结果、提交和证据链接。整理前后必须对账任务 ID、状态、依赖、授权门和证据集合，不能以瘦身为由删除仍有恢复价值的项目实例，也不新建历史台账。

既有项目首次采用这套结构时，先运行非严格 lint 取得迁移警告，在不暂停无冲突实现的前提下完成一次范围单一的台账迁移；对账任务 ID、状态、依赖、授权门和证据无遗漏后再启用 `--strict`。不能把新结构直接作为既有项目的突然阻塞门，也不能因兼容旧格式而永久跳过迁移。

总控只使用五个主状态：`READY / ACTIVE / VERIFY / BLOCKED / CLOSED`。`READY` 表示开放但尚未执行；`ACTIVE` 表示正在真实执行，恢复动作也属于 ACTIVE；`VERIFY` 表示已有结果，正在等待已命名的审查、集成、回归、真实 Case 或发布门；`BLOCKED` 只表示内部恢复路径已穷尽且确实等待外部条件；`CLOSED` 表示承诺的交付边界已经满足或工作被正式替代。心跳、RED/GREEN、candidate、review PASS、integration、regression、recovery count 都是机器证据或门，不新增主状态。

既有项目仍兼容旧台账词汇：`PENDING → READY(dispatchable=false)`、`RECOVERING → ACTIVE(health=recovering)`、`DONE → CLOSED(done)`、`SUPERSEDED → CLOSED(superseded)`；旧词只作为解析兼容，不再作为总控需要额外管理的状态。已有 `RECOVERING` 行仍须保留恢复负责人、当前根因假设、下一恢复动作和触发检查点，直到项目自然迁移；新规则不要求为了改名批量重写历史台账。一个执行波次可以包含多个彼此独立的 `ACTIVE` 工作包，但它们必须没有直接顺序依赖、负责人和文件所有权清楚、共享运行环境已隔离或登记。无法满足这些条件时顺序执行。同一工作包同一时刻只有一个负责人，同一文件同一时刻只有一个写入者。

每个可执行项至少包含文章所需的九类信息：目标、非目标 / 边界、依赖、预计读写范围、当前状态、验收标准、验证方式、证据位置和本项下一步动作；再记录负责人、暂停条件。高风险项增加回滚、禁止动作和人类确认点。验证收据只在确有复用价值时登记，不是所有工作包的必填字段。

方法层与项目实例必须分开：本参考保存状态定义、粒度选择、拆分信号和完成规则；项目任务台账保存当前目标、任务拆分、状态、阻塞点、下一步动作、验收标准和证据位置，同时明确项目已选择的实际粒度与当前拆分结果。边界、依赖、负责人、读写范围、暂停条件和高风险门可嵌入对应任务项；已完成台账项及其证据在仍有恢复、验收或依赖价值时保留。不得把通用粒度分类、方法说明或教学文字复制进项目台账，也不得以“方法已移入 Skill”或“Git 已有历史”为由删除仍有查询价值的项目实例。

## Goal 自动判断与循环

只有满足以下授权前提之一时才可创建 Goal：用户明确要求使用 Goal；用户明确要求本 Skill 持续推进长期任务；项目规则已记录长期任务自动 Goal。仅因普通任务隐式加载 Skill，不构成 Goal 授权。

授权成立且运行环境支持 Goal 时，出现任一条件即自动创建或在当前项结束后切换 Goal：

- 任务跨阶段、跨会话或需要 Compact 后继续；
- 存在多个依赖工作包、Agent、分支或 worktree；
- 涉及认证、费用、迁移、外部写入、发布或其他高风险副作用；
- 用户要求持续执行直到可观察结果完成；
- 执行中出现重复阻塞、目标漂移或需要从大项拆出场景项。

普通问答、一次性审查、单文件低风险小改和无需恢复的短任务不创建 Goal。一个 Goal 对应一个可共同验收的小里程碑；其中可以只有一个工作包，也可以包含同一执行波次内互不依赖的多个工作包。不能把开放 backlog、没有共同验收的高风险模块或整个项目塞入一个 Goal。项目总控持续拥有整个项目，但系统同时只运行一个滚动里程碑 Goal；各工作包自己的目标、边界、验收与证据由台账固定，不随 Goal 轮换而漂移。并行旁路工作可以保持 `READY / ACTIVE / VERIFY`（legacy `RECOVERING` 计入 ACTIVE），但不得静默扩张当前 Goal 的完成条件。

建设阶段选择滚动 Goal 时优先关闭业务功能纵向切片。每个 Goal 必须新增一个可观察的用户能力，或关闭一个明确阻止父级业务闭环 / 发布门通过的功能、可靠性、安全或迁移缺口；若两者都不是，就只作为现有 Goal 的工作包，不单独升级为系统 Goal。选择时用一句话说明它关闭哪个父级缺口以及为何优先于其他 `READY`，不另建评分表、排期报告或治理文档。

循环为：读取当前 Goal 与工作包 → 核对现场 → 标记本波次 `ACTIVE` → 执行与验证 → 分别回写状态、证据、风险和各项下一步动作 → 完成或阻塞 Goal。需要协调时另写一个“协调下一动作”，但它不替代各工作包自己的下一步。不得先结束 Goal、后补台账。

不同阶段使用相称闭环：开发项采用“实现与定向检查 → 真实业务 Case → 修复优化 → 审查全部 diff → 冻结候选 → 取得与风险相称的完成证据 → 范围单一的本地提交 → 台账回写”；需求、设计、调研和文档项用对应可观察产物与确认或链接证据，不为形式强行运行代码或操作界面。内容与环境未变且验收规则允许时可复用收据；项目要求新证据时按要求复验。

## 控制事件、ACK 与恢复

总控的“持续”是持续拥有项目交付责任，不是让一个 turn 长时间占用执行通道。工作包新增、移除、改派，依赖或状态变化，候选产生、验收失败、规则更新和用户反馈都属于控制事件。每个事件回合只闭合一组相关动作，并用一次**原子控制事务**同步当前 Goal、活动工作包、`READY`、阻塞、下一可见检查点、证据和协调下一动作；若新增工作不能与当前 Goal 共同验收，就明确排入后续 Goal，不得静默扩张或另开第二个当前 Goal。

控制事件按因果事务而不是固定动作数量划分。事件开始时仍只在临时输入中固定 event_id / event_type / primary_task / candidate_revision / allowed_actions / allowed_files / terminal_receipt，每次追加动作前由 scripts/event_scope_guard.py 判定。实际跨 scope 的实现、另一候选的内容修改、无关反馈或等待未来输入仍属于后续事件；但由完整项目投影推导出的 project-wide dispatch / defer / block / recompute 是同一 Controller 控制事务的一部分，即使目标来自另一业务线，也不能仅因 primary_task 不同而推迟。此类跨任务控制面动作必须无文件写入、不得直接开始实现，并明确来自 project-wide runnable projection；否则继续 QUEUE_NEXT_EVENT。Reviewer PASS → integration → current-main verify → fact convergence → project-wide recompute，以及 Reviewer FAIL → correction assignment → dispatch，都属于 mandatory continuation，不能在阶段性成功处人为切断。事件结束时仍把合同与真实动作日志交给 control_event_guard.py 复核，保证跨任务只扩展控制面调度，不扩张实现 scope。

原子同步后重新计算执行容量。无文件、环境或顺序冲突的 READY / derived-runnable 项应在容量内派发；无法派发时记录精确原因。未集成候选保持 CANDIDATE / VERIFY，不因它尚未集成而饿死其他已就绪工作。阶段性 PASS、Reviewer verdict、integration success 或一次控制收据都不是天然 Yield 边界。Controller 必须继续消费当前安全可执行的 mandatory successors，动作产生新事实后再次 project-wide recompute；只有所有 runnable 已有 DISPATCH / DEFER / BLOCKED 决策、Controller 自身动作已执行或合法 hard defer/block、事实已经收敛，且 Continuation Debt=0，才允许形成最终 Stop/Yield 收据。

每个控制事件结束前都用 `scripts/control_event_guard.py --ledger <唯一台账> --repo <canonical repo>` 校验一次临时 JSON。输入除台账 SHA-256、槽位、全部 `READY` 决定、必需 Reviewer 与规则 ACK 外，还必须包含本轮 `event_contract`、`event_actions`、`terminal_receipt_issued=true`、全部 live 未合入 worktree 候选的 `candidate_packages` 决定和本轮 `new_assignments`。若事件合同表示当前 Goal / 里程碑正在完成、关闭或收口，还必须提供 `goal_rollover`：先做项目级依赖 / READY 重算；有下一可执行工作时 `status=rolled` 并把台账当前 Goal 指到另一个真实开放包，整个项目已完成时才可 `project_complete`；若声称全项目阻塞则 `status=project_blocked`，并附完整 `blocked_scan` 让同一门禁内联执行 `preblock_guard`，不能用一句自然语言“无 READY”代替项目级证明。门禁直接从 Git 枚举未合入候选，拒绝遗漏候选、同一集成流堆积超过一个 live candidate，或在该流仍有 live candidate 时继续开新 Writer；代码已被主线等价吸收但为真实端验收保留 worktree 时可标 `absorbed`，失败候选仅作恢复取证保留时可标 `parked`。这两种终态通过成功收据写入本机候选生命周期状态并退出 live WIP，原 worktree `HEAD` 变化时自动失效并重新进入 live 队列。确因容量或顺序集成排队的候选必须写明 `queued` 原因和下一检查点，无冲突的另一端 / 另一集成流仍可并行。

**Project-context Fact-First 门禁：** 每次回答“当前项目 / 当前治理 / 现有模型 / 现有机制 / 当前 Runtime 状态”等问题前，必须读取当前适用的项目规则链、项目 SKILL、Runtime SKILL、台账、Git 与 canonical runtime state，并把读取结果冻结为本回合 receipt。同回合纠偏刷新必须优先使用 UserPromptSubmit receipt 冻结的 prompt-time working directory；Stop 事件即使省略 cwd 或把 cwd 规范化为 repo root，也不能覆盖这个作用域。只有不存在旧 receipt 时才可回退到当前事件 cwd。这样才能确保从根目录到原始 cwd 的全部适用 AGENTS.md 作用域链始终生效。Stop 前不仅要检测“已存在文件内容变了”，也要检测事实源状态迁移：例如 prompt 时 `runtime_state=not_found`、Stop 前变成 `verified`，必须判为 authoritative source changed、刷新事实并在同一回合纠偏。请求“现有机制”但权威事实源解析为 `not_found` 时，最终回答只能是 uncertainty-only 的 `UNKNOWN / NOT FOUND`（或等价“未找到 / 未知”），可选地再附一条受限说明：当前权威事实源没有找到、建立或确认该机制。该说明使用封闭语法，不接受任意尾随文本；不能因为回答里出现过一个 UNKNOWN token，就继续附加“但/不过/确定”等转折结论、分数、维度、替代规则、provider/model 或其他确定性机制。缺失事实不能用聊天历史、模型记忆或近似方案补造。

### Mandatory Continuation 与 Continuation Debt

每次 Writer/Reviewer terminal、Reviewer PASS/FAIL、Controller verification、integration、recovery、disconnect、lease-expired、runtime unhealthy、dependency satisfied、task-state change 或事实漂移进入控制循环后，都先转换为 canonical state transition，再推导 mandatory successor。Reviewer PASS 默认形成 integration → current-main verification → fact convergence → project-wide recompute；Reviewer FAIL 默认形成 correction/rework → dispatch；integration PASS 默认释放对应执行占用、收敛 ledger/runtime/Git/receipt 事实并重算全项目 runnable。已持久化且 requires_user=false 的明确 next_action、待消费 terminal receipt、candidate/review/recovery/correction 同样进入 canonical controller-action projection，不建立第二 scheduler。

这些立即动作同时形成 Continuation Debt。control_loop_receipt 必须列出 continuation_debt_ids 以及尚未解决的 open_continuation_debt_ids；只有 executed，或具备 hard reason_code、精确原因、可追溯机器证据（DEFER 还要 next_checkpoint）时才算清债。FACT_PROJECTION_DRIFT、INTEGRATION_NOT_CONTINUED、POST_INTEGRATION_RECOMPUTE_MISSING、KNOWN_NEXT_ACTION_NOT_EXECUTED、CONTINUATION_DEBT_NOT_CLEARED 等偏差直接进入现有 deviation → correction → recurrence L1-L4；同 fingerprint 复发升级，L4 仍只能按唯一 Controller 交接规则处理，绝不产生第二 Controller。Self-Check 发现任何 open debt 或 runnable 时必须改变当前控制决策，不能只输出“下一步应该继续”。

`available_slots` 不再是总控可自由填写的判断：控制收据必须同时携带 `capacity_projection={source: host_runtime, evidence: receipt:...|artifact:..., total_slots, occupied_task_ids[]}`；门禁把占用任务与台账 `ACTIVE / RECOVERING` 投影逐项对账，并按 `max(0, total_slots - occupied)` 重算槽位，不一致即拒绝。`READY` 延后必须填写结构化 `reason_code`，只接受容量已满、文件冲突、共享环境、顺序集成、外部阻塞或授权门，不能用“下一事件”“稍后处理”或主观优先级留下空槽。每个 `new_assignments` 还必须声明 `execution_mode`、非空 `owned_files` 与路由合同；默认路由要绑定真实项目规则文件和 SHA，安全 fallback 要绑定已终止且结果已知的前序失败证据，`controller` 自写只能使用受限 `controller_exception` 与精确停止条件。必需 Reviewer 与规则更新的受影响 live 任务仍由命令行显式声明，脚本不会自行推断。输入不写入项目文件；门禁只替代确定性的完整性检查，不能代替总控对文件冲突、优先级、证据质量或真实完成的判断。

仅靠结束时手动运行门禁不足以约束长回合。持续总控注册为 **registered controller** 后，使用本 Skill 自带的宿主适配器接入同一 `scripts/lifecycle_hook.py` 生命周期执行器：桌面 Hook 使用 `SessionStart / UserPromptSubmit / PreToolUse / PostToolUse / SubagentStop / Stop`，Web Bridge 把可信 Web 会话事件翻译成相同机器事件。两端共用唯一 Controller、台账、runtime、candidate、规则握手和四门，不新增宿主专用 READY 或第二状态机。

桌面 SessionStart / UserPromptSubmit 建立当前回合，PostToolUse 只持久化有界的真实工具调用 ID、工具名、输入哈希和结果状态，不保存命令正文；registered controller 的 control_event_guard.py 必须由 Hook 所用的同一精确 Python 解释器直接执行安装副本内的精确 Guard 路径，不接受同名脚本、替代解释器或 shell 拼接，并携带 --controller-session。临时控制 JSON 中的 machine_trace 必须与该会话当前 turn_id 的机器投影完全一致。轨迹超过有界容量时本回合拒签，不能静默丢弃早期调用。PreToolUse 继续给控制收据建立串行栅栏：仍有已放行工具时拒绝启动收据，收据运行中拒绝并发放行其他工具。成功收据只原子授权“这个精确投影此刻可以 Stop/Yield”，不是禁止继续工作的命令；同一回合若 Controller 继续执行安全后继动作，Hook 立即撤销旧 receipt_turn_id / must_yield 授权、恢复 pending_control_event，并要求动作结束后重新 project-wide recompute 与签发新收据。控制收据必须枚举 canonical controller actions 为 Continuation Debt；executed 或有机器证据的 hard BLOCKED/DEFER 才能清债，open_continuation_debt_ids 非空时 Stop/Yield 必须 fail closed。重复 Stop 不能成为逃生口。纯状态查询只提供 Observation 快照，不作为 continue/resume/scheduling 触发，也不改变已有 runnable、debt 或评分状态。

Hook 只对 canonical repository `main` checkout 上登记的唯一逻辑总控生效；这里的 canonical 描述仓库表面，绝不表示出站 task / thread 目标。Writer / Reviewer 工作树不受影响；Hook 负责事件感知、工具轨迹与确定性门禁，不能代替总控判断文件冲突、优先级或 Assignment 内容。部分宿主工具路径可能不触发 Hook，因此项目台账、Git/runtime 对账和终态收据仍是第二道边界。非托管 Hook 必须完成运行平台对精确定义的审核与信任；随后显式 arm 一次绑定当前 hooks、lifecycle 与 target-guard 哈希、单一 Controller 和随机 run ID 的 Canary，并在同一协议链按顺序观察 `SessionStart → PreToolUse allow → PostToolUse → receipt Stop authorization → same-turn continuation invalidates authorization → fresh receipt / Stop → SubagentStop`。安装器只有在该严格序列 24 小时内完成后才把桌面适配器标记为 `enabled`；跨会话、乱序或旧安装观察不能拼成通过收据，否则保持 `degraded`。

### Controller Ownership 与 Session Binding

Controller 是长期身份，Session 是临时载体，Binding 是机器证明，Authorization 是执行权限。Runtime 必须把这四个概念拆开，禁止再用一个 controller_identity=VERIFIED/UNVERIFIED 同时回答“项目有没有总控”和“当前会话能否代表它执行”。

project_controller_state 只从现有 Controller registry + Git common-dir 派生：EXISTING / UNIQUE / ACTIVE 表示项目已有唯一逻辑 controller_id；ABSENT 才表示尚未建立 Controller；两个真实 registry ownership 命中同一项目时为 CONFLICT 并 fail closed，绝不静默挑一个。这个项目级 ownership 不依赖某一具体 host session receipt 存活，因此当前宿主证明链缺失时不能把 Controller 描述成“不存在 / 不是总控 / 需要重新任命”。

session_binding_state 单独描述当前执行入口：host / session_id / verification / reason / provenance / binding_mode / target_generation；其上统一导出 `identity_state=VERIFIED|DEGRADED|UNVERIFIED|CONFLICTED`。VERIFIED 才允许 Controller-exclusive mutation；DEGRADED 表示唯一 logical Controller、ownership/lineage 与无冲突事实仍成立，但 Host verifier、capability、registration 或治理/安装合同暂不可用，此时安全 control-plane 动作（consume terminal/result/verdict、fact projection、project-wide recompute、dependency resolution、continuation/recovery bookkeeping、deterministic read-only validation）继续，身份敏感或不可逆 mutation 单独 fail closed；UNVERIFIED / STALE 表示当前入口证据不足，仅关闭该入口的 Controller-exclusive side effects；CONFLICTED 只用于真实双 Controller、foreign session 或 generation/ownership 冲突，并同时关闭恢复和 mutation。任何非 VERIFIED 状态都不清除项目 ownership、Goal、pending lifecycle、Continuation Debt、runnable、Reviewer/candidate/recovery。已有唯一 Controller 时 create_new_controller_allowed=false；只有 project_controller_state=ABSENT 才可进入 NEW_CONTROLLER_REQUIRED。

用户再次说“你是这个项目唯一总控”属于治理意图与 ownership authorization：它可以帮助确定应恢复 registry 中哪个 existing controller_id，但不能代替 Host session machine proof。反过来，Host session proof 缺失也不能抹掉既有任命。完整证明链是“现有唯一 project controller → trusted current host session → bind/current target → execution authorization”。

Same-controller session recovery 只复用现有 registry、__controller_sessions__、__controller_targets__ 和可信 Host verifier，不创建新事实源或第二状态机。可信新 Web session 到达时，Runtime 在 registry 锁内再次确认项目仍只有原 controller_id、session 未被其他 Controller 占有且没有 active outbound lease，然后只给该 existing Controller 增加/保留 alias、把 Web current target generation 单调推进到新 session；旧 alias 保留为 STALE lineage。Desktop target 和 ownership 不受 Web target 换代影响。没有注册可信 Host verifier，或宿主根本不暴露可信 session id 时，恢复保持 UNVERIFIED，reason=HOST_SESSION_ID_UNAVAILABLE，不写 registry、不创建 Controller。

Project-context / SessionStart receipt 必须同时输出 project_controller_state、session_binding_state、identity_state 与 Runtime identity capability contract。例如唯一 Controller 已存在但 Web ID/Host verifier 不可得时，应显示 project Controller EXISTING / UNIQUE / ACTIVE，identity_state=DEGRADED，current execution HOST_SESSION_ID_UNAVAILABLE，controller_actions_allowed=false，safe_control_actions_allowed=true，create_new_controller_allowed=false；验证能力存在但新 session 尚未证明属于该 lineage 时才是 UNVERIFIED / SAME_CONTROLLER_SESSION_RECOVERY_REQUIRED。治理规则要求的 capability 在精确安装副本不存在时必须报告 `RUNTIME_CONTRACT_DRIFT`，canonical direct identity CLI 为 `scripts/controller_target_guard.py identity`；禁止把缺失的 `web_lifecycle_bridge.py controller-identity` 之类旧入口当身份失败。恢复为 VERIFIED 后，restore payload 继续携带同一 Controller 的 pending_control_event / triggers / Continuation Debt / runnable_ids / candidate_revisions / next_action；只要 Goal 未完成且存在可安全继续的 mandatory work，就立即回到原 Controller control loop，而不是停在“身份恢复完成”。

顶层 registry key 仍是稳定逻辑 controller_id，Hook 实际来源记为 source_session_id，消息、导航与 native resume 的唯一目标是当前宿主的 execution_target_session_id。这些字段可能相等，但禁止因曾经相等而混用。

`--bind-desktop-session <controller-id> <desktop-session-id> <repo>` 只登记历史可识别别名，别名本身没有执行权。切换当前桌面任务必须运行 `--replace-desktop-session <controller-id> <desktop-session-id> <repo> --expected-generation <n>`：这是 registry 锁内的 generation CAS，首次显式迁移 legacy 或 alias-without-target 只能传 `0`；已有 target 只能传其当前 generation，缺失或陈旧值一律 fail closed。replace CLI 本身只机器校验 CAS、active lease 与 ownership，**不能**验证宿主 interrupt / status / provenance；可信宿主或操作者必须在调用前完成该对账，本仓库不得把模型收据、审计引用或 CLI 成功冒充宿主停止证明。成功后它替换唯一 active slot、严格递增 generation，并保留旧 alias 作为不可执行的已知入口；replace **只切未来路由**，不能撤销已经启动的宿主动作。没有该宿主证据时不得对新 target resume / write。`SessionStart / UserPromptSubmit / PreToolUse / PostToolUse / Stop / SubagentStop` 等桌面入站事件在读写 lifecycle state 前必须先证明 `source_session_id` 是当前 target；否则静默不接管、不写 state、不触发唤醒。退休当前入口使用 `--unbind-desktop-session <controller-id> <desktop-session-id> <repo> --expected-generation <n>`；它使用同一 CAS 规则写入 `unbound` tombstone，不能删除记录后回退到旧 canonical。`bind` 永远不能激活 tombstone，复用同一 session ID 也必须执行新的 replace 并形成更高 generation。只有完全没有 desktop alias 和 target 元数据的旧 registry 才兼容使用 canonical 入口；已有 alias 但没有 current target 时一律 fail closed，必须显式 replace / unbind。

native wake 在启动进程前自动经过 `scripts/controller_target_guard.py`，并以 registry 共享读锁把最终 target resolution 与进程启动绑定；wake receipt 同时记录逻辑 `controller_id`、`execution_target_session_id` 与 target generation，换代后旧 debounce 收据失效。Hook 只把三类 Controller 定向动作作为受保护出站：发送消息、导航，以及显式线程目标的打开任务；其他 App / MCP 工具不在此覆盖宣称内。它们在 `PreToolUse` 校验目标并以完整 Hook payload 的 `tool_use_id` 持久创建 target-bound lease，lease 覆盖实际 tool use，且只在匹配的 `PostToolUse` 后释放。active lease 存在时 replace / unbind 必须被阻止；缺少完整 Hook payload、`tool_use_id` 或匹配 PostToolUse 一律 fail closed，不能以模型日志或自报收据补齐，lease 也不会自动过期。仅在可信宿主已核实 terminal / interrupted 后，管理员才可执行 `controller_target_guard.py reconcile --repo <repo> --host <host> --controller-id <controller-id> --tool-use-id <tool-use-id> --action <action> --target-session-id <id> --generation <n> --host-receipt-reference <reference> --reason <reason>`：它只释放精确匹配的 lease / target / generation，保留最近 64 条审计；审计引用只定位已核实事实，不能成为宿主证明。不经过该 Hook 的这三类调用必须先运行 `controller_target_guard.py check --repo <repo> --host <desktop-host> --action message|navigate --target-session-id <id>` 并只使用 `ALLOWED` 收据中的精确目标。独立 check 与 App 执行之间不具备宿主原子性；宿主未暴露 Hook 或原子 capability 时适配器必须标为 degraded，Skill 文本不能冒充 App 已执行或机器已强制该 Guard。

| 误判理由 | 机器结论 |
| --- | --- |
| “canonical 一直是 execution thread” | canonical 只证明逻辑归属；存在显式 current target 时向 canonical 出站必须拒绝。 |
| “alias 列表只有一个 / 最后一个就是新的” | 列表没有 current 语义；缺少 target generation 就 fail closed。 |
| “用户又粘贴了旧链接 / 旧任务仍 active” | 链接、标题和活跃状态都不改变 registry 当前代际。 |

出现目标来自历史记忆、canonical 与 current 不同、alias 无 generation、旧任务仍在运行或 Guard 收据缺失时，立即停止出站动作；不得先发后核、同时通知两边或用“同一逻辑总控”解释双任务并行。


长时外部 Agent / Reviewer 的结束不能只依赖同步 shell 返回。需要自动接续时，`run_external_agent.mjs` 先持久化标准 `external_agent_terminal` receipt（可绑定已持久化 `result_path`），再由 `terminal_continuation.py` 强制校验 receipt 自带仓库身份并以 Git common-dir 找到现有唯一 Controller；同步阶段只写入 `SubagentStop` lifecycle event 与待消费 receipt 路径，Wake 由独立子进程调用现有 Supervisor，因此 `DEFERRED` 不会把已完成 Agent 重新标成失败。未消费 receipt 在 `SessionStart` 仍保持 pending，native resume 明确要求先读这些结果；只有真正闭合该 pending control event 的成功 control-event receipt 才清空已消费路径。该机制只补“结果可持久化 → 结束可触发 → 恢复可继续”的最短闭环，不引入第二 Controller、watchdog 或另一套任务状态机。
### Controller Health 与 Wake Supervisor

Adaptive Agent Runtime 只保留一个 registered controller。`Controller Health` 是从现有 controller binding、生命周期、host adapter 与 runtime 事实即时派生的只读投影，不是 `no mutable health ledger` 之外的新状态源；`Wake Supervisor` 也是确定性基础设施，不是第二个 Agent、第二个 controller 或第二套项目状态机。项目身份边界仍是 `Git common-dir`，controller worktree events require binding proof；普通 Writer / Reviewer worktree 不能凭同一 common-dir 冒充总控执行面。

任何真实事件只要让 `pending_control_event=true`，都进入同一 generic wake path。健康结果只用于选择同一 registered controller 的执行方式：`ACTIVE` 不重复唤醒；`DEFERRED`（例如 active writer）保留 pending 并等待新机器证据后重试；可恢复宿主问题先做 same-controller continuation。当前不捆绑生产 peer host verifier，generic peer fallback 必须保持 degraded / `DEFERRED`；只有宿主提供并注册可信 host-attested verifier 后才可标为 `CONFIRMED` 并进入既有授权 fallback。缺 verifier、验证失败或仅有本地 / 模型自报时 fail closed。`DEAD` 必须有足够机器证据证明当前与 peer continuation 都不可安全执行，并进入 explicit Resume/Replace handling，绝不自动 Replace。wake success does not clear pending；只有既有 control-event closure receipt 才能清除该事件。

生命周期 state 同时记录最近真实控制事件的 `controller_host`：Web 桥收据标记 Web 宿主，原生桌面 Hook 标记桌面宿主；具体机器枚举只在模型路由参考中定义；同一 registered controller 跨端续接时随最新真实事件更新，禁止把 thread ID 固定等同于某个宿主。该字段用于 Dispatch Gate 的就近与 peer-host fallback，不是新的项目状态。

浏览器托管的控制面通过本地工具桥操作项目时，如果宿主不产生原生生命周期事件，只允许做受限桥接：canonical repo 只用于解析 project_controller_state，**不能作为当前 Web conversation/session 的身份证明**。Legacy registry key 只表示稳定逻辑 controller_id / state lineage；controller_session_id 保留该逻辑身份，web_session_id 只在 Host machine proof + current target 校验通过后才能使 session_binding_state=VERIFIED，真正的桌面续接目标由 execution_target_session_id 单独解析，event_source 与 execution_host 分别记录事件来源与实际执行宿主。Registry 的 `__controller_sessions__` 保存 Web binding，只有显式 `bind-web-session` 才可在锁内原子写入；它不负责发现 Web ID，调用方必须先从可信宿主来源取得。一个 Web session 不能同时归属两个 Controller。`post-shell`、`session-start`、`translate-receipt`、`arm-computer` 对 registered repo 都必须携带匹配的 Web session identity；常驻 `audit-once` 若启动参数未携带 Web session，只允许从仍有效的 `manual_user_authorized / resume_only` lease 解析同一已绑定 session，否则 fail closed，禁止按 repo 自动冒认；GUI lease 还必须把相同 `web_session_id` 写入租约并在消费时复核。当前工具桥如果根本不暴露可信 Web conversation/session ID，就明确降级：zsh 自动桥不生成 lifecycle 事件且不得覆盖原 shell 退出码，不得伪造绑定。Web Adapter 只有在上述身份成立时才可生成 `AGENTS.md → 唯一台账 → MEMORY.md → WIKI_INDEX.md → Git/runtime` 的 SessionStart 等价恢复载荷。控制门闭合还要回读工具桥的真实审计收据，只有 `control_event_guard.py` 输出包含 `control-event: allowed` 才能当成成功收据；不能仅凭 shell 退出码推断。没有 repo + verified Web identity 的 GUI 事件不得猜测映射。宿主最终回复没有可验证的本地 `Stop` 回调时，桥接层不能伪称直接观测到“回复结束”；可在真实 allowed 控制收据后做短暂 debounce，再自动调用 `native-stop`，由 Guard 解析当前桌面 target 后复用同一逻辑 Controller 的生命周期。native resume 使用桥自身的确定性 runtime PATH，并在真正 resume 前预检 repo、逻辑 Controller、当前 target generation、宿主 CLI 可执行文件、所需解释器 / runtime 与轻量版本检查；失败保持 `pending_control_event=true`，不能把 lifecycle 当作闭合。若宿主 thread-store 明确返回当前 target `already has an active writer`，机器记录 `RESUME_DEFERRED_ACTIVE_WRITER`：这表示当前 target 已有 writer，不是 peer-host 不可用，也不得因此创建第二总控或再次跨端；pending 必须留存，并由既有 auto-native-stop 以有界退避继续重试相同 generation，不能把“当前 writer 正忙”降级成等待用户再说继续。同一 Controller / auto-stop state / receipt 的延迟 supervisor 必须是单实例租约：重复 schedule 只增加 coalesced 证据而不得再起 detached 进程；只有持当前 supervisor token 的实例可以原子替换 token 并 rearm 下一轮，不同 receipt 才能 supersede 旧实例。旧 token、stale receipt 或已失去租约的 detached 进程必须无副作用退出，禁止用并发 supervisor 堆积维持 continuation。其他失败才按机器 failure class 判断是否属于既有 host fallback 允许集合。detached launcher 的 stdout/stderr 写入限量轮转日志，state 仅保留有界 head/tail diagnostics、精确 command、returncode 与时间戳，不再丢到 `/dev/null`。只有 Guard-approved target resume 成功才写 `RESUME_CONFIRMED`。连续新收据会覆盖旧的延迟 Stop；target 换代会使旧 debounce 收据失效；任何时候都不创建第二个长期总控。由于 `goal_rollover` 已在控制收据层先强制，自动 native Stop 是第二层生命周期保险，而不是依赖 Web UI 时序猜测正确性。

完成既有项目的一次性迁移后，台账每次提交或总控 yield 前运行 `scripts/ledger_consistency_guard.py <ledger>`：当前 Goal 和下一检查点必须指向开放任务，`ACTIVE / RECOVERING` 必须有唯一负责人；任何出现在“下一可见检查点”或任务“下一步”里的**任务式 ID**都必须先成为显式任务行，但明确标注为 `Assignment <ID>` 且可归属到已声明父 Task 的执行身份不创建额外 Task 行；Assignment / session / lease / attempt 身份只进入 canonical runtime / audit receipt，不成为项目状态。`RECOVERING` 除恢复动作 / checkpoint 外，还必须绑定已 delivered ACK 的 Assignment，或记录已完成且可验证的恢复动作。控制收据还必须逐项回报全部 `ACTIVE / RECOVERING` 的真实 Assignment 租约状态，不能省略、错配或用文字承诺代替；若总控已经完成一次恢复动作但任务仍需继续，必须形成新的可执行 Assignment，否则应按真实结果转入 `VERIFY / BLOCKED / DONE`。若仍保留顶部活动指针则必须与任务表完全一致。门禁失败只修受影响字段，不展开台账重写；未迁移项目先用普通 lint 得到警告并继续无冲突实现。

下游工作唯一缺口是尚未冻结的共享契约、字段语义或适配边界时，不得把“输入未准备好”长期写成空槽理由。先把缺失输入拆成能独立验收的上游工作包，明确下游释放条件；上游进入主线并通过相称验证后，在同一状态事件中更新台账并释放下游。契约冻结前禁止下游猜测字段、状态或单位并行实现；这类顺序等待是有效依赖，不是低并行失职。

每条顺序集成流的未处理候选 `WIP`（Work In Progress，尚未完成或尚未提交的现场）上限为 `1`：候选通过约定检查并到达总控后，先审实际 diff、顺序集成到项目主线并完成相称的主线回归，再释放同一集成流的下一个候选；无冲突的其他集成流和旁路工作仍可并行。候选接收、主线集成、主线回归、真实端 `PASS / FAIL` 或阻塞解除都属于必须在同一短事件中最小回写的状态转换，不能拖到整轮验收末尾；若台账中的主线或候选指针落后真实仓库，总控不得 yield。该同步只改受影响工作包、证据和下一步，不展开台账重写或历史整理。

候选审查发现相邻缺陷时，先判断它是否使当前契约或验收结论无效：会使当前结果无效的缺陷纳入本包修复；不影响当前边界的缺陷登记为独立工作包、证据和优先级，不静默忽略，也不借机扩张当前 Goal。相邻缺陷不能成为拖延当前有效候选集成或随手跨范围重构的理由。

**Stop/Yield Gate**：任何宿主事件只要已经持久化明确 next_action 且 requires_user=false，该 continuation 就属于现有 pending_control_event，在 closure receipt、真正用户依赖或可验证阻塞出现前，每一次原生 Stop 都必须继续 block，重复 Stop 不能作为逃生口。注册 Controller 每次取得控制权后必须完成固定 control loop：读取 current main / TASK_LEDGER / runtime / candidate / receipt 事实 → 消费 terminal / Reviewer / candidate / recovery 事件 → 从整份台账推导 project-wide runnable 与 PENDING derived slice → 计算容量、文件/环境冲突和依赖 → 对每个 runnable 给出 DISPATCH / DEFER(reason) / BLOCKED(reason) → 在容量允许时并行派发所有无冲突 runnable → 清空 Controller 自己立即可执行的 review / integration / acceptance / recovery / ACK / correction → 再次 project-wide 重算。Stop/Yield 前的 control_loop_receipt.completed_steps 必须按此固定顺序完整出现，并绑定当前 ledger hash、完整 runnable/candidate/Controller-action/correction 集；不能用“检查过了”或 Self-Check 文案替代机器收据。存在 ACTIVE Writer 只说明有人仍在工作，不能证明 Controller 本人已经无事可做。任何 machine-verifiable Controller deviation 都必须从既有 controller-cycle-evidence FAILED receipt 派生 mandatory correction，并作为同一 canonical project control projection 中的 Controller action 处理；未闭合 correction、空闲容量下漏派发、未消费 Reviewer/candidate/recovery 或无当前回合完整 control-loop receipt 均禁止 Stop/Yield。不得为 correction 建第二台账、第二 scheduler 或第二 Controller。

总控带着仍在运行的子任务 yield 前，还必须验证候选完成、ACK、失败或需要关注事件是否会**实际触发总控的新回合**；不能根据“任务仍 running”或产品可能支持事件通知就假设会自动唤醒。若当前运行环境不会自动触发新回合，则在结束前选择一种真实可用的恢复路径：使用产品提供的任务续作 / 线程唤醒机制，或保持有界等待直到子任务给出本轮约定的 checkpoint。没有可用唤醒路径时，该子任务仍是总控当前动作，不能把它登记为“等待候选”后 idle。额度上限、客户端退出或其他平台级强制中断属于外部中断；恢复后第一步从最近候选、工具事件与工作树 checkpoint 续接，不把中断前的旧 `ACTIVE` 状态当成仍在执行。

任务创建或消息发送不等于已经开工。Agent 实例和本次 Assignment 是两个身份；运行层可记录 `RESERVED → ACKED / ACTIVE → CANDIDATE` 握手 / 产物阶段，但这些不是项目主状态。Writer / Reviewer 的 delivered ACK 至少包含绝对仓库根、分支与 `HEAD / status`、精确所有权、首个复现或 RED、停止条件；项目可按风险追加运行环境与禁止范围。通过 `run_external_agent.mjs` 启动带 Assignment 身份的外部 Agent 时，必须先提供精确 ACK 文件，并由 `scripts/assignment_lease_guard.py` 对 Assignment、任务、Agent、仓库、分支和当前 HEAD 做启动前校验；不合规时在 Agent 进程 spawn 前失败。上一 Assignment 未 `FROZEN / TERMINAL`、文件 / worktree 未释放，或新 Assignment 未取得完整 ACK 时不得写入。Writer 的 canonical runtime lease 同时占用 `task:<task_id>` 与规范化绝对路径 `worktree:<path>` 两个互斥键；任何仍非 terminal 的 Writer 只要任务或可写 worktree 任一冲突，新 Assignment 在 start 前 fail closed。Reviewer 不取得 worktree 写锁并保持只读；若 Reviewer 转成 Writer，必须先形成新的 Writer Assignment 并取得该 worktree 锁。Reviewer 改为同候选 Writer 后失去该 revision 的非作者资格。若 ACK 前已经出现 WIP，立即冻结该现场并只指定一个恢复负责人；补齐 ACK 后从原 checkpoint 恢复，不因握手缺失自动丢弃有效 WIP，也不得叠加多个恢复 Writer。第一次缺少 delivered ACK 时，总控必须主动定向 follow-up，要求尽快收缩到可见 checkpoint，不能因执行者没有主动汇报而放任不管。第二次判定中断前必须同时核对**消息送达、任务活动、工具事件、工作树状态**四类证据；即时核对不新建表格、报告或治理文档。

**Controller deviation → correction**：Runtime 与 Controller 责任分开。Runtime 负责唤醒同一个 Controller、提供完整 project-wide machine projection，并把 terminal/candidate/review/integration/recovery 事件送入控制循环；Controller 负责每次取得控制权后履行完整 control loop，不能把局部重算、漏派发、提前 Stop 或错误归因全部推给 Runtime。任何能被 TASK_LEDGER、runtime、Git、Reviewer、验收证据或机器规则确定性证明的偏差，都从既有 FAILED controller-cycle-evidence 收据结构化为 deviation_code / responsibility / affected_scope / stable fingerprint / level / mandatory correction。Correction 不拥有新事实源，只是同一 canonical project control projection 中的强制 Controller action。L1 当轮本地纠正；L2 当前闭环 mandatory correction；L3 项目级调度/关键路径 fault，强制完整 project-wide recompute 与独立验证；L4 同根因复发或 Controller 已不能可靠恢复时，只能按现有唯一 Controller 交接规则处理，绝不能并存第二 logical Controller。相同 fingerprint 的历史 FAILED receipts 会提高 recurrence_count 并升级 level；已纠正后再次发生仍按复发处理。关闭 correction 必须提供 traceable execution evidence 与独立 verification evidence，执行者和验证者不能相同；底层 receipt persistence 同样拒绝伪造 corrected closure。fingerprint 只是既有 machine receipt 的派生证据，不成为新的项目状态台账。

**Web Agent / Reviewer execution adapter**：Web Controller lifecycle bridge 与 Web 子 Agent execution 是两类不同身份，禁止把前者的 web_session_id 绑定逻辑直接当成后者的运行证明。任何 Web Writer/Reviewer spawn 前必须先 prepare 产生 canonical dispatch ticket；只有独立 Host verifier 证明 fresh / non-replay provenance，并明确 attested Runtime 可读取的 exact machine-event path 后，Runtime 才能消费与 ticket 匹配的 started observation，并由 bind 创建 canonical lease。公开 start_web_assignment()、caller-supplied Host event ingest 与 CLI direct start 永久 fail closed；公开 API 不接受 verifier callback，调用方不能靠 event_source_probe、自写 JSON/JSONL、浏览器/tab/DOM 状态、lambda verifier 或其他本地可写材料自证 trusted source。下划线/private helper 名称本身也不是权限边界：任何会写 canonical Web lease / terminal 的内部 helper 必须在实际 mutation 前再次从 Host-attested source path 或 Host verifier 复验 provenance，不能信任上游已经解释好的 observation dict。新 Web Assignment 必须记录 agent_id / provider / model / agent_type，Host-attested spawn 事件必须与预登记 model / agent_type 精确一致，否则 bind fail closed。成功 bind 后 canonical lease 写 execution_transport=web、health_mode=progress_watchdog，并保留 task/worktree exclusive keys；Reviewer 只持任务互斥键。正式安装的 launchd health-only scheduler 只写 readiness heartbeat 并周期调用既有 web_lifecycle_bridge.reconcile_managed_web_assignments，不实现第二套 wake/reentry，也不修改应用 Host 或 AI-Bridge Host。reconciler 只从 canonical lease、Host-attested structured child events 与可验证 Git/evidence progress 判断现场；页面存在、PID、tab、DOM 文案、Connection interrupted 或 missing UI 信号都不能刷新 progress，也不能被提升为 terminal。只有从 Host verifier attested event path 解析出的 completed / failed / interrupted / cancelled / disconnected 才能作为对应 execution session 的机器 terminal；terminal mutation helper 只接收 attested path locator + exact observation id，并自行重新解析与核对当前 session / started_at。canonical lease 的 started_at 在 structured collaboration 与 Host-attested 两条路径都必须绑定经过复验的真实机器 started 事件时间；Host verifier 还必须对该 started timestamp 本身做 attestation，不能使用 Host ingestion、health supervisor 扫描或 reconcile 的时间，避免 deadline 漂移或合法 terminal 被错误判为“早于 lease”。公开 direct structured-terminal ingest 永久 fail closed。progress_watchdog lease 的 PID/heartbeat lease 过期不作为 Web execution terminal；达到 progress deadline 后先进入 stale grace，超过 grace 才 unhealthy:progress_stale_beyond_grace。terminal 或 unhealthy 统一经 terminal_continuation → 既有 same-controller Web reentry supervisor 交回同一个 registered logical Controller；web_reentry_unavailable 这类瞬时 reentry 失败保留 pending 并重新 arm 同一 supervisor，禁止第二套 wake 状态机。被唤醒 Controller 拿到新的 replacement execution session 后走 recover，Runtime 从 canonical 合同自动派生 attempt+1 与新 lease并共享 recovery budget；side_effect 结果未知、legacy side-effect 合同未知或无法机器证明 provider mutation boundary 的情况全部 fail closed。Reviewer 的 prepared/bound Assignment 必须冻结精确 immutable candidate revision。当前正式 Web 子 Agent 宿主没有独立的 production Host machine-event verifier，Runtime 的公开 machine-event status 也不暴露 verifier 注入参数，因此即使 health service 已配置，web_agent_execution 仍保持 host_limited 且 configured=false，structured_terminal=unavailable、host_terminal=unavailable；Runtime 不会为了“看起来能跑”退化到本地 JSONL 或浏览器观察。已有 canonical external executors 继续按正式 route 使用。same-controller reentry 后、任何新 delegation 前，Controller 必须从完整 TASK_LEDGER 重算 DAG / READY / WIP、derived-runnable 与 route policy，不能把上一次 Web executor 当作隐式 route cache。

运行时 heartbeat / PID 只证明存活，不证明进展。只有 Git HEAD、tracked worktree status hash、测试 / 证据 receipt、artifact fingerprint 或 blocker evidence fingerprint 至少一项产生新的非空值，`assignment_progress` 才刷新进展 deadline；重复同一指纹只续 heartbeat。Assignment runtime 以 `git rev-parse --git-common-dir` 下的 `adaptive-delivery/runtime-assignments.json` 为跨 main / Writer / Reviewer worktree 的唯一机器账本；`run_external_agent.mjs` 对 Assignment-bound 执行必须先用共享 `route_contract` 校验 Assignment route 与实际 CLI provider/model/auth mode 精确一致，并在 `safe_fallback` 时从 canonical prior lease 证明真实失败 terminal，再在 spawn 前把含 exact `model / agent_type / auth_mode / policy_class / route_decision / route_contract` 的 `assignment_started` 原子 apply 到该账本。JSONL 只作可选审计副本，不能替代 canonical state。相同任务合同默认最多允许两次 recovery；第三次执行若再次失败、进展超时或进程失效，运行层派生 `health=budget_exhausted`，同一 Assignment 禁止再开 attempt 4；总控必须把策略变化固化为新的 Assignment 合同（缩范围、换 Agent/session/provider、修环境后的新执行、拆 Assignment、限时接管或形成真实外部 BLOCKED 证明），不能继续同路径重试。最终成功不会抹除历史 recovery count。

为控制治理复杂度，runtime reconciliation 只替换不可靠的存活推断，不新增第二套状态机，也不新增总控人工必填字段。普通短任务不强制 checkpoint；只有长任务、高风险任务或已经进入恢复路径的工作包才需要可恢复检查点。恢复时优先从最近已验收 checkpoint 继续，已验收阶段不重复执行；checkpoint 是恢复锚点，不是新的项目状态或审批层。

回收只保护恢复点，不解决阻塞。既有台账回收后可把该包写作 `RECOVERING`（主状态仍为 `ACTIVE + health=recovering`），先区分消息 / 握手、任务运行、工具故障、工作树 / 分支冲突、依赖环境和目标合同等原因，再选择恢复原执行者、从 checkpoint 补派、收缩范围、修复环境或由主 Agent 接管。只有恢复路径已经穷尽且确实等待用户、外部系统、凭证、付费授权或其他不可内部消解的状态时，工作包才标 `BLOCKED`。

Required Review `PASS` 后若声明已集成，`control_event_guard.py` 还必须用 Git 证明 candidate revision 是当前主线 revision 的祖先，并绑定 current-main regression evidence；只填写 main revision 字符串不能证明已集成。Reviewer 通道失效时，优先按相同协议追问并补派独立 Reviewer。只有关键路径因此停滞、当前没有可安全补派的审查者，且总控未参与该实现时，总控才可进行一次限时、窄范围的非作者审查；给出 verdict 与证据后立即回到调度，不承担常驻或跨页面审查，也不能因此让其他无冲突 `READY` 闲置。

工作包 `BLOCKED` 不自动映射为系统 Goal blocked。只有当前 Goal 和项目都没有可执行的 `READY / ACTIVE / VERIFY`（legacy `RECOVERING` 计入 ACTIVE）、没有待集成候选、内部恢复路径已经穷尽，并且所有剩余工作都等待同一个真实外部条件时，才可把系统 Goal 标记为 blocked；否则保持系统 Goal active，继续推进不受影响的旁路工作，并在原阻塞解除后恢复该里程碑。旁路工作不改变当前 Goal 的完成条件，也不需要创建第二个系统 Goal。

这里的“项目”必须按唯一台账的全部开放工作包解释，不能缩成当前 Goal、当前阶段、P0/P1、正在验收的端或总控主观选择的优先级。优先级只能决定先做哪个，不能让本来可执行的 P2、后续里程碑或另一端工作从存活扫描中消失；“不服务当前 Goal”“不占旁路槽”“以后再做”都不是不可执行原因。GUI、模拟器、显示会话、凭证、付费授权或外部 API 暂不可用时，只把依赖该资源的包标为等待；代码、文档、审查、另一端或其他环境不依赖它时仍须继续。

调用系统 `update_goal(blocked)` 前必须完成一次短暂、非文档化的**项目级反例扫描**：固定唯一台账 revision，逐项检查全部开放包能否在当前文件、环境、授权和槽位下产生真实进展，同时检查 live 任务、待审 / 待集成候选及总控本人动作。使用 `scripts/preblock_guard.py --ledger <唯一台账>` 校验这份临时 JSON；脚本会直接解析台账中的开放工作包 ID，并拒绝遗漏或伪造的扫描清单。脚本通过只证明当前输入和台账满足阻塞前提，总控仍须对运行环境、授权和任务状态判断负责。扫描结果不另建表格或长期报告，只在调度收据保留一行摘要。任何一个可推进反例、活动任务、候选或总控动作都会禁止系统阻塞。

Goal 工具要求“同一阻塞连续出现三轮”只是系统阻塞的必要门槛，不是充分理由；项目级反例扫描仍然优先。自动化工具报告“Mac locked”或界面不可见时，先把它描述为该 GUI 验收资源不可用，并用独立系统状态复核；即使两项都显示锁定，也不能据此推断整个项目无可执行工作，更不能把用户是否主动锁屏作为未经证明的事实。

事实源、产品、视觉、技术、协作或验收规则变化时，变更者须向所有受影响的 live 任务发送精确版本、变更摘要、影响范围和新停止条件，并逐个取得 **loaded ACK**。Adaptive Agent Runtime 自身发布必须通过 `scripts/install_skill.py` 写入精确 revision、文件哈希、影响范围与停止条件的安装 manifest；升级只能沿当前正式安装 revision 的 Git 祖先链前进：候选仓库必须真实包含 installed revision，且 installed revision 必须是 candidate 的祖先，否则安装 fail closed。临时仓库或 detached hotfix 允许用于诊断和候选验证，但在正式安装前必须把该 hotfix Git lineage 吸收到 canonical Runtime 历史，禁止“安装副本比主仓库更新、后续开发从旧主线继续”的双事实源。完整 Web Runtime 候选还必须由安装器从 exact candidate revision 重新物化并执行固定的跨语言高风险回归集：瞬时 Web reentry 失败必须重新 arm 既有 supervisor，Writer/Reviewer terminal 与 stale 必须回到同一 Controller，重复 terminal 不得重复 wake；project-wide fairness / correction / verifier-seam 回归必须通过；Node external routing 还必须验证 heterogeneous Kimi/Grok、CLI route mismatch fail-closed、safe fallback 需要 canonical prior terminal、external start 持久化 exact route contract。`route_contract.py`、`assignment_lease_guard.py` 或这些测试文件任一缺失本身都视为发布门失败。registered controller lifecycle 自动比较 installed revision 与项目 Git common-dir 中的 `rule-handshake.json`，出现漂移时直接注入 `rule_update_pending:<revision>`，不依赖浏览器 / GUI 消息。规则更新的唤醒由机器按影响和当前执行事实分三档：命中正在运行 Assignment 的路由、运行时、交付或生命周期关键变化使用 `immediate`；影响后续 live Assignment 但允许当前原子事务安全收尾时使用 `after_event`，当前事务可闭合但不得开启新 Assignment，闭合后立即加载；不影响当前运行时行为的更新使用 `next_turn`，只在下一自然回合加载，不主动打断。三档都复用同一 handshake 和 lifecycle state，不新增通知台账，也不由模型临场猜测。总控读取新规则后用 `scripts/rule_handshake.py ack` 对 exact revision 生成机器回执，再把现有台账“规则版本”行同步到同一 revision；在 `impact=live_assignments` 时，ACK 或台账同步任一未完成，新的 Assignment-bound external Agent 都必须在 spawn 前 fail closed。文件已共享、消息已发送、安装已完成或台账单独更新都不能替代完整握手；无关工作继续。只有 installed revision、controller loaded receipt 与台账规则版本三者一致后才能宣称新规则对该项目生效。

调度偏差优先从最近可运行 checkpoint 纠正：更新控制面、收缩范围、补派互斥 `READY` 或回收单个失效握手。一次候选失败、一次 ACK 迟到、一个 clean 工作树或一次调度失误都不足以更换总控。只有同一总控在收到纠偏要求后**连续两次**仍违反调度闭环，或发生必须立即止损的严重安全、数据或授权事故，才从当前主线与台账恢复点更换总控；新总控接管后旧总控停止，始终只保留一个。

## 开工前候选与共享环境对账

开始新实现前先固定当前仓库、`HEAD`、分支和未提交 diff。只有项目实际存在多个 worktree、相关候选或共享运行环境，且它们可能与当前范围重叠时，再检查：

1. 与当前工作包相关的 worktree、分支、未合并提交和候选产物；
2. 相关候选父链、实际 diff、测试与事实源版本；
3. 数据库、缓存、对象存储、端口、后台进程和输出目录是否被多个工作区共享。

分支或 worktree 只隔离文件，不自动隔离运行环境。共享环境状态只能作为待对账证据，不能证明当前分支已实现功能。发现重叠候选时先登记依赖与冲突，选择一个主候选后补缺口；禁止在未对账时从头重复实现。并行工作默认隔离 schema、端口和产物目录；无法隔离时在台账记录所有者与副作用。

共享 GUI、设备模拟器或外部验收工具只保留一个由集成者维护的**唯一常驻验收入口**，该入口绑定当前候选路径和环境。隔离 Writer 默认交付 checkpoint，不为各自 worktree 反复注册长期入口；只有唯一入口无法保真复现且已登记原因时才创建临时入口，并在事件回合结束前移除入口记录，但不删除源码、候选或 worktree。

## 主线、授权与完成

项目应声明唯一主线分支，不在全局规则中硬编码 `main`。新功能候选必须记录集成归宿；未集成到主线前保持 `VERIFY`，不得标记 `CLOSED`（legacy `DONE`）。如果项目已记录持续 merge / push 授权，可以在门禁通过后执行；否则停在已验证本地提交和明确授权点。

合并、远端 push、PR、发布、部署、付费调用和破坏性操作分别遵守各自授权边界。功能验证通过不自动扩大外部写入权限。

`CLOSED`（legacy `DONE`）要求验收标准全部满足、证据已回写、实际 diff 已审查，并且集成状态与项目承诺一致。测试数量、文件存在、旧日志、HTTP 成功或 Agent 报告不能替代真实结果。

## Compact 与恢复

Compact 不创建项目文件，也不作为完成证据。临时摘要至少保留：用户最新要求、当前 Goal / 执行波次 / 台账项、目标与非目标、仓库根与 `HEAD` / 分支 / 未提交 diff、已完成及证据、仍有效的验证收据及其候选快照、未完成步骤、约束与禁止动作、失败路径、阻塞、各活动项下一步动作和必要的协调下一动作。

恢复时先用仓库、唯一台账和权威文档校验摘要；摘要中的漂移事实失效后，以当前证据为准。稳定结论才进入 `MEMORY.md`；多资料综合理解才进入 Wiki；两者都不能直接改变台账状态。
