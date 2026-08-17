# 流程：triage 自然语言支持入口

## 当前入口

普通用户每轮都发送 `triage <求助内容>`，也可以写成 `@Bot triage <求助内容>`。只有 Reply、没有
`triage` 的消息不会触发插件。私聊、群聊和频道共用同一入口与当前请求者鉴权规则。

```text
current triage text + optional direct Reply
                    ↓
入口限流、长度守门、Reply 正文与 correlation 捕获
                    ↓
按 adapter + Bot + conversation + actor Claim scope Thread
                    ↓
Semantic assessment（只接收当前文字）
                    ↓
确定性 router（只执行一个 action）
   ├─ GUIDANCE → public facts + 路由后 Thread / Reply context
   │            → Answer Agent v2 → 有依据回答或确定性回退
   ├─ BUG_ASSESSMENT / reported_observation
   │            → public teaching contract → bounded Bug Agent
   │            → runtime / log / conversation / source / design / deployment
   │            → deterministic reconciliation → bug / not_bug / unknown
   ├─ BEHAVIOR_EXPLORATION → 模型外 SUPERUSER 鉴权；受限取证尚未接通
   ├─ FEATURE_FEEDBACK → 有界状态；尚不创建外部工单
   ├─ unresolved / task unavailable → CLARIFY
   └─ policy blocked / unsupported → REFUSE / OUT_OF_SCOPE
```

Semantic、Guidance 与 Bug 是三个独立模型任务。一个 Provider/model 在某一任务通过 Gate，不能把资格继承给
其他任务。当前 semantic v7 中文 Prompt v5 与 Bug assessment 中文 Prompt v8 已分别通过自己的真实 Provider
Gate，并只登记各自精确组合。Public Guidance v2 只有两条真实 Provider smoke，仍属于 provisional
dogfood，而不是 held-out 质量资格。

## 一次补充的 scope Thread

Thread 不再由 Reply 选择。`SupportThreadTurnCoordinator` 以
`adapter + Bot + conversation + actor` 的 HMAC scope 保存最多一个 active Thread，并用单活动 turn lease
串行化处理：

1. 没有活动 Thread 时，本轮是首轮；Thread 暂存规范化首轮文字、首轮直接 Reply 的可见正文和可选的
   correlation ID。当前尚未把 subject、operation 或 fact refs 结构化写入 Thread。
2. 只有首轮确实需要用户补充时，回答发送成功后才调用 `await_supplement`。发送失败、处理异常、拒绝、
   超长输入或终局 action 都关闭 Thread；不依赖 UniSeg Receipt message ID 建立续接点。
3. 下一条同 scope 显式 `triage` 自动消费唯一一次补充，无需 Reply。补充轮的当前文字仍必须自己形成可路由
   goal 或 `reported_observation`；Thread / Reply 只能补 subject、操作和证据，不能让“继续”“看看这个”
   自动变成意图。
4. Guidance 给出实际命中能力的教学后立即关闭；只有“有能力可枚举但用户没有说明具体功能”才等待一次
   补充。能力资料本身不可用时直接关闭，不反复追问用户。
5. Bug 得到 `bug` 或 `not_bug` 后关闭；首轮 `unknown` 可请求一次实际操作、Bot 返回或报错，补充轮仍
   `unknown` 则以预算耗尽关闭。第二轮不会再次等待。
6. idle / absolute TTL、容量淘汰、并发 `BUSY`、发送失败和进程重启都失败关闭。Thread 是单进程短期事务，
   不是聊天历史或跨重启会话。

Reply 仍有两个与 Thread 独立的作用：可见正文供路由后的 Guidance / Bug 消歧；message ID 通过独立引用索引
解析本机 runtime correlation。OneBot Bug Provider 另按当前 Bot 与群读取一次最新历史，不依赖 Reply 锚点；
历史窗口中的消息 / Reply / 发言人 ID 可以作为会话关系事实进入 Bug Agent，但不改变 Thread 或工具 scope。
未知或过期 Reply 不妨碍创建新 Thread，也不会恢复旧 Thread。

## Semantic v7 与确定性路由

`SupportAssessmentRequest` 的远端投影仍闭合为 `schema_version + request_text`。领域上限为 8000 字，入口先
执行固定 2000 字限制和秘密守门。它不包含 Reply、Thread、身份、scope、配置、日志、源码、运行证据或能力
索引。

