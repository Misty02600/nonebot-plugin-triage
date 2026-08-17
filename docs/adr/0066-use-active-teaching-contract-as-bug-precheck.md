# ADR-0066：用当前公开教学合同前置筛查普通用户 Bug

## 状态

| 状态 | 决策日期 |
|---|---|
| 已采纳；首个保守纵切已实现 | 2026-08-15 |

## 背景

公开教学注释已经同时面向 Triage Answer Agent 与后续帮助展示。如果系统一边用这些内容教用户，另一边
又完全拒绝承认它们对用法的约束，用户即使严格照做也无法据此说明行为偏差；反过来，如果把所有自动注释
直接当成完整实现真值，又会让同一份源码生成“应该怎样”，再循环证明当前实现正确。

普通用户 Bug 入口还需要先回答两个较小的问题：用户报告的是哪一项当前公开能力，以及用户实际做了什么。
被动监听、支撑 Matcher、动态入口和无法确定调用形式的候选不应因为源码中存在就成为可教学能力；缺少明确
subject 或具体观察时，也不应先开放聊天、运行、日志、源码、设计或部署证据工具。

当前产品暂不引入人工审核、待审、批准或驳回教学注释的工作流。部署者选择自动生成与自动服务，系统就应
对实际向用户服务的那个 revision 承担合同责任。此前遗留的冗余配置清理不在本 ADR 中扩展为审核模式。

## 决定

### 统一公开可教学能力门禁

1. 能力影子、教学注释、帮助展示、普通 Guidance、Bug subject 定位与第一层用法检查必须复用同一个模型外
   `public teachable` 准入结果，不各自维护可见性规则。
2. 只有本轮成功注册和观察、可由用户主动调用、触发与展示形式可以确定、当前 adapter 匹配、受众为
   `public`、无 blocking issue 且 generation 新鲜的能力才能进入普通 ServingView。
3. 被动监听、定时或启动行为、仅承担状态推进或内部协作的支撑 Matcher，以及无法确定命令头、调用形式或
   用户可观察身份的动态入口，不注册成普通用户 capability，也不生成公开教学注释或展示 YAML。底层观察可
   继续作为维护者诊断和源码分析证据，但不能进入普通用户检索或模型上下文。
4. Reply 中保存的旧 `capability_id`、`fact_ids` 或 revision 只能跳过自然语言召回，不能跳过当前 ServingView。
   每次使用前仍须重新验证注册、受众、adapter、blocking issue、新鲜度与合同 revision；旧引用不能复活已
   隐藏、未加载、restricted、stale 或不确定的能力。

### 自动服务的教学注释成为第一层合同

5. 仅生成到 Triage data 目录、尚未被任何用户入口消费的文件不是合同。教学注释一旦被 Triage 的公开 Answer
   或确定性模板实际使用，或者被导出并由 Migut Help 等用户界面直接展示，其当前公开结构化字段就成为
   `active teaching contract`。
6. 当前不增加人工审核状态、审核队列、`auto / review` 模式或逐条批准配置。满足现有 Evidence、schema、
   ServingView 与 revision 门禁的自动注释，在进入实际教学服务时自动成为 active contract；部署者无需额外
   确认。尚未被服务的生成文件与失效缓存不能获得这一地位。
7. 第一层合同只覆盖实际公开服务的结构化教学字段，包括有序 `usages`、公开 requirements、interaction、
   supported subjects、公开场景 / 角色条件和 behavior boundaries。内部源码解释、配置键值、Evidence 正文、
   自由推理过程和未进入公开投影的字段不属于用户合同。
8. Triage 发出的教学回答应绑定 `capability_id + fact_ids + contract revision`。用户 Reply 该回答时可以精确
   恢复当时服务的教学版本，但仍须经过当前 ServingView；版本已经变化、失效或无法对齐时，不得断言用户
   违反当前合同。

### Bug 调查前先做用法合同检查

9. 当前 `triage` 必须先表达 Bug 目标或真实观察。路由后，模型外 resolver 使用当前文字、直接 Reply 和 scope
   Thread 的有界引用定位唯一 public capability，并取得非空的具体观察。能力身份与观察是两个字段：前者说明
   “哪个功能”，后者说明“实际发生了什么”；Reply 中的一次消息或 correlation 只是可选 operation anchor。
10. 缺少唯一 subject，或者缺少判断用法所需的实际操作、Bot 返回或报错时，共用 scope Thread 的唯一一次
    用户补充机会。这就是既有“必要信息不足”，不再另建一套 Bug 参数或额外轮次。补充仍无效时关闭 Thread，
    不开放证据工具、不建立 Bug 记录，也不产生其他副作用。
11. 在上述信息就绪前，Bug Agent 不得调用聊天历史、runtime、日志、源码、设计 RAG、部署或深层源码导航
    工具。当前请求、精确 Reply、Thread 中的结构化引用和公开能力索引属于入口上下文解析，不是 Agent 证据
    工具。
12. 用法检查只在结构化 active contract 能无歧义支持结论时短路：
    - 用户明确违反调用形式、必需输入或公开前提时，返回 `not_bug / teach_correction`，复用 public Guidance
      具体说明正确指令、参数、步骤和示例；不启动正式 Bug Agent，也不记录问题；
    - 用户行为处于公开 behavior boundary 内时，可以返回 `not_bug / explain_public_condition`；
    - 用户用法符合合同、但报告的可观察结果与合同冲突时，才进入正式 Bug Agent；
    - 合同缺失、含糊、stale、相互冲突或无法覆盖本次操作时，不能据此声称用户用错；有具体异常时进入正式
      调查，证据仍不足则保持 `unknown`。
