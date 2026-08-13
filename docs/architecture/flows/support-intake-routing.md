# 流程：triage 自然语言支持入口

## 当前入口与已采纳目标

普通用户每轮都发送 `triage <求助内容>`，也可以写成 `@Bot triage <求助内容>`。Reply 是可选的结构化
上下文，不是独立触发器：只有 Reply、没有 `triage` 的消息不会进入本插件。显式请求精确回复 Triage 最近
一次仍有效的回答时，插件尝试续接同一 Thread；未知、过期、旧回答或跨作用域 Reply 则按新请求处理。
当前实现已经允许私聊、群聊和频道请求进入同一入口；需要权限的后续分支都针对当前 Bot / Event 的请求者
执行同一鉴权，不因会话类型改变规则。

```text
triage + request text + optional Reply
                 ↓
场景、长度、入口限流和最小上下文边界
                 ↓
语义 assessment（模型只产出 goals / observation / maintenance depth）
                 ↓
公开用法 / 参数 / 权限 / 场景 / 可信运行回执初检（模型外）
                 ↓
确定性 router（唯一 action / authorization 决策者）
   ├─ 未配置 / 请求期失败 → abstain → CLARIFY
   ├─ GUIDANCE → SHOW_GUIDANCE → public 事实
   ├─ BEHAVIOR_EXPLANATION 或 MAINTENANCE_DETAIL → BEHAVIOR_EXPLORATION_CANDIDATE
   │                                  └─ 当前明确回复未接通；不读取受限证据
   ├─ FEATURE_FEEDBACK → FEATURE_FEEDBACK_CANDIDATE
   │                                  └─ 当前明确回复未接通；不建 incident / 外部工单
   ├─ INCIDENT_INTAKE + REPORTED_OBSERVATION + 可信初检仍为运行失败
   │                   → OPEN_INCIDENT + 绑定精确 LiveReportRequest 的一次性 authorization
   │                   → LiveReportService 校验后才可建单
   └─ policy blocked → REFUSE；不调用证据或工具
```

每轮正常回答后 handler 都结束，不使用 Waiter 悬挂协程。教学或澄清回答会建立不含正文的短期内存 Thread，
首次回答发送期间用短期 pending reservation 防止该 Thread 被 idle TTL 或容量淘汰，但不延长 absolute TTL；
当前 Alconna Matcher 从单条、经校验的 UniSeg Receipt 取得回答 message ID，再通过 HMAC 索引绑定到
adapter、Bot、场景和 actor。下一条显式
`triage` 由同一个 Alconna Matcher 接收；每轮先经过入口限流，随后由 UniSeg 提供结构化 Reply / Target，
Thread 协调器再原子 Claim 仍可续接的上下文。Claim 会立即消费旧 Reply，并保证每个 Thread 同时只有一个
active turn；并发续问返回“上一轮仍在处理”，不进入业务分支。默认 idle 15 分钟、
absolute 30 分钟且不跨重启。当前合同测试覆盖 OneBot V11 群聊 / 私聊与 Discord 频道 / 私聊；其他
adapter 未验证或 Receipt 失败时按新请求处理，而不是监听其普通 Reply。

Claim 内部异常与 `NOT_FOUND` 分开：异常只返回“上下文暂时不可用”并失败关闭，不把已部分处理的续问再次
当作新请求。入口限流拒绝发生在 Claim 前，因此不会消费 Reply。

首轮空输入不调用 assessment，而是建立 clarification Thread 并提示补充问题；首轮超长在建 Thread 前拒绝；
首轮非空 assessment abstain 则返回保守澄清并建立一个 clarification Thread。续问已经 Claim 旧 Reply 后，
clarification Thread 遇到空或超长输入会立即关闭；guidance Thread 遇到同样输入会返回提示，并只在发送成功
后登记新的续接点。任一 Thread 的非空续问若 assessment abstain / unresolved，都会关闭当前 Thread。

