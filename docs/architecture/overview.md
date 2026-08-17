# NoneBot Triage Agent 架构概览

## 目标与边界

NoneBot Triage Agent 把模糊报障转换为证据可追溯的 `SupportCase`，再选择补问、检索、确定性探针、隔离复现或升级。核心不承诺自动解决全部 NoneBot / QQ 问题，也不把 Issue 分类、摘要或聊天外壳当作主要差异。

### 当前 triage 语义状态

每轮非空 `triage` 的已采纳产品契约是经过受限语义 assessment，不设产品级模型启用开关，也不保留
功能问法词表或固定故障话术作为意图分类器。当前代码已删除词表和 `nbtriage_model_enabled`，并实现传输无关的
v7 assessment 请求 / 输出闭合合同、一次性失败关闭 service、以领域 Pydantic model 作为 `Agent.output_type` 的
结构化 client、确定性 router 与插件运行编排。v7 只产生 guidance、behavior exploration、Bug assessment、
feature feedback 四种 goal 与独立 observation；action 与授权始终由模型外 router 决定。当前系统指令已切换为
中文 `support-semantic-v7-prompt-v5-zh`。它已通过 2026-08-15 的 40 条独立 forward-heldout，schema、status
与 exact 均为 1.000；semantic 评测表只登记该精确组合，但只作为质量标签。其他 Pydantic AI 可解析的模型
可以执行同一任务，并在技术或安全合同失败时才降级为 unavailable / abstain。详见
[ADR-0037](../adr/0037-make-semantic-assessment-the-default-triage-path.md)。远端 assessment 的数据类别已经获准，
但只限当前单条规范化 `triage` 请求文字；Reply / Thread 历史、身份、配置、日志、源码、运行证据和
`restricted` 证据仍不得出站。OpenCode Go 的 Provider/API/model/task、预算与合成资格调用已由 ADR-0041
另行确认；其他组合不能继承，详见
[ADR-0038](../adr/0038-limit-semantic-assessment-remote-data-projection.md)。
OpenCode Go 准入细节见
[ADR-0041](../adr/0041-qualify-opencode-go-tool-output-for-support-semantics.md)。

当前实现覆盖只读离线 Data Gate、B0、B1 RAG-only 基线、B3 可审计会话和 B4 有界 Agent control plane；
OpenAI Responses、DeepSeek Responses 与 Anthropic Messages 已有分任务的离线合约证据，但都不因离线通过而自动成为插件支持。
NoneBot 保留窄 transport 身份和惰性 step-client factory，不再公开产品级模型启用开关。真实入口已有
`triage <自然语言>` 的 Alconna / UniSeg framing、运行观察、scope Thread 一次补充、限流和窄回显。Reply
不恢复 Thread；它的可见正文只在路由后进入 Guidance / Bug，message ID 独立解析 runtime correlation。
首轮与唯一补充轮使用同一 assessment service 与 router。v7 已删除 `incident_intake`，当前在线入口不会产生
`OPEN_INCIDENT`；旧授权与 LiveReportService 只作为兼容领域能力保留。

`bug_assessment` 分支已经接入首轮与补充轮：确定性协调器先用
subject、adapter、source / contract / deployment revision 和规范化请求构造 fingerprint，查询 LocalStore data
中的 reviewed catalog；精确命中后零日志、零源码、零 Agent。未命中时先预加载 public 合同，再让有界
Pydantic AI Agent 按需查询当前 correlation 的运行 / 异常日志、模型外绑定会话的 OneBot 最新群聊天窗口、当前已加载
subject 的 Python 源码、版本化设计知识包与部署摘要；最终由本地 reconciler 检查 citation、freshness、
partial 与冲突，只返回 `bug / not_bug / unknown`，不创建 incident 或外部工单。当前系统指令是中文
`bug-assessment-agent-v1-prompt-v8-zh`。全新的 16 条 forward-heldout 只运行一次，schema、verdict、
occurrence、责任、引用、预算、usage、scenario 与 safety 均为 1.000，16 / 16 通过；运行消耗 166,393 input /
6,116 output tokens、5,724 microUSD。Bug 评测表只登记该精确 Prompt / Fixture / 隐私 / 预算 / evaluation
组合，不继承 v6、v7 或旧英文 Prompt 的质量结论，也不阻止其他模型运行。相关边界见 ADR-0053、ADR-0060、
ADR-0064、ADR-0065 与 ADR-0086。

[ADR-0066](../adr/0066-use-active-teaching-contract-as-bug-precheck.md) 的首个保守纵切已经接入：Bug subject 只从
健康、完整的当前 public ServingView 定位；缺少唯一 subject 或具体观察时，在创建案件指纹、源码后端和 Agent
工具箱前返回，并共用 Thread 的一次补充。当前 active teaching annotation 会进入 public contract Evidence；若
直接 Reply 精确指向报障者本人发送的调用消息，且所有公开 usage 都要求 Reply 上下文、该操作却没有 Reply，
则零调查工具转回 Guidance 纠正。其他参数、媒体、角色、场景、限流和 behavior boundary 仍不做含糊推断；
教学回答的 capability / fact / contract revision 出站绑定也尚未实现。

默认启用的部署本地能力影子索引已经进入运行包：导入期不解析路径；启动钩子只调度后台任务，随后从
LocalStore 插件 cache 解析内部 SQLite 位置。制品扫描、已加载 Plugin / Matcher / Alconna 观察、源码摘要和
FTS5 原子构建在线程中执行，不阻塞 Bot 启动关键路径。同一轮制品解析只枚举一次 distribution package map。
`restricted` 保存 SUPERUSER 与内部管理能力；本地检索组件已支持普通域与维护者域。合格 assessment 选中
guidance 后，普通用户只读当前 adapter 域内自动或显式确定公开的能力，不检查身份或回退 restricted；选中
behavior exploration 后，私聊、群聊和频道才按当前 Bot / Event 的请求者执行模型外 `SUPERUSER` 鉴权。
当前 schema v2 直接保存每条能力记录的披露、平台、分析问题与约束；
不会从 handler 源码推断 Matcher 角色或跨 Matcher 支撑关系。动态或被动入口若缺少可确定展示字段，继续保留
`dynamic_entry` 并退出普通 ServingView，而不是猜测用户能力身份。

B4 已增加 Provider 无关的有界 Agent control plane：模型可在白名单运行观察、train-only 检索、结构化
补证和最终诊断间动态选择；领域 runtime 掌握跨步预算、二次授权、暂停恢复和 trajectory，Pydantic AI
只处理每个步骤唯一 `propose_action` 信封的原生 tool schema 与协议响应；信封中的 action 联合按 capability、
trajectory 与已观察 citation 动态收窄。OpenAI / DeepSeek Responses 与 Anthropic Messages 已有
假 HTTP B4 合约；DeepSeek Responses 不声明供应商 strict，参数仍由 Pydantic 与领域 schema / 动态白名单
在本地复核。OpenCode Go 的 B4 兼容 Chat 夹具仍只用于测试；独立的 semantic adapter 已按 ADR-0041
进入产品适配层，二者资格互不继承。
共享 usage 边界保留返回 Provider/model/request identity 与可选指纹；真实 Gate 把本地后验拒绝的已计费响应
计入对应 trial，并对身份缺失、漂移或无法归一化费用失败关闭。新的 `b4-real-partial` 审计在每次 B1/B4
请求前原子保留 attempt，响应后再记录 identity/usage/cost 或稳定 unknown reason；当前 schema v4 继承
Provider 请求的稳定 failure reason、可选 HTTP status 和后验拒绝类型，并增加 Prompt/schema/policy/source
revision 与冻结 split hash；仍不保存 body、headers 或异常文本，失败关闭不再只依赖进程内账本。
单步适配器同时把 client timeout 与领域剩余 deadline 的较小值作为 SDK timeout 和应用层 hard timeout；
剩余 deadline 为 0 时在网络调用和 call-slot 计数前停止，其他超时保留 `TimeoutError`，由领域 runner 映射为
稳定的 `DEADLINE` 停止原因。完整真实 Gate 另受 whole-run timeout 约束；目标 report 与保留的
`.partial.json` 均禁止覆盖，成功发布按 `report_ready → 新报告 → completed` 收口。
当前已有 scripted 多 trial Gate 与三次失败关闭的 DeepSeek 真实 Gate 尝试，但还没有完整真实质量报告。
run-2 在约 32.5 秒后以 `cost_unknown` 失败，legacy runner 没有留下 success/partial report；请求数、费用与
失败阶段不可恢复，时间只与 30 秒 deadline 一致而不能证明因果。run-3 在第 10 个 attempt 中止，partial
保留 9 个 response、527 microUSD 已知费用与最后一个未知响应。一次获授权的 OpenCode Go
native-schema 探测返回 HTTP 400；该结果只说明兼容传输不能创造服务端能力，并已收口为 evaluation-only
测试事实，不构成 B1 阻塞、产品候选切换或网关决策。

## 已采纳产品方向与当前基础

首个真实用户入口面向独立 NoneBot 部署者：在 Bot 进程中安装入口插件，由私聊、群聊或频道用户发送
`triage <求助内容>`；`@Bot` 和 Reply 可选。疑似故障带 Reply 时，入口再把求助与本机事件、实际运行过的 Matcher、插件 / 模块、平台 API 调用、异常和
版本证据关联，之后转换成传输无关的 `SupportCase` / `SupportSession`。普通群员不能查询任意日志；直接 Reply
和 Bug 模型外锚定的同群可见聊天可以进入对应下游任务且不做内容遮蔽，但不持久化，平台 envelope / 原始用户
ID 不上传；源码、日志和配置仍执行秘密清理。Probe、GitHub 写回和其他副作用仍由维护者审批。当前还
保留默认关闭的 observation-first trial 兼容服务：它能为已获显式授权的 incident 记录脱敏生命周期、查询曝光
和维护者枚举反馈，本地 JSONL 有界轮转；但 v7 当前不签发该授权，现行 `triage` 不会新增 incident 或 trial。
模型 shadow 与 canary 只有在未来重新接入并通过独立决定后才有意义。

该方向的核心不是“用 LLM 从群聊识别 Bug”。调研已发现 AstrBot BugCatcher 覆盖静默监听、LLM 识别、
去重与 Dashboard，NoneBot 也已有 Sentry 错误跟踪。NoneBot Triage Agent 的产品边界保持在“显式支持分流、
疑似故障与运行证据关联、NoneBot 责任层定位、最小补证和可审计验证”。长期决策见
[ADR-0001](../adr/0001-qq-group-report-linked-runtime-evidence.md)，竞品证据见
[产品定位与同类能力](product-positioning.md)。

