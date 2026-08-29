# 从哪里开始看这个项目

## 先记住

- NoneBot Triage Agent 把自然语言求助转换为受证据、权限、隐私和预算约束的支持流程；模型只负责受限语义
  判断或生成候选，领域 runtime 保留路由、授权、状态和副作用控制权。
- `nbtriage` 保存传输无关的领域能力，`nonebot_plugin_triage` 负责 NoneBot / UniSeg / Adapter 入口与运行
  适配；QQ、OneBot 和 NoneBot 传输类型不进入领域核心。
- 当前用户入口是显式 `triage`。能力教学、行为探索和 Bug assessment 共享只读 Evidence 边界，但拥有
  独立任务合同、资格结论和状态生命周期。
- 运行观察、短期 Thread、可重建能力索引、长期 Bug 工作流、知识包和 Behavior checkpoint 分别使用不同
  状态层；不要因为都位于本地就把它们视为同一持久化语义。
- architecture 描述系统现在如何工作；[当前 ADR](../adr/README.md)解释仍在约束架构的主要取舍；
  [ADR 历史](../adr/history/README.md)只用于追溯旧方案、局部实现理由和被替代决定。

## 按问题查找

| 想知道什么 | 看哪里 |
|---|---|
| 系统边界、公开入口、逻辑组件、状态和安全不变量 | [架构概览](overview.md) |
| 当前模型组合具有什么质量证据，哪些结论不能继承 | [模型 Provider 支持矩阵](model-provider-support.md) |
| 为什么帮助插件、Runtime Matcher 和模型都不能单独成为能力真值 | [帮助数据源与复用边界](help-source-adapters.md) |
| 产品与相邻能力的边界 | [产品定位](product-positioning.md) |
| `triage` 如何鉴权、分流并进入 Guidance、Behavior 或 Bug 流程 | [支持入口分流](flows/support-intake-routing.md) |
| 部署本地能力证据如何生成、分析、缓存和公开 | [能力影子索引](flows/capability-shadow-index.md) |
| 跨平台 Reply、运行证据与短期 Thread 如何关联 | [跨平台支持入口](flows/cross-platform-report-intake.md) |
| OneBot V11 如何把入站和出站消息引用绑定到运行证据 | [OneBot V11 引用关联](flows/onebot-v11-reply-reference-correlation.md) |
| NoneBot 运行观察允许保存哪些字段 | [运行观察入口](flows/runtime-observation-intake.md) |
| Alconna 命令结构、能力快照和解析回执如何分工 | [Alconna 能力与解析回执](flows/alconna-capability-and-parse-receipts.md) |
| 离线 Agent 单步为何不能直接执行动作 | [有界 Agent 单步](flows/bounded-agent-step.md) |
| B1 支持会话如何审批补证与 Oracle 结果 | [可审计支持会话](flows/support-session.md) |
| 当前不可达但仍保留兼容合同的 Incident / trial 流程 | [短期 Incident 聚类](flows/incident-clustering.md)、[观察型生产 trial](flows/observation-first-trials.md) |

## 阅读决定

先从 [当前架构决定](../adr/README.md) 按领域找到仍有效的边界。只有需要理解旧方案为什么被放弃、某项局部
实现为什么曾经这样选择，或追溯替代关系时，才进入 [ADR 历史记录](../adr/history/README.md)。历史文件
保留原编号和当时表述，但不自动代表当前实现。
