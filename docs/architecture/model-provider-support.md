# 模型 Provider 支持矩阵

最后更新：2026-08-14

这份矩阵描述 NoneBot Triage Agent 的受控 B1 单次结构化输出与 B4 单步原生工具调用，不代表 Pydantic AI
或厂商 SDK 的全部能力。Pydantic AI `ModelProfile` 负责模型传输能力和默认结构化输出方式；每一行只按
`Provider + API 族 + 精确 model + task/schema/Prompt + 隐私策略 + 预算 + 评测 revision` 准入。“OpenAI-compatible”本身不是
支持声明。B1、B4、支持入口语义 assessment 和公开能力 Answer Agent 的合约分别记账，不能用其中一项资格
自动推导另一项。

## 状态含义

- **支持**：静态依赖、完整离线合约、参数核验、获授权线上实测和回归门均通过；
- **实验性**：存在实现或部分证据，但至少缺少一项正式准入门；不得作为默认生产承诺；
- **不支持**：尚无实现、明确不满足 native schema / 零工具边界，或尚未进入计划。

已采纳的产品契约要求每轮非空 `triage` 请求默认经过受限语义 assessment，不设产品级模型启用开关。
传输无关的 v5 请求投影与输出 schema、一次性失败关闭 service、固定 Prompt 的结构化 Pydantic AI Agent client
和确定性 router 已经实现；模型只产出 signals，不产出 action 或 authorization。插件 runtime 必须持有
assessment service，首轮与续问每轮调用一次；通用 client 以 `output_mode=auto` 消费 ModelProfile，不维护
第二份传输能力结构，也不会在失败后切换输出方式。router 签发的进程内授权绑定精确 `LiveReportRequest`，建单
服务会在副作用前原子验证并一次性消费，不能重放或换请求。OpenCode Go semantic factory 与任务资格门
已经实现；未配置 transport 时 unavailable service 仍 abstain 并保守澄清。这不是可选的
词表产品模式。真实 API 资格绑定精确模型、调用上限、token 上限和费用预算。

维护者已经单独批准语义 assessment 的数据类别：只允许发送当前单条、经规范化和模型前秘密守门的
`triage` 请求文字。Reply / Thread 历史、身份与 scope、配置、环境变量、日志、源码、运行证据、能力索引和
`restricted` 证据均不得进入请求。该数据批准本身不是任一 Provider/model 的“支持”证据。OpenCode Go 的
精确 Provider/API/model/task、数美元资格预算和真实合成调用已由 ADR-0041 另行授权并通过 held-out Gate；
其他组合仍没有可发起请求的资格。

公开能力 Answer Agent 是另一项任务：router 选中 guidance 后，它只接收当前单条问题与已经在模型外过滤为
public 的能力事实，返回带事实 ID 引用的自然语言回答。当前 OpenCode Go 实现已通过闭合 schema、秘密守门、
单次 required output tool、零 retry、Provider 身份和 Handler 回退的离线合约，但还没有独立真实模型 held-out
回答质量 Gate。因此它只作为当前 Bot 的受控 dogfood 能力，不继承 semantic assessment 的“支持”资格。

`evaluate-b4-real` 已提供 DeepSeek Responses、OpenAI Responses 与 Anthropic Messages 的同模型多 trial
harness。报告显式绑定 Prompt/schema/policy/source revision 与冻结 regression / forward-hidden split；
B1/B4 后验结构拒绝作为 trial 失败计量，只有无法恢复 usage/cost 等边界才中止整场。DeepSeek 首轮因响应后 usage 审计缺口失败关闭；run-2 又在约 32.5 秒后以 `cost_unknown`
失败且没有 partial。run-3 的新审计保留了 10 个 attempt、9 个 response、527 microUSD 已知费用与最后一个
未知响应，但仍无 success report。三次失败都不构成质量或 Provider 线上资格证据。OpenAI 与 Anthropic
尚未执行。OpenCode Go 的 B4 历史 smoke 不构成产品资格；只有独立 `support-semantic-v5` held-out Gate
支持下表的 semantic-assessment 行。

