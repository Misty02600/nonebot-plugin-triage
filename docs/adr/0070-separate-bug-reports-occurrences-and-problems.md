# ADR-0070：分离 Bug Report、Occurrence 与 Problem

## 状态

| 状态 | 决策日期 |
|---|---|
| 已采纳；Bug 的 Report / Occurrence / Problem 领域与 ORM 纵切已实现，unknown 持久化由 ADR-0078 暂缓 | 2026-08-15 |

## 背景

[ADR-0068](0068-treat-qualified-agent-bug-verdicts-as-operational-decisions.md) 已决定：合格 Agent 的 `bug`
是正式产品 verdict，可以自动建立部署本地问题；经过实际调查的深度 `unknown` 也可以建立维护者待判记录。
现有 `ConfirmedBugProblem` 原型只保留一个 fingerprint、首次 / 最后时间、`occurrence_count` 和最新 decision，
无法区分：

- 用户重复提交同一次失败；
- 多名用户分别复现同一问题；
- 多份报告引用同一条 runtime correlation；
- 一个长期问题跨多次独立发生；
- 人工改判后需要回查哪些具体发生曾被归入该问题。

如果每次用户提交都直接执行 `occurrence_count += 1`，报告次数会被误当成实际发生次数；如果只保存计数，未来
也无法重新检查错误聚合或拆分问题。

Sentry 的公开模型提供了适合本项目的参考形状：单次 Event 是具体观察，具有相同 fingerprint 的 Event 被聚合
为 Issue；Issue 再维护首次 / 最后发生、数量和生命周期状态。Triage 不复制 Sentry schema，而是采用相同的
“具体事件与长期问题分离”原则，并额外保留用户 Report，因为一次用户提交不一定等于一次新发生。

参考：

