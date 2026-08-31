# Adaptive Agent Runtime Productization Design

## Goal

将现有 `adaptive-delivery` 产品升级为 **Adaptive Agent Runtime**，并在不破坏现有安装、controller、runtime、rule handshake、receipt、Keychain 与项目续接的前提下，补齐一键安装/宿主适配、Web 上下文恢复与续作、自动 host fallback、Controller Self-Check，以及两项必要可靠性能力：副作用幂等保护和 Assignment 执行策略冻结。

## Naming and compatibility

- 对外产品名立即使用 **Adaptive Agent Runtime**。
- 旧 `adaptive-delivery` 作为兼容 alias 保留；迁移期不允许形成第二套 canonical runtime/state。
- `.git/adaptive-delivery/`、旧 controller registry、旧 lifecycle/scoring state、旧 install manifest 与 Keychain service 继续可读；迁移只建立兼容解析或迁移入口，不复制运行事实。
- 已存在的 `adaptive-delivery@<revision>` handshake/receipt 继续有效，除非规则内容本身变化触发现有握手机制。
- 安装 manifest 记录 `product_name/product_slug` 与当次 host capability 报告，但技术 Skill ID、安装路径和 canonical state 继续沿用 `adaptive-delivery`。

## Core product architecture

Adaptive Agent Runtime 分为三层：

1. **Core**：五层上下文治理、四 Gate、Assignment/runtime、evidence/review/integration、Controller Self-Check。
2. **Host Adapter**：Desktop/Codex native lifecycle、AI-Bridge Web adapter、未来其他本地桥；宿主不可用时明确降级。
3. **Optional enterprise runtime**：未来 A2A durable store/outbox/event replay/remote worker，不在本次实现。

AI-Bridge 只是 Web 本地能力 Adapter，不是核心前提。没有 AI-Bridge 时，Web 使用纯 Web/文件能力并明确 degraded capability，不虚构本地 repo/runtime 可见性。

## Web session restore and lifecycle

- 为 Web 新会话提供确定性的项目绑定与 SessionStart 等价恢复入口。
- 恢复顺序保持现有五层治理：`AGENTS -> TASK_LEDGER -> MEMORY -> relevant Wiki/authoritative docs -> Git/runtime`。
- 不新增第六层上下文、不新增 Web 专用台账。
- 继续复用现有 `pending_control_event` 和 lifecycle snapshot；不新增 Outbox 或第二通知状态机。
- 修复同一 registered controller 的 resume 通道：若当前 host 恢复因允许的 runtime failure 明确失败，可进入已有授权 peer-host fallback；`active writer` 等状态必须区分“控制器已活跃”与“真正未恢复”，不得创建第二总控。

## Host fallback

- 首选当前 `controller_host`。
- 只有当前 attempt 已明确终止、无 unknown result、无 partial write、无新增 billing/authorization boundary，且 failure class 属于已允许 runtime/availability 类，才可跨到已授权 peer host。
- host 变化不改变 controller identity、task lineage、checkpoint、attempt/recovery budget 或模型档位。
- peer host 不可机器调用时明确 `peer_host_unavailable`，不使用浏览器盲打字冒充可靠执行器。

## Controller Self-Check

- 从正式 `controller-performance-scoring.md` 派生短版 Self-Check；总控知道优秀行为标准，但不读取/计算自身实时总分或维度得分。
- Self-Check 在 SessionStart/控制事件/恢复事件时可注入，用于检查：关键路径、READY 派发、候选审查/集成、异常恢复、自主续作、证据与控制面一致性。
- Self-Check 是行为纠偏，不是新评分系统或新状态机。
- 单次事故不得自动向全局 Skill 加规则；只有重复跨场景证据且独立审查确认治理缺口时才升级全局规则。

## Reliability additions

### Side-effect idempotency guard

对付款、发布、消息发送、资源创建、外部写入等不可安全重复的动作，引入机器可判定的副作用重试策略：

- 明确 `side_effect` 与 `idempotency` 事实。
- 结果 uncertain/unknown 时，无幂等保障禁止自动 retry/fallback 触发重复副作用。
- 稳定 idempotency key 仅作为执行契约传递；只有具体 side-effect adapter 能机器证明 provider/API 在真实写入边界应用了幂等保障时，unknown outcome 才有资格在既有 recovery budget 内重试。当前通用外部 Agent adapter 不具备该证明，因此 unknown side effect 一律先对账、禁止自动重试。
- 该能力进入现有 Delivery/Dispatch 决策，不创建第二任务状态机。

### Assignment execution strategy freeze

Assignment 第一次启动时冻结可验证执行策略摘要，至少覆盖 provider/model/reasoning tier/execution transport/controller host policy 等影响行为的字段。

- 同一 Assignment 的 recovery 不得静默漂移策略。
- peer-host fallback 若属于合同已允许的 host policy，可保持同一 Assignment/lineage；实际 `execution_host` 变化必须记录。
- 需要换 provider/model/tier/核心 transport 或改变执行策略时，必须创建新 Assignment 合同；旧 recovery budget 不被偷偷重置。

## Installer and capability detection

- 默认单入口安装：Core + 探测到的可用 Adapter + hooks/preflight。
- Codex/Desktop 可用时安装/验证 native hooks。
- AI-Bridge 可用时启用 Web adapter。
- 无 AI-Bridge 时不报失败，输出 Web degraded capability。
- 安装结果必须报告 enabled/degraded/blocked 能力，不把“文件存在”当成“宿主已信任/已激活”。
- 旧安装升级必须保持已有机器状态可读；新安装只向用户展示 Adaptive Agent Runtime 新名称。

## Non-goals

本次不实现：完整 A2A Server、SQLite 第二状态机、Transactional Outbox、第二 event cursor、企业 SSO/RBAC、集中控制台、远程 worker server、另一套模型路由 server。

## Acceptance

1. 现有安装升级后旧 `$adaptive-delivery`、旧 state/runtime/receipt/handshake 继续工作。
2. 新安装对外暴露 `Adaptive Agent Runtime` / `adaptive-agent-runtime` 产品身份，但底层 Skill ID 继续为 `adaptive-delivery`，且不会创建两套 canonical runtime。
3. 无 AI-Bridge 时安装成功并明确降级；有 AI-Bridge 时启用 Web adapter。
4. Web 新会话可得到项目绑定/恢复入口，恢复五层上下文而不新增项目状态层。
5. lifecycle 异常保留 pending 事实；当前 host 合法失败时可按既有策略切已授权 peer host，保持唯一 controller 和 lineage。
6. Controller Self-Check 从正式评分模型派生但不暴露实时分数。
7. 非幂等副作用在 unknown outcome 下禁止自动重试。
8. Assignment 同合同 recovery 不能静默漂移 provider/model/关键策略。
9. 全量现有回归与新增 clean-install/existing-install tests 通过。
