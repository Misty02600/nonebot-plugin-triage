# NoneBot Triage Agent 架构概览

## 目标与边界

NoneBot Triage Agent 把模糊报障转换为证据可追溯的 `SupportCase`，再选择补问、检索、确定性探针、隔离复现或升级。核心不承诺自动解决全部 NoneBot / QQ 问题，也不把 Issue 分类、摘要或聊天外壳当作主要差异。

当前实现覆盖 Data Gate 的只读离线链路、B0 确定性评测、已冻结的 B1 RAG-only 基线和 B3 可审计会话切片：公开 GitHub Issue 发现与采集、输入 / Gold 隔离、时间线 / PR / commit 引用、可版本化人工标注、Case 草稿、完整性评估、人工启动的逐 Case Oracle Probe，以及只读公开输入的检查表 / 检索基线。B1 增加 train-only 检索证据包、严格结构化模型边界和响应缓存；历史 DeepSeek 直接 SDK 路径已完成 validation 与 held-out 正式评测，新的 Pydantic AI 层则用 OpenAI Responses、DeepSeek Responses 与 Anthropic Messages 假 HTTP 验证同一 B1 native schema 契约。OpenAI 与 Anthropic 以语义独立的公开 extra 和身份记账；DeepSeek 只保留仓库维护者评测栈，三者均未因离线通过而升级为正式支持。OpenCode Go 的兼容 Chat spike 仅保留为 `tests/support/opencode_go_backend.py` 中的 evaluation-only 测试夹具，用于离线验证 wire 与 usage 失败关闭；它不进入 wheel、公开 extra、CLI、插件配置或 Provider 资格。NoneBot 已公开默认关闭的窄模型配置和惰性 step-client factory；当前资格表为空，安装实验性产品 extra 或填写组合都不会让插件调用模型。B3 把冻结预测映射为固定动作、持久化状态与事件；补证动作每轮只从模型候选中选择一个槽位，接收白名单化脱敏回执后再从剩余冻结候选重规划，执行型动作则必须显式审批后才能关联已有 Oracle 结论。面向真实入口已经实现传输无关运行观察、`triage <自然语言>` 的 Alconna / UniSeg 支持入口、确定性首轮意图和显式公开能力 Provider；丰富 Alconna AST 快照与解析回执仍是仓库实验。NoneBot 2.5 公共 hook 用事件 state 贯穿关联 ID；UniSeg 统一入站 Reply / Target，HMAC 索引关联近期消息，OneBot V11 Provider 补齐 Bot 出站消息引用。只有疑似故障建立短期 `LiveIncident`；入口已有场景过滤、限流、窄回显与 `SUPERUSER` 白名单摘要查询。模型资格表仍为空，模型 Agent、诊断重评估、自动化隔离 Runner、服务端数据库、普通用户工单查询与 GitHub 写回尚未进入运行入口。

默认关闭的部署本地能力影子索引已经进入运行包：它在启动时只读收集已加载 Plugin、Matcher、Alconna、
安装来源和可变源码摘要，原子生成带 `public / review / restricted` 披露态的本地 FTS5 候选库。
`restricted` 保存 SUPERUSER 与内部管理能力，但只允许模型外鉴权后的查询路径读取；第一阶段仍只供维护者
检索和覆盖审计，不接入群聊回答。

B4 已增加 Provider 无关的有界 Agent control plane：模型可在白名单运行观察、train-only 检索、结构化
补证和最终诊断间动态选择；领域 runtime 掌握跨步预算、二次授权、暂停恢复和 trajectory，Pydantic AI
只处理每个步骤唯一 `propose_action` 信封的原生 tool schema 与协议响应；信封中的 action 联合按 capability、
trajectory 与已观察 citation 动态收窄。OpenAI / DeepSeek Responses 与 Anthropic Messages 已有
假 HTTP B4 合约；DeepSeek Responses 不声明供应商 strict，参数仍由 Pydantic 与领域 schema / 动态白名单
在本地复核。OpenCode Go 兼容 Chat 只在测试夹具中验证相同的传输与计费失败语义，不属于产品适配层。
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

首个真实用户入口面向独立 NoneBot 部署者：在 Bot 进程中安装入口插件，由群聊或频道用户发送
`triage <求助内容>`；`@Bot` 和 Reply 可选。疑似故障带 Reply 时，入口再把求助与本机事件、实际运行过的 Matcher、插件 / 模块、平台 API 调用、异常和
版本证据关联，之后转换成传输无关的 `SupportCase` / `SupportSession`。普通群员不能查询任意日志；原始群聊和
日志默认不上传、不长期保存；Probe、GitHub 写回和其他副作用仍由维护者审批。为尽快进入真实使用，当前还
提供默认关闭的 observation-first trial：只对疑似故障产生的 incident 记录脱敏生命周期、查询曝光和维护者枚举反馈，
本地 JSONL 有界轮转，写入失败可见但不阻断报障；模型 shadow 与 canary 仍是后续独立晋级阶段。

该方向的核心不是“用 LLM 从群聊识别 Bug”。调研已发现 AstrBot BugCatcher 覆盖静默监听、LLM 识别、
去重与 Dashboard，NoneBot 也已有 Sentry 错误跟踪。NoneBot Triage Agent 的产品边界保持在“显式支持分流、
疑似故障与运行证据关联、NoneBot 责任层定位、最小补证和可审计验证”。长期决策见
[ADR-0001](../adr/0001-qq-group-report-linked-runtime-evidence.md)，竞品证据见
[产品定位与同类能力](product-positioning.md)。

修复闭环采用责任层路由与分级自治：L0 观察、L1 建议、L2 配置 / 生命周期修复、L3 维护者授权的上游
协作、L4 本地或维护者拥有插件的隔离代码修复。模型不直接持有 Shell、配置或 GitHub 写权限；高权限动作
进入专用执行器并保持逐动作审批。当前只交付 L0 的纯核心观察契约和既有 L1 控制面基础，其余均为规划
能力。完整边界见 [ADR-0002](../adr/0002-tiered-autonomy-and-ownership-aware-remediation.md)。

