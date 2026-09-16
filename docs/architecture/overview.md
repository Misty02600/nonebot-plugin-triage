# NoneBot Triage Agent 架构概览

## 目标与边界

NoneBot Triage Agent 把模糊报障转换为证据可追溯的 `SupportCase`，再选择补问、检索、确定性探针、隔离复现或升级。核心不承诺自动解决全部 NoneBot / QQ 问题，也不把 Issue 分类、摘要或聊天外壳当作主要差异。

### 当前 triage 语义状态

每轮非空 `triage` 的已采纳产品契约是经过受限语义 assessment，不设产品级模型启用开关，也不保留
功能问法词表或固定故障话术作为意图分类器。当前代码已删除词表和 `nbtriage_model_enabled`，并实现传输无关的
v8 联合 assessment 请求 / 输出闭合合同、一次性失败关闭 service、以领域 Pydantic model 作为 `Agent.output_type` 的
结构化 client、确定性 router 与插件运行编排。v8 一次产生 guidance、behavior exploration、Bug assessment、
feature feedback 四种 goal、独立 observation 与公开插件 selection；action 与授权始终由模型外 router 决定。当前系统指令是
中文 `support-semantic-v8-catalog-prompt-v5-zh`。当前 semantic 资格表为空；所有 Pydantic AI 可解析的模型
可以执行同一任务，并在技术或安全合同失败时才降级为 unavailable / abstain。详见
[ADR-0037](../adr/0037-make-semantic-assessment-the-default-triage-path.md)。远端 assessment 的数据类别已经获准，
限于当前请求、当前 public catalog、直接相关 Reply 与同 scope 有界补充问答；身份、scope、内部 owner 映射、
配置、日志、源码、运行证据和 `restricted` 证据仍不得出站。历史 OpenCode 与使用已删除 Qwen 3.6 专属设置的评测只解释旧报告，
其他组合不能继承；当前模型边界见 [ADR-0129](../adr/0129-use-only-pydantic-ai-native-model-transports.md)，数据边界见
[ADR-0038](../adr/0038-limit-semantic-assessment-remote-data-projection.md)。

当前实现覆盖只读离线 Data Gate、B0、B1 RAG-only 基线、B3 可审计会话和 B4 有界 Agent control plane；
OpenAI Responses、DeepSeek Responses 与 Anthropic Messages 已有分任务的离线合约证据，但都不因离线通过而自动成为插件支持。
NoneBot 保留窄 transport 身份和惰性 step-client factory，不再公开产品级模型启用开关。真实入口已有
`triage <自然语言>` 的 Alconna / UniSeg framing、运行观察、scope Thread 最多两次补充、限流和窄回显。Reply
不恢复 Thread；它的可见正文只在路由后进入 Guidance / Bug，message ID 独立解析 runtime correlation。
首轮与补充轮使用同一 assessment service 与 router。旧的短期受理兼容链路已经从运行代码、配置和维护
CLI 中删除；确认的 Bug 只进入当前 ORM 工作流。

`bug_assessment` 分支已经接入首轮与补充轮：确定性协调器先用
subject、adapter、source / contract / deployment revision 和规范化请求构造 fingerprint，查询 LocalStore data
中的 reviewed catalog；精确命中后零日志、零源码、零 Agent。未命中时先预加载 public 合同，再让有界
Pydantic AI Agent 按需查询当前 correlation 的运行 / 异常日志、模型外绑定会话的 OneBot 最新群聊天窗口、当前已加载
subject 的 Python 源码、版本化设计知识包与部署摘要；最终由本地 reconciler 检查 citation、freshness、
partial 与冲突，只返回 `bug / not_bug / unknown`，不执行外部副作用。当前系统指令是中文
`bug-assessment-agent-v1-prompt-v23-zh`，默认使用 15 次请求、12 次通用证据和 1 次 conversation 额度，并在硬上限前
切换到无函数工具调用的最终提交。旧 v8 的 16 条 forward-heldout 曾取得 schema、verdict、occurrence、责任、
引用、预算、usage、scenario 与 safety 全部 1.000，但只能作为旧 Prompt 与旧预算的历史证据。
当前 Bug 资格表为空；v23 重跑前按未验证执行，不继承旧 Prompt 或旧英文 Prompt 的质量结论。
相关边界见 ADR-0053、ADR-0060、ADR-0064、ADR-0065、ADR-0086、ADR-0130 与 ADR-0145。

[ADR-0066](../adr/0066-use-active-teaching-contract-as-bug-precheck.md) 的公开预检已经接入联合选择：Bug 范围从
健康、完整的当前 public catalog 选择并复核一个或多个插件；缺少对象、操作或具体观察时，在创建案件指纹、
源码后端和 Agent 工具箱前返回，并共用 Thread 的两次补充额度。当前 active teaching annotation 会进入 public contract Evidence；若
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
在本地复核。旧 OpenCode B4 兼容 Chat 夹具和 semantic adapter 已退出当前代码；冻结 fixture 与报告只作
历史追溯，不能产生当前资格。
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
ID 不上传；源码、日志和配置仍执行秘密清理。Probe、GitHub 写回和其他副作用仍由维护者审批。

生产 Pydantic AI Agent 共用脱敏 OpenTelemetry instrumentation。启用模型 transport 时，Agent run、模型请求
和工具执行 spans 经过 Triage 字段白名单后写入 LocalStore data 的轮转 `agent-traces.jsonl`；它保留 trace ID、
耗时、状态、Provider/model、token、费用和安全任务关联。教学注释另记录最终响应的 part 类型、文本/思考/
工具参数字符数和可解析结构数量，仍不保留 Prompt、源码、模型原文、工具参数/结果、异常正文或配置值。
telemetry 失败只关闭诊断记录，不阻断模型任务；完整边界见
[ADR-0089](../adr/0089-persist-redacted-pydantic-ai-agent-traces.md)。

该方向的核心不是“用 LLM 从群聊识别 Bug”。调研已发现 AstrBot BugCatcher 覆盖静默监听、LLM 识别、
去重与 Dashboard，NoneBot 也已有 Sentry 错误跟踪。NoneBot Triage Agent 的产品边界保持在“显式支持分流、
疑似故障与运行证据关联、NoneBot 责任层定位、最小补证和可审计验证”。长期决策见
[ADR-0001](../adr/0001-qq-group-report-linked-runtime-evidence.md)，竞品证据见
[产品定位与同类能力](product-positioning.md)。

修复闭环采用责任层路由与分级自治：L0 观察、L1 建议、L2 配置 / 生命周期修复、L3 维护者授权的上游
协作、L4 本地或维护者拥有插件的隔离代码修复。模型不直接持有 Shell、配置或 GitHub 写权限；高权限动作
进入专用执行器并保持逐动作审批。当前只交付 L0 的纯核心观察契约和既有 L1 控制面基础，其余均为规划
能力。完整边界见 [ADR-0002](../adr/0002-tiered-autonomy-and-ownership-aware-remediation.md)。

