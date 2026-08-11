# ADR-0012：让 Pydantic AI Deferred Tools 位于领域 Agent runtime 之后

## 状态

已采纳

## 日期

2026-08-09

## 当时遇到了什么

ADR-0010 已决定用单 Agent、typed tools 和有界循环验证动态取证，但仍需决定由谁拥有循环、工具执行、
持久状态和暂停恢复。完全自研 OpenAI Responses 与 Anthropic Messages 的 tool calling wire 会重复处理厂商
协议；直接把整个会话交给通用 Agent runtime，又会让框架的重试、消息历史和工具执行语义进入产品安全
边界。

本次针对项目锁定的 `pydantic-ai-slim==2.27.0` 核对了 Agent typed tools、`Agent.iter()`、usage limit、
tool retry、deferred tool 与 message history。关键事实是：Pydantic AI 可以把工具参数解析为严格 schema，
`CallDeferred` 可以在工具实现执行前暂停；但 `UsageLimits.tool_calls_limit` 只统计成功执行的工具，工具和
输出校验默认各有一次 retry budget，message history 也属于框架协议状态。它们不能直接替代项目自己的
授权与会话不变量。

## 最后决定

采用混合边界：

1. `nbtriage` 领域 runtime 拥有循环、状态、预算、策略复核、工具执行、暂停恢复、trajectory 和停止原因；
2. 每个模型步骤临时创建一个 Pydantic AI `Agent`，只负责把项目允许的 action 映射为 Provider 原生 tool
   schema、校验参数并解析协议响应；
3. 所有工具函数立即抛出 `CallDeferred`。框架不执行项目工具；返回的唯一 deferred call 必须再次通过
   项目 action schema、动态白名单和会话预算后，才能由领域 runtime 读取白名单 observation；
4. 每步固定 `retries=0`、`UsageLimits(request_limit=1, tool_calls_limit=1, ...)`，Provider SDK
   `max_retries=0`；文本回答、多个工具调用、审批请求、非法参数和未知 action 均失败关闭；
5. 每一步只允许一次 Provider 请求，但一次会话可以在项目的 turn/tool/token/deadline/cost 上限内执行
   多步。框架计数只是内层防线，领域账本才是跨步权威；
6. 不持久化 Pydantic AI message history，也不保存私有 Chain-of-Thought。下一步 Prompt 从
   `AgentRunState` 中的结构化 action、规范化 observation、短摘要、引用和 usage 重新构造；
7. `request_evidence` 产生项目 interruption。恢复必须提交与 run、Case、slot 精确绑定的脱敏
   `EvidenceReceipt`，并替换 pending observation；不得重放已经记录的 action；
8. 首批 action 只有白名单运行证据读取、train-only 本地支持证据检索、结构化补证请求和严格最终诊断。
   不暴露 Shell、任意文件/HTTP、MCP、配置修改、重启、代码执行或外部写入。

本 ADR 不改变 ADR-0008 对 B1 Direct Request 的零工具决定。它只对 B4 新增一条独立的 Agent step 路径，
因此在 B4 范围内取代 ADR-0008 “不采用 Agent/toolset”的排除项；领域反腐层、逐 Provider 准入、零重试、
显式预算和失败关闭等原决定继续有效。

## 为什么这样选

- 相比完全自研厂商 wire，Pydantic AI 复用 OpenAI Responses 与 Anthropic Messages 的原生 tool schema、
  参数校验和协议解析，减少跨 Provider 重复代码；
- 相比直接运行完整框架 loop，项目可以独立证明一次请求、零重试、动态 action 白名单、跨步预算、
  interruption 绑定和无框架历史持久化；
- deferred tool 把“模型提出调用”和“应用批准并执行”分成可测试的两个阶段，正好对应本项目的不可信证据
  与权限边界；
- 领域状态不依赖框架消息类型，Provider 或 Agent 库升级不会直接改变持久化格式、恢复语义和产品审计口径。

## 没有采用的方案

- **自研所有 Provider tool calling wire**：安全边界最窄，但会重复处理不同响应块、工具 schema、usage 与
  request ID，维护成本不能带来额外产品能力；
- **直接用 `Agent.run()` 执行整个会话**：开发量较小，但框架将拥有循环和工具调度，且默认 retry、成功
  工具计数和 message history 语义不等于项目的预算与恢复契约；
