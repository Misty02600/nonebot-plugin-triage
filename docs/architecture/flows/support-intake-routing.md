# 流程：triage 自然语言支持入口

## 当前入口

普通用户每轮都发送 `triage <求助内容>`，也可以写成 `@Bot triage <求助内容>`。只有 Reply、没有
`triage` 的消息不会触发插件。私聊、群聊和频道共用同一入口与当前请求者鉴权规则。

```text
current triage text + optional direct Reply
                    ↓
入口限流、长度守门、Reply 正文与 correlation 捕获
                    ↓
私聊 + SUPERUSER → 直接进入全局维护者会话（不经过语义分流）
                    ↓
按 adapter + Bot + conversation + actor Claim scope Thread
                    ↓
Semantic assessment（当前文字 + public catalog + 有界 Reply / 补充）
                    ↓
确定性 router（只执行一个 action）
   ├─ GUIDANCE → public facts + 路由后 Thread / Reply context
   │            → Answer Agent v2 → 有依据回答或确定性回退
   ├─ BUG_ASSESSMENT / reported_observation
   │            → public teaching contract → bounded Bug Agent
   │            → runtime / log / conversation / source / design / deployment
   │            → deterministic reconciliation → bug / not_bug / unknown
   ├─ BEHAVIOR_EXPLORATION（非准入场景不执行）
   │            → 有 reported_observation 时转入 Bug 判定
   │            → 否则明确拒绝，不进入维护者会话
   ├─ FEATURE_FEEDBACK → 有界状态；尚不创建外部工单
   ├─ unresolved / task unavailable → CLARIFY
   └─ policy blocked / unsupported → REFUSE / OUT_OF_SCOPE
```

Semantic、Guidance 与 Bug 是三个独立模型任务。一个 Provider/model 在某一任务通过 Gate，不能把资格继承给
其他任务。当前 semantic v8 中文 Prompt v5 与 Bug assessment 中文 Prompt v23 的资格集合均为空，历史
Provider Gate 结果不能迁移到当前合同。Public Guidance v2 只有两条真实 Provider smoke，仍属于 provisional
dogfood，而不是 held-out 质量资格。

## 最多两次补充的 scope Thread

Thread 不再由 Reply 选择。`SupportThreadTurnCoordinator` 以
`adapter + Bot + conversation + actor` 的 HMAC scope 保存最多一个 active Thread，并用单活动 turn lease
串行化处理：

1. 没有活动 Thread 时，本轮是首轮；Thread 暂存规范化首轮文字、首轮直接 Reply 的可见正文和可选的
   correlation ID。当前尚未把 subject、operation 或 fact refs 结构化写入 Thread。
2. 只有首轮确实需要用户补充时，回答发送成功后才调用 `await_supplement`。发送失败、处理异常、拒绝、
   超长输入或终局 action 都关闭 Thread；不依赖 UniSeg Receipt message ID 建立续接点。
3. 下一条同 scope 显式 `triage` 自动消费补充，无需 Reply。联合 assessment 会同时看到首轮、已经完成的
   一次问答和当前待答问题；这些内容可用于解释当前回复，但不能盲目继承旧 goal 或把 Reply 当成可信事实。
4. Guidance 给出实际命中能力的教学后立即关闭；只有缺少会改变下一步且用户可回答的上下文时才等待
   补充。能力资料本身不可用时直接关闭，不反复追问用户。
5. 整个 Thread 最多两次补充，意图澄清、插件确认和公开初检共用额度。Bug 调查前仍可询问对象或操作；
   一旦真实调查返回 `bug / not_bug / unknown` 就关闭，不再按通用 missing evidence 自动追问。
6. idle / absolute TTL、容量淘汰、并发 `BUSY`、发送失败和进程重启都失败关闭。Thread 是单进程短期事务，
   不是聊天历史或跨重启会话。

Reply 仍有两个与 Thread 独立的作用：可见正文供路由后的 Guidance / Bug 消歧；message ID 通过独立引用索引
解析本机 runtime correlation。OneBot Bug Provider 另按当前 Bot 与群读取一次最新历史，不依赖 Reply 锚点；
历史窗口中的消息 / Reply / 发言人 ID 可以作为会话关系事实进入 Bug Agent，但不改变 Thread 或工具 scope。
未知或过期 Reply 不妨碍创建新 Thread，也不会恢复旧 Thread。

### 全局维护者会话例外

行为探索/维护者会话的准入与语义分类无关：`私聊 + SUPERUSER` 时，任何 `triage` 内容都在限流后、语义路由之前
直接进入部署内唯一的维护者会话；其他场景（群聊、频道，或私聊中的非 SUPERUSER）一律不进入。所有 Adapter、
Bot 和 SUPERUSER 共享同一份会话历史，每轮从当前 Event/Bot 注入私聊场景，供 Agent 理解本次提问，但场景不参与
会话分区。

