# 当前架构决策

这里列出仍作为当前一等架构约束的 ADR。精确状态、局部替代关系和完整理由以各 ADR 正文为准；
已经替代、未采纳、评测性、实现级或仅作支持性解释的记录见[历史 ADR](history/README.md)。

当前根目录保留 84 份，历史区保存 63 份。这个数量是逐份按架构边界判断后的结果，不是配额，也不是
为了简短而合并决定。阅读系统现状时先从[架构入口](../architecture/README.md)进入，再按问题查本索引。

没有进入 ADR 的理由也不会丢失：跨实现的当前事实进入 architecture / flow，局部不变量进入代码注释和
高价值测试，模型资格与一次评测进入版本化评测合同或本地报告，已完成实现的过程理由由 Git / PR 历史承担，
尚未成熟的探索进入 scratch。只有已经获得明确授权、仍在规划或实施中的工作才进入 plan，不事后补建。

## 产品入口、路由与会话

| ADR | 当前决定 |
|---|---|
| [ADR-0001](0001-qq-group-report-linked-runtime-evidence.md) | 以 QQ 群显式报障与 NoneBot 本机运行证据关联作为首个真实用户入口 |
| [ADR-0003](0003-unified-capability-guidance-and-incident-intake.md) | 用一个显式支持入口统一承接 guidance、Bug 判断与功能反馈，并由模型外 router 选择单一动作 |
| [ADR-0006](0006-cross-platform-alconna-entry-and-reference-providers.md) | 从第一版采用 Alconna 跨平台入口，并把出站引用差异隔离为 Provider；统一私聊拒绝由 ADR-0028 收窄 |
| [ADR-0020](0020-use-triage-command-for-natural-language-support.md) | 用必选 `triage <自然语言>` 承接求助，不监听普通消息，也不把 Reply 本身当作支持意图 |
| [ADR-0028](0028-allow-private-triage-and-superuser-request-context-replies.md) | 允许 triage 私聊进入统一分流，并向已鉴权 SUPERUSER 的原始提问会话返回完整行为解释 |
| [ADR-0037](0037-make-semantic-assessment-the-default-triage-path.md) | 删除本地意图词表和产品启用开关，每轮 triage 默认经受限语义 assessment，transport 不可用时 abstain |
| [ADR-0060](0060-use-scope-thread-and-post-route-conversation-context.md) | 用稳定 scope Thread 承接一次显式补充；Semantic 只看当前文字，Reply 与会话内容只在路由后作为任务上下文 |
| [ADR-0065](0065-only-expose-conversation-history-for-supported-platforms.md) | 只在 Adapter 有真实会话历史 Provider 时向 Bug Agent 暴露聊天工具；不再用本地滚动窗口模拟跨平台历史 |
| [ADR-0101](0101-use-langgraph-checkpoints-for-long-running-behavior-inquiries.md) | 已替代的加密 LangGraph Behavior 工作区；SUPERUSER 自由对话已迁移到 ADR-0128 |
| [ADR-0128](0128-use-native-message-snapshots-for-maintainer-conversations.md) | 用 Pydantic AI、Harness 压缩和 LocalStore 单文件消息快照保存部署内唯一的维护者自由对话；每轮注入当前场景，忙时直接拒绝新请求，已实施 |
| [ADR-0147](0147-use-five-minute-entry-cooldown-with-superuser-exemption.md) | 统一入口冷却默认 5 分钟；SUPERUSER 在 `triage` 主入口与 `triage 报错查询` 均豁免冷却，非 SUPERUSER 仍按同一 scope 窗口限流 |

## 自治、Agent 控制与远端数据安全

