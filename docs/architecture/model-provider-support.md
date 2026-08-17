# 模型 Provider 支持矩阵

最后更新：2026-08-17

这份矩阵记录 NoneBot Triage Agent 对精确模型组合已经取得的质量证据，不代表 Pydantic AI 或厂商 SDK 的
全部能力，也不是运行白名单。Pydantic AI `ModelProfile` 负责模型传输能力和默认结构化输出方式；项目按
`Provider + API 族 + 精确 model + task/schema/Prompt + 隐私策略 + 预算 + 评测 revision` 记录 held-out。
未登记组合可以运行，但只能标记为未验证；B1、B4、支持入口语义 assessment、教学注释、公开 Answer 和
Bug Agent 的质量结论分别记账，不能相互继承。“OpenAI-compatible”本身也不授权任意 base URL。

## 状态含义

- **已验证**：精确 transport、任务、Prompt、隐私和预算组合完成了所列 held-out；
- **未验证**：Pydantic AI 与项目任务合同允许运行，但项目没有该精确组合的完整质量结论；
- **不可用**：缺少实现或 Provider 依赖、ModelProfile 不满足任务技术要求，或请求了项目禁止的任意 base URL。

已采纳的产品契约要求每轮非空 `triage` 请求默认经过受限语义 assessment，不设产品级模型启用开关。
传输无关的 v7 请求投影与输出 schema、一次性失败关闭 service、固定 Prompt 的结构化 Pydantic AI Agent client
和确定性 router 已经实现；模型只产出 signals，不产出 action 或 authorization。插件 runtime 必须持有
assessment service，首轮与续问每轮调用一次；通用 client 以 `output_mode=auto` 消费 ModelProfile，不维护
第二份传输能力结构，也不会在失败后切换输出方式。OpenCode Go semantic factory 与公开评测记录已经实现；
当前中文 Prompt v5 已通过精确绑定的 40 条 forward-heldout，schema、status 与 exact 均为 1.000，
`QUALIFIED_SEMANTIC_TASKS` 只登记该精确组合。该集合只表示已验证质量；其他可解析组合仍会执行相同的
schema、隐私、预算和模型外路由合同，失败时才变成 unavailable / abstain。这不是词表产品模式，也不能由
capability annotation 的评测结果推导 semantic 质量。

维护者已经单独批准语义 assessment 的数据类别：只允许发送当前单条、经规范化和模型前秘密守门的
`triage` 请求文字。Reply / Thread 历史、身份与 scope、配置、环境变量、日志、源码、运行证据、能力索引和
`restricted` 证据均不得进入请求。该数据批准本身不是任一 Provider/model 的“支持”证据。OpenCode Go 的
精确 Provider/API/model/task、数美元资格预算和真实合成调用已由 ADR-0041 另行授权；历史结果只属于当时
精确 Prompt。当前中文 v7 Prompt v5 已通过自己的真实 forward-heldout，没有继承英文 Prompt v4 的资格。

普通用户 Bug 判定是独立任务，不继承 semantic assessment、guidance、能力注释或历史 B4 smoke 的资格。
[ADR-0053](../adr/0053-allow-relevant-source-and-log-bodies-for-bug-assessment.md) 允许独立合格的 Bug Agent
接收与本案相关的源码、关联日志、完整 traceback 和获准设计摘录，并要求这些部署证据在出站前清理秘密。
[ADR-0060](../adr/0060-use-scope-thread-and-post-route-conversation-context.md) 另允许直接 Reply 进入路由后任务；
[ADR-0061](../adr/0061-read-latest-bounded-conversation-window-for-bug-assessment.md) 把 Bug 聊天读取收窄为当前会话
最新有界窗口，并允许投影会话关系所需的消息 / 用户 ID、角色和段元数据。聊天正文不做凭据或个人信息遮蔽；
平台 transport envelope 不进入。[ADR-0065](../adr/0065-only-expose-conversation-history-for-supported-platforms.md)
进一步规定：只有已绑定真实历史 Provider 时才暴露聊天工具，其他平台不使用本地滚动窗口模拟能力。
当前已经实现 Pydantic AI 原生 Agent / Tools、闭合 candidate schema、确定性 reconciliation、有界源码 / 日志 /
设计 / 对话工具、LocalStore reviewed catalog 与插件运行接线。中文 Prompt v6 与 v7 的冻结失败结果不回写、
不重算，也不向新 Prompt 继承。Prompt v8 先用 5 条 development case 验证“没有会话历史 Provider 时不调用
不存在的工具”等边界，再只运行一次全新的 16 条 forward-heldout。该正式 Gate 的 schema、verdict、occurrence、
responsibility、citation、budget、usage、scenario 与 safety 均为 1.000，16 / 16 通过；共消耗 166,393 input /
6,116 output tokens、5,724 microUSD。`QUALIFIED_BUG_TASKS` 只登记这一个精确组合，完整 trajectory 仅保存在
本地 `reports/`。
2026-08-16 另以非冻结合成案件完成一次产品级开发验收：真实 NoneBot 插件装载、生产 OpenCode Go factory、
本地 NoneBot 2.5.0 知识索引、Bug Agent、版本化技术签名与临时 SQLite ORM 在同一进程串联。完整证据案件
得到 `bug / single_observed / target_plugin` 与 `contract_outcome` 签名；同一签名的两次 Report 归入同一个
Problem，并累计为两次 Occurrence，待处理查询返回一项，执行“解决”后不再返回。证据不足案件保持
`unknown` 且不生成记录命令。两案共 9 次模型请求、36,339 input / 746 output tokens。该结果只验证真实
产品接线与失败闭合，不是新的 held-out，也不改变 Prompt v8 的资格 identity。
2026-08-14 的 OpenCode Go 官方资料列出
`deepseek-v4-flash` 不用于训练、保留为 0 天，
同时注明 ZDR 当期只有效至 2026-08-31；后续资格运行必须重新核对，不能从 semantic 支持行永久继承。

