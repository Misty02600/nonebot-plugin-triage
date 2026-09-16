# ADR-0101：用 LangGraph Checkpoint 保存长期开发者行为讨论

| 状态 | 决策日期 | 最近复评 |
|---|---|---|
| 已替代；SUPERUSER 自由对话已经迁移到 [ADR-0128](0128-use-native-message-snapshots-for-maintainer-conversations.md) | 2026-08-21 | 2026-09-15 |

## 当时遇到了什么

四意图路由已经把公开教学、行为探索、Bug 判断和功能建议分开。公开教学与 Bug 受理仍适合一次补充的
短期 `SupportThread`；行为探索却需要维护者围绕同一部署事实持续追问，在 Bot 重启后继续，并把旧结论的
证据 revision、`partial`、`stale`、冲突和未知带到下一轮。

ADR-0025 已确定行为探索必须先执行模型外维护者鉴权，再协调能力影子、源码、配置投影、版本和运行观察等
多源证据；但它明确没有建立持续会话。ADR-0060 的 `SupportThread` 又只允许一次补充、只驻留内存且不跨
重启，不能直接扩展成长期工作区，否则会改变 Guidance 与 Bug 已经验证的交互合同。

LangGraph 提供 thread-scoped State、逐 super-step checkpoint、恢复、历史和整 Thread 删除。这里的
“thread-scoped memory”即使长期落盘，仍然只是同一讨论的工作状态；跨 Thread 自动复用的用户偏好、项目
事实或成功轨迹是另一类长期记忆，不应因为会话需要跨重启就一起引入。

## 决定

### 复评后继续采用 Pydantic AI + LangGraph 的方案 A

2026-09-15 以 Pydantic AI 2.43.0 与 Pydantic AI Harness 0.31.0 重新核对持久化边界。Pydantic AI Core
已经提供 `conversation_id` / `run_id`、消息序列化与恢复入口、history processor / compaction，以及
Temporal、DBOS、Prefect、Restate、AWS Lambda 等 durable execution 集成；Harness `StepPersistence` 也已
提供 File / SQLite / MongoDB store、Agent step event、continuable snapshot、`continue_run()` / `fork_run()`
和 tool-effect ledger。不能再用“Pydantic AI 不支持持久化”解释本 ADR。

但 `StepPersistence` 的官方边界仍不是完整 graph-state checkpoint：它不恢复 capability per-run state、任意
workspace、graph node、retry counter 或 in-flight stream，副作用去重、整 Conversation 删除、TTL / event /
media GC 仍由调用方承担，官方文档与内置 store 也没有提供本 ADR 当前依赖的 checkpoint 加密与 PostgreSQL
adapter。其
continuable snapshot 保存 Pydantic AI 消息，而 Behavior 的持久化合同明确禁止保存用户原文、原始工具结果和
完整模型 I/O。

因此继续采用方案 A，但把理由收窄为职责匹配，而不是框架能力有无：

- Pydantic AI 拥有模型 / Provider、单轮 Agent loop、工具协议、结构化输出和**单轮内部**消息历史处理与
  compaction；项目不实现通用 history 修复、裁剪或摘要框架；
- LangGraph Checkpointer 只拥有经过领域校验、最小化和加密的**跨轮 Behavior Workspace**，以及 Thread
  checkpoint / 恢复 / 整体删除；
- 项目领域层拥有 schema、scope 鉴权、Evidence provenance / freshness、Claim、预算、投递幂等和保留策略；
- Behavior 不同时启用 Harness `StepPersistence`，避免完整消息快照与安全 Workspace 形成第二份持久真值；
- Harness `Memory` 是模型可写且可能过期的非权威笔记，不用作 Claim、授权、审批或项目事实真源；
- `UseThreadExecutor` 只是同步回调的线程池能力，与对话 Thread 持久化无关。

