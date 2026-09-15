# ADR-0132：Bug 候选结论由判断原因派生

| 状态 | 决策日期 |
|---|---|
| 已采纳；正式调查入口继续暂停 | 2026-09-13 |

## 问题

现有七种候选原因已经分别表达 Bug、非 Bug 或无法判断。要求模型同时填写 `verdict` 和 `reason` 会产生矛盾组合，再为这些组合增加校验并不提供新的取证能力。

## 决定

- 模型只选择 `reason`，程序派生候选 `verdict`：实现或运行违背合同对应 `bug`；公开前置条件未满足、有意配置、符合非 Bug 定义的暂时外部故障对应 `not_bug`；证据不足或冲突对应 `unknown`。原因表示完整判断类别，看到外部超时本身不代表已经满足非 Bug 条件。
- `BugAssessmentCandidate.verdict` 是只读派生属性，不进入模型 schema 或序列化，也不接受构造时传入。内部最终 `BugAssessmentDecision` 继续保存 `verdict`，下游工作流及持久化结构不变。
- 原有候选形状和证据检查继续生效；确定性原因仍需引用与责任候选，不确定原因仍需缺失证据类别。reconciler 可以因引用无效、证据陈旧或不完整、缺少预期或实际证据而收口为 `unknown`。派生结论不证明引用与报告之间的因果关系。
- 删除双字段的不确定原因组合检查，不增加模型调用、修复机会或诊断规则矩阵。模型提示修订为 v10，旧提示资格不继承；不迁移历史候选报告，不改变最终决策 schema、工具预算、追问、登记或入口暂停状态。

局部替代 [ADR-0050](0050-use-a-bounded-agent-for-user-bug-assessment.md) 第 10 项中要求模型独立填写 proposed verdict 的规定；其余职责边界不变。初检交接仍遵循 [ADR-0144](0144-resume-bug-investigation-from-public-precheck.md)。

## 后续兼容扩展（2026-09-14）

用户认可后增加 `behavior_matches_contract → not_bug`，表达证据足以解释的正常行为；此原因允许责任候选为空，避免猜测缺陷责任方。继续由原因派生唯一 verdict，未增加模型调用、双重结论或范围字段。对应固定回复限定为本次报告行为，当前规则见[受理流程](../architecture/flows/support-intake-routing.md)。原七种原因的语义和历史数据继续保留。
