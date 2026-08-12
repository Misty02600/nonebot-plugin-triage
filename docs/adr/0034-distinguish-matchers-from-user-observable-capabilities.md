# ADR-0034：区分 Matcher 事实与用户可观察能力

| 状态 | 决策日期 |
|---|---|
| 已采纳 | 2026-08-13 |

## 当时遇到了什么

部署能力影子的第一版采集单位接近 NoneBot 的 `Matcher`：注册对象有稳定来源、命令结构和插件归属，适合
证明“当前进程里观察到了什么”。但 Matcher 是框架执行结构，不是天然的产品能力身份。一项用户可观察能力
可能由命令入口、后续消息接收、会话推进和结果清理等多个 Matcher 协作完成；一个 Matcher 也可能为多个
可观察效果提供共同支撑。若把每个 Matcher 直接展示成一项能力，会制造重复、泄漏内部步骤，并把实现重构
误判为能力变化。

反过来，在采集时就凭名称或模型摘要合并 Matcher，也会丢掉源码位置、revision、Rule、Permission、handler
和运行注册等可核对事实。出现语义歧义时，维护者将无法判断错误来自采集、归并还是回答生成。

## 决策

1. 运行时 Plugin、Matcher、Alconna 命令、handler 绑定、Rule、Permission 与源码位置先作为带来源、revision
   和证据标识的构建期事实处理；采集阶段不因为它们看起来相似就合并。当前持久层只保存派生
   `CapabilityRecord` 及压缩后的支撑证据，独立事实表尚未实现。
2. `Capability` 表达用户可观察的效果与使用合同，由事实层派生，而不与 Matcher 保持一对一关系。Matcher 与
   Capability 的关系允许多对多；能力身份不能只使用 Matcher ID、文件路径或行号生成。
3. 只有证据支持相同的可观察效果、入口合同、受众与平台范围时，才把事实归入同一能力。命令别名、同一流程
   的后续输入接收、状态推进、结果采集或清理 Matcher 可以成为同一能力的证据，但不能仅凭同属一个插件或
   LLM 判断语义相近就合并。
4. 只承担后续接收、状态推进、清理、通知处理或内部协作的支撑 Matcher 不单独进入普通 ServingView。它仍
   保留为能力的来源或依赖证据。若同一 Matcher 也提供独立、可由用户观察或触发的效果，则可在证据支持下
   关联另一项能力，不能因“支撑”标签一律隐藏。
5. 无法确定某个事实应形成独立能力、归入哪项能力或仅承担支撑作用时，保留事实和候选关系，并记录具体的
   blocking issue `capability_mapping_unknown`。它与 `dynamic_entry` 可以并存：前者表示 Matcher 到能力的
   关系未知，后者表示用户触发合同未知。系统既不能猜测归并，也不能退化为“每个 Matcher 一项能力”。
6. LLM 只生成引用既有 Evidence ID 与 revision 的语义 Claim，例如候选可观察效果、语义边界、同义表达和
   候选归并关系。模型不能凭自身输出创造精确语法、决定 `public / restricted`、决定平台范围、删除
   `analysis_issues`，或把缺少证据的归并直接升级为可服务能力。输出仍需结构校验、证据闭包复核与模型外
   策略门禁。
7. 普通用户检索和回答只消费派生后的 Capability；当前维护者视图可以查看派生记录与未解决 issue，构建期
   的支撑 Matcher 则压缩为 `supporting.matchers` Claim 和 Evidence。独立原始事实、映射关系与支撑角色的
   维护者视图尚未实现。原插件的 Matcher、Rule、Permission 和 handler 仍负责最终执行资格。

## 为什么这样选

- 事实先行保留了可重建、可追责的来源，语义归并错误不会污染原始运行观察；
- 按用户可观察效果组织帮助，避免把实现细节或多阶段协作重复展示为多个功能；
- 多对多关系能覆盖命令、多轮输入、被动功能和共享 handler，而不强迫所有插件采用同一种注册形式；
- 明确 issue 比静默猜测或全量人工审批更容易自动消解、评测和定向排查；
- LLM 负责难以用 AST 表达的语义，但不能越过证据、披露和平台门禁。

## 没有采用的方案

- **每个 Matcher 固定生成一项 Capability**：实现简单，但会重复能力、暴露支撑步骤，并让源码重排破坏能力
  身份。
- **按插件归并全部 Matcher**：同一个插件通常包含多项互不相干的用户效果，粒度过粗。
- **让 LLM 直接生成最终能力目录**：缺少确定性 provenance，模型错误、证据冲突和 revision 变化无法局部
  定位。
- **丢弃看似内部的 Matcher**：以后无法解释能力为何需要某种输入、状态或约束，也无法复核隐藏判断是否正确。

## 带来的影响

- 快照、索引与评测需要区分事实召回、Matcher-to-Capability 映射和最终能力服务三个阶段；
- 能力级 Claim 必须能回溯到一个或多个事实 Evidence，支撑 Matcher 不增加普通用户目录项；
- `capability_mapping_unknown` 成为 ADR-0032 `analysis_issues` 的扩展原因，并阻止该候选关系进入普通
  ServingView；
- 应分别评测事实召回率、能力归并准确率、支撑 Matcher 误展示次数和 LLM Claim 的证据覆盖率；
- 第一阶段已在运行时快照中接入有界函数效果分析：以函数源码位置区分同名 handler；`message / passive`
  Matcher 的明确用户输出可消除 `dynamic_entry`；只有已证明属于当前 Matcher、Bot 或消息对象的输出 API
  才算用户输出，名字相似但接收者
  未知的调用保持未决；只有 `message / passive` Matcher 在效果覆盖完整、且与已确认用户能力共享状态资源时
  才会折叠为 `supporting.matchers` 证据，不再独立进入 ServingView；command / Alconna 不会因状态效果被折叠；
  其余动态 Matcher 增加
  `capability_mapping_unknown` 并失败关闭。该阶段不执行 handler、Rule 或 Permission。
- 完整独立事实表、跨 revision 稳定 Capability 身份、复杂跨模块调用图、LLM 候选关系和一般多对多查询图仍未
  实现；当前支撑关系随目标 `CapabilityRecord` 持久化，不能把 matcher candidate ID 当作长期审批身份。

## 替代关系

- 补充 [ADR-0021](0021-use-deployment-local-capability-shadow-index.md) 的能力影子构建流程：运行时观察先进入
  事实层，再派生用户可观察能力；
- 补充 [ADR-0025](0025-explain-plugin-behavior-from-deployment-evidence.md) 的语义分析边界：LLM 可以提出带
  证据的能力归并 Claim，但不能成为唯一事实来源；
- 扩展 [ADR-0032](0032-separate-capability-audience-analysis-and-platform-status.md) 的具体分析问题集合，不
  改变 `disclosure`、`PlatformScope`、`RecordState` 或 ServingView 的模型前门禁。

## 相关文档

- [部署本地能力影子索引](../architecture/flows/capability-shadow-index.md)
- [可选帮助数据源与复用边界](../architecture/help-source-adapters.md)
- [架构概览](../architecture/overview.md)