出现以下任一变化时重新比较 Pydantic AI 单栈与方案 A：`StepPersistence` 能恢复项目所需的类型化 workspace
并满足加密、最小化和整 Conversation 删除；Behavior 不再需要 workspace / graph 恢复而只需消息续聊；或
方案 A 的双框架集成成本经真实故障测试证明高于单栈收益。任何替换 spike 都必须复用相同的重启恢复、错误
密钥失败关闭、原文不落盘、scope 隔离、整 Thread 删除、未决副作用与 schema 升级合同。

### 让 Checkpoint 成为首版 Behavior 工作区的唯一持久真值

1. 行为探索首版采用嵌入式 LangGraph。锁定 `langgraph==1.2.11`、`langgraph-checkpoint==4.2.0`、
   `langgraph-checkpoint-sqlite==3.1.1`、`aiosqlite==0.22.1` 与 `pycryptodome==3.23.0`，由
   `AsyncSqliteSaver` 保存 State；Pydantic AI Agent 继续拥有
   模型、Provider `ModelProfile`、结构化输出、工具与单次 ReAct 循环。LangGraph 不替代 Pydantic AI，
   Pydantic AI 的完整 `message_history` 也不进入 LangGraph，Behavior 不再并行启用 Harness
   `StepPersistence`。
2. 每个 `adapter + Bot + conversation + actor` 只有一个长期 Behavior 工作区。通过部署本地长期稳定密钥、
   结构化编码和用途隔离的 HMAC 直接派生内部 `thread_id`；不暴露 Thread ID，不允许调用者提交任意
   `thread_id` 或 `checkpoint_id`，首版也不提供多工作区、列表、共享、归档或跨作用域恢复。
3. 在上述收窄合同下，不新增 Behavior `Inquiry / Turn / Run / Artifact / Claim / Outbox` ORM 表。
   Checkpoint 保存当前安全会话聚合、幂等窗口、解释产物和系统认知到的投递状态。只有以后出现多 Inquiry、
   ACL、列表与归档、多 worker、后台可靠投递、单消息删除、密钥无缝轮换或独立 Artifact 生命周期时，才
   引入图外持久控制面。
4. SQLite saver 只支持单机单 writer Bot。启动时必须取得同一 checkpoint 数据库的进程级独占锁；同一
   Thread 的所有运行、读取、投递状态修改和删除再经过进程内 keyed admission。并发第二个 Turn 直接返回
   `BUSY`，不排队、不回滚旧 Turn，也不打断正在执行的 Agent。多进程或多 worker 部署必须改用经过资格
   验证的 Postgres / Agent Server 等协调后端，不能仅依赖进程内锁。

### State 只保存安全、版本化、可恢复的语义工作区

5. State 使用项目定义的严格 Typed / Pydantic schema，至少包含：
   - `state_schema_version` 与 `graph_revision`；
   - 不透明 scope binding digest；
   - 有界的安全 Turn、工作摘要、开放问题和最近事件幂等窗口；
   - `observed_structure / observed_behavior / static_inference / unknown` Claim；
   - 带 source kind、受控 locator、revision、captured time 和 freshness 的 Evidence Reference；
   - 当前 Explanation Artifact revision；
   - `pending / sending / sent / failed / unknown` 投递状态。
6. State、node / task return、pending write、interrupt payload 和 checkpoint metadata 都不得包含用户原文、
   原始源码、原始配置值、日志正文、绝对路径、凭据、Bot / Event / Target、工具参数与原始结果、Provider
   原始输入输出、Pydantic AI messages、系统 Prompt、隐藏推理或未脱敏异常。用户问题与受限证据只存在于
   当前 runtime context、task 参数或 node 局部变量；持久 Turn 只保留请求 digest、Artifact revision 和投递
   状态。模型输出必须经模型外 Evidence ID 闭包、basis、freshness、路径与秘密检查后，才能形成可
   checkpoint 的 Claim / Artifact。
7. Saver 使用稳定派生的独立加密密钥和 `EncryptedSerializer`；底层 `JsonPlusSerializer` 关闭 pickle
   fallback，并只接受当前 State 所需的 JSON / msgpack 安全值。加密只保护静态介质，不能替代第 6 条的
   数据最小化。密钥丢失、错误或 checkpoint 无法解密时失败关闭，绝不能把旧 Thread 当作空 Thread 覆盖。
