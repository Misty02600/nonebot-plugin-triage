# 模型 Provider 支持矩阵

最后更新：2026-09-15

这份矩阵记录 NoneBot Triage Agent 对精确模型组合已经取得的质量证据，不代表 Pydantic AI 或厂商 SDK 的
全部能力，也不是运行白名单。Pydantic AI `ModelProfile` 负责模型传输能力和默认结构化输出方式；项目按
`Provider + API 族 + 精确 model + endpoint revision + settings revision + task/schema/Prompt + 隐私策略 + 预算 + 评测 revision`
记录 held-out。
未登记组合可以运行，但只能标记为未验证；B1、B4、支持入口语义 assessment、教学注释、公开 Answer 和
Bug Agent 的质量结论分别记账，不能相互继承。“OpenAI-compatible”本身不能决定 Provider 身份；部署者
需要用 `openai-chat:<model>` 显式选择兼容 Chat 传输，再用 Base URL 指向目标服务。其他部署端地址只能
覆盖已经显式选择、且构造器支持该参数的 Pydantic AI Provider。

当前依赖为 Pydantic AI / Evals 2.43.0、Harness 0.31.0。Provider HTTP 客户端与 Mock 传输同步
使用 HTTPX2；Harness 文件工具通过原生 Toolset capability 注册，前缀适配保留公开工具名以供
能力事件归属。当前模型构造只使用 Pydantic AI 原生解析和 Provider factory；项目不再维护 OpenCode
专属 Profile、HTTP 请求改写或费用归一化。SDK/价格数据的升级可能改变传输细节和费用估算，按新环境重新预检。

## 状态含义

当前教学单次输出预算为 32768，累计为 384k；下表既有真实评测的 16384 / 192k 数值保留为历史条件，
不能继承为新预算的资格。语义、Guidance、Bug 分别使用 240 / 2048 / 800 单次输出；维护者对话默认使用
8192 单次输出与 512k 单轮累计。累计 token 是防止失控的宽松止损线，不是常态配额。

DeepSeek 官方 Chat 绑定完全使用 Pydantic AI 2.43.0 的 `DeepSeekProvider`、模型 Profile 和统一
`ModelSettings.thinking`。项目不再为 `deepseek-flash` 补别名 Profile，也不覆盖 `max_tokens`/
`max_completion_tokens` 映射；模型名和请求字段是否受支持由当前 Pydantic AI 版本决定。
教学、语义分流、公开引导、Bug 与行为 Agent 共用返回模型名检查：名称精确相等，或预期与实际 Provider
均为 `deepseek` 且请求 `deepseek-v4-flash`、返回 `deepseek-flash`，才视为名称匹配。
该单向映射依据官方端点的已捕获响应，不匹配反向映射、其他型号或兼容网关；Provider 身份检查保持独立。
原始响应名称不重写，诊断继续保留请求与返回身份，资格键也不做别名归并；不同返回模型的费用仍不冒用请求模型计价。

- **已验证**：精确 transport、任务、Prompt、隐私和预算组合完成了所列 held-out；
- **未验证**：Pydantic AI 与项目任务合同允许运行，但项目没有该精确组合的完整质量结论；
- **不可用**：缺少实现或 Provider 依赖、ModelProfile 不满足任务技术要求，地址不符合安全策略，或所选
  Provider 不支持部署端地址覆盖。

已采纳的产品契约要求每轮非空 `triage` 请求默认经过受限语义 assessment，不设产品级模型启用开关。
传输无关的 v7 请求投影与输出 schema、一次性失败关闭 service、固定 Prompt 的结构化 Pydantic AI Agent client
和确定性 router 已经实现；模型只产出 signals，不产出 action 或 authorization。插件 runtime 必须持有
assessment service，首轮与续问每轮调用一次；通用 client 以 `output_mode=auto` 消费 ModelProfile，不维护
第二份传输能力结构，也不会在失败后切换输出方式。当前 `QUALIFIED_SEMANTIC_TASKS` 为空；
国内 Alibaba Qwen3.6 Flash 在已删除的专属非思考设置下曾取得 schema / status 1.000、exact 0.975，
但该结果与旧 OpenCode 结果一样只保留为历史证据。所有可解析组合都按未验证运行，并执行相同的
schema、隐私、预算和模型外路由合同，失败时才变成 unavailable / abstain。这不是词表产品模式，也不能由
capability annotation 的评测结果推导 semantic 质量。

