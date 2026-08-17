# ADR-0080：把一次能力分析投影为多个公开教学条目

- 状态：已采纳；领域与投影纵切已实现，v34 / v8 Gate 已冻结通过
- 决策日期：2026-08-16

## 背景

旧教学合同把一个 `CapabilityAnalysisRequest` 压成一条注释，并让模型用 `{command}` 代替确定性命令正文。
这同时产生了三个问题：

1. Alconna 的不同子命令会被合并成一个展示条目，即使它们本来就是不同功能；
2. `{command}` 既增加模型理解和校验负担，也无法表达参数化 Matcher 工厂完整的业务前缀与聚合入口；
3. `interaction` 把后续对话过程提升成固定展示 schema，但 Migut Help 并不需要这个字段，普通 Answer 也只需要
   高层、公开的使用说明。

另一方面，同一功能的 Option、别名、回复输入和参数省略形式仍然应保留在一个条目中。参数化 Matcher 工厂也
不应展开成几百次相同模型调用；它只在存在可靠共同语义时贡献一个聚合条目。

## 决策

### 1. 分析单元与展示条目分离

一次能力或工厂分析仍是一个 `CapabilityAnalysisRequest`，但请求携带一组由模型外确定的 `invocations`，模型
必须返回 entry ID 完全相同的一组 `entries`。模型不能新增、删除、合并或拆分 entry；无法为整个分析单元形成
可靠公开知识时，只能返回 `knowledge_enabled=false` 与空 entries。

普通命令通常只有 `root` entry。确定性 Alconna 子命令各自成为独立 entry。同一子命令或普通命令的参数格式、
Option、别名与回复输入只是该 entry 的多条 `usages`，不因为语义看起来不同而让代码擅自拆成功能。

### 2. 删除 `{command}` 输出合同

模型直接输出完整的命令正文与参数格式，不再返回 `{command}`：

- `anchored` invocation 由 runtime / parser 提供确定性的 `command_body`。每条 usage 必须原样包含它一次，且不能
  添加 NoneBot 全局 `COMMAND_START`；
- `complete` invocation 只用于参数化工厂等无法预先给出单一正文、但允许模型依据完整 Evidence 生成一个聚合
  入口的情况。它只能返回一条 usage；证据不足时关闭知识；
- 插件业务前缀属于命令正文，NoneBot 全局 start 不属于模型所有；后者继续留给部署与最终展示适配层处理。

模型外投影拒绝残留 `{command}`、未知花括号、同一 anchored 正文重复出现、把后续对话写进 usage，以及不平衡
的 `[]`、`<>`、`()`。

### 3. 删除结构化 interaction

教学 schema 不再保存 `interaction.mode` 或步骤列表。确有帮助的后续交互只允许作为简短
`input_requirement` 或 Answer Markdown 高层说明；不得把多轮过程塞进 usage，也不得披露被动监听、内部学习、
缓存或实现机制。

### 4. 两个公开消费者共享 entry，保持不同投影

- Migut Help YAML 把每个启用 entry 投影为独立 command，保留该 entry 的有序 usages；
- Answer Markdown 按 entry 组织公开补充知识；
- 同一 capability / factory 的两类文件继续由同一个 generation 原子切换；
- 上一版公开 entry 只作为减少措辞漂移的非证据 baseline，当前 Evidence 仍是唯一事实来源。

全局消息、通知、请求和其他没有可靠公开调用形式的被动监听器仍不进入第一阶段教学。参数化工厂若无法形成
共同说明，也不展开成员或让模型自行分组。

## 实现

- 领域请求新增 `CapabilityInvocationTarget`，模型输出新增 `CapabilityAnalysisEntryOutput`；
- `CapabilityTeachingAnnotation` 改为持有 `CapabilityTeachingEntry` 序列，cache schema 升为 6；
- runtime adapter 从当前命令结构生成普通 entry，并把确定性的 Alconna 叶子子命令拆成不同 entry；
- Help YAML、Answer facts / Markdown、Bug 教学初检和持久化 baseline 均改为消费 entries；
- Prompt 改为中文 `capability-teaching-annotation-v4-prompt-v16-zh`，任务合同改为
  `capability-teaching-annotation-agent-v3`；