澄清 Thread 只接受一次明确关联的续答；续答可转入教学。首轮与续问现在每轮都调用一次相同 assessment
service 和 router；若未来合格 transport 返回 `reported_observation`，且当前 Reply 解析出的模型外运行回执
明确失败，router 才会为该轮构造的精确 `LiveReportRequest` 签发授权，报障服务原子校验并一次性消费后才
建立 incident，随后关闭 Thread。授权不能
换请求或重放。未配置 transport 时 runtime 使用 unavailable service；配置 ADR-0041 的精确 OpenCode Go
transport 后，`SHOW_GUIDANCE` 与满足模型外可信失败条件的 `OPEN_INCIDENT` 已可达。behavior candidate 已在
分类后执行模型外 SUPERUSER 鉴权，但内部证据取证与解释仍缺少下游编排。澄清 Thread 的续答为空、超长、refuse 或 assessment 再次 unresolved（包括 abstain）时
立即关闭且不再追问；guidance Thread 遇到空或超长续答时，会返回提示并在发送成功后登记新的续接点。
只有 router 明确选择 `SHOW_GUIDANCE`，guidance Thread 才会继续能力检索；旧回答在处理轮 Claim 时立即失效，
只有新回答成功发送并取得唯一、同作用域且平台结构合法的 Receipt 后才建立新的续接点。发送、取消、
多结果、绑定或处理失败不会复活旧引用，也不会重发已经可能到达平台的回答。续问转报障
时不会把 Bot 的教学回答当作用户故障证据。

当前已接入 `triage` 自由文本参数、短期 Thread 和公开能力说明组件，但已经删除所有自然语言词表快判。
确定性入口只负责 framing、长度、限流、Thread Claim、规范化和空输入；所有非空文本都交给非可选
assessment service 和纯 router。公开能力说明与报障服务已有本地实现，并可由合格 OpenCode Go assessment
signals 进入；behavior candidate 已有 router action 和模型外鉴权，后续取证与解释尚未实现。
其中 `behavior_exploration` 继续消费 `triage` 后的自然语言，不要求额外子命令；它回答插件为什么在当前部署
中这样表现，并在模型外鉴权后才读取受限行为证据。当前 router 把需要源码、内部配置、环境、版本、调用流
或运行证据的内部原因标为 behavior candidate；Matcher 已鉴权但暂不读取 restricted 证据。语义 assessment 已是每轮 `triage` 的正式
运行路径，不设产品级启用开关。未配置 transport 时 runtime 装配 unavailable service；当前已实现并准入
OpenCode Go / Chat Completions / `deepseek-v4-flash` / non-thinking / 60 秒 / 240 token / Prompt v5 的
`support-semantic-v5` 精确组合。这不是可选的无模型产品模式，也不回退到正则。固定话术、
不明确、否定或假设性请求不会被强行记成故障；源码中不再保留可由文本适配层直接产出的
`REPORT_PROBLEM` 枚举。故障入口只能来自 assessment 之后的确定性 routing decision 与其进程内授权。

传输无关的 v5 语义合同、一次性异步 service、以 Pydantic model 作为 `Agent.output_type` 的结构化 client 和
确定性 router 已经实现；
Matcher 的首轮与续问均已消费相同 service。`SupportAssessmentRequest` 只接收版本号和
当前单条规范化请求文字；`SupportSemanticAssessment.goals` 可以同时包含 `guidance`（使用指导）、
`behavior_exploration`（行为探索）、`incident_intake`（故障受理）和 `feature_feedback`（功能建议）。
`reported_observation` 独立表示用户是否陈述了实际发生的 Bot 行为。行为探索本身覆盖源码、配置、环境、
版本、调用流和运行证据等内部请求。合同不包含 reason、confidence、lifecycle、action 或
authorization 字段；`ASSESSED` 至少包含一个目标或独立信号，未决状态不得夹带信号。

这些信号不是现有 `IntakeSignals v1` 或第六种 `IntakeDisposition`。现有确定性 router 不读取原文：安全
策略优先，未决 assessment 进入澄清，单独现象报告仍澄清；只有安全通过、
模型输出 `incident_intake + reported_observation` 且模型外可信运行回执明确失败时，router 才决定
`OPEN_INCIDENT`，并签发
不可复制、不可序列化且绑定精确 decision 与 `LiveReportRequest` 的进程内
`IncidentAuthorization`。`LiveReportService` 已在读取证据或写状态前原子校验并一次性消费
这个授权；重放或换请求失败关闭，模型字段不能直接传给建单服务。