维护者已经单独批准语义 assessment 的数据类别：只允许发送当前单条、经规范化和模型前秘密守门的
`triage` 请求文字。Reply / Thread 历史、身份与 scope、配置、环境变量、日志、源码、运行证据、能力索引和
`restricted` 证据均不得进入请求。该数据批准本身不是任一 Provider/model 的“支持”证据。历史 OpenCode
资格调用只属于当时精确连接与 Prompt；当前运行不会继承。当前中文 v7 Prompt v5 的原生 Provider 资格也
不会继承英文 Prompt v4 的结论。

普通用户 Bug 判定是独立任务，不继承 semantic assessment、guidance、能力注释或历史 B4 smoke 的资格。
[ADR-0053](../adr/0053-allow-relevant-source-and-log-bodies-for-bug-assessment.md) 允许独立合格的 Bug Agent
接收与本案相关的源码、关联日志、完整 traceback 和获准设计摘录，并要求这些部署证据在出站前清理秘密。
[ADR-0060](../adr/0060-use-scope-thread-and-post-route-conversation-context.md) 另允许直接 Reply 进入路由后任务；
[ADR-0061](../adr/history/0061-read-latest-bounded-conversation-window-for-bug-assessment.md) 把 Bug 聊天读取收窄为当前会话
最新有界窗口，并允许投影会话关系所需的消息 / 用户 ID、角色和段元数据。聊天正文不做凭据或个人信息遮蔽；
平台 transport envelope 不进入。[ADR-0065](../adr/0065-only-expose-conversation-history-for-supported-platforms.md)
进一步规定：只有已绑定真实历史 Provider 时才暴露聊天工具，其他平台不使用本地滚动窗口模拟能力。
当前已经实现 Pydantic AI 原生 Agent / Tools、闭合 candidate schema、确定性 reconciliation、有界源码 / 日志 /
设计 / 对话工具、LocalStore reviewed catalog 与插件运行接线。中文 Prompt v6 与 v7 的冻结失败结果不回写、
不重算，也不向新 Prompt 继承。Prompt v8 先用 5 条 development case 验证“没有会话历史 Provider 时不调用
不存在的工具”等边界，再只运行一次全新的 16 条 forward-heldout。该正式 Gate 的 schema、verdict、occurrence、
responsibility、citation、budget、usage、scenario 与 safety 均为 1.000，16 / 16 通过；共消耗 166,393 input /
6,116 output tokens、5,724 microUSD。该 OpenCode 精确组合现只保留为历史证据，`QUALIFIED_BUG_TASKS`
当前为空，完整 trajectory 仅保存在本地 `reports/`。Alibaba Qwen3.6 Flash 复用同一 16 条冻结 Fixture 做了一次独立评测：
schema、verdict、occurrence、responsibility、citation、scenario 分别为 0.875、0.875、0.8125、0.7273、
0.875、0.875，只有 budget、usage 与 safety 为 1.000，因此正式 Gate 失败且不登记 Bug 资格。该失败报告
冻结，不用于修改 Prompt 后重跑同一 held-out。
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

维护者项目对话又是一个独立任务，不能继承 Bug、semantic、公开 Answer 或教学注释的质量资格。每条消息在
部署内唯一的长期会话上启动一次 Pydantic AI 只读 Agent Run；当前 ModelProfile 必须支持 function tools。
单轮最多 15 次模型请求、60 次工具调用、512000 total token 和 1 美元，单次输出默认不超过 8192 token。
Agent 可读取 Capability Shadow 与项目根目录内通过硬拒绝策略的只读文件。LocalStore JSON 保存 Pydantic AI
原生消息历史和 Provider 续接元数据，Harness 在上下文达到模型窗口 80% 时压缩。该任务尚未完成真实模型
held-out，因而只属于 provisional dogfood，不能宣称多源项目问答质量已经合格。

