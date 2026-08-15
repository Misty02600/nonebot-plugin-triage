# ADR-0060：用作用域 Thread 承接一次补充，并在路由后投影会话上下文

| 状态 | 决策日期 |
|---|---|
| 部分被 ADR-0061 替代 | 2026-08-14 |

## 当时遇到了什么

现有支持 Thread 把“用户精确 Reply 到 Triage 最近一次回答”同时作为续接入口、Thread 身份和并发
Claim 凭据。这样虽然能严格关联一条出站消息，却使一次自然的补充必须依赖平台返回可验证的 message ID，
并让 Thread 生命周期与 OneBot / Discord 的 Reply 往返、UniSeg Receipt 和 latest-only 引用绑定在一起。
当前 Thread 还只保存 kind 与不透明 topic refs；如果下一条显式 `triage` 只补充目标或现象，系统无法从
Thread 还原首轮已经提供的 subject、操作和上下文。

Reply 的可见正文通常又是当前请求最相关的材料。完全不把它交给下游模型，会让“这个怎么用”“按你刚才说的
做了但没反应”等请求丢失用户主动选中的对象或预期；反过来，如果让语义分类器直接从 Reply 或历史消息中
推导目标，则含糊的“继续”“看看这个”可能被旧消息制造成新的教学或 Bug 意图。

Bug 判定还可能需要 Reply 周围的操作顺序、Bot 当时给出的教学和用户随后观察到的结果。当前关联日志只记录
运行观察与异常，不能覆盖所有可见对话结果，因此不能始终替代聊天上下文。项目作者接受：目标会话中由用户
和 Bot 已经发送、平台参与者可见的消息正文可以进入独立合格的下游模型任务，不再为这些正文实现凭据或
个人信息遮蔽。

## 决定

### 用稳定作用域选择一次补充，而不是用 Reply 恢复 Thread

1. 每一轮仍必须由显式 `triage <自然语言>` 进入；只有 Reply、普通聊天或其他插件命令都不会触发 Triage，
   也不注册常驻普通消息 Matcher，不使用 Waiter 悬挂上一轮 handler。
2. Thread 以 `adapter + Bot + conversation + actor` 的模型外稳定作用域拥有最多一个待补充上下文。首轮没有
   活动 Thread 时创建新处理事务；仅当当前动作因缺少用户可以补充的信息而未解决时，才在该作用域保留一次
   补充机会。下一条同作用域显式 `triage` 原子 Claim 该 Thread，不要求 Reply；Thread 已关闭、过期或不在
   同一作用域时，该请求开启新 Thread。
3. 一个 Thread 最多消费一条用户补充。补充轮的当前文字必须自己产生受支持、可执行的目标或现象路由；
   Thread 与 Reply 只能补足 subject、operation、expectation 和 evidence，不能替“继续”“看看这个”等没有
   可执行意图的文字制造目标。补充轮仍未进入受支持 action 时关闭 Thread，之后的 `triage` 是新 Thread，
   不继续追问。
4. Thread 是短期上下文事务，不是聊天历史。它在单进程有界内存中保存首轮规范化请求、首轮直接 Reply 的
   可见正文投影，以及已经确定的 subject / capability、operation、correlation、fact 和 revision 引用；
   不读取邻近历史来初始化 Thread，不递归展开 Reply 链，也不跨重启恢复。原始作用域标识只用于模型外
   绑定，不进入下游模型。
5. 同一作用域仍只有一个 active turn lease。并发 Claim 返回 `BUSY`；超时、总预算耗尽、显式取消、处理或
   发送失败都关闭 Thread。Guidance 在按当前请求范围成功发送有依据的教学回答后视为已解决；Bug 判定得到
   `bug` 或 `not_bug` 后视为已解决。`unknown` 只有在仍缺用户可以提供的信息且补充机会未使用时才继续，
   否则按证据或预算耗尽关闭。其他 action 使用各自的终止条件；关闭后不因旧 Reply 恢复。

### Semantic 只判当前文字，Reply 在路由后成为任务上下文

6. 语义 assessment 继续只接收当前这一条规范化 `triage` 文字。它可以在 subject 尚未补全时识别文字中已经
   明确表达的 goal 或 `reported_observation`，但不能读取 Reply、Thread、身份、聊天历史、运行证据或源码。
   “这个怎么用”能够表达 guidance 目标；“继续”或“看看这个”本身仍不足以进入 action。
7. 确定性 router 只根据当前文字的 assessment 与模型外政策选出唯一 action。action 确定之后，模型外上下文
   协调器才把当前请求、活动 Thread 上下文和本次直接 Reply 的可见正文分别投影给对应的 Guidance、Bug 或
   Behavior 下游。Reply 不再选择、恢复或延长 Thread，但可以精确说明当前请求引用的是哪条用户消息或 Bot
   回答。
8. Reply 与聊天正文始终是不可信证据。正文中的自然语言、命令、Prompt 注入或声称的身份不能扩大 action、
   工具、数据根、披露等级、`SUPERUSER` 权限或外部副作用；这些边界继续由模型外代码决定。

