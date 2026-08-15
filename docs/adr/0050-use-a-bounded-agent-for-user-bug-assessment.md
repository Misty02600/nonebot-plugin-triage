# ADR-0050：在确定性协调器内使用有界 Agent 判定普通用户报告的 Bug

| 状态 | 决策日期 |
|---|---|
| 已采纳；首个只读三值判定纵切已实现；责任范围由 ADR-0052 补充，数据投影由 ADR-0053、ADR-0060 部分替代 | 2026-08-14 |

## 当时遇到了什么

当前支持入口已经把 `reported_observation` 与 `incident_intake` 分开，并要求模型外可信运行失败后才允许
建立 incident。但普通用户在决定是否上报前，还缺少一个更窄的问题入口：根据已有报告、当前请求上下文、
公开行为合同、运行记录和当前源码，判断这次现象是插件 Bug、不是插件 Bug，还是证据不足。

这个判断不能只靠固定 Workflow。不同插件的 Matcher、Rule、Permission、handler、外部依赖和失败阶段组合
无法穷举，模型需要根据已经取得的证据动态决定是否继续查看运行摘要、同类发生模式或源码事实。但也不能把
整条链交给模型：历史结论必须优先复用，Reply 与日志读取范围、源码披露、费用、工具次数和最终三值结论都
属于系统政策，不能依赖 Prompt 自律。

并行的 help-spec 工作正在把 Matcher、命令、参数、权限、Rule、handler 条件和 revision 整理成确定性事实，
并以人工确认的帮助规格作为正式教学真值。Bug 判定应复用这层事实，不再建立第二套源码解析和 revision
合同。

## 决策

1. 增加面向普通用户的 `bug_assessment` 产品目标，与 `guidance`、`behavior_exploration` 和
   `incident_intake` 分离。首版只判断 `bug`、`not_bug` 或 `unknown`，不自动上报、不建立 incident，也不
   承担开发者审理、通知或持久工单生命周期。
2. 整体采用 Agentic Workflow：模型外的 `BugAssessmentCoordinator` 负责固定顺序、安全与副作用边界；
   一个有界 `BugAssessmentAgent` 负责在允许的证据中动态取证和提出候选结论；确定性 reconciler 复核后才
   形成最终 verdict。不能把纯 Workflow 或纯 Agent 作为整条能力的唯一控制面。
3. Coordinator 先取得有界上下文：当前规范化请求、精确 Reply 指向的本次操作与 Bot 回执、结构化解析或
   运行回执、adapter / scene、目标 plugin / capability、source revision、人工帮助合同 revision 和安全的
   deployment generation。它不读取任意最近聊天记录，不把其他用户消息、平台身份或无关 Thread 纳入上下文。
   上下文不能唯一确定 subject、operation 或适用 revision 时，直接返回 `unknown`。
4. Coordinator 根据上述上下文构造不含原始文字和身份的 `BugCaseFingerprint`，并首先查询经过审核的历史
   verdict。只有 fingerprint、适用 revision / generation 和行为合同均完全匹配，结论为已验证的 `bug` 或
   `not_bug`，且不存在冲突时，才能直接复用。命中后必须零日志读取、零源码分析、零 Bug Agent 调用。
   普通未审核报告、模型候选、相似文本、陈旧记录和历史 `unknown` 只能作为后续发生频率证据，不能短路。
5. 历史记录未命中后，先执行公开合同初检。缺少公开必填参数、角色或场景不满足、公开限流或其他已经由
   人工确认合同正面解释的现象，可以确定性返回 `not_bug`；“没有找到问题”或缺少日志不能产生 `not_bug`。
6. 仍未解决时才运行 `BugAssessmentAgent`。实现直接使用 Pydantic AI
   `Agent(output_type=BugAssessmentCandidate)`、`ModelProfile` 和框架原生工具编排，不新增重复的结构化输出、
   tool schema 或 transport capability 层。Agent 不获得报告写入、incident、消息发送、配置修改、插件执行或
   任意文件读取工具。
7. Agent 的首版只读工具限于取得当前请求关联的结构化运行摘要、同类发生模式和当前 revision 的受控源码
   事实。它可以根据已知证据决定是否继续调用某项工具或停止，但每类工具最多调用一次，并受预冻结的请求数、
   工具数、超时和费用上限约束。工具只返回闭合 schema 与 Evidence ID，不返回原始消息、异常消息、完整
   traceback、API 参数 / 返回值、绝对路径、原始配置或整段源码。
8. 发生模式与 Bug verdict 正交。内部可以区分 `isolated`、`repeated`、`systematic` 和 `unknown`，但“只发生
   一次”不能自动变成 `not_bug`，重复发生也不能自动变成 `bug`。buffer drop、重启、过期、revision 漂移或
   采集不完整时不得声称某问题只发生过一次。