- 正式评测改用全新的 v3 24 条纯合成 forward-heldout，其中 12 条先运行真实 ast-grep 源码提取。

## 评测结果与资格

冻结 bundle `capability-teaching-v3-forward-heldout-24-20260816-a-v16-zh` 仅正式运行一次。结果为：

- schema、Evidence 闭合、公开投影、预算、工具用例和 12/12 源码提取均为 1.000；
- 安全合规率为 0.9167；语义合规率为 0.3333；8/24 用例通过；
- 73,423 input tokens、16,297 output tokens、7,970 microUSD；
- 资格身份检查全部通过，但质量 Gate 失败，因此没有产生新的 Provider 资格。

失败同时揭示了评测与生成两侧问题。多数 usage 失败来自首版 v3 正则把合理的 Option 展开、完整成员枚举或等价
占位写法判得过窄；真实生成缺口则包括 `to_me` 未稳定写出 `@bot`、可选参数偶尔写成必填、以及公开文字出现
`OWNER` / `MEMBER` 框架符号。冻结数据与报告不得因结果修改后重跑；后续修正必须使用新的 Prompt / 合同和
新的 forward-heldout。

### 后续落实确认（2026-08-16）

Prompt v34 把上一版注释继续限定为非证据基线，并要求当前 Evidence 仍支持时保留旧 entry 中非空的
`synonyms`、`supported_subjects`、`input_requirements` 与 `behavior_boundaries`。模型外评测同时保留逐字指标，
并新增不受规范排序影响的成员集合保留指标；前者衡量编辑漂移，后者作为检索字段不得无故丢失的硬合同。

全新 v8 forward-heldout 已冻结并只正式运行一次：20 条案例、12 条源码案例，schema、Evidence 闭合、公开
投影、安全、预算、工具和源码提取均为 1.000，语义为 0.950；25 次请求、111,811 input token、11,318
output token、6,398 microUSD。1 条逐字基线案例与 2 条成员保留案例均为 1.000。唯一语义失败来自冻结 Gold
额外要求模型声明旧行为已不存在；模型实际已正确删除失效图片语义并生成新用法，因此不修改 fixture 后重跑。
当前运行时资格精确绑定 v34 / v8，v33 / v7 与更早报告继续作为冻结历史证据。

## 后果

- 领域、cache 与输出文件格式是有意的破坏性升级，旧单条注释不会被当作 schema 6 当前结果读取；
- 当前实现可以继续受控观察，但不能宣称稳定生产可用，也不能继承 semantic、Bug 或 Answer 的模型资格；
- 后续可以改进 alias / 多个 accepted command body、`to_me` 的确定性投影和公开框架符号拒绝，但这些变更必须
  更新 generation contract，并在新的 held-out 上重新资格；
- 若未来决定由代码按业务语义拆 Option、重新引入结构化多轮交互、允许被动监听进入普通公开知识，或让模型
  自行决定 entry 数量，需要 successor ADR。

## 相关决定

- [ADR-0058：用确定性证据与有界源码导航生成教学注释](0058-use-deterministic-evidence-and-bounded-navigation-for-teaching-annotations.md)
- [ADR-0062：结构化能力教学的用法、约束与交互](0062-structure-capability-teaching-usages-requirements-and-interactions.md)
- [ADR-0069：分离帮助展示与 Answer 知识，并收窄静态分析职责](0069-separate-help-display-from-answer-knowledge-and-bound-static-analysis.md)
- [ADR-0077：把上一版机器生成教学内容作为非证据基线](0077-use-previous-generated-teaching-content-as-a-non-evidentiary-baseline.md)
