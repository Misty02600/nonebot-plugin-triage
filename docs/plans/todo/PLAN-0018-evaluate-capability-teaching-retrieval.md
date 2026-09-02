# PLAN-0018：评测教学注释检索质量

| 状态 | 最后更新 |
|---|---|
| 讨论中 | 2026-08-23 |

## 背景

公开能力查询同时使用 Runtime 能力索引和模型生成的教学注释。当前已确定先采用可解释的词法排序：command
与 alias 精确命中最高，注释 name、独立 search term 依次降低，summary 只作低权重补充。项目作者要求暂不
为此单独运行评测；本计划保留后续用真实用户问法验证检索质量的工作。

## 当前设计与缺陷

- `src/nbtriage/capability/catalog/records.py::search_capability_index` 使用 SQLite FTS5 trigram 和标准化 lookup term 检索
  Runtime `CapabilityRecord`。
- `src/nonebot_plugin_triage/capability/shadow.py::_augment_hits_with_annotation_terms` 将公开注释 name、独立
  search term 和 summary 加入同一个候选排序，并在 limit 前按 annotation capability ID 收敛 family。
- `src/nbtriage/capability/teaching/annotations.py::validate_capability_search_term` 要求每项是一条独立短语，拒绝用顿号、
  逗号、分号或 `|` 把多个查询词拼成一项。
- 当前权重是明确的首版启发式，还没有用真实问法测量 top-1/top-3、跨插件歧义、family 精确成员召回或
  summary 偶然命中。现阶段没有证据支持引入 embedding 或向量数据库。

## 技术路线

1. 从已验证真实插件中策展一小组脱敏问法，覆盖精确命令、alias、自然语言能力名、支持对象、family 成员、
   跨插件相似词和无匹配查询；每条标注允许的 teaching unit 与不得召回的受限能力。
2. 固定当前 Runtime snapshot、教学 annotation generation 和检索权重，报告 Recall@1、Recall@3、MRR、无答案
   精度以及 family 折叠后的候选数；不同来源的分数分开记录。
3. 先比较词法权重、候选合并和 query normalization 的小幅调整。只有词法方案在稳定语义改写上持续失败，
   才另行讨论 embedding、混合检索和相应的隐私、版本、索引与成本边界。

## 非目标

- 当前不运行付费模型、不建立向量索引，也不把搜索日志或用户原文直接写入评测集。
- 不用检索分数绕过 public / restricted、adapter、partial、stale 或 teaching knowledge 门禁。

## 完成标准与验证

| 验收项 | 输入 | 预期结果 | 验证方式 |
|---|---|---|---|
| 评测集 | 脱敏真实问法与固定 generation | 每条问法有允许目标、拒绝目标和来源说明 | dataset schema 与人工复核 |
| 词法基线 | command、alias、name、search term、summary 和 family 问法 | 权重顺序、family 收敛和 limit 行为与当前合同一致 | 确定性 pytest runner |
| 质量报告 | 至少包含精确、自然语言、歧义和无答案类别 | 分类别报告 Recall@1/3、MRR、精度与失败案例 | 本地 `reports/` 工件 |
| 后续决定 | 词法失败案例与候选优化结果 | 有足够证据决定保持词法、继续调权或提出混合检索 ADR | 复核报告与实际错误样本 |

## 相关文档

- [能力影子索引流程](../../architecture/flows/capability-shadow-index.md)
- [ADR-0026：在检索与模型前隔离能力知识受众域](../../adr/0026-filter-capability-knowledge-before-retrieval.md)