请求需求、生命周期处置和证据披露是三条正交轴。“刚才没响应”只表示用户报告了一个尚未验证的现象，
不证明正确调用仍失败，也不自动建立 incident；“哪个配置控制”只表示请求了维护细节，不自动授权读取配置。
普通用户先在 public 证据域内获得用法、角色、场景、限流和非秘密启用前提的解释。配置字段、原始或当前值、
内部后端和源码位置不进入普通回答；若某项设置由已经公开的群内管理指令承接，回答只引用该公开指令合同、
允许披露的角色要求和用户可观察结果，而不是底层配置。hidden、SUPERUSER-only 或 restricted 管理入口仍与
不存在保持不可区分。`SUPERUSER` 身份只扩大 behavior 分支的可读证据域，不会把用户疑惑自动改成故障。

## 输入与数据边界

- 当前单条规范化 `triage` 请求文字已获准在模型前秘密守门通过后，投影给通过
  `Provider + API 族 + 精确 model + semantic task/schema/Prompt + 隐私策略 + 预算 + 评测 revision` 资格门的远端
  Provider；OpenCode Go 精确组合已经通过 held-out 资格门；`IntakeSignals`、`LiveIncident`、trial 和运行证据仍不保存原文；
- v5 请求 allowlist 合同已经用闭合 schema 固定为 `schema_version + request_text`，领域硬上限为 8000 字符；
  实际入口仍先执行固定的 2000 字产品上限。现有 Agent client 只把该对象序列化为紧凑 JSON，固定 Prompt
  不含当前请求文字；OpenCode Go 的 Pydantic AI Profile 让 Agent 生成一个 required、不可执行的 output tool，
  项目不再手写 output schema、tool 名称或响应 part 解析；client 关闭 instrument，并使用
  有界 timeout / 输出 token，并在响应后执行领域二次校验。未配置 transport 时 runtime 装配 unavailable
  service；
- assessment 请求不得包含 Reply / origin 正文、以往请求或 Thread 历史、用户 / Bot / 会话身份与 scope、
  配置、环境变量、日志、运行证据、源码、能力索引内容、`restricted` 证据或关联标识。固定 Prompt 版本、
  task 标识和输出 Schema 只能承载协议控制信息，不能夹带这些上下文；
- OpenCode Go 的 Provider/API/model/task、资格试验与数美元预算已由 ADR-0041 单独确认；结构化输出能力与
  默认方式由 Pydantic AI ModelProfile 决定，详见 ADR-0042；其他组合仍不得
  从数据类别授权中推导资格。`SUPERUSER` 的本地鉴权也不扩大远端投影；
- 文字、插件元数据和 Reply 都是不可信证据，不能直接变成命令执行、工具调用或维护动作；
- Alconna / UniSeg 负责提供第一个结构化 `Reply.id` 和 Target；入口不读取 `msg` 或 origin 正文。只有
  Discord 等必须区分直接 Reply 与转发引用的平台，才瞬时读取结构化 origin 的引用类型和消息 ID，并要求
  origin ID 与 `Reply.id` 等值；Thread 索引另外
  判断该 ID 是否属于 Triage、是否绑定当前 actor / Bot / 场景，以及 Thread 是否有效和指向最近回答；
- Reply 缺失或引用过期不妨碍求助，但无法产生可信运行失败，因此现象报告只会澄清，不猜测其他消息；
- 所有求助只在统一入口经过一次不保存平台身份的轻量 HMAC 限流；首轮、续问、澄清、指导与报障共用同一
  `adapter + Bot + conversation + actor` 窗口，建单服务不再执行第二层限流；
- 同一 Thread 的并发正确性由一次性 Reply Claim 和短期 turn lease 保证；它不替代入口限流，也不提供
  跨进程或跨重启协调；
- 普通用户能力说明优先采用显式公开 Provider，未命中时只检索当前 adapter 域内自动确定 `public` 的已加载
  命令；`CommandMeta.hide=True`、停用、带 blocking `analysis_issues`、`restricted` 和其他 adapter 能力不进入
  guidance。维护者 CLI 仍可显式检索完整影子；聊天中的内部证据只能在 behavior exploration 分类和模型外
  鉴权后进入未来取证编排，并保留问题与执行资格未知提示；
  影子字段在回显前中和 mention 与 Unicode 控制字符；两条路径都不会为回答问题重新执行命令 `parse()`、
  behavior、executor、Rule、Permission 或 handler；
