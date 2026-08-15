# 架构决策记录

| ADR | 状态 | 决策 |
|---|---|---|
| [ADR-0001](0001-qq-group-report-linked-runtime-evidence.md) | 已采纳 | 以 QQ 群显式报障与 NoneBot 本机运行证据关联作为首个真实用户入口 |
| [ADR-0002](0002-tiered-autonomy-and-ownership-aware-remediation.md) | 已采纳 | 以分级自治、责任层路由和动作专用执行器扩展诊断到修复闭环 |
| [ADR-0003](0003-unified-capability-guidance-and-incident-intake.md) | 部分被替代 | 用统一显式入口分流能力导航、指令纠错、疑似故障、无关与不安全请求；触发细节由 ADR-0020 更新 |
| [ADR-0004](0004-onebot-v11-first-and-keyed-message-reference-index.md) | 部分被替代 | OneBot V11 作为首个 QQ dogfood 与带密钥引用索引来源 |
| [ADR-0005](0005-first-group-report-interaction-policy.md) | 已被替代 | 原 OneBot 专属报障交互策略，保留为决策历史 |
| [ADR-0006](0006-cross-platform-alconna-entry-and-reference-providers.md) | 部分被替代 | 从第一版采用 Alconna 跨平台入口，并把出站引用差异隔离为 Provider；统一私聊拒绝由 ADR-0028 收窄 |
| [ADR-0007](0007-single-distribution-dual-namespace.md) | 已采纳 | 采用单仓库、单发行包、插件入口与领域核心双命名空间结构 |
| [ADR-0008](0008-pydantic-ai-controlled-model-adaptation.md) | 已采纳；语义 assessment 实现由 ADR-0044、Provider extra 所有权由 ADR-0047 部分替代 | 采用 Pydantic AI 的 Model / Provider / Profile 与 Direct Request 作为受控 B1 多模型 API 适配层 |
| [ADR-0009](0009-use-async-model-boundary.md) | 已采纳 | 模型调用核心采用异步协议，同步 CLI 只在进程边缘桥接 |
| [ADR-0010](0010-use-bounded-evidence-seeking-agent-loop.md) | 已采纳 | 用单 Agent、typed tools、有界循环、HITL 与 trajectory Gate 验证 Agent 能力 |
| [ADR-0011](0011-expose-disabled-qualified-model-configuration.md) | 部分被替代 | 保留无配置密钥/base URL、精确资格门和单步客户端；产品启用开关由 ADR-0037 删除 |
| [ADR-0012](0012-use-pydantic-ai-deferred-tools-behind-domain-runtime.md) | 已采纳 | 用领域 runtime 掌握循环与授权，只借用 Pydantic AI Deferred Tools 做单步多 Provider 适配 |
| [ADR-0013](0013-use-mandatory-output-tool-for-opencode-go-b1.md) | 未采纳 | 不把一次 OpenCode Go 测试升级为 B1 输出契约或产品网关决定 |
| [ADR-0014](0014-use-observation-first-production-trials.md) | 部分被替代 | 先用零模型、脱敏、可反馈的观察型生产 trial 建立真实评测闭环 |
| [ADR-0015](0015-separate-versioned-evals-from-local-runtime-data.md) | 部分被替代 | 用 `evals/` 保存版本化评测合同，并与本地数据、报告和 MLflow 运行状态分离；冻结机器报告的发布边界由 ADR-0016 收紧 |
| [ADR-0016](0016-keep-maintainer-evaluation-tooling-out-of-install-surface.md) | 已采纳 | 保留双命名空间领域核心，但把维护者 CLI、MLflow 和历史机器报告排除在插件安装发行面之外 |
| [ADR-0017](0017-run-deterministic-evaluations-through-pytest.md) | 已采纳 | 通过现有 pytest job 执行确定性评测回归，当前不增加专用 job、摘要或 Artifact |
| [ADR-0018](0018-use-localstore-only-for-enabled-trial-audit-log.md) | 已采纳 | 只用 LocalStore 保存显式启用的 trial 审计 JSONL，其余诊断关联继续保持内存态 |
| [ADR-0019](0019-distribute-rag-corpus-as-versioned-knowledge-pack.md) | 已采纳 | 基础发行包不内置 RAG 语料；产品需要时再发布独立、可选、版本化的离线知识包 |
| [ADR-0020](0020-use-triage-command-for-natural-language-support.md) | 已采纳；动态配置与两级限流由 ADR-0045 部分替代 | 用必选 `triage` 指令承接自然语言求助；ADR-0031 将该要求恢复并细化到 Thread 续问 |
| [ADR-0021](0021-use-deployment-local-capability-shadow-index.md) | 已采纳；启用和路径策略由 ADR-0045 部分替代 | 用字段级证据构建部署本地能力影子索引，先评估再接入回复 |
| [ADR-0022](0022-limit-capability-shadow-guidance-to-superusers.md) | 聊天 guidance fallback 由 ADR-0046 部分替代 | SUPERUSER 维护者 CLI 仍保留；聊天内部问题改由 behavior exploration 分类后鉴权 |
| [ADR-0023](0023-defer-orm-until-durable-business-state.md) | 已采纳；首次 Bug 权威记录评审由 ADR-0054 落实 | 按状态语义分层存储；首版 reviewed Bug catalog 采用 LocalStore 单写者 snapshot，出现在线并发写入时再评审 ORM |
| [ADR-0024](0024-auto-publish-deterministic-capability-fields.md) | 已采纳 | 确定且低风险的命令字段自动公开；其余异常由 ADR-0032 拆为具体 `analysis_issues` |
| [ADR-0025](0025-explain-plugin-behavior-from-deployment-evidence.md) | 已采纳 | 用多源部署证据向已鉴权开发者解释插件行为，并区分观察事实、静态推导与未知 |
| [ADR-0026](0026-filter-capability-knowledge-before-retrieval.md) | 已采纳；回答投影由 ADR-0027 细化 | 在检索与模型前按受众和 adapter 隔离能力知识，普通用户不感知受限或跨 adapter 能力 |
| [ADR-0027](0027-constrain-guidance-with-facts-not-fixed-wording.md) | 已采纳 | 用事实输出合同约束能力帮助，模型自由组织措辞并具体说明公开能力的可验证约束 |
| [ADR-0028](0028-allow-private-triage-and-superuser-request-context-replies.md) | 已采纳 | 允许 triage 私聊进入统一分流，并向已鉴权 SUPERUSER 的原始提问会话返回完整行为解释 |
| [ADR-0029](0029-control-model-config-values-with-deployment-deny-list.md) | 已采纳 | 由部署者 deny-list 控制能力相关配置值进入模型，原值不持久化或对外披露 |
| [ADR-0030](0030-continue-support-thread-by-exact-reply.md) | 已替代 | 曾允许精确回复 Triage 已登记回答免命令续问；触发入口由 ADR-0031 收紧，剩余 exact-Reply 生命周期由 ADR-0060 更新 |
| [ADR-0031](0031-require-triage-for-support-thread-continuation.md) | 部分被替代 | 所有支持轮次都要求显式 `triage`；ADR-0060 改由稳定作用域承接一次补充，Reply 只作路由后上下文 |
| [ADR-0032](0032-separate-capability-audience-analysis-and-platform-status.md) | 已采纳 | 分离能力受众、平台范围、分析问题与约束，由派生 ServingView 取代 review 审批层 |
| [ADR-0033](0033-serialize-support-thread-turns-with-single-use-reply-claims.md) | 部分被替代 | ADR-0060 以作用域 Claim 替代一次性 Reply Claim；单活动 lease、`BUSY`、TTL 与失败关闭继续有效 |
| [ADR-0034](0034-distinguish-matchers-from-user-observable-capabilities.md) | 已替代 | 曾按用户可观察效果归并 Matcher；由 ADR-0036 收窄为独立确定性记录 |
| [ADR-0035](0035-settle-support-thread-replies-from-uniseg-receipts.md) | 部分被替代 | ADR-0060 不再用 Receipt message ID 建立 Thread 续接点；当前 Matcher 仍拥有发送事务与失败关闭 |
| [ADR-0036](0036-keep-capability-shadow-deterministic-and-record-oriented.md) | 已采纳 | 保持能力影子确定且以记录为单位，删除无消费者的 Matcher 角色和逐记录源码对齐推断 |
| [ADR-0037](0037-make-semantic-assessment-the-default-triage-path.md) | 已采纳；incident 条件由 ADR-0040 收紧 | 删除本地意图词表和产品启用开关，每轮 triage 默认经受限语义 assessment，transport 不可用时 abstain |
| [ADR-0038](0038-limit-semantic-assessment-remote-data-projection.md) | 已采纳 | 只允许向合格语义 assessment transport 投影当前单条规范化 triage 请求文字，其他上下文仍禁止出站 |
| [ADR-0039](0039-use-griffe-for-installed-public-framework-source-evidence.md) | Griffe 后端已由 ADR-0057 替代并移除 | 保留安装版本、源码 revision、来源归属与 Evidence 边界 |
| [ADR-0040](0040-require-trusted-preflight-failure-before-incident.md) | 已采纳；由 ADR-0043 进一步收紧，专用限流由 ADR-0045 部分替代 | 用户报告只形成未验证信号；仅模型外可信初检仍失败时进入 incident |
| [ADR-0041](0041-qualify-opencode-go-tool-output-for-support-semantics.md) | schema / Prompt / 资格 revision 由 ADR-0043 替代；手写 output tool 由 ADR-0044、独立 extra 由 ADR-0047 替代 | 以单一不可执行 output tool 准入 OpenCode Go 的首个语义组合 |
| [ADR-0042](0042-use-pydantic-ai-model-profile-for-structured-output.md) | 已采纳；手写输出定义由 ADR-0044 部分替代 | 由 Pydantic AI ModelProfile 唯一决定结构化输出方式，项目只维护任务资格 |
| [ADR-0043](0043-separate-support-goals-observations-and-maintenance-depth.md) | 已替代 | 曾用目标、现象陈述和维护证据深度三组字段取代 flat needs；taxonomy 由 ADR-0046 接续 |
| [ADR-0044](0044-use-pydantic-ai-agent-output-type-for-support-semantics.md) | 已采纳 | 语义 assessment 直接以 Pydantic model 作为 Pydantic AI Agent output_type，不再手写重复的结构化输出层 |
| [ADR-0045](0045-use-one-triage-cooldown-and-localstore-capability-cache.md) | 已采纳 | 固定命令与入口边界，只保留统一 triage 冷却，并默认用 LocalStore cache 管理能力影子 |
| [ADR-0046](0046-merge-internal-reasoning-into-behavior-exploration.md) | 已采纳 | 用行为探索目标统一内部原因与维护证据请求，保留独立现象字段和模型外 SUPERUSER 鉴权 |
| [ADR-0047](0047-reuse-pydantic-ai-provider-extras.md) | 已采纳 | 直接复用 Pydantic AI 的 `anthropic` / `openai` Provider extras，不再重复锁 SDK 或提供 OpenCode Go 同义 extra |
| [ADR-0048](0048-use-public-facts-for-guidance-answer-agent.md) | 部分被替代 | Guidance 仍由公开事实约束；ADR-0060 允许在路由后加入有界 Thread 与直接 Reply 上下文 |
| [ADR-0050](0050-use-a-bounded-agent-for-user-bug-assessment.md) | 部分被替代；首个只读三值纵切已实现；责任范围、数据投影和历史存储分别由 ADR-0052、0053、0054 补充 | ADR-0060 允许 Bug Agent 使用直接 Reply 与模型外锚定的会话上下文，三值结论与确定性协调边界不变 |
| [ADR-0051](0051-let-the-bug-assessment-agent-query-design-rag.md) | 已采纳；首个只读知识包消费者已实现；正文投影由 ADR-0053 部分替代 | 允许 Bug Agent 在历史与公开合同初检未命中后查询版本化设计 RAG，并保持设计、源码与运行证据分层 |
| [ADR-0052](0052-define-bug-across-the-bot-software-responsibility-chain.md) | 已采纳；首个责任候选 schema 与评测已实现 | 把普通用户 Bug verdict 定义到整个 Bot 软件责任链，并单独保留内部责任候选 |
| [ADR-0053](0053-allow-relevant-source-and-log-bodies-for-bug-assessment.md) | 部分被替代；首个有界源码、关联日志与 Bug Prompt v8 精确资格纵切已实现 | ADR-0060 仅取消直接 Reply 与锚定聊天正文的内容遮蔽；当前中文 Prompt v8 已通过独立 Gate，源码、日志与配置仍遵守既有清理边界 |
| [ADR-0054](0054-store-reviewed-bug-problems-in-localstore.md) | 已采纳；首个 LocalStore data catalog 已实现 | 使用 LocalStore data 保存人工审核的问题与 verdict catalog，并限制首版为维护者单写、在线只读 snapshot |
| [ADR-0055](0055-use-ast-grep-for-matcher-source-shape-extraction.md) | 已采纳；直接替换已实现 | 用固定、只读的 ast-grep 规则替代 Matcher 源码形状的手写 AST 遍历，同时保留 Triage 的运行时门禁、预算和 Evidence 边界 |
| [ADR-0056](0056-use-serena-for-optional-bug-source-navigation.md) | 已采纳；插件内纵切已实现 | 用隔离的 Serena 只读 MCP 可选增强 Bug 源码符号导航，保留有界文本后备与 Triage Evidence 门禁 |
| [ADR-0057](0057-select-source-analysis-tools-by-evidence-stage.md) | 已采纳；Direct Jedi 已接入教学链，真实模型资格待完成 | 依赖定义采用 Direct Jedi，glob/文本永久兜底；不为该职责并行维护 Griffe、MultiLSPy 或 Serena |
| [ADR-0058](0058-use-deterministic-evidence-and-bounded-navigation-for-teaching-annotations.md) | 已采纳；教学接线已实现，待真实模型重新资格 | 教学注释先消费确定性 Evidence Pack，再经 Triage 有界源码导航补证，并以插件源码粗粒度失效全部注释 |
| [ADR-0059](0059-share-read-only-evidence-access-across-agent-flows.md) | 已采纳；共享领域工具已实现并接入教学 Agent | 共享只读 FileSystem、Jedi 转到定义、路径拒绝与内存配置值证据边界，并移除项目自有 Griffe reader；Bug 复用仍待后续接线 |
| [ADR-0060](0060-use-scope-thread-and-post-route-conversation-context.md) | 部分被 ADR-0061 替代 | 用稳定作用域承接一次显式补充，Semantic 只看当前文字；Reply 邻近聊天读取由 ADR-0061 改为最新窗口 |
| [ADR-0061](0061-read-latest-bounded-conversation-window-for-bug-assessment.md) | 部分被 ADR-0064、ADR-0065 替代 | Bug Agent 一次读取当前会话最新有界窗口；ADR-0065 进一步规定无原生历史 Provider 时不暴露工具 |
| [ADR-0062](0062-structure-capability-teaching-usages-requirements-and-interactions.md) | 已采纳；首个 schema 与投影已实现 | 用有序多用法、结构化角色与多种限流、独立交互和检索字段表达教学注释，移除通用 limiter 名称猜测 |
| [ADR-0063](0063-keep-plugin-startup-independent-from-model-enhancements.md) | 已采纳；已实现 | 未配置或不可用的模型增强不得阻断插件导入，保留确定性能力索引并让教学注释、语义与 Answer 安全降级 |
| [ADR-0064](0064-refine-bug-conversation-evidence-and-verdict-contract.md) | 已采纳；本地跨平台缓冲由 ADR-0065 替代；Prompt v8 精确资格已通过 | 把最新聊天窗口收窄到 30 条，保留窗口外精确 Reply；明确共享预算、单次补充、代码内不变量与 occurrence 语义 |
| [ADR-0065](0065-only-expose-conversation-history-for-supported-platforms.md) | 已采纳；已实现 | 只在 Adapter 有真实会话历史 Provider 时向 Bug Agent 暴露聊天工具；不再用本地滚动窗口模拟跨平台历史 |
