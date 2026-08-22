# ADR-0102：大聚合保留有用概括且不预设槽位名称

| 状态 | 决策日期 |
|---|---|
| 已采纳 | 2026-08-21 |

## 当时遇到了什么

family 只生成一条共同公开 annotation，因此聚合 usage 必然比每个成员的精确 Parser 合同更概括。
早期 Prompt 为异构 family 预设“操作参数”，并要求类别越多时继续在 summary 或行为边界逐类解释。
这把结构安全要求误写成了文案模板：模型容易照抄固定词，也会为了覆盖大型 family 而重复完整成员清单。

Parser shape 已经能证明每个成员的必选性、顺序、Option、重复性和输入类型下限；完整成员事实也保存在
Runtime manifest 中。公开聚合 entry 需要覆盖这些结构，但不需要再充当成员目录。

## 决定

1. family 聚合 usage 只表示成员选择位和输入类别并集的概览。精确成员的必选性、顺序和直接调用形式仍由
   当前 Runtime record 负责，不把聚合 usage 解释成每个成员都相同的 Parser 合同。
2. 模型外冻结成员选择位、必选性、可选性、重复性和 Parser 已确认的输入结构；公开槽位名称由模型根据
   当前 Evidence 生成，Prompt 不提供固定成品词。
3. 同一聚合位置有二至三类公开语义时，usage 可以直接枚举；四至六类时可以使用由 Evidence 命名的概念
   槽位，并在 summary 中说明这些类别。
4. 七类及以上使用概念槽位，summary 或 behavior boundary 不逐类列举，只保留对理解功能或正确调用确有
   价值的简短概括。同一边界也适用于 family 成员命令：七个及以上成员不得在公开说明中重复完整成员名单。
5. “参数”与其他模型生成的槽位名称接受相同的通用公开文本和 usage 校验，不设专门门禁、优先级或强制
   补充说明。
6. 模型外只校验 Parser 能确定的结构覆盖。聚合 usage 遗漏输入槽位或把包含文本与数值的 family 窄化为
   纯数值时进入一次现有 output correction；correction 只报告缺少的结构类别，不替模型生成成品 usage。
7. 完整成员和 shape 继续进入请求、fingerprint 与 Runtime 查询；不采样成员，不为插件新增成员目录、
   专属字段、持久化 catalog 或确定性槽位词典。

## 为什么这样选

- Parser 能判断结构是否被删改，但不能替用户选择最合适的业务名称。
- 固定槽位词会诱导模型复述答案，大型聚合逐类解释又会重复 manifest 并扩大输出成本。
- 小集合直接枚举、中集合有界说明、大集合只做概括，可以保持帮助文本可读，同时保留完整底层事实。
- 现有 summary、usage 和 behavior boundaries 足以表达共同教学知识，无需扩充公开 Schema。

## 带来的影响

- 当前 Prompt 为 v56，request revision 为 v23，公开 schema 为 8。
- 模型外 family 输入类别覆盖校验继续只报告缺少的结构类别，不提供槽位成品名称。
- 历史 Prompt / request 的 held-out 与插件诊断只保留历史意义，不能继承给当前合同。

## 关系

- 细化 [ADR-0095](0095-preserve-family-member-invocations-and-compress-only-display.md) 的完整成员保留与
  聚合展示边界。
- 细化 [ADR-0099](0099-separate-parser-structure-from-public-slot-names.md) 的 Parser 结构与公开命名分离。
- 与 [ADR-0100](0100-keep-migut-help-descriptions-minimal.md) 一致：Help 只消费简短 summary，不追加其他
  requirement 或 behavior boundary。

## 落实与确认

- `capability_model_adapter.SYSTEM_INSTRUCTION` 不包含固定的聚合槽位成品词；
- family 输入覆盖校验保留 Parser 结构，不对“参数”增加专门逻辑；
- 回归测试覆盖遗漏类别的定向 correction、Uniseg `At` 输入和普通 `[参数]` 槽位。

## 相关文档

- [能力影子索引流程](../architecture/flows/capability-shadow-index.md)
- [可选帮助数据源与复用边界](../architecture/help-source-adapters.md)
- [模型与 Provider 支持矩阵](../architecture/model-provider-support.md)