`evaluate-b4-real` 通过统一 `provider:model` 与 Pydantic AI 原生 Provider 提供同模型多 trial harness。
报告显式绑定 Prompt/schema/policy/source revision 与冻结 regression / forward-hidden split；
B1/B4 后验结构拒绝作为 trial 失败计量，只有无法恢复 usage/cost 等边界才中止整场。DeepSeek 首轮因响应后 usage 审计缺口失败关闭；run-2 又在约 32.5 秒后以 `cost_unknown`
失败且没有 partial。run-3 的新审计保留了 10 个 attempt、9 个 response、527 microUSD 已知费用与最后一个
未知响应，但仍无 success report。三次失败都不构成质量或 Provider 线上资格证据。OpenAI 与 Anthropic
尚未执行。OpenCode Go 的 B4、semantic v6 / 英文 v7 Prompt v4 与 Bug 英文 Prompt v4 / v5 历史结果都不能
继承给当前中文 Prompt。

插件保留窄 transport 身份与预算配置，但已删除产品级 `enabled` 字段。Pydantic AI
`provider:model` 是唯一 transport 选择；旧 `NBTRIAGE_MODEL_BACKEND` 已删除，出现时会拒绝配置。未配置 model 时，semantic、
教学注释与 Answer 子服务进入 unavailable，完整插件仍可通过商城式无私有密钥导入并保留确定性能力索引；
已配置 transport 但缺少 Provider SDK、密钥或任务所需传输能力时，对应模型增强记录降级而不阻断启动。
各任务 `QUALIFIED_*_TASKS` 只保留精确评测历史，不参与客户端装配或正式本地 Problem 写入许可。
Tool / Native 支持及默认选择仍只由 Pydantic AI `ModelProfile` 表达；测试注入 fake service 只验证调用
编排，不产生质量标签。

## 当前与历史证据矩阵

教学任务的当前 Prompt、request、Schema 与单元预算标识以
[教学合同常量](../../src/nbtriage/capability/teaching/annotations.py)为准；服务并发由部署配置和运行日志记录，
不属于单元预算标识。下表历史评测只证明当时的精确合同，不继承为当前版本的质量资格。OpenCode 行全部
是历史证据，不是当前产品支持行，也不再有专属 adapter 或回归测试。

2026-09-11 整合导航后端、回复用法与场景 Evidence 修正时，独立提交使用 Schema 13 / Prompt v114 /
request v81。当时的整合版本保留现有入口模式级 Prompt 装配；尚未并入细粒度条件片段及 Alconna 分隔符、
参数数量说明工作。此前诊断的合同编号保留原意，不继承为本次整合的模型质量资格。

以下 v72–v80 / v110–v113 为并行开发阶段的验证记录，不代表当前整合版本。
2026-09-12 整合为 Prompt v115 / request v82 / Schema v13：包含既有 v114 / v81 修复、
条件 Prompt 片段、Alconna 分隔符 / 前置词 / 重复选项 / 有限参数数量 / AntiPattern 事实与文档工具指引。
未并入正在开发的 Answer、教学召回或知识索引格式改动；此前真实诊断不自动成为此整合版本的模型质量资格。
本次仅用暂存内容导出的独立副本验证：完整 capability 测试 529 项通过，教学核心、适配器与发现层
BasedPyright 为 0 错误 / 0 警告，改动 Python 文件 Ruff 检查与格式检查通过；未新增真实模型请求。

2026-09-10 的 Prompt v110 / request v72 补齐生效 Alconna 分隔符的确定性模板与校验，Schema 仍为 v12。
分层语法、回复省略、别名 / mention、family、缓存失效和公开输出已有离线回归；尚未运行该修订的真实模型
诊断或 held-out，因此不据此声称模型理解负担、token 消耗或生成质量已经改善。

随后 request v73 补齐顶层常规 `requires` 前置词的有序事实与确定性模板；Prompt v110 / Schema v12
不变。该修订仅有离线结构与合成 parser 证据，不新增真实模型质量资格。

request v74 进一步标记并拒绝尚不支持的关键字参数，避免生成缺少固定键名的错误位置参数用法；
Prompt v110 / Schema v12 仍不变，不新增模型能力或真实模型质量资格。