8. 每轮开始时重新读取当前 evidence generation。旧 Claim 的 revision 不再匹配、来源本身 stale、证据缺失
   或冲突时，先降为 `stale / conflicted / unknown`，再允许 Agent 回答；已保存的历史结论不能自动升级为
   当前项目事实。模型提出的每项非 unknown Claim 都必须引用本轮真正取得的 Evidence ID。

### 一轮 Run 完成不关闭长期 Thread

9. 四意图 taxonomy 与“Semantic 只看当前文字”保持不变。明确识别为 Behavior 的请求在路由后鉴权并进入
   长期 Thread。若当前文字只能得到原因为 assessment unresolved 的 `CLARIFY` 或 `OUT_OF_SCOPE`，则仅在先
   通过 `SUPERUSER` 且同 scope 已存在 Behavior Thread 时，把它作为自然续问；明确 Bug、Guidance、Feature
   或 Refuse 的结果始终优先，不会被旧 Thread 改写。
10. 普通跟进是同一 Thread 上的一次新 graph invocation：结束的是本轮 Run，不是 Thread。首版不使用
    `interrupt()` 表示日常多轮聊天；只有将来一次调查确实需要跨进程等待审批或补证时，才把 interrupt / resume
    作为同一未完成 Run 的恢复机制，并在每次 resume 前重新鉴权。
11. Pydantic AI ReAct 首版位于一个 LangGraph node 内，工具全部只读；实际调用再包进 LangGraph `task`。
    task 参数可以携带当前用户原文而不落 checkpoint，task 结果只能是已验证的安全 JSON 候选或稳定失败码，
    因为 task 结果与错误也可能进入 checkpoint。进程在 task 完成前崩溃仍可能重复模型费用，但不会重复外部写
    操作；每次 Run 仍受 request、tool、token、费用和 deadline 硬预算。
12. 单轮 ReAct 内的消息历史增长由 Pydantic AI 的 history processor、Harness compaction strategy 或经过资格
    验证的 Provider 原生 compaction 处理；不在领域层实现通用截断、Tool Call 配对修复或摘要运行时。压缩结果
    只是本轮模型上下文，不自动成为证据或跨轮权威状态；需要进入下一轮的内容仍须经过 Evidence / Claim 校验
    后投影到安全 Workspace。首个有界 ReAct 未达到压缩触发条件，因此当前只冻结所有权，不提前启用能力。

### 幂等、投递和删除采用保守语义

13. 只接受 Adapter 提供的稳定、不能由消息正文伪造的 event / message ID。其用途隔离 HMAC 与当前请求
    digest 进入有界幂等窗口：相同 event 和相同正文不再运行 Agent；相同 event 与不同正文失败关闭。窗口
    之外不承诺永久去重，也不使用“正文 + 时间”猜造 Event ID。
14. 发送不是可重放 Graph node。Graph 先以 `durability="sync"` 保存 Artifact 和 `pending`；当前调用栈再
    同步保存 `sending(invocation_id)`，之后最多调用平台一次。合法 Receipt 后写 `sent`；平台调用已经开始却
    抛错、取消、Receipt 无法验证或进程在保存 `sent` 前崩溃时，恢复为 `unknown`，绝不自动重发。该协议
    倾向 at-most-once，只表达系统对投递的认知，不承诺平台 exactly-once 或可靠最终送达。
15. 首版只支持整 Thread 删除。删除在 scope lock 内调用 `adelete_thread(thread_id)`，同时清除 checkpoints
    与 writes；不声称能从历史 checkpoint 中单独擦除一个 Turn。删除后若平台极晚重投旧 Event，仍可能在
    同一派生 ID 上建立新 Thread；需要严格 tombstone 时必须增加图外持久控制面。