插件保留窄 transport 身份与预算配置，但已删除产品级 `enabled` 字段。未配置 backend/model 表示没有
transport，runtime 因此装配 unavailable service；这不是用户可切换的“关闭 semantic assessment”模式。
现有 `QUALIFIED_PLUGIN_MODELS` 仍只是 B1 的精确 `(backend, model)` 注册表且当前为空。semantic assessment
使用独立的 `QUALIFIED_SEMANTIC_TASKS`，绑定 Provider、API 族、精确 model、task/schema/Prompt、隐私策略、
预算与评测 revision，并已接入 semantic service factory。Tool/Native 支持及默认选择只在 Pydantic AI
ModelProfile 中表达。测试注入 fake service只验证调用编排，不改变资格表。

## 当前矩阵

| Provider | API 族 | model / profile | 安装依赖 | 离线合约 | 获授权线上门 | 当前状态 | 主要证据或缺口 |
|---|---|---|---|---|---|---|---|
| OpenAI | Responses | 尚未固定发布模型；profile 必须声明 native JSON Schema 与 function tools | `openai`：`pydantic-ai-slim[openai]==2.27.0`；底层 SDK 由 Pydantic AI extra 声明，基础 wheel 不安装模型依赖 | B1 Direct Request JSON Schema 与 B4 `function_call` 假 HTTP 合约通过 | 未执行新 adapter 资格实测 | 实验性 | `tests/test_model_adapters.py`、`tests/test_agent_provider_adapters.py`；线上门未完成，不能作为默认 Provider |
| DeepSeek | Responses | `deepseek-v4-flash` 滚动别名；`reasoning=none`；`temperature=0`；Provider wire 不承诺 OpenAI strict 字段 | 仓库 `maintainer` group：`pydantic-ai-slim[openai]==2.27.0`；底层 OpenAI SDK 由该 Provider extra 声明；使用显式 `DeepSeekProvider` 和固定官方 endpoint；不提供插件 extra，适配器不进入 wheel | B1 Direct Request 原生 JSON Schema 与 B4 `function_call` 假 HTTP 合约通过；`store=false`、零 SDK retry、usage / request ID / cost 归一化已覆盖；B4 参数仍由 Pydantic 与领域层本地复核 | 有旧直接 SDK B1 工件；三次正式 B4 Gate 均失败关闭且无完整报告。run-3 partial 证明可恢复已知响应/费用与未知请求边界，但不提供 promotion decision | 实验性 | 仅供维护者评测；`tools/nbtriage_maintainer/deepseek_adapter.py`、`src/nbtriage/pydantic_agent_adapter.py`、三份中止记录与离线 tests；滚动别名和未完成线上门阻止正式支持 |
| Anthropic | Messages | 尚未固定发布模型；profile 必须声明 native JSON Schema 与 tools；离线合约使用 `claude-sonnet-4-5` | `anthropic`：`pydantic-ai-slim[anthropic]==2.27.0`；底层 SDK 由 Pydantic AI extra 声明，基础 wheel 与 `openai` extra 均不安装 Anthropic SDK | B1 native JSON Schema 与 B4 `tool_use` 假 HTTP 合约通过 | 未执行资格实测 | 实验性 | `tests/test_model_adapters.py`、`tests/test_agent_provider_adapters.py`；线上门未完成，离线模型名不构成发布承诺 |
| Google | GenAI | 未选择 | 未定义 | 未执行 | 未执行 | 不支持 | 候选后续 API 族，尚无 adapter |
| 任意第三方 | OpenAI-compatible Chat / Responses | 任意 URL / 模型 | 不提供 | 未执行 | 未执行 | 不支持 | 必须逐 Provider、API 族和 model profile 新增行，禁止由兼容标签继承支持 |
| OpenCode Go | Chat Completions | `deepseek-v4-flash`；non-thinking；required 单一 Pydantic AI Agent output tool；60 秒 / 240 token；Prompt v5 | 复用 `openai` extra：`pydantic-ai-slim[openai]==2.27.0`；不声明内容重复的 OpenCode Go extra | 假 HTTP 覆盖最小 payload、Agent `output_type` 生成的唯一 tool、零 retry、身份/usage/费用与本地双层校验 | taxonomy v5 冻结后首次运行全新 40 条、未写入 Prompt 的纯合成 held-out：schema / status 1.000、全字段精确匹配 0.975；40 请求；50,197 / 3,774 token；1,782 microUSD | 支持 | 仅限 `support-semantic-v5-prompt-v1` 与 `opencode-go-heldout-40-20260813-v5-taxonomy`；滚动模型别名或任一 profile 变化必须重跑 Gate；详见 ADR-0046 |
| OpenCode Go | Chat Completions | `deepseek-v4-flash`；non-thinking；required 单一 `PublicGuidanceAnswer` output tool；60 秒 / 240 token；Prompt `public-guidance-answer-v1-prompt-v1` | 复用 `openai` extra | 闭合问题 / public facts 输入、唯一 output tool、秘密零请求、引用 ID 校验、零 retry、Provider 身份与 Handler 确定性回退均通过；无工具 | 1 条纯公开“搜图用法”真实 smoke 成功；尚未执行独立真实模型 held-out 回答质量 Gate | 实验性 | 仅用于当前 Bot 受控 dogfood；任务记录为 `opencode-go-public-guidance-smoke-1-20260814-v1`，不能继承上一行 semantic 资格；详见 ADR-0048 |
| OpenCode Go | Chat Completions | `deepseek-v4-flash`；non-thinking；Pydantic AI Agent 结构化 claims / constraints；60 秒 / 240 token；Prompt `capability-teaching-annotation-v1-prompt-v1` | 复用 `openai` extra | 运行时记录先行、有界已加载源码与 deny-list 后配置投影、Evidence 引用闭包、零业务工具、零 retry、公开文本去实现细节与 LocalStore 无源码 cache 已通过本地合同测试 | 尚未执行真实 Provider held-out；不继承 semantic 或 Answer Agent 资格 | 实验性 | 仅在部署者显式选择 `NBTRIAGE_CAPABILITY_ANNOTATION_MODE=auto` 时用于受控 dogfood；加载失败、未观察到或 restricted 能力不会进入任务 |