2026-09-11 在同一合同下运行一个合成 `MultiVar(str, 3)` 自主查定义诊断。OpenCode Go 首次因缺少
会话标识返回 HTTP 400；按官方要求仅在隔离诊断中补充客户端身份和稳定会话请求头后，一个有效模型请求
生成并发布 1 个单元。模型未调用源码读取或定义导航工具，输出 `pack <内容>...`，遗漏原生解析器已验证的
最多 3 项限制。初始证据不含完整构造行，故不能据此声称模型看到了该行却误解了参数；它证明仅提供工具
不足以保证补全未进入初始事实的语义。该单样本不提升质量资格；生产 Provider 请求头适配不在本次变更内。

随后 request v75 从原生 MultiVar 采集有限数量事实，并由代码在完整教学视图补入上限。Prompt v110 /
Schema v12 保持不变；这项保证由合成 parser、固定模型输出和缓存/发布回归验证，不依赖模型主动查定义。
没有为本修订追加真实模型请求，也不继承前述单样本为当前质量资格。

request v76 将已确认的无参数原生 count Option 渲染为可重复组；复用现有结构校验，
Prompt v110 / Schema v12 不变。此次为确定性语法支持，没有追加真实模型请求或提升语义质量资格。

request v77 复用该标记支持带普通参数的原生 append Option，保持 Prompt v110 / Schema v12；
组后重复性由代码确定，累积值的业务用途仍由 Handler 证据说明，没有追加真实模型请求。

Prompt v111 在既有入口模式 / baseline 分段上，按当前请求进一步选择五类专项规则：
aliases、shortcut_count、canonical_usages、config_projections、gate_candidates 非空时才分别加入
别名合并、快捷指令、模板命名与保留、配置引用、门禁候选关联的详细说明。多入口取所需片段的并集，
每段只加入一次，入口专项规则明确只适用于具备对应事实的 entry。通用权限与限流判断、源码中未被
候选覆盖的条件、配置禁用行为、回复证据和公开字段职责保持常驻；不以缺少候选推断没有限制。
实际模型请求使用条件装配函数，SYSTEM_INSTRUCTION 继续保留全量片段用于兼容与合同检查。
request v77 / Schema v12 不变，Prompt ID 隔离旧缓存。92 项 Prompt / 模型适配合同测试通过；
没有追加真实模型测评，不声称语义质量、token 用量或时延已经改善。

request v78 保留 AntiPattern 的模式身份，避免将其 origin 误作为正向输入类型；复用现有
framework_semantics，在 runtime 参数结构或 family shapes 出现该身份时附带 nepattern 0.7.8
的反向验证概念。具体规则仍依赖当前源码证据，Prompt v111 / Schema v12 不变。
265 项快照、模板、Evidence 装配与模型适配合同测试通过，没有追加真实模型测评。

Prompt v113 收紧文档工具随工具装配的使用指引：缺少框架 API 一般含义时优先检索版本匹配文档，
查询使用具体 API 与问题；当前插件行为由插件源码和 Runtime 支持。文档不足或与源码存在疑问时，
核对版本并按需导航已安装框架定义；证据足够时停止，不强制检索或同时读取两种来源。
此修订沿用当前 request v79 / Schema v13，不改变检索器或增加静态框架语义。
110 项 Prompt、工具与模型适配合同测试通过。
离线工具合同只能验证指引进入模型请求及证据捕获，不能证明模型会自主选择正确工具或改善教学质量。

2026-09-11 随后用 DeepSeek 官方 `deepseek-v4-flash`、当前 thinking-high 设置做了三案各一次的合成诊断：
got 查询一次版本匹配文档后完成，完整证据用例无补证；reject 直接导航框架源码并追到内部状态方法和
重载声明，耗尽六次工具预算后完成。三案均通过结构校验，主要教学语义正确，但两个交互用例的文字
有将纯文本提取收窄为纯文本输入要求的风险；不能登记为全部语义通过。共七次模型请求，无旧版对照，
不能证明 v113 改善或工具选择已稳定。使用生产客户端和工具，合成请求与访问根由 harness 组装；
没有启动真实 Bot 或测量完整生产发现链。价格表无法计算该模型成本，美元限制未生效，
请求 / 工具 / token / 时间上限保持启用；原始诊断只保留在本地 reports，不新增质量资格。

