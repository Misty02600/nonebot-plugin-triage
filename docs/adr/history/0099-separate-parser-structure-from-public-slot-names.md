# ADR-0099：分离 Parser 参数结构与公开槽位名称

| 状态 | 决策日期 |
|---|---|
| 已采纳；family 聚合槽位由 [ADR-0102](0102-keep-family-aggregate-parameters-actionable.md)、联合输入传递由 [ADR-0111](0111-preserve-alconna-union-input-types-in-family-shapes.md) 细化 | 2026-08-21 |

## 当时遇到了什么

ADR-0081 把 Alconna 已确认的参数顺序、必选性、重复性、Option 和别名冻结为 canonical usage，
避免模型改坏调用结构。但首版也把 `Arg.name` 逐字冻结成公开占位名，使 `meme_name`、`num`、`text`
等实现标识直接进入帮助文字。Parser 能确定这些字段在语法中的位置，却不能仅凭变量名证明它们应公开叫
“表情名”“角度”或“尺寸”。

Alconna 的 `Arg.notice`、`CommandMeta.usage` 和 Handler 源码可以为命名提供证据，但它们也可能陈旧、含
内部术语或与当前 Runtime 结构不一致，不能覆盖 Parser 已确认的语法。

## 决定

1. Runtime adapter 把 Parser 参数渲染为稳定匿名槽位 `slot:N`。Parser 继续唯一拥有命令、子命令、参数
   顺序、`<>` / `[]`、`...`、Option token 和 Option 别名。
2. 模型必须为匿名槽位生成简短公开名称。`Arg.notice`、能与结构对应的显式 usage、Handler 与说明源码都
   是命名 Evidence，而不是无条件真值；不得只凭内部变量名猜业务含义。
3. 输出校验器比较结构模板时只允许替换槽位内文字；改变括号、顺序、Option、别名、重复性或槽位数量均
   进入现有 correction，仍失败才关闭单元。`slot:N` 不得进入最终公开注释。
4. 精确 family 成员查询不再次调用模型。该确定性回退只按类型提供保守名称：图片、文本、整数、数值；
   类型也不能确定时使用“参数”。不建立 `img → 图片`、`num → 角度` 之类变量名字典。
5. 普通、无结构化 Parser 的 Matcher 仍保持 `anchor_only`，不得为了适用本决定而伪造参数 shape。

## 没有采用的方案

- 不继续逐字公开 `Arg.name`；它是结构标识，不是公开语义合同。
- 不让 notice 或声明 usage 覆盖 Parser 的必选性、顺序和 Option。
- 不增加插件专属参数类型、meme 成员目录或第二套注释生成器。
- 不让模型自由重写整条 usage；它只拥有槽位公开名称。

## 带来的影响

- Prompt 升为 v42，request revision 升为 v7；schema 7 的最终公开字段不变。
- 旧 Prompt / request 的 held-out 只能作为历史证据，当前合同必须重新评测。
- family 的 Parser shape 仍按结构去重；参数内部名和 notice 可以留在有界 Evidence 中供模型理解，但不会
  作为 canonical 公开文字。

## 替代关系

- 替代 [ADR-0081](0081-close-unknown-teaching-gates-and-freeze-parser-owned-usages.md) 中“参数名称也必须
  逐字复制”的部分；ADR-0081 对 Parser 语法结构和未知 gate 的安全目标继续有效。
- 细化 [ADR-0098](0098-deduplicate-complete-family-member-manifests.md) 的 Parser shape：shape 保存结构与
  命名 Evidence，公开槽位名不参与 shape 等价性。

## 相关文档

- [能力影子索引流程](../../architecture/flows/capability-shadow-index.md)
- [模型与 Provider 支持矩阵](../../architecture/model-provider-support.md)