公开能力 Answer Agent 是另一项任务：router 选中 guidance 后，它接收当前单条问题、模型外过滤为 public 的
能力事实、经 Evidence 闭包校验的公开教学注释，以及有界的首轮 / 直接 Reply 可见正文，返回带事实 ID 引用的自然语言回答。Conversation Context
只能消歧，不能成为能力事实或权限。当前 v2 实现已通过闭合 schema、单次 required output tool、零 retry、
Provider 身份和 Handler 回退的离线合约，并完成 Reply 指代与恶意 Reply 权限覆盖两条真实 smoke；仍没有独立
held-out 回答质量 Gate，因此只属于 provisional dogfood。

`evaluate-b4-real` 已提供 DeepSeek Responses、OpenAI Responses 与 Anthropic Messages 的同模型多 trial
harness。报告显式绑定 Prompt/schema/policy/source revision 与冻结 regression / forward-hidden split；
B1/B4 后验结构拒绝作为 trial 失败计量，只有无法恢复 usage/cost 等边界才中止整场。DeepSeek 首轮因响应后 usage 审计缺口失败关闭；run-2 又在约 32.5 秒后以 `cost_unknown`
失败且没有 partial。run-3 的新审计保留了 10 个 attempt、9 个 response、527 microUSD 已知费用与最后一个
未知响应，但仍无 success report。三次失败都不构成质量或 Provider 线上资格证据。OpenAI 与 Anthropic
尚未执行。OpenCode Go 的 B4、semantic v6 / 英文 v7 Prompt v4 与 Bug 英文 Prompt v4 / v5 历史结果都不能
继承给当前中文 Prompt。

插件保留窄 transport 身份与预算配置，但已删除产品级 `enabled` 字段。未配置 backend/model 时，semantic、
教学注释与 Answer 子服务进入 unavailable，完整插件仍可通过商城式无私有密钥导入并保留确定性能力索引；
已配置 transport 但缺少 Provider SDK、密钥或任务所需传输能力时，对应模型增强记录降级而不阻断启动。
`QUALIFIED_PLUGIN_MODELS` 与各任务 `QUALIFIED_*_TASKS` 只保留精确评测历史，不参与客户端装配或正式本地
Problem 写入许可。Tool / Native 支持及默认选择仍只由 Pydantic AI `ModelProfile` 表达；测试注入 fake
service 只验证调用编排，不产生质量标签。

## 当前矩阵

