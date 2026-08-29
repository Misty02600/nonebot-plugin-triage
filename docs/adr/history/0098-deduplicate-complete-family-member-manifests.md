# ADR-0098：用完整成员清单和 Parser shape 去重 family 请求

| 状态 | 决策日期 |
|---|---|
| 已采纳 | 2026-08-20 |

## 当时遇到了什么

ADR-0095 要求一次 family 分析保留全部 Runtime 成员，避免从代表成员或共享 Handler 猜测成员调用。
这个安全边界正确，但首版请求把每个成员的 invocation 同时写入外层 `family_members` 和
`runtime_family_members` Evidence，Evidence 内又以通用 claims 重复命令头、alias、参数、组件和约束。
结构相同的数百个 Alconna Matcher 也分别复制同一份 Args / Option / Subcommand。

真实 `nonebot_plugin_memes` 的 424 成员 family 因而在一个 teaching unit 内形成巨大上下文；每次源码工具
调用、输出修正和模型请求都会重新携带这份重复数据。简单取消 Evidence 数量或 Agent 用量上限只能让任务
继续运行，不能消除输入成本，也会让模型先整理重复事实再判断共同语义。

另一方面，family 候选由闭包 Handler 代码身份形成，不只包含 Alconna。普通 `on_command` 能确定命令头、
alias、前缀和空白规则，却不能证明 Handler 内手写参数语法。若把空 `arguments` 解释成“确定没有参数”，
或者要求所有普通 Matcher 都提供可比较 shape，会把 Alconna 能力错误推广为通用 Parser 能力。

## 决定

1. family 继续适用于所有具备稳定公开调用锚点、共享同一闭包 Handler 代码身份且披露集合完整的 Runtime
   Matcher，不限定为 Alconna，也不按 Matcher 文本相似度创建 family。
2. Provider 必须看到本轮全部成员；不采样、不截断、不选代表成员。请求把成员事实归一化成两类 Evidence：
   `runtime_family_members` 保存每个成员的能力 ID、命令、alias、语法可信度和必要声明；
   `runtime_family_shapes` 保存去重后的 Parser 参数结构，成员只引用 `shape_id`。
3. 只有 Runtime adapter 能证明参数结构完整时才能创建 shape。当前 `parser_exact` 只用于 Alconna；普通
   `on_command` 标为 `anchor_only`，`on_startswith` 标为 `open_tail`，`on_fullmatch` 可标为
   `literal_exact`。缺少 shape 不表示没有参数，也不构成关闭 family 的理由。
4. 相同 Parser 结构在一次请求中只保存一次。共享 Handler、helper、gate、固定权限和配置投影继续只提供
   一次；成员 gate 不一致且当前单条 family 注释无法安全表达时仍在模型前失败关闭。
5. Provider payload 的 `family_manifest` 只携带成员数量和 manifest Evidence ID，不再复制每个成员
   invocation。领域请求仍保留完整 `CapabilityFamilyMember.invocations`，供模型外校验和当前 Runtime 查询
   使用；fingerprint 通过 manifest Evidence 摘要绑定完整成员集合。
6. 模型仍必须检查完整成员清单是否具有可信共同业务概念，并只输出一条 family 注释。参数数量、图片或
   文字输入、可选性和 Parser shape 不同本身不关闭 family；语义离群、成员绑定不可靠、披露不完整或不可
   表达的 gate 冲突才关闭。

## 没有采用的方案

- 不为 meme 或任何单个插件增加成员目录、模板字段或持久化 catalog；manifest 只存在于当前分析请求。
- 不为每个成员分别调用模型；成员共享源码仍只分析一次。
- 不通过成员抽样、数量上限或代表 shape 降低成本；这些做法无法证明完整 family 的共同语义。
- 不把普通命令的空参数投影成 Parser shape，也不从 Python 变量名猜测参数语义。
- 第一版不增加多阶段 family 分类 Agent、Patch-only 修正协议或独立缓存；先测量无损去重后的实际收益。

## 带来的影响

- 大 family 的模型输入从“每成员重复 invocation、claims、constraints 和 Parser AST”收敛为“全部成员的
  紧凑行 + 唯一 Parser shapes + 一份共享源码合同”；成员数量和语义覆盖不变。
- 普通命令 family 仍可生成共同知识，但没有结构化 Parser 时只能依据调用锚点、声明 Evidence 和共享源码，
  不能声称精确尾部语法。
- Prompt 升为 v41、request revision 升为 v6；此前 v13 和之后所有旧 request revision 评测只保留历史
  证据，不能继承到当前合同。schema 7 的公开输出字段没有变化。

## 落实与确认

- `capability_analysis_adapter._family_member_invocations` 分别生成去重 shape Evidence 和完整成员 Evidence；
- `capability_model_adapter._build_payload` 只发送紧凑 `family_manifest` 引用；
- 分析 fingerprint 同步改用 manifest 引用和 Evidence 摘要；
- 回归覆盖普通命令 `anchor_only`、异构 Alconna shape、相同 shape 去重和 Provider payload 不再复制成员
  invocation。

## 替代关系

- 细化 [ADR-0095](0095-preserve-family-member-invocations-and-compress-only-display.md) 第 2 项的请求内表示；
  ADR-0095 的完整成员集合、单一 family teaching unit、参数异构不关闭和查询展示边界继续有效。

## 相关文档

- [能力影子索引流程](../../architecture/flows/capability-shadow-index.md)
- [可选帮助数据源与复用边界](../../architecture/help-source-adapters.md)