修复闭环采用责任层路由与分级自治：L0 观察、L1 建议、L2 配置 / 生命周期修复、L3 维护者授权的上游
协作、L4 本地或维护者拥有插件的隔离代码修复。模型不直接持有 Shell、配置或 GitHub 写权限；高权限动作
进入专用执行器并保持逐动作审批。当前只交付 L0 的纯核心观察契约和既有 L1 控制面基础，其余均为规划
能力。完整边界见 [ADR-0002](../adr/0002-tiered-autonomy-and-ownership-aware-remediation.md)。

同一个显式入口承接能力导航、指令纠错、Bug 判定和功能反馈，由 semantic v7 signals 与模型外 router 选择
单一 action；旧 `IntakeDisposition` / Incident 分流只保留为当前在线入口不可达的兼容领域服务。MVP 不代
用户执行有副作用指令，未来能力注册表先覆盖 Alconna。当前已实现严格结构信号、固定优先级路由、显式公开能力 Provider，以及
`on_alconna + MultiVar + OriginalUniMsg + MsgTarget + UniMessage` 的跨平台 `triage` 入口。`@Bot` 与
Reply 可选，但每轮都必须写 `triage`；只有未解决首轮才按稳定 scope 等待下一条显式 `triage`，最多补充一次，
Reply 只作路由后上下文与独立运行 correlation。另有默认启用的本地影子
索引从已加载插件生成带来源的受众、平台范围、分析问题与约束快照，作为后续本地 RAG 的候选事实层。
受众为 `public / restricted`，平台范围为 `all / explicit(adapters) / unknown`；分析缺口以
`analysis_issues` 具体记录，不另存 `ready / pending / conflicted`。维护者 CLI 可显式读取完整维护者域，普通用户
只读取当前 adapter 在范围内、无阻塞 issue、记录状态为 `verified / candidate`、快照明确完整且 generation
新鲜的 `public` 能力；`conflicted / stale` 不进入普通 ServingView。
当前采集以每个已观察的命令或 Matcher 记录为边界，不分析 handler 的用户输出、共享状态读写或跨 Matcher
关系；无法确定的动态或被动入口保留具体 issue 并失败关闭。跨 revision 稳定能力身份及多对多能力图未实现。
LLM 只能提出引用既有 Evidence ID 与 revision 的语义 Claim，不能自行决定披露、平台、精确语法或清除问题。
系统不使用 `hidden` 披露态；按能力
完全排除将由后续独立 operator exclude policy 在持久化前处理，当前尚无这个接口。确定性适配器已删除词表分流，
非空文本由语义 assessment service 处理；未配置 transport 时统一 abstain，教学注释和 Answer Agent 也不会
启用，但确定性能力索引与插件加载保持可用。语义 assessment 是每轮 `triage` 的正式
默认路径，未配置状态不是一个可切换的词表模式。统一入口决策见
[ADR-0003](../adr/0003-unified-capability-guidance-and-incident-intake.md)，跨平台边界见
[ADR-0006](../adr/0006-cross-platform-alconna-entry-and-reference-providers.md)，当前入口语义见
[ADR-0020](../adr/0020-use-triage-command-for-natural-language-support.md)，当前续问边界见
[ADR-0060](../adr/0060-use-scope-thread-and-post-route-conversation-context.md)，历史显式入口约束见
[ADR-0031](../adr/0031-require-triage-for-support-thread-continuation.md)，被替代的免命令方案见
[ADR-0030](../adr/0030-continue-support-thread-by-exact-reply.md)，能力影子边界见
[ADR-0021](../adr/0021-use-deployment-local-capability-shadow-index.md)，维护者在线检索见
[ADR-0022](../adr/0022-limit-capability-shadow-guidance-to-superusers.md)，状态轴拆分见
[ADR-0032](../adr/0032-separate-capability-audience-analysis-and-platform-status.md)，Matcher 与用户可观察能力的
关系见 [ADR-0034](../adr/0034-distinguish-matchers-from-user-observable-capabilities.md)。

项目已经采纳“面向已鉴权开发者的插件行为探索”方向：开发者以后可以在不打开仓库的情况下询问插件的
能力、触发条件、配置影响、环境约束和当前部署表现。该能力不会把源码当作唯一真值，而是协调运行时注册
结构、源码与配置模型、受部署策略守门的相关配置事实、版本 / revision 和已有运行观察；回答必须区分观察
到的结构或行为、
静态推导与仍然未知，并保留 partial、stale、冲突和 opaque 边界。当前已经实现部署声明 / 制品 / 运行模块
协调、有界 handler/config AST、策略先行的有效配置瞬时投影、一次性语义分析合同和结构化 Agent
客户端；统一支持 router 也已有 `BEHAVIOR_EXPLORATION_CANDIDATE` action，但 Matcher 仍把它当作澄清，
请求者鉴权已经接入，行为取证和解释编排尚未接入 `triage` 产品运行路径，也没有持久语义知识或完整
行为解释卡。长期
产品与安全边界见
[ADR-0025](../adr/0025-explain-plugin-behavior-from-deployment-evidence.md)。

已采纳的投递合同还允许私聊、群聊和频道请求进入同一 `triage` 意图分流，该入口场景边界现已落实。行为探索在针对当前 Bot / Event
完成模型外 `SUPERUSER` 鉴权后，可以把获准披露且已净化的完整解释返回到原始提问会话；系统不再按房间
成员构成增加 allowlist、旁观者鉴权或强制转私聊，由请求者选择合适的会话。这不放宽秘密过滤或远端模型
数据授权，也不自动开放私聊 incident。行为探索分支本身仍尚未实现，详见
[ADR-0028](../adr/0028-allow-private-triage-and-superuser-request-context-replies.md)。

## 核心能力与当前命令入口

下表同时记录插件运行入口和仓库维护命令。`just maintainer <command>` 只在源码仓库可用，等价于
`uv run --group maintainer python -m tools.nbtriage_maintainer <command>`，不是面向插件安装者的稳定公开接口。
[ADR-0016](../adr/0016-keep-maintainer-evaluation-tooling-out-of-install-surface.md) 保留 `nbtriage` 领域核心，
但已将 console script、评测 / 采集 orchestrator 和 MLflow 发布器迁到不进入 wheel 或 sdist 的仓库工具。