同一个显式入口承接能力导航、指令纠错和故障报障，但先用独立 `IntakeDisposition` 决定教学、纠错、诊断、
说明范围或拒绝；只有疑似故障进入技术 `ResponsibilityLayer`。MVP 不代用户执行有副作用指令，未来能力
注册表先覆盖 Alconna。当前已实现严格结构信号、固定优先级路由、显式公开能力 Provider，以及
`on_alconna + MultiVar + OriginalUniMsg + MsgTarget + UniMessage` 的跨平台 `triage` 入口。`triage` 必选，
`@Bot` 与 Reply 可选；另有默认关闭的本地影子索引从已加载插件生成带来源的
`public / review / restricted` 快照，作为后续本地 RAG 的候选事实层。系统不使用 `hidden` 披露态；按能力
完全排除将由后续独立 operator exclude policy 在持久化前处理，当前尚无这个接口。当前只有确定性首轮
分流，模型 Agent 尚未获得运行资格。统一入口决策见
[ADR-0003](../adr/0003-unified-capability-guidance-and-incident-intake.md)，跨平台边界见
[ADR-0006](../adr/0006-cross-platform-alconna-entry-and-reference-providers.md)，当前入口语义见
[ADR-0020](../adr/0020-use-triage-command-for-natural-language-support.md)，能力影子边界见
[ADR-0021](../adr/0021-use-deployment-local-capability-shadow-index.md)。

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
| `just maintainer gate` | 评估 Case 是否具备公共字段和模式特有证据，并核对版本化 Oracle 运行结果 | 生成本地 JSON 报告；不修改 Case；引用不一致的运行结果无效 | `tools/nbtriage_maintainer/gate.py`、`tools/nbtriage_maintainer/runtime_results.py` |
| `just maintainer summarize-trials` | 严格读取当前 trial JSONL 与有界轮转备份，输出无标识的运营窗口摘要 | 按 event ID 去重；损坏、冲突、超长、截断或未知版本只计数；不输出原事件、失败形状或任何 incident / trial / event / cluster ID | `src/nbtriage/live_trials.py`、`tools/nbtriage_maintainer/cli.py` |
| `just maintainer evaluate-b0` | 在冻结 split 上运行固定检查表、规则路由和 train-only 相似 Case 检索 | 预测只读公开 Issue 输入；Gold 只进入评分器；不调用模型或外部工具 | `src/nbtriage/baselines.py`、`tools/nbtriage_maintainer/evaluation.py` |
| `just maintainer evaluate-s3` | 在独立合成 Fixture 上比较冻结 B0 与 B1 模型前安全拒绝 | 不读取真实秘密或生产数据；不检索、不调用模型、不调用外部工具 | `src/nbtriage/safety.py`、`tools/nbtriage_maintainer/safety_evaluation.py` |
| `just maintainer build-bot-docs-index` | 从外部 `bot-docs` 的批准子集构建本地 SQLite FTS5 派生索引 | 不修改源目录或 vendor 独立 Markdown 副本；目标不得位于 `bot-docs` 内；已有索引只在显式 `--replace` 时原子替换 | `tools/nbtriage_maintainer/bot_docs.py`、`tools/nbtriage_maintainer/cli.py` |
| `just maintainer search-bot-docs` | 用 metadata 或 hybrid 策略检索项目事实、工程配方和当前 OneBot API 文档 | 只读本地索引；返回文件哈希、修订、标题和精确版本；不调用网络、模型或工具 | `tools/nbtriage_maintainer/bot_docs.py`、`tools/nbtriage_maintainer/cli.py` |
| `just maintainer search-capabilities` | 检索部署启动时生成的本地能力影子索引 | 默认只返回 `public`；`--include-review` 纳入待复核候选；带外确认授权后可用 `--include-restricted`，该开关不自行鉴权；不调用模型或能力代码 | `src/nbtriage/capabilities.py`、`tools/nbtriage_maintainer/cli.py` |
| `just maintainer evaluate-bot-docs-retrieval` | 在 25 条公开合成问题上比较 metadata 基线与 hybrid 检索 | 固定 Recall@5 / MRR / 来源完整率合同；报告写本地忽略目录；0 模型和外部工具调用 | `tools/nbtriage_maintainer/bot_docs_evaluation.py`、`evals/datasets/fixtures/bot-docs-retrieval-v1.json` |
| `just maintainer evaluate-b1-openai` | 用 train-only 证据和一次 Responses 原生 JSON Schema 运行 validation 或 heldout | 需要 `model-openai` extra；必须显式模型、输出 / 调用上限和付费确认；Pydantic AI Direct Request 仍按 Case 串行；请求关闭存储、工具、遥测和自动重试，但不声称零数据保留；响应按完整请求缓存 | `src/nbtriage/rag.py`、`src/nbtriage/model_adapters.py`、`src/nbtriage/openai_adapter.py`、`tools/nbtriage_maintainer/evaluation.py` |
| `just maintainer evaluate-b1-deepseek` | 用 DeepSeek V4 Flash 非思考模式运行同一 B1 契约 | 只接受 `deepseek-v4-flash`；固定 `reasoning=none`、`temperature=0`；使用独立密钥、缓存和报告 | `tools/nbtriage_maintainer/cli.py`、`tools/nbtriage_maintainer/providers.py` |
| `just maintainer evaluate-b3-evidence-policy` | 在 B1 validation 输出上冻结单步补证策略 | 只接受 validation-only 报告；不调用模型或工具；现有 held-out 被拒绝 | `tools/nbtriage_maintainer/evidence_policy.py`、`tools/nbtriage_maintainer/evidence_policy_evaluation.py` |
| `just maintainer evaluate-b3-evidence-receipts` | 在纯合成 Fixture 上验证结构化回执守门和请求绑定 | 只评估白名单 schema、脱敏、疑似 secret 与错绑；不判断证据真伪；0 模型 / 工具调用 | `src/nbtriage/evidence_receipts.py`、`tools/nbtriage_maintainer/evidence_receipt_evaluation.py` |
| `just maintainer export-answer-quality-review` | 把完整真实 B4 报告中 `forward_hidden` 的完成态候选导出为本地人工评审包 | 只接受 schema v3、纯合成、真实模型多 trial B4 报告并核对 Fixture/split 哈希；只复制领域层规范化证据事实，不复制 Gold、Prompt、消息历史、原始日志或 Provider 响应；输出拒绝覆盖 | `tools/nbtriage_maintainer/answer_review_export.py`、`tools/nbtriage_maintainer/agent_evaluation.py` |
| `just maintainer evaluate-answer-quality` | 用四轴 0–2 人工 rubric 汇总固定 `answer + citations` 标注 | 默认合成校准只验证评分锚点；候选质量必须来自真实 B4 的 `forward_hidden` 多 trial 报告、使用独立人工复核，并同时通过来源 B4 Gate、均值、逐样本和关键零分硬门；结果只属于 `offline_fixed_fixture`，不构成生产质量证据；非校准报告拒绝覆盖 | `tools/nbtriage_maintainer/answer_quality_evaluation.py`、`evals/rubrics/answer-quality-v1.json`、`evals/datasets/fixtures/answer-quality-calibration-v1.json` |
| `just maintainer evaluate-b4-scripted` | 用 scripted model 在冻结 regression / forward-hidden split 上验证动态 action、预算、暂停恢复、轨迹评分和 Gold 隔离 | 0 真实 Provider 请求、0 外部工具调用；报告记录 Prompt/schema/policy/source revision 与结构化输出通过率，但明确不具备晋级资格 | `src/nbtriage/bounded_agent.py`、`tools/nbtriage_maintainer/agent_evaluation.py`、`evals/datasets/fixtures/b4-bounded-agent-v1.json`、`evals/datasets/splits/b4-gate-v1.json` |
| `just maintainer evaluate-b4-real` | 在明确付费/出站授权后，让同一 Provider/model 多 trial 对照 B1、B3 与 B4 | 支持 DeepSeek / OpenAI Responses 与 Anthropic Messages；只用 forward-hidden 指标判断晋级；B1/B4 后验结构拒绝计入 trial，未知费用仍中止；每次请求前/响应后更新 partial audit，success/partial 路径禁止覆盖；仍无完整质量报告或 Provider 资格 | `tools/nbtriage_maintainer/agent_evaluation.py`、`tools/nbtriage_maintainer/cli.py`、`evals/datasets/splits/b4-gate-v1.json` |
| `just maintainer publish-evaluation-mlflow` | 维护者把已经落盘的评测 JSON 发布到显式 MLflow experiment 以比较迭代；不属于插件安装接口 | MLflow 只持有可查询副本，不参与评测；按报告与终态 audit 摘要幂等；真实 B4 成功报告缺少完成态 audit 时拒绝；默认写本机 `127.0.0.1` | `tools/nbtriage_maintainer/mlflow_tracking.py`、`tools/nbtriage_maintainer/cli.py`、`docs/adr/0016-keep-maintainer-evaluation-tooling-out-of-install-surface.md` |
| `just maintainer session-*` | 从冻结 B1 预测创建、接收脱敏回执、审批、关联已有 Oracle 结果并查看支持会话 | `needs_evidence` 只接收当前槽位并从剩余候选重规划；`verify` 未显式审批不能附加结果；不执行代码或外部写入 | `tools/nbtriage_maintainer/sessions.py`、`tools/nbtriage_maintainer/cli.py` |
| `RuntimeObservation` / `RuntimeObservationBuffer` | 接收 NoneBot 观察桥提交的最小化事件、Matcher、插件、API 与异常标识，并按关联 ID 生成证据包 | 不接收消息正文、用户 / 群 ID、API 参数或结果；容量与 TTL 必须由调用方显式给出；仅单进程内存 | `src/nbtriage/runtime_observations.py` |
| `NoneBotRuntimeObserver` | 显式注册 NoneBot 2.5 公共 hook，用事件 state 关联 event、实际 Matcher 与其内部 API 生命周期 | fail-open；只读取框架 / 插件标识和异常类 / 栈模块；Matcher 外 API 不猜测归属 | `src/nonebot_plugin_triage/nonebot_runtime.py` |
| `UniversalReferenceBridge` / `PlatformMessageReferenceIndex` | 通过 UniSeg Target / message ID 统一绑定入站消息，并以带密钥摘要短期关联 correlation ID | 原始适配器 / Bot / 会话 / 消息 ID 只瞬时参与 HMAC；不保存正文；显式容量与 TTL | `src/nonebot_plugin_triage/universal_references.py`、`src/nbtriage/message_references.py` |
| `OneBotV11OutgoingReferenceProvider` | 从 Matcher 内成功的 OneBot 群发送结果补齐 Bot 输出引用 | OneBot 是可选依赖；只读路由字段和结构化 message ID；不保存完整 API data / result | `src/nonebot_plugin_triage/onebot_v11_references.py` |
| Alconna `triage` Matcher / support intake adapter | 接收必选指令后的自由文本；`@Bot` 与 Reply 可选；能力问题直接说明，未知请求澄清 | 文本只瞬时用于首轮分流；私聊拒绝；公开能力必须显式登记；不执行用户文字或任意命令解析 | `src/nonebot_plugin_triage/handlers.py`、`src/nonebot_plugin_triage/support_intake.py` |
| `LiveReportService` | 只在疑似故障分支建立最小 `LiveIncident`；Reply 命中时关联近期运行 bundle，缺失或未命中时保留空证据 | HMAC scope 限流；无模型、网络、Probe 或外部写入 | `src/nonebot_plugin_triage/live_reports.py` |
| `NBTriageConfig` / `NBTriageModelService` | 公开默认关闭的模型 backend、精确模型、timeout 与输出 token 配置，并为每个模型步骤创建单次客户端 | 禁用时不导入 Provider；密钥只读厂商环境变量；当前资格表为空，真实启用失败；没有 endpoint、工具或 Matcher 调用配置 | `src/nonebot_plugin_triage/config.py`、`src/nonebot_plugin_triage/model_runtime.py`、`src/nonebot_plugin_triage/runtime.py` |
| `IncidentQueryService` / Alconna query Matcher | 让维护者按不透明受理编号查看短期白名单摘要，并识别活动 TTL 内的相似显式报障 | `SUPERUSER` 在读取前守门；cluster 只基于最小失败标识且不代表底层异常总数；不返回聊天、平台身份、correlation ID、API 参数或任意日志 | `src/nbtriage/live_incidents.py`、`src/nbtriage/incident_queries.py`、`src/nonebot_plugin_triage/incident_queries.py`、`src/nonebot_plugin_triage/handlers.py` |
| `LiveTrialService` / trial Matchers | 为已受理 incident 建立 observation-only trial，记录查询曝光、revisioned 枚举反馈和活动聚合 | 默认 off；observe 必须有本地轮转 JSONL sink；只保存最小失败形状和计数，写入失败计数但不改变受理；反馈 / 统计要求 `SUPERUSER`；零模型 / 工具 / 外部写入 | `src/nbtriage/live_trials.py`、`src/nonebot_plugin_triage/trials.py`、`src/nonebot_plugin_triage/live_reports.py`、`src/nonebot_plugin_triage/handlers.py` |
| `build_reply_report_signals` / `build_unlinked_report_signals` | 分别把已命中的引用或无证据故障转为确定性入口信号 | 两者都要求上游已判定用户在报告问题；没有明确失败时不把生命周期成功误称为行为成功；不调用模型 | `src/nbtriage/reply_reports.py` |
| `parse_intake_signals` / `route_intake` | 把受信边界产生的显式触发、意图、相关性、命令解析、运行与安全信号分流为教学、纠错、疑似故障、无关或危险 | 不接收文本、命令原文或身份；危险优先、解析错误先纠错、矛盾或不足只补问；不调用模型或工具 | `src/nbtriage/intake.py` |
| 公开能力 Provider / 部署本地能力影子 | 运行入口只解释显式登记的公开命令；可选影子索引自动观察已加载的 Alconna、普通 Matcher、被动能力与插件来源 | 运行回答过滤未登记、`CommandMeta.hide=True`、停用和可见性失败能力；影子候选默认 `review`，SUPERUSER 与内部管理能力为 `restricted`，不重跑 `parse()`、Rule、Permission 或 handler | `src/nonebot_plugin_triage/support_intake.py`、`src/nonebot_plugin_triage/capability_snapshot.py`、`src/nbtriage/capabilities.py` |
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
                 └─→ adapter outgoing Provider ───────────┴─→ keyed reference index

