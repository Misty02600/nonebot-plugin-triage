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
| [ADR-0008](0008-pydantic-ai-controlled-model-adaptation.md) | 已采纳 | 采用 Pydantic AI 的 Model / Provider / Profile 与 Direct Request 作为受控多模型 API 适配层 |
| [ADR-0009](0009-use-async-model-boundary.md) | 已采纳 | 模型调用核心采用异步协议，同步 CLI 只在进程边缘桥接 |
| [ADR-0010](0010-use-bounded-evidence-seeking-agent-loop.md) | 已采纳 | 用单 Agent、typed tools、有界循环、HITL 与 trajectory Gate 验证 Agent 能力 |
| [ADR-0011](0011-expose-disabled-qualified-model-configuration.md) | 已采纳 | 公开默认关闭、无密钥/base URL 且只装配已准入组合的 NoneBot 模型配置 |
| [ADR-0012](0012-use-pydantic-ai-deferred-tools-behind-domain-runtime.md) | 已采纳 | 用领域 runtime 掌握循环与授权，只借用 Pydantic AI Deferred Tools 做单步多 Provider 适配 |
| [ADR-0013](0013-use-mandatory-output-tool-for-opencode-go-b1.md) | 未采纳 | 不把一次 OpenCode Go 测试升级为 B1 输出契约或产品网关决定 |
| [ADR-0014](0014-use-observation-first-production-trials.md) | 部分被替代 | 先用零模型、脱敏、可反馈的观察型生产 trial 建立真实评测闭环 |
| [ADR-0015](0015-separate-versioned-evals-from-local-runtime-data.md) | 部分被替代 | 用 `evals/` 保存版本化评测合同，并与本地数据、报告和 MLflow 运行状态分离；冻结机器报告的发布边界由 ADR-0016 收紧 |
| [ADR-0016](0016-keep-maintainer-evaluation-tooling-out-of-install-surface.md) | 已采纳 | 保留双命名空间领域核心，但把维护者 CLI、MLflow 和历史机器报告排除在插件安装发行面之外 |
| [ADR-0017](0017-run-deterministic-evaluations-through-pytest.md) | 已采纳 | 通过现有 pytest job 执行确定性评测回归，当前不增加专用 job、摘要或 Artifact |
| [ADR-0018](0018-use-localstore-only-for-enabled-trial-audit-log.md) | 已采纳 | 只用 LocalStore 保存显式启用的 trial 审计 JSONL，其余诊断关联继续保持内存态 |
| [ADR-0019](0019-distribute-rag-corpus-as-versioned-knowledge-pack.md) | 已采纳 | 基础发行包不内置 RAG 语料；产品需要时再发布独立、可选、版本化的离线知识包 |
| [ADR-0020](0020-use-triage-command-for-natural-language-support.md) | 已采纳 | 用必选 `triage` 指令承接自然语言求助；ADR-0031 将该要求恢复并细化到 Thread 续问 |
| [ADR-0021](0021-use-deployment-local-capability-shadow-index.md) | 已采纳 | 用字段级证据构建默认关闭的部署本地能力影子索引，先评估再接入回复 |
| [ADR-0022](0022-limit-capability-shadow-guidance-to-superusers.md) | 已采纳 | 只在模型外 SUPERUSER 鉴权后把影子候选接入 triage 维护者回复 |
| [ADR-0023](0023-defer-orm-until-durable-business-state.md) | 已采纳 | 按状态语义分层存储，等权威业务状态需要事务与恢复时再评审并优先复用 ORM 基础设施 |
| [ADR-0024](0024-auto-publish-deterministic-capability-fields.md) | 已采纳 | 确定且低风险的命令字段自动公开；其余异常由 ADR-0032 拆为具体 `analysis_issues` |
| [ADR-0025](0025-explain-plugin-behavior-from-deployment-evidence.md) | 已采纳 | 用多源部署证据向已鉴权开发者解释插件行为，并区分观察事实、静态推导与未知 |
| [ADR-0026](0026-filter-capability-knowledge-before-retrieval.md) | 已采纳；回答投影由 ADR-0027 细化 | 在检索与模型前按受众和 adapter 隔离能力知识，普通用户不感知受限或跨 adapter 能力 |
| [ADR-0027](0027-constrain-guidance-with-facts-not-fixed-wording.md) | 已采纳 | 用事实输出合同约束能力帮助，模型自由组织措辞并具体说明公开能力的可验证约束 |
| [ADR-0028](0028-allow-private-triage-and-superuser-request-context-replies.md) | 已采纳 | 允许 triage 私聊进入统一分流，并向已鉴权 SUPERUSER 的原始提问会话返回完整行为解释 |
| [ADR-0029](0029-control-model-config-values-with-deployment-deny-list.md) | 已采纳 | 由部署者 deny-list 控制能力相关配置值进入模型，原值不持久化或对外披露 |
| [ADR-0030](0030-continue-support-thread-by-exact-reply.md) | 已替代 | 曾允许精确回复 Triage 已登记回答免命令续问；触发入口由 ADR-0031 收紧 |
| [ADR-0031](0031-require-triage-for-support-thread-continuation.md) | 部分被替代 | 所有支持轮次都要求显式 `triage`；Reply 提交时机由 ADR-0033 细化 |
| [ADR-0032](0032-separate-capability-audience-analysis-and-platform-status.md) | 已采纳 | 分离能力受众、平台范围、分析问题与约束，由派生 ServingView 取代 review 审批层 |
| [ADR-0033](0033-serialize-support-thread-turns-with-single-use-reply-claims.md) | 已采纳 | 一次性消费 Reply，并用单 Thread lease 串行化支持处理轮 |
| [ADR-0034](0034-distinguish-matchers-from-user-observable-capabilities.md) | 已采纳 | 先以 Matcher 等构建期事实为锚点，再按用户可观察效果归并能力；支撑 Matcher 压缩为证据 |