9. 源码分析只针对当前已确认 subject 和 revision，优先消费 help-spec / capability source evidence 提供的
   Matcher、参数、Rule、Permission、handler 条件、直接调用和结果出口事实；不额外 import 或执行插件、Rule、
   Permission、handler、parser、validator、默认工厂或探针。人工帮助规格描述预期行为，源码事实描述当前
   实现，任一方都不能单独证明本次实际发生了什么。
10. `BugAssessmentCandidate` 必须声明 proposed verdict、occurrence pattern、reason code、已使用 Evidence ID
    和缺失证据。模型不能直接产生最终 verdict。确定性 reconciler 检查 Evidence ID 闭包、revision、freshness、
    partial、drop 和冲突；它可以接受候选或把 `bug` / `not_bug` 降级为 `unknown`，不能在缺少候选证据时把
    `unknown` 升级为更强结论。
11. 普通用户只看到三值结论和一条安全原因，例如“当前行为与公开合同不一致”“本次行为符合公开使用条件”
    或“现有上下文、运行记录或实现证据不足”。回答不得出现其他报告者、内部报告 ID、correlation ID、源码、
    函数或类名、Rule / Permission 名、配置字段、日志原文、异常类型、内部路径或 Evidence ID。
12. 原始 Reply、日志和源码只允许在部署本地的有界读取链中瞬时使用。本 ADR 不授权把原始上下文、源码、
    日志或 restricted 证据发送给远端模型；真实 Provider 只能在用途专属的数据投影、Provider / API / model /
    profile、Prompt / schema revision、隐私、预算和 held-out 资格均获准后接入。未获准或证据投影不足时返回
    `unknown`。
13. 实现与评审时必须逐阶段做 Agent 适用性审查，不能先把整项能力预设成 Workflow 或 Agent。鉴权、数据
    投影、确定性短路、副作用授权、证据闭包和最终降级属于固定政策；如果“下一项证据或工具的选择”确实依赖
    中间观察，而且无法用稳定、有限的规则枚举，则应显式评估有界 Agent。审查结果要用测试可验证的工具集合、
    调用上限、停止条件和失败关闭语义落地，不能只以 Prompt 说明代替边界。

## 为什么这样选

- verified report 的确定性优先短路保证已有结论不会被重复分析，也可以用测试证明命中时没有日志、源码和
  模型费用；
- 上下文、鉴权、数据投影、工具预算和最终证据门属于政策，保留在模型外才能失败关闭；
- 日志与源码之间的因果关系、第三方插件的条件组合和下一项最有价值证据无法靠有限规则长期穷举，适合由
  有界 Agent 动态判断；
- 三值 verdict 和 occurrence pattern 分离，避免把单次失败、重复报告或缺少证据误当成 Bug 结论；
- 复用 help-spec 的确定性事实可以让教学和 Bug 判定共享来源 / revision 语义，同时继续保持人工帮助合同、
  内部源码事实和实际运行观察各自只证明自己的字段。

## 没有采用的方案

### 把全部过程写成固定 Workflow

可以严格控制顺序，但会把不同插件的源码、日志和责任边界穷举成不断扩张的条件分支，最终重新形成词表式
判断，也无法让系统按当前证据选择下一项最有价值的只读工具。

### 把上下文、报告查询和所有工具交给纯 Agent

模型不能硬保证先查 verified report、精确命中后停止、只读取当前 Reply、遵守数据投影或不重复调用工具；
这些约束若只写进 Prompt，无法形成可证明的安全、费用和零副作用合同。

### 只看源码或只看日志

源码不能证明本次路径已经发生；日志不能单独定义预期行为，也可能 partial、dropped 或来自其他 revision。
Bug 判定必须协调人工合同、当前实现与实际运行证据。

### 将 `bug` 结果自动转成 incident

判定和上报是不同用户目标。自动建单会把只读分析升级成持久业务状态与后续通知，本切片明确后置。

## 带来的影响

- 需要新增传输无关的上下文、fingerprint、verified verdict、runtime / occurrence / source summary、Agent 候选
  和最终 decision 合同，以及插件侧有界证据适配；
- 需要为 `bug_assessment` 扩展语义 taxonomy，并用全新 held-out 重新资格化精确模型组合；已有 semantic、
  guidance 或 behavior 资格不能继承；
- 需要一个只读 verified verdict repository 端口；首版可以没有可用记录，但不能把未审核 LiveIncident 或
  trial 自动提升为可复用结论；
- help-spec 的确定性事实接口稳定前，不修改其占用模块或重复开发源码提取器；
- 需要用 spy / fake 工具冻结执行顺序：verified 命中时零后续读取，公开初检命中时零 Agent，partial / stale /
  drop 时不产生 absence claim，任意路径都零 incident 与零报告写入；