- **使用 Pydantic AI deferred result 作为持久状态**：适合通用 HITL，但仍会把框架消息历史带入领域存储；
  本项目只借用单步 deferred 协议，持久化仍使用领域 schema；
- **继续使用 Direct Request 并拒绝 typed tools**：适合 B1 one-shot，却不能验证模型基于 observation 动态
  选择行动这一 B4 目标。

## 带来的影响

- `bounded_agent.py` 成为 Provider 无关的 Agent control plane；`pydantic_agent_adapter.py` 只是可替换的
  单步端口；
- Provider 合约要同时覆盖 B1 无工具 JSON Schema 与 B4 原生工具调用，两者不能混为同一支持等级；
- 框架升级时必须复核 deferred、retry、usage 与多工具响应语义；
- 脚本模型 Gate 只能证明控制流和安全边界。真实模型质量、方差、延迟和费用仍需另获明确授权后，用同一
  Provider/model/预算做多 trial 才能决定是否接入插件精确报障入口。

## 落实与确认

- 2026-08-09：完成领域 runner、Pydantic AI deferred step adapter、DeepSeek / OpenAI Responses 与
  Anthropic Messages 假 HTTP 合约、脚本多 trial Gate、同模型真实 Gate harness 和暂停恢复测试。DeepSeek
  专用 factory 在 Provider wire 为 `strict=false` 时仍保留 Pydantic 参数解析和领域 schema / 动态白名单
  二次校验，并固定非思考模式、一次请求与零 SDK retry。真实 Gate 的审计账本保留每个返回响应的
  Provider / model / request identity、usage 与费用；即使 action 随后被本地拒绝也不会漏计，身份缺失、
  漂移或已保留请求无法取得 usage 时失败关闭；
- 2026-08-09：维护者确认“记录决策并继续实现，只有授权门等真实阻塞项再确认”；
- 2026-08-09：维护者精确授权 DeepSeek 首轮真实 Gate。运行因 Provider 已返回后
  `Agent.run()` 抛错而丢失 usage，费用账本按设计失败关闭且没有生成评测报告。adapter 随后使用 Pydantic AI
  2.27.0 的公开 `capture_run_messages()` 提取最后一个 `ModelResponse`，让
  `AgentStepResponseError` 携带已发生请求的 usage、Provider/model/request identity 与可选 fingerprint；
  B1 与 B4 各一次窄诊断已验证前者正常计价、后者在后验拒绝时仍可计价。首次运行的准确请求数不可恢复，
  因此当时的 DeepSeek 完整重跑等待新的增量授权；
- 2026-08-09：为测试本机已有的 OpenCode Go API 增加 evaluation-only B4 factory，固定 Go Chat endpoint、
  独立审计身份、非思考模式、禁并行 tool calls 和零 SDK retry；假 HTTP 已覆盖 deferred tool wire、返回
  身份 / fingerprint、cache hit/miss 费用与单请求失败。它不代表 Provider 迁移，不进入公开 extra、CLI、
  NoneBot 配置或资格表，也不改变 B1；网关与发布支持留待以后出现实际需求时再讨论；
- 2026-08-09：一次另行授权的 OpenCode Go B4 tool smoke 只做 1 次纯合成 direct client invocation，未执行
  工具也没有 retry；本地没有观察到响应，外层执行器约 388.7 秒后终止，Provider 是否受理、usage 与费用
  均未知。常规领域 runner 原已有 remaining-deadline 外层守门，这次直调 client 暴露的是 adapter 级缺口。
  `PydanticAIAgentStepClient` 随后以 `asyncio.timeout()` 强制 client timeout 与领域剩余 deadline 的较小值，
  保留 `TimeoutError` 供 runner 映射 `DEADLINE`；零剩余 deadline 在网络调用前停止且不消费 call slot。
  timeout、零 deadline、已有响应审计与正常路径的离线定向测试通过；这只是既有有界性决定的落实证据，
  不产生产品、网关或 Provider 决策；