共享只读文件工具使用 Harness 0.22.0 的路径边界：直接读取和目录遍历都按链接解析后的真实目标检查
根范围与拒绝规则，不能借符号链接或 Windows junction 读取根外文件、绕过根内受限目录。数据库主文件及
WAL、SHM、journal 伴随文件均不可读。它仍是进程内的路径检查，不是隔离沙箱，也不承诺抵御其他进程在
检查与读取之间恶意替换文件；上游修复见 [Harness #364](https://github.com/pydantic/pydantic-ai-harness/pull/364)。

同一个显式入口承接能力导航、指令纠错、Bug 判定和功能反馈，由 semantic v7 signals 与模型外 router 选择
单一 action。MVP 不代
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
[ADR-0031](../adr/history/0031-require-triage-for-support-thread-continuation.md)，被替代的免命令方案见
[ADR-0030](../adr/history/0030-continue-support-thread-by-exact-reply.md)，能力影子边界见
[ADR-0021](../adr/0021-use-deployment-local-capability-shadow-index.md)，维护者在线检索见
[ADR-0022](../adr/history/0022-limit-capability-shadow-guidance-to-superusers.md)，状态轴拆分见
[ADR-0032](../adr/0032-separate-capability-audience-analysis-and-platform-status.md)，Matcher 与用户可观察能力的
关系见 [ADR-0034](../adr/history/0034-distinguish-matchers-from-user-observable-capabilities.md)。

项目已经实现面向已鉴权维护者的全局项目对话：每条消息是一次新的 Pydantic AI 只读 Agent Run，整个插件
部署只有一个跨 Bot、Adapter、群聊和私聊共享的会话。当前可信场景每轮注入但不参与分区；Capability Shadow
检索和项目根目录只读文件工具可用于核对代码、配置投影和日志，凭据类路径继续硬拒绝。

LocalStore 的 `maintainer-conversation.json` 是会话真值，保存 Pydantic AI 原生消息和 Provider 续接元数据。
Harness 根据真实上下文窗口比例压缩旧历史；文件通过同目录临时文件、`fsync` 与 `os.replace` 原子替换。
普通 Support Thread 的最多两次补充合同不变。维护者会话的状态、停止与新对话合同见
[ADR-0128](../adr/0128-use-native-message-snapshots-for-maintainer-conversations.md)。

已采纳的投递合同还允许私聊、群聊和频道请求进入同一 `triage` 意图分流，该入口场景边界现已落实。行为探索在针对当前 Bot / Event
完成模型外 `SUPERUSER` 鉴权后，可以把获准披露且已净化的完整解释返回到原始提问会话；系统不再按房间
成员构成增加 allowlist、旁观者鉴权或强制转私聊，由请求者选择合适的会话。这不放宽秘密过滤或远端模型
数据授权。当前 Behavior 分支在每次模型请求和工具执行前重新鉴权，
详见 [ADR-0028](../adr/0028-allow-private-triage-and-superuser-request-context-replies.md)。

## 核心能力与当前命令入口

下表同时记录插件运行入口和仓库维护命令。`just maintainer <command>` 只在源码仓库可用，等价于
`uv run --group maintainer python -m tools.nbtriage_maintainer <command>`，不是面向插件安装者的稳定公开接口。
[ADR-0016](../adr/0016-keep-maintainer-evaluation-tooling-out-of-install-surface.md) 保留 `nbtriage` 领域核心，
但已将 console script、评测 / 采集 orchestrator 迁到不进入 wheel 或 sdist 的仓库工具。

| 核心能力或公开入口 | 对外含义与适用场景 | 关键状态或副作用 | 主要实现位置 |
|---|---|---|---|
| `just maintainer discover` | 从带角色与生态证据的活动仓库形成跨仓库均衡待审池 | 启发式分数只排序，不确认根因或 Oracle；完整调研与活动 manifest 分离 | `tools/nbtriage_maintainer/discovery.py`、`evals/datasets/catalog/repositories.json`、`evals/datasets/catalog/repository-catalog.json` |
| `just maintainer collect` | 从候选清单读取公开 GitHub Issue，形成原始快照与策展草稿 | 只读访问 GitHub；写本地忽略目录；已有 Case 默认保留 | `tools/nbtriage_maintainer/cli.py`、`tools/nbtriage_maintainer/collector.py` |
| `just maintainer enrich-*` | 将时间线、关联 PR、PR commits 与直接引用 commit 边界写入隐藏 Gold | 只读 GitHub；REST 时间线与认证 GraphQL `CONNECTED_EVENT` 合并，后者无 Token 时显式记录跳过；不进入 Case 输入 | `tools/nbtriage_maintainer/github.py`、`tools/nbtriage_maintainer/timeline.py` |
| `just maintainer apply-annotations` | 把版本管理的人工判断合并到生成 Case | 只允许修改 `curation` 字段 | `tools/nbtriage_maintainer/curation.py` |
| `just maintainer gate` | 评估 Case 是否具备公共字段和模式特有证据，并核对版本化 Oracle 结果声明的内部一致性 | 生成本地 JSON 报告；不修改 Case；引用、Case / Probe revision 或完整性不一致时失败关闭；当前不执行 Probe，也没有本地执行回执或外部 attestation | `tools/nbtriage_maintainer/gate.py`、`tools/nbtriage_maintainer/runtime_results.py` |
| `just maintainer evaluate-b0` | 在冻结 split 上运行固定检查表、规则路由和 train-only 相似 Case 检索 | 预测只读公开 Issue 输入；Gold 只进入评分器；不调用模型或外部工具 | `src/nbtriage/baselines.py`、`tools/nbtriage_maintainer/evaluation.py` |
| `just maintainer evaluate-s3` | 在独立合成 Fixture 上比较冻结 B0 与 B1 模型前安全拒绝 | 不读取真实秘密或生产数据；不检索、不调用模型、不调用外部工具 | `src/nbtriage/safety.py`、`tools/nbtriage_maintainer/safety_evaluation.py` |
| `just maintainer search-capabilities` | 检索部署启动后在后台生成的本地能力影子索引 | 默认只返回可服务的 `public`；`--include-unresolved` 纳入带具体分析问题的记录；带外确认授权后可用 `--include-restricted`，该开关不自行鉴权；不调用模型或能力代码 | `src/nbtriage/capability/catalog/records.py`、`tools/nbtriage_maintainer/cli.py` |
| `just maintainer evaluate-capability-teaching` | 用显式 Provider / endpoint / model / settings / Prompt / Schema / request revision / 隐私 / 预算合同运行教学注释 forward-heldout | 已消费的 v8–v13 保留为不可改写的历史证据；OpenCode Go / DeepSeek V4 Flash 的 request v3 / Prompt v39 / schema 7 v13 为 16/20，语义 0.800，Gate 失败。当前教学合同 保留全部 family 成员，将无损列式成员清单与唯一 Parser shapes 分离去重，完整保留 Alconna 联合输入成员，把 Parser 结构模板中的匿名槽位交给模型依据 Evidence 命名，并要求 family 聚合槽位在压缩后仍可操作；tool-mode 与 native-mode 都直接提交顶层教学分析对象，不接受 `output` 包装或 JSON 字符串，输出格式或投影错误最多纠正两次。静态工厂成员的唯一目标插件本地 Callable 有界加入首包，不逐成员运行 Agent。NoneBot / Uninfo 的固定 Permission 以 OR alternatives 保存，Migut 只投影可无损归约的单一 SUPERUSER，或精确 `admin OR owner` 组合。`platform_scope` 留在模型外 Runtime 路由；role 表示调用者身份，access 表示入口查询的可配置权限、名单或开放资格，业务准备状态进入 behavior boundary。源码实际使用 `Uninfo` / `QryItrface` 注解，或参数注解、默认值或 Handler 装饰器 `parameterless` 唯一静态展开为 `Annotated[..., Depends(provider)]` / `Depends(provider)` 且 provider 源码使用相关类型时，加入版本化框架语义 Evidence，明确机器人 `self_id`、调用者 `user.id` 与会话 `scene_path` 的区别；已发现 gate 的 constraint 必须关联 candidate，其他由 Handler/helper Evidence 直接证明的执行限制允许不关联候选。现有 Evidence 不足以支持或否定拟公开事实时，Agent 可从已知符号、路径或调用位置按需补证，取得足够证据或确认无法唯一判断后停止。注册 gate 的唯一模块级绑定链会在同一预算内补入本地 statement 与一层外部函数；初始和动态 Python Evidence 提供请求内位置句柄，模型一次调用即可完成定义跳转、revision 复核和可引用读取。编译扩展 stub 和过长定义只给不可引用导航，均不递归依赖树。生产默认使用十次请求与十次导航，第八次导航或第七次已完成请求后最多提示一次收敛，并在导航耗尽、已完成八次请求或进入最终时间窗口后以 `tool_choice=none` 提交结果；累计 total-token 不再控制阶段或作为生产硬上限。Provider SDK 对瞬时失败最多重试两次，教学层不重跑整个 Agent；维护诊断保存 SDK 内部失败 attempt 的有界脱敏响应。当前 high-thinking 合同尚无独立 held-out，不能继承 v13 或 max-thinking 诊断结论 | `tools/nbtriage_maintainer/capability_teaching_evaluation.py`、`tools/nbtriage_maintainer/model_evaluation_target.py`、`evals/datasets/fixtures/capability-teaching-v13-forward-heldout.json`、[ADR-0102](../adr/history/0102-keep-family-aggregate-parameters-actionable.md)、[ADR-0108](../adr/history/0108-preserve-permission-disjunctions-in-teaching-requirements.md)、[ADR-0109](../adr/history/0109-delegate-transient-http-retries-to-provider-sdks.md)、[ADR-0110](../adr/history/0110-preload-static-family-member-callables.md)、[ADR-0111](../adr/history/0111-preserve-alconna-union-input-types-in-family-shapes.md)、[ADR-0113](../adr/0113-separate-routing-authorization-and-business-readiness-in-teaching.md)、[ADR-0114](../adr/history/0114-follow-static-gate-bindings-and-bind-jedi-to-request-evidence.md)、[ADR-0115](../adr/history/0115-open-python-definitions-through-request-bound-navigation-handles.md)、[ADR-0116](../adr/history/0116-classify-role-and-access-by-the-executed-gate.md) |
| `just maintainer evaluate-b1` | 用 train-only 证据和一次 Provider 原生 JSON Schema 运行 validation 或 heldout | 必须显式 `provider:model`、输出 / 调用上限和付费确认；统一模型绑定按 ModelProfile 在请求前核对 native schema；Pydantic AI Direct Request 仍按 Case 串行，响应按完整请求缓存；新的通用目标在冻结新合同前只生成 `custom_unqualified` 报告 | `src/nbtriage/rag.py`、`src/nbtriage/model_adapters.py`、`tools/nbtriage_maintainer/model_evaluation_target.py`、`tools/nbtriage_maintainer/evaluation.py` |
| `just maintainer evaluate-b3-evidence-policy` | 在 B1 validation 的脱敏策展投影上冻结单步补证策略 | 只接受内容 SHA-256 与 11 条规模均固定的官方 validation-only 投影，并复核字段白名单、枚举和 Case 身份；不调用模型或工具；内容替换、held-out、Provider 元数据及未知字段被拒绝 | `tools/nbtriage_maintainer/evidence_policy.py`、`tools/nbtriage_maintainer/evidence_policy_evaluation.py` |
| `just maintainer evaluate-b3-evidence-receipts` | 在纯合成 Fixture 上验证结构化回执守门和请求绑定 | 正式 Gate 绑定冻结 Fixture 的原始 SHA-256、集合 ID 和 16 条 Case；自定义内容仅生成 `custom_unqualified` 报告且 CLI 非零退出；只评估白名单 schema、脱敏、疑似 secret 与错绑，不判断证据真伪；0 模型 / 工具调用 | `src/nbtriage/evidence_receipts.py`、`tools/nbtriage_maintainer/evidence_receipt_evaluation.py` |
| `just maintainer export-answer-quality-review` | 把完整真实 B4 报告中 `forward_hidden` 的完成态候选导出为本地人工评审包 | 只接受 schema v3、纯合成、真实模型多 trial B4 报告并核对 Fixture/split 哈希；只复制领域层规范化证据事实，不复制 Gold、Prompt、消息历史、原始日志或 Provider 响应；输出拒绝覆盖 | `tools/nbtriage_maintainer/answer_review_export.py`、`tools/nbtriage_maintainer/agent_evaluation.py` |
| `just maintainer evaluate-answer-quality` | 用四轴 0–2 人工 rubric 汇总固定 `answer + citations` 标注 | 默认合成校准只验证评分锚点；候选质量必须来自真实 B4 的 `forward_hidden` 多 trial 报告、使用独立人工复核，并同时通过来源 B4 Gate、均值、逐样本和关键零分硬门；结果只属于 `offline_fixed_fixture`，不构成生产质量证据；非校准报告拒绝覆盖 | `tools/nbtriage_maintainer/answer_quality_evaluation.py`、`evals/rubrics/answer-quality-v1.json`、`evals/datasets/fixtures/answer-quality-calibration-v1.json` |
| `just maintainer evaluate-b4-scripted` | 用 scripted model 在冻结 regression / forward-hidden split 上验证动态 action、预算、暂停恢复、轨迹评分和 Gold 隔离 | 0 真实 Provider 请求、0 外部工具调用；报告记录 Prompt/schema/policy/source revision 与结构化输出通过率，但明确不具备晋级资格 | `src/nbtriage/bounded_agent.py`、`tools/nbtriage_maintainer/agent_evaluation.py`、`evals/datasets/fixtures/b4-bounded-agent-v1.json`、`evals/datasets/splits/b4-gate-v1.json` |
| `just maintainer evaluate-b4-real` | 在明确付费/出站授权后，让同一 `provider:model` 多 trial 对照 B1、B3 与 B4 | 统一经过 Pydantic AI 原生 Provider、ModelProfile 与 settings；只用 forward-hidden 指标判断晋级；B1/B4 后验结构拒绝计入 trial，未知费用仍中止；每次请求前/响应后更新 partial audit，success/partial 路径禁止覆盖；仍无完整质量报告或 Provider 资格 | `tools/nbtriage_maintainer/model_evaluation_target.py`、`tools/nbtriage_maintainer/agent_evaluation.py`、`tools/nbtriage_maintainer/cli.py`、`evals/datasets/splits/b4-gate-v1.json` |
| `just maintainer session-*` | 从冻结 B1 预测创建、接收脱敏回执、审批、关联已有 Oracle 结果并查看支持会话 | `needs_evidence` 只接收当前槽位并从剩余候选重规划；`verify` 未显式审批不能附加结果；不执行代码或外部写入 | `tools/nbtriage_maintainer/sessions.py`、`tools/nbtriage_maintainer/cli.py` |
| `RuntimeObservation` / `RuntimeObservationBuffer` | 接收 NoneBot 观察桥提交的最小化事件、Matcher、插件、API 与异常标识，并按关联 ID 生成证据包 | 不接收消息正文、用户 / 群 ID、API 参数或结果；容量与 TTL 必须由调用方显式给出；仅单进程内存 | `src/nbtriage/runtime_observations.py` |
| `NoneBotRuntimeObserver` | 显式注册 NoneBot 2.5 公共 hook，用事件 state 关联 event、实际 Matcher 与其内部 API 生命周期 | fail-open；只读取框架 / 插件标识和异常类 / 栈模块；Matcher 外 API 不猜测归属 | `src/nonebot_plugin_triage/nonebot_runtime.py` |
| `UniversalReferenceBridge` / `PlatformMessageReferenceIndex` | 通过 UniSeg Target / message ID 统一绑定入站消息，并以带密钥摘要短期关联 correlation ID | 原始适配器 / Bot / 会话 / 消息 ID 只瞬时参与 HMAC；不保存正文；显式容量与 TTL | `src/nonebot_plugin_triage/universal_references.py`、`src/nbtriage/message_references.py` |
| OneBot V11 outgoing reference Provider | 从 Matcher 内成功的群发送结果补齐运行证据 correlation | OneBot 是可选依赖；只读路由字段和 message ID；不结算 Thread，不保存完整 API data / result 或被回复正文 | `src/nonebot_plugin_triage/onebot_v11_references.py` |
| `SupportThreadRecord` / scope Turn coordinator | 以 HMAC scope 保存首轮有界 request / Reply / correlation，并只允许下一条同 scope 显式 `triage` 补充一次 | 单进程、有界、TTL 后逻辑失效并在下一次协调器操作时惰性清理；HMAC 绑定 adapter、Bot、场景和 actor；Reply 与 Receipt 不选择 Thread；只有发送成功才等待补充，第二轮、终局 action、异常或发送失败都关闭；不跨重启 | `src/nbtriage/support/threads.py`、`src/nonebot_plugin_triage/support/threads.py`、`src/nonebot_plugin_triage/support/responses.py` |
| Pydantic AI maintainer conversation | 让已鉴权维护者跨入口持续交流项目内容；每条消息是新 Run，Run 完成不关闭全局会话 | LocalStore JSON 保存原生消息；Harness 比例压缩；十五次请求与六十次工具保险丝，第十三次请求后最多提示一次收敛，第十五次请求动态 `tool_choice=none`；每轮及每次工具执行重新鉴权；项目文件与 Capability Shadow 只读；全局非排队 admission；停止保留快照，新对话整体替换 | `src/nbtriage/behavior/conversation_agent.py`、`src/nonebot_plugin_triage/behavior/conversation_store.py`、`src/nonebot_plugin_triage/behavior/contracts.py`、`src/nonebot_plugin_triage/behavior/evidence.py`、`src/nonebot_plugin_triage/behavior/service.py`、`src/nonebot_plugin_triage/behavior/runtime.py`、`src/nonebot_plugin_triage/handlers.py`、[ADR-0128](../adr/0128-use-native-message-snapshots-for-maintainer-conversations.md) |
| Alconna `triage` Matcher / support intake adapter | 每轮以必选指令接收自由文本；先 Claim 普通 scope Thread，再让 Semantic 联合判断当前文字、公开目录与有界 Reply / 补充上下文；Behavior 进入全局维护者会话 | Alconna / UniSeg 提供命令、Reply / Target 和发送抽象；普通 scope lease 判断归属、TTL、最多两次补充与并发；Behavior 先鉴权并使用全局 admission；私聊、群聊和频道每轮注入当前场景；Reply message ID 只作独立运行 correlation | `src/nonebot_plugin_triage/handlers.py`、`src/nonebot_plugin_triage/support/intake.py` |
| `SupportAssessmentRequest` / `SupportSemanticAssessment` | 冻结语义 assessment v8 的联合请求投影和受限多标签输出 | 请求包含版本号、当前规范化文字、public catalog、直接 Reply 与有界补充问答；输出包含四类 goal、独立 observation 与 public plugin selection，或澄清 / unsupported；不包含 action、回答或副作用授权 | `src/nbtriage/support/semantics.py` |
| semantic Agent output client / assessment service / support router | 直接以 `SupportSemanticAssessment` 作为 Pydantic AI Agent `output_type`；把秘密、超时、传输失败和非法输出收敛为 abstain，再映射为唯一 action | 中文 `support-semantic-v8-catalog-prompt-v5-zh`；当前资格集合为空，所有 Pydantic AI 可解析组合均标记未验证 | `src/nbtriage/support/_model_adapter.py`、`src/nonebot_plugin_triage/support/semantic_runtime.py`、`src/nonebot_plugin_triage/support/semantic.py`、`src/nbtriage/support/routing.py` |
| Bug assessment coordinator / bounded Agent | 沿用已选插件和公开初检，预加载公开事实、Thread 与直接 Reply；仍未解决时按需展开成员目录并选择聊天、运行、日志、源码、设计和部署证据，最后确定性形成三值结论 | OneBot 群历史由当前 Bot / 群模型外绑定并一次读取最新最多 30 条，精确 Reply 独立预装；没有历史 Provider 时不暴露聊天工具；默认 15 请求 / 1 次独立聊天 / 12 次通用证据 / 300k token / 300 秒 / 16,384 单次 output，不设默认美元上限；第 10 次通用证据或第 12 次已完成请求后最多提示一次收敛，证据耗尽或第 13 次已完成请求后以 `tool_choice=none` 收尾并保留输出纠正。工具 schema 按初始范围稳定，超额调用由执行层拒绝。聊天正文、必要 ID 与角色不遮蔽，源码 / 日志仍清理；模型候选不能写问题库或披露内部证据。中文 Prompt v23 与当前预算尚未重新通过 held-out，旧结果只作历史证据 | `src/nbtriage/bug/assessment.py`、`src/nbtriage/bug/_agent.py`、`src/nbtriage/bug/conversation.py`、`src/nbtriage/bug/logs.py`、`src/nbtriage/bug/source.py`、`src/nbtriage/bug/design.py`、`src/nonebot_plugin_triage/bug/assessment.py`、`src/nonebot_plugin_triage/bug/onebot_v11_conversation.py`、[ADR-0145](../adr/0145-combine-configurable-bug-budgets-with-finalization.md) |
| public capability Answer Agent | router 选择 guidance 后，把 public runtime 事实、经校验的教学注释与路由后有界 Thread / Reply 上下文交给第二个 Pydantic AI Agent | 教学注释不会直接绕过 Answer Agent；上下文只能消歧，不能覆盖事实或权限；无工具、单请求、零 retry；未知引用、非法输出或 transport 失败退回确定性模板；v2 两条真实 smoke 通过，尚无 held-out | `src/nbtriage/public_guidance.py`、`src/nbtriage/public_guidance_model_adapter.py`、`src/nonebot_plugin_triage/capability/shadow.py`、`src/nonebot_plugin_triage/support/guidance.py`、`src/nonebot_plugin_triage/support/guidance_runtime.py`、`src/nonebot_plugin_triage/handlers.py` |
| `NBTriageConfig` / `ConfigValuePolicy` / capability analysis | 以 Pydantic AI `provider:model` 配置精确 transport 身份和预算，无产品启用开关；可选 Base URL 只改变部署连接，旧 `NBTRIAGE_MODEL_BACKEND` 会被明确拒绝；把 runtime 命令结构、ast-grep Matcher 结构、已加载源码和当前内存配置投影装配成首个 Evidence Pack，必要时允许 Agent 用共享只读 FileSystem / 定义导航补证；轻量规划后，Evidence 以内部有界准备池逐单元进入模型分析池，同插件单元的准备与分析可以流水重叠 | model 是唯一必需的 transport 选择；semantic、Bug、public guidance 与 capability annotation 分任务记录质量。当前教学合同（版本见[合同常量](../../src/nbtriage/capability/teaching/annotations.py)） 公开 name、summary、usages、search terms、behavior boundaries 和 requirements；`platform_scope` 只留在模型外 Runtime 路由，role / access / behavior boundary 分别拥有调用者身份、可配置权限、名单或开放资格与业务准备状态。tool-mode 与 native-mode 都使用顶层 `_AnalysisOutput` Schema，不接受额外包装或字符串兼容；输出格式或投影错误只纠正一次。Permission 用 OR alternatives 保留角色 / 原子场景 / 授权分支，直接 scene requirement 用 `allowed_scenes` 保存完整允许集合，独立 requirement 仍表示独立条件；Handler/helper Evidence 直接证明的执行限制不要求伪造 gate candidate。family 请求保留全部成员，并把无损列式成员清单与 Parser shapes 分离去重；Alconna 联合类型完整进入 shape，Uniseg `At` 作为直接 `@用户` 输入参与完整性校验；静态成员 Callable 可在源码预算内加入首包。Parser canonical usage 使用匿名结构槽位，模型只负责公开命名；family 聚合槽位必须覆盖全部 shape，“参数”与其他槽位名称使用相同的通用校验；七类以上允许简单概括但不逐类解释，Prompt 不预设槽位成品词。生产默认使用十次请求与十次导航，第八次导航或第七次已完成请求后最多提示一次收敛，并在导航耗尽、已完成八次请求或进入最终时间窗口后动态设置 `tool_choice=none`；累计 total-token 不再控制阶段或作为生产硬上限。Provider SDK 对瞬时失败最多重试两次，教学层不重启 Agent。初始 Evidence 条目数不另设总量上限，但每条 Evidence 和 Agent token 预算继续受限。目标插件工具使用稳定 `target_plugin` 坐标；定义导航负责理解已知符号，根内文本搜索负责定位出现、调用或状态访问位置；注册 gate 的静态模块绑定链与其唯一一层外部函数可在首包闭合，初始和动态 Python Evidence 提供请求内位置句柄，唯一目标一次调用即完成定义跳转、revision 复核和可引用读取；过长定义只给不可引用目标且不递归。固定备选采用 `≤3 / 4–6 / ≥7` 展示边界。当前合同尚无独立 held-out，`QUALIFIED_CAPABILITY_ANNOTATION_TASKS` 为空；`.env*`、凭据、数据库、教学日志、人工帮助和评测 Gold 不可读 | `src/nonebot_plugin_triage/config.py`、`src/nonebot_plugin_triage/config_policy.py`、`src/nonebot_plugin_triage/capability/teaching/analysis.py`、[ADR-0102](../adr/history/0102-keep-family-aggregate-parameters-actionable.md)、[ADR-0108](../adr/history/0108-preserve-permission-disjunctions-in-teaching-requirements.md)、[ADR-0109](../adr/history/0109-delegate-transient-http-retries-to-provider-sdks.md)、[ADR-0110](../adr/history/0110-preload-static-family-member-callables.md)、[ADR-0111](../adr/history/0111-preserve-alconna-union-input-types-in-family-shapes.md)、[ADR-0113](../adr/0113-separate-routing-authorization-and-business-readiness-in-teaching.md)、[ADR-0114](../adr/history/0114-follow-static-gate-bindings-and-bind-jedi-to-request-evidence.md)、[ADR-0115](../adr/history/0115-open-python-definitions-through-request-bound-navigation-handles.md)、[ADR-0116](../adr/history/0116-classify-role-and-access-by-the-executed-gate.md)、[ADR-0122](../adr/history/0122-pipeline-capability-evidence-preparation-and-analysis.md) |
| 公开能力 Provider / 部署本地能力影子 | 普通用户解释显式 Provider 或自动确定公开的当前 adapter 能力；维护者 CLI 可显式检索已加载 Alconna、普通 Matcher、被动能力与插件来源形成的影子候选 | 普通查询在 SQL 召回前限定当前 adapter 的 public，partial / stale 与 blocking issue 均失败关闭；Behavior 只读取字段白名单、去路径与去配置值的安全结构投影；不推断跨 Matcher 角色，不重跑 `parse()`、Rule、Permission 或 handler | `src/nonebot_plugin_triage/capability/discovery/registry.py`、`src/nonebot_plugin_triage/capability/guidance.py`、`src/nonebot_plugin_triage/capability/shadow.py`、`src/nonebot_plugin_triage/capability/discovery/snapshot.py`、`src/nonebot_plugin_triage/behavior/evidence.py`、`src/nbtriage/capability/catalog/records.py` |
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
ordinary scope Thread → first unresolved response sent → next explicit triage consumes one supplement → close
maintainer conversation ─→ SUPERUSER check ─→ global native-message session ─→ read-only project tools
                                                        └─→ Pydantic AI ReAct → reconciler → explanation artifact
regular capability query ─→ current-adapter public ID domain ─→ SQL FTS ───────→ UniMessage guidance
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
                                   → bounded FileSystem/定义导航 Agent → revision-bound annotation cache
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
| Versioned knowledge pack | 从固定 revision 的官方 NoneBot 文档构建、校验和打包 SQLite FTS5 索引，运行时以 manifest 和摘要验证后供 Bug 与教学 Agent 只读检索 | 当前库存只保留 NoneBot 2.5.0；基础 wheel / sdist 不携带语料；下载或校验失败保留旧包或明确降级为无知识库 | 独立 ZIP 资产与 LocalStore active 副本；维护评测复用生产 `KnowledgeIndexReader` | `src/nbtriage/knowledge_index.py`、`src/nonebot_plugin_triage/knowledge_pack_runtime.py`、`tools/nbtriage_maintainer/knowledge_pack/`、[ADR-0019](../adr/0019-distribute-rag-corpus-as-versioned-knowledge-pack.md) |
| Pydantic AI 公共控制与评测客户端 | 基础 wheel 提供 Agent、结构化输出、Harness、定义导航、通用 B1 Direct Request 和 B4 deferred step；维护评测目标统一经 `infer_model()` 构造 | 项目不维护 Provider 专属 SDK factory、Profile 补丁或 wire parser；B1 三类 tools 为空且 instrumentation 关闭，B4 工具只 deferred；密钥由原生 Provider 读取 | 无长期状态 | `src/nbtriage/model_contracts.py`、`src/nbtriage/model_adapters.py`、`src/nbtriage/pydantic_agent_adapter.py`、`src/nbtriage/readonly_tools/`、`tools/nbtriage_maintainer/model_evaluation_target.py` |
| NoneBot model runtime boundary | 以 Pydantic AI `provider:model` 和可选 Base URL 解析实际 `(provider, model, API family)`、连接/settings revision 与惰性客户端 | 只调用 `infer_model()` 和 Provider factory；ModelProfile、结构化输出、统一 thinking 与 usage 由 Pydantic AI 拥有；项目不识别厂商 URL、不维护专属 Profile 或请求改写；缺少 Provider extra 不影响插件导入 | 无长期状态；API Key 只存在于 Provider 标准进程环境和 SDK 客户端 | `src/nbtriage/_model_runtime/settings.py`、`src/nonebot_plugin_triage/task_model_runtime.py`、`src/nonebot_plugin_triage/config.py`、[ADR-0129](../adr/0129-use-only-pydantic-ai-native-model-transports.md) |
| Bug assessment Agent / task runtime | 用 Pydantic AI Agent 原生 `output_type` 与只读 Tools 产生 `BugAssessmentCandidate`，再由本地 reconciler 形成三值 verdict | 中文 Prompt v23；公开成员、会话、运行、日志、源码、设计与部署工具；thinking 关闭；默认 300 秒 / 16,384 单次 output、15 请求 / 1 次聊天 + 12 次通用证据 / 300k hidden emergency fuse / 无默认美元上限；预算可配置并进入资格指纹，工具 schema 按初始范围稳定，达到收尾边界后设置 `tool_choice=none`；当前 `QUALIFIED_BUG_TASKS` 为空，旧 held-out 只作历史证据 | reviewed catalog 位于 LocalStore data；评测 trajectory 只写被忽略的本地 reports；线上聊天不持久化 | `src/nbtriage/bug/_agent.py`、`src/nbtriage/bug/assessment.py`、`src/nbtriage/bug/conversation.py`、`src/nonebot_plugin_triage/bug/assessment.py`、[ADR-0050](../adr/0050-use-a-bounded-agent-for-user-bug-assessment.md)、[ADR-0145](../adr/0145-combine-configurable-bug-budgets-with-finalization.md) |
| Provider response usage / identity | 从 Pydantic AI 响应提取 Provider、model、request ID 与可选 fingerprint，并按返回身份归一化 microUSD | 返回 Provider 不匹配或模型漂移时不回退请求侧价格；身份缺失可记录但真实 Gate 不得晋级 | 无长期状态 | `src/nbtriage/_model_runtime/usage.py`、`src/nbtriage/model_adapters.py`、`src/nbtriage/pydantic_agent_adapter.py` |
| Evidence request policy | 按故障阶段把 B1 多槽位候选收缩为当前轮唯一问题 | 只用于维护者离线评测与会话；只能选择模型候选；空候选失败；validation 冻结后等待前向隐藏集 | validation 策略工件 | `tools/nbtriage_maintainer/evidence_policy.py`、`tools/nbtriage_maintainer/evidence_policy_evaluation.py` |
| Evidence receipt contract | 把九类补证限制为已脱敏、字段白名单化的结构摘要和原始材料指纹；schema v2 以域分隔规范摘要绑定 receipt / session / Case / slot、原始材料指纹、字节数与规范化 facts | 拒绝任意额外字段、疑似 secret、错绑、不完整摘要和版本错配；不读取原始材料；`receipt_revision` 是内容地址而非签名，不能证明 facts 真实来自指纹所指材料 | 合成 Fixture 与冻结守门报告 | `src/nbtriage/evidence_receipts.py`、`tools/nbtriage_maintainer/evidence_receipt_evaluation.py` |
| Answer review exporter / rubric evaluator | B4 schema v3 先保留完成态 `answer + citations` 和白名单化 review context；导出器再把真实多 trial 的 `forward_hidden` 候选转换为待人工评分的固定集，评分器按 groundedness、completeness、limitation awareness 和 overclaim control 四轴汇总 | Gold 只在模型运行后生成评审要点；标注 schema v3 绑定整个 Fixture 与冻结 rubric；候选来源要求真实 B4 报告、同名 completed partial audit，以及一致的 evaluation contract、Fixture/split ID 与摘要。该校验防止明显错绑，不重演账本、trial 指标或 promotion gate，也不是本地文件防篡改签名或 Provider 身份证明 | 版本化 rubric、合成校准 Fixture与校准标注；候选评审包和完整报告写入本地 `artifacts/` | `tools/nbtriage_maintainer/answer_review_export.py`、`tools/nbtriage_maintainer/answer_quality_evaluation.py`、`evals/rubrics/answer-quality-v1.json`、`evals/curation/answer-quality/calibration-v1.json` |
| Support session control plane | 把 B1 route 映射为固定动作，约束回执、重规划、审批与结果附加的合法状态变化 | 读取冻结报告、合格回执和 Runtime validator 结论；不读取 Issue 指令执行工具 | schema v4 本地会话 JSON、预测报告哈希、带 `receipt_revision` 的脱敏回执摘要、action result 与顺序事件；旧 schema 失败关闭 | `tools/nbtriage_maintainer/sessions.py` |
| B4 bounded Agent runtime | 拥有循环、按 capability / 已观察轨迹收缩 action 白名单、参数二次校验、跨步预算、observation 执行、暂停恢复和稳定停止原因 | 只读取既有 `RuntimeEvidenceBundle`、train-only retriever 与精确绑定的脱敏回执；不导入 Provider、Pydantic AI 或 NoneBot 类型 | schema v2 `AgentRunState`：结构化 action、可重算 `receipt_revision` 的规范化 observation、摘要、引用、usage、outcome 与可选脱敏终态失败分类；旧 state 失败关闭 | `src/nbtriage/bounded_agent.py` |
| Pydantic AI Agent step adapter | 把本步允许 action 与 citation 约束映射为唯一 `propose_action` 原生工具信封，并把唯一调用 deferred 给领域层 | 每步一个临时 Agent；`retries=0`、一次请求、最多一个调用；hard timeout 取 client timeout 与领域剩余 deadline 的较小值，零剩余值不耗 call slot，`TimeoutError` 交给 runner 映射 `DEADLINE`；不执行项目工具、不持久化框架历史；Provider 未承诺 strict 时仍执行本地复核；响应后的框架错误通过 `capture_run_messages()` 保留 usage / identity | 无长期状态 | `src/nbtriage/pydantic_agent_adapter.py`、`tools/nbtriage_maintainer/model_evaluation_target.py` |
| B4 evaluation harness | 用不泄漏 Gold 的 staged evidence Fixture 统计 trajectory、usage、安全和晋级条件；真实模式在每个 trial 重跑同模型 B1、从该结果计算 B3，再运行 B4；独立 `b4-real-partial` 保存授权、进度、请求 attempt、账本与失败 code/stage | scripted 模式不得晋级；真实模式显式确认理论请求/token/cost/deadline/whole-run 上限；请求前原子 checkpoint，响应后记账或保留稳定 unknown reason；Provider 错误只保存类别与可选 HTTP status；success/partial 路径禁止覆盖 | scripted 报告已冻结；DeepSeek run-1/run-2/run-3 中止证据保留，run-3 已验证 partial v1，当前 v3 尚无线上工件和完整正式报告 | `src/nbtriage/_model_runtime/failures.py`、`tools/nbtriage_maintainer/agent_evaluation.py`、`tools/nbtriage_maintainer/cli.py` |
| Runtime observation core | 校验传输无关的事件 / Matcher / API 生命周期摘要并按关联标识形成证据包 | NoneBot 适配器只能提交白名单标识；核心不导入框架类型；不调用模型、网络或外部工具 | 显式容量与 TTL 的单进程内存缓冲、累计丢弃计数 | `src/nbtriage/runtime_observations.py` |
| NoneBot runtime observer | 把事件 state 传播的关联 ID 与公共 event、run、API hook 压缩为核心观察 | 显式注册、采集错误 fail-open；不读取 Event 内容、身份、API data / result；不关联 Matcher 外 API | 观察器本地丢弃计数；观察本身进入核心 buffer | `src/nonebot_plugin_triage/nonebot_runtime.py` |
| Platform message reference index | 用 HMAC 精确绑定适配器、Bot、会话和消息引用 | 原始 scope / 引用只瞬时参与摘要；显式密钥、容量与 TTL；不持久化 | 摘要到 correlation ID 的单进程有界索引与丢弃计数 | `src/nbtriage/message_references.py` |
| Universal reference bridge | 用 UniSeg exporter 从任意受支持入站事件提取 Target 与 message ID | 不导入适配器事件类型；Target source 不进入稳定 scope；显式注册、fail-open | 桥本地丢弃计数；映射进入通用引用索引 | `src/nonebot_plugin_triage/universal_references.py` |
| OneBot V11 outgoing reference provider | 从 Matcher 内成功群发送结果提取运行证据 message ID | OneBot Adapter 由宿主安装注册，不是插件依赖或 extra；只在模块存在时延迟加载；缺失时仅停用此增强，不读取被回复正文或执行外部查询 | Provider 本地丢弃计数；映射只进入通用运行证据引用索引 | `src/nonebot_plugin_triage/onebot_v11_references.py` |
| Support Thread store / scope Turn coordinator | 保存首轮有界 request / Reply / correlation、已完成补充和当前待答问题；同 scope 下一条显式 `triage` 原子消费补充，让一个 scope 同时只有一个处理轮 | 有界内存、idle / absolute TTL；HMAC 绑定 adapter、Bot、场景和 actor；不依赖 Reply / Receipt，最多两次补充，终局或失败关闭，不跨重启 | `SupportThreadInitialContext`、scope lease 与 HMAC scope 索引；不保存邻近历史或原始平台身份 | `src/nbtriage/support/threads.py`、`src/nonebot_plugin_triage/support/threads.py`、`src/nonebot_plugin_triage/support/responses.py` |
| Maintainer conversation | 用一个 LocalStore JSON 原子快照保存 Pydantic AI 原生消息；所有 Bot、Adapter、入口和 SUPERUSER 共享历史，每轮注入当前场景；Harness 按上下文比例压缩；十五次请求与六十次工具保险丝，第十三次请求后最多提示一次收敛，第十五次请求动态 `tool_choice=none` | 每轮重鉴权；只读文件工具硬拒绝凭据类路径；全局只允许一个 Run，新请求忙时直接拒绝；停止保留快照，新对话取消后整体替换；不恢复后台调用栈 | `session_id`、时间戳与原生 `ModelMessage` 序列 | `src/nbtriage/behavior/conversation_agent.py`、`src/nonebot_plugin_triage/behavior/conversation_store.py`、`src/nonebot_plugin_triage/behavior/service.py`、`src/nonebot_plugin_triage/behavior/runtime.py` |
| Alconna triage entry | 每轮接收必选 `triage` 后的当前自由文本；Reply 可选；SUPERUSER 项目讨论使用全局维护者会话 | Semantic 联合读取当前文字、公开目录和有界 Reply / 补充上下文；普通 scope Thread 最多两次补充；维护者已有上下文可承接 unresolved / out-of-scope 续问；Reply ID 独立关联 runtime。所有轮次先过轻量 HMAC 限流；模型未配置或 transport / schema / Evidence 校验失败时安全降级 | 短期 Support Thread、全局维护者消息快照、本地公开能力 Provider 和维护者影子视图 | `src/nonebot_plugin_triage/handlers.py`、`src/nonebot_plugin_triage/support/intake.py` |
| Deployment-local capability shadow | 启动钩子后台生成字段级 Claim、Evidence、Constraint 和本地 FTS5 索引；每个已观察命令或 Matcher 保持为独立记录 | 默认启用；导入期不解析路径，扫描与构建在线程中执行；首次可服务 generation 发布前普通用户回退显式 Provider；普通用户只读派生 ServingView，SUPERUSER 鉴权后可定向检索未解决或受限记录；不做 handler 效果、跨 Matcher 角色或逐记录源码清单推断；LocalStore 解析或刷新失败保留上一索引或降级，普通视图另行拒绝 partial / stale | LocalStore 插件 cache 中可删除重建的 SQLite 派生数据与内存构建状态 | `src/nonebot_plugin_triage/capability/discovery/snapshot.py`、`src/nonebot_plugin_triage/capability/shadow.py`、`src/nbtriage/capability/catalog/records.py` |
| Public capability provider | 运行时说明显式登记且当前可见的公开能力，并向影子快照提供明确披露意图 | 未登记、`CommandMeta.hide=True`、停用或不可见能力失败关闭；不重跑 `parse()` 或执行命令 | 进程内 Provider 注册 | `src/nonebot_plugin_triage/capability/discovery/registry.py`、`src/nonebot_plugin_triage/capability/guidance.py` |

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
- 产品 Bug Agent 消费带 manifest 的独立版本化知识包，运行副本进入 LocalStore cache，基础 wheel / sdist
  不携带语料。NoneBot 文档由仓库采集器固定官方
  revision，运行检索绑定当前安装的 `nonebot2` 精确版本。现有 `knowledge-v2026.08.1` 与 stable catalog 已冻结，
  只包含 NoneBot 2.5.0 文档；启动后仍先恢复已验证 active 包，再后台检查该静态 catalog，使新安装能够取得
  冻结资产。任何下载或校验失败都保留旧包或退化为无该类证据，不阻断插件加载。见
  [ADR-0019](../adr/0019-distribute-rag-corpus-as-versioned-knowledge-pack.md) 与
  [ADR-0067](../adr/0067-refresh-knowledge-pack-from-stable-catalog-at-startup.md)、
  [ADR-0131](../adr/0131-freeze-the-nonebot-only-knowledge-pack.md)；
- 冻结知识包使用索引格式 2 / `knowledge-sqlite-fts5-jieba-v2`：文档与查询共用 jieba 搜索分词，
  点分 API 名保留完整形式并拆出组成部分，SQLite BM25 对定位标题与正文排序。组件、来源种类和版本仍由
  原文表过滤，Evidence 的原文、定位和哈希保持不变；教学文档工具仍返回最多三条。运行时继续按原算法读取
  格式 1 的 trigram 旧包，不在安装或查询时改写它。当前仓库只保留固定 NoneBot 快照采集、Markdown 分块、
  构建、评测、打包和验证代码，不维护 OpenAPI / TypeScript 等通用来源或自动发布工作流。未来若明确重开
  知识包维护，新资产必须作为新的 Evidence revision 重新评测；此边界不引入向量模型或模型重排；
- 格式 2 的 reader 另有 `identifier-context-v2` 查询排序修订：保留原 BM25 前两名，第三位最多补一个片段。
  同来源、同文件的直接父节若包含前两条缺少的中文问题词及相关 API，可以补回切块丢失的上下文；否则在
  snake_case / CamelCase 拆词查询的前 20 个候选中，优先选择主 API 标题下同时提及其他所问 API 的片段，
  没有这种候选时沿用拆词前三名内取首个未重复片段的规则。结果按 Evidence ID 去重；所有查询使用相同组件、版本和
  来源过滤，不修改语料、Evidence 哈希或索引格式；该修订仅需升级 reader，已有格式 2 包无需重建。格式 1
  仍走原算法。各片段 score 是所在查询的 BM25 分数，跨查询不可比较，调用方应保留返回顺序。
  框架文档评测复用生产 reader，按前 3 条 / 每条 1800 字符核对答案事实，并独立记录排序修订与代码摘要；
- 能力影子 SQLite 是 LocalStore 插件 cache 中可删除重建的部署本地派生数据，不再暴露路径配置，也不进入
  Git 或发行物；导入期不解析 cache，启动刷新失败不会阻止插件或模型语义分流；首次可服务 generation 发布前
  普通用户回退显式 Provider；带 `analysis_issues` 的记录只有维护者显式检索时返回，`restricted` 会持久化但
  只有模型外上下文鉴权通过后才能检索；后续 operator exclude policy 将负责在持久化前完全排除指定能力，
  当前尚无这个按能力排除接口；
- `evals/curation/batches/` 保存人工晋级批次，`evals/curation/annotations/` 保存可复建的人工结论；二者不复制原始 Issue 正文；
- `evals/datasets/catalog/`、`evals/datasets/fixtures/` 与 `evals/datasets/splits/` 保存可审查输入、合成安全集合和冻结切分；
- `evals/oracles/` 保存 schema v2 Oracle 历史声明，以 Case / Oracle 规范化版本与 Probe 原始字节 SHA-256 绑定引用校验，可作为回归合同复建依据；它没有原始 stdout、退出码、统一 Runner 回执或外部签名，不能单独证明 Probe 实际执行；完整机器报告已迁入本地 `reports/`，`evals/` 不再保存运行快照；
- `curation.field_provenance` 为每个资格字段记录 `source.body`、`gold.comment.<id>` 或策展推断来源；Gate 不接受没有来源标记的完整字段；
- `visibility_boundary` 固定为目标 Issue 的 `opened_at`；当前 GitHub API 无法证明 Issue 正文未在后来编辑，因此 schema 明确记录 `body_edit_history_unavailable`，不能把当前正文误称为严格历史快照；
- `evals/datasets/splits/data-gate-v1.json` 按 `opened_at` 建立 train / validation / held-out 时间窗；相同根因簇、重复 / 回移植和相同 Oracle 引用必须留在同一 split；
- `artifacts/sessions/` 保存本地会话状态、白名单化脱敏回执摘要及其内容地址、审批与结果引用；不复制 Issue 正文、原始日志或配置值，当前文件适配器不提供多进程并发写入协调；
- `artifacts/answer-quality/<evaluation-id>/` 保存从真实 B4 固定合成集导出的候选、待完成或已完成人工标注与离线质量报告；它不属于插件实例状态或生产数据，文件默认拒绝覆盖；
- `artifacts/` 与 `reports/` 整体是本地运行输出；旧 MLflow 目录、数据库和 WAL/SHM 的忽略保护继续保留，防止残留工件进入 Git。未来 run 记录应引用 Git 中 `evals/` 合同的内容哈希或 revision；
- `RuntimeObservationBuffer` 当前只保存进程内最小化标识；领域构造器要求显式容量和 TTL，NoneBot 部署层
  默认使用 10,000 条和 900 秒，容量或过期淘汰计数进入证据包；当前不提供崩溃恢复；
- `NoneBotRuntimeObserver` 的关联 ID 只存在于 NoneBot event / Matcher state 和上述缓冲；hook 采集失败只增加观察器本地丢弃计数，不中断 Bot，Matcher 外 API 当前不记录；
- `PlatformMessageReferenceIndex` 只保存 HMAC 摘要、correlation ID 与存入时间；原始 Target / Bot / actor / message scope 只在调用栈中出现；进程重启后密钥和索引一起丢失，跨 Worker 与历史回复尚不支持；
- `SupportThreadTurnCoordinator` 只在单进程内为同一 adapter、Bot、conversation 与 actor 保存首轮规范化请求、
  直接 Reply 的可见正文和不透明 correlation ID，并允许后续显式 `triage` 消费最多两次补充机会；它不保存
  邻近聊天历史，超时、终局、额度耗尽、发送失败或进程重启都会结束 Thread；
- `PublicCapability` 是当前显式注册表面向回答层的最小进程内投影；部署本地 `CapabilitySnapshot` 另外保存候选事实、来源和分析问题，两者都不保存或重放用户命令解析结果；
- `AgentRunState` 只保存领域 action、规范化 observation、短摘要、引用、usage、pending interruption、停止原因与可选的脱敏终态失败分类；它不保存异常文本、Provider body/header、Pydantic AI message history、Fixture Gold、原始日志、秘密或私有 Chain-of-Thought；
- 所有本地生成工件默认 Git 忽略；`evals/` 只版本化经过审查的评测合同、人工判断和可复建 Oracle 结论，完整机器运行输出与历史报告不进入发布包或 Git。目录职责见 [ADR-0015](../adr/0015-separate-versioned-evals-from-local-runtime-data.md)，收紧后的发行边界与通过 pytest 进入 CI 的确定性评测回归分别见 [ADR-0016](../adr/0016-keep-maintainer-evaluation-tooling-out-of-install-surface.md) 和 [ADR-0017](../adr/history/0017-run-deterministic-evaluations-through-pytest.md)。

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
- NoneBot 模型配置不接受 API Key，但允许部署者配置经过约束的 Base URL，也不提供独立的产品启用开关；
  配置通过 Pydantic AI `provider:model` 唯一选择 transport，旧 backend 字段会被拒绝；未配置 model 时不导入 Provider，
  semantic、教学注释与 Answer 子服务会 unavailable，但完整插件仍能启动并保留确定性能力索引。semantic assessment 与 Bug
  assessment 分别使用独立的任务评测表。当前中文 semantic v8 Prompt v5 与 Bug Prompt v23 的资格集合均为空；
  历史 Prompt 的结果不能继承。模型传输能力与结构化输出默认方式由 Pydantic AI ModelProfile 拥有。旧 B1
  各任务 `QUALIFIED_*_TASKS` 只记录精确评测历史；质量结论不能互相继承，未登记组合仍可运行；
- 后续 G2 / G3 执行必须进入独立可销毁 Runner，不能在控制面或真实 QQ Bot 进程中安装插件。
- 当前 15 条内容一致的历史声明记录了人工审计 detached worktree 的结果：包级探针据称使用 `uv run --isolated` 与目标 lockfile，源码提取探针据称只编译目标函数 / 模型 / 迁移体并注入内存替身。现有 schema 没有保存可重算的进程回执，因此这些记录不能升级为执行真实性证明；即使后续补齐本地回执，该边界也不是容器级隔离，不能推广到任意商店插件。

## 质量与演进检查点

教学 fixture 评测的通用执行层使用维护者依赖 Pydantic Evals 2.28.0：原生 Dataset / Case / Evaluator /
CaseLifecycle 负责串行运行、显式重复及异常记录，现有领域评分负责教学语义与质量门槛。全部重复共享费用
预算，费用达到阈值或未知后不再发起后续用例；普通任务失败和评分器故障分开统计。框架默认平均分不替代
项目通过率，未执行或评分器故障使实验不完整。CLI 保存本地业务报告，没有第二套正式报告。
`replay-capability-teaching` 使用报告中的请求、模型输出和已生成投影运行当前评分器，不调用模型、工具或
源码准备，也不重新投影；保存来源摘要与评分版本，复评不产生独立冷测或模型资格。历史报告缺少证据或
请求 revision 不兼容时拒绝复评。真实 NoneBot 宿主冷测继续使用原来的 preflight/run 入口。
确定性回归仍通过现有 pytest / CI 执行；不自动运行付费评测。MLflow 依赖、专用发布器和服务器启动入口已按
[ADR-0125](../adr/0125-remove-mlflow-tracking-from-maintainer-evaluations.md) 移除，结果追溯与复评使用本地 JSON。

版本化评测合同保留 Data Gate、B0/B1 基线、S3 安全拒绝、B3 审批补证和 B4 有界 Agent 的独立 Fixture、
split、rubric 与资格门。模型质量只绑定精确的模型、Provider、Prompt、Schema、工具和数据投影 revision；
旧 Prompt、单次 smoke 或 scripted run 的结果不能继承为当前组合的质量结论。

公开 CI 只运行可重复、无网络、无凭据的测试合同。真实 Provider 运行、费用、延迟、partial audit 和机器报告
保存在本地 `reports/`，并由对应评测合同引用；一次运行的精确 token、耗时和通过数量不属于稳定
架构事实。当前 revision 的质量结论必须由当前代码重新执行 lock、lint、type、test 和构建检查得出，不能从
本页历史文字继承。

稳定流程入口：B3 会话、单步补证和结构化回执见[支持会话流程](flows/support-session.md)；运行观察、
确定性入口分流及 Alconna 能力发现分别见
[运行观察流程](flows/runtime-observation-intake.md)、[支持入口分流](flows/support-intake-routing.md)和
[Alconna 能力发现](flows/alconna-capability-discovery.md)。真实 NoneBot 观察、OneBot 引用、
跨平台报障边界见[运行观察流程](flows/runtime-observation-intake.md)、
[OneBot V11 引用流程](flows/onebot-v11-reply-reference-correlation.md)、
[跨平台显式报障入口](flows/cross-platform-report-intake.md)。
部署本地能力候选、来源与索引边界见[能力影子索引](flows/capability-shadow-index.md)。