- 当前实现允许私聊、群聊和频道进入相同本地守门和意图分流，调用者鉴权规则不随会话类型变化。公开教学、
  用法纠错与澄清可以原路回复；当前
  behavior candidate 会先执行模型外鉴权，但仍不读取 restricted 证据；
- 行为探索读取 restricted 证据前必须先通过当前 Bot / Event 的模型外 `SUPERUSER` 鉴权；该检查已接入首轮与续问。
  鉴权通过后，可以在请求者选择的原始私聊、群聊或频道返回完整解释，不按会话成员构成增加 allowlist、
  旁观者鉴权或强制转私聊。回答仍受秘密过滤、文本净化与证据分级约束，restricted 证据进入远端模型仍需
  独立部署授权；
- 允许私聊进入 `triage` 不自动开放私聊疑似故障受理；当前报障服务继续拒绝私聊。普通用户不能读取
  incident 摘要，查询、反馈和统计仍由 `SUPERUSER` 权限保护。

## 建单服务内的次级领域分流

`route_intake` 仍存在，但它不是自然语言意图分类器，也不读取 `triage` 原文。只有 semantic router 已根据
`incident_intake + reported_observation + 模型外可信运行失败` 签发一次性建单授权后，
`LiveReportService` 才会再次解析同一
Reply 并复核失败；复核通过后才生成编号，把结构化运行状态转换成旧 `IntakeSignals v1`，
再交给该次级 router 组合 incident。
`runtime_status=succeeded` 不能证明用户观察到的行为正确；命令少参数或权限不足也不能直接升级成插件故障。
任何固定文字、模型 action 字段或未验证现象都不能直接构造这条次级输入。

只有已授权的 incident 路径后续进入技术责任层。能力说明、用法纠错和行为探索不会污染 incident、trial 或
失败聚类。

## 相关决定

- [ADR-0003：统一能力导航与故障入口](../../adr/0003-unified-capability-guidance-and-incident-intake.md)
- [ADR-0020：triage 自然语言入口与可选 Reply](../../adr/0020-use-triage-command-for-natural-language-support.md)
- [ADR-0022：SUPERUSER 能力影子候选检索](../../adr/0022-limit-capability-shadow-guidance-to-superusers.md)
- [ADR-0025：用多源部署证据解释插件行为](../../adr/0025-explain-plugin-behavior-from-deployment-evidence.md)
- [ADR-0028：允许 triage 私聊并向 SUPERUSER 原会话返回行为解释](../../adr/0028-allow-private-triage-and-superuser-request-context-replies.md)
- [ADR-0030：免命令精确回复续问（已替代）](../../adr/0030-continue-support-thread-by-exact-reply.md)
- [ADR-0031：支持 Thread 续问仍要求显式 triage](../../adr/0031-require-triage-for-support-thread-continuation.md)
- [ADR-0033：用一次性 Reply Claim 串行化支持 Thread 处理轮](../../adr/0033-serialize-support-thread-turns-with-single-use-reply-claims.md)
- [ADR-0035：用经校验的 UniSeg Receipt 结算 Thread 出站引用](../../adr/0035-settle-support-thread-replies-from-uniseg-receipts.md)
- [ADR-0032：分离能力受众、平台范围与分析问题](../../adr/0032-separate-capability-audience-analysis-and-platform-status.md)
- [ADR-0037：把语义 assessment 作为 triage 的正式默认路径](../../adr/0037-make-semantic-assessment-the-default-triage-path.md)
- [ADR-0038：限定语义 assessment 的远端数据投影](../../adr/0038-limit-semantic-assessment-remote-data-projection.md)
- [ADR-0040：只有可信初检仍失败才进入 incident](../../adr/0040-require-trusted-preflight-failure-before-incident.md)
- [ADR-0043：分离支持目标、现象陈述与维护证据深度](../../adr/0043-separate-support-goals-observations-and-maintenance-depth.md)
- [ADR-0044：语义 assessment 直接使用 Pydantic AI Agent output_type](../../adr/0044-use-pydantic-ai-agent-output-type-for-support-semantics.md)
- [ADR-0046：统一行为探索目标](../../adr/0046-merge-internal-reasoning-into-behavior-exploration.md)
- [Alconna 能力与解析回执](alconna-capability-and-parse-receipts.md)
- [运行观察入口](runtime-observation-intake.md)