同日以当前 request v80 / Prompt v113 / Schema 13 另做三案两组的文档预供对照：夹具预先指定
got/reject 的 API 名，预供组把现有版本匹配检索结果去重后加入初始 Evidence，工具继续可用。
got 的补证调用由3降至1，reject 由6降至1，主要交互语义均正确；完整证据对照两组均无补证，
但其中一组多一次结构纠正，不能把总请求差异全部归因于文档预供。agentic reject 本次查过文档，
复合查询却未命中所需说明；预供组仍存在一次重读源码或重复查询，说明选择顺序并非唯一问题。
此小样本支持评估已知相关 API 的有界文档预供，不证明通用 API 识别已实现或稳定节省比例；
生产行为未因此改变，成本仍不可计算，不新增模型资格。

| Provider | API 族 | model / profile | 安装依赖 | 离线合约 | 获授权线上门 | 当前状态 | 主要证据或缺口 |
|---|---|---|---|---|---|---|---|
| OpenAI | Responses | 部署者使用 `openai:<model>` 选择模型；profile 必须声明当前任务所需的 JSON Schema 与 function tools | 基础 wheel 安装 Pydantic AI 控制层；`openai` extra 只补 Provider SDK | 原生 Provider/Profile 构造与通用 B1/B4 客户端合同通过；旧厂商 wire 测试仅作历史证据 | 未执行当前任务 held-out | 未验证 | 项目尚无精确模型质量结论；产品 runtime 不再提供 `openai-responses` backend 别名 |
| DeepSeek | Chat Completions（Pydantic AI 原生 Provider） | `deepseek:<模型 ID>`；短任务使用 `thinking=false`，维护者对话与教学使用 `thinking=high`；教学单次输出 32768、单元累计 384k | 安装 `openai` extra；使用 `DEEPSEEK_API_KEY` 与 Provider 默认 endpoint | Pydantic AI 2.43.0 原生 Provider/Profile、统一 thinking、结构化输出和 usage；项目不补模型别名或请求字段 | 旧 OpenCode 与早期 DeepSeek 诊断仅保留历史证据 | 未验证 | 新组合必须按任务、设置、连接和预算独立评测 |
| Anthropic | Messages | 部署者使用 `anthropic:<model>`；profile 必须声明当前任务所需的结构化输出与 tools | `anthropic` extra 补 Provider SDK | 原生 Provider/Profile 构造与通用 B1/B4 客户端合同通过；旧厂商 wire 测试仅作历史证据 | 未执行当前任务 held-out | 未验证 | 历史离线模型名不构成质量承诺；产品 runtime 不再提供 `anthropic-messages` backend 别名 |
| Google | GenAI | 使用 Pydantic AI 官方 `google:<model>` 模型标识 | 部署者另行安装 Pydantic AI 所需 Google Provider 依赖 | 运行时由 ModelProfile 检查当前任务能力 | 未执行 | 未验证 | 无项目专用 adapter；通用 Pydantic AI transport 可运行，实际能力不足时任务失败关闭 |
| Alibaba Cloud Model Studio | OpenAI-compatible Chat | `alibaba:<model>`；只传递 Pydantic AI 统一 settings，不注入 `enable_thinking` 等厂商私有字段 | `openai` extra；Key 由 Pydantic AI 原生 `AlibabaProvider` 读取 | Provider factory、受限 Base URL 覆盖、统一 ModelProfile / settings / usage 路径通过本地合同测试 | Qwen3.6 Flash 的 40 条 semantic forward-heldout 只属于已删除的 `alibaba-qwen3.6-non-thinking-v2`；schema / status 1.000、exact 0.975 | 未验证 | `QUALIFIED_SEMANTIC_TASKS` 为空；若原生 Profile 不支持任务所需 thinking / tool / output 组合，运行时失败关闭，项目不增加厂商补丁 |
| Alibaba Cloud Model Studio | OpenAI-compatible Chat | `qwen3.6-flash`；历史国内 endpoint 与已删除 settings v2；120 秒 / 800 output token；中文 Bug Prompt v8 | 同上 | Agent 原生 Tools / ToolOutput、串行工具约束、身份和评测侧官方牌价成本上界可运行 | 16 条冻结 forward-heldout 的 verdict / occurrence 为 0.875 / 0.8125；schema / citation / scenario 为 0.875，responsibility 为 0.7273；成本上界 46,372 microUSD；Gate 失败 | 未验证 | 不进入 `QUALIFIED_BUG_TASKS`；Pydantic AI 价格表暂不能在请求中执行 Qwen 的美元 cost limit，评测器只在每案结束后按显式牌价审计 |
| Alibaba Cloud Model Studio | OpenAI-compatible Chat | `qwen3.6-flash`；历史国内 endpoint 与已删除 settings v2；300 秒 / 16384 output token；历史 capability Prompt `capability-teaching-annotation-v4-prompt-v38-zh` | 同上 | v38 把已识别且能归属到当前 Matcher 的 Uninfo Permission 投影为模型外 `fixed_constraints`，最终公开 requirement 无条件合并安全下限 | 20 条 v11 forward-heldout 的 schema / Evidence / 投影 / 安全 / 预算 / 工具案例 / 12 条源码提取均为 1.000，语义 0.750、旧列表成员保留 0.500；Gate 失败 | 未验证 | 该结果只属于历史 schema / Prompt，不进入 `QUALIFIED_CAPABILITY_ANNOTATION_TASKS`，也不继承给当前 schema 7 |
| 任意第三方 | Pydantic AI 已支持的 Provider | 使用官方 `provider:model` 标识；可选受限 Base URL | 部署者安装对应 Provider 依赖 | 运行时由 ModelProfile 与项目 schema / Evidence 校验；构造器不支持地址覆盖时失败 | 未执行 | 未验证 | 自定义连接默认未验证；项目不因协议兼容标签继承质量结论，也不自动路由或 fallback |
| 任意第三方 | OpenAI-compatible Chat | 使用 `openai-chat:<model>` 并显式配置受限 Base URL | `openai` extra；Key 使用 `OPENAI_API_KEY` | 复用 Pydantic AI `OpenAIChatModel` 与通用 profile，继续执行项目 schema / Evidence 校验 | 未执行 | 未验证 | 可以运行，但兼容协议不等于厂商能力或质量已验证；不自动发现模型、路由或 fallback |

