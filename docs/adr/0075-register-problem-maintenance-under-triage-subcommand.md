# ADR-0075：把问题维护注册为 triage 子命令

## 状态

| 状态 | 决策日期 |
|---|---|
| 已采纳；真实 Alconna 子命令、鉴权、查询与维护动作已实现 | 2026-08-15 |

## 背景

[ADR-0045](0045-use-one-triage-cooldown-and-localstore-capability-cache.md) 曾把 `triage`、`报错查询`、`报错反馈`
和 `报错统计` 固定为四个顶层命令。[ADR-0072](0072-use-opaque-problem-ids-and-minimal-maintainer-lifecycle.md)
沿用了 `报错查询 <编号> ...` 示例。

当前代码也确实注册了两套独立 Matcher：`triage` 使用接收任意文字的 Alconna 根命令；`报错查询`、`报错反馈`
和 `报错统计` 则各自拥有顶层 Alconna 命令。仅把文档改写成 `triage 报错查询` 并不会令它成为真实子命令，
而且自由文本 `triage` 入口可能把维护动作送入 Semantic LLM。项目作者确认问题查询和改判必须登记为
`triage` 的子命令，而不是新的顶层命令。

## 决定

1. Problem 查询与维护动作进入同一个公开命令根 `triage`，使用真实 Alconna 子命令分支：
   - `triage 报错查询 <问题编号>`；
   - `triage 报错查询 <问题编号> 确认Bug`；
   - `triage 报错查询 <问题编号> 确认非Bug`；
   - `triage 报错查询 <问题编号> 解决`。
2. 不再为 Problem 维护注册独立顶层 `报错查询` Matcher。插件元数据、帮助、usage、测试和能力投影只能宣传
   上述 `triage` 子命令形式。
3. 子命令必须由 Alconna / 模型外路由在 Semantic assessment 之前识别。命令参数、动作、公开 ID 格式或权限
   无效时直接返回确定性维护错误；不得把维护文字当普通自然语言送给 Semantic、Guidance 或 Bug Agent。
4. 子命令沿用 `triage` 的显式入口和统一冷却；`@Bot` 仍可选。每次执行前模型外重新检查当前 actor 是
   SUPERUSER，Reply、公开问题 ID、Thread 或 Agent 自然语言都不能授予维护权限。
5. 普通 `triage <求助内容>` 继续接收自然语言。命令树必须让已知维护子命令优先于自由文本 request 分支，
   不能依靠 Matcher 同优先级、注册顺序或 `block=True` 偶然决定哪一支执行。
6. 维护子命令不创建或续接 Support Thread。查询是一次只读终局；确认 Bug、确认非 Bug 与解决分别在 ORM
   事务成功后返回一次确定性结果。失败不会回退到自然语言 triage。
7. 本决定只迁移 Problem 查询与 ADR-0072 的首版维护动作。现有 trial 专用 `报错反馈`、`报错统计` 是否同时
   收口为 `triage` 子命令是独立兼容清理，不在本决定中顺带改变。
8. 历史 `报错查询 <incident-id>` 查询的是短期 Incident，Problem 使用 `P-...` 中性 ID。实现时删除或明确迁移
   旧顶层入口，不能让同一命令文本按编号形状隐式访问两套所有权不同的存储。当前短期 Incident 不跨重启，
   首个 Problem 版本不需要为旧内存记录提供持久兼容层。

## 理由

- `triage` 是插件唯一显式产品入口，问题维护作为子命令比继续增加顶层命令更容易发现和管理；
- 真正的 Alconna 子命令可以在模型调用前确定性解析参数与权限，不会让维护动作依赖意图分类；
- 共用根命令自然继承统一冷却、命令前缀和元数据合同，同时维护分支仍能保持 SUPERUSER 边界；
- 明确的命令树优先级比多个同优先级 Matcher 的注册顺序更可靠，也更适合后续添加查询参数；
- 暂不顺带迁移 trial 命令，避免把 Problem 工作流与仍属观察型试运行的旧功能合并成一次大范围兼容变更。

## 带来的影响

- `support_command` 需要加入 Problem maintenance 子命令，或由共享 Alconna 命令树派生确定性分支；现有
  `query_matcher` 不能继续作为 Problem 顶层入口；
- 自由文本 catch-all、子命令参数、permission、block 和优先级需要集成测试，特别要证明维护动作零 Semantic
  调用、非 SUPERUSER 无数据查询、普通 triage 不受影响；
- `QUERY_COMMAND` 不能再表示一个可独立调用的顶层命令；产品合同应改为子命令路径，避免 Handler 与元数据
  拼出不同语法；
- README、PluginMetadata 和 Migut Help 展示需要同步 `triage 报错查询 ...`；
- 已部署的独立顶层“报错查询”是破坏性命令迁移，应在版本说明中明确，不保留会误读两类编号的静默 alias。

## 没有采用的方案

### 保留独立 Matcher，只在文档前加 triage

没有采用。真实解析器仍会把 `triage 报错查询 ...` 当自由文本，维护权限和参数边界也无法由子命令表达。

### 让 Semantic LLM 识别维护意图

没有采用。维护动作具有权限和持久化副作用，必须由模型外显式命令、参数 schema 与 SUPERUSER 鉴权决定。

### 本轮同时迁移所有 trial 命令

没有采用。`报错反馈`、`报错统计` 属于旧观察型试运行，不是 Problem Decision / lifecycle；是否保留需要单独
核对其当前可达性和兼容价值。

## 与既有决定的关系

- [ADR-0079](0079-list-pending-problems-with-triage-query.md) 补充无编号的
  `triage 报错查询`，用于列出全部待处理 Problem；

- 部分替代 [ADR-0045](0045-use-one-triage-cooldown-and-localstore-capability-cache.md) 的固定顶层命令集合：
  `triage` 根和统一冷却继续有效，Problem 查询不再是独立顶层命令；
- 部分替代 [ADR-0072](0072-use-opaque-problem-ids-and-minimal-maintainer-lifecycle.md) 的命令示例；维护动作和
  SUPERUSER 边界不变，调用形式统一增加 `triage` 根；
- Problem Decision 的写入语义由
  [ADR-0074](0074-preserve-append-only-problem-decisions.md) 定义，事务由
  [ADR-0073](0073-use-nonebot-orm-for-authoritative-bug-workflow-state.md) 定义。
- [ADR-0076](0076-remove-legacy-trial-feedback-and-stats-chat-commands.md) 已决定直接删除旧 trial feedback / stats
  聊天入口，不把它们迁入本命令树。

## 相关文档

- [ADR-0072：使用中性公开问题编号与最小维护生命周期](0072-use-opaque-problem-ids-and-minimal-maintainer-lifecycle.md)
- [统一支持入口](../architecture/flows/support-intake-routing.md)