16. 当前 State 的条目数、字段长度和序列化字节有硬上限；每 Thread checkpoint 数也有保守上限。SQLite
    saver 3.1.1 没有可用的原生 `keep_latest` prune，首版不用 beta `DeltaChannel`，到达上限后拒绝继续并要求
    维护者显式整 Thread 重置，不用“删除后再写回”冒充原子 compact。长期保存不等于无限增长。

### 跨 Thread Memory 明确后置

17. 首版不启用 LangGraph Store 或 Harness `Memory`，不从历史 Thread 自动提炼用户画像、项目事实、工具批准
    或 few-shot。当前 Thread 的安全状态可以长期保存，但归档 Thread 本身不自动成为 episodic / semantic
    memory。
18. 以后确需跨 Thread 记忆时，新增独立 `MemoryCandidate → Policy / Validation → MemoryItem` 流程；每项
    Memory 必须有 deployment / project / actor namespace、来源 Thread / Artifact、Evidence revision、TTL、
    ACL、冲突 / supersession 和删除语义。模型与不可信项目文字不能直接写全局权威记忆。

## 为什么这样选

- 它直接复用 LangGraph 已有的 State、checkpoint、恢复和整 Thread 删除，不再为相同职责维护一套 ORM
  会话状态机；
- 稳定 scope HMAC 和不暴露 Thread ID 让首版能够在不建立 Inquiry Directory 的情况下完成 owner 绑定；
- 把 Pydantic AI ReAct 放在一个只读 node 内，保留项目已有 Provider、资格、工具和结构化输出边界；
- 明确区分“长期保存同一任务状态”和“跨任务自动记忆”，避免把全部聊天向量化后当作权威项目知识；
- `unknown` 投递、单 writer、整 Thread 删除和容量硬上限把 SQLite checkpoint 的真实限制转成可测试合同，
  而不是隐藏在乐观描述里。

## 没有采用的方案

### 为 Behavior 同时建立完整 ORM 聚合和 LangGraph checkpoint

没有采用。首版没有多 Inquiry、共享 ACL、独立 Artifact 审批、后台投递或复杂查询需求；两份权威状态会增加
事务、删除、迁移和修复成本。上述触发条件出现后，ORM 可以只补图外产品控制面，而不是复制 Graph State。

### 把完整消息历史或 Pydantic AI message_history 当作长期记忆

没有采用。它会把旧工具调用、模型文本、权限暗示、Prompt 注入和过期项目事实重新放进可信控制流，也违反
ADR-0089 的生产持久化边界。下一轮上下文由安全摘要、相关近期 Turn、当前 Claim 与重新获取的证据编译。

### 用最新 Harness StepPersistence 直接替代 LangGraph Checkpointer

复评后暂不采用。`StepPersistence` 已适合保存和恢复 Agent 消息、Run step 与 tool-effect ledger，但当前版本
明确不恢复任意 workspace / graph node / capability state，也没有直接满足本项目加密、整 Conversation 删除和
不保存用户原文的合同。若只为使用它而另建 Behavior ORM 状态、过滤层和删除器，会重新形成两份持久状态；
按上述复评条件证明单栈能完整满足合同后再替换。

### 首版启用 LangGraph Store 或向量化全部历史

没有采用。同 Thread 连续性由 Checkpointer 已经解决；Store 是跨 Thread 检索与记忆问题，需要独立的
namespace、权限、来源、有效期、撤销和污染防护。

### 用 interrupt 表示每一次用户跟进

没有采用。一次回答完成后 Graph Run 已结束，Thread 仍可继续；把普通聊天都悬成未结束 Run 会复杂化节点
重放、部署迁移和长期资源治理。interrupt 只保留给真正未完成的审批或补证等待。

## 带来的影响

- 新增 LangGraph 与 SQLite checkpointer 基础依赖，以及 checkpoint 加密所需的密码实现；Provider SDK 仍按
  现有 extra 可选安装；
- 新增长期 Behavior State、Pydantic AI 行为探索 Agent、Capability Shadow 只读证据工具、加密 saver
  生命周期、scope / process admission 和投递状态协议；