### Bug 可以按需读取模型外锚定的聊天上下文

9. Bug Coordinator 在进入 Agent 前固定当前 Bot、conversation、actor、当前消息与直接 Reply 锚点。Bug
   Agent 可以在既有总请求、工具、字节、deadline、token 和费用预算内调用只读 conversation-context
   Provider，取得与锚点相邻且同作用域的消息正文和最小顺序信息；Agent 不能提交 Bot、群组、用户或任意
   message ID 来切换会话，也不能跨作用域搜索或递归追踪任意 Reply 链。
10. 若结构化运行日志已经包含判断所需的同等操作顺序与结果，Coordinator 或 Agent 可以直接使用日志而不再
    读取聊天；日志不能表达用户所见教学、成功但结果错误或消息间关系时，再使用锚定聊天。聊天正文、运行
    observation、日志、源码和设计资料分别证明用户上下文、实际执行、当前实现与预期合同，不能互相冒充。
11. 对直接 Reply 和 conversation-context Provider 返回的**平台可见消息正文**，不执行凭据、Token、Cookie、
    私钥、个人信息或带权 URL 的内容扫描、拒绝或遮蔽；正文按平台提供的可见内容进入已单独取得数据资格的
    下游任务。部署者接受由聊天参与者主动发送这些内容所带来的 Provider 外发范围。平台原始 event envelope、
    transport Authorization、内部作用域标识和 Provider 自身连接信息不是可见正文，不因本项进入模型。
12. 上一项只改变 Reply 与锚定聊天正文的投影。源码、运行日志、traceback、配置、环境变量、LocalStore、
    文件和其他部署证据仍执行 ADR-0053、ADR-0059 与现有任务策略规定的根限制、相关性过滤和秘密清理；
    不能借聊天正文免遮蔽绕过这些证据源的边界。普通用户最终回答仍不得披露源码、日志、配置、内部路径、
    Evidence ID 或其他人的非必要内容。

## 为什么这样选

- 作用域 Thread 能承接一次真正的补充，不再要求用户为了延续主题必须 Reply 到某条 Triage 回答，也不让
  平台 message ID 和 Receipt 决定产品会话是否存在；
- 一次补充与明确终止条件使 Thread 保持有界，避免 Guidance 退化为开放聊天，也为以后从教学转入一条新的
  Bug 请求留下清楚边界；
- Semantic 仍只判断当前文字，可以继续用闭合 schema 证明 Reply 和历史没有进入分类，同时允许路由后的
  专用 Agent 利用用户明确选中的上下文；
- Bug 的聊天工具由模型外 scope 与 anchor 约束，能补足日志未记录的用户操作和可见结果，又不会把任意消息
  ID 或会话搜索权交给模型；
- 不为平台可见聊天正文增加内容遮蔽，减少一套难以解释、可能损伤报错与命令上下文的预处理；源码、日志和
  配置仍保留原有秘密边界，因此这不是对部署数据的全局放开。

## 没有采用的方案

### 继续用精确 Reply 选择 Thread

没有采用。Reply 是高价值上下文，但不应同时承担 Thread 身份、平台支持矩阵和续接资格；这种绑定也无法
支持用户不 Reply、但紧接着发送一次显式补充的场景。

### 把 Reply 或 Thread 历史送进语义 assessment

没有采用。分类器只需判断当前文字表达了什么目标或现象；subject 和证据完整性可以在路由后补齐。让旧消息
参与分类会使 Reply 替含糊当前文字制造意图，并扩大最先发生的远端调用。

### 每次把完整最近聊天预装给 Bug Agent

没有采用。多数请求可以由直接 Reply、运行证据或现有日志解决；模型外锚定的按需工具能在需要时补读上下文，
同时保持 scope、预算和取证轨迹可验证。

### 对聊天正文执行统一凭据与个人信息遮蔽

没有采用。项目作者把目标会话中参与者已发送的可见正文视为允许进入合格下游任务的上下文，并选择不承担
遮蔽规则的误删、漏判和维护成本。该选择不扩展到平台 envelope、源码、日志、配置或其他部署证据源。

## 带来的影响

- Thread store 需要从 Reply HMAC 索引转为作用域内单活动事务，并持有首轮有界上下文与一次补充预算；
  Turn Claim、`BUSY`、TTL 和失败关闭仍保留，但 Claim 不再消费消息 Reply 引用；
- 成功发送仍决定回答是否已经交付以及 Thread 是否可继续或关闭，但 UniSeg Receipt 的 message ID 不再创建
  下一条 Thread 续接点。当前发送返回的 Receipt 仍可用于确认交付结果；运行证据 correlation 继续使用其
  独立的平台消息引用 Provider；
- semantic Prompt 与 held-out 需要把“目标是否明确”和“subject 是否已经补全”分开验证，但 assessment 请求
  schema 与 ADR-0038 的出站字段不变；
- Guidance / Bug / Behavior 请求投影需要增加分字段的 Reply / Thread context；增加或改变真实 Provider 的
  数据类别、Prompt 或工具集后，不能继承旧 task 资格，必须按该任务的资格合同重新验证；