| Provider | API 族 | model / profile | 安装依赖 | 离线合约 | 获授权线上门 | 当前状态 | 主要证据或缺口 |
|---|---|---|---|---|---|---|---|
| OpenAI | Responses | 部署者选择模型；profile 必须声明当前任务所需的 JSON Schema 与 function tools | 基础 wheel 安装 Pydantic AI 控制层；`openai` extra 只补 Provider SDK | B1 Direct Request JSON Schema 与 B4 `function_call` 假 HTTP 合约通过 | 未执行当前任务 held-out | 未验证 | 可通过 `openai-responses` 或 `pydantic-ai` backend 使用；项目尚无精确模型质量结论 |
| DeepSeek | Responses | `deepseek-v4-flash` 滚动别名；`reasoning=none`；`temperature=0`；Provider wire 不承诺 OpenAI strict 字段 | 仓库 `maintainer` group：`pydantic-ai-slim[openai]==2.28.0`；底层 OpenAI SDK 由该 Provider extra 声明；使用显式 `DeepSeekProvider` 和固定官方 endpoint；不提供插件 extra，适配器不进入 wheel | B1 Direct Request 原生 JSON Schema 与 B4 `function_call` 假 HTTP 合约通过；`store=false`、零 SDK retry、usage / request ID / cost 归一化已覆盖；B4 参数仍由 Pydantic 与领域层本地复核 | 有旧直接 SDK B1 工件；三次正式 B4 Gate 均失败关闭且无完整报告。run-3 partial 证明可恢复已知响应/费用与未知请求边界，但不提供 promotion decision | 未验证 | 此行 adapter 仅供维护者评测；产品可另通过 Pydantic AI 官方 Provider 标识运行，但不继承这三次 Gate 的质量结论 |
| Anthropic | Messages | 部署者选择模型；profile 必须声明当前任务所需的结构化输出与 tools；离线合约使用 `claude-sonnet-4-5` | `anthropic` extra 补 Provider SDK | B1 native JSON Schema 与 B4 `tool_use` 假 HTTP 合约通过 | 未执行当前任务 held-out | 未验证 | 可通过 `anthropic-messages` 或 `pydantic-ai` backend 使用；离线模型名不构成质量承诺 |
| Google | GenAI | 使用 Pydantic AI 官方 `google-gla:<model>` 等模型标识 | 部署者另行安装 Pydantic AI 所需 Google Provider 依赖 | 运行时由 ModelProfile 检查当前任务能力 | 未执行 | 未验证 | 无项目专用 adapter；通用 Pydantic AI transport 可运行，实际能力不足时任务失败关闭 |
| 任意第三方 | Pydantic AI 已支持的官方 Provider | 使用官方 `provider:model` 标识 | 部署者安装对应 Provider 依赖 | 运行时由 ModelProfile 与项目 schema / Evidence 校验 | 未执行 | 未验证 | 不包含任意自定义 base URL；项目不因协议兼容标签继承质量结论 |
| 任意第三方 | OpenAI-compatible Chat / Responses | 任意 URL / 模型 | 不提供 | 未执行 | 未执行 | 不可用 | 自定义 base URL 继续禁止；需要项目明确实现固定 Provider 身份与隐私边界 |
| OpenCode Go | Chat Completions | `deepseek-v4-flash`；non-thinking；required 单一 Pydantic AI Agent output tool；60 秒 / 240 token；中文 `support-semantic-v7-prompt-v5-zh` | 复用 `openai` extra：`pydantic-ai-slim[openai]==2.28.0`；不声明内容重复的 OpenCode Go extra | 假 HTTP 覆盖最小 payload、Agent `output_type` 生成的唯一 tool、零 retry、身份/usage/费用与本地双层校验 | 40 条独立 forward-heldout：schema / status / exact 均为 1.000；81,920 input / 3,736 output tokens；1,667 microUSD | 已验证 | `QUALIFIED_SEMANTIC_TASKS` 记录 `opencode-go-forward-heldout-40-20260815-v7-prompt-v5-zh-e` 精确组合 |
| OpenCode Go | Chat Completions | `deepseek-v4-flash`；non-thinking；Pydantic AI Agent `BugAssessmentCandidate` output tool + 会话 / 运行 / 日志 / 源码 / 设计 / 部署只读 Tools；120 秒 / 800 output token；中文 Prompt `bug-assessment-agent-v1-prompt-v8-zh` | 复用 `openai` extra | 原生 Tool / `prepare` 收缩、最新 conversation 窗口、闭合参数与 output、零 Provider retry、一次 output correction、Evidence ID / revision reconciliation、请求 / token / 费用上限均通过离线合同；最多 9 请求、1 次独立聊天 + 6 次通用证据读取；没有历史 Provider 时初始信封明确禁止调用不存在的聊天工具；Provider 并行越界调用不读取证据 | 全新 16 条 forward-heldout：schema、verdict、occurrence、responsibility、citation、budget、usage、scenario、safety 均 1.000；166,393 input / 6,116 output tokens、5,724 microUSD | 已验证 | `QUALIFIED_BUG_TASKS` 记录 `opencode-go-bug-forward-heldout-16-20260815-v1-prompt-v8-zh-d` 精确组合；聊天正文与必要身份关系不遮蔽，源码 / 日志仍清理；详见 ADR-0050、0053、0060、0061、0064、0065 |
| OpenCode Go | Chat Completions | `deepseek-v4-flash`；non-thinking；required 单一 `PublicGuidanceAnswer` output tool；60 秒 / 240 token；中文 Prompt `public-guidance-answer-v2-prompt-v2-zh` | 复用 `openai` extra | 闭合 question / conversation_context / public facts 输入、唯一 output tool、事实引用校验、零 retry、Provider 身份和 Handler 确定性回退均通过；无工具 | 中文 Prompt 的纯合成最小真实 smoke 已通过并正确引用公开 fact；尚无独立 held-out | 未验证 | 可以运行；任务记录为 `pending-opencode-go-public-guidance-v2-prompt-v2-zh`，不能继承 semantic 的质量结论；详见 ADR-0048、0060 |
| OpenCode Go | Chat Completions | `deepseek-v4-flash`；请求显式关闭 thinking；Pydantic AI Agent 输出模型外固定 ID 的多个 teaching entry、结构化 claims / constraints、完整命令正文、内部 gate resolutions 与 Answer Markdown；最多 8 请求 / 5 次证据工具 / 120k total token / 0.05 美元；60 秒 / 16384 output token；当前中文 Prompt `capability-teaching-annotation-v4-prompt-v35-zh` | Harness 0.20.0、Jedi 0.20.0 与 Pydantic AI 公共层属于基础依赖；`openai` extra 只补 OpenCode Go 所用的 OpenAI-compatible Provider SDK | runtime / ast-grep / 内存配置首包、Triage 维护的 Uninfo 常用 Permission 稳定语义、只读 FileSystem、Direct Jedi、版本限定文档检索、动态 Evidence 引用闭包、Alconna 子命令独立 entry、parser 固定 canonical usage、槽位外 `...` 多值记法、Runtime aliases、精确 `@bot`、参数化 Matcher 仅按 Runtime Handler 精确代码身份聚合、数字限流引用、疑似门禁三值闭合、Answer Markdown 安全回退、上一版非证据基线、插件与文件 revision 复核，以及 YAML + Markdown 单 generation 原子发布均有本地合同测试；v35 新增 `display_trigger`，模型外展开后必须与 Runtime literal 集合完全相等，首次错误定向重试，第二次确定性枚举回退 | 最近一次正式质量证据仍是冻结的 v34 / v8：20 条 forward-heldout 中 schema、Evidence、公开投影、安全、预算、工具和 12 条源码提取均 1.000，语义 0.950；25 请求、111,811 input / 11,318 output token、6,398 microUSD。v35 两条已知开发案例的 180 秒诊断 smoke 均通过，共 2 请求、8,410 input / 3,511 output token、1,037 microUSD；同样例使用当前 60 秒超时时四条均在取得响应前 transport failure。生产脱敏 trace 先确认多个复杂单元在 4096 token 处截断，随后又确认两个单元虽已携带 `thinking: disabled`，仍由 Provider 返回纯 Thinking 并在 8192 token 处截断；因此运行预算临时提高至 16384，同时把领域 Evidence 闭包校验前移到 output validator。这些变更尚未形成新的 forward-heldout | 未验证 | 当前 `QUALIFIED_CAPABILITY_ANNOTATION_TASKS` 为空，运行记录使用 `unverified:capability-teaching-annotation-agent-v3:capability-teaching-annotation-v4-prompt-v35-zh`；ADR-0086 允许其正常运行。OpenCode Go 是否始终遵守 thinking 关闭参数仍需生产观察；Provider 延迟与后台吞吐也需继续记录。`.env*`、凭据、教学日志、人工帮助和评测 Gold 不可读；加载失败、未观察到、restricted、补证后仍未知的权限/限流或无可靠共同语义的能力不会进入公开教学；生成 YAML 仍不由 Migut Help 直接消费 |