- 实现前要形成逐阶段的 Agent 适用性审查记录，至少说明该阶段是在执行可枚举政策，还是在根据中间观察选择
  下一项证据，并分别解释为什么使用确定性步骤或有界 Agent；
- 本决定没有创建仓库 plan；后续落实以本节记录的代码、测试和资格证据为准。

## 落实与确认

- **已确认**：项目作者接受以有界 Agent 处理开放式日志 / 源码取证，并由确定性 Workflow 承担上下文、历史
  短路、安全、预算、reconciliation 和普通用户投影；首版只返回 `bug`、`not_bug` 或 `unknown`。
- **已实现首个纵切**：语义 schema v6 已增加 `bug_assessment` 目标；首轮与续问都能进入独立 Bug
  assessment route。领域层已经实现 fingerprint、reviewed catalog、Evidence、Agent candidate、确定性
  reconciliation 和固定普通用户回复；插件层已经接入有界 Reply correlation、运行观察、关联 traceback、
  当前已加载 subject 的受控源码、版本化设计知识包和部署摘要。最终只返回 `bug`、`not_bug` 或 `unknown`，
  不建立 incident，也不自动上报。
- **Agent 与资格**：实现直接使用 Pydantic AI `Agent(output_type=BugAssessmentCandidate)`、原生 `Tool`、
  `ModelProfile` 与 `UsageLimits`。OpenCode Go / Chat Completions / `deepseek-v4-flash` / Prompt
  `bug-assessment-agent-v1-prompt-v4` 的全新 16 条 held-out 通过：verdict、责任候选、citation closure 与预算
  合规均为 1.000，occurrence 为 0.9375；共 179,138 input / 6,682 output token，6,508 microUSD。
- **当前限制**：公开合同预检已经在 Agent 前固定加载，但首个运行适配器尚未把具体公开参数 / 权限错误实现成
  无模型的 `not_bug` 快判；任意已实现 `PublicBugPrechecker` 仍可确定性短路。运行日志目前只覆盖与
  correlation 精确关联、由 NoneBot runtime hook 捕获的 Matcher / API 异常与完整 traceback，不读取任意宿主
  文件日志。reviewed catalog 已有离线原子发布 API，但尚无维护者交互式审核界面。

## 关系

- 补充 [ADR-0040](0040-require-trusted-preflight-failure-before-incident.md)：Bug 判定发生在上报之前，不改变
  可信失败才可建 incident 的规则；
- 复用 [ADR-0044](0044-use-pydantic-ai-agent-output-type-for-support-semantics.md) 的 Pydantic AI 原生结构化输出
  方向，但本 Agent 可以使用受控只读业务工具；
- 继续区分 [ADR-0046](0046-merge-internal-reasoning-into-behavior-exploration.md) 的 SUPERUSER 行为探索：普通
  用户 Bug 判定只返回安全三值结论，不提供源码解释；
- 不改变 [ADR-0025](0025-explain-plugin-behavior-from-deployment-evidence.md) 的内部证据披露与只读边界；
- 后续 help-spec ADR-0049 可以定义人工帮助规格和确定性源码事实的所有权，本决定只消费其稳定接口，不预先
  规定其文件格式或实现；
- 设计 RAG 查询由 [ADR-0051](0051-let-the-bug-assessment-agent-query-design-rag.md) 补充，文档证据只证明
  匹配范围内的预期行为，不能单独证明当前实现或本次运行。
- Bug 的责任范围由 [ADR-0052](0052-define-bug-across-the-bot-software-responsibility-chain.md) 补充为整个
  Bot 软件责任链，不再限于用户最先提到的插件；
- 源码、日志和设计正文的远端投影边界由
  [ADR-0053](0053-allow-relevant-source-and-log-bodies-for-bug-assessment.md) 部分替代：允许相关正文进入独立
  合格的 Bug task，同时保留秘密清理、范围 / 预算、reconciliation 和普通用户安全输出。
- verified verdict repository 使用 LocalStore data 保存的单写者、在线只读 catalog，详见
  [ADR-0054](0054-store-reviewed-bug-problems-in-localstore.md)。
- [ADR-0060](0060-use-scope-thread-and-post-route-conversation-context.md) 部分替代第 3、7、12 项：允许直接
  Reply 与模型外锚定的聊天正文进入独立合格的 Bug task；历史短路、公开合同初检、总预算、确定性
  reconciliation、三值结论与零自动上报继续有效。

## 相关文档

- [支持入口分流](../architecture/flows/support-intake-routing.md)
- [可选帮助数据源与复用边界](../architecture/help-source-adapters.md)
- [观察型生产 trial](../architecture/flows/observation-first-trials.md)
