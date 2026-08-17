# ADR-0082：参数化 Matcher 只按 Runtime Handler 代码身份聚合

- 状态：已采纳；v4 正式 held-out 未通过，v26 开发回归已补齐已知机制
- 决策日期：2026-08-16
- 部分替代：[ADR-0081](0081-close-unknown-teaching-gates-and-freeze-parser-owned-usages.md) 中为参数化工厂
  构造成员数量、成员名和省略标记的决定
- 后续关系：[ADR-0083](0083-resolve-unknown-teaching-gates-before-closing-public-knowledge.md) 替代本 ADR
  实现记录中的 `blocking_unknown_*` 模型前短路；本 ADR 的 Runtime Handler 聚合边界不变。

## 背景

首个参数化 Matcher 纵切为了避免数百个 Runtime Matcher 重复分析同一闭包 Handler，使用 AST 从闭包函数
向上寻找直接外层函数，并把外层函数位置当作工厂身份。它还在成员不超过四个时把 Runtime command header
交给模型，更多时发送成员数量和“成员表已省略”。

这个实现没有解析插件专属对象，但仍然隐含了两个不能由 Python 语法保证的假设：直接外层函数就是一个语义
一致的 Matcher 工厂；同一外层函数中的闭包 Handler 可以合并。一个 setup 函数可以定义多个用途不同的闭包，
装饰器、包装器和返回 callable 也可能改变这种形状。继续补规则会重新形成项目专用且脆弱的工厂解析器，违背
[ADR-0069](0069-separate-help-display-from-answer-knowledge-and-bound-static-analysis.md) 已确定的“静态分析限定
证据范围，不替模型理解工厂语义”。

## 决策

1. 参数化 Matcher 只有在每条 Runtime 记录都绑定同一段闭包 Handler 代码时才允许聚合。代码身份由当前
   Runtime callable 的插件模块、`__qualname__`、`co_firstlineno`、源码 revision 共同构成；模型、AST 或
   command 文本相似度不能创建或合并该身份。
2. 一个 Matcher 同时绑定多个插件 Handler、缺少精确代码身份、源码 revision 不一致，或同组存在未通过
   public / restricted 准入的成员时，整个候选组失败关闭。第一版不尝试拆组、推断共同工厂或选择代表成员。
3. AST 只用精确代码身份在已批准源码中取出对应 Handler 函数正文。它不再向上寻找外层函数，也不把外层
   函数命名为工厂；需要更多上下文时，教学 Agent 通过已有只读源码工具在批准插件根内自行导航。
4. 初始 Evidence Pack 不再包含 `member_count`、`member_headers`、`member_headers_omitted` 或其他帮助模型
   理解工厂的摘要。模型根据 Handler、获准配置、Runtime 当前性和按需取得的源码判断是否有可靠共同语义；
   无法确认完整公开用法时输出 `knowledge_enabled=false`。
5. [ADR-0081](0081-close-unknown-teaching-gates-and-freeze-parser-owned-usages.md) 的通用展示规则继续有效：当前
   Evidence 明确给出的同一位置备选值不超过四个时可以枚举，更多时使用概念槽位。该阈值不再触发参数化
   Matcher 成员采集，也不能成为工厂聚合依据。
6. Runtime parser 已确认的 Alconna 参数、Option、子命令 canonical usage，以及未知权限、必要参数或限流
   关闭整个公开教学单元的决定不受影响。

## 实现与验证

- Runtime handler reference 新增 `qualname` 与 `code_firstlineno`，来源是本轮已经加载的 Python callable；
- `ParameterizedHandlerCodeIdentity` 替代外层工厂锚点，分析单元 ID 绑定精确 Handler 身份和源码 revision；
- 参数化分析的初始源码 Evidence 只包含精确 Handler 函数；同一外层函数中的两个不同闭包由测试证明不会
  被合并；
