# ADR-0074：用追加式 Problem Decision 保留 Agent 判断与人工改判

## 状态

| 状态 | 决策日期 |
|---|---|
| 已采纳；Bug 与人工复核的追加式 Decision 已实现，unknown Decision 由 ADR-0078 暂缓 | 2026-08-15 |

## 背景

[ADR-0068](0068-treat-qualified-agent-bug-verdicts-as-operational-decisions.md) 已决定：合格 Agent 的 `bug`
是正式产品 verdict，人工复核是事后监督而不是发布前审批；人工仍可以把 Bug 改判为非 Bug。
[ADR-0072](0072-use-opaque-problem-ids-and-minimal-maintainer-lifecycle.md) 又定义了“确认Bug”和“确认非Bug”维护
动作，但尚未决定这些动作是覆盖 Problem 当前字段，还是保留每次判断。

如果只覆盖 `current_verdict`，后续就无法区分“Agent 原本判断错误”“主人只是确认了原判断”和“主人后来又
改变了结论”，也无法把人工 override 回流为 Agent 评测样例。另一方面，保存完整模型 trajectory、聊天、源码
或日志会把 Problem 数据库变成第二套证据仓库，明显超出维护判断历史所需的范围。

## 决定

1. 每个持久 Problem 使用只追加的 `ProblemDecision` 记录判断历史。既有 Decision 不原地修改、不删除；需要
   纠正时追加新 Decision。
2. `ProblemDecision` 至少包含：
   - 稳定 decision ID、problem ID、创建时间；
   - verdict：`bug`、`not_bug` 或深度 `unknown`；
   - decision source：合格 Agent、人工确认、人工 override；
   - 可用时的前一条 decision ID，用于形成明确顺序；
   - Agent task / Prompt / Schema / qualification revision，或人工 actor 的作用域 HMAC；
   - 支持本次决定的 Evidence receipt 集合摘要与 assessment revision。
3. Decision 不保存聊天正文、Reply 正文、源码、日志、配置、模型消息、自由推理、平台原始用户 ID 或
   Evidence 正文。维护者可以通过仍有效的 receipt 进入独立调查工具；receipt 过期不会改写历史 verdict。
4. `BugProblem` 保存当前 `verdict`、`decision_source`、`review_status` 和 `current_decision_id`，作为快速查询投影；
   追加 Decision 与更新当前投影必须在 ADR-0073 的同一 ORM 事务中提交。Decision 历史是判断变化的权威来源，
   当前投影不得单独更新。
5. Agent 首次输出 `bug` 或完成调查的深度 `unknown` 时追加第一条 Decision。Agent `not_bug` 仍按 ADR-0068
   默认不建立 Problem，因此不会只为统计自动创建 Decision。
6. 对已经是 Agent Bug 的 Problem 执行“确认Bug”，追加 verdict 仍为 `bug`、source 为人工确认的 Decision，
   并把 `review_status` 改为已复核；这不是把候选升级为正式 Bug。
7. 执行“确认非Bug”时追加 verdict 为 `not_bug`、source 为人工 override 的 Decision，更新当前投影并保留
   Agent 原判断。深度 `unknown` 也通过相同动作进入人工 `not_bug` 终局。
8. 对深度 `unknown` 执行“确认Bug”时追加人工 `bug` Decision。只有当前 verdict 为 `bug` 的 Problem 才能进入
   “解决”生命周期。
9. 同一维护命令的消息重投或事务重试必须使用模型外幂等键，不能生成重复 Decision。两个并发维护动作必须
   通过当前 decision ID / 乐观并发条件收敛；后到动作不能基于已经过期的当前状态静默覆盖先到动作。
10. Problem 的“开放、已解决、回归”等 lifecycle 变化不伪装成 verdict Decision。若需要审计生命周期，使用
    独立的最小 lifecycle event；首版查询可以显示当前 lifecycle，不要求同时实现完整事件浏览界面。
11. `triage 报错查询 <编号>` 默认显示当前 verdict、来源、是否人工复核、lifecycle 和最近一次判断时间；普通
    用户始终只看到固定事务回执。完整 Decision 历史仅供后续 SUPERUSER 查询扩展，不在首版聊天输出中展开。

## 理由

- Agent 的正式判断与人工 override 都是事实；覆盖字段会抹掉发现 Agent 缺陷所需的最重要反馈；
- 追加式 Decision 能把“确认原判断”和“改变原判断”区分开，同时不把人工确认误写成 Agent 才使 Bug 生效；
- 当前投影让常规查询保持简单，事务内同步又避免事件历史和当前状态不一致；
- 只保存 revision 与 receipt 摘要即可复盘判断来源，不需要长期复制敏感、体积大的证据正文；
- 幂等键和基于 current decision 的并发条件能避免重复命令或并发主人操作制造多条假历史。

## 带来的影响

- ORM schema 除 Problem 外需要 Decision 表，以及 Problem 指向当前 Decision 的约束；
- Repository / Unit of Work 要提供追加 Decision 并更新当前投影的单一事务操作，Handler 不直接修改 ORM 字段；
- 评测与维护统计可以按 qualification revision 和人工 override 聚合，但不能从 Decision 记录恢复原始模型
  trajectory；
- 未来若增加“撤销改判”，仍应追加新的纠正 Decision，不删除历史；
- 生命周期审计与 verdict 审计保持分开，首版无需为了完整事件溯源扩大聊天命令。

## 没有采用的方案

### 只覆盖 Problem.current_verdict

没有采用。它会丢失 Agent 原判断、人工确认与 override 顺序，也无法把误判可靠回流为评测样例。

### 保存完整 Agent trajectory

没有采用。Decision 负责判断历史，不是运行证据仓库；完整消息和工具结果会扩大隐私、容量、迁移和保留成本。

### 把 lifecycle 变化也记为 verdict Decision

没有采用。“已解决”和“回归”不改变问题是否为 Bug，把两类状态塞进同一枚举会再次混淆判断与处理进度。

## 与既有决定的关系

- [ADR-0078](0078-defer-persisting-unknown-bug-assessments.md) 暂缓为深度 `unknown` 建立首条 Decision
  及其人工裁决路径；确定 `bug` 的追加式判断与人工 override 继续有效；

- 落实 [ADR-0068](0068-treat-qualified-agent-bug-verdicts-as-operational-decisions.md) 的正式 Agent verdict、人工事后
  监督与 override 回流；
- 补充 [ADR-0072](0072-use-opaque-problem-ids-and-minimal-maintainer-lifecycle.md) 的“确认Bug / 确认非Bug”动作；
- 使用 [ADR-0073](0073-use-nonebot-orm-for-authoritative-bug-workflow-state.md) 的事务保存 Decision 与 Problem 当前
  投影，但领域 Decision 不依赖 ORM 类型。

## 相关文档

- [ADR-0068：把合格 Agent 的 Bug verdict 作为正式判断并由人工事后监督](0068-treat-qualified-agent-bug-verdicts-as-operational-decisions.md)
- [ADR-0073：使用 NoneBot ORM 保存权威 Bug 工作流状态](0073-use-nonebot-orm-for-authoritative-bug-workflow-state.md)