| 核心能力或公开入口 | 对外含义与适用场景 | 关键状态或副作用 | 主要实现位置 |
|---|---|---|---|
| `just maintainer discover` | 从带角色与生态证据的活动仓库形成跨仓库均衡待审池 | 启发式分数只排序，不确认根因或 Oracle；完整调研与活动 manifest 分离 | `tools/nbtriage_maintainer/discovery.py`、`evals/datasets/catalog/repositories.json`、`evals/datasets/catalog/repository-catalog.json` |
| `just maintainer collect` | 从候选清单读取公开 GitHub Issue，形成原始快照与策展草稿 | 只读访问 GitHub；写本地忽略目录；已有 Case 默认保留 | `tools/nbtriage_maintainer/cli.py`、`tools/nbtriage_maintainer/collector.py` |
| `just maintainer enrich-*` | 将时间线、关联 PR、PR commits 与直接引用 commit 边界写入隐藏 Gold | 只读 GitHub；REST 时间线与认证 GraphQL `CONNECTED_EVENT` 合并，后者无 Token 时显式记录跳过；不进入 Case 输入 | `tools/nbtriage_maintainer/github.py`、`tools/nbtriage_maintainer/timeline.py` |
| `just maintainer apply-annotations` | 把版本管理的人工判断合并到生成 Case | 只允许修改 `curation` 字段 | `tools/nbtriage_maintainer/curation.py` |
| `just maintainer gate` | 评估 Case 是否具备公共字段和模式特有证据，并核对版本化 Oracle 结果声明的内部一致性 | 生成本地 JSON 报告；不修改 Case；引用、Case / Probe revision 或完整性不一致时失败关闭；当前不执行 Probe，也没有本地执行回执或外部 attestation | `tools/nbtriage_maintainer/gate.py`、`tools/nbtriage_maintainer/runtime_results.py` |
| `just maintainer summarize-trials` | 严格读取当前 trial JSONL 与有界轮转备份，输出无标识的运营窗口摘要 | 按 event ID 去重；损坏、冲突、超长、截断或未知版本只计数；不输出原事件、失败形状或任何 incident / trial / event / cluster ID | `src/nbtriage/live_trials.py`、`tools/nbtriage_maintainer/cli.py` |
| `just maintainer evaluate-b0` | 在冻结 split 上运行固定检查表、规则路由和 train-only 相似 Case 检索 | 预测只读公开 Issue 输入；Gold 只进入评分器；不调用模型或外部工具 | `src/nbtriage/baselines.py`、`tools/nbtriage_maintainer/evaluation.py` |
| `just maintainer evaluate-s3` | 在独立合成 Fixture 上比较冻结 B0 与 B1 模型前安全拒绝 | 不读取真实秘密或生产数据；不检索、不调用模型、不调用外部工具 | `src/nbtriage/safety.py`、`tools/nbtriage_maintainer/safety_evaluation.py` |
| `just maintainer build-bot-docs-index` | 从外部 `bot-docs` 的批准子集构建本地 SQLite FTS5 派生索引 | 不修改源目录或 vendor 独立 Markdown 副本；目标不得位于 `bot-docs` 内；已有索引只在显式 `--replace` 时原子替换 | `tools/nbtriage_maintainer/bot_docs.py`、`tools/nbtriage_maintainer/cli.py` |
| `just maintainer search-bot-docs` | 用 metadata 或 hybrid 策略检索项目事实、工程配方和当前 OneBot API 文档 | 只读本地索引；返回文件哈希、修订、标题和精确版本；不调用网络、模型或工具 | `tools/nbtriage_maintainer/bot_docs.py`、`tools/nbtriage_maintainer/cli.py` |
| `just maintainer search-capabilities` | 检索部署启动后在后台生成的本地能力影子索引 | 默认只返回可服务的 `public`；`--include-unresolved` 纳入带具体分析问题的记录；带外确认授权后可用 `--include-restricted`，该开关不自行鉴权；不调用模型或能力代码 | `src/nbtriage/capabilities.py`、`tools/nbtriage_maintainer/cli.py` |
| `just maintainer evaluate-bot-docs-retrieval` | 在内容 SHA-256 固定的 25 条公开合成问题上比较 metadata 基线与 hybrid 检索 | 固定 Recall@5 / MRR / 来源完整率合同；自定义 Fixture 只能生成 `custom_unqualified` 报告；报告写本地忽略目录；0 模型和外部工具调用 | `tools/nbtriage_maintainer/bot_docs_evaluation.py`、`evals/datasets/fixtures/bot-docs-retrieval-v1.json` |
| `just maintainer evaluate-capability-teaching` | 用精确 OpenCode Go / Prompt / Schema / 隐私 / 预算合同运行教学注释 forward-heldout | 当前 v8 正式 bundle 固定 20 条纯合成 Fixture，其中 12 条先对冻结 Python 源码运行真实 ast-grep 提取器；JSON 或任一源码字节变化都会使资格身份失效；必须显式确认付费与预算，并逐条原子更新 partial audit；v34 / v8 Gate 已通过且不得重跑 | `tools/nbtriage_maintainer/capability_teaching_evaluation.py`、`evals/datasets/fixtures/capability-teaching-v8-forward-heldout.json`、`evals/datasets/fixtures/capability-teaching-v8-sources/` |
| `just maintainer evaluate-b1-openai` | 用 train-only 证据和一次 Responses 原生 JSON Schema 运行 validation 或 heldout | 需要 `openai` extra；必须显式模型、输出 / 调用上限和付费确认；Pydantic AI Direct Request 仍按 Case 串行；请求关闭存储、工具、遥测和自动重试，但不声称零数据保留；响应按完整请求缓存 | `src/nbtriage/rag.py`、`src/nbtriage/model_adapters.py`、`src/nbtriage/openai_adapter.py`、`tools/nbtriage_maintainer/evaluation.py` |
| `just maintainer evaluate-b1-deepseek` | 用 DeepSeek V4 Flash 非思考模式运行同一 B1 契约 | 只接受 `deepseek-v4-flash`；固定 `reasoning=none`、`temperature=0`；使用独立密钥、缓存和报告 | `tools/nbtriage_maintainer/cli.py`、`tools/nbtriage_maintainer/providers.py` |
| `just maintainer evaluate-b3-evidence-policy` | 在 B1 validation 的脱敏策展投影上冻结单步补证策略 | 只接受内容 SHA-256 与 11 条规模均固定的官方 validation-only 投影，并复核字段白名单、枚举和 Case 身份；不调用模型或工具；内容替换、held-out、Provider 元数据及未知字段被拒绝 | `tools/nbtriage_maintainer/evidence_policy.py`、`tools/nbtriage_maintainer/evidence_policy_evaluation.py` |
| `just maintainer evaluate-b3-evidence-receipts` | 在纯合成 Fixture 上验证结构化回执守门和请求绑定 | 正式 Gate 绑定冻结 Fixture 的原始 SHA-256、集合 ID 和 16 条 Case；自定义内容仅生成 `custom_unqualified` 报告且 CLI 非零退出；只评估白名单 schema、脱敏、疑似 secret 与错绑，不判断证据真伪；0 模型 / 工具调用 | `src/nbtriage/evidence_receipts.py`、`tools/nbtriage_maintainer/evidence_receipt_evaluation.py` |
| `just maintainer export-answer-quality-review` | 把完整真实 B4 报告中 `forward_hidden` 的完成态候选导出为本地人工评审包 | 只接受 schema v3、纯合成、真实模型多 trial B4 报告并核对 Fixture/split 哈希；只复制领域层规范化证据事实，不复制 Gold、Prompt、消息历史、原始日志或 Provider 响应；输出拒绝覆盖 | `tools/nbtriage_maintainer/answer_review_export.py`、`tools/nbtriage_maintainer/agent_evaluation.py` |
| `just maintainer evaluate-answer-quality` | 用四轴 0–2 人工 rubric 汇总固定 `answer + citations` 标注 | 默认合成校准只验证评分锚点；候选质量必须来自真实 B4 的 `forward_hidden` 多 trial 报告、使用独立人工复核，并同时通过来源 B4 Gate、均值、逐样本和关键零分硬门；结果只属于 `offline_fixed_fixture`，不构成生产质量证据；非校准报告拒绝覆盖 | `tools/nbtriage_maintainer/answer_quality_evaluation.py`、`evals/rubrics/answer-quality-v1.json`、`evals/datasets/fixtures/answer-quality-calibration-v1.json` |
| `just maintainer evaluate-b4-scripted` | 用 scripted model 在冻结 regression / forward-hidden split 上验证动态 action、预算、暂停恢复、轨迹评分和 Gold 隔离 | 0 真实 Provider 请求、0 外部工具调用；报告记录 Prompt/schema/policy/source revision 与结构化输出通过率，但明确不具备晋级资格 | `src/nbtriage/bounded_agent.py`、`tools/nbtriage_maintainer/agent_evaluation.py`、`evals/datasets/fixtures/b4-bounded-agent-v1.json`、`evals/datasets/splits/b4-gate-v1.json` |
| `just maintainer evaluate-b4-real` | 在明确付费/出站授权后，让同一 Provider/model 多 trial 对照 B1、B3 与 B4 | 支持 DeepSeek / OpenAI Responses 与 Anthropic Messages；只用 forward-hidden 指标判断晋级；B1/B4 后验结构拒绝计入 trial，未知费用仍中止；每次请求前/响应后更新 partial audit，success/partial 路径禁止覆盖；仍无完整质量报告或 Provider 资格 | `tools/nbtriage_maintainer/agent_evaluation.py`、`tools/nbtriage_maintainer/cli.py`、`evals/datasets/splits/b4-gate-v1.json` |
| `just maintainer publish-evaluation-mlflow` | 维护者把已经落盘的评测 JSON 发布到显式 MLflow experiment 以比较迭代；不属于插件安装接口 | MLflow 只持有按内容摘要幂等的查询副本，不重新执行评测；默认只接受小型正式评测 ID 白名单，显式允许的自定义或未知工件统一标为不可比较；真实 B4 成功报告还必须配对完成态同名 audit 且来源摘要一致；默认写本机 `127.0.0.1` | `tools/nbtriage_maintainer/mlflow_tracking.py`、`tools/nbtriage_maintainer/cli.py`、`docs/adr/0016-keep-maintainer-evaluation-tooling-out-of-install-surface.md` |
| `just maintainer session-*` | 从冻结 B1 预测创建、接收脱敏回执、审批、关联已有 Oracle 结果并查看支持会话 | `needs_evidence` 只接收当前槽位并从剩余候选重规划；`verify` 未显式审批不能附加结果；不执行代码或外部写入 | `tools/nbtriage_maintainer/sessions.py`、`tools/nbtriage_maintainer/cli.py` |
| `RuntimeObservation` / `RuntimeObservationBuffer` | 接收 NoneBot 观察桥提交的最小化事件、Matcher、插件、API 与异常标识，并按关联 ID 生成证据包 | 不接收消息正文、用户 / 群 ID、API 参数或结果；容量与 TTL 必须由调用方显式给出；仅单进程内存 | `src/nbtriage/runtime_observations.py` |
| `NoneBotRuntimeObserver` | 显式注册 NoneBot 2.5 公共 hook，用事件 state 关联 event、实际 Matcher 与其内部 API 生命周期 | fail-open；只读取框架 / 插件标识和异常类 / 栈模块；Matcher 外 API 不猜测归属 | `src/nonebot_plugin_triage/nonebot_runtime.py` |
| `UniversalReferenceBridge` / `PlatformMessageReferenceIndex` | 通过 UniSeg Target / message ID 统一绑定入站消息，并以带密钥摘要短期关联 correlation ID | 原始适配器 / Bot / 会话 / 消息 ID 只瞬时参与 HMAC；不保存正文；显式容量与 TTL | `src/nonebot_plugin_triage/universal_references.py`、`src/nbtriage/message_references.py` |
| OneBot V11 outgoing reference Provider | 从 Matcher 内成功的群发送结果补齐运行证据 correlation | OneBot 是可选依赖；只读路由字段和 message ID；不结算 Thread，不保存完整 API data / result 或被回复正文 | `src/nonebot_plugin_triage/onebot_v11_references.py` |
| `SupportThreadRecord` / scope Turn coordinator | 以 HMAC scope 保存首轮有界 request / Reply / correlation，并只允许下一条同 scope 显式 `triage` 补充一次 | 单进程、有界、TTL 后逻辑失效并在下一次协调器操作时惰性清理；HMAC 绑定 adapter、Bot、场景和 actor；Reply 与 Receipt 不选择 Thread；只有发送成功才等待补充，第二轮、终局 action、异常或发送失败都关闭；不跨重启 | `src/nbtriage/support_threads.py`、`src/nonebot_plugin_triage/thread_references.py`、`src/nonebot_plugin_triage/support_responses.py` |
| Alconna `triage` Matcher / support intake adapter | 每轮以必选指令接收自由文本；先 Claim scope Thread，再让 Semantic 只判断当前文字，路由后才投影 Thread / Reply 上下文 | Alconna / UniSeg 提供命令、Reply / Target 和发送抽象；scope lease 判断归属、TTL 与并发；私聊、群聊和频道统一鉴权；Reply message ID 只作独立运行 correlation | `src/nonebot_plugin_triage/handlers.py`、`src/nonebot_plugin_triage/support_intake.py` |
| `SupportAssessmentRequest` / `SupportSemanticAssessment` | 冻结语义 assessment v7 的最小请求投影和受限多标签输出 | 请求闭合为版本号与当前单条规范化文字；输出只包含 guidance、behavior exploration、Bug 判定、feature feedback 与独立 observation，或澄清 / unsupported；不包含 action、回答或副作用授权 | `src/nbtriage/support_semantics.py` |
| semantic Agent output client / assessment service / support router | 直接以 `SupportSemanticAssessment` 作为 Pydantic AI Agent `output_type`；把秘密、超时、传输失败和非法输出收敛为 abstain，再映射为唯一 action | 中文 Prompt v5；payload 只有当前单条规范化文字的闭合请求投影；模型不产生 action 或授权；40 条独立 Gate 的 schema / status / exact 均为 1.000，评测集合只记录精确 OpenCode Go 组合，其他组合可运行但标记未验证 | `src/nbtriage/opencode_go_semantic_adapter.py`、`src/nbtriage/support_semantic_model_adapter.py`、`src/nonebot_plugin_triage/semantic_runtime.py`、`src/nonebot_plugin_triage/semantic_assessment.py`、`src/nbtriage/support_routing.py` |
| Bug assessment coordinator / bounded Agent | 先精确复用已审核 LocalStore verdict，再预加载公开合同、Thread 与直接 Reply；仍未解决时动态选择聊天、运行、日志、源码、设计和部署证据，最后确定性形成三值结论 | OneBot 群历史由当前 Bot / 群模型外绑定并一次读取最新最多 30 条，精确 Reply 独立预装；没有历史 Provider 时不暴露聊天工具，也不使用本地滚动窗口；9 请求 / 1 次独立聊天 / 6 次通用证据；聊天正文、必要 ID 与角色不遮蔽，源码 / 日志仍清理；模型候选不能建 incident 或披露内部证据。中文 Prompt v8 的 16 条独立 forward-heldout 全部门为 1.000，已进入精确资格集合 | `src/nbtriage/bug_assessment.py`、`src/nbtriage/bug_agent.py`、`src/nbtriage/bug_conversation.py`、`src/nbtriage/bug_logs.py`、`src/nbtriage/bug_source.py`、`src/nbtriage/bug_design.py`、`src/nonebot_plugin_triage/bug_assessment_runtime.py`、`src/nonebot_plugin_triage/onebot_bug_conversation.py` |
| public capability Answer Agent | router 选择 guidance 后，把 public runtime 事实、经校验的教学注释与路由后有界 Thread / Reply 上下文交给第二个 Pydantic AI Agent | 教学注释不会直接绕过 Answer Agent；上下文只能消歧，不能覆盖事实或权限；无工具、单请求、零 retry；未知引用、非法输出或 transport 失败退回确定性模板；v2 两条真实 smoke 通过，尚无 held-out | `src/nbtriage/public_guidance.py`、`src/nbtriage/public_guidance_model_adapter.py`、`src/nonebot_plugin_triage/capability_shadow.py`、`src/nonebot_plugin_triage/public_guidance.py`、`src/nonebot_plugin_triage/public_guidance_runtime.py`、`src/nonebot_plugin_triage/handlers.py` |
| `LiveReportService`（兼容层） | 只在持有显式签发、绑定精确 decision 与当前 `LiveReportRequest` 的建单授权时建立最小 `LiveIncident`；v7 当前入口不签发 | 原子消费授权后检查场景并再次解析同一 Reply；只有运行失败复核通过才生成编号和写状态；无 Reply、引用未命中、成功或空回执失败关闭；无模型、网络、Probe 或外部写入 | `src/nonebot_plugin_triage/live_reports.py` |
| `NBTriageConfig` / `ConfigValuePolicy` / capability analysis | 配置精确 transport 身份和预算，无产品启用开关；把 runtime 命令结构、ast-grep Matcher 结构、已加载源码和当前内存配置投影装配成首个 Evidence Pack，必要时允许 Agent 用共享只读 FileSystem / Jedi 补证 | backend/model 必须成对；semantic、Bug、public guidance 与 capability annotation 分任务记录质量，评测结果不能相互继承但也不阻止未验证模型运行；中文 capability annotation v34 已以全新 v8 forward-heldout 取得精确质量标签，v33 / v7 及更早结果保持冻结历史证据；`.env*`、凭据、数据库、教学日志、人工帮助和评测 Gold 不可读 | `src/nonebot_plugin_triage/config.py`、`src/nonebot_plugin_triage/config_policy.py`、`src/nonebot_plugin_triage/capability_analysis_adapter.py`、`src/nonebot_plugin_triage/capability_analysis_tools.py`、`src/nonebot_plugin_triage/runtime_config_evidence.py`、`src/nbtriage/readonly_tools/`、`src/nbtriage/capability_model_adapter.py` |
| `IncidentQueryService` / Alconna query Matcher（兼容层） | 让维护者按不透明受理编号查看已有短期白名单摘要 | `SUPERUSER` 在读取前守门；v7 不再新增 incident；cluster 只基于最小失败标识，不返回聊天、平台身份、correlation ID、API 参数或任意日志 | `src/nbtriage/live_incidents.py`、`src/nbtriage/incident_queries.py`、`src/nonebot_plugin_triage/incident_queries.py`、`src/nonebot_plugin_triage/handlers.py` |
| `LiveTrialService` / trial Matchers（兼容层） | 为已授权 incident 建立 observation-only trial；当前 live semantic 不产生入口 | 默认 off；observe 必须有本地轮转 JSONL sink；只保存最小失败形状和计数；反馈 / 统计要求 `SUPERUSER`；零模型 / 工具 / 外部写入 | `src/nbtriage/live_trials.py`、`src/nonebot_plugin_triage/trials.py`、`src/nonebot_plugin_triage/live_reports.py`、`src/nonebot_plugin_triage/handlers.py` |
| `build_reply_report_signals` / `build_unlinked_report_signals`（兼容层） | 把 Reply 回执或无证据报告转换为旧确定性入口信号 | v7 router 不消费这些信号；旧服务仍要求明确失败才能获得 incident 授权，不调用模型 | `src/nbtriage/reply_reports.py` |
| `parse_intake_signals` / `route_intake`（兼容层） | 把旧受信结构信号分流为教学、纠错、疑似故障、无关或危险 | 当前 live `triage` 使用 semantic v7 router；此层不接收文本、命令原文或身份，也不调用模型或工具 | `src/nbtriage/intake.py` |
| 公开能力 Provider / 部署本地能力影子 | 普通用户解释显式 Provider 或自动确定公开的当前 adapter 能力；维护者 CLI 可显式检索已加载 Alconna、普通 Matcher、被动能力与插件来源形成的影子候选 | 普通查询在 SQL 召回前限定当前 adapter 的 public，partial / stale 与 blocking issue 均失败关闭；聊天内部问题等待 behavior exploration 取证编排；不推断跨 Matcher 角色，不重跑 `parse()`、Rule、Permission 或 handler | `src/nonebot_plugin_triage/support_intake.py`、`src/nonebot_plugin_triage/capability_shadow.py`、`src/nonebot_plugin_triage/capability_snapshot.py`、`src/nbtriage/capabilities.py` |
| `SupportCase` schema v1 | 冻结打开时输入边界，分离当前 API 快照中的后续材料 | Issue 正文可能被事后编辑，必须保留时间完整性限制 | `tools/nbtriage_maintainer/models.py` |