- [Sentry：Enriching Events 与 Event Fingerprinting](https://docs.sentry.io/platforms/javascript/guides/tanstackstart-react/enriching-events)
- [Sentry：Issue Details](https://docs.sentry.io/product/issues/issue-details/)
- [Sentry：Issue Status](https://docs.sentry.io/product/issues/states-triage/)

## 决定

### 三层领域对象

1. 持久化模型分为三个语义层：
   - `BugReport`：一次用户向 `triage` 提交的报告；
   - `BugOccurrence`：一次具体、独立的异常行为观察；
   - `BugProblem`：一个可以包含多个 occurrence 的长期缺陷或待判问题。
2. 一个 Report 最多归属一个 Occurrence；多个 Report 可以指向同一个 Occurrence。一个 Occurrence 最多归属一个
   Problem；一个 Problem 可以包含多个 Occurrence。需要人工纠正错误归组时，必须保留可追溯的重新关联能力，
   不能只修改总计数。
3. `report_count` 与 `occurrence_count` 分开，并由关联记录派生，不作为可以任意递增的独立权威字段：
   - 同一操作失败后，用户重复描述两次，是两个 Report、一个 Occurrence；
   - 不同用户分别执行并失败，是多个 Report、多个 Occurrence；
   - 多名用户引用同一条运行失败，是多个 Report、一个 Occurrence。

### 最小持久字段

4. `BugReport` 是薄 receipt，只保留建立幂等与关联所需的最小字段：schema / report ID、接收时间、可用时的
   入站幂等摘要、报障者作用域 HMAC、终局类型，以及关联的 occurrence / problem ID。它不复制聊天正文、
   Reply 正文、Agent trajectory、源码、日志或配置。
5. `BugOccurrence` 只保存本次具体观察的最小可复核身份：schema / occurrence ID、观察时间、subject、adapter、
   观察来源种类、可用时的 operation / correlation / failure signature 摘要、实际参与判定的 source / contract /
   deployment revision，以及 Evidence receipt 引用。不存在的平台字段必须显式缺失，不得为了凑完整结构进行
   模糊聊天搜索、伪造 correlation 或额外依赖平台 API。
6. 同一 adapter 消息重投、同一 correlation 或同一 operation anchor 能模型外证明为同一次观察时，必须幂等
   复用 Occurrence；没有稳定锚点时宁可建立新的未聚合 Occurrence，也不能仅凭自然语言相似度合并发生次数。
7. `BugProblem` 只保存长期聚合状态：schema / problem ID、后续决定的问题身份、当前 verdict、decision source、
   review status、lifecycle status、subject / responsibility、首次 / 最后观察时间、当前适用性，以及关联
   occurrence。具体源码、日志、聊天、模型消息和 Evidence 正文不复制进 Problem。
8. Report 与 Occurrence 的正文证据仍由现有有界运行 / 日志 / 会话 /源码 Evidence 系统按其生命周期管理；长期
   记录只保留 receipt、摘要、revision 和完整性信息。Evidence 已过期并不删除历史 occurrence，但会影响它能否
   用于未来自动短路或重新裁决。

### 创建边界

9. 只有 ADR-0068 允许持久化的终局才创建三层记录：合格 Agent `bug`，或已经完成实际调查的深度 `unknown`。
   subject / observation 不足、无效补充、Agent 不可用和未执行实际调查的失败继续零持久化。
10. 首次符合条件的终局至少创建一个 Report 和一个 Occurrence；它们是否新建 Problem，或者链接到现有
    Problem，由后续问题身份与版本适用性决定。该决定形成前不得使用当前 request 文本摘要作为 Problem
    聚合键。

## 理由

- Report 表达“有人提交了什么处理请求”，Occurrence 表达“实际发生了几次”，Problem 表达“背后是不是同一
  个长期缺陷”；三者的计数和生命周期不同，不能合并；
- 薄 Report 能提供幂等和审计，而不把聊天存档复制成新的持久化系统；
- 独立 Occurrence 保留当时实际使用的版本与 Evidence receipt，使错误聚合可以复核、拆分和重新关联；
- 字段限定为当前入口、运行观察和 Evidence 系统已经能够廉价提供的信息；缺失字段是合法状态，不为完善记录
  扩大平台读取、模型工具或依赖；
- Problem 不保存每次调查正文，避免结构膨胀，并让长期问题状态独立于短期 Evidence 生命周期。

## 带来的影响

- 现有 `ConfirmedBugProblem.occurrence_count` 不能继续作为唯一发生事实，需要迁移为 Problem 与独立
  Occurrence 的关联派生结果；
- `runtime-confirmed-bug-problems.json` 原型不能直接成为最终 schema；迁移前仍保持未接线；
- 重复报告不会自动增加 occurrence，重复 occurrence 也不会复制 Problem；
- 后续必须单独决定 Problem identity、版本变化、回归、自动合并 / 拆分以及人工 override 对历史关联的影响；
- 首版可以继续使用 LocalStore data 和单进程单写者原子替换，不因为三层逻辑立即引入 ORM。只有出现多进程
  并发写、复杂查询或跨记录事务需求时，才按 ADR-0054 / ADR-0023 重新评审存储实现。

## 没有采用的方案

### 每次 Report 都等于一次 Occurrence

没有采用。消息重试、重复描述和多人引用同一次失败都会夸大发生次数。

### Problem 只保存 occurrence_count

没有采用。无法审计计数来源，也无法在误聚合后拆分或重新关联具体发生。

### 在长期记录中复制聊天、日志和源码正文

没有采用。这会形成第二套无界证据存储，增加隐私、容量、失效和迁移负担；长期层只保存 receipt 与 revision。

## 与既有决定的关系

- [ADR-0078](0078-defer-persisting-unknown-bug-assessments.md) 暂缓深度 `unknown` 建立三层记录；确定
  `bug` 的 Report / Occurrence / Problem 分层继续有效；

- [ADR-0073](0073-use-nonebot-orm-for-authoritative-bug-workflow-state.md) 已决定用 ORM 事务保存三层记录；本 ADR
  原先允许继续使用文件型单写者的实施选项不再适用；
- 补充 [ADR-0068](0068-treat-qualified-agent-bug-verdicts-as-operational-decisions.md) 的自动 Bug / 深度 unknown
  持久化语义；
- 补充 [ADR-0054](0054-store-reviewed-bug-problems-in-localstore.md) 的 LocalStore data 所有权；其单写者文件限制和
  ORM 重评门槛已经由 ADR-0073 接续，损坏时不得错误短路的语义继续有效；
- 采用 Sentry Event → Issue 的工程形状作为参考，不继承其服务、字段、云端存储或默认 grouping 算法；
- [ADR-0071](0071-group-bug-problems-with-versioned-evidence-fingerprints.md) 进一步用版本化 Evidence 指纹定义
  Problem identity、跨版本回归与精确聚合，并禁止把 request 文本摘要当成 Problem key。

## 相关文档

- [ADR-0068：把合格 Agent 的 Bug verdict 作为正式判断并由人工事后监督](0068-treat-qualified-agent-bug-verdicts-as-operational-decisions.md)
- [支持入口、Thread、Guidance 与 Bug 判定](../architecture/flows/support-intake-routing.md)