## 既有 B4 测试 transport 与本次资格的关系

OpenCode Go 当前只有任务级实现；`support-semantic-v7` 中文 Prompt v5 与 Bug Prompt v8 已分别登记自己的精确评测记录。仓库另保留
一条 B4-only 测试
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
- [ADR-0052：把 Bug 定义到整个 Bot 软件责任链](../adr/0052-define-bug-across-the-bot-software-responsibility-chain.md)
- [ADR-0053：允许 Bug Agent 使用相关源码与日志正文](../adr/0053-allow-relevant-source-and-log-bodies-for-bug-assessment.md)
- [ADR-0060：用作用域 Thread 承接一次补充并在路由后投影会话上下文](../adr/0060-use-scope-thread-and-post-route-conversation-context.md)
- [ADR-0061：为 Bug 判断读取当前会话最新有界聊天窗口](../adr/0061-read-latest-bounded-conversation-window-for-bug-assessment.md)
- [ADR-0065：只为明确支持的平台提供 Bug 会话历史工具](../adr/0065-only-expose-conversation-history-for-supported-platforms.md)
- [ADR-0012：让 Pydantic AI Deferred Tools 位于领域 Agent runtime 之后](../adr/0012-use-pydantic-ai-deferred-tools-behind-domain-runtime.md)
- [有界 Agent 单步与恢复流程](flows/bounded-agent-step.md)
- [OpenCode Go](https://opencode.ai/docs/go/)
- [DeepSeek Responses API](https://api-docs.deepseek.com/api/create-response)
- [DeepSeek Models & Pricing](https://api-docs.deepseek.com/quick_start/pricing)
