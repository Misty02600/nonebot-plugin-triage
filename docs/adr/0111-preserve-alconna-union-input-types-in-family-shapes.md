# ADR-0111：在 family Parser shape 中保留 Alconna 联合输入类型

| 状态 | 决策日期 |
|---|---|
| 已采纳 | 2026-08-22 |

## 当时遇到了什么

Alconna 的 `MultiVar(Union[Text, Image, At], "*")` 能确定用户可直接提供文字、图片和提及用户三种
消息段，但 Runtime 快照只读取 `MultiVar.origin`。该值会退化为 `typing.Any`，导致 family shape 丢失
联合成员；模型即使从 Handler 看见 `At`，模型外校验器也无法发现聚合 usage 漏写 `@用户`。

## 决定

1. Runtime 快照对能直接读取的 Alconna `UnionPattern` 保存全部成员的稳定限定类型名，而不是把联合输入
   压成 `typing.Any`。该信息继续随 `command.arguments` 进入去重后的 `runtime_family_shapes`、请求指纹和
   源码复核边界。
2. Parser 仍只拥有输入结构和类型下限，不负责猜业务名称；联合成员只是给模型和校验器提供完整输入集合。
3. Uniseg `At` 表示用户可以直接输入 `@用户`。聚合 usage 枚举直接输入形式时必须保留它，不能因为 Handler
   最终把提及转换成头像图片，就只在行为边界中说明而从 usage 删除。
4. 不为 memes 或其他单个插件新增字段、成员目录或类型词典；无法从 Parser 稳定取得的业务语义仍由当前
   Evidence 解释，证据不足时不猜测。

## 为什么这样选

- 这是 Parser 已经拥有但在适配时被丢失的结构事实，修复数据传递比在 Prompt 中硬编码某个插件示例可靠。
- 保留联合成员后，模型外可以只报告缺少的输入类别，仍不替模型生成完整公开 usage。
- 类型限定名是现有 `pattern_type` 合同的自然扩展，不需要增加插件专属 Schema。

## 带来的影响

- request revision 从 v22 升为 v23；公开 schema 继续为 8。
- 同轮 Prompt 澄清最终收敛为 v56。
- v53 / request v22 的插件诊断仍是历史观测，不能作为当前合同的质量结论。

## 关系

- 细化 [ADR-0099](0099-separate-parser-structure-from-public-slot-names.md) 的“Parser 锁结构、模型命名”边界。
- 继续遵守 [ADR-0098](0098-deduplicate-complete-family-member-manifests.md) 的 shape 去重；联合类型参与 shape
  内容和 fingerprint，不恢复逐成员重复 AST。
- 继续遵守 [ADR-0102](0102-keep-family-aggregate-parameters-actionable.md) 的展示密度边界。

## 落实与确认

- `capability_snapshot._alconna_pattern_type()` 保存稳定联合成员；
- family 输入覆盖校验识别 Uniseg `Text / Image / At`，漏写 `@用户` 时进入现有一次 correction；
- 回归测试覆盖联合类型快照和聚合 usage 的提及输入保留。

## 相关文档

- [能力影子索引流程](../architecture/flows/capability-shadow-index.md)
- [可选帮助数据源与复用边界](../architecture/help-source-adapters.md)
- [模型与 Provider 支持矩阵](../architecture/model-provider-support.md)
