# 模型 Provider 支持矩阵

最后更新：2026-08-11

这份矩阵描述 NoneBot Triage Agent 的受控 B1 单次结构化输出与 B4 单步原生工具调用，不代表 Pydantic AI
或厂商 SDK 的全部能力。每一行按 `Provider + API 族 + model/profile` 准入；“OpenAI-compatible”本身不是
支持声明。B1 与 B4 的离线合约分别记账，不能用 B1 资格自动推导 Agent 工具资格。

## 状态含义

- **支持**：静态依赖、完整离线合约、参数核验、获授权线上实测和回归门均通过；
- **实验性**：存在实现或部分证据，但至少缺少一项正式准入门；不得作为默认生产承诺；
- **不支持**：尚无实现、明确不满足 native schema / 零工具边界，或尚未进入计划。

无论状态如何，当前 NoneBot 插件入口都**不会调用模型**。现有模型实现只服务显式 CLI 评测或离线 Provider
合约；任何真实 API 调用仍要求精确模型、调用上限、token 上限、费用预算和单独确认。

`evaluate-b4-real` 已提供 DeepSeek Responses、OpenAI Responses 与 Anthropic Messages 的同模型多 trial
harness。报告显式绑定 Prompt/schema/policy/source revision 与冻结 regression / forward-hidden split；
B1/B4 后验结构拒绝作为 trial 失败计量，只有无法恢复 usage/cost 等边界才中止整场。DeepSeek 首轮因响应后 usage 审计缺口失败关闭；run-2 又在约 32.5 秒后以 `cost_unknown`
失败且没有 partial。run-3 的新审计保留了 10 个 attempt、9 个 response、527 microUSD 已知费用与最后一个
未知响应，但仍无 success report。三次失败都不构成质量或 Provider 线上资格证据。OpenAI 与 Anthropic
尚未执行。OpenCode Go 只用于真实模型
测试，不是当前产品 Provider、网关或资格候选，也没有加入该 harness；一次成功的 test-only 窄 B4 smoke
也不会进入下表。

插件已经公开默认关闭的窄装配配置，但运行资格注册表只接收状态为“支持”的精确 backend/model 组合；
当前没有“支持”行，因此任何真实启用都会在导入 SDK 或读取密钥前失败。测试可以注入 fake 合格组合验证
生命周期，这不改变矩阵状态。

## 当前矩阵

| Provider | API 族 | model / profile | 安装依赖 | 离线合约 | 获授权线上门 | 当前状态 | 主要证据或缺口 |
|---|---|---|---|---|---|---|---|
| OpenAI | Responses | 尚未固定发布模型；profile 必须声明 native JSON Schema 与 function tools | `model-openai`：`pydantic-ai-slim[openai]==2.27.0` 与锁定 OpenAI SDK；基础 wheel 不安装模型依赖 | B1 Direct Request JSON Schema 与 B4 `function_call` 假 HTTP 合约通过 | 未执行新 adapter 资格实测 | 实验性 | `tests/test_model_adapters.py`、`tests/test_agent_provider_adapters.py`；线上门未完成，不能作为默认 Provider |
| DeepSeek | Responses | `deepseek-v4-flash` 滚动别名；`reasoning=none`；`temperature=0`；Provider wire 不承诺 OpenAI strict 字段 | 仓库 `maintainer` group：`pydantic-ai-slim[openai]==2.27.0` 与 `openai==2.53.0`；使用显式 `DeepSeekProvider` 和固定官方 endpoint；不提供插件 extra，适配器不进入 wheel | B1 Direct Request 原生 JSON Schema 与 B4 `function_call` 假 HTTP 合约通过；`store=false`、零 SDK retry、usage / request ID / cost 归一化已覆盖；B4 参数仍由 Pydantic 与领域层本地复核 | 有旧直接 SDK B1 工件；三次正式 B4 Gate 均失败关闭且无完整报告。run-3 partial 证明可恢复已知响应/费用与未知请求边界，但不提供 promotion decision | 实验性 | 仅供维护者评测；`tools/nbtriage_maintainer/deepseek_adapter.py`、`src/nbtriage/pydantic_agent_adapter.py`、三份中止记录与离线 tests；滚动别名和未完成线上门阻止正式支持 |
| Anthropic | Messages | 尚未固定发布模型；profile 必须声明 native JSON Schema 与 tools；离线合约使用 `claude-sonnet-4-5` | `model-anthropic`：`pydantic-ai-slim[anthropic]==2.27.0` 与 `anthropic==0.121.0`；基础 wheel 与两个 Responses extra 均不安装 Anthropic SDK | B1 native JSON Schema 与 B4 `tool_use` 假 HTTP 合约通过 | 未执行资格实测 | 实验性 | `tests/test_model_adapters.py`、`tests/test_agent_provider_adapters.py`；线上门未完成，离线模型名不构成发布承诺 |
| Google | GenAI | 未选择 | 未定义 | 未执行 | 未执行 | 不支持 | 候选后续 API 族，尚无 adapter |
| 任意第三方 | OpenAI-compatible Chat / Responses | 任意 URL / 模型 | 不提供 | 未执行 | 未执行 | 不支持 | 必须逐 Provider、API 族和 model profile 新增行，禁止由兼容标签继承支持 |