[optional @Bot] triage + free text → MsgTarget → deterministic first-pass intent
                 ├─→ capability / unknown → public capability or one clarification → UniMessage
                 └─→ suspected incident → rate limit
                          ├─→ Reply hit → keyed reference index → evidence bundle ─┐
                          └─→ no/missed Reply → empty evidence ────────────────────┤
                                                                                  ↓
                                                         short-lived LiveIncident → UniMessage receipt
SUPERUSER query Matcher ─→ exact incident ID ─→ whitelisted IncidentSummary ─────→ UniMessage receipt
                 └─→ observe trial → local rotating JSONL + summary_viewed event
SUPERUSER feedback/stats ─→ enum feedback / active aggregate ─────────────────────→ UniMessage receipt

triage request text → controlled intent boundary → trusted minimal signals → intake router
                         ├─→ guidance / correction / refuse
                         └─→ suspected incident → future SupportCase

explicit public Alconna provider → deterministic guidance formatter
registered Alconna AST → repository-only rich capability snapshot
existing Arparma ─────→ minimal parse receipt ─────────→ trusted command_status
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
| Runtime result validator | 核对运行状态、Probe、故障 / 修复引用和两侧 Oracle 命中 | 只读版本化结果；不执行第三方代码 | Oracle 运行结论 | `tools/nbtriage_maintainer/runtime_results.py` |
| B0 predictor | 抽取版本值和证据状态，给出固定补问、症状 / 阶段 / 责任层与路由 | 只读 `source` 和仓库身份；train-only 检索；不接触 `curation` | 无长期状态 | `src/nbtriage/baselines.py` |
| Evaluation harness | 加载冻结 split、隔离预测与 Gold、计算分层指标并写报告；不进入发行包 | 不修改 Case；历史 S3 无分母时不伪造样本，改由独立合成评测补充 | 评测报告 schema v1 | `tools/nbtriage_maintainer/evaluation.py`、`tools/nbtriage_maintainer/safety_evaluation.py` |
| Safety pre-model guard | 识别目标 Case 中明确请求越过凭据、控制面、生产、账号、私密数据或外部写入边界的组合 | 只读公开 `source`；命中后不检索、不读缓存、不调用模型；不能替代副作用入口授权 | 风险类别与拒绝预测 | `src/nbtriage/safety.py`、`src/nbtriage/rag.py` |
| B1 RAG-only runner | 生成有界目标输入和 train-only 证据包，异步校验版本 / 枚举 / 引用并缓存响应 | 检测到疑似秘密时在模型前停止；非法输出不写缓存；不暴露工具；不拥有事件循环 | 本地忽略的响应缓存 | `src/nbtriage/rag.py` |
| bot-docs local retriever | 对批准的 facts / recipes / OneBot Adapter 2.4.6 API 文档做标题感知分块、全文检索和逐文件结果去重 | 源文档归外部 `bot-docs` 所有；legacy NapCat / NoneBot2 不进入索引；当前不被 B1、B4 或 NoneBot 入口调用，基础发行包不携带索引 | 本地忽略的 SQLite 索引与评测报告；未来产品知识包独立版本化 | `tools/nbtriage_maintainer/bot_docs.py`、`tools/nbtriage_maintainer/bot_docs_evaluation.py`、[ADR-0019](../adr/0019-distribute-rag-corpus-as-versioned-knowledge-pack.md) |
| Pydantic AI Direct Request / OpenAI adapter | 把通用 B1 请求映射为 Responses 原生 JSON Schema，并按 profile、Provider/model 身份和一次调用预算失败关闭 | 只由 `model-openai` extra 安装；三类 tools 为空、instrumentation 与存储关闭，仓库维护者 CLI 已使用该 factory；API Key 只读环境，不外推为零数据保留 | 无长期状态 | `src/nbtriage/model_contracts.py`、`src/nbtriage/model_adapters.py`、`src/nbtriage/openai_adapter.py` |
| Anthropic Messages adapter | 用同一 B1 请求与输出契约映射官方 Messages native `output_config.format`，验证领域层不依赖 Responses 专属语义 | 只由 `model-anthropic` extra 安装；SDK 重试为零，无自定义 endpoint、tools、fallback、CLI 或插件触发；离线通过只标记实验性 | 无长期状态 | `src/nbtriage/anthropic_adapter.py`、`tests/test_model_adapters.py` |
| NoneBot model runtime boundary | 把公开配置映射为 code-level 资格门和惰性 step-client factory | runtime 持有 factory 而非长期累计调用客户端；每个 step 固定 `max_calls=1`；禁用路径不解析密钥或导入 SDK，当前无真实合格组合 | 无长期状态；API Key 只存在于进程闭包与新建 SDK 客户端 | `src/nonebot_plugin_triage/model_runtime.py`、`src/nonebot_plugin_triage/config.py` |
| DeepSeek Responses adapters | 历史直接 SDK 维护命令保留冻结 B1 基线；专用 Pydantic factory 为真实 B4 harness 同时提供 B1 native JSON Schema 与 B4 deferred tool step | 仓库 `maintainer` group 固定官方 endpoint、显式 `DeepSeekProvider`、`deepseek-v4-flash`、`reasoning=none`、`temperature=0` 和零 SDK retry；没有插件 extra；滚动别名未获线上资格；Provider `strict=false` 时仍做本地双层参数验证 | 无长期状态 | `tools/nbtriage_maintainer/providers.py`、`tools/nbtriage_maintainer/deepseek_adapter.py`、`tools/nbtriage_maintainer/cli.py` |
| OpenCode Go evaluation test fixture | 用兼容 Chat 的假 HTTP spike 验证 renderer、请求次数、身份与 cache usage；真实模型证据从单工具 smoke 扩展到四工具多调用反例和单一 typed action 信封的两步 control | 只服务测试；信封 control 不覆盖首次约 388.7 秒的未知历史，也不进入 wheel、公开 extra、CLI、插件配置、正式 Gate backend 或 Provider 资格 | 无长期状态；历史机器记录仅在维护者本地保留 | `tests/support/opencode_go_backend.py`、`tests/test_agent_provider_adapters.py` |
| Provider response usage / identity | 从 Pydantic AI 响应提取 Provider、model、request ID 与可选 fingerprint，并按返回身份归一化 microUSD | 返回 Provider 不匹配或模型漂移时不回退请求侧价格；身份缺失可记录但真实 Gate 不得晋级 | 无长期状态 | `src/nbtriage/model_usage.py`、`src/nbtriage/model_adapters.py`、`src/nbtriage/pydantic_agent_adapter.py` |
| Evidence request policy | 按故障阶段把 B1 多槽位候选收缩为当前轮唯一问题 | 只用于维护者离线评测与会话；只能选择模型候选；空候选失败；validation 冻结后等待前向隐藏集 | validation 策略工件 | `tools/nbtriage_maintainer/evidence_policy.py`、`tools/nbtriage_maintainer/evidence_policy_evaluation.py` |
| Evidence receipt contract | 把九类补证限制为已脱敏、字段白名单化的结构摘要和原始材料指纹 | 拒绝任意额外字段、疑似 secret、错绑和不完整摘要；不读取原始材料 | 合成 Fixture 与冻结守门报告 | `src/nbtriage/evidence_receipts.py`、`tools/nbtriage_maintainer/evidence_receipt_evaluation.py` |
| Answer review exporter / rubric evaluator | B4 schema v3 先保留完成态 `answer + citations` 和白名单化 review context；导出器再把真实多 trial 的 `forward_hidden` 候选转换为待人工评分的固定集，评分器按 groundedness、completeness、limitation awareness 和 overclaim control 四轴汇总 | Gold 只在模型运行后生成评审要点；草稿标注明确不可评分；通过结果只属于 `offline_fixed_fixture`，结构化 B4 Gate 和未来部署 shadow/canary 均不能被它替代 | 版本化 rubric、合成校准 Fixture与校准标注；候选评审包和完整报告写入本地 `artifacts/` 或显式 MLflow | `tools/nbtriage_maintainer/answer_review_export.py`、`tools/nbtriage_maintainer/answer_quality_evaluation.py`、`evals/rubrics/answer-quality-v1.json`、`evals/curation/answer-quality/calibration-v1.json` |
| Support session control plane | 把 B1 route 映射为固定动作，约束回执、重规划、审批与结果附加的合法状态变化 | 读取冻结报告、合格回执和 Runtime validator 结论；不读取 Issue 指令执行工具 | 本地会话 JSON、预测报告哈希、脱敏回执摘要与顺序事件 | `tools/nbtriage_maintainer/sessions.py` |
| B4 bounded Agent runtime | 拥有循环、按 capability / 已观察轨迹收缩 action 白名单、参数二次校验、跨步预算、observation 执行、暂停恢复和稳定停止原因 | 只读取既有 `RuntimeEvidenceBundle`、train-only retriever 与精确绑定的脱敏回执；不导入 Provider、Pydantic AI 或 NoneBot 类型 | 可序列化 `AgentRunState`：结构化 action、规范化 observation、摘要、引用、usage 与 outcome | `src/nbtriage/bounded_agent.py` |
| Pydantic AI Agent step adapter | 把本步允许 action 与 citation 约束映射为唯一 `propose_action` 原生工具信封，并把唯一调用 deferred 给领域层 | 每步一个临时 Agent；`retries=0`、一次请求、最多一个调用；hard timeout 取 client timeout 与领域剩余 deadline 的较小值，零剩余值不耗 call slot，`TimeoutError` 交给 runner 映射 `DEADLINE`；不执行项目工具、不持久化框架历史；DeepSeek Responses 依赖 Pydantic + 领域本地复核；Provider 响应后的框架错误通过 `capture_run_messages()` 保留 usage / identity | 无长期状态 | `src/nbtriage/pydantic_agent_adapter.py`、`tools/nbtriage_maintainer/deepseek_adapter.py`、`src/nbtriage/openai_adapter.py`、`src/nbtriage/anthropic_adapter.py` |
| B4 evaluation harness | 用不泄漏 Gold 的 staged evidence Fixture 统计 trajectory、usage、安全和晋级条件；真实模式在每个 trial 重跑同模型 B1、从该结果计算 B3，再运行 B4；独立 `b4-real-partial` 保存授权、进度、请求 attempt、账本与失败 code/stage | scripted 模式不得晋级；真实模式显式确认理论请求/token/cost/deadline/whole-run 上限；请求前原子 checkpoint，响应后记账或保留稳定 unknown reason；Provider 错误只保存类别与可选 HTTP status；success/partial 路径禁止覆盖 | scripted 报告已冻结；DeepSeek run-1/run-2/run-3 中止证据保留，run-3 已验证 partial v1，当前 v3 尚无线上工件和完整正式报告 | `src/nbtriage/provider_failures.py`、`tools/nbtriage_maintainer/agent_evaluation.py`、`tools/nbtriage_maintainer/cli.py` |
| Runtime observation core | 校验传输无关的事件 / Matcher / API 生命周期摘要并按关联标识形成证据包 | NoneBot 适配器只能提交白名单标识；核心不导入框架类型；不调用模型、网络或外部工具 | 显式容量与 TTL 的单进程内存缓冲、累计丢弃计数 | `src/nbtriage/runtime_observations.py` |
| NoneBot runtime observer | 把事件 state 传播的关联 ID 与公共 event、run、API hook 压缩为核心观察 | 显式注册、采集错误 fail-open；不读取 Event 内容、身份、API data / result；不关联 Matcher 外 API | 观察器本地丢弃计数；观察本身进入核心 buffer | `src/nonebot_plugin_triage/nonebot_runtime.py` |
| Platform message reference index | 用 HMAC 精确绑定适配器、Bot、会话和消息引用 | 原始 scope / 引用只瞬时参与摘要；显式密钥、容量与 TTL；不持久化 | 摘要到 correlation ID 的单进程有界索引与丢弃计数 | `src/nbtriage/message_references.py` |
| Universal reference bridge | 用 UniSeg exporter 从任意受支持入站事件提取 Target 与 message ID | 不导入适配器事件类型；Target source 不进入稳定 scope；显式注册、fail-open | 桥本地丢弃计数；映射进入通用引用索引 | `src/nonebot_plugin_triage/universal_references.py` |
| OneBot V11 outgoing provider | 从 Matcher 内成功群发送结果提取 message ID，并用统一 Target scope 回填 | 只在安装 OneBot 时加载；不处理入站事件或公开命令；不保存完整 API 参数 / 结果 | Provider 本地丢弃计数；映射进入通用引用索引 | `src/nonebot_plugin_triage/onebot_v11_references.py` |
| Alconna triage entry | 接收必选 `triage` 后的自由文本，`@Bot` 和 Reply 可选；先分流能力说明、故障或澄清 | 文本只瞬时使用；不读 Reply 正文 / origin；所有求助先过轻量 HMAC 限流，只有疑似故障再过建单限流并进入 incident | 显式公开能力 Provider；疑似故障才有短期 LiveIncident | `src/nonebot_plugin_triage/handlers.py`、`src/nonebot_plugin_triage/support_intake.py`、`src/nonebot_plugin_triage/live_reports.py` |
| Deployment-local capability shadow | 启动时从已加载插件生成字段级 Claim、Evidence、Constraint 和本地 FTS5 索引 | 默认关闭；源码哈希不依赖 `uv.lock`；`restricted` 在模型外鉴权前不返回；失败保留上一个完整索引；当前不进入消息请求路径 | 配置路径下的可删除 SQLite 派生数据与内存构建状态 | `src/nonebot_plugin_triage/capability_snapshot.py`、`src/nonebot_plugin_triage/capability_shadow.py`、`src/nbtriage/capabilities.py` |
| Maintainer incident query | 在 NoneBot `SUPERUSER` 权限通过后，按编号读取固定字段摘要与活动 cluster 计数 | 普通成员在读取前被拒绝；无任意时间范围、日志导出或原始事件读取 | 读取现有 LiveIncident 与同缓冲内 cluster；不创建持久状态 | `src/nbtriage/live_incidents.py`、`src/nbtriage/incident_queries.py`、`src/nonebot_plugin_triage/incident_queries.py`、`src/nonebot_plugin_triage/handlers.py` |
| Observation-first trial | 在 incident 已受理后建立 `intake-v1` trial，追加 started / summary_viewed / feedback 事件并提供活动与离线窗口统计 | 默认 off；observe 需要审计 sink；失败写入 fail-open 且计数；只接受枚举反馈；离线汇总严格校验后只返回无标识聚合；不调用模型或工具 | 短期有界 trial 状态；本地单进程轮转 JSONL；trial / event / incident 不透明 ID 与最小失败形状；脱敏窗口摘要 | `src/nbtriage/live_trials.py`、`tools/nbtriage_maintainer/cli.py`、`src/nonebot_plugin_triage/trials.py`、`src/nonebot_plugin_triage/live_reports.py`、`src/nonebot_plugin_triage/handlers.py` |
| Explicit-report adapter | 根据匹配 correlation 的运行 bundle 或无证据空 bundle 构造确定性故障信号 | 上游已确认故障意图；只把明确失败标为失败；不读取文本 | 无长期状态 | `src/nbtriage/reply_reports.py` |
| Support intake router | 在技术责任诊断前区分能力教学、指令纠错、疑似故障、无关与危险请求 | 文本只由入口意图适配层瞬时处理；核心只接收结构信号；不代用户执行 | 无长期状态；只返回 disposition、固定动作、原因与补问标记 | `src/nbtriage/intake.py` |
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
- `data/rag/bot-docs.sqlite3` 只保存从外部 `bot-docs` 批准子集派生的分块、FTS5 索引和来源元数据；源 Markdown 不复制进仓库，`reports/bot-docs-retrieval.json` 同样只作本地评测输出；基础 wheel / sdist 不携带这些数据，未来产品需要离线 RAG 时使用带 manifest 的独立版本化知识包，运行副本进入 LocalStore cache 或部署者显式外部路径，见 [ADR-0019](../adr/0019-distribute-rag-corpus-as-versioned-knowledge-pack.md)；
- `NBTRIAGE_CAPABILITY_SHADOW_PATH` 指向的 SQLite 是当前部署本地派生数据：只在显式配置后由启动钩子原子生成，不进入 Git 或发行物；`review` 只有维护者显式检索时返回，`restricted` 会持久化但只有模型外上下文鉴权通过后才能检索；后续 operator exclude policy 将负责在持久化前完全排除指定能力，当前尚无这个按能力排除接口；
- `evals/curation/batches/` 保存人工晋级批次，`evals/curation/annotations/` 保存可复建的人工结论；二者不复制原始 Issue 正文；
- `evals/datasets/catalog/`、`evals/datasets/fixtures/` 与 `evals/datasets/splits/` 保存可审查输入、合成安全集合和冻结切分；
- `evals/oracles/` 保存经过引用校验、可作为回归合同复建依据的 Oracle 结论；完整机器报告已迁入本地 `reports/` 或 MLflow，`evals/` 不再保存运行快照；
- `curation.field_provenance` 为每个资格字段记录 `source.body`、`gold.comment.<id>` 或策展推断来源；Gate 不接受没有来源标记的完整字段；
- `visibility_boundary` 固定为目标 Issue 的 `opened_at`；当前 GitHub API 无法证明 Issue 正文未在后来编辑，因此 schema 明确记录 `body_edit_history_unavailable`，不能把当前正文误称为严格历史快照；
- `evals/datasets/splits/data-gate-v1.json` 按 `opened_at` 建立 train / validation / held-out 时间窗；相同根因簇、重复 / 回移植和相同 Oracle 引用必须留在同一 split；
- `artifacts/sessions/` 保存本地会话状态、白名单化脱敏回执摘要、审批与结果引用；不复制 Issue 正文、原始日志或配置值，当前文件适配器不提供多进程并发写入协调；
- `artifacts/answer-quality/<evaluation-id>/` 保存从真实 B4 固定合成集导出的候选、待完成或已完成人工标注与离线质量报告；它不属于插件实例状态或生产数据，文件默认拒绝覆盖；
- `artifacts/` 与 `reports/` 整体是本地运行输出；MLflow 的 `mlruns/`、`mlartifacts/`、数据库和 WAL/SHM 同样不进入 Git。未来 run 记录应引用 Git 中 `evals/` 合同的内容哈希或 revision；
- `RuntimeObservationBuffer` 当前只保存进程内最小化标识；构造时必须显式选择容量和最长 7 天的 TTL，容量或过期淘汰计数进入证据包；尚未选择生产默认值，也不提供崩溃恢复；
- `NoneBotRuntimeObserver` 的关联 ID 只存在于 NoneBot event / Matcher state 和上述缓冲；hook 采集失败只增加观察器本地丢弃计数，不中断 Bot，Matcher 外 API 当前不记录；
- `PlatformMessageReferenceIndex` 只保存 HMAC 摘要、correlation ID 与存入时间；原始 Target / Bot / actor / message scope 只在调用栈中出现；进程重启后密钥和索引一起丢失，跨 Worker 与历史回复尚不支持；
- `LiveIncidentBuffer` 保存不透明编号、确定性 intake、运行证据 bundle 与创建时间，并用最小失败标识的稳定哈希维护同容量 / TTL 的活动 cluster count/first/last；签名不含 observation / correlation ID、时间、异常消息、平台身份或聊天正文，当前也不持久化；
- observation-first trial 在 `observe` 模式当前仍把最小审计事件写到相对路径 `logs/nbtriage-trials.jsonl`；[ADR-0018](../adr/0018-use-localstore-only-for-enabled-trial-audit-log.md) 已决定在实现时将这一个部署者拥有的文件迁到 LocalStore data 目录，而不持久化上述观察、引用和 incident 缓冲；
- `IntakeSignals` / `IntakeDecision` 当前只在调用链中传递结构状态，不保存用户文字、命令原文、用户 / 群 ID，也未接入会话存储；
- `AlconnaCapability` 是当前注册表的进程内快照；`AlconnaParseReceipt` 只保留能力标识、四类状态、固定原因和头部匹配标记，不保存 `Arparma.origin`、错误数据、异常文本或匹配值；
- `AgentRunState` 只保存领域 action、规范化 observation、短摘要、引用、usage、pending interruption 与停止原因；不保存 Pydantic AI message history、Fixture Gold、原始日志、身份、秘密或私有 Chain-of-Thought；
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
- 任意适配器的原始 Bot / 会话 / 成员 / 消息 ID 只能瞬时参与带密钥摘要，不能写入领域工件、日志或模型输入；跨 adapter / Bot / Target 和过期引用必须未命中，不能按时间猜测；
- 跨平台入口与出站引用覆盖必须分开声明；没有对应 Provider 时不得伪称能关联 Bot 主动输出；
- 支持入口的危险标记拥有绝对路由优先级；命令解析错误不能直接升级为插件故障，冲突或不足信号不能强行产生责任层；
- 能力发现不得调用已注册命令的 `parse()`；Alconna 元数据只能作为不受信证据，不能覆盖策略或触发工具；
- 部署本地能力影子不得调用任意第三方 Rule、Permission、handler、behavior 或 executor；绝对本机路径、Token、配置原文和私密日志不得进入索引；SUPERUSER、`CommandMeta.hide=True` 与内部管理能力必须保存为 `restricted`，并在任何模型或检索器看到前由模型外鉴权守门；
- 不自动创建 Issue、PR、评论或标签；
- Token 只从进程环境读取，不写仓库、不进入缓存或报告、不输出；
- NoneBot 模型配置不接受 API Key 或 base URL；禁用时不导入模型 Provider，未通过支持矩阵线上门的精确
  backend/model 在 SDK 与密钥读取前失败；