| ADR | 当前决定 |
|---|---|
| [ADR-0002](0002-tiered-autonomy-and-ownership-aware-remediation.md) | 以分级自治、责任层路由和动作专用执行器扩展诊断到修复闭环 |
| [ADR-0010](0010-use-bounded-evidence-seeking-agent-loop.md) | 用单 Agent、typed tools、有界循环、HITL 与 trajectory Gate 验证 Agent 能力 |
| [ADR-0012](0012-use-pydantic-ai-deferred-tools-behind-domain-runtime.md) | 用领域 runtime 掌握循环与授权，只借用 Pydantic AI Deferred Tools 做单步多 Provider 适配 |
| [ADR-0130](0130-finalize-production-agents-before-hard-budget-exhaustion.md) | 生产多步 Agent 最多提示一次收敛阶段，并用动态 `tool_choice="none"` 保留最终交付机会；Bug 数值与工具可见性由 ADR-0145 修正，累计预算由 ADR-0146 修正 |
| [ADR-0145](0145-combine-configurable-bug-budgets-with-finalization.md) | Bug 沿用可配置的离散执行边界和稳定工具 schema，同时保留 checkpoint / finalizing 收尾机制；累计 token 配置由 ADR-0146 移除 |
| [ADR-0146](0146-remove-cumulative-budgets-from-bug-and-maintainer-agents.md) | Bug 与维护者 Agent 不使用累计 token、累计输出或美元熔断，继续由请求、工具、时限、单次输出与单请求窗口边界约束 |
| [ADR-0038](0038-limit-semantic-assessment-remote-data-projection.md) | 只允许向合格语义 assessment transport 投影当前单条规范化 triage 请求文字，其他上下文仍禁止出站 |
| [ADR-0053](0053-allow-relevant-source-and-log-bodies-for-bug-assessment.md) | 允许任务相关源码、日志与 traceback 经专用准入和秘密清理后进入 Bug assessment，同时保持普通用户安全投影 |
| [ADR-0059](0059-share-read-only-evidence-access-across-agent-flows.md) | 跨 Agent 共享受逻辑根、realpath containment、敏感文件拒绝和 revision 约束的只读 Evidence 工具 |
| [ADR-0089](0089-persist-redacted-pydantic-ai-agent-traces.md) | 生产默认仍只保存无正文轨迹；显式维护诊断可保存 assistant 正文与 thinking，但不保存初始 Prompt、密钥或真实私有源码 |
| [ADR-0097](0097-capture-complete-capability-model-output-in-explicit-maintenance-runs.md) | 显式单插件维护运行可把完整模型输出写入本地诊断文件，生产 trace 仍保持脱敏 |

## 发行、部署与模型 Provider

| ADR | 当前决定 |
|---|---|
| [ADR-0007](0007-single-distribution-dual-namespace.md) | 采用单仓库、单发行包、插件入口与领域核心双命名空间结构 |
| [ADR-0008](0008-pydantic-ai-controlled-model-adaptation.md) | 采用 Pydantic AI 的 Model / Provider / Profile 与 Direct Request 作为受控 B1 多模型 API 适配层 |
| [ADR-0016](0016-keep-maintainer-evaluation-tooling-out-of-install-surface.md) | 把维护者 CLI 和历史机器报告排除在插件安装发行面之外；MLflow 依赖与发布安排由 ADR-0125 局部替代 |
| [ADR-0042](0042-use-pydantic-ai-model-profile-for-structured-output.md) | 由 Pydantic AI ModelProfile 唯一决定结构化输出方式，项目只维护任务资格 |
| [ADR-0063](0063-keep-plugin-startup-independent-from-model-enhancements.md) | 未配置或技术不可用的模型增强不得阻断插件导入；未评测组合本身不再触发降级 |
| [ADR-0084](0084-install-pydantic-ai-control-plane-by-default-and-keep-providers-and-adapters-optional.md) | 默认安装 Pydantic AI 控制层、Harness 与导航依赖；具体导航后端由 ADR-0123 替代为 ty；Provider SDK 和 Adapter 按需安装 |
| [ADR-0086](0086-treat-model-evaluation-as-a-quality-label.md) | held-out 只提供公开质量标签；未评测模型和自定义连接可在相同安全合同下运行 |
| [ADR-0090](0090-configure-pydantic-ai-provider-base-urls-at-deployment.md) | 保留标准 `provider:model` 与 ModelProfile，并允许部署者为支持该参数的 Pydantic AI Provider 配置受限 Base URL |
| [ADR-0091](0091-use-pydantic-ai-model-ids-as-the-public-transport-selector.md) | 直接以 Pydantic AI `provider:model` 选择 transport；Base URL 连接兼容服务 |
| [ADR-0092](0092-remove-legacy-model-backend-configuration.md) | 删除旧 backend 字段、专用 runtime 分支和 OpenCode 密钥别名；旧配置明确失败并迁移到唯一的 `provider:model` 入口 |
| [ADR-0129](0129-use-only-pydantic-ai-native-model-transports.md) | 只维护 Pydantic AI 原生模型解析、Provider/Profile、统一 thinking 与 usage；累计预算数值已由 ADR-0146 替代 |