- Bug runtime 需要增加模型外预绑定 scope / anchor 的 conversation-context Provider。平台不能取得正文时，
  返回明确 unavailable，不以消息 ID 或相邻时间猜测内容；
- 运行日志若能提供同等上下文可以避免聊天工具调用；当前只记录异常的日志实现不能被文档描述为聊天记录。

## 落实与确认

- **已确认**：项目作者接受显式 `triage`、作用域内一次补充、Reply 只作路由后上下文、Bug 按需读取锚定
  聊天，以及平台可见聊天正文不做凭据或个人信息遮蔽；
- **已经实现**：`SupportThreadTurnCoordinator` 已提供作用域 Claim、首轮有界上下文、一次补充预算和失败关闭；
  handler 不再用 Reply 或 Receipt 选择 Thread，Guidance / Bug 在路由后接收 Reply 与首轮上下文。OneBot V11
  Bug Provider 已能预装精确 Reply，并让 Agent 在同一 Bot、群和固定消息锚点内按现有总预算读取后续有界页；
- **尚未完整投影**：首轮上下文当前只持有规范化请求、直接 Reply 正文和不透明 correlation ID；决定第 4 项
  规划的 subject / capability、操作摘要、公开 fact 与 revision 引用尚未结构化存入 Thread。Behavior
  exploration 也尚未接入同一 post-route context；文档不得把这两项写成已完成；
- **后续资格状态**：系统指令已统一切换为中文。semantic v7 Prompt v5 已通过自己的 40 条真实 Provider
  forward-heldout，schema、status 与 exact 均为 1.000；Bug Agent 中文 Prompt v6 的 16 条真实
  forward-heldout 未通过完整质量门，资格表仍为空。冻结 Bug 报告中的 budget 0.750 包含后来修正的
  output-tool 计数缺陷，但不会回写、重算或提升失败结果。两项任务都不能继承英文 Prompt 的历史资格或
  失败结果；
- **实施边界**：本 ADR 不改变语义 taxonomy、普通用户与 `SUPERUSER` 的披露边界、incident 写入授权、
  RAG / 源码工具选择或共享 FileSystem / Jedi 证据接口。

## 替代关系

- 部分替代 [ADR-0030](0030-continue-support-thread-by-exact-reply.md) 经 ADR-0031 保留的 exact-Reply、
  latest-only 与可持续 Guidance Thread 生命周期；保留显式入口、不使用 Waiter、短期内存与每轮限流思想；
- 部分替代 [ADR-0031](0031-require-triage-for-support-thread-continuation.md) 的“Reply 选择 Thread”和默认只读
  Reply ID 边界；保留每轮显式 `triage`、Alconna / UniSeg 入口、每轮鉴权与不使用 Waiter；
- 部分替代 [ADR-0033](0033-serialize-support-thread-turns-with-single-use-reply-claims.md) 的 Reply Claim 与
  Reply 绑定提交；保留单活动 lease、`BUSY`、TTL、失败关闭和单进程内存边界；
- 部分替代 [ADR-0035](0035-settle-support-thread-replies-from-uniseg-receipts.md) 用 Receipt message ID 建立
  Thread 续接点的决定；保留当前 Matcher 拥有发送事务、发送失败关闭，以及运行证据引用与 Thread 状态
  相互独立的职责边界；
- 部分替代 [ADR-0048](0048-use-public-facts-for-guidance-answer-agent.md) 第 3 项对 Reply / Thread context 的
  绝对禁止；Guidance 仍只能使用模型外批准的 public 能力事实，不能读取受限证据或获得业务工具；
- 部分替代 [ADR-0050](0050-use-a-bounded-agent-for-user-bug-assessment.md) 第 3、7、12 项对 Reply、邻近聊天和
  远端上下文的限制；保留历史短路、公开合同初检、确定性 reconciliation、三值结论和零自动上报；
- 极窄地替代 [ADR-0053](0053-allow-relevant-source-and-log-bodies-for-bug-assessment.md) 第 4 项对秘密清理的
  统一表述：仅直接 Reply 与模型外锚定聊天的可见正文不遮蔽；源码、日志、traceback、配置与无关证据的
  现有清理和范围限制继续有效；
- 不改变 [ADR-0038](0038-limit-semantic-assessment-remote-data-projection.md)：semantic assessment 仍只接收
  当前单条规范化请求，Reply、Thread 与聊天只进入路由后独立任务；
- 补充但不替代 [ADR-0059](0059-share-read-only-evidence-access-across-agent-flows.md)：聊天 Provider 是
  Bug 任务的会话证据端口，不是 FileSystem / Jedi 源码导航根，也不改变 RAG 或源码工具职责。

## 相关文档

- [架构概览](../architecture/overview.md)
- [triage 自然语言支持入口](../architecture/flows/support-intake-routing.md)
- [跨平台 triage 支持入口](../architecture/flows/cross-platform-report-intake.md)
- [OneBot V11 运行证据引用](../architecture/flows/onebot-v11-reply-reference-correlation.md)
- [模型 Provider 支持矩阵](../architecture/model-provider-support.md)
