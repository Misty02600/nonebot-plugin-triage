# ADR-0083：先解释未知教学门禁，再决定是否关闭公开知识

- 状态：已采纳
- 决策日期：2026-08-16
- 替代范围：替代 [ADR-0081](0081-close-unknown-teaching-gates-and-freeze-parser-owned-usages.md)
  第 2 节中“宿主发现未知门禁后不调用模型并直接关闭”的实施边界；其 parser canonical usage 与有限枚举决定
  继续有效。

## 背景

ADR-0081 要求未知权限或限流不能被解释成“不存在”，这个安全目标继续有效。但开发回归曾把
`blocking_unknown_*` Evidence 在模型调用前直接关闭。它混淆了两件事：AST 能确定某段表达式位于
`permission=`、`rule=` 或其他执行控制位置，却未必能确定它真的限制用户。第三方包装器可能始终放行、只记录
指标，或在当前运行配置下关闭限制。直接关闭会跳过原本已经提供的 Jedi、源码读取、版本文档和运行配置补证
能力。

## 决策

### 1. 静态层只登记 gate candidate

ast-grep 只在结构位置足够明确时登记 `gate candidate`，记录候选种类、受影响 entry 和结构 Evidence。候选
本身不是公开约束，也不能单独证明权限或限流存在。已由 Runtime 或稳定框架语义确定的限制不重复登记为未知
候选。

### 2. 模型必须逐项形成内部 resolution

教学 Agent 对每个候选返回且只返回一个内部 resolution：

- `constraint`：实际存在会影响公开使用方式的限制；相应公开 constraint 必须关联该 candidate；
- `no_constraint`：定义、框架事实或当前运行配置明确证明它不限制当前使用；不生成公开的“没有限制”说明；
- `unresolved`：有界补证后仍无法确认。

每个 resolution 必须引用候选自己的结构 Evidence。`constraint` 与 `no_constraint` 还必须引用候选之外的函数
定义、框架事实、运行配置或等价的实际语义 Evidence；只重复候选名称或源码位置不能把它升级为已解释事实。

### 3. 模型外闭合，而不是模型外猜语义

模型外校验器负责检查 candidate / resolution 一一对应、Evidence 与配置引用闭合，以及 `constraint` 与公开
entry 的关联。它不根据函数名猜测 limiter，也不自行决定第三方代码语义。

只要任一 candidate 仍为 `unresolved`，`knowledge_enabled` 就必须为 `false`。Runtime 注册、披露、平台、
已确认 Permission / Rule、parser 语法和源码 revision 仍由模型外拥有，模型不能用 resolution 绕过或放宽。

## 实现与验证

- `CapabilityAnalysisRequest` 增加内部 `gate_candidates`，并纳入模型 payload 与分析 fingerprint；
- `CapabilityAnalysisOutput` 增加内部 `gate_resolutions`，公开 constraint 只增加不进入投影的 candidate 关联；
- `CapabilityAnalysisService` 删除 `blocking_unknown_*` 的模型前短路，并在返回前校验 resolution 与动态
  Evidence 闭合；
- Matcher 源码适配器只为未被稳定语义解析的 `permission=` / `rule=` 结构生成候选，`to_me()` 与已解析的
  Uninfo 权限继续走确定性投影；
- 开发回归保留“最终无法解释则关闭”的案例，同时新增“定义证明始终放行则保留知识”的合同测试。

## 后果

- 未知安全门禁仍不会被静默当成“不存在”，但可以利用现有只读源码工具消除可解释的误报；
- 普通帮助和 Answer 看不到 `gate candidate`、`resolution`、Evidence ID 或“没有限制”这类内部结论；
- 当前静态候选只覆盖可靠的 Matcher 注册控制位。候选不是 Agent 发现约束的前置条件：handler 或 helper
  内的第三方权限、限流和执行条件仍由 Agent 阅读已批准源码并在必要时导航到完整定义后判断；只有把新的
  结构位置升级为强制闭合的 candidate 时，才需要先证明静态定位精度；
- Prompt、generation contract 与开发回归 revision 随本决定升版；正式 Provider 资格仍需新的未见
  forward-heldout，不能继承既有结果。

## 相关决定

- [ADR-0058：用确定性证据与有界源码导航生成教学注释](0058-use-deterministic-evidence-and-bounded-navigation-for-teaching-annotations.md)
- [ADR-0059：跨 Agent 流程共享只读证据访问](0059-share-read-only-evidence-access-across-agent-flows.md)
- [ADR-0081：未知安全门禁关闭公开教学，并冻结 parser 拥有的用法](0081-close-unknown-teaching-gates-and-freeze-parser-owned-usages.md)
- [PLAN-0017：收敛多条目教学注释的生成与评测合同](../plans/done/PLAN-0017-qualify-multi-entry-capability-teaching.md)