教学 Prompt 按请求组合四个稳定片段：core 始终发送，anchored 与 family 按 invocation mode 发送，baseline
只在存在 previous annotation 时发送；不再把无关的 Parser、family 或旧基线规则塞入每个教学单元，也不按
alias、mention、gate 或 config 等细粒度标志继续拆分组合。

首包只沿 Handler / helper 的普通调用自动展开两层，并在深度边界执行定义导航前停止；已展示调用继续附带
`navigation_ref`。Handler 形参中的 `Depends(provider)` 与注册装饰器显式
`parameterless=[Depends(provider)]` 都作为确定性依赖边闭合。complete family 的完整成员 manifest 仍在首包闭合，但 family 只获得
`python_open_definition`，用于在有限工具预算内选择性补读少量共同定义，不能浏览目录、全文搜索或逐成员读源码。

教学 Agent 在每次执行证据工具前原子领取预算，同一单元内使用 Pydantic AI 顺序执行模式；即使模型在
一个响应中提交多个工具调用，超出 10 次余额的调用也不执行，并返回 `tool_budget_exhausted`。不同单元仍可并发。
显式维护诊断则在 Pydantic AI 的 `WrapperModel` 调用边界保存每个 Provider 响应，因此即使随后因输入预算、
输出截断或结构校验失败退出，也能保存原始 assistant 文本、thinking 和 tool call 参数；仍不保存初始 Prompt或密钥。

## 已归档的 OpenCode B4 证据

