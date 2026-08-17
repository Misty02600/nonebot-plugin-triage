# ADR-0078：在可记录性合同确定前不持久化 unknown 判断

## 状态

| 状态 | 决策日期 |
|---|---|
| 已采纳；unknown 固定终局与不落库边界已实现 | 2026-08-16 |

## 背景

[ADR-0068](0068-treat-qualified-agent-bug-verdicts-as-operational-decisions.md) 曾区分入口信息不足、分析服务
不可用和“完成实际调查后仍无法判断”的深度 `unknown`，并允许最后一类建立维护者待判记录。后续讨论发现，
当前领域结果还不能由模型外可靠证明一次调查已经达到可记录深度，也不能区分“缺少部署知识”与“已有充分
证据但仍存在多个合理解释”。

当前设计知识消费者也只使用已经接入且版本适用的知识。目标插件、Adapter、Alconna、Uninfo、NapCat 或其他
直接依赖的适用知识缺失时，不得让模型用相邻版本、自由记忆或源码猜测补齐预期合同。此类知识缺口可能令
Bug 无法判断，但并不自动形成一个具有稳定身份、值得长期维护的 Problem。

## 决定

1. 在新的可记录性合同被采纳前，任何 `unknown` 都不创建 Report、Occurrence、Problem、公开问题编号或
   ProblemDecision。该边界同时适用于：
   - 用户没有提供可定位的 subject、具体操作或观察；
   - 唯一一次补充仍无效；
   - Agent、Provider、平台上下文、源码导航或设计知识不可用；
   - Agent 已读取现有日志、源码或合同，但仍不能安全得到 `bug / not_bug`。
2. 当前只使用已经实际接入且能确定版本适用性的知识。缺少目标插件、Adapter、Alconna、Uninfo、NapCat 或
   其他依赖的关键知识时，结果收敛为 `unknown`；不得扩大到未经版本绑定的知识，也不得把“没有命中”解释成
   “不存在相关合同”。
3. 普通用户对 `unknown` 只收到模型外固定终局：`暂时无法判断是不是 Bug。`。不得包含“已记录”“已关联”、
   问题编号、内部缺证原因、源码、日志、配置或责任候选。
4. 合格 Agent 的 `bug` 仍是正式产品 verdict，并继续进入 ADR-0070～ADR-0074 定义的持久化事务；`not_bug`
   仍不创建 Problem。只有 `unknown` 的持久化被暂缓。
5. 缺少能由用户补充的信息时，仍可按作用域 Thread 合同询问一次。第二轮仍为 `unknown` 后关闭 Thread，且不
   因用户补充次数或工具调用次数自动把结果升级为可记录案件。
6. 未来若要恢复深度 `unknown` 记录，必须由新的 ADR 定义模型外可验证的 recordability 条件、维护者待处理
   生命周期、公开回执和噪声控制；不能仅凭 Agent 自报“已完成深入调查”。

## 理由

- `unknown` 描述的是结论缺失，不天然拥有可稳定聚合和处理的问题身份；
- 在知识范围尚不完整时自动记录，会把框架知识缺口、Provider 故障和真实疑难问题混入同一待处理列表；
- 先让确定的 `bug` 走通 Report / Occurrence / Problem 事务，可以验证实际维护体验，再决定深度 `unknown`
  是否值得建立独立工作流；
- 对外使用单一安全终局即可满足普通用户预期，内部仍可通过运行日志和评测统计观察 `unknown` 原因。

## 带来的影响

- ADR-0071、ADR-0072 中“深度 unknown 已记录”的固定回执暂不适用；
- ADR-0074 中为深度 `unknown` 建立首条 Decision、再人工确认 Bug / 非 Bug 的路径暂不实现；
- ORM 首个纵切只需要为确定的 `bug` 建立权威 Problem 工作流，不需要先设计 unknown 待审表；
- 当前知识范围不足会提高 `unknown` 比例，但不会形成错误的 `not_bug` 或噪声 Problem。

## 没有采用的方案

### 继续保存所有深度 unknown

没有采用。当前没有模型外 recordability 判据，无法保证列表中的每条记录都已经完成了足够调查。

### 缺少知识时让模型按常识补齐

没有采用。框架和插件行为依赖精确版本；模型记忆或相邻版本知识不能成为 Bug 预期合同。

### 因为暂不落库而把 unknown 改成 not_bug

没有采用。存储策略不能改变证据结论；缺少预期或实际证据时必须继续失败关闭为 `unknown`。

## 与既有决定的关系

- 部分替代 [ADR-0068](0068-treat-qualified-agent-bug-verdicts-as-operational-decisions.md) 的深度 `unknown`
  持久化，保留合格 Agent `bug` 的正式 verdict 与人工事后监督；
- 部分替代 [ADR-0070](0070-separate-bug-reports-occurrences-and-problems.md)、
  [ADR-0071](0071-group-bug-problems-with-versioned-evidence-fingerprints.md)、
  [ADR-0072](0072-use-opaque-problem-ids-and-minimal-maintainer-lifecycle.md) 和
  [ADR-0074](0074-preserve-append-only-problem-decisions.md) 中依赖深度 `unknown` 持久化的条款；
- 保留 [ADR-0051](0051-let-the-bug-assessment-agent-query-design-rag.md) 的版本化设计知识与缺证失败关闭边界。

## 相关文档

- [ADR-0051：允许 Bug Agent 查询设计 RAG](0051-let-the-bug-assessment-agent-query-design-rag.md)
- [ADR-0068：把合格 Agent 的 Bug verdict 作为正式判断](0068-treat-qualified-agent-bug-verdicts-as-operational-decisions.md)