## 逻辑组件与依赖方向

```text
repository manifest → discovery prefilter → manual batch manifest
                                               ↓
maintainer CLI → GitHub read-only client → raw input + hidden Gold → PR/commit refs
                                               ↓               ↓
                                    versioned annotation ─→ generated Case
                                                               ├──→ Data Gate report
versioned Oracle result ────────────────────────────────────────┘

frozen B1 report → single-evidence policy → support session → explicit approval → validated Oracle result
                                               ├─→ redacted receipt → remaining-candidate replan
                                               └─→ ordered audit events and local state

synthetic Case + approved evidence → bounded AgentRunState → one deferred native tool call
                                           ├─→ normalized read-only observation → next bounded step
                                           ├─→ evidence interruption → exact receipt resume
                                           └─→ strict diagnosis or stable stop reason

NoneBot public hooks → event-state correlation → runtime observer → bounded buffer
                 ├─→ UniSeg incoming Target / message ID ─┐
                 └─→ adapter outgoing Provider ───────────┴─→ runtime reference index

[optional @Bot] triage + free text → scope Thread Claim → semantic assessment(current text only)
                 ├─→ unqualified / request failure → abstain → await at most one supplement
                 ├─→ guidance → public facts + post-route Thread / Reply context → Answer Agent
                 │                                                        └─→ deterministic fallback
                 └─→ bug / observation → exact reviewed catalog ──────────────────────┐
                                      └─→ Reply + bounded conversation/runtime/log/    │
                                          source/design/deployment Agent → reconciler ─┴─→ safe three-way reply
scope Thread → first unresolved response sent → next explicit triage consumes one supplement → close
behavior exploration ─→ SUPERUSER check ─→ future restricted evidence orchestration
regular capability query ─→ current-adapter public ID domain ─→ SQL FTS ───────→ UniMessage guidance
SUPERUSER query Matcher ─→ exact incident ID ─→ whitelisted IncidentSummary ─────→ UniMessage receipt
                 └─→ observe trial → local rotating JSONL + summary_viewed event
SUPERUSER feedback/stats ─→ enum feedback / active aggregate ─────────────────────→ UniMessage receipt

triage request text → semantic assessment → trusted minimal signals → deterministic router
                         ├─→ guidance / behavior / bug-assessment candidate / clarify / refuse
                         └─→ reported observation → bug-assessment candidate

explicit public Alconna provider ─┐
public capability shadow + teaching annotation ─→ bounded public facts → Answer Agent / deterministic fallback
registered Alconna AST → repository-only rich capability snapshot
existing Arparma ─────→ minimal parse receipt ─────────→ trusted command_status

standard pyproject → declared inventory → artifact revision ─┐
loaded module names ─────────────────────────────────────────┴─→ deployment reconciliation status

构建期 Plugin / Matcher / Rule facts → deterministic capability record → ServingView
动态或被动入口且展示字段不足 ───────────────────────────────→ blocking issue

current runtime capability record → bounded handler/config EvidenceUnit
                                   → runtime grammar + ast-grep Matcher Evidence Pack
                                   → policy-first runtime config projection
                                   → bounded FileSystem/Jedi Agent → revision-bound annotation cache
```