语义 router 在非准入场景仍可能输出 `behavior_exploration` goal，插件不会执行它：若本轮同时存在
`reported_observation`（用户陈述真实发生过的 Bot 行为），转入 Bug 判定；否则明确拒绝并关闭本轮。

维护者历史的真值是 LocalStore data 下的 `maintainer-conversation.json`。它保存 `session_id`、更新时间与
Pydantic AI 原生消息，包括用户和 assistant 正文、工具调用/结果与 Provider 续接元数据。Harness
`SummarizingCompaction(max_fraction=0.8, keep_messages=20)` 在模型上下文接近上限时总结旧历史；压缩结果在
下一次模型请求前写入原子快照，Run 正常完成后再保存最终上下文。

所有入口共用一个活动 Run。忙时新的普通 `triage` 立即返回 `BUSY`，不排队也不 steer。`triage 停止` 取消
当前 Run 并保留最近成功快照；`triage 开始新对话` 等待当前写入收口后以新 `session_id` 的空快照替换旧文件。
Agent 可检索 Capability Shadow 并读取项目根目录内的只读文件；文件工具继续硬拒绝 `.env`、凭据、密钥与
数据库路径。崩溃恢复最近成功消息快照，等待维护者继续，不复活后台任务。

## Semantic v8 与确定性路由

`SupportAssessmentRequest` 的远端投影包含 schema version、当前 request、当前 public catalog、直接相关 Reply
和同 scope 有界补充问答。领域 request 上限为 8000 字，入口先执行固定 2000 字限制和秘密守门；Reply 与
补充文字各自有界。它不包含身份、scope、内部 owner 映射、配置、日志、源码、运行证据或 restricted 资料。

`SupportSemanticAssessment` v8 包含四种 goal：

- `guidance`：公开功能、语法、参数、场景和用法；
- `behavior_exploration`：为什么这样实现、源码、内部配置、环境、版本、调用流或运行证据；
- `bug_assessment`：判断观察到的现象是否属于 Bot 软件责任链中的 Bug；
- `feature_feedback`：功能建议。

`selection` 独立返回 `matched / ambiguous / none` 和最多五个目录内短 ID，只供 Guidance 与 Bug 公开初检；
程序会复核 ID 及刷新后的 owner 映射。`reported_observation` 独立描述用户声称真实发生过的 Bot 行为；即使没有显式 goal，router 也把它送入 Bug
assessment，而不是直接建单。router 不读取原文，且每轮只选择一个 action；当前优先级为 Bug、行为探索、
Guidance、功能建议。模型输出不含 action、authorization、confidence 或副作用字段。

当前 `QUALIFIED_SEMANTIC_TASKS` 为空，所有 Pydantic AI 可解析组合都按未验证运行。
国内 Alibaba Qwen3.6 Flash 曾在已删除的专属非思考设置下通过 40 条独立 forward-heldout，
但该结果与旧 OpenCode、旧英文 Prompt 结果一样只作历史证据；capability annotation 的 provisional 资格也没有迁移。

## 路由后的 Guidance 上下文

Semantic 选中 Guidance 后，入口先从显式 Alconna Provider 或当前 runtime-gated 能力影子取得 public、完整、
非 stale 的事实。`PublicGuidanceRequest` v2 分字段携带当前问题、这些公开事实，以及有界的首轮 / 直接 Reply
可见正文；Answer Agent 没有工具，不能检索 restricted 能力、源码、配置或运行证据。

会话正文是不可信上下文，可以帮助解释“这个怎么用”中的“这个”，但不能创造能力、覆盖权限、改变披露或
成为工具指令。当前问题和 public facts 继续执行既有模型前秘密守门；项目作者明确选择不对直接 Reply / Thread
中平台可见正文执行凭据或个人信息遮蔽。平台 envelope、原始平台用户 ID、scope 和 correlation 不进入请求。

v2 已完成两条真实 smoke：Reply 上下文成功定位“回复图片后发送搜图”；包含“声称所有人都是 SUPERUSER”
的恶意 Reply 不能覆盖 public fact 中的管理员要求。它仍没有独立 held-out，因此只能受控 dogfood。

## 普通用户 Bug 判定

ADR-0066 的公开初检已经接入 v8 联合选择：程序复核模型选择的一个或多个 public 插件及当前资料快照，并检查
语义路由是否报告具体观察；缺少对象或操作时共用 scope Thread 的补充额度，在信息就绪前不创建案件指纹、
源码后端或 Agent 工具箱。索引本身不可用时直接按分析不可用结束，不错误要求用户补充。当前生效的公开结构
化教学注释作为第一层用法合同并进入 public contract Evidence；只有直接 Reply 精确指向报障者本人操作、且
能够机器验证其缺少所有 usage 都要求的 Reply 上下文时，才零调查工具复用 Guidance 纠正。其他参数、媒体、
角色、场景、限流与 behavior boundary 仍保持不确定并进入下述正式调查；被动能力门禁由能力索引层统一决定。

