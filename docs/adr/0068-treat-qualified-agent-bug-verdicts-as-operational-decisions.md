# ADR-0068：把合格 Agent 的 Bug verdict 作为正式判断并由人工事后监督

> 后续关系：ADR-0086 已取消“只有精确已资格模型才能写入正式本地 Problem”的限制；所有模型仍须通过
> 本 ADR 的模型外 Evidence reconciliation，并记录实际 Provider、model 与评测标签。

## 状态

| 状态 | 决策日期 |
|---|---|
| 深度 unknown 持久化由 ADR-0078 暂缓；合格 Agent Bug 决定继续有效 | 2026-08-15 |

## 背景

[ADR-0054](0054-store-reviewed-bug-problems-in-localstore.md) 首先建立了人工审核 `bug / not_bug` 目录，
并把首版限制为维护者单写、在线只读。后续代码又形成了尚未接到 Bug runtime 的
`ConfirmedBugProblem` 原型：它只保存 Agent 已判定的 `bug` 和发生次数，但把 Agent verdict、人工复核和
问题生命周期都压进了“confirmed”一个概念，也不能保存经过完整调查后仍为 `unknown` 的维护者待判案件。

当前产品目标不是让 Bug Agent 只替维护者收集材料。只有通过精确 task qualification、独立 forward-heldout、
模型外 Evidence reconciliation、revision / budget / disclosure 门禁的组合才可以产生终局 verdict；一旦满足
这些条件，Agent 应当能够独立给出产品判断。另一方面，人工仍需要纠正误判、关闭或忽略问题，并把错误案例
反馈给评测与流程优化。

本决定只解决“Agent verdict 是否需要逐条人工批准”。同一问题如何识别和聚合多次 occurrence、历史结论何时
可以短路新调查，以及维护命令的完整语法另行决定。

## 决定

1. 通过当前精确资格组合并经模型外 reconciler 接受的 `bug` 是 Triage 的正式产品 verdict，不是等待人工
   批准才能成立的候选。系统可以立即向用户说明已确认并记录问题，并在 LocalStore data 中建立本地问题记录。
2. 人工监督采用事后复核和纠错，不作为每条 Agent Bug 的发布前门禁。问题记录必须分别保存：
   - `verdict`：`bug / not_bug / unknown`；
   - `decision_source`：Agent、公开前置检查或人工 override 等来源；
   - `review_status`：未复核、已复核或已改判；
   - `lifecycle_status`：开放、已解决或已忽略等处理状态。
   字段名称和枚举值可以在实施计划中收敛，但四个语义维度不得再次合并成一个 `confirmed` 状态。
3. 维护者确认 Agent Bug 只把复核状态改为“已复核”，不改变其在确认前已经成立的 `bug` verdict。维护者改判
   为非 Bug 时，人工 override 具有更高权威，并必须保留原 Agent 决定、适用 revision、改判时间和安全的改判
   原因，以便审计和形成后续评测样例。
4. 已经完成实际聊天、运行、日志、源码、设计或部署调查，却仍因证据冲突、实现语义不明确或责任无法闭合而
   得到的 `unknown`，可以建立需要维护者判断的调查记录。它不是 Bug，普通用户只看到“暂时无法判断是不是
   Bug，但已经记录”。
5. 下列 `unknown` 不建立问题或调查记录：尚未定位具体 public subject、没有具体观察、唯一一次用户补充仍
   无效、当前 Provider / Agent 不可用，或者在没有实际调查的情况下仅因平台缺少可选上下文而失败。它们按
   [ADR-0066](0066-use-active-teaching-contract-as-bug-precheck.md) 在开放工具前结束，不产生持久副作用。
6. Agent `not_bug` 默认不建立问题记录。公开前置条件、正确用法与安全回复继续由既有教学和披露策略处理；
   是否为维护质量统计保存匿名 decision event 不在本决定范围内。
7. 自动持久化只写部署本地 LocalStore data，不自动创建 GitHub Issue、发送外部通知、修改标签、执行修复或
   操作生产环境。任何此类副作用仍须新的明确授权和风险评审。
8. Agent 判断的准确性通过资格 Gate、线上监测、人工 override 样例和新 forward-heldout 持续维护。出现误判
   时应修复证据链、Prompt、Schema、reconciler 或工具边界，而不是把所有正确判断永久降级为逐条人工审批。