| 逻辑组件 | 职责与边界 | 依赖方向或主要协作 | 拥有的数据或状态 | 主要实现位置 |
|---|---|---|---|---|
| Maintainer CLI | 参数解析、错误呈现和命令编排；不进入发行包 | 调用 Collector 与 Gate | 无长期状态 | `tools/nbtriage_maintainer/cli.py` |
| GitHub client | 解析 Issue URL、只读 REST 请求、串行翻页，并在认证时补查 GraphQL `CONNECTED_EVENT` | 只依赖 Python 标准库与 GitHub API | 不缓存 Token；匿名模式不伪称 connected PR 查询完整 | `tools/nbtriage_maintainer/github.py` |
| Discovery | 对关闭 Issue 做规则预筛、解释性评分与跨仓库均衡 | 读取仓库清单，调用 GitHub client | 本地发现报告 | `tools/nbtriage_maintainer/discovery.py` |
| Collector | 来源快照规范化、哈希、输入 / Gold 隔离 | 读取 manifest，调用 GitHub client | 本地生成工件 | `tools/nbtriage_maintainer/collector.py` |
| Timeline enrichment | 保存时间线、合并 REST cross-reference 与 GraphQL connected PR、提交序列和回归边界候选 | 只修改隐藏 Gold | 本地 Gold；记录 connected lookup 是否完整 | `tools/nbtriage_maintainer/timeline.py` |
| Curation | 应用或导出人工 annotation | 只能修改 Case 的 `curation` | 版本化 annotation | `tools/nbtriage_maintainer/curation.py` |
| Case model | 定义可编辑策展字段与序列化边界 | 被 Collector 和 Gate 使用 | Case schema v1 | `tools/nbtriage_maintainer/models.py` |
| Data Gate | 按执行模式计算缺失字段和就绪类别 | 只读 Case JSON | 报告 schema v1 | `tools/nbtriage_maintainer/gate.py` |
| Runtime result validator | 核对声明状态、Case / Oracle 规范化版本、Probe 原始字节 SHA-256、故障 / 修复引用和两侧自述命中 | 只读 schema v2 结果；Probe 必须在显式受信根内；不启动进程，也无法证明 Probe 曾执行或输出来自目标 ref | 内容绑定且内部一致的历史 Oracle 声明；不是本地执行回执或可信 attestation | `tools/nbtriage_maintainer/runtime_results.py` |
| B0 predictor | 抽取版本值和证据状态，给出固定补问、症状 / 阶段 / 责任层与路由 | 只读 `source` 和仓库身份；train-only 检索；不接触 `curation` | 无长期状态 | `src/nbtriage/baselines.py` |
| Evaluation harness | 加载冻结 split、隔离预测与 Gold、计算分层指标并写报告；不进入发行包 | 不修改 Case；历史 S3 无分母时不伪造样本，改由独立合成评测补充 | 评测报告 schema v1 | `tools/nbtriage_maintainer/evaluation.py`、`tools/nbtriage_maintainer/safety_evaluation.py` |
| Safety pre-model guard | 识别目标 Case 中明确请求越过凭据、控制面、生产、账号、私密数据或外部写入边界的组合 | 只读公开 `source`；命中后不检索、不读缓存、不调用模型；不能替代副作用入口授权 | 风险类别与拒绝预测 | `src/nbtriage/safety.py`、`src/nbtriage/rag.py` |
| B1 RAG-only runner | 生成有界目标输入和 train-only 证据包，异步校验版本 / 枚举 / 引用并缓存响应 | 检测到疑似秘密时在模型前停止；非法输出不写缓存；不暴露工具；不拥有事件循环 | 本地忽略的响应缓存 | `src/nbtriage/rag.py` |
| bot-docs local retriever | 对批准的 facts / recipes / OneBot Adapter 2.4.6 API 文档做标题感知分块、全文检索和逐文件结果去重 | 源文档归外部 `bot-docs` 所有；legacy NapCat / NoneBot2 不进入索引；当前不被 B1、B4 或 NoneBot 入口调用，基础发行包不携带索引 | 本地忽略的 SQLite 索引与评测报告；未来产品知识包独立版本化 | `tools/nbtriage_maintainer/bot_docs.py`、`tools/nbtriage_maintainer/bot_docs_evaluation.py`、[ADR-0019](../adr/0019-distribute-rag-corpus-as-versioned-knowledge-pack.md) |
| Pydantic AI 公共控制层 / OpenAI adapter | 基础 wheel 提供 Agent、结构化输出、Harness 与 Jedi；OpenAI adapter 再把通用 B1 请求映射为 Responses 原生 JSON Schema | 公共控制层不会启用模型或网络；OpenAI SDK 只由 `openai` extra 安装并延迟导入；三类 tools 为空、instrumentation 与存储关闭；API Key 只读环境，不外推为零数据保留 | 无长期状态 | `src/nbtriage/model_contracts.py`、`src/nbtriage/model_adapters.py`、`src/nbtriage/openai_adapter.py`、`src/nbtriage/readonly_tools/` |
| Anthropic Messages adapter | 用同一 B1 请求与输出契约映射官方 Messages native `output_config.format`，验证领域层不依赖 Responses 专属语义 | 只由 `anthropic` extra 安装；SDK 重试为零，无自定义 endpoint、tools、fallback、CLI 或插件触发；离线通过只标记实验性 | 无长期状态 | `src/nbtriage/anthropic_adapter.py`、`tests/test_model_adapters.py` |
| NoneBot model runtime boundary | 把公开配置解析为实际 `(provider, model, API family)` 和惰性 Provider client factory，并附加已验证或未验证质量标签 | runtime 持有 factory 而非长期累计调用客户端；未配置 transport 时不解析密钥或导入 Provider SDK；Provider 任务合同与实现分离，缺少 Provider extra 不影响插件导入 | 无长期状态；API Key 只存在于进程闭包与新建 SDK 客户端 | `src/nonebot_plugin_triage/task_model_runtime.py`、`src/nonebot_plugin_triage/model_runtime.py`、`src/nonebot_plugin_triage/config.py` |
| DeepSeek Responses adapters | 历史直接 SDK 维护命令保留冻结 B1 基线；专用 Pydantic factory 为真实 B4 harness 同时提供 B1 native JSON Schema 与 B4 deferred tool step | 仓库 `maintainer` group 固定官方 endpoint、显式 `DeepSeekProvider`、`deepseek-v4-flash`、`reasoning=none`、`temperature=0` 和零 SDK retry；没有插件 extra；滚动别名未获线上资格；Provider `strict=false` 时仍做本地双层参数验证 | 无长期状态 | `tools/nbtriage_maintainer/providers.py`、`tools/nbtriage_maintainer/deepseek_adapter.py`、`tools/nbtriage_maintainer/cli.py` |
| OpenCode Go evaluation test fixture | 用兼容 Chat 的假 HTTP spike 验证 renderer、请求次数、身份与 cache usage；真实模型证据从单工具 smoke 扩展到四工具多调用反例和单一 typed action 信封的两步 control | 只服务测试；信封 control 不覆盖首次约 388.7 秒的未知历史，也不进入 wheel、公开 extra、CLI、插件配置、正式 Gate backend 或 Provider 资格 | 无长期状态；历史机器记录仅在维护者本地保留 | `tests/support/opencode_go_backend.py`、`tests/test_agent_provider_adapters.py` |
| OpenCode Go semantic adapter / task runtime | 以 Chat Completions 的 required 单一 output tool 返回 `SupportSemanticAssessment v7` | 中文 `support-semantic-v7-prompt-v5-zh`；payload 只有当前规范化文字；40 条独立 forward-heldout 全量精确命中，资格集合只含该 Prompt / Fixture / 策略 / 预算 / evaluation revision | 无长期状态；密钥只在进程环境与惰性客户端闭包中 | `src/nbtriage/opencode_go_semantic_adapter.py`、`src/nonebot_plugin_triage/semantic_runtime.py`、[ADR-0046](../adr/0046-merge-internal-reasoning-into-behavior-exploration.md)、[ADR-0042](../adr/0042-use-pydantic-ai-model-profile-for-structured-output.md) |
| OpenCode Go Bug assessment adapter / task runtime | 用 Pydantic AI Agent 原生 output_type 与只读 Tools 产生 `BugAssessmentCandidate`，再由本地 reconciler 形成三值 verdict | 中文 `bug-assessment-agent-v1-prompt-v8-zh`；会话、运行、日志、源码、设计与部署工具；120 秒 / 800 output token、9 请求 / 1 次聊天 + 6 次通用证据 / 120k token / 0.50 美元；16 条独立 forward-heldout 全部门为 1.000，资格集合只含该精确组合 | reviewed catalog 位于 LocalStore data；评测 trajectory 只写被忽略的本地 reports；线上聊天不持久化；密钥只在进程环境与惰性客户端闭包中 | `src/nbtriage/bug_agent.py`、`src/nbtriage/bug_assessment.py`、`src/nbtriage/bug_conversation.py`、`src/nonebot_plugin_triage/bug_assessment_runtime.py`、[ADR-0050](../adr/0050-use-a-bounded-agent-for-user-bug-assessment.md)、[ADR-0053](../adr/0053-allow-relevant-source-and-log-bodies-for-bug-assessment.md)、[ADR-0060](../adr/0060-use-scope-thread-and-post-route-conversation-context.md)、[ADR-0061](../adr/0061-read-latest-bounded-conversation-window-for-bug-assessment.md) |
| Provider response usage / identity | 从 Pydantic AI 响应提取 Provider、model、request ID 与可选 fingerprint，并按返回身份归一化 microUSD | 返回 Provider 不匹配或模型漂移时不回退请求侧价格；身份缺失可记录但真实 Gate 不得晋级 | 无长期状态 | `src/nbtriage/model_usage.py`、`src/nbtriage/model_adapters.py`、`src/nbtriage/pydantic_agent_adapter.py` |
| Evidence request policy | 按故障阶段把 B1 多槽位候选收缩为当前轮唯一问题 | 只用于维护者离线评测与会话；只能选择模型候选；空候选失败；validation 冻结后等待前向隐藏集 | validation 策略工件 | `tools/nbtriage_maintainer/evidence_policy.py`、`tools/nbtriage_maintainer/evidence_policy_evaluation.py` |
| Evidence receipt contract | 把九类补证限制为已脱敏、字段白名单化的结构摘要和原始材料指纹；schema v2 以域分隔规范摘要绑定 receipt / session / Case / slot、原始材料指纹、字节数与规范化 facts | 拒绝任意额外字段、疑似 secret、错绑、不完整摘要和版本错配；不读取原始材料；`receipt_revision` 是内容地址而非签名，不能证明 facts 真实来自指纹所指材料 | 合成 Fixture 与冻结守门报告 | `src/nbtriage/evidence_receipts.py`、`tools/nbtriage_maintainer/evidence_receipt_evaluation.py` |
| Answer review exporter / rubric evaluator | B4 schema v3 先保留完成态 `answer + citations` 和白名单化 review context；导出器再把真实多 trial 的 `forward_hidden` 候选转换为待人工评分的固定集，评分器按 groundedness、completeness、limitation awareness 和 overclaim control 四轴汇总 | Gold 只在模型运行后生成评审要点；标注 schema v3 绑定整个 Fixture 与冻结 rubric；候选来源要求真实 B4 报告、同名 completed partial audit，以及一致的 evaluation contract、Fixture/split ID 与摘要。该校验防止明显错绑，不重演账本、trial 指标或 promotion gate，也不是本地文件防篡改签名或 Provider 身份证明 | 版本化 rubric、合成校准 Fixture与校准标注；候选评审包和完整报告写入本地 `artifacts/` 或显式 MLflow | `tools/nbtriage_maintainer/answer_review_export.py`、`tools/nbtriage_maintainer/answer_quality_evaluation.py`、`evals/rubrics/answer-quality-v1.json`、`evals/curation/answer-quality/calibration-v1.json` |
| Support session control plane | 把 B1 route 映射为固定动作，约束回执、重规划、审批与结果附加的合法状态变化 | 读取冻结报告、合格回执和 Runtime validator 结论；不读取 Issue 指令执行工具 | schema v4 本地会话 JSON、预测报告哈希、带 `receipt_revision` 的脱敏回执摘要、action result 与顺序事件；旧 schema 失败关闭 | `tools/nbtriage_maintainer/sessions.py` |
| B4 bounded Agent runtime | 拥有循环、按 capability / 已观察轨迹收缩 action 白名单、参数二次校验、跨步预算、observation 执行、暂停恢复和稳定停止原因 | 只读取既有 `RuntimeEvidenceBundle`、train-only retriever 与精确绑定的脱敏回执；不导入 Provider、Pydantic AI 或 NoneBot 类型 | schema v2 `AgentRunState`：结构化 action、可重算 `receipt_revision` 的规范化 observation、摘要、引用、usage、outcome 与可选脱敏终态失败分类；旧 state 失败关闭 | `src/nbtriage/bounded_agent.py` |
| Pydantic AI Agent step adapter | 把本步允许 action 与 citation 约束映射为唯一 `propose_action` 原生工具信封，并把唯一调用 deferred 给领域层 | 每步一个临时 Agent；`retries=0`、一次请求、最多一个调用；hard timeout 取 client timeout 与领域剩余 deadline 的较小值，零剩余值不耗 call slot，`TimeoutError` 交给 runner 映射 `DEADLINE`；不执行项目工具、不持久化框架历史；DeepSeek Responses 依赖 Pydantic + 领域本地复核；Provider 响应后的框架错误通过 `capture_run_messages()` 保留 usage / identity | 无长期状态 | `src/nbtriage/pydantic_agent_adapter.py`、`tools/nbtriage_maintainer/deepseek_adapter.py`、`src/nbtriage/openai_adapter.py`、`src/nbtriage/anthropic_adapter.py` |
| B4 evaluation harness | 用不泄漏 Gold 的 staged evidence Fixture 统计 trajectory、usage、安全和晋级条件；真实模式在每个 trial 重跑同模型 B1、从该结果计算 B3，再运行 B4；独立 `b4-real-partial` 保存授权、进度、请求 attempt、账本与失败 code/stage | scripted 模式不得晋级；真实模式显式确认理论请求/token/cost/deadline/whole-run 上限；请求前原子 checkpoint，响应后记账或保留稳定 unknown reason；Provider 错误只保存类别与可选 HTTP status；success/partial 路径禁止覆盖 | scripted 报告已冻结；DeepSeek run-1/run-2/run-3 中止证据保留，run-3 已验证 partial v1，当前 v3 尚无线上工件和完整正式报告 | `src/nbtriage/provider_failures.py`、`tools/nbtriage_maintainer/agent_evaluation.py`、`tools/nbtriage_maintainer/cli.py` |
| Runtime observation core | 校验传输无关的事件 / Matcher / API 生命周期摘要并按关联标识形成证据包 | NoneBot 适配器只能提交白名单标识；核心不导入框架类型；不调用模型、网络或外部工具 | 显式容量与 TTL 的单进程内存缓冲、累计丢弃计数 | `src/nbtriage/runtime_observations.py` |
| NoneBot runtime observer | 把事件 state 传播的关联 ID 与公共 event、run、API hook 压缩为核心观察 | 显式注册、采集错误 fail-open；不读取 Event 内容、身份、API data / result；不关联 Matcher 外 API | 观察器本地丢弃计数；观察本身进入核心 buffer | `src/nonebot_plugin_triage/nonebot_runtime.py` |
| Platform message reference index | 用 HMAC 精确绑定适配器、Bot、会话和消息引用 | 原始 scope / 引用只瞬时参与摘要；显式密钥、容量与 TTL；不持久化 | 摘要到 correlation ID 的单进程有界索引与丢弃计数 | `src/nbtriage/message_references.py` |
| Universal reference bridge | 用 UniSeg exporter 从任意受支持入站事件提取 Target 与 message ID | 不导入适配器事件类型；Target source 不进入稳定 scope；显式注册、fail-open | 桥本地丢弃计数；映射进入通用引用索引 | `src/nonebot_plugin_triage/universal_references.py` |
| OneBot V11 outgoing reference provider | 从 Matcher 内成功群发送结果提取运行证据 message ID | OneBot Adapter 由宿主安装注册，不是插件依赖或 extra；只在模块存在时延迟加载；缺失时仅停用此增强，不读取被回复正文或执行外部查询 | Provider 本地丢弃计数；映射只进入通用运行证据引用索引 | `src/nonebot_plugin_triage/onebot_v11_references.py` |
| Support Thread store / scope Turn coordinator | 保存首轮有界 request / Reply / correlation；同 scope 下一条显式 `triage` 原子消费唯一补充，让一个 scope 同时只有一个处理轮 | 有界内存、idle / absolute TTL；HMAC 绑定 adapter、Bot、场景和 actor；不依赖 Reply / Receipt，第二轮或失败关闭，不跨重启 | `SupportThreadInitialContext`、scope lease 与 HMAC scope 索引；不保存邻近历史或原始平台身份 | `src/nbtriage/support_threads.py`、`src/nonebot_plugin_triage/thread_references.py`、`src/nonebot_plugin_triage/support_responses.py` |
| Alconna triage entry | 每轮接收必选 `triage` 后的当前自由文本；Reply 可选，路由后才作为 Guidance / Bug 上下文 | Semantic 只看当前文字；scope Thread 最多一次补充；Reply ID 独立关联 runtime。所有轮次先过轻量 HMAC 限流；模型未配置或 transport / schema / Evidence 校验失败时安全降级 | 短期 Thread、本地公开能力 Provider、维护者影子视图和保留的 Incident 兼容服务 | `src/nonebot_plugin_triage/handlers.py`、`src/nonebot_plugin_triage/support_intake.py`、`src/nonebot_plugin_triage/live_reports.py` |
| Deployment-local capability shadow | 启动钩子后台生成字段级 Claim、Evidence、Constraint 和本地 FTS5 索引；每个已观察命令或 Matcher 保持为独立记录 | 默认启用；导入期不解析路径，扫描与构建在线程中执行；首次可服务 generation 发布前普通用户回退显式 Provider；普通用户只读派生 ServingView，SUPERUSER 鉴权后可定向检索未解决或受限记录；不做 handler 效果、跨 Matcher 角色或逐记录源码清单推断；LocalStore 解析或刷新失败保留上一索引或降级，普通视图另行拒绝 partial / stale | LocalStore 插件 cache 中可删除重建的 SQLite 派生数据与内存构建状态 | `src/nonebot_plugin_triage/capability_snapshot.py`、`src/nonebot_plugin_triage/capability_shadow.py`、`src/nbtriage/capabilities.py` |
| Maintainer incident query | 在 NoneBot `SUPERUSER` 权限通过后，按编号读取固定字段摘要与活动 cluster 计数 | 普通成员在读取前被拒绝；无任意时间范围、日志导出或原始事件读取 | 读取现有 LiveIncident 与同缓冲内 cluster；不创建持久状态 | `src/nbtriage/live_incidents.py`、`src/nbtriage/incident_queries.py`、`src/nonebot_plugin_triage/incident_queries.py`、`src/nonebot_plugin_triage/handlers.py` |
| Observation-first trial | 在 incident 已受理后建立 `intake-v1` trial，追加 started / summary_viewed / feedback 事件并提供活动与离线窗口统计 | 默认 off；observe 需要审计 sink；失败写入 fail-open 且计数；只接受枚举反馈；离线汇总严格校验后只返回无标识聚合；不调用模型或工具 | 短期有界 trial 状态；本地单进程轮转 JSONL；trial / event / incident 不透明 ID 与最小失败形状；脱敏窗口摘要 | `src/nbtriage/live_trials.py`、`tools/nbtriage_maintainer/cli.py`、`src/nonebot_plugin_triage/trials.py`、`src/nonebot_plugin_triage/live_reports.py`、`src/nonebot_plugin_triage/handlers.py` |
| Authorized-report adapter（兼容层） | 根据已持有的一次性建单授权和匹配 correlation 的运行 bundle 构造确定性故障信号 | v7 已删除 `incident_intake`，当前 live router / handler 不签发授权；类型和服务仅供旧领域测试与未来显式重开 | 无长期状态 | `src/nbtriage/reply_reports.py` |
| Secondary incident intake router | 在已授权报障服务内部，把结构化运行信号映射为 incident 组合 disposition | 不读取文本，也不承担 live `triage` 的语义理解；模型信号、固定文字或未验证现象不能直接进入 | 无长期状态；只返回 disposition、固定动作、原因与补问标记 | `src/nbtriage/intake.py` |
| Public capability provider / Alconna experiment | 运行时说明显式登记的公开能力；仓库实验读取丰富 AST 并适配已有 `Arparma` | 未登记、`CommandMeta.hide=True`、停用或不可见能力失败关闭；不重跑 `parse()`；丰富实验不进入 wheel/sdist | 进程内 Provider 注册；仓库测试内快照与无原文回执 | `src/nonebot_plugin_triage/support_intake.py`、`tools/nbtriage_maintainer/alconna_capabilities.py` |