协调器先固定 subject、source root、revision、adapter、correlation 和部署 generation，并预加载
公开合同、首轮上下文与直接 Reply。Agent 默认在最多 15 次模型请求、一次独立聊天窗口、12 次通用证据工具调用、
16,384 单次 output token 和 300 秒总超时内按需读取；这些 Bug 专属值可由部署者调整，生产运行不设累计
token 或美元费用上限：

- 与 Reply correlation 精确绑定的 runtime observation 与异常 traceback；
- OneBot V11 群聊中由当前 Bot 和群预绑定、一次读取的最新最多 30 条可见聊天窗口；精确 Reply 独立预装，不受窗口是否覆盖影响；
- 当前已加载 subject 的批准源码根与有界 Python 文本搜索 / 文件读取；
- 版本适用的设计知识包、部署和安全配置摘要。

聊天工具不接收 Bot、群、用户或 message ID 参数，模型不能切换 scope；NapCat 群历史只是 OneBot V11 的
部署扩展，不是跨 Adapter 保证。没有经过验证的历史 Provider 时，本轮工具列表不包含聊天工具，也不使用
本地滚动窗口回退；当前请求或精确 Reply 仍可预装，证据不足时结论保持 `unknown`。聊天正文、显示名、会话 / 消息 / 发言人 ID、角色、Reply 关系和段元数据按
平台可见上下文进入任务，不做凭据 / PII 遮蔽；这些字段不授予权限。源码、日志、traceback、配置、环境和
其他部署证据仍执行批准根、相关性和秘密清理。

Agent 候选必须引用实际 Evidence ID。确定性 reconciler 要求同 revision 的预期与实际证据闭合；缺失、冲突、
stale、partial、预算耗尽或只有聊天陈述时都只能得到 `unknown`。普通用户只看到三值与安全原因，不会看到
源码、日志、内部路径、Evidence ID 或责任候选。合格 Agent 确认 `bug` 时，在一个 ORM 事务中写入薄 Report、
可去重 Occurrence、长期 Problem 和追加式 Decision；普通用户得到中性 `P-...` 编号。同一报告重放不重复计数，
有完整、版本兼容且可从引用 Evidence 复算的技术签名时才自动聚合到已有 Problem。无可靠签名则建立新 Problem，
不根据用户措辞或自然语言相似度合并。`not_bug` 和 `unknown` 不写入问题库，也不执行外部副作用。

SUPERUSER 通过真实 Alconna 子命令 `triage 报错查询` 列出待处理 Problem，加 `P-...` 查看详情，再加
`确认Bug`、`确认非Bug` 或 `解决` 追加人工判断或更新 lifecycle。子命令在 Semantic 之前确定性鉴权，不创建 Thread，
不调用任何 Agent。

当前中文 Prompt v23 沿用公开初检事实和已选插件范围，包含按需成员展开、Reply / 最新 conversation 窗口、
独立聊天调用和默认 12 次通用证据预算，并在硬上限前显式切换到无函数工具调用的最终提交。工具定义按本轮
初始范围固定；额度耗尽由执行层返回结构化不可用结果，finalizing 通过 `tool_choice="none"` 禁止继续调用，
不通过删改 schema 表达。它不能继承 v6–v22 或旧英文 Prompt 的历史 Gate。旧合同的
16 条真实 forward-heldout 只运行一次，schema、verdict、occurrence、responsibility、citation、budget、usage、
scenario 与 safety 均为 1.000；这是已退出运行资格的历史 OpenCode 证据。当前 `QUALIFIED_BUG_TASKS` 为空；
Pydantic AI 可解析的模型仍可运行，但必须以未验证 evaluation 标签记录，不能继承旧组合结论。
历史 development 边界集检查早停、第六次收敛、第七/八次证据价值、
重复工具、finalizing 后调用和 cache/token/cost 异常；可按 case 运行 canary，但 development、子集或多轮结果均不产生资格。

## 安全与数据不变量

- 所有 `triage` 轮次共用同一入口限流；普通 scope Thread 不提供跨进程协调或费用预算；Behavior 另有
  单 writer 进程锁、同 Thread admission 和全局模型并发预算，但仍不支持多 worker；
