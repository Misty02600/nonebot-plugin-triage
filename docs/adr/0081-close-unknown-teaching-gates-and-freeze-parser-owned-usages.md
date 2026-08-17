# ADR-0081：未知安全门禁关闭公开教学，并冻结 parser 拥有的用法

- 状态：部分被 ADR-0082、ADR-0083 替代；parser canonical usage 与有限枚举决定继续有效
- 决策日期：2026-08-16
- 后续关系：[ADR-0082](0082-group-parameterized-matchers-only-by-runtime-handler-code-identity.md) 删除本 ADR
  为参数化工厂构造成员数量、成员名和省略标记的部分；有限枚举仍是通用展示规则。
- 后续关系：[ADR-0083](0083-resolve-unknown-teaching-gates-before-closing-public-knowledge.md) 保留
  “最终未知必须关闭”的安全目标，但改为先让 Agent 对结构候选补证并区分实际约束、已证明无约束与仍未知。

## 背景

[ADR-0080](0080-model-capability-teaching-as-multiple-public-entries.md) 已让 Runtime / parser 决定公开 entry，模型
负责生成完整用法与公开说明。第一次真实 Provider Gate 暴露了两个边界问题：

1. Runtime Alconna 树已经给出参数必选性、子命令和 Option，但模型仍可能把 `[]` 改成 `<>`、遗漏别名，或
   把同一 Option 拆成另一项功能；
2. 第三方权限或限流候选无法确认时，若仅忽略它，Answer 与后续 Bug 前置判断可能把“未知”误当成“不限流”或
   “所有人可用”。

人工 Migut Help 也同时存在少量记法漂移，例如参数化工厂的成员名称实际必填，却使用表示可省略的 `[]`。

## 决策

### 1. 精确 parser 语法由模型外拥有

Runtime 已确认的 Alconna 参数、叶子子命令和 Option 由 Triage 规范化为 `canonical_usages`。模型仍生成名称、
摘要、特殊说明和 Answer Markdown，但必须逐字返回 canonical usage，不得改变：

- 必填 `<>` 与可选 `[]`；
- Option token 及其别名；
- 子命令所属 entry；
- parser 已确认的参数顺序。

这不是重新实现 Alconna parser。Triage 只读取现有 Runtime 结构投影；普通 `on_command` 中由业务代码解析的参数
仍由模型依据有界 Evidence 说明。

### 2. 未知安全门禁关闭整个公开教学单元

只有调用入口、必要参数、公开性、权限和全部限流都足够确定时，`knowledge_enabled` 才能为 `true`。源码出现
相关门禁后，即使允许导航补证，仍无法确定其公开语义时，整个分析单元关闭：

- 不进入 Migut Help YAML；
- 不进入普通 Answer 检索；
- 不得供 Bug 前置判断推断“不限流”或“所有人可用”；
- 关闭数量和有界分析单元标识进入部署日志。

关闭项预期是少数保守失败。模型可以关闭知识，但不能绕过 Runtime、披露、平台或 Evidence 完整性检查强制
开启。

### 3. 有限枚举以四个为边界

同一位置的确定备选值不超过四个时可以直接枚举；超过四个时使用一个简短概念槽位，不把长成员表塞进帮助
用法。参数化工厂的成员名称是必填时使用 `<成员名>`，而不是 `[成员名]`。

工厂首包只在成员不超过四个时附带具体 Runtime header；更多成员只附带数量与“已省略成员表”事实。详细公开
成员若以后确有 Answer 需求，继续留在 Answer Markdown，不为此新建专用成员索引。

## 实现与验证

- `CapabilityInvocationTarget` 新增有界 `canonical_usages`，并进入模型 payload 与分析 fingerprint；
- NoneBot adapter 从 `command.arguments` / `command.components` 生成一条稳定用法；Option 超过四项时压成
  `[可选参数]`；
- 模型输出校验和公开投影都拒绝改写 canonical usage，并把 anchored command body 从子串判断收紧为完整词元；
- 公开文本额外拒绝 `OWNER`、`MEMBER`、`ADMIN`、`Permission` 等框架内部词；
- 刷新状态新增 disabled 计数，日志以有界列表显示关闭的教学单元；
- v3 正式 fixture 与报告保持冻结；新 Prompt 使用单独的 v4 开发回归集，后续正式资格必须使用新的
  forward-heldout。

最终开发诊断使用四条新合成案例，分别覆盖 parser 固定 Option、未知限流关闭、四成员有限枚举和五成员概念
槽位。结果为 schema、Evidence 闭合、公开投影、安全、语义、预算和工具边界全部 1.000；共 6 次 Provider
request、14,926 input token、2,218 output token、781 microUSD。该运行显式处于 diagnostic 模式，不产生正式
资格；此前每次失败报告均原样保留，没有覆盖或改写。

## 后果

- 结构化 parser 的帮助用法更稳定，但参数名称沿用 Runtime 结构提供的名称；参数文案优化不能以改坏必选性为
  代价；
- 第三方权限或限流无法确认时会牺牲少量覆盖率，换取 Help、Answer 与 Bug 前置判断不产生危险的否定推断；
- “四个”是当前展示政策，不是 parser 语义；未来若人工语料证明阈值应调整，可在不改变安全所有权的前提下
  更新 generation contract 与评测；
- 若未来要为普通业务代码建立完整参数 parser、允许未知安全门禁保留部分公开 entry，或把长成员表做成专用
  检索索引，需要 successor ADR。

## 相关决定

- [ADR-0058：用确定性证据与有界源码导航生成教学注释](0058-use-deterministic-evidence-and-bounded-navigation-for-teaching-annotations.md)
- [ADR-0069：分离帮助展示与 Answer 知识，并收窄静态分析职责](0069-separate-help-display-from-answer-knowledge-and-bound-static-analysis.md)
- [ADR-0080：把一次能力分析投影为多个公开教学条目](0080-model-capability-teaching-as-multiple-public-entries.md)
- [PLAN-0017：收敛多条目教学注释的生成与评测合同](../plans/done/PLAN-0017-qualify-multi-entry-capability-teaching.md)
