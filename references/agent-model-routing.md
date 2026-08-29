# Agent and model routing

## 职责边界

Adaptive Delivery 负责把项目目标拆成可验收工作包，为每个工作包产出一次路由合同，并直接调用相应的原生子 Agent 或外部运行器执行。Task Navigator 等生命周期插件只选择 Codex 任务容器、保持续接、标题和最近进展；不得读取、执行或修改项目路由合同，也不得参与拆包、模型、供应商、认证、文件所有权、集成或验收。

主 Agent 持有范围、共享契约、写入所有权、集成与最终验收。可用并发不是配额：只有两个以上文件所有权不重叠、无顺序依赖的 `READY` 工作包才并行；同一文件同一时刻只有一个 Writer。

模型路由授权只允许总控在对应类别内自动选择执行通道，不会自动扩大工作范围或强制每个包都单设调试 / 非作者审查。总控按失败代价、候选独立性和现有审查证据决定是否拆出 Reviewer；`backend` 授权只覆盖后端实现、后端调试和后端候选审查，不包含前端、UI/UX 或视觉验收。默认模型表示当前工具能力、项目偏好与成本约束下的首选，不代表该模型对所有任务绝对更强；已有高质量候选、低风险小改或不可安全拆分时仍可由主 Agent 直接闭环。

## 路由顺序

1. 先读取项目 `AGENTS.md`、任务台账、当前工作区和已确认的用户选择；项目或本轮显式选择优先于本参考的默认值。
2. 先判断是否需要分派。轻量任务、根因尚未查明的 Bug、共享契约仍未稳定或主 Agent 可直接闭环时不为使用模型而拆包。
3. 对可分派工作包只标一个主类别：`frontend`、`backend` 或 `general`。跨前后端任务先按文件所有权拆开；无法安全拆开的任务由主 Agent 或通用高能力 Agent 承担。
4. 选择执行通道与最低可靠推理强度，生成路由合同后再调用执行器。

## 默认模型策略

| 工作包 | 首选执行通道 | 首选模型 | 说明 |
| --- | --- | --- | --- |
| `frontend` | 外部 Kimi Code Agent | Kimi K3 | `auth_mode: oauth` 使用 Kimi Code 会员登录；`auth_mode: api` 使用 Kimi 开放平台按量付费 API Key，模型 ID 为 `kimi-k3` |
| `backend` | 外部 Grok Build Agent | Grok 4.6（`grok-4.6`） | `auth_mode: oauth` 使用 Grok 会员；`auth_mode: api` 使用 xAI API Key |
| 清晰、窄范围、重复性 `general` | Codex 原生子 Agent | `gpt-5.6-luna` | 优先低时延、低成本 |
| 常规实现、调试、审查 `general` | Codex 原生子 Agent | `gpt-5.6-terra` | 默认通用执行层 |
| 架构、复杂根因、高风险审查、总控 `general` | Codex 原生子 Agent | `gpt-5.6-sol` | 只在复杂度和失败代价需要时使用 |

推理强度从能可靠完成任务的最低档开始；不得把最高档作为所有子 Agent 的默认值。用户或项目选择了固定模型时，不得静默换成别的模型。

## 推理强度选择

主 Agent 对每个工作包分别选择模型和推理强度，不继承某个全局固定档。原生或外部子 Agent 派发均不得省略 `reasoning_effort`；执行通道不能精确支持所选档位时停止派发，不退回供应商默认值。

| 档位 | 适用判断 |
| --- | --- |
| `low` | 固定输出、机械检查、窄范围重复修改，错误容易发现和重做 |
| `medium` | 边界明确的常规实现、测试补充或局部审查 |
| `high` | 多文件实现、普通调试、需要权衡的代码审查 |
| `xhigh` | 架构决策、复杂根因、跨模块契约或高风险审查 |
| `max` | 失败代价极高且额外推理能改变结果；不得作为默认档 |

复杂度变化时为下一次派发重新选择；已经运行的 Agent 不在中途静默改模型或强度。用户或项目固定了模型、计费通道或强度时优先服从；未固定部分仍由主 Agent 按上述判断选择。

## 认证与计费通道路由

- 外部模型合同必须显式填写 `auth_mode: oauth | api`。OAuth/会员与 API Key 是不同的认证、额度和计费路线，执行器不得自行选择。
- 先采用项目规则或用户本轮明确选择。若两种模式都可用但合同未指定，必须在真实调用前请用户选择；不得把“已有登录”或“检测到 Key”当成计费授权。
- 同一种模型也可能支持两条通道：模型 ID 不足以证明实际计费路线，必须同时核对 `agent_type`、`auth_mode` 和凭据来源。
- 一条通道不可用时停在对应门禁前。除非路由合同明确授权，不得从 OAuth 自动切到 API、从 API 自动切到 OAuth，或改用另一个模型。
- 当前用户的本地默认选择是：Kimi K3 使用 `api`，Grok 4.6 使用 `oauth`。项目或本轮的新选择可以覆盖它；该偏好不是公开插件对其他用户的默认值。
- 需要登录、凭据配置、无调用预检或真实执行时，读取 [External Agent authentication and execution](external-agent-auth.md)。这些能力由 Adaptive Delivery 自己提供，不要求安装 Task Navigator 或 Codex Continuity。