## 既有 B4 测试 transport 与本次资格的关系

OpenCode Go 现在仅以 ADR-0046 的 `support-semantic-v5` 精确组合进入上表。仓库另保留一条 B4-only 测试
transport，用于在获授权时探索
真实模型的 tool calling：固定 Go Chat endpoint、`OPENCODE_API_KEY`、`deepseek-v4-flash`、非思考模式、
`parallel_tool_calls=false`、一次请求和零 SDK retry，并用独立审计身份归一化返回 model、request ID、
fingerprint、usage 与 cache hit/miss 等价费用。该 B4 夹具不因 semantic 资格而晋级。

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

1. B1 使用 Provider 原生 JSON Schema；semantic assessment 直接用
   `Agent(output_type=SupportSemanticAssessment)`，由 Pydantic AI `ModelProfile` 选择并校验 Native 或唯一
   不可执行的 Tool Output；项目不手写平行 output schema/tool/part parser，profile 未明确支持时在请求前失败，
   且不动态切换或重试；
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
- [ADR-0037：把语义 assessment 作为 triage 的正式默认路径](../adr/0037-make-semantic-assessment-the-default-triage-path.md)
- [ADR-0038：限定语义 assessment 的远端数据投影](../adr/0038-limit-semantic-assessment-remote-data-projection.md)
- [ADR-0041：准入 OpenCode Go 工具输出式语义 assessment](../adr/0041-qualify-opencode-go-tool-output-for-support-semantics.md)
- [ADR-0042：由 Pydantic AI ModelProfile 决定结构化输出方式](../adr/0042-use-pydantic-ai-model-profile-for-structured-output.md)
- [ADR-0043：分离支持目标、现象陈述与维护证据深度](../adr/0043-separate-support-goals-observations-and-maintenance-depth.md)
- [ADR-0044：语义 assessment 直接使用 Pydantic AI Agent output_type](../adr/0044-use-pydantic-ai-agent-output-type-for-support-semantics.md)
- [ADR-0012：让 Pydantic AI Deferred Tools 位于领域 Agent runtime 之后](../adr/0012-use-pydantic-ai-deferred-tools-behind-domain-runtime.md)
- [有界 Agent 单步与恢复流程](flows/bounded-agent-step.md)
- [OpenCode Go](https://opencode.ai/docs/go/)
- [DeepSeek Responses API](https://api-docs.deepseek.com/api/create-response)
- [DeepSeek Models & Pricing](https://api-docs.deepseek.com/quick_start/pricing)
