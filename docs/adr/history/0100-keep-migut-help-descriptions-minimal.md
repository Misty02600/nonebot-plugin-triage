# ADR-0100：保持 Migut Help 描述为最小功能说明

| 状态 | 决策日期 |
|---|---|
| 已采纳 | 2026-08-21 |

## 背景

核心教学 entry 同时保存 summary、behavior boundaries 和 requirements，供 Answer LLM 按问题选择事实。
Migut Help 的 description 是紧凑帮助展示，不适合继续承载角色、场景、授权范围、行为边界和详细限流；
把这些字段全部拼进 description 会使帮助图冗长，并让同一事实同时由核心字段和 summary 承担。

## 决定

1. Migut Help 的 description 只投影核心 entry 的 summary。
2. summary 继续允许在一句话内同时说明功能，以及仅凭 usage 难以理解的重要参数含义；它不承担权限、场景、
   授权范围、行为边界或具体限流说明。
3. Migut Help 原生能够表达的标准角色和冷却标记继续分别投影为 `permission` 与 `has_cd`；`role=custom`、
   scene、access、behavior boundaries 和限流文字不再追加到 description。
4. 完整公开事实继续进入 Answer knowledge，由 Answer LLM 根据问题组织回答；Help 的有损展示不改变核心教学合同。

## 没有采用的方案

- 不把所有未投影字段统一拼进 description。
- 不为复合权限增加 Migut Help 专属文本协议或插件特例。
- 不删除核心 requirements 或 behavior boundaries；它们仍由 Answer 和后续消费者使用。

## 影响

- 帮助描述保持短小，主要回答“这是什么、参数是什么意思”；
- 自定义角色、授权范围和具体限流不会出现在帮助图，但 Answer 仍能解释；
- 标准 `admin / superuser` 和是否存在冷却仍可由 Migut Help 原生字段展示。

## 替代关系

- 替代 [ADR-0094](../0094-simplify-the-public-capability-teaching-contract.md) 第 8 项中把 access 统一追加为
  “需授权”以及把其他非原生要求并入 Help 描述的展示方向；核心公开字段和 Answer 合同继续有效。

## 相关文档

- [可选帮助数据源与复用边界](../../architecture/help-source-adapters.md)
