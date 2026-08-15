# ADR-0053：允许 Bug Agent 使用相关源码与日志正文

| 状态 | 决策日期 |
|---|---|
| 已采纳；聊天正文清理边界由 ADR-0060 极窄替代 | 2026-08-14 |

## 当时遇到了什么

[ADR-0050](0050-use-a-bounded-agent-for-user-bug-assessment.md) 与
[ADR-0051](0051-let-the-bug-assessment-agent-query-design-rag.md) 最初保守地禁止把源码正文、日志正文和
大部分内部摘录发送给远端 Bug Agent，只允许结构化摘要。这个边界虽然降低了外发风险，却会让模型看不到
分支条件、异常上下文、调用关系和完整 traceback，无法承担已经选定的开放式源码 / 日志根因分析。

主流运维 AI 也没有采用“一律禁止正文”的通用原则。Sentry Seer 官方说明会联合错误消息、stack trace、日志、
trace 和代码库分析问题；Datadog Bits Investigation 官方说明会查询 logs、traces、source code 和运行知识。
这些实践支持的边界是受控取数与数据治理，不是先把所有诊断证据压成无正文摘要。

## 决策

1. 为 `bug_assessment` 建立独立数据投影与任务资格。通过该资格门的 Bug Agent 可以接收与本案相关的源码
   正文、日志正文、完整 traceback 和获准设计文档摘录；不再把“正文是否存在”本身当作拒绝条件。
2. 源码工具必须绑定已确认的 subject 和 source revision，并允许 Agent 搜索受控代码根、打开真实源码 span
   或完整相关文件、追踪 caller / callee / import / 配置读取点和相邻分支。它不能读取批准根之外的任意文件，
   也不能 import、执行或动态求值目标插件。默认先取相关文件和 span，但诊断需要时可以继续扩展，而不是被
   结构化摘要永久截断。
3. 日志工具必须优先绑定当前 Reply / correlation；不能精确关联时，只能在明确的 Bot、subject、deployment
   generation 和时间窗中检索，并把关联强度标为不确定。工具可以返回相关原始日志行、异常消息、业务错误
   上下文和完整 traceback，同时保留时间、来源、drop / partial / stale 状态。缺少日志不能证明未发生。
4. 出站前仍执行确定性秘密与范围清理：Token、cookie、Authorization、私钥、密码、凭据和未脱敏秘密配置
   永不发送；无关用户消息、无关服务日志和批准代码根之外的文件不得因同处一个日志文件或仓库而进入 Agent
   trajectory。对诊断必要的符号、相对路径、异常消息和参数可以保留，不能用一刀切移除再次破坏因果分析。
5. 模型外协调器固定允许的代码根、revision、日志源、correlation / 时间窗、单次与累计字节数、工具次数、
   deadline 和费用。Agent 可以在这些边界内决定下一项最有价值的证据，不能扩大数据源或自行切换 Provider。
6. 真实资格绑定精确 `Provider + API + model/profile + bug task + Prompt/schema revision + 数据策略 + 预算 +
   held-out`，不能继承语义分类、guidance 或历史 B4 smoke 的资格。资格必须记录 Provider 当前的训练与保留政策
   及复核日期；政策过期或变化时停止真实请求并返回 `unknown`。
7. 2026-08-14 的 OpenCode Go 官方资料列出 `deepseek-v4-flash` 不用于训练、数据保留为 0 天，并注明其 ZDR
   协议按月续约、当期有效至 2026-08-31。这足以支持进入 Bug task 独立资格评测，不等于该 task 已经准入，
   也不能在 2026-08-31 后不复核就继续外发。
8. 原始源码与日志只在本次受控 Agent trajectory 中瞬时使用；除非另有明确审计授权，不写入仓库、版本化
   eval、MLflow、普通日志或持久 verdict。测试使用合成源码和合成日志，不冻结真实部署正文。
9. 普通用户回复仍只显示 `bug`、`not_bug` 或 `unknown` 和安全原因，不显示源码、函数或类名、内部路径、
   日志正文、异常细节、其他用户内容或设计摘录。允许远端 Agent 读取证据不等于允许向普通会话披露证据。

## 为什么这样选

- 源码分析需要看到真实分支、调用和数据流；日志分析需要看到异常周边和完整 traceback，结构化摘要只能作为
  索引或首轮线索，不能替代正文；
- 相关性、秘密清理、Provider 数据政策、预算和用户输出投影可以直接控制实际风险，不必以牺牲诊断能力为代价；
- 当前 OpenCode Go 对精确模型给出了不训练和零保留承诺，但月度 ZDR 也说明这些承诺必须作为可失效资格证据，
  不能从一次确认永久继承。