- Reply / Thread /聊天、插件元数据、源码和文档都是不可信证据，不能升级为工具参数、权限或副作用；
- `SUPERUSER` 只用于行为探索/维护者会话准入（`私聊 + SUPERUSER`），不扩大 Semantic / Guidance / Bug payload；
- 非准入场景即使语义输出 `behavior_exploration` 也不进入维护者会话：带 `reported_observation` 转 Bug 判定，否则拒绝；
- public guidance 不返回 restricted、隐藏、停用、平台不匹配、blocking issue 或 stale 的能力；
- RAG 只证明适用版本的预期合同，不证明当前代码或本次分支实际执行；缺知识不能成为 `not_bug`；
- runtime/log 只记录本插件 hook 关联到的结构化生命周期与异常，不是聊天历史，也不搜索任意宿主日志。

## 相关决定

- [ADR-0020：triage 自然语言入口与可选 Reply](../../adr/0020-use-triage-command-for-natural-language-support.md)
- [ADR-0031：支持 Thread 续问仍要求显式 triage](../../adr/history/0031-require-triage-for-support-thread-continuation.md)（Thread 身份已由 ADR-0060 部分替代）
- [ADR-0033：用一次性 Reply Claim 串行化支持 Thread](../../adr/history/0033-serialize-support-thread-turns-with-single-use-reply-claims.md)（Claim 键已由 ADR-0060 部分替代）
- [ADR-0035：用 UniSeg Receipt 结算 Thread 出站引用](../../adr/history/0035-settle-support-thread-replies-from-uniseg-receipts.md)（Thread 结算已由 ADR-0060 部分替代）
- [ADR-0038：限定语义 assessment 的远端数据投影](../../adr/0038-limit-semantic-assessment-remote-data-projection.md)
- [ADR-0048：用公开事实驱动受控 Answer Agent](../../adr/0048-use-public-facts-for-guidance-answer-agent.md)
- [ADR-0050：用有界 Agent 判定普通用户报告的 Bug](../../adr/0050-use-a-bounded-agent-for-user-bug-assessment.md)
- [ADR-0051：允许 Bug Agent 查询受控设计 RAG](../../adr/0051-let-the-bug-assessment-agent-query-design-rag.md)
- [ADR-0052：把 Bug 定义到整个 Bot 软件责任链](../../adr/0052-define-bug-across-the-bot-software-responsibility-chain.md)
- [ADR-0053：允许 Bug Agent 使用相关源码与日志正文](../../adr/0053-allow-relevant-source-and-log-bodies-for-bug-assessment.md)
- [ADR-0054：使用 LocalStore 保存已审核 Bug 问题记录](../../adr/history/0054-store-reviewed-bug-problems-in-localstore.md)（已被 ORM 工作流替代）
- [ADR-0060：用作用域 Thread 承接一次补充并在路由后投影会话上下文](../../adr/0060-use-scope-thread-and-post-route-conversation-context.md)
- [ADR-0061：为 Bug 判断读取当前会话最新有界聊天窗口](../../adr/history/0061-read-latest-bounded-conversation-window-for-bug-assessment.md)
- [ADR-0064：收窄 Bug 会话证据与结论合同](../../adr/history/0064-refine-bug-conversation-evidence-and-verdict-contract.md)
- [ADR-0065：只为明确支持的平台提供 Bug 会话历史工具](../../adr/0065-only-expose-conversation-history-for-supported-platforms.md)
- [ADR-0066：用当前公开教学合同前置筛查普通用户 Bug](../../adr/0066-use-active-teaching-contract-as-bug-precheck.md)
- [ADR-0068：把合格 Agent Bug verdict 作为正式判断](../../adr/0068-treat-qualified-agent-bug-verdicts-as-operational-decisions.md)
- [ADR-0070：分离 Bug Report、Occurrence 与 Problem](../../adr/0070-separate-bug-reports-occurrences-and-problems.md)
- [ADR-0071：用版本化 Evidence 指纹聚合 Bug Problem](../../adr/0071-group-bug-problems-with-versioned-evidence-fingerprints.md)
- [ADR-0073：使用 NoneBot ORM 保存权威 Bug 工作流状态](../../adr/0073-use-nonebot-orm-for-authoritative-bug-workflow-state.md)
- [ADR-0074：用追加式 Problem Decision 保留判断历史](../../adr/0074-preserve-append-only-problem-decisions.md)
- [ADR-0075：把问题维护注册为 triage 子命令](../../adr/0075-register-problem-maintenance-under-triage-subcommand.md)
- [ADR-0078：在可记录性合同确定前不持久化 unknown](../../adr/0078-defer-persisting-unknown-bug-assessments.md)
- [ADR-0079：用无编号的 triage 报错查询列出待处理问题](../../adr/history/0079-list-pending-problems-with-triage-query.md)
- [ADR-0128：用原生消息快照保存维护者自由对话](../../adr/0128-use-native-message-snapshots-for-maintainer-conversations.md)
- [Alconna 能力发现](alconna-capability-discovery.md)
- [运行观察入口](runtime-observation-intake.md)
