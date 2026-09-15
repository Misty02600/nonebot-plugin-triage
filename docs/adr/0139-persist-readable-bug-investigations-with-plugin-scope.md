# ADR-0139：保存可读的 Bug 调查结论并支持插件级建档

| 状态 | 决策日期 |
|---|---|
| 已采纳；由用户认可后实施，正式 Bug 调查入口继续暂停 | 2026-09-13 |

## 问题

现有问题标题来自教学摘要，长期记录只有判断、版本和证据引用，维护者难以理解具体故障。新调查入口按插件范围取证，但旧建档要求唯一能力，导致已接受的 Bug 可能无法登记。同一开放问题的后续调查也未必保留独立 Decision。

## 决定

- 沿用 Report / Occurrence / Problem / Decision 和 ORM 事务，不新增工单服务。只有被 reconciler 接受的 `bug` 建档；`unknown`、`not_bug` 不新建问题。
- 调查模型在同一次最终输出中提供 `report.title` 和 `report.summary`。标题说明具体故障；摘要用自然语言说明现象、与预期的差异、证据支持的判断依据和影响结论的确认范围。不要求根因或修复建议，不保存自由思考过程或完整证据正文。
- 新模型输出中的 Bug 必须有有效报告，使用原有的一次结构化输出修复机会，不增加总结调用或专门的文案修复循环。历史 Candidate / Decision 允许缺少 report；历史数据库摘要为空，不回填。缺少建档内容不等于非 Bug，也不能声称已经记录。
- `affected_plugin_refs` 表示调查确认的受影响范围，不照抄前序候选，不等同于责任方。程序只接受本次范围内的引用并转换为已有 owner 标识。可选 `capability_id` 只有确认具体能力时填写，须属于唯一受影响插件；多插件问题或未定位子命令时保留插件级范围。
- 现有 `subject_id` 保存明确能力 ID，或由排序后的已确认 owner 集合计算的 `plugins:` 范围身份；Problem / Occurrence 增加可读的 owner 集合。范围身份只参与既有技术签名，不凭相同插件、标题或摘要自动合并。相同签名聚合与旧 verdict 的版本适用性仍是两个问题。
- 首次建档采用报告标题，后续自动关联不改标题。每次新的、被接受的调查追加保存 Decision 和摘要；同一 report 重投仍幂等。同一 occurrence 的重新报告不增加发生次数。
- 人工复核仍是当前有效判断时，重复调查留档而不覆盖人工投影；新 occurrence 触发既有回归规则。维护详情分别显示当前判断来源和最近一次 Agent 调查摘要，不把后者当成人工确认或改判理由。Decision 的 `previous_decision_id` 记录该次判断基于的有效决定，可能与最近一次调查不同。
- 入库前对标题和摘要使用现有秘密脱敏。普通用户继续收到模型外固定编号回执，只有事务提交成功才说“已记录”。完整证据保留和恢复不由摘要代替。

## 实施边界

迁移 `f82c4a7d193b` 为历史行保留空 owner 集合与空摘要，部署更新仍使用 `nb orm upgrade`；不对宿主数据库自动执行升级。本轮不增加语义去重、自动合并拆分、报告生成 Agent 或自动修复建议。

摘要是否忠实于证据仍需要模型评测，字段与引用校验不能证明文字中的因果关系。模型提示修订为 v17，不继承旧提示的已验证资格。

补充 [ADR-0070](0070-separate-bug-reports-occurrences-and-problems.md)、[ADR-0071](0071-group-bug-problems-with-versioned-evidence-fingerprints.md)、[ADR-0073](0073-use-nonebot-orm-for-authoritative-bug-workflow-state.md) 和 [ADR-0074](0074-preserve-append-only-problem-decisions.md) 的可读结论与判断历史；接续 [ADR-0144](0144-resume-bug-investigation-from-public-precheck.md) 的插件范围调查。现有数据库与普通用户权限边界继续有效。

## 后续边界修订

[ADR-0140](0140-use-explicit-failure-fingerprints-and-reversible-grouping.md) 局部替代旧首版聚合身份与签名来源，明确完整现场的显式指纹、独立记录及可审计拆分；本文保留原决定。
