# ADR-0079：用无编号的 triage 报错查询列出待处理问题

## 状态

| 状态 | 决策日期 |
|---|---|
| 已采纳；待处理 Problem 列表与 Alconna 子命令已实现 | 2026-08-16 |

## 背景

[ADR-0075](0075-register-problem-maintenance-under-triage-subcommand.md) 已决定使用
`triage 报错查询 <问题编号>` 查询或维护单个 Problem，但这要求主人事先知道编号。普通用户成功报告 Bug
后会收到编号，主人却还没有无需外部通知即可发现全部新问题的入口。当前也没有授权创建 GitHub Issue、发送
私信或调用其他外部通知系统。

## 决定

1. 增加无编号的 SUPERUSER 子命令 `triage 报错查询`，列出当前全部待处理 Problem。它与带编号的详情查询
   属于同一个真实 Alconna 子命令分支，不增加新的顶层 Matcher。
2. 首版“待处理”定义为当前 verdict 为 `bug` 且 lifecycle 不是 `resolved` 的 Problem，包括首次开放和已解决后
   再次出现的回归问题。人工改判为 `not_bug` 或已经解决的 Problem 不进入默认列表。
3. 每项只显示维护所需的稳定摘要：公开问题编号、标题或 subject、Report 数、Occurrence 数、是否已经人工
   复核、当前 lifecycle 和最近发生时间。默认不展开源码、日志、配置、Evidence ID、责任候选或 Agent
   trajectory；需要详情时再执行带编号查询。
4. 结果按最近发生时间倒序。没有待处理项时返回确定性空列表提示。条目过多时允许 Handler 拆成多条有界
   消息发送，但首版不额外增加页码、过滤器或排序参数。
5. 该命令在 Semantic assessment 之前模型外解析，每次重新检查当前 actor 是 SUPERUSER，沿用统一 triage
   冷却，不创建或续接 Support Thread，也不调用 Answer / Bug Agent。
6. 列表数据只能来自 ADR-0073 的权威 ORM Problem 查询。ORM 尚未接线或读取失败时返回确定性不可用提示，
   不得回退读取旧 `runtime-confirmed-bug-problems.json`、临时日志或模型生成的目录。
7. ADR-0078 已暂缓 `unknown` 持久化，因此首版列表没有 unknown 待判项。未来若重新引入该工作流，应由其
   successor 明确是否加入默认列表，不能只按空 verdict 混入。

## 理由

- 无编号查询解决主人不知道新 P-ID 的发现问题，不需要建立外部通知权限和失败重试系统；
- 列表只提供摘要，既能支持日常维护，也避免一次聊天输出泄露或塞入大量调查细节；
- 复用 `triage` 命令树、SUPERUSER 鉴权和 ORM 真值，避免形成第二套管理入口或列表缓存；
- 首版不做分页和过滤可以先验证真实问题量，再根据使用体验扩展。

## 带来的影响

- Problem Repository 需要提供按 verdict、lifecycle 和最近 occurrence 排序的只读列表查询；
- Alconna 命令树必须区分无参数列表、带 ID 详情和带 ID 动作，并保证无参数形式不会落入自然语言 request；
- 集成测试需要证明非 SUPERUSER 无法枚举、空列表和多条拆分可预测、命令零 Semantic 调用；
- README、插件元数据和 Migut Help 在命令接线时同步展示无编号列表形式。

## 没有采用的方案

### 启动时主动发送摘要

没有采用。启动消息需要选择收件人、处理重复启动和发送失败，也会引入当前未授权的外部副作用。

### 默认展示所有历史 Problem

没有采用。已解决和已改判非 Bug 的历史会掩盖当前需要处理的事项；它们仍可通过未来的显式过滤查询访问。

### 为列表单独注册顶层命令

没有采用。问题维护已经收口到 `triage`，独立顶层 Matcher 会重新制造命令和权限分叉。

## 与既有决定的关系

- 补充 [ADR-0075](0075-register-problem-maintenance-under-triage-subcommand.md) 的命令树，新增无编号列表形式；
- 使用 [ADR-0072](0072-use-opaque-problem-ids-and-minimal-maintainer-lifecycle.md) 的公开问题编号与维护摘要；
- 只读取 [ADR-0073](0073-use-nonebot-orm-for-authoritative-bug-workflow-state.md) 的权威 Problem 状态；
- 遵守 [ADR-0078](0078-defer-persisting-unknown-bug-assessments.md) 的 unknown 不落库边界。

## 相关文档

- [ADR-0075：把问题维护注册为 triage 子命令](0075-register-problem-maintenance-under-triage-subcommand.md)
- [ADR-0073：使用 NoneBot ORM 保存权威 Bug 工作流状态](0073-use-nonebot-orm-for-authoritative-bug-workflow-state.md)