## 理由

- Bug Agent 的产品职责是独立完成三值判断；若所有 `bug` 都只能成为候选，Agent 实际上退化为维护者材料
  收集器，与已建立的任务资格和模型外 reconciler 不一致；
- 创建一条部署本地、可撤销的问题记录影响低且不产生外部副作用，适合自动执行并由人事后监督；
- 把 verdict、来源、复核和生命周期分开，既承认合格 Agent 的正式判断，也能准确表达“尚未人工看过”、
  “人工改判”和“已忽略但仍是 Bug”等不同事实；
- 深度 `unknown` 对维护者有调查价值，而入口信息不足或服务不可用没有问题身份，不应制造噪声记录；
- 人工 override 是发现评测和流程缺口的高价值反馈，应保留并回流，而不是只覆盖最终字段。

## 带来的影响

- 现有 `ConfirmedBugProblem` / `runtime-confirmed-bug-problems.json` 原型不能原样接线，需要迁移到中性的问题与
  调查模型，并显式区分 verdict、来源、复核与生命周期；
- 当前 `reviewed-bug-problems.json` 可以继续保存人工 verdict catalog，但不再是问题记录能够存在的唯一来源；
- 只有人工审核记录能否短路后续 Agent、Agent Bug 能否短路精确重复案件，以及两个存储是否合并，仍由后续
  的问题身份与 occurrence 决定处理；
- 当前 Bug runtime 仍只返回 decision，尚未写入自动问题或深度 unknown；实施前需要先完成问题身份、版本
  适用性和重复 occurrence 的独立决定。

## 没有采用的方案

### 所有 Agent Bug 必须人工批准

没有采用。它会把已经通过任务资格和确定性协调器的正式 verdict 降成候选，使 Agent 无法独立完成产品职责，
并把维护者注意力消耗在逐条批准上。人工仍可以复核、改判和忽略。

### Agent Bug 无法被人工覆盖

没有采用。合格组合降低但不能消除误判；人工 override 必须具有更高权威，并成为后续评测和流程修正的输入。

### 所有 unknown 都建立记录

没有采用。没有具体 subject、观察或有效调查的 unknown 不代表一个可维护问题，只会制造无法行动的噪声。

## 与既有决定的关系

- [ADR-0078](0078-defer-persisting-unknown-bug-assessments.md) 暂缓本 ADR 的深度 `unknown` 持久化；合格
  Agent `bug` 的正式 verdict、人工事后监督和入口不足不落库继续有效；

- 部分替代 [ADR-0054](0054-store-reviewed-bug-problems-in-localstore.md) 第 3、8 项：人工 reviewed catalog 继续
  维持维护者单写和在线只读，但合格 Agent 的正式 Bug 与深度 unknown 可以写入独立的运行问题所有权域；
- 保留 ADR-0054 的 LocalStore data 所有权、原子写入、完整性、revision、损坏失败语义，以及只有精确 verified
  catalog 可以短路的现行边界；短路规则是否扩大留待问题身份决定；
- 部分替代 [ADR-0050](0050-use-a-bounded-agent-for-user-bug-assessment.md) 的“首版只判断、不建立记录”范围；
- 补充 [ADR-0066](0066-use-active-teaching-contract-as-bug-precheck.md)：入口信息不足继续零工具、零持久化，
  只有完成正式调查后的 `bug` 或深度 `unknown` 才进入本决定的记录边界；
- [ADR-0070](0070-separate-bug-reports-occurrences-and-problems.md) 进一步把持久化对象分为 Report、Occurrence
  与 Problem，并禁止把 Report 次数直接当成 occurrence 次数；
- [ADR-0071](0071-group-bug-problems-with-versioned-evidence-fingerprints.md) 进一步规定 Problem 由模型外可复算的
  版本化 Evidence 指纹聚合，并固定普通用户成功 / 深度 unknown 回执。
- [ADR-0074](0074-preserve-append-only-problem-decisions.md) 进一步规定 Agent 判断、人工确认和 override 以追加式
  Decision 保存，不能覆盖原判断。

## 相关文档

- [支持入口、Thread、Guidance 与 Bug 判定](../architecture/flows/support-intake-routing.md)
- [项目架构概览](../architecture/overview.md)