- 旧 `runtime_family_members` Evidence 与外层函数向上查找逻辑已删除；
- `complete` 聚合用法新增模型外结构门禁：圆括号只能列同一成员槽位的简短固定值，共同参数写在括号外；
  把带有各自参数的完整命令塞进一个 `(A|B)` 时触发重试，无法形成共同调用结构则关闭知识；
- anchored 用法新增冗余可选参数门禁：一条带 `[...]` 的用法已经包含省略形式，不得再同时输出短写法；
- Prompt / generation contract 升为 v21，v18 / v19 的四案例开发诊断只保留为历史结果，不能继承为当前
  合同资格；
- v21 的八条真实 Provider 开发诊断全部通过，其中四条从合成插件源码开始，覆盖普通命令、Uninfo 管理
  权限、三成员同构工厂和异构工厂关闭。schema、Evidence 闭合、公开投影、安全、语义、工具、预算与源码
  提取合规率均为 1.000，共 15 次请求、48,499 input token、8,187 output token、3,958 microUSD。该开发集
  仍没有正式 forward-heldout 身份和完整覆盖，只证明当前回归机制，不构成 Provider 资格。
- 冻结 v4 forward-heldout 已按一次性 Gate 运行并保留原始失败报告：20 条案例、22 次请求，schema、Evidence、
  公开投影、安全和预算合规率均为 0.850，语义合规率为 0.650，工具与源码提取合规率为 1.000；共
  69,466 input token、11,485 output token、5,505 microUSD。该结果不能由后续 Prompt 重跑覆盖，也没有赋予
  当前任务 Provider 资格。
- 开发回归随后扩为 12 条，其中 8 条直接包含合成插件源码，新增 alias、回复输入、精确 `to_me()`、业务前缀
  参数化聚合和多重限流。v26 Provider 诊断后，当前 Prompt / generation contract 已升为 v27 / v24；v27
  另补 Migut Help 的 `...` 多值记法与参数化 family 关闭率观测，但尚未运行 Provider Gate。本地合同新增
  以下模型外门禁：
  Runtime alias 与精确 `@bot` 要求进入 invocation；完整聚合用法必须包含成员槽位；已引用的数字限流值必须
  进入公开说明；宿主标记为 `blocking_unknown_*` 的安全未知项不调用模型并直接关闭；不安全的 Answer Markdown
  回退为已验证的公开 claims。各新增机制已分别通过真实 Provider 诊断，完整开发集的若干重跑仍暴露模型
  非确定性，因此没有把分项通过冒充为统一的 12/12 Provider Gate。

## 后果

- 代码不再猜测 Python 工厂语义，分组错误面显著缩小；相同闭包 Handler 生成的大量 Matcher 仍只分析一次；
- 一个工厂若为不同成员生成不同 Handler 代码，首版会分别分析或关闭，而不会为了提高覆盖率自动合并；
- 模型可能需要额外读取外层源码，因此复杂能力的工具调用和失败关闭比例可能上升；这比错误合并后公开错误
  用法更可接受；
- 若以后需要跨不同 Handler 代码聚合，必须由新的确定性 Runtime 身份或插件公开元数据提供依据，不能恢复
  基于 AST 外层结构或文本相似度的推断。

## 相关决定

- [ADR-0069：分离帮助展示与 Answer 知识，并收窄静态分析职责](0069-separate-help-display-from-answer-knowledge-and-bound-static-analysis.md)
- [ADR-0077：把上一版机器生成教学内容作为非证据基线](0077-use-previous-generated-teaching-content-as-a-non-evidentiary-baseline.md)
- [ADR-0080：把一次能力分析投影为多个公开教学条目](0080-model-capability-teaching-as-multiple-public-entries.md)
- [ADR-0081：未知安全门禁关闭公开教学，并冻结 parser 拥有的用法](0081-close-unknown-teaching-gates-and-freeze-parser-owned-usages.md)
- [PLAN-0017：收敛多条目教学注释的生成与评测合同](../plans/done/PLAN-0017-qualify-multi-entry-capability-teaching.md)
