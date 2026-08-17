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
| [ADR-0011](0011-expose-disabled-qualified-model-configuration.md) | 部分被 ADR-0037、ADR-0086 替代 | 保留无配置密钥/base URL 和单步客户端；产品启用开关由 ADR-0037 删除，评测不再是运行许可 |
| [ADR-0012](0012-use-pydantic-ai-deferred-tools-behind-domain-runtime.md) | 已采纳 | 用领域 runtime 掌握循环与授权，只借用 Pydantic AI Deferred Tools 做单步多 Provider 适配 |
| [ADR-0013](0013-use-mandatory-output-tool-for-opencode-go-b1.md) | 未采纳 | 不把一次 OpenCode Go 测试升级为 B1 输出契约或产品网关决定 |
| [ADR-0014](0014-use-observation-first-production-trials.md) | 部分被替代 | 先用零模型、脱敏、可反馈的观察型生产 trial 建立真实评测闭环 |
| [ADR-0015](0015-separate-versioned-evals-from-local-runtime-data.md) | 部分被替代 | 用 `evals/` 保存版本化评测合同，并与本地数据、报告和 MLflow 运行状态分离；冻结机器报告的发布边界由 ADR-0016 收紧 |
| [ADR-0016](0016-keep-maintainer-evaluation-tooling-out-of-install-surface.md) | 已采纳 | 保留双命名空间领域核心，但把维护者 CLI、MLflow 和历史机器报告排除在插件安装发行面之外 |
| [ADR-0017](0017-run-deterministic-evaluations-through-pytest.md) | 已采纳 | 通过现有 pytest job 执行确定性评测回归，当前不增加专用 job、摘要或 Artifact |
| [ADR-0018](0018-use-localstore-only-for-enabled-trial-audit-log.md) | 已采纳 | 只用 LocalStore 保存显式启用的 trial 审计 JSONL，其余诊断关联继续保持内存态 |
| [ADR-0019](0019-distribute-rag-corpus-as-versioned-knowledge-pack.md) | 部分被 ADR-0067 替代 | 基础发行包不内置 RAG 语料；默认发现与更新改由 stable catalog 提供 |
| [ADR-0020](0020-use-triage-command-for-natural-language-support.md) | 已采纳；动态配置与两级限流由 ADR-0045 部分替代 | 用必选 `triage` 指令承接自然语言求助；ADR-0031 将该要求恢复并细化到 Thread 续问 |
| [ADR-0021](0021-use-deployment-local-capability-shadow-index.md) | 已采纳；启用和路径策略由 ADR-0045 部分替代 | 用字段级证据构建部署本地能力影子索引，先评估再接入回复 |
| [ADR-0022](0022-limit-capability-shadow-guidance-to-superusers.md) | 聊天 guidance fallback 由 ADR-0046 部分替代 | SUPERUSER 维护者 CLI 仍保留；聊天内部问题改由 behavior exploration 分类后鉴权 |
| [ADR-0023](0023-defer-orm-until-durable-business-state.md) | 部分被 ADR-0073 接续；其他状态分层仍有效 | 按状态语义分层存储；长期 Bug 工作流已进入 ORM，短期关联、审计日志和可重建索引仍保持原分层 |
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
| [ADR-0041](0041-qualify-opencode-go-tool-output-for-support-semantics.md) | schema / Prompt revision 由 ADR-0043 替代；手写 output tool 由 ADR-0044、独立 extra 由 ADR-0047、运行白名单由 ADR-0086 替代 | 保留 OpenCode Go 首个语义组合的历史评测证据 |
| [ADR-0042](0042-use-pydantic-ai-model-profile-for-structured-output.md) | 已采纳；手写输出定义由 ADR-0044 部分替代 | 由 Pydantic AI ModelProfile 唯一决定结构化输出方式，项目只维护任务资格 |
| [ADR-0043](0043-separate-support-goals-observations-and-maintenance-depth.md) | 已替代 | 曾用目标、现象陈述和维护证据深度三组字段取代 flat needs；taxonomy 由 ADR-0046 接续 |
| [ADR-0044](0044-use-pydantic-ai-agent-output-type-for-support-semantics.md) | 已采纳 | 语义 assessment 直接以 Pydantic model 作为 Pydantic AI Agent output_type，不再手写重复的结构化输出层 |
| [ADR-0045](0045-use-one-triage-cooldown-and-localstore-capability-cache.md) | 已采纳 | 固定命令与入口边界，只保留统一 triage 冷却，并默认用 LocalStore cache 管理能力影子 |
| [ADR-0046](0046-merge-internal-reasoning-into-behavior-exploration.md) | 已采纳 | 用行为探索目标统一内部原因与维护证据请求，保留独立现象字段和模型外 SUPERUSER 鉴权 |
| [ADR-0047](0047-reuse-pydantic-ai-provider-extras.md) | 部分被 ADR-0084 替代 | 继续复用 Pydantic AI 的 Provider extras；公共控制层改由基础依赖安装 |
| [ADR-0048](0048-use-public-facts-for-guidance-answer-agent.md) | 部分被替代 | Guidance 仍由公开事实约束；ADR-0060 允许在路由后加入有界 Thread 与直接 Reply 上下文 |
| [ADR-0050](0050-use-a-bounded-agent-for-user-bug-assessment.md) | 部分被替代；首个只读三值纵切已实现；责任范围、数据投影和历史存储分别由 ADR-0052、0053、0054 补充 | ADR-0060 允许 Bug Agent 使用直接 Reply 与模型外锚定的会话上下文，三值结论与确定性协调边界不变 |
| [ADR-0051](0051-let-the-bug-assessment-agent-query-design-rag.md) | 已采纳；首个只读知识包消费者已实现；正文投影由 ADR-0053 部分替代 | 允许 Bug Agent 在历史与公开合同初检未命中后查询版本化设计 RAG，并保持设计、源码与运行证据分层 |
| [ADR-0052](0052-define-bug-across-the-bot-software-responsibility-chain.md) | 已采纳；首个责任候选 schema 与评测已实现 | 把普通用户 Bug verdict 定义到整个 Bot 软件责任链，并单独保留内部责任候选 |
| [ADR-0053](0053-allow-relevant-source-and-log-bodies-for-bug-assessment.md) | 部分被替代；首个有界源码、关联日志与 Bug Prompt v8 精确资格纵切已实现 | ADR-0060 仅取消直接 Reply 与锚定聊天正文的内容遮蔽；当前中文 Prompt v8 已通过独立 Gate，源码、日志与配置仍遵守既有清理边界 |
| [ADR-0054](0054-store-reviewed-bug-problems-in-localstore.md) | 已被 ADR-0068、ADR-0073 替代；旧 JSON catalog 已删除 | 保留历史文件决策记录；当前权威 Bug 工作流使用 ORM 与追加式 Decision |
| [ADR-0055](0055-use-ast-grep-for-matcher-source-shape-extraction.md) | 已采纳；直接替换已实现 | 用固定、只读的 ast-grep 规则替代 Matcher 源码形状的手写 AST 遍历，同时保留 Triage 的运行时门禁、预算和 Evidence 边界 |
| [ADR-0056](0056-use-serena-for-optional-bug-source-navigation.md) | 已被 ADR-0085 替代 | 历史 Serena Bug-only 纵切；实现、配置与 extra 已移除 |
| [ADR-0057](0057-select-source-analysis-tools-by-evidence-stage.md) | 已采纳；Direct Jedi 已接入教学链，真实模型资格待完成 | 依赖定义采用 Direct Jedi，glob/文本永久兜底；不为该职责并行维护 Griffe、MultiLSPy 或 Serena |
| [ADR-0058](0058-use-deterministic-evidence-and-bounded-navigation-for-teaching-annotations.md) | 已采纳；静态证据职责由 ADR-0069 细化 | 教学注释使用 Triage 有界源码导航；确定性层只拥有准入、范围、当前性和验证，不承担工厂业务语义摘要 |
| [ADR-0059](0059-share-read-only-evidence-access-across-agent-flows.md) | 已采纳；共享领域工具已实现并接入教学 Agent | 共享只读 FileSystem、Jedi 转到定义、路径拒绝与内存配置值证据边界，并移除项目自有 Griffe reader；Bug 复用仍待后续接线 |
| [ADR-0060](0060-use-scope-thread-and-post-route-conversation-context.md) | 部分被 ADR-0061 替代 | 用稳定作用域承接一次显式补充，Semantic 只看当前文字；Reply 邻近聊天读取由 ADR-0061 改为最新窗口 |
| [ADR-0061](0061-read-latest-bounded-conversation-window-for-bug-assessment.md) | 部分被 ADR-0064、ADR-0065、ADR-0066 替代 | Bug Agent 一次读取当前会话最新有界窗口；ADR-0065 进一步规定无原生历史 Provider 时不暴露工具，ADR-0066 重新界定自动服务教学注释的第一层合同地位 |
| [ADR-0062](0062-structure-capability-teaching-usages-requirements-and-interactions.md) | 部分被 ADR-0069、ADR-0080 替代 | 结构化字段继续服务帮助展示；Answer 详细知识改由独立自由 Markdown，interaction 与 `{command}` 已删除，多 entry 合同由 ADR-0080 接续 |
| [ADR-0063](0063-keep-plugin-startup-independent-from-model-enhancements.md) | 已采纳；资格门部分被 ADR-0086 替代 | 未配置或技术不可用的模型增强不得阻断插件导入；未评测组合本身不再触发降级 |
| [ADR-0064](0064-refine-bug-conversation-evidence-and-verdict-contract.md) | 部分被 ADR-0065、ADR-0066 替代；Prompt v8 精确资格已通过 | 把最新聊天窗口收窄到 30 条，保留窗口外精确 Reply；ADR-0066 重新界定自动服务教学注释的第一层合同地位 |
| [ADR-0065](0065-only-expose-conversation-history-for-supported-platforms.md) | 已采纳；已实现 | 只在 Adapter 有真实会话历史 Provider 时向 Bug Agent 暴露聊天工具；不再用本地滚动窗口模拟跨平台历史 |
| [ADR-0066](0066-use-active-teaching-contract-as-bug-precheck.md) | 已采纳；首个保守纵切已实现 | 只让当前公开主动能力进入教学合同域；subject / observation readiness 与精确 Reply 用法纠正已接线，更广参数 / 角色 / 场景检查和教学回答 revision 绑定仍待实现 |
| [ADR-0067](0067-refresh-knowledge-pack-from-stable-catalog-at-startup.md) | 已采纳；已实现 | 启动后后台检查 stable catalog，校验新包后原子切换；所有更新失败均保留旧包或降级且不阻断插件加载 |
| [ADR-0068](0068-treat-qualified-agent-bug-verdicts-as-operational-decisions.md) | 模型资格限制被 ADR-0086 替代；深度 unknown 持久化由 ADR-0078 暂缓 | 经模型外 reconciler 接受的 Agent Bug 是正式 verdict，人工负责事后复核与改判 |
| [ADR-0069](0069-separate-help-display-from-answer-knowledge-and-bound-static-analysis.md) | 已采纳；首个纵切已实现，源码级 Provider held-out 未通过 | 分离 Migut Help 展示与 Answer Markdown，让 Answer 同时消费两种公开投影，并让静态分析只界定证据范围 |
| [ADR-0070](0070-separate-bug-reports-occurrences-and-problems.md) | 已采纳；Bug 领域与 ORM 纵切已实现，unknown 持久化暂缓 | 用薄 Report、具体 Occurrence 和长期 Problem 分离提交次数、实际发生次数与问题生命周期，并记录 Sentry Event → Issue 参考 |
| [ADR-0071](0071-group-bug-problems-with-versioned-evidence-fingerprints.md) | 已采纳；Bug 指纹与保守聚合已实现，unknown 回执暂缓 | 用模型外版本化 Evidence 指纹聚合同根因，禁止文本自动合并，并固定 Bug 的“已记录 / 已关联”回执 |
| [ADR-0072](0072-use-opaque-problem-ids-and-minimal-maintainer-lifecycle.md) | 已采纳；Bug 编号与维护生命周期已实现，merge / alias 与 unknown 编号暂缓 | 向普通用户显示中性短问题编号，固定 Bug 事务回执，并定义最小维护动作 |
| [ADR-0073](0073-use-nonebot-orm-for-authoritative-bug-workflow-state.md) | 已采纳；首个四表 ORM 纵切、迁移与接线已实现，merge / alias 暂缓 | 用 NoneBot ORM 的事务保存 Report、Occurrence、Problem、公开 ID / alias 与人工维护生命周期，保留其他状态的原有分层 |
| [ADR-0074](0074-preserve-append-only-problem-decisions.md) | 已采纳；Bug 与人工复核 Decision 已实现，unknown Decision 暂缓 | 追加保存 Agent 判断、人工确认和 override，并让 Problem 当前 verdict 成为同事务更新的查询投影 |
| [ADR-0075](0075-register-problem-maintenance-under-triage-subcommand.md) | 已采纳；命令树、鉴权、查询与维护已实现 | 把问题查询、确认 Bug / 非 Bug 与解决注册为真实 `triage 报错查询` 子命令，并在 Semantic 之前确定性鉴权分流 |
| [ADR-0076](0076-remove-legacy-trial-feedback-and-stats-chat-commands.md) | 已采纳；聊天入口、元数据与当前文档已删除 | 删除当前不可达的 `报错反馈` 与 `报错统计` 聊天命令，保留底层 trial 工件和离线汇总 |
| [ADR-0077](0077-use-previous-generated-teaching-content-as-a-non-evidentiary-baseline.md) | 已采纳；已实现，待真实 Provider held-out | 重生成时把上一版机器生成公开文字作为非证据编辑基线，当前 Evidence 保持唯一事实所有权 |
| [ADR-0078](0078-defer-persisting-unknown-bug-assessments.md) | 已采纳；unknown 固定终局与不落库边界已实现 | 在可记录性合同确定前不持久化任何 unknown，缺关键知识时失败关闭且不声称已记录 |
| [ADR-0079](0079-list-pending-problems-with-triage-query.md) | 已采纳；待处理列表与命令树已实现 | 用无编号的 `triage 报错查询` 列出全部未解决 Bug Problem |
| [ADR-0080](0080-model-capability-teaching-as-multiple-public-entries.md) | 已采纳；领域与投影纵切已实现，v34 / v8 Gate 已冻结通过 | 一次能力分析可产生多个固定 entry，删除 `{command}` 与 interaction，并把 Alconna 子命令投影为独立帮助条目 |
| [ADR-0081](0081-close-unknown-teaching-gates-and-freeze-parser-owned-usages.md) | 部分被 ADR-0082、ADR-0083 替代；parser canonical usage 与有限枚举决定继续有效 | Runtime parser 已确认的用法由模型外冻结，并以四个为通用有限枚举边界；未知门禁的补证生命周期由 ADR-0083 接续 |
| [ADR-0082](0082-group-parameterized-matchers-only-by-runtime-handler-code-identity.md) | 已采纳；v4 正式 held-out 未通过，v26 开发回归已补齐已知机制 | 参数化 Matcher 只按 Runtime Handler 精确代码身份聚合；不再由 AST 猜外层工厂或构造成员摘要 |
| [ADR-0083](0083-resolve-unknown-teaching-gates-before-closing-public-knowledge.md) | 已采纳；已实现，待新 Provider Gate | AST 只登记疑似门禁；Agent 以实际定义、框架或运行配置解释为约束、无约束或仍未知，只有仍未知才关闭公开知识 |
| [ADR-0084](0084-install-pydantic-ai-control-plane-by-default-and-keep-providers-and-adapters-optional.md) | 已采纳；已实现 | 默认安装 Pydantic AI 控制层、Harness 与 Jedi，Provider SDK 和 NoneBot Adapter 仍由部署按需安装 |
| [ADR-0085](0085-remove-serena-bug-source-backend.md) | 已采纳；已实现 | 删除 Serena MCP extra、Bug-only 后端与配置，Bug 固定使用内置有界文本源码读取 |
| [ADR-0086](0086-treat-model-evaluation-as-a-quality-label.md) | 已采纳；已实现 | held-out 只提供公开质量标签；未评测模型可运行全部任务并在相同安全合同下写入本地 Bug Problem |