- 2026-08-09：hard-deadline 修正后的第二次独立 OpenCode Go test-only smoke 在 3465 ms 返回唯一
  `request_evidence` action，slot 为 `logs`；共 1 次 Provider 请求、660 / 78 input / output tokens，按测试
  价目归一化为 115 microUSD 等价值。Provider 身份与返回模型分别匹配测试 backend 和请求模型，request ID
  存在且未返回可选 fingerprint，自动 / 手工 retry 与项目工具执行均为 0。这只落实了窄 B4 tool wire 的
  一次线上样本，不覆盖第一次 388.7 秒结果未知的历史，也不构成产品 Provider、模型网关、资格晋级或
  多 trial 质量证据；完整机器记录只在维护者本地保留；
- 2026-08-09：维护者为第二次独立正式 DeepSeek Gate run-2 精确授权 4 Fixture × 3 trial、最多 60 请求、
  每 trial 4000 / 1000 token、30 秒 deadline / Provider timeout、900 秒 whole-run watchdog 与 0.03 USD。
  legacy runner 约 32.5 秒后以 `cost_unknown` 失败，没有 success report、partial audit、retry 或 rerun；实际
  请求数、Provider acceptance、token、费用和失败阶段均不可恢复，时间只与 30 秒 deadline 一致而不证明
  因果。完整机器记录只在维护者本地保留；
- 2026-08-09：真实 Gate 随后增加独立 `b4-real-partial` schema。B1/B4 wrapper 在请求前原子保留 attempt，
  响应后记录 identity/usage/cost 或稳定 unknown reason；CLI 增加 whole-run timeout、保留 `.partial.json`、
  success no-overwrite publish 与 `report_ready/completed` 收口。离线定向覆盖 checkpoint 写失败不出站、响应
  后本地拒绝仍记账、deadline/cancel/provider/local unknown、whole-run 取消及报告发布失败；这些只是既有
  失败关闭与审计决定的落实，不构成真实模型质量、产品 Provider 或插件资格结论；
- 2026-08-10：第三次独立 DeepSeek Gate 在第 10 个 attempt 中止；partial 保留 9 个 response、527 microUSD
  已知费用与最后一个未知响应，在线确认请求前/响应后 checkpoint 能保全失败边界，但仍无完整报告；
- 2026-08-10：在不改变本 ADR 单步 deferred 决定的前提下，partial schema v3 使用 Pydantic AI 2.27
  `ModelHTTPError.status_code` 与 `ModelAPIError` 将未知 Provider 请求分为 request rejected、provider timeout、
  rate limited、server error 与 transport error；只保存稳定类别和可选 HTTP status，不保存 body、headers 或
  异常文本，普通本地领域错误保持 `local_error`。run-3 的 schema v1 不做追溯改写；
- 2026-08-10：真实 OpenCode Go test-only 诊断发现，把四种 action 渲染为四个并列工具会诱发同一响应多
  调用。B4 单步现只渲染一个 `propose_action` deferred 信封；领域 runtime 按 capability 和已观察轨迹收缩
  联合，adapter 再按已取得 evidence 收窄 citation，最终版本/枚举也进入 typed schema。Pydantic validator
  在 deferred 前校验，领域 parser 在返回后复核；文本、多调用、越权和非法参数仍失败关闭。一个 4000-token
  control 已用两次请求完成 runtime observation → finish。该实现是本 ADR“唯一 deferred call + 领域拥有
  授权”的落实，不更改 Accepted 决定，也不把测试 transport 晋级为产品 Provider；
- NoneBot 插件的零模型入口和空资格注册表保持不变，两次 test-only smoke 都没有把任何组合晋级为支持。

## 相关文档与证据

- [ADR-0010：用有界证据获取循环验证 Agent 能力](0010-use-bounded-evidence-seeking-agent-loop.md)
- [有界 Agent 单步与恢复流程](../architecture/flows/bounded-agent-step.md)
- [模型 Provider 支持矩阵](../architecture/model-provider-support.md)
- [Pydantic AI：Deferred Tools](https://ai.pydantic.dev/deferred-tools/)
- [Pydantic AI：Usage Limits](https://ai.pydantic.dev/agent/#usage-limits)
- [Pydantic AI：Tool Retries](https://ai.pydantic.dev/tools-advanced/#tool-retries)
- [Pydantic AI：Message History](https://ai.pydantic.dev/message-history/)