## 不进入支持矩阵的测试 transport

OpenCode Go 不属于上表的产品支持或资格候选。仓库只保留一条 B4-only 测试 transport，用于在获授权时探索
真实模型的 tool calling：固定 Go Chat endpoint、`OPENCODE_API_KEY`、`deepseek-v4-flash`、非思考模式、
`parallel_tool_calls=false`、一次请求和零 SDK retry，并用独立审计身份归一化返回 model、request ID、
fingerprint、usage 与 cache hit/miss 等价费用。它不进入公开 extra、CLI/backend、NoneBot 插件配置、资格
注册表或支持晋级。

假 HTTP 只证明测试 adapter 的 Chat wire、身份、费用与失败关闭行为。2026-08-09 的一次获授权纯合成
native JSON Schema 测试返回 HTTP 400，且没有输出、usage 或可归一费用；该结果不证明 B4 tool calling、
质量、隐私或产品资格。此前据此提出的 ADR-0013 已因范围澄清记为未采纳，不改变 ADR-0008 的 B1 契约。

随后一次获授权 B4 smoke 只调用客户端一次，使用纯合成输入、仅暴露 `request_evidence`、3000 / 256 token、
配置 30 秒 timeout、零重试且零工具执行。本地没有观察到响应，外层执行器约 388.7 秒后强制终止；Provider
是否受理、usage 和费用均未知，也没有在该授权下补发。因此它既不是 Go tool calling 成功证据，也不是
不支持证据。

维护者随后以“继续”给出第二次独立精确授权。相同 test-only Go / `deepseek-v4-flash`、纯合成输入、仅
`request_evidence`、3000 / 256 token、30 秒 hard deadline、零重试、零工具执行和最多一次 client
invocation 下，第二次在 3465 ms 成功返回 `request_evidence(logs)`；decision summary 为 100 字符，账本
记录 1 个请求、660 input / 78 output token 与 115 microUSD Go 配额等价费用，返回身份为 `opencode-go` /
`deepseek-v4-flash`，request ID 存在、fingerprint 为 `null`。这不声称现金支出，也不能反推第一次请求是否
受理。完整机器记录属于维护者本地报告；本文只保留人工复核后的聚合事实。

后续真实模型诊断确认，并列四个 action tool 会诱发多调用；当前测试 adapter 只发送一个
`propose_action` deferred 信封，按 capability、轨迹和已取得 citation 动态收窄联合 schema，并把最终诊断
枚举与版本格式前移到 typed action。一个 4000-token control 已用两次请求完成
`read_runtime_evidence → finish_diagnosis`，但只有单个成功 loop sample，仍不证明多 trial 质量、正式支持、
网关或插件资格。完整机器记录只在维护者本地保留。
维护者允许继续合理使用 Go 做 test-only 探索；未来产品网关或 Provider 方向仍另行调研。

## 所有支持行必须满足的共同调用不变量

1. 使用 Provider 原生 JSON Schema；profile 未明确支持时在请求前失败；
2. 项目与 SDK 自动重试均为零；验证失败、拒答、截断或不支持参数时不 fallback；
3. Provider、API 族和精确模型与缓存键、报告和客户端身份一致；返回 Provider / 模型身份必须完整、唯一并
   与请求匹配，滚动别名不能只按请求名推断实际身份；
4. 数据存储、遥测、base URL、密钥来源、timeout、token 与调用预算显式可核对；
5. 测试全局禁止意外真实模型请求；线上资格测试只使用获批固定组合和合成输入。
6. Agent step 的应用层 hard deadline 使用 client timeout 与领域剩余 deadline 的较小值；剩余 deadline 为 0
   时必须在网络调用前停止且不消费 call slot。SDK/ModelSettings timeout 不能单独作为墙钟有界性证据。