OpenCode Go 的 `support-semantic-v7`、Bug Prompt v8 与 B4 transport 结果只作为冻结历史记录保留。专属
Provider、Profile、客户端、费用归一化和假 HTTP 回归已经删除；维护 CLI 也不再接受 OpenCode backend。
若部署者自行把该地址配置成 Pydantic AI 的通用兼容 endpoint，只能获得未验证标签，项目不承诺其非标准
thinking、请求字段、身份或费用语义。

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
再由 Pydantic 参数解析、项目 action schema、白名单和剩余预算二次校验。Provider 是否支持 strict tool
definition 完全取自原生 ModelProfile；无论 wire 是否 strict，项目都执行 Pydantic 与领域层本地复核，且不能
把本地复核称为供应商 strict。Pydantic AI 不拥有会话循环、工具执行或持久化 message history；当前
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
- [ADR-0009：模型调用核心采用异步协议](../adr/history/0009-use-async-model-boundary.md)
- [ADR-0011：公开默认关闭且按资格门装配的模型配置](../adr/history/0011-expose-disabled-qualified-model-configuration.md)
- [ADR-0090：在部署端配置 Pydantic AI Provider 地址](../adr/0090-configure-pydantic-ai-provider-base-urls-at-deployment.md)
- [ADR-0091：用 Pydantic AI 模型 ID 作为公开传输选择器](../adr/0091-use-pydantic-ai-model-ids-as-the-public-transport-selector.md)
- [ADR-0092：删除旧模型 backend 配置兼容](../adr/0092-remove-legacy-model-backend-configuration.md)
- [ADR-0037：把语义 assessment 作为 triage 的正式默认路径](../adr/0037-make-semantic-assessment-the-default-triage-path.md)
- [ADR-0038：限定语义 assessment 的远端数据投影](../adr/0038-limit-semantic-assessment-remote-data-projection.md)
- [ADR-0041：准入 OpenCode Go 工具输出式语义 assessment](../adr/history/0041-qualify-opencode-go-tool-output-for-support-semantics.md)
- [ADR-0042：由 Pydantic AI ModelProfile 决定结构化输出方式](../adr/0042-use-pydantic-ai-model-profile-for-structured-output.md)
- [ADR-0043：分离支持目标、现象陈述与维护证据深度](../adr/history/0043-separate-support-goals-observations-and-maintenance-depth.md)
- [ADR-0044：语义 assessment 直接使用 Pydantic AI Agent output_type](../adr/history/0044-use-pydantic-ai-agent-output-type-for-support-semantics.md)
- [ADR-0052：把 Bug 定义到整个 Bot 软件责任链](../adr/0052-define-bug-across-the-bot-software-responsibility-chain.md)
- [ADR-0053：允许 Bug Agent 使用相关源码与日志正文](../adr/0053-allow-relevant-source-and-log-bodies-for-bug-assessment.md)
- [ADR-0060：用作用域 Thread 承接一次补充并在路由后投影会话上下文](../adr/0060-use-scope-thread-and-post-route-conversation-context.md)
- [ADR-0061：为 Bug 判断读取当前会话最新有界聊天窗口](../adr/history/0061-read-latest-bounded-conversation-window-for-bug-assessment.md)
- [ADR-0065：只为明确支持的平台提供 Bug 会话历史工具](../adr/0065-only-expose-conversation-history-for-supported-platforms.md)
- [ADR-0012：让 Pydantic AI Deferred Tools 位于领域 Agent runtime 之后](../adr/0012-use-pydantic-ai-deferred-tools-behind-domain-runtime.md)
- [ADR-0128：用原生消息快照保存维护者自由对话](../adr/0128-use-native-message-snapshots-for-maintainer-conversations.md)
- [有界 Agent 单步与恢复流程](flows/bounded-agent-step.md)
- [OpenCode Go](https://opencode.ai/docs/go/)
- [Alibaba Qwen3.6 Flash 模型能力与价格](https://help.aliyun.com/zh/model-studio/qwen3-6-flash)
- [Alibaba OpenAI-compatible Chat 参数](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions)
- [DeepSeek Responses API](https://api-docs.deepseek.com/api/create-response)
- [DeepSeek Models & Pricing](https://api-docs.deepseek.com/quick_start/pricing)
