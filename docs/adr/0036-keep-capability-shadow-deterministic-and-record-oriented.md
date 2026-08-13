# ADR-0036：保持能力影子确定且以记录为单位

| 状态 | 决策日期 |
|---|---|
| 已采纳 | 2026-08-13 |

## 决策

能力影子继续以运行时 Matcher / Alconna 观察生成独立 `CapabilityRecord`，保留 schema v2 的
`disclosure`、`platform_scope`、`analysis_issues`、`constraints` 与模型前 ServingView 门禁。

删除两类尚无实际消费者、但显著增加维护成本的推断层：

- 不再扫描 handler AST 来推断状态读写、Matcher 角色或 `supporting.matchers` 关系；动态入口继续用
  `dynamic_entry` 失败关闭，确定的 keyword / regex 入口仍保留 `trigger.factory` 和 `trigger.entries`。
- 不再构建逐记录 module source manifest 与 deployment alignment。普通查询仍要求本轮 snapshot 完整且
  新鲜，并要求 deployment inventory 成功且完整；受众、平台、issue 和记录状态仍在召回与 `limit` 前过滤。

制品 revision、版本、VCS commit 和有界文件摘要仍用于部署清单与诊断，但不再被提升为逐能力源码身份合同。

## 原因与影响

当前产品需要的是安全、可解释的帮助候选，不是源码级供应链证明或一般 Matcher 图分析。保留全局刷新
完整性与 ServingView 可以继续阻止 stale、partial、restricted、平台不匹配和未解决候选进入普通回答；删除
精细推断层则避免把静态分析启发式误当成能力事实，也减少 wheel `RECORD`、源码布局与运行对象之间的脆弱
耦合。

如果未来出现明确消费者和评测数据，可重新提出独立 ADR；不能从本决定推导为自动恢复旧实现。

## 替代关系

- 替代 [ADR-0034](0034-distinguish-matchers-from-user-observable-capabilities.md) 的 Matcher 角色归并实现；
- 收窄 [ADR-0026](0026-filter-capability-knowledge-before-retrieval.md) 与
  [ADR-0032](0032-separate-capability-audience-analysis-and-platform-status.md) 的逐能力源码对齐要求；
- 不改变上述 ADR 的受众隔离、平台隔离、分析问题和模型前过滤边界。

## 相关文档

- [部署本地能力影子索引](../architecture/flows/capability-shadow-index.md)