`SupportSemanticAssessment` v7 只包含四种 goal：

- `guidance`：公开功能、语法、参数、场景和用法；
- `behavior_exploration`：为什么这样实现、源码、内部配置、环境、版本、调用流或运行证据；
- `bug_assessment`：判断观察到的现象是否属于 Bot 软件责任链中的 Bug；
- `feature_feedback`：功能建议。

`reported_observation` 独立描述用户声称真实发生过的 Bot 行为；即使没有显式 goal，router 也把它送入 Bug
assessment，而不是直接建单。router 不读取原文，且每轮只选择一个 action；当前优先级为 Bug、行为探索、
Guidance、功能建议。模型输出不含 action、authorization、confidence 或副作用字段。

当前中文 v7 Prompt v5 已通过 40 条独立 forward-heldout；schema、status 与 exact 均为 1.000，
`QUALIFIED_SEMANTIC_TASKS` 只登记该精确组合。旧英文 Prompt 或 capability annotation 的 provisional
资格没有迁移。

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

ADR-0066 的首个保守纵切已经接入：模型外 resolver 先从当前 ServingView 定位唯一 public 主动能力，并检查
语义路由是否报告具体观察；缺少能力或观察时共用 scope Thread 的一次补充，在信息就绪前不创建案件指纹、
源码后端或 Agent 工具箱。索引本身不可用时直接按分析不可用结束，不错误要求用户补充。当前生效的公开结构
化教学注释作为第一层用法合同并进入 public contract Evidence；只有直接 Reply 精确指向报障者本人操作、且
能够机器验证其缺少所有 usage 都要求的 Reply 上下文时，才零调查工具复用 Guidance 纠正。其他参数、媒体、
角色、场景、限流与 behavior boundary 仍保持不确定并进入下述正式调查；被动能力门禁由能力索引层统一决定。

协调器先固定 subject、source root、revision、adapter、correlation 和部署 generation，并预加载
公开合同、首轮上下文与直接 Reply。Agent 在最多 9 次模型请求、一次独立聊天窗口、6 次通用证据工具调用、120k total token、
800 output token 和 0.50 美元单轮上限内按需读取：

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
不根据用户措辞或自然语言相似度合并。`not_bug` 和 `unknown` 不写入问题库，也不创建 incident 或外部工单。

SUPERUSER 通过真实 Alconna 子命令 `triage 报错查询` 列出待处理 Problem，加 `P-...` 查看详情，再加
`确认Bug`、`确认非Bug` 或 `解决` 追加人工判断或更新 lifecycle。子命令在 Semantic 之前确定性鉴权，不创建 Thread，
不调用任何 Agent。

当前中文 Prompt v8 包含 Reply / 最新 conversation 窗口、独立聊天调用和 6 次通用证据预算，并通过初始信封
明确告知 Agent 本案是否存在聊天历史 Provider。它不能继承 v6、v7 或旧英文 Prompt 的历史 Gate。全新的
16 条真实 forward-heldout 只运行一次，schema、verdict、occurrence、responsibility、citation、budget、usage、
scenario 与 safety 均为 1.000；`QUALIFIED_BUG_TASKS` 只登记对应 Prompt / Fixture / 策略 / 预算 / evaluation
revision 的精确组合，匹配部署配置与密钥后才建立真实 Bug Agent client。

## 保留但不在当前入口可达的 Incident 兼容层

`OPEN_INCIDENT`、`IncidentAuthorization`、`LiveReportService`、短期 Incident 与 trial 查询仍作为旧领域兼容
能力保留；v7 已删除 `incident_intake` goal，当前 semantic router 和 handler 不会产生这条 action。它们不能
被文档、固定文字、模型候选或聊天正文直接调用。未来若重新接入写入生命周期，需要新的明确副作用授权与
模型外证据门。

## 安全与数据不变量

- 所有 `triage` 轮次共用同一入口限流；scope Thread 不提供跨进程协调或费用预算；
- Reply / Thread /聊天、插件元数据、源码和文档都是不可信证据，不能升级为工具参数、权限或副作用；
- `SUPERUSER` 只在 router 已选中 behavior exploration 后模型外鉴权，不扩大 Semantic / Guidance / Bug payload；
- public guidance 不返回 restricted、隐藏、停用、平台不匹配、blocking issue 或 stale 的能力；
- RAG 只证明适用版本的预期合同，不证明当前代码或本次分支实际执行；缺知识不能成为 `not_bug`；
- runtime/log 只记录本插件 hook 关联到的结构化生命周期与异常，不是聊天历史，也不搜索任意宿主日志。