## 评测合同与知识包

| ADR | 当前决定 |
|---|---|
| [ADR-0015](0015-separate-versioned-evals-from-local-runtime-data.md) | 用 `evals/` 保存版本化评测合同，并与本地数据和报告分离；发布边界由 ADR-0016 收紧，MLflow 安排由 ADR-0125 局部替代 |
| [ADR-0125](0125-remove-mlflow-tracking-from-maintainer-evaluations.md) | 移除 MLflow 依赖及专用发布入口，使用本地评测报告追溯与复评，保留 pytest / CI 和评测合同边界 |
| [ADR-0019](0019-distribute-rag-corpus-as-versioned-knowledge-pack.md) | 基础发行包不内置 RAG 语料；默认发现与更新改由 stable catalog 提供 |
| [ADR-0051](0051-let-the-bug-assessment-agent-query-design-rag.md) | 允许 Bug Agent 在历史与公开合同初检未命中后查询版本化设计 RAG，并保持设计、源码与运行证据分层 |
| [ADR-0067](0067-refresh-knowledge-pack-from-stable-catalog-at-startup.md) | 启动后后台检查 stable catalog，校验新包后原子切换；所有更新失败均保留旧包或降级且不阻断插件加载 |
| [ADR-0131](0131-freeze-the-nonebot-only-knowledge-pack.md) | 冻结现有 NoneBot 2.5.0 知识包与 stable catalog，只保留 Markdown 构建和验证代码，不再维护多来源语料或自动发布工作流 |

## 能力影子、行为解释与公开教学