- 后续 G2 / G3 执行必须进入独立可销毁 Runner，不能在控制面或真实 QQ Bot 进程中安装插件。
- 当前 15 个已验证案例来自人工审计的 detached worktree：包级探针使用 `uv run --isolated` 与目标 lockfile，源码提取探针只编译目标函数 / 模型 / 迁移体并注入内存替身。该边界不是容器级隔离，不能推广到任意商店插件。

## 质量与演进检查点

当前 100 个候选中已有 38 个 Case 完成策展：20 个 `ready_for_execution`、16 个非执行就绪、2 个排除。15 个案例实际取得 `buggy_ref` 目标失败且 `fixed_ref` 通过，1 个 Linux 案例被当前 Runner 阻塞，运行结果无失败或无效引用。36 个合格 Case 已按时间切为 train 21、validation 11、held-out 4，并通过测试确认根因簇与 Oracle 引用不跨 split。Data Gate 的规格、运行和泄漏检查均达到门槛，“可执行复现”可保留为后续 MVP 的受限核心能力。B0 已冻结：held-out 路由 / 阶段准确率均为 0.50，缺失证据 micro-F1 为 0.00；它是有效下界而不是可上线方案。冻结的 B1 DeepSeek 基线在 held-out 上把路由准确率提升到 0.75、故障阶段准确率提升到 1.00，症状、责任层与版本值 micro-F1 也高于 B0，但缺失证据 micro-F1 仍为 0.00。独立 S3 集合使用 6 个纯合成 Fixture 补足历史数据没有安全拒绝分母的缺口：冻结 B0 只拒绝 1 / 6，B1 pre-model guard 拒绝 6 / 6，且无模型或工具调用。B3 已用 `adapter-qq #202` 验证“预测 → 待审批 → 明确批准 → 关联 Oracle → 完成”的 4 事件流程；第二切片又在 validation 上把每个补证动作的平均问题数从 4.125 降到 1.000、precision 从 0.303 升到 0.750；第三切片的 16 条纯合成回执 Fixture（9 有效、7 无效）实现 1.000 接受 / 拒绝准确率，并把合格回执接入单步重规划。三条切片都没有新增模型或工具调用。B4 scripted Gate 用 4 个合成 Fixture、8 个 trial 验证动态只读 action、补证暂停恢复、安全拒绝与 Gold 隔离：task success 为 0.875、useful action precision 为 1.000、安全违规与 blocked action 均为 0；它有 0 个真实 Provider 请求和 0 个外部工具调用，因此不具备插件晋级资格。同模型真实 Gate harness 已实现；2026-08-09 获授权的 DeepSeek 首轮失败关闭记录保留。OpenCode Go 只保留为 `tests/support/opencode_go_backend.py` 中的 evaluation-only 兼容 Chat 夹具；一次获授权 native-schema 探测返回 400，只形成“兼容传输不等于服务端能力”的测试结论，不构成 B1 阻塞、产品候选或网关决策。真实入口已产品化为 `nonebot-plugin-triage`：Alconna Matcher、UniSeg Reply / Target、通用入站引用桥、OneBot 可选出站 Provider、HMAC 限流、短期 LiveIncident、窄回显和 `SUPERUSER` 白名单查询已经组合并通过 wheel 隔离加载测试。模型调用核心已迁移为端到端异步协议，CLI 只在边缘桥接且评测仍按 Case 串行。OpenAI、DeepSeek Responses 与 Anthropic Messages B1 factory 已通过全离线 native schema，三条产品 Provider 的 B4 tool-call wire 均有假 HTTP 合约；OpenCode Go 夹具不进入 wheel、公开 extra、CLI、插件配置或资格矩阵。

当前本地 Python 3.12 完整回归为 505 项；Ruff 全仓、format、BasedPyright、wheel / sdist build、严格元数据检查及 base wheel 26 包隔离验证均通过。此前 Python 3.11、3.12、3.13 的跨版本记录对应改写前的 422 项测试，合入后仍应由 CI 重新执行当前矩阵；本地 Python 3.14 曾在 pytest 收集前因 `async_asgi_testclient.utils` 缺失中止，尚不能记为通过或代码回归。OpenAI-only、DeepSeek-only 与 Anthropic-only 的隔离安装保留此前验证记录。

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