## 相关决定

- [ADR-0020：triage 自然语言入口与可选 Reply](../../adr/0020-use-triage-command-for-natural-language-support.md)
- [ADR-0031：支持 Thread 续问仍要求显式 triage](../../adr/0031-require-triage-for-support-thread-continuation.md)（Thread 身份已由 ADR-0060 部分替代）
- [ADR-0033：用一次性 Reply Claim 串行化支持 Thread](../../adr/0033-serialize-support-thread-turns-with-single-use-reply-claims.md)（Claim 键已由 ADR-0060 部分替代）
- [ADR-0035：用 UniSeg Receipt 结算 Thread 出站引用](../../adr/0035-settle-support-thread-replies-from-uniseg-receipts.md)（Thread 结算已由 ADR-0060 部分替代）
- [ADR-0038：限定语义 assessment 的远端数据投影](../../adr/0038-limit-semantic-assessment-remote-data-projection.md)
- [ADR-0048：用公开事实驱动受控 Answer Agent](../../adr/0048-use-public-facts-for-guidance-answer-agent.md)
- [ADR-0050：用有界 Agent 判定普通用户报告的 Bug](../../adr/0050-use-a-bounded-agent-for-user-bug-assessment.md)
- [ADR-0051：允许 Bug Agent 查询受控设计 RAG](../../adr/0051-let-the-bug-assessment-agent-query-design-rag.md)
- [ADR-0052：把 Bug 定义到整个 Bot 软件责任链](../../adr/0052-define-bug-across-the-bot-software-responsibility-chain.md)
- [ADR-0053：允许 Bug Agent 使用相关源码与日志正文](../../adr/0053-allow-relevant-source-and-log-bodies-for-bug-assessment.md)
- [ADR-0054：使用 LocalStore 保存已审核 Bug 问题记录](../../adr/0054-store-reviewed-bug-problems-in-localstore.md)（已被 ORM 工作流替代）
- [ADR-0060：用作用域 Thread 承接一次补充并在路由后投影会话上下文](../../adr/0060-use-scope-thread-and-post-route-conversation-context.md)
- [ADR-0061：为 Bug 判断读取当前会话最新有界聊天窗口](../../adr/0061-read-latest-bounded-conversation-window-for-bug-assessment.md)
- [ADR-0064：收窄 Bug 会话证据与结论合同](../../adr/0064-refine-bug-conversation-evidence-and-verdict-contract.md)
- [ADR-0065：只为明确支持的平台提供 Bug 会话历史工具](../../adr/0065-only-expose-conversation-history-for-supported-platforms.md)
- [ADR-0066：用当前公开教学合同前置筛查普通用户 Bug](../../adr/0066-use-active-teaching-contract-as-bug-precheck.md)
- [ADR-0068：把合格 Agent Bug verdict 作为正式判断](../../adr/0068-treat-qualified-agent-bug-verdicts-as-operational-decisions.md)
- [ADR-0070：分离 Bug Report、Occurrence 与 Problem](../../adr/0070-separate-bug-reports-occurrences-and-problems.md)
- [ADR-0071：用版本化 Evidence 指纹聚合 Bug Problem](../../adr/0071-group-bug-problems-with-versioned-evidence-fingerprints.md)
- [ADR-0073：使用 NoneBot ORM 保存权威 Bug 工作流状态](../../adr/0073-use-nonebot-orm-for-authoritative-bug-workflow-state.md)
- [ADR-0074：用追加式 Problem Decision 保留判断历史](../../adr/0074-preserve-append-only-problem-decisions.md)
- [ADR-0075：把问题维护注册为 triage 子命令](../../adr/0075-register-problem-maintenance-under-triage-subcommand.md)
- [ADR-0078：在可记录性合同确定前不持久化 unknown](../../adr/0078-defer-persisting-unknown-bug-assessments.md)
- [ADR-0079：用无编号的 triage 报错查询列出待处理问题](../../adr/0079-list-pending-problems-with-triage-query.md)
- [Alconna 能力与解析回执](alconna-capability-and-parse-receipts.md)
- [运行观察入口](runtime-observation-intake.md)