| ADR | 当前决定 |
|---|---|
| [ADR-0021](0021-use-deployment-local-capability-shadow-index.md) | 用字段级证据构建部署本地能力影子索引，先评估再接入回复 |
| [ADR-0024](0024-auto-publish-deterministic-capability-fields.md) | 确定且低风险的命令字段自动公开；其余异常由 ADR-0032 拆为具体 `analysis_issues` |
| [ADR-0025](0025-explain-plugin-behavior-from-deployment-evidence.md) | 用多源部署证据向已鉴权开发者解释插件行为，并区分观察事实、静态推导与未知 |
| [ADR-0026](0026-filter-capability-knowledge-before-retrieval.md) | 在检索与模型前按受众和 adapter 隔离能力知识，普通用户不感知受限或跨 adapter 能力 |
| [ADR-0029](0029-control-model-config-values-with-deployment-deny-list.md) | 由部署者 deny-list 控制能力相关配置值进入模型，原值不持久化或对外披露 |
| [ADR-0032](0032-separate-capability-audience-analysis-and-platform-status.md) | 分离能力受众、平台范围、分析问题与约束，由派生 ServingView 取代 review 审批层 |
| [ADR-0036](0036-keep-capability-shadow-deterministic-and-record-oriented.md) | 保持能力影子确定且以记录为单位，删除无消费者的 Matcher 角色和逐记录源码对齐推断 |
| [ADR-0048](0048-use-public-facts-for-guidance-answer-agent.md) | Guidance 仍由公开事实约束；ADR-0060 允许在路由后加入有界 Thread 与直接 Reply 上下文 |
| [ADR-0057](0057-select-source-analysis-tools-by-evidence-stage.md) | 保留源码分层与 glob/文本兜底；Direct Jedi 后端选择由 ADR-0123 部分替代 |
| [ADR-0123](0123-use-refresh-scoped-ty-definition-navigation.md) | 教学刷新懒启动并共享 ty 进程，握手后并发查询，结束回收；不支持热重载或跨刷新常驻解析器 |
| [ADR-0058](0058-use-deterministic-evidence-and-bounded-navigation-for-teaching-annotations.md) | 教学注释使用 Triage 有界源码导航；确定性层只拥有准入、范围、当前性和验证，不承担工厂业务语义摘要 |
| [ADR-0066](0066-use-active-teaching-contract-as-bug-precheck.md) | 只让当前公开主动能力进入 teaching contract，并在正式 Bug 调查前用模型外公开合同做零工具筛查 |
| [ADR-0080](0080-model-capability-teaching-as-multiple-public-entries.md) | 一次能力分析仍可产生多个固定 entry，Alconna 叶子仍投影为独立帮助条目 |
| [ADR-0083](0083-resolve-unknown-teaching-gates-before-closing-public-knowledge.md) | AST 只登记疑似门禁；Agent 以实际定义、框架或运行配置解释为约束、无约束或仍未知，只有仍未知才关闭公开知识 |
| [ADR-0093](0093-shard-capability-annotation-cache-by-plugin.md) | 教学缓存按插件分片、按单元部分生成，并与不可变 generation 和唯一活动指针明确分离 |
| [ADR-0126](0126-publish-maintainer-boundary-edits-with-teaching-generations.md) | SUPERUSER 精确微调已有行为边界，与结构化注释一起原子发布；人工文字可恢复但不是永久覆盖或新 Evidence |
| [ADR-0127](0127-separate-teaching-condition-shape-from-gate-origin.md) | 教学使用 condition_group 表达有限 OR 组合，按业务含义选择原子条件，不按 Permission / Rule 来源分类 |
| [ADR-0094](0094-simplify-the-public-capability-teaching-contract.md) | 公开 teaching entry 使用六类稳定字段，模型内部表示与 Help / Answer 的确定性公开投影分层 |
| [ADR-0124](0124-express-non-private-teaching-scenes-directly.md) | 允许直接保存非私聊谓词，不强制枚举场景全集；保留更窄独立限制，不扩展通用权限计算器 |
| [ADR-0113](0113-separate-routing-authorization-and-business-readiness-in-teaching.md) | `platform_scope` 留在确定性 Runtime，业务准备状态进入 behavior boundary；role/access 按当前替代关系解释 |
| [ADR-0121](0121-checkpoint-completed-teaching-units-before-atomic-publication.md) | 单元完成后持久化未发布候选和维护诊断；插件仍经单一 generation 原子发布，中断后只补缺失单元 |

## Bug 判定与权威工作流