## 没有采用的方案

### 只发送结构化源码事实和日志摘要

这会丢失异常周边、跨函数条件、字符串构造和分支细节，使 Agent 无法验证结构化提取器遗漏的因果链，最终把
大量本可判断的案例降级为 `unknown`。

### 默认上传整个仓库和全部运行日志

完整无差别上传既增加无关数据和费用，也会把其他用户、其他服务和秘密带入上下文。允许正文不代表取消
subject、revision、correlation、时间窗和代码根边界。

### 因为 OpenCode Go 当前是零保留就取消本地清理

零保留不等于可以发送凭据或无关个人数据，而且 DeepSeek ZDR 需要按月续约。秘密清理和最小相关范围仍是
本项目自己的责任。

## 带来的影响

- 需要实现受控源码搜索 / 读取工具和关联日志读取工具，而不是只实现摘要 schema；
- 需要在网络前用 spy 冻结实际 payload，并测试完整 traceback、跨文件源码、秘密清理、无关用户排除、
  partial / dropped、Prompt injection、字节 / 工具 / 费用上限；
- 需要为 Bug task 使用全新 held-out 真实模型评测，且在每次资格运行前复核 OpenCode Go 精确模型的数据政策；
- 需要把历史“原始源码和日志不得出站”保留为被替代的历史决定，不能让旧文档继续约束新实现；
- 本决定的数据授权不单独构成真实资格；当前实际网络调用只限下述已经完成 adapter、Prompt、预算和
  held-out Gate 的精确 OpenCode Go Bug task 组合。

## 落实与确认

- **已确认**：项目作者明确允许相关源码、日志正文与 traceback 进入 Bug Agent，并要求不要以过度谨慎削弱
  源码诊断能力；
- **外部依据**：[Sentry Seer](https://docs.sentry.io/product/ai-in-sentry/seer)、
  [Datadog Bits Investigation](https://docs.datadoghq.com/bits_ai/bits_investigation/chat_bits_investigation/)、
  [Datadog 调查集成](https://docs.datadoghq.com/bits_ai/bits_investigation/configure/) 和
  [OpenCode Go 数据政策](https://dev.opencode.ai/docs/go/)；
- **已实现**：源码工具只搜索 / 读取当前已加载 subject 的批准 Python 根，不 import、执行或跨根读取；日志
  工具只返回当前 correlation 捕获的异常正文与完整 traceback，并在入 buffer 和模型投影前执行秘密清理。
  Agent 另可读取当前运行摘要、部署摘要与受控设计知识包，所有工具都有次数、字节、请求、timeout、token
  和费用上限。
- **已资格化**：OpenCode Go / Chat Completions / `deepseek-v4-flash` / non-thinking / Prompt
  `bug-assessment-agent-v1-prompt-v4` 的全新 16 条 held-out 通过：verdict 1.000、occurrence 0.9375、责任候选
  1.000、citation closure 1.000、预算合规 1.000；179,138 input / 6,682 output token，6,508 microUSD。
- **当前限制**：日志正文来源仅为本插件 runtime hook 精确关联捕获的 Matcher / API 异常，不是任意宿主文件
  日志检索；源码工具限 Python 文件与已加载模块根。OpenCode Go ZDR 到期后仍须复核，不能由本次 Gate 永久
  继承。

## 关系

- 部分替代 [ADR-0050](0050-use-a-bounded-agent-for-user-bug-assessment.md) 第 7、12 项对源码 / 日志正文与远端
  投影的限制，保留其只读工具、模型外预算、reconciliation 和普通用户安全输出；
- 部分替代 [ADR-0051](0051-let-the-bug-assessment-agent-query-design-rag.md) 第 5、9 项对设计正文出站的保守
  限制，保留知识包、authority、revision、applicability 和 Prompt 注入边界；
- 不扩大 [ADR-0038](0038-limit-semantic-assessment-remote-data-projection.md) 的语义分类 payload；Bug assessment
  使用独立 task 和独立数据资格。
- [ADR-0060](0060-use-scope-thread-and-post-route-conversation-context.md) 仅取消直接 Reply 与模型外锚定聊天
  可见正文的内容遮蔽；源码、日志、traceback、配置和其他部署证据仍执行本 ADR 的根限制、相关性过滤与秘密清理。

## 相关文档

- [模型 Provider 支持矩阵](../architecture/model-provider-support.md)
- [支持入口分流](../architecture/flows/support-intake-routing.md)
