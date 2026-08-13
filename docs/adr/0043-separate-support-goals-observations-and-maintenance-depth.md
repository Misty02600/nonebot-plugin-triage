# ADR-0043：分离支持目标、现象陈述与维护证据深度

| 状态 | 决策日期 |
|---|---|
| 已替代；taxonomy 由 ADR-0046 接续 | 2026-08-13 |

## 当时遇到了什么

`SupportSemanticAssessment v2` 把 `reported_observation`、`asks_why`、`asks_guidance` 与
`requests_maintenance_detail` 放在同一个 `needs` 集合中，又让模型输出
`ambiguous_reference / insufficient_context / unsupported_request` 等原因。实际讨论和 OpenCode Go
合成评测暴露了三个问题：

- “用户刚才观察到失败”是事实范围，不是用户希望系统采取的动作；
- “要看哪个配置、版本或 handler”是证据深度，不等于一定还要重复标记“解释原因”；
- 未决原因的细分不会改变当前分流，模型也不能稳定区分“指代含糊”和“上下文不足”。

同时，仅有现象陈述和可信运行失败仍不足以证明用户希望创建 incident。建单既需要模型识别到明确的故障受理
目标，也需要模型外可信证据和确定性授权。

## 决策

1. `SupportSemanticAssessment v4` 使用三组正交字段：
   - `goals`：`guidance`（使用指导）、`behavior_explanation`（行为解释）、
     `incident_intake`（故障受理）、`feature_feedback`（功能建议），允许多选；
   - `reported_observation`：用户是否声称当前或过去实际发生过 Bot 行为；
   - `maintenance_detail_requested`：是否索取源码、Matcher / Rule / handler、内部配置、环境、依赖、adapter
     或版本等维护者证据。
2. `status` 只保留 `assessed / needs_clarification / unsupported`。模型不再输出 reason、置信分数、action、
   lifecycle、authorization 或回答文本；本地 policy / transport / schema 失败继续由独立 execution status 表达。
3. `maintenance_detail_requested=true` 可以独立成立，不要求同时产生 `behavior_explanation`。是否允许披露仍由
   模型外身份、ServingView 与字段安全策略决定。
4. incident 只有在 `goals` 包含 `incident_intake`、`reported_observation=true`，并且模型外可信运行证据明确
   失败时才可由确定性 router 签发一次性授权。任一条件缺失都不建单。
5. 结构化输出继续由 Pydantic AI `ModelProfile` 在请求前选择。OpenCode Go 仍固定 Tool Output；项目任务资格
   更新为 `support-semantic-v4`、schema v4、`support-semantic-v4-prompt-v1` 与新的 held-out revision。
6. development 24 条只用于调试；全新、未写入 Prompt 且在冻结后首次发送的 36 条纯合成 held-out 必须达到
   schema 合法率 100%、status 准确率 100%、全部字段精确匹配至少 90%。本次结果为 36/36，48,137 input /
   4,310 output token，归一费用 1,641 microUSD，response ID 36/36，fingerprint 0/36。

## 原因与影响

- 分类依据变成“用户要什么结果 × 是否陈述真实发生 × 要多深的证据”，不再用题材词决定生命周期；
- 功能建议有独立候选分支，不与“报告者对诊断结果的反馈”或 incident 混名；
- 模型输出字段更少，维护细节不再与行为解释重复，未决状态也不再保存无消费者的原因枚举；
- 当前 `guidance` 可以进入公开能力说明；behavior 与 feature feedback 已有各自候选动作和明确零副作用回复，
  但完整证据探索、反馈持久化与开发者审理尚未实现；
- 36/36 只证明当前合成边界可行，不等于真实用户分布已经验证。真实试运行仍需记录混淆和澄清率，不能
  用生产文本回填本次 held-out 后继续声称它未见过。

## 没有采用的方案

- **保留 flat needs**：会继续把目标、事实范围和证据深度混成同一维度；
- **保留细分 reason**：当前分流不消费这些差异，held-out 已证明它们容易发生无产品影响的摇摆；
- **增加 confidence 数字**：不同 Provider 的分数不可直接校准，确定性安全门也不应依赖模型自报概率；
- **观察到失败且证据失败就自动建单**：这会忽略用户是否明确请求进入故障生命周期。

## 替代关系

- 替代 [ADR-0041](0041-qualify-opencode-go-tool-output-for-support-semantics.md) 中
  `support-semantic-v2` 的 schema、Prompt 与资格评测 revision；Provider、API、model、Profile、隐私、预算、
  Tool Output、一次请求和零重试边界继续有效。
- 进一步收紧 [ADR-0040](0040-require-trusted-preflight-failure-before-incident.md)：可信失败之外，还必须有
  明确 `incident_intake` 目标。
- 继续遵守 [ADR-0042](0042-use-pydantic-ai-model-profile-for-structured-output.md) 的传输能力所有权。

## 落实与确认

- v4 闭合 schema、Prompt、Pydantic AI Agent `output_type`、OpenCode Go task 资格、评测器和 runtime 已更新；
- router 已分别消费 goals、observation 和 maintenance depth，并保持 action / authorization 在模型外；
- Matcher 对 behavior、feature feedback 与 out-of-scope 使用不同的零副作用结果，不再都落到“功能还是报障”的
  通用澄清；
- 单元、集成和全仓测试通过；开发集 24/24、全新 held-out 36/36，完整机器报告仅保存在忽略的 `reports/`。

## 相关文档

- [支持入口分流](../architecture/flows/support-intake-routing.md)
- [模型 Provider 支持矩阵](../architecture/model-provider-support.md)
- [架构总览](../architecture/overview.md)