| ADR | 当前决定 |
|---|---|
| [ADR-0050](0050-use-a-bounded-agent-for-user-bug-assessment.md) | 用受限 Agent 收集 Bug 证据，由确定性 coordinator / reconciler 掌握预算、引用闭合、三值结论和外部副作用边界 |
| [ADR-0052](0052-define-bug-across-the-bot-software-responsibility-chain.md) | 把普通用户 Bug verdict 定义到整个 Bot 软件责任链，并单独保留内部责任候选 |
| [ADR-0068](0068-treat-qualified-agent-bug-verdicts-as-operational-decisions.md) | 经模型外 reconciler 接受的 Agent Bug 是正式 verdict，人工负责事后复核与改判 |
| [ADR-0070](0070-separate-bug-reports-occurrences-and-problems.md) | 用薄 Report、具体 Occurrence 和长期 Problem 分离提交次数、实际发生次数与问题生命周期，并记录 Sentry Event → Issue 参考 |
| [ADR-0071](0071-group-bug-problems-with-versioned-evidence-fingerprints.md) | 用模型外版本化 Evidence 指纹聚合同根因，禁止文本自动合并，并固定 Bug 的“已记录 / 已关联”回执 |
| [ADR-0072](0072-use-opaque-problem-ids-and-minimal-maintainer-lifecycle.md) | 向普通用户显示中性短问题编号，固定 Bug 事务回执，并定义最小维护动作 |
| [ADR-0073](0073-use-nonebot-orm-for-authoritative-bug-workflow-state.md) | 用 NoneBot ORM 的事务保存 Report、Occurrence、Problem、公开 ID / alias 与人工维护生命周期，保留其他状态的原有分层 |
| [ADR-0074](0074-preserve-append-only-problem-decisions.md) | 追加保存 Agent 判断、人工确认和 override，并让 Problem 当前 verdict 成为同事务更新的查询投影 |
| [ADR-0075](0075-register-problem-maintenance-under-triage-subcommand.md) | 把问题查询、确认 Bug / 非 Bug 与解决注册为真实 `triage 报错查询` 子命令，并在 Semantic 之前确定性鉴权分流 |
| [ADR-0078](0078-defer-persisting-unknown-bug-assessments.md) | 在可记录性合同确定前不持久化任何 unknown，缺关键知识时失败关闭且不声称已记录 |
| [ADR-0132](0132-derive-bug-candidate-verdict-from-reason.md) | 模型选择判断原因，程序派生候选 verdict；保留最终证据检查和下游决策结构 |
| [ADR-0133](0133-read-bug-member-evidence-from-the-bound-snapshot.md) | 按 ID 读取本轮绑定的公开成员快照，共用调查工具与证据预算 |
| [ADR-0134](0134-share-public-guidance-facts-with-bug-investigation.md) | Bug 调查沿用公开初检事实，避免重复生成同一教学正文 |
| [ADR-0135](0135-expand-bug-member-directories-on-demand.md) | 首轮提供教学单元目录，需要时再按 unit_ref 展开完整成员 |
| [ADR-0136](0136-configure-bug-investigation-budgets.md) | 部署可调 Bug 超时、单次输出和工具额度，默认 12 次通用取证 / 15 次请求并保持稳定工具定义；累计 token 配置由 ADR-0146 移除 |
| [ADR-0137](0137-interpret-support-supplements-with-the-pending-question.md) | Semantic 结合首轮问题与实际追问理解补充；明确的新任务独立判断 |
| [ADR-0138](0138-combine-support-intent-and-plugin-selection.md) | 一次联合判断支持意图与公开插件对象，并在选定范围内交接 Bug |
| [ADR-0139](0139-persist-readable-bug-investigations-with-plugin-scope.md) | 保存可读 Bug 调查结论与已确认插件范围，支持插件级建档 |
| [ADR-0140](0140-use-explicit-failure-fingerprints-and-reversible-grouping.md) | 分离故障指纹与证据版本，支持保守聚合、审计拆分和歧义停用 |
| [ADR-0141](0141-stop-bug-investigation-without-generic-supplements.md) | 调查返回 unknown 时保留具体证据缺口并结束，不再自动追加通用追问 |
| [ADR-0142](0142-hide-superuser-paths-from-public-capability-materials.md) | 公开目录、教学与帮助统一隐藏超级用户专属路径，同时保留真实鉴权 |
| [ADR-0143](0143-share-two-supplements-across-support-and-bug-assessment.md) | 普通求助与调查前澄清共用最多两轮补充并保留问答 |
| [ADR-0144](0144-resume-bug-investigation-from-public-precheck.md) | 已选插件成为调查范围，复核公开资料快照并携带初检实际输入输出 |