## 外部模型门禁

- Codex 自定义 Provider 当前要求 Responses API。Moonshot 的 Kimi API 当前公开的是 Chat Completions 兼容接口，因此 Kimi K3 的两种认证模式都经 Kimi Code CLI 执行，不能把 API 模式伪装成原生 Codex Provider。OAuth 使用 Kimi Code 管理的会话与模型别名；API 模式使用临时 `KIMI_MODEL_*` 配置，密钥不写进合同。
- Grok 的两种认证模式都经 Grok Build CLI 执行。OAuth 使用 `grok login` 的可刷新会话；API 模式必须隔离 OAuth 会话后注入 `XAI_API_KEY`，从而保证不会因为本机已有登录而走错计费通道。
- 外部模型执行前确认：运行器存在、模型标识可解析、认证已配置、工作目录与文件所有权正确、工具权限和停止条件明确。
- API/订阅可能计费。未获得本轮付费授权时，只验证配置、命令发现、假服务或 dry-run，不发送真实推理请求。
- 首选通道不可用时，默认停在该工作包的外部门禁前并报告缺失项；只有用户或项目规则明确允许降级时，才能改走通用模型，并在路由合同和交付中记录实际模型，禁止静默降级。
- 外部 Agent 的修改视为候选交付。主 Agent仍须核对实际 diff、运行本项目验证，并完成真实入口验收。

## 外部执行可见性

Kimi Code 与 Grok Build 由总控通过外部运行器启动，不会注册为 Codex 原生子 Agent，因此通常不会出现在原生子 Agent 列表中。不得只为获得一张原生 Agent 卡片而再套一层 Luna、Terra 或 Sol；这种包装会显示错误的执行模型、额外消耗原生模型额度，并模糊实际责任边界。

总控必须在当前任务的用户可见进度中用紧凑 Markdown 文本框展示外部执行者，不得只留下终端流或事后口头说明。彩色卡只用于 `execution_route: external-agent`；Codex 原生子 Agent 继续使用系统卡片，不再生成重复文本框或配色。每张外部卡最多三行：

```text
╭─ <Agent 色标> <model> · <状态图标> <状态>
│ <work_package> · <category> · <auth_mode> · <reasoning_effort>
╰─ <候选、验证或简短原因>
```

- Agent 色标按外部运行器固定映射：Kimi Code 使用 `🟣`，Grok Build 使用 `🟦`。同一运行器的不同工作包不随机换色，Web、Mini、API 等任务差异用工作包和类别字段区分。
- 状态图标固定为：`🟢 运行中`、`🟡 已返回`、`✅ 已验收`、`🟠 阻塞`、`🔴 结果未知`。Agent 色标表示“谁在执行”，状态图标表示“执行到哪一步”，两者不得混用。
- `运行中` 只在无调用预检通过、授权边界满足且即将发出真实请求时显示；预检本身不能冒充模型正在工作。
- 每次调用最多一张 `运行中` 和一张终态卡，不做定时刷新。外部进程返回后若主 Agent 能在同一短事件内立即验收，省略 `已返回`，直接显示 `已验收`；只有验收需要等待时才显示 `已返回`。
- 主 Agent 审查实际 diff、确认文件所有权并完成合同验证后，才能显示 `已验收`。
- 预检或授权门未通过时显示 `阻塞`，不显示 `运行中`。请求可能已消耗额度、结果未知或留下部分修改时显示 `结果未知`，停止自动重试与降级。
- 外部路线经授权降级为 Codex 原生子 Agent 时，外部卡只记录原路线的终态；新路线由系统原生卡片展示，不再创建并行的外部彩色卡。
- 可见状态只包含工作包、运行器、模型、认证模式、推理强度、阶段和候选结果；不得显示凭据值、完整环境、原始密钥来源内容或敏感模型输出。
- 这些进度行是当前执行收据，不新增平行台账。项目已有唯一任务台账时，只把最终路由、候选、验证和阻塞结果写回对应工作包。

## 路由合同

每次派发至少包含以下事实；项目台账已有对应字段时只引用，不复制第二份状态源：

```yaml
route_owner: adaptive-delivery
work_package: <稳定 ID>
category: frontend | backend | general
execution_route: external-agent | native-subagent | main-agent
agent_type: <原生角色或外部运行器>
model: <精确模型标识>
model_provider: <需要时填写>
auth_mode: oauth | api  # external-agent 必填
credential_source: cli-session | environment | os-keychain  # 不写密钥值
reasoning_effort: low | medium | high | xhigh | max  # 原生或外部子 Agent 必填
repository_root: <绝对路径>
base_revision: <提交或明确的非 Git 快照>
owned_files: <唯一写入范围>
goal: <可观察结果>
non_goals: <明确排除项>
acceptance: <验收条件>
verification: <执行者应运行的检查>
stop: <完成、阻塞或交回条件>
fallback: blocked | <经授权的替代路由>
```

执行器如果不能精确保留合同中的模型或权限边界，应拒绝派发并把冲突交回主 Agent，不自行猜测。