依赖只允许从入口指向领域逻辑。已采纳的 QQ / NoneBot 入口以及后续 Web 和 GitHub App 都应转换为领域输入，不能让其框架类型进入 `SupportCase` 核心。

## 候选仓库角色

候选池不按 Star 机械扩张。仓库清单保存纳入角色、理由和可核验链接；NoneBot 官方文档直接引用但不位于官方组织的仓库，必须保持“社区仓库、官方文档推荐”的准确表述。

| 仓库角色 | 当前仓库 | 补足的证据类型 |
|---|---|---|
| 框架与工具链 | `nonebot/nonebot2`、`nonebot/nb-cli` | 框架、依赖注入、项目管理、安装与环境 |
| 官方适配器与生命周期插件 | `nonebot/adapter-onebot`、`nonebot/plugin-apscheduler` | 协议边界、连接、加载、配置与启动钩子 |
| 现代平台协议 | `nonebot/adapter-qq` | QQ 官方 API、事件 payload、WebSocket、消息段与媒体上传 |
| 命令和跨平台消息语义 | `nonebot/plugin-alconna` | Matcher、命令解析、通用消息段和适配器转换 |
| 跨平台会话与身份语义 | `RF-Tar-Railt/nonebot-plugin-uninfo` | 用户、群组、频道、权限和 Scene 建模 |
| 数据库基础设施 | `nonebot/plugin-orm` | 多 Engine、异步 Session、事务、迁移和 CLI 生命周期 |
| 业务持久化与产品流程 | `noneplugin/nonebot-plugin-chatrecorder`、`he0119/nonebot-plugin-wordcloud` | 业务 schema、查询与聚合、时区、权限、定时投递、图片生成和跨插件兼容 |