- Bug、Guidance、Feature 路由与一次补充 `SupportThread` 不改；只有 Behavior 明确获得长期续问例外；
- 首个证据纵切可以只覆盖当前 Capability Shadow 的安全结构事实，并对尚未接通的源码、配置与运行证据明确
  返回 partial / unknown；后续证据工具必须继续服从 ADR-0059 的 task-specific policy；
- 部署者必须保护并备份本地身份 / checkpoint 密钥与数据库。密钥丢失不会泄露明文，但会使旧 Thread 无法
  恢复；
- SQLite 首版不适合多 worker 和无限会话规模。达到产品触发条件时升级控制面或后端，不在当前 saver 上
  堆叠未经验证的并发与裁剪逻辑。

## 落实与确认

- **2026-08-21 技术资格 spike**：在当前 Windows 环境验证 LangGraph 1.2.11、
  `AsyncSqliteSaver` 3.1.1、同步 durability、关闭 pickle fallback 的加密 serializer、跨连接重启恢复、
  `aupdate_state()` 投递状态更新和 `adelete_thread()` 整 Thread 删除；明文 canary 未出现在 SQLite / WAL，
  错误密钥读取失败关闭。
- **2026-08-21 首个纵向切片**：已实现长期 State 与模型外 reconciler、Pydantic AI 只读 ReAct、
  Capability Shadow 安全投影、插件生命周期、scope / process admission、保守投递状态、整 Thread 重置、
  处理器续接和高价值合同测试。当前切片仍统一报告 partial；源码、配置、运行观察等多源证据与独立真实模型
  held-out 质量资格尚未实现。
- 本 ADR 已由项目作者确认方向；它不表示未完成的证据源或跨 Thread Memory 已经可用。

## 替代关系

- 被 [ADR-0128](0128-use-native-message-snapshots-for-maintainer-conversations.md) 替代其 SUPERUSER 自由对话的
  Workspace、加密、actor/scope 隔离和 checkpoint 控制；旧 Behavior 实现已从运行代码移除；
- 部分替代 [ADR-0025](0025-explain-plugin-behavior-from-deployment-evidence.md) 第 8 条“不建立持续会话”以及
  “不引入跨重启权威状态”的范围；其多源证据、鉴权、Claim basis、只读与秘密边界继续有效；
- 只对 Behavior 长期续问部分替代
  [ADR-0060](0060-use-scope-thread-and-post-route-conversation-context.md) 的一次补充和不跨重启边界；Guidance
  与 Bug 的短期 Thread，以及 Semantic 只读当前文字的规则继续有效；
- 延续 [ADR-0059](0059-share-read-only-evidence-access-across-agent-flows.md) 的共享只读底座与 task-specific
  policy；
- 延续 [ADR-0063](0063-keep-plugin-startup-independent-from-model-enhancements.md) 的可选模型失败降级；
- 延续 [ADR-0084](0084-install-pydantic-ai-control-plane-by-default-and-keep-providers-and-adapters-optional.md)
  的 Pydantic AI `ModelProfile` 所有权；
- 延续 [ADR-0089](0089-persist-redacted-pydantic-ai-agent-traces.md) 的生产正文持久化禁区。

## 相关资料

- [LangGraph Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api)
- [LangGraph Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)
- [LangGraph Checkpointers](https://docs.langchain.com/oss/python/langgraph/checkpointers)
- [LangGraph Memory](https://docs.langchain.com/oss/python/concepts/memory)
- [LangGraph Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)
- [LangGraph SQLite Checkpointer](https://pypi.org/project/langgraph-checkpoint-sqlite/)
- [Pydantic AI Messages and chat history](https://pydantic.dev/docs/ai/core-concepts/message-history/)
- [Pydantic AI Harness Step Persistence](https://pydantic.dev/docs/ai/harness/step-persistence/)
- [Pydantic AI Durable Execution](https://pydantic.dev/docs/ai/capabilities/durable_execution/overview/)
- [Pydantic AI Harness Memory](https://pydantic.dev/docs/ai/harness/memory/)