13. 如果用户严格遵循系统实际服务的合同，而实现没有兑现，公开合同可以成为“本应发生什么”的第一层预期
    证据。问题责任可以落到插件实现、教学注释、帮助发布或 stale contract；不能把自动生成作为把责任退回给
    用户的理由。
14. active teaching contract 只证明已公开的用法与行为边界，不自动证明所有内部实现、未公开分支或产品选择
    正确。更深层 Bug verdict 仍要求同适用 revision 的预期与实际证据闭合，并经过现有 reconciler。

### 普通用户回复

15. `teach_correction` 直接复用 public Guidance Answer Agent 与相同的 public facts，不重新执行 semantic 分类，
    也不向 Answer Agent提供源码、日志、配置或责任证据。它可以详细纠正指令、参数、输入、Reply 顺序和公开
    场景要求。
16. 其他公开策略保持受限：`explain_public_condition` 只能解释已进入合同的公开条件；`retry_later` 只说明
    暂时性失败与重试；`generic_not_bug` 只给概括结论。配置键、配置值、群名单、Matcher、Rule、handler、
    源码、日志、Evidence ID 和内部责任候选不得进入普通用户回复。

## 理由

- 实际服务而非生成来源决定产品责任：自动内容一旦用于教学，就应成为用户可以依赖的公开承诺；
- 第一层用法检查能在明显误用时零调查工具完成纠正，减少成本、延迟和无意义的源码读取；
- ServingView、注释生成、帮助展示和 Bug subject 共用门禁，避免被动或不确定入口从另一条路径重新暴露；
- 把合同范围限定到公开结构化字段，既允许按教学内容纠错，也避免把当前源码的自由总结循环提升为完整实现
  真值；
- 唯一一次补充继续覆盖意图后缺失的 subject 和操作信息，不增加新的对话生命周期或 Waiter。

## 带来的影响

- 正式 Bug Agent 前已经增加模型外 subject / observation readiness 与 active teaching contract precheck；
- 教学服务必须保留可追溯的 capability、fact 与 contract revision，Migut Help 真正接入时也要能标识发布
  revision；
- 明显错误用法将直接进入公开教学纠正，不再消耗 Bug Agent 工具预算或建立问题记录；
- 自动注释错误一旦实际服务，就可能成为教学或发布缺陷，而不是由“未经人工审核”免责；
- 当前 runtime 只从健康、完整的 public ServingView 定位唯一 subject；缺少 subject 或具体观察时在创建案件
  指纹、源码后端与 Agent 工具箱之前返回。Handler 共用既有一次补充，Uninfo 成员查询也延迟到聊天工具实际
  读取时执行。
- 当前短路检查刻意保守：仅当直接 Reply 精确指向报障者本人的操作消息、消息调用当前 invocation、所有公开
  usage 都要求 Reply 上下文而该操作没有 Reply 时，才返回 `teach_correction`。其他参数、媒体、角色、场景、
  限流与 behavior boundary 的确定性检查仍待后续证据模型支持，含糊情况继续正式调查或 unknown。
- 当前 public contract Evidence 已包含所选能力的 active teaching annotation 与 revision；教学回答的
  `capability_id + fact_ids + contract revision` 出站绑定仍未实现。

## 没有采用的方案

### 所有自动注释都不是合同

没有采用。系统已经把自动注释用于用户教学时，再否认其合同作用会让用户无法依赖系统自己的说明。

### 所有生成文件立即成为合同

没有采用。未进入任何服务入口的 data 文件、失效缓存或候选输出没有影响用户，不能仅因生成成功获得公开
合同地位。

### 先建立人工审核工作流

当前没有采用。部署者暂时选择自动生成与自动服务；本 ADR 不增加待审区、批准状态、审核命令或相关配置。

## 与既有决定的关系

- 部分替代 [ADR-0061](0061-read-latest-bounded-conversation-window-for-bug-assessment.md) 第 11 项及“没有公开
  文档时把教学注释当预期合同”的未采用结论：未服务的生成注释仍不是合同，但实际服务的公开结构化字段
  现在是第一层合同；
- 部分替代 [ADR-0064](0064-refine-bug-conversation-evidence-and-verdict-contract.md) 第 13 项：人工确认不再是
  教学合同成立的唯一方式；
- 补充 [ADR-0032](0032-separate-capability-audience-analysis-and-platform-status.md)、
  [ADR-0034](0034-distinguish-matchers-from-user-observable-capabilities.md) 的 ServingView 与用户可观察能力门禁；
- 补充 [ADR-0058](0058-use-deterministic-evidence-and-bounded-navigation-for-teaching-annotations.md) 与
  [ADR-0062](0062-structure-capability-teaching-usages-requirements-and-interactions.md) 的注释生成、结构和服务责任；
- 保留 [ADR-0060](0060-use-scope-thread-and-post-route-conversation-context.md) 的一次补充与 Reply 路由后上下文
  边界；不增加新的 Thread 轮次。

## 相关文档

- [支持入口、Thread、Guidance 与 Bug 判定](../architecture/flows/support-intake-routing.md)
- [项目架构概览](../architecture/overview.md)