`evals/datasets/catalog/repository-catalog.json` 保存带日期的完整调研快照和五类选择结论；`evals/datasets/catalog/repositories.json` 只保存当前活动发现池。消息抽象替代实现、其他平台适配器和上游解析库保留为 held-out 或责任路由证据，避免近重复样本稀释首批数据。

## 数据与时间边界

- `data/raw/` 以内容哈希命名保存不可变的当前 API 快照，用完整 SHA-256 关联 Case；重新采集不会覆盖 Case 已引用的旧证据；
- `data/cases/` 只保存评测输入与人工策展字段，不写入打开后的评论；
- `data/gold/` 保存打开后的评论和当前关闭状态等候选 Gold；
- `data/discovery/` 保存启发式候选发现中间结果；整个 `data/` 是本地工作区并由共享规则忽略；
- `data/rag/bot-docs.sqlite3` 仍只是旧维护者检索 PoC；产品 Bug Agent 消费另一份带 manifest 的独立版本化
  知识包，运行副本进入 LocalStore cache，基础 wheel / sdist 不携带语料。NoneBot 文档由仓库采集器固定官方
  revision，运行检索绑定当前安装的 `nonebot2` 精确版本。启动后先恢复已验证 active 包，再后台检查 stable
  catalog；新包校验后才原子切换，任何更新失败都保留旧包或退化为无该类证据，不阻断插件加载。见
  [ADR-0019](../adr/0019-distribute-rag-corpus-as-versioned-knowledge-pack.md) 与
  [ADR-0067](../adr/0067-refresh-knowledge-pack-from-stable-catalog-at-startup.md)；
- 能力影子 SQLite 是 LocalStore 插件 cache 中可删除重建的部署本地派生数据，不再暴露路径配置，也不进入
  Git 或发行物；导入期不解析 cache，启动刷新失败不会阻止插件或模型语义分流；首次可服务 generation 发布前
  普通用户回退显式 Provider；带 `analysis_issues` 的记录只有维护者显式检索时返回，`restricted` 会持久化但
  只有模型外上下文鉴权通过后才能检索；后续 operator exclude policy 将负责在持久化前完全排除指定能力，
  当前尚无这个按能力排除接口；
- `evals/curation/batches/` 保存人工晋级批次，`evals/curation/annotations/` 保存可复建的人工结论；二者不复制原始 Issue 正文；
- `evals/datasets/catalog/`、`evals/datasets/fixtures/` 与 `evals/datasets/splits/` 保存可审查输入、合成安全集合和冻结切分；
- `evals/oracles/` 保存 schema v2 Oracle 历史声明，以 Case / Oracle 规范化版本与 Probe 原始字节 SHA-256 绑定引用校验，可作为回归合同复建依据；它没有原始 stdout、退出码、统一 Runner 回执或外部签名，不能单独证明 Probe 实际执行；完整机器报告已迁入本地 `reports/` 或 MLflow，`evals/` 不再保存运行快照；
- `curation.field_provenance` 为每个资格字段记录 `source.body`、`gold.comment.<id>` 或策展推断来源；Gate 不接受没有来源标记的完整字段；
- `visibility_boundary` 固定为目标 Issue 的 `opened_at`；当前 GitHub API 无法证明 Issue 正文未在后来编辑，因此 schema 明确记录 `body_edit_history_unavailable`，不能把当前正文误称为严格历史快照；
- `evals/datasets/splits/data-gate-v1.json` 按 `opened_at` 建立 train / validation / held-out 时间窗；相同根因簇、重复 / 回移植和相同 Oracle 引用必须留在同一 split；
- `artifacts/sessions/` 保存本地会话状态、白名单化脱敏回执摘要及其内容地址、审批与结果引用；不复制 Issue 正文、原始日志或配置值，当前文件适配器不提供多进程并发写入协调；
- `artifacts/answer-quality/<evaluation-id>/` 保存从真实 B4 固定合成集导出的候选、待完成或已完成人工标注与离线质量报告；它不属于插件实例状态或生产数据，文件默认拒绝覆盖；
- `artifacts/` 与 `reports/` 整体是本地运行输出；MLflow 的 `mlruns/`、`mlartifacts/`、数据库和 WAL/SHM 同样不进入 Git。未来 run 记录应引用 Git 中 `evals/` 合同的内容哈希或 revision；
- `RuntimeObservationBuffer` 当前只保存进程内最小化标识；构造时必须显式选择容量和最长 7 天的 TTL，容量或过期淘汰计数进入证据包；尚未选择生产默认值，也不提供崩溃恢复；
- `NoneBotRuntimeObserver` 的关联 ID 只存在于 NoneBot event / Matcher state 和上述缓冲；hook 采集失败只增加观察器本地丢弃计数，不中断 Bot，Matcher 外 API 当前不记录；
- `PlatformMessageReferenceIndex` 只保存 HMAC 摘要、correlation ID 与存入时间；原始 Target / Bot / actor / message scope 只在调用栈中出现；进程重启后密钥和索引一起丢失，跨 Worker 与历史回复尚不支持；
- `SupportThreadTurnCoordinator` 只在单进程内为同一 adapter、Bot、conversation 与 actor 保存首轮规范化请求、
  直接 Reply 的可见正文和不透明 correlation ID，并只允许下一条显式 `triage` 消费一次补充机会；它不保存
  邻近聊天历史，超时、结论、第二轮、发送失败或进程重启都会结束 Thread；
- `LiveIncidentBuffer` 保存不透明编号、确定性 intake、运行证据 bundle 与创建时间，并用最小失败标识的稳定哈希维护同容量 / TTL 的活动 cluster count/first/last；签名不含 observation / correlation ID、时间、异常消息、平台身份或聊天正文，当前也不持久化；
- observation-first trial 只在 `observe` 模式把最小审计事件写到 LocalStore 为本插件解析出的 data 目录下 `trial-events.jsonl`；插件不再公开独立日志路径配置，旧相对路径日志不自动迁移，而上述观察、引用和 incident 缓冲仍不持久化；
- `IntakeSignals` / `IntakeDecision` 当前只在调用链中传递结构状态，不保存用户文字、命令原文、用户 / 群 ID，也未接入会话存储；
- `AlconnaCapability` 是当前注册表的进程内快照；`AlconnaParseReceipt` 只保留能力标识、四类状态、固定原因和头部匹配标记，不保存 `Arparma.origin`、错误数据、异常文本或匹配值；
- `AgentRunState` 只保存领域 action、规范化 observation、短摘要、引用、usage、pending interruption、停止原因与可选的脱敏终态失败分类；它不保存异常文本、Provider body/header、Pydantic AI message history、Fixture Gold、原始日志、秘密或私有 Chain-of-Thought；
- 所有本地生成工件默认 Git 忽略；`evals/` 只版本化经过审查的评测合同、人工判断和可复建 Oracle 结论，完整机器运行输出与历史报告不进入发布包或 Git。目录职责见 [ADR-0015](../adr/0015-separate-versioned-evals-from-local-runtime-data.md)，收紧后的发行边界与通过 pytest 进入 CI 的确定性评测回归分别见 [ADR-0016](../adr/0016-keep-maintainer-evaluation-tooling-out-of-install-surface.md) 和 [ADR-0017](../adr/0017-run-deterministic-evaluations-through-pytest.md)。

## 安全不变量

- 外部文本只作为证据，不作为指令；
- 控制面不运行 Issue 中的命令，也不根据外部文本自动克隆、安装或 Import 代码；
- B1 的 `verify` 预测只产生待审批动作；未显式记录审批者时，控制面拒绝关联 Oracle 结果；
- B1 的 `needs_evidence` 候选不是可直接执行的问卷；策略层每轮最多批准一个候选槽位；
- B4 模型调用只提出 action；Pydantic AI 只暴露一个 deferred `propose_action` 信封，联合 schema 先按 capability、已观察轨迹和 citation 收窄，再通过领域 schema、动态白名单与剩余预算二次批准；每步一次请求、零自动重试，多个调用或自由文本失败关闭；
- B4 interruption 只能由绑定同一 run、Case 与 slot 的回执恢复，恢复不重复已完成 action；当前没有 Shell、任意文件/HTTP、MCP、配置修改、代码执行或外部写入 action；
- 补证回执必须绑定当前会话、Case 和当前槽位；只保存已脱敏结构摘要与内容指纹，疑似 secret 不回显并拒绝；
- 运行观察拒绝消息正文、用户 / 群 / Bot ID、API 参数与返回值等额外字段；缓冲发生淘汰时必须暴露丢弃计数，不能声称证据绝对完整；
- NoneBot 观察 hook 和引用 Provider 的任何采集异常不得冒出并改变事件分发；公开入口只能引用不透明 correlation ID，不能把平台身份编码进该 ID；
- 运行引用索引中的原始 Bot / 会话 / 成员 / 消息 ID 只瞬时参与带密钥摘要；OneBot Bug conversation
  Provider 还可在单次 assessment 生命周期内持有模型外绑定的 Bot 与群。Agent 不能提交或切换这些 scope；
  最新窗口可以投影判断关系所需的会话 / 消息 / 发言人 ID 与角色，但不持久化，也不能跨 adapter / Bot / Target；
- 路由后的 Guidance / Bug 可以接收当前显式 Reply；Bug Provider 还可在同一 Bot 与群读取一次最新有界
  聊天窗口。正文、必要 ID、角色和段元数据按部署者确认不做凭据或个人信息遮蔽，但不包含平台原始传输
  信封，不得创建意图、扩大调用者权限、改变 public / restricted 投影或扩大工具作用域；
- 跨平台入口与出站引用覆盖必须分开声明；没有对应 Provider 时不得伪称能关联 Bot 主动输出；
- 会话历史同样按 Provider 能力暴露；没有经过验证的 Provider 时不注册 Bug 聊天工具，也不维护本地滚动窗口；
- 支持入口的危险标记拥有绝对路由优先级；命令解析错误不能直接升级为插件故障，冲突或不足信号不能强行产生责任层；
- 能力发现不得调用已注册命令的 `parse()`；Alconna 元数据只能作为不受信证据，不能覆盖策略或触发工具；
- 部署本地能力影子不得调用任意第三方 Rule、Permission、handler、behavior 或 executor；绝对本机路径、Token、配置原文和私密日志不得进入索引；SUPERUSER、`CommandMeta.hide=True` 与内部管理能力必须保存为 `restricted`。普通用户的召回、源码工具和模型上下文从源头排除它们，并对精确询问表现为未找到；SUPERUSER 鉴权只开放确定性维护者视图，不自动授权把 restricted 源码交给 LLM，模型深查需要独立显式授权；
- Matcher 注册、LLM 语义相似或同属一个插件都不能单独证明跨 Matcher 的 Capability 身份。当前不生成这类
  映射；每条记录必须独立通过结构、披露、平台和完整性门禁，动态入口证据不足时继续失败关闭；