7. 真实多 trial Gate 在请求前/响应后原子更新独立 partial audit，并由 whole-run timeout 包住完整运行；
   success report 与保留的 `.partial.json` 路径不得覆盖。未知响应只能记录稳定原因，不能猜测 token 或费用。

B1 当前正式准入契约额外要求 `function_tools`、`native_tools`、`output_tools` 全部为空，输出先经项目
Pydantic schema，再经 B1 枚举和引用边界验证；非法输出不写缓存。测试 transport 的能力缺口不会改变或
降低这项产品准入契约，不能用 JSON object 或提示词 JSON 静默降级。

B4 额外要求每步只暴露一个领域 runtime 动态构造的 `propose_action` 信封工具并立即 deferred；信封中的
action 联合只能包含当前 capability / 轨迹允许的动作，citation 只能来自已观察证据。只接受唯一 tool call，
再由 Pydantic 参数解析、项目 action schema、白名单和剩余预算二次校验。支持 Provider strict tool definition 时
必须显式启用；DeepSeek Responses 当前 wire 为 `strict=false`，因此只能把 Pydantic 与领域层复核称为本地
验证，不能声称供应商 strict。Pydantic AI 不拥有会话循环、工具执行或持久化 message history；当前
不使用 MCP、handoff、内置工具或任意外部副作用。真实 Gate 还要求 B1 Direct Request 与 B4 step 的费用都
能按 Provider/model usage 归一化；响应已产生但被本地 action 校验拒绝时仍记 usage、费用与身份，已保留
请求却无法取得 usage、身份不符、未知价格或超过声明预算时失败关闭。

首次 DeepSeek 真实 Gate 已验证一个额外失败语义：Pydantic AI 在 Provider 已返回后仍可能于 tool / usage limit
或本地后验阶段抛出 `AgentRunError`。单步 adapter 现在用框架公开的 `capture_run_messages()` 提取最后一个
`ModelResponse`，把已发生请求的 usage 与返回身份交给领域账本；没有响应的传输错误仍不得猜测费用。
OpenCode Go 的未知响应 smoke 又验证了另一条边界：生产 `PydanticAIAgentStepClient` 现在用
`asyncio.timeout()` 包住 `Agent.run()`，并以较小 deadline 作为本地硬上限。该修正的离线定向验证通过，
第二次独立 smoke 也在 30 秒 hard deadline 内返回；但这仍不能反向推导第一次已终止请求的 Provider 状态、
usage 或费用。

第二次独立正式 DeepSeek Gate run-2 精确授权 4 Fixture × 3 trial、最多 60 请求、每 trial 4000 / 1000
token、30 秒 deadline / Provider timeout、900 秒 whole-run watchdog 与 0.03 USD；legacy runner 约 32.5 秒
后以 `cost_unknown` 失败，没有 success report、partial audit、retry 或 rerun。请求数、Provider acceptance、
token、费用与失败阶段都不能恢复，时间只与 30 秒 deadline 一致而不证明因果。随后本地增加的
`b4-real-partial` schema 会在请求前保留 unknown attempt，响应后记录已计费或稳定 unknown reason，并以
`aborted`、`report_ready`、`completed` 区分收口状态；它不补全本次历史，也不提升 DeepSeek 支持状态。

DeepSeek 的 `deepseek-v4-flash` 不是固定 snapshot。后续每份真实报告必须记录运行时间、响应返回的实际模型
身份、request ID 和供应商指纹（若提供）；不同日期的运行不得仅凭相同别名认定为同一模型复现。专用
仓库维护者 DeepSeek 栈与固定官方 endpoint 也不构成插件 extra 或任意 OpenAI-compatible URL 的支持入口。

## 相关决定与证据

- [ADR-0008：采用 Pydantic AI 的受控模型适配层](../adr/0008-pydantic-ai-controlled-model-adaptation.md)
- [ADR-0009：模型调用核心采用异步协议](../adr/0009-use-async-model-boundary.md)
- [ADR-0011：公开默认关闭且按资格门装配的模型配置](../adr/0011-expose-disabled-qualified-model-configuration.md)
- [ADR-0012：让 Pydantic AI Deferred Tools 位于领域 Agent runtime 之后](../adr/0012-use-pydantic-ai-deferred-tools-behind-domain-runtime.md)
- [有界 Agent 单步与恢复流程](flows/bounded-agent-step.md)
- [OpenCode Go](https://opencode.ai/docs/go/)
- [DeepSeek Responses API](https://api-docs.deepseek.com/api/create-response)
- [DeepSeek Models & Pricing](https://api-docs.deepseek.com/quick_start/pricing)
