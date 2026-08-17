# ADR-0087：验证并压缩 Runtime 命令别名后再生成教学用法

| 状态 | 决策日期 |
|---|---|
| 已采纳；已实现，待新模型评测 | 2026-08-17 |

## 背景

NoneBot 2.5 的 Runtime `CommandRule` 可以确认一组实际生效的命令文字，但不会保留主命令与 aliases 的
声明顺序。此前 Triage 从排序后的集合中任选一个 `command_body`，只把其他值作为模型参考，导致帮助图只显示
`口他`、`拨动滚轮`、`s` 等偶然排在前面的别名。直接让模型自由重写完整 usage 又会把命令事实、参数结构和
展示压缩混在一起，无法证明 `(取消|关闭)(全体|全员)禁言` 没有遗漏或创造触发词。

Migut Help 还使用 `...` 表示同一参数槽位可以重复。旧生成合同把它写在槽位内部，如 `<图片...>`；为了让
操作符的作用域明确，需要统一成 `<图片>...` 或 `[图片]...`。

## 决策

1. Runtime 观察到的 `command_body + aliases` 是同一功能入口的完整触发词真值。模型不能新增、删除或把别名
   拆成新的教学 entry；NoneBot 全局 `COMMAND_START` 也不进入这组文字。
2. 模型继续用确定性的 `command_body` 生成 usage，并额外返回可选 `display_trigger`。该字段只能包含固定文字、
   `|` 与可嵌套圆括号，用来压缩完全等价的别名集合，不得包含参数、`@bot`、全局命令前缀或说明文字。
3. Triage 用有界语法解析器展开 `display_trigger`。展开集合必须与 Runtime 触发词集合完全相等，且不得重复；
   解析深度、展开数量和文本长度均受限。校验通过后，Triage 才把 usage 中唯一的 `command_body` 替换为
   `display_trigger`，参数、Option、子命令、回复上下文和 `@bot` 仍由原合同拥有。
4. 首次出现语法错误或集合不相等时，通过 Pydantic AI `ModelRetry` 把缺失与多余项反馈给模型，并明确只修复
   `display_trigger`。第二次仍无效时，不关闭教学知识，而是退回模型外生成的完整别名枚举；若命令本身包含
   当前表达式语法的元字符或超出展示预算，则保留确定性的 `command_body`。
5. 模型返回未压缩但集合完全相等的表达式时直接接受。压缩属于展示质量，不是权限、公开性或知识启用门禁；
   其他 schema、Evidence、用法和安全错误仍按原合同重试或失败关闭。
6. 重复参数统一写为 `<参数>...`（至少一次）和 `[参数]...`（零次或多次）。`...` 必须位于一个完整槽位之后；
   `<参数...>`、`[参数...]` 和独立省略号均拒绝。Runtime parser 生成的 canonical usage 直接采用这一格式。
7. 通用目标槽位只约定 `@用户` 与 `用户ID`。插件特有代号、昵称或其他业务输入仍由模型依据有界源码说明，
   不为此增加脆弱的业务 AST 解析器。

## 为什么这样选

- Runtime 与 parser 继续拥有命令和参数真值，模型只优化别名集合的可读表达；
- 展开集合相等校验可以安全支持 `(禁言|(禁|口|踩)(他|她))` 这类嵌套压缩，而不要求代码理解中文语义；
- 一次定向重试给模型修复简洁性的机会，确定性回退则避免低质量压缩关闭原本正确的知识；
- 不在 Migut Help 中重新实现选择逻辑，生成 YAML 本身就是可以直接渲染的一种正确用法。

## 没有采用的方案

- 继续只展示排序后的一个 Runtime literal：安全但会随机偏向冷门别名，且丢失公开触发形式；
- 让模型直接重写包含别名、参数与前缀的完整 usage：难以区分展示错误与命令事实错误；
- 为中文别名编写语义压缩规则或静态解析用户 ID、昵称、代号：与插件业务耦合且容易漏判；
- 因别名压缩失败关闭整个教学单元：把软展示质量错误错误升级成公开知识安全门禁。

## 带来的影响

- 有利：帮助图能稳定展示完整别名集合，并允许模型生成更紧凑的等价表达式；
- 有利：Alconna / Runtime parser 的参数必选性与 Option 结构仍不会被别名展示改写；
- 代价：模型输出 schema 新增 `display_trigger`，Prompt revision 变更，上一版 v34 / v8 评测只保留为历史质量
  证据，v35 在新 held-out 完成前标记为未验证但仍可运行；
- 风险：含 `()|` 等表达式元字符的极少数真实命令暂不压缩全部 aliases，而是保守显示确定性主命令。

## 落实与确认

- `src/nbtriage/capability_usage.py` 实现有界展开、集合相等校验和确定性枚举回退；
- `src/nbtriage/capability_model_adapter.py` 增加 `display_trigger` 输出、一次定向重试与第二次失败回退；
- `src/nbtriage/capability_annotations.py` 在公开投影边界再次验证并替换命令正文；
- `src/nonebot_plugin_triage/capability_analysis_adapter.py` 把 parser 多值参数改为槽位外 `...`；
- 单元测试覆盖嵌套压缩、集合差异、预算失败、重试回退、公开投影和重复参数格式。
- 2026-08-17 使用 OpenCode Go `deepseek-v4-flash` 做了两条 v35 诊断 smoke：嵌套别名生成
  `(禁(言|他|她)|口(他|她)|踩(他|她)) <用户>`，必填多值参数生成 `批量压缩 <图片>...`；两条的
  schema、Evidence、公开投影、安全、语义和预算均通过，共 2 次请求、8,410 input / 3,511 output token、
  1,037 microUSD。该诊断使用已知开发案例，不构成新的 forward-heldout 资格。
- 同一轮先按当前 60 秒生产超时运行四条诊断，均在获得 Provider 响应前按 transport failure 失败，0 个可确认
  请求、0 个可确认 token / 费用；把单条诊断超时放宽到 180 秒后上述两条成功。因而当前 Prompt 质量与
  Provider 延迟必须分开评估，是否调整生产超时和后台吞吐另行决定。

## 相关文档

- [ADR-0062：结构化能力教学的用法、约束与交互](0062-structure-capability-teaching-usages-requirements-and-interactions.md)
- [ADR-0080：把一次能力分析投影为多个公开教学条目](0080-model-capability-teaching-as-multiple-public-entries.md)
- [ADR-0081：未知安全门禁关闭公开教学，并冻结 parser 拥有的用法](0081-close-unknown-teaching-gates-and-freeze-parser-owned-usages.md)
- [ADR-0086：把模型评测作为质量标签而不是运行许可](0086-treat-model-evaluation-as-a-quality-label.md)