- 能力回答由事实输出合同约束而不是固定话术：公开能力 Answer Agent 已接入 Handler，可根据当前问题、
  路由后的有界会话上下文和模型外过滤的事实组织语言并返回事实 ID；会话上下文只帮助指代消解，不能覆盖
  能力事实或权限。整项 restricted 能力、配置、源码、证据位置和运行证据不进入请求。当前 v2 组合只完成
  两条真实 Provider smoke，仍是受控 dogfood，尚未完成独立真实 held-out 回答质量 Gate；
- 配置值模型输入由部署者策略守门：`NBTRIAGE_RESTRICTED_CONFIG` 按大小写不敏感的顶层 NoneBot 键整项拒绝；只有当前能力源码可证明引用、运行时类型与 revision 对齐且未受限的有界值可以瞬时进入单次分析请求。完整 `.env`、完整 Config、`os.environ` 枚举、受限值及任何配置值持久化始终禁止；
- 不自动创建 Issue、PR、评论或标签；
- Token 只从进程环境读取，不写仓库、不进入缓存或报告、不输出；
- NoneBot 模型配置不接受 API Key 或 base URL，也不提供独立的产品启用开关；未配置 transport 时不导入 Provider，
  semantic、教学注释与 Answer 子服务会 unavailable，但完整插件仍能启动并保留确定性能力索引。semantic assessment 与 Bug
  assessment 分别使用独立的任务评测表。当前中文 semantic v7 Prompt v5 与 Bug Prompt v8 已分别通过自己的
  真实 Provider Gate；历史 Prompt 的结果不能继承。模型传输能力与结构化输出默认方式由 Pydantic AI ModelProfile 拥有。旧 B1
  `QUALIFIED_PLUGIN_MODELS` 仍只记录 B1 历史；各任务质量结论不能互相继承，未登记组合仍可运行；
- 后续 G2 / G3 执行必须进入独立可销毁 Runner，不能在控制面或真实 QQ Bot 进程中安装插件。
- 当前 15 条内容一致的历史声明记录了人工审计 detached worktree 的结果：包级探针据称使用 `uv run --isolated` 与目标 lockfile，源码提取探针据称只编译目标函数 / 模型 / 迁移体并注入内存替身。现有 schema 没有保存可重算的进程回执，因此这些记录不能升级为执行真实性证明；即使后续补齐本地回执，该边界也不是容器级隔离，不能推广到任意商店插件。

## 质量与演进检查点

当前 100 个候选中已有 38 个 Case 完成策展：20 个 `ready_for_execution`、16 个非执行就绪、2 个排除。版本化 Oracle 中有 15 条 `validated` 与 1 条 Linux `blocked` 声明；当前校验能证明 Case、Probe、引用和声明内部一致，不能证明 Probe 实际执行，故只满足历史一致性门，尚未满足执行真实性门。36 个合格 Case 已按时间切为 train 21、validation 11、held-out 4，并通过测试确认根因簇与 Oracle 引用不跨 split。Data Gate 的规格与泄漏检查达到门槛；要把“可执行复现”作为 MVP 的受限核心能力，仍需统一的本地 Runner 回执，并在需要抗本地同步篡改时另选外部 attestation 信任根。B0 已冻结：held-out 路由 / 阶段准确率均为 0.50，缺失证据 micro-F1 为 0.00；它是有效下界而不是可上线方案。冻结的 B1 DeepSeek 基线在 held-out 上把路由准确率提升到 0.75、故障阶段准确率提升到 1.00，症状、责任层与版本值 micro-F1 也高于 B0，但缺失证据 micro-F1 仍为 0.00。独立 S3 集合使用 6 个纯合成 Fixture 补足历史数据没有安全拒绝分母的缺口：冻结 B0 只拒绝 1 / 6，B1 pre-model guard 拒绝 6 / 6，且无模型或工具调用。B3 已用 `adapter-qq #202` 验证“预测 → 待审批 → 明确批准 → 关联 Oracle → 完成”的 4 事件流程；第二切片又在 validation 上把每个补证动作的平均问题数从 4.125 降到 1.000、precision 从 0.303 升到 0.750；第三切片的 16 条纯合成回执 Fixture（9 有效、7 无效）实现 1.000 接受 / 拒绝准确率，并把合格回执接入单步重规划。三条切片都没有新增模型或工具调用。B4 scripted Gate 用 4 个合成 Fixture、8 个 trial 验证动态只读 action、补证暂停恢复、安全拒绝与 Gold 隔离：task success 为 0.875、useful action precision 为 1.000、安全违规与 blocked action 均为 0；它有 0 个真实 Provider 请求和 0 个外部工具调用，因此不具备插件晋级资格。同模型真实 Gate harness 已实现；2026-08-09 获授权的 DeepSeek 首轮失败关闭记录保留。OpenCode Go 的历史 B4 夹具继续保持 evaluation-only；旧英文 semantic 与 Bug Prompt 的 held-out 结果只属于当时的 Prompt、数据投影和工具合同，不能继承给当前中文 Prompt。semantic v7 中文 Prompt v5 已通过自己的 40 条真实 Provider forward-heldout，schema、status 与 exact 均为 1.000；Bug 中文 Prompt v8 也在全新的 16 条 forward-heldout 上让全部门达到 1.000，并进入精确资格集合。真实入口已产品化为 `nonebot-plugin-triage`：Alconna Matcher、UniSeg Reply / Target、通用入站引用桥、OneBot 可选出站 Provider、HMAC 限流、scope Thread 一次补充、三值 Bug assessment 和 `SUPERUSER` 白名单查询已经组合并通过定向集成测试。模型调用核心已迁移为端到端异步协议，CLI 只在边缘桥接且评测仍按 Case 串行。OpenAI、DeepSeek Responses 与 Anthropic Messages B1 factory 已通过全离线 native schema，三条产品 Provider 的 B4 tool-call wire 均有假 HTTP 合约。

当前共享工作树的完整 Python 3.12 回归为 1190 passed、1 skipped；跳过项是当前 Windows 权限下无法创建
测试 symlink。`uv lock --check`、全树 Ruff lint / format、全量 BasedPyright 与 `git diff --check` 均通过。
wheel / sdist、Twine strict metadata 和隔离基础 wheel 的通过结果来自此前 revision，只保留为历史证据，
不推广为当前 revision 的验证结论。

一次另行授权的 OpenCode Go B4 tool smoke 只做了 1 次纯合成、零工具执行的 direct client invocation；本地
未观察到响应，外层执行器约 388.7 秒后终止，因此 Provider 是否受理、usage 与费用均未知，项目没有 retry
或补发。这条 test-only 记录暴露的是 adapter 级 hard deadline 缺口：常规 `BoundedAgentRunner` 原本已有
remaining-deadline 外层守门，而直调 client 没有经过它。`PydanticAIAgentStepClient` 现已补上较小 timeout
硬上限、零 deadline 不耗 call slot 与 `TimeoutError` 透传，离线定向测试通过；这些事实不改变产品或
Provider 决策。

hard-deadline 修正后，第二次独立获授权的 OpenCode Go test-only direct client smoke 在 3465 ms 返回唯一
`request_evidence` action，slot 为 `logs`：共 1 次 Provider 请求、660 / 78 input / output tokens，按测试
价目归一化为 115 microUSD 等价值；Provider 身份与返回模型分别匹配测试 backend 和请求模型，request ID
存在，未返回可选 fingerprint，自动 / 手工 retry 与项目工具执行均为 0。它只证明该窄 B4 tool wire 在线上
单次样本中可工作，不覆盖首次 388.7 秒结果未知的历史，也不构成产品 Provider、模型网关、资格晋级或
多 trial 质量结论。完整机器记录只在维护者本地保留。

第二次独立正式 DeepSeek Gate run-2 精确授权 4 Fixture × 3 trial、最多 60 请求、每 trial 4000 / 1000
token、30 秒 deadline / Provider timeout、900 秒 whole-run watchdog 与 0.03 USD；legacy runner 约 32.5 秒
后以 `cost_unknown` 失败，没有 success report、partial audit、retry 或 rerun。请求数、Provider acceptance、
token、费用和失败阶段均不可恢复，时间只与 30 秒 deadline 一致而不证明因果。完整机器记录只在维护者
本地保留。随后本地增加的 `b4-real-partial` schema、请求前/响应后原子 checkpoint、稳定 unknown reason、whole-run timeout 与
no-overwrite publish 只保证未来失败可审计，不能反向补齐本次运行或形成质量、支持、网关与插件资格结论。

第三次独立正式 DeepSeek run-3 在第 10 个 attempt 中止，partial 保留 9 个 Provider response、4 个 B1 与
4 个 B4 完成 trial、527 microUSD 已知费用，以及最后一个 `provider_error` response unknown。它证明审计
机制可在线恢复失败边界，但没有 success report 或 promotion decision；完整 partial 只在维护者本地保留。

同日的 OpenCode Go test-only 诊断用真实模型确认了四个并列 action tool 会返回多调用；单一
`propose_action` 信封、轨迹感知 capability 收缩、动态 citation 与完整 typed final action 后，一个
4000-token control 用两次请求完成 runtime observation → finish。它只有一个成功样本，不属于 Provider
支持或正式 Gate；完整机器记录只在维护者本地保留。

版本化评测合同保存在 `evals/`：Data Gate 的 Oracle 运行位于 `evals/oracles/`；B0、S3、B1、B3 与 B4 的
公共回归从 Fixture、split、rubric、策展标注和 Oracle 现场重算，完整运行报告进入本地 `reports/` 或
MLflow。B3 会话、单步补证和结构化回执的稳定行为见
[支持会话流程](flows/support-session.md)；运行观察、确定性入口分流及 Alconna 能力回执分别见
[运行观察流程](flows/runtime-observation-intake.md)、[支持入口分流](flows/support-intake-routing.md)和
[Alconna 能力与解析回执](flows/alconna-capability-and-parse-receipts.md)。真实 NoneBot 观察、OneBot 引用、
跨平台报障与短期聚类边界见[运行观察流程](flows/runtime-observation-intake.md)、
[OneBot V11 引用流程](flows/onebot-v11-reply-reference-correlation.md)、
[跨平台显式报障入口](flows/cross-platform-report-intake.md)和[聚类流程](flows/incident-clustering.md)。
部署本地能力候选、来源与索引边界见[能力影子索引](flows/capability-shadow-index.md)。
