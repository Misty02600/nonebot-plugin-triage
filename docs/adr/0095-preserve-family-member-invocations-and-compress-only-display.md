# ADR-0095：保留 family 成员调用事实并只压缩展示

| 状态 | 决策日期 |
|---|---|
| 已采纳；请求内成员表示由 [ADR-0098](0098-deduplicate-complete-family-member-manifests.md) 细化，聚合参数展示由 [ADR-0102](0102-keep-family-aggregate-parameters-actionable.md) 细化 | 2026-08-20 |

## 当时遇到了什么

ADR-0094 让参数化 Matcher family 只保存一条共同公开注释，并从当前 Runtime 索引有界补充少量成员。
但生产请求仍把 family 压成唯一的 `entry_id="family"`，没有向模型提供成员数量、命令或参数结构；查询
命中具体成员时也只补命令头。结果是模型无法可靠概括大 family，Answer 也无法回答“摸摸需要什么参数”。

更严重的是，Prompt 把“成员参数结构不同”当作关闭整个 family 的理由。meme 一类工厂可以共享可信的业务
概念和 Handler，同时让不同成员分别接收图片、文字或不同数量的参数；这种差异属于成员调用合同，不应被
误判为 family 语义不成立。

## 决定

1. family 仍是一个不可拆分 teaching unit，只运行一次模型并只输出一条共同公开 annotation；不新增公开
   `members / variants / catalog` 字段，也不为成员分别写注释缓存。
2. family 请求必须包含本轮全部公开 Runtime 成员的确定调用事实：成员能力 ID、anchored 命令、alias、
   Runtime parser 能确定的参数、Option、子命令、canonical usage、提及要求和对应 Evidence。共享 Handler 与
   源码切片仍只提供一次。
3. 成员参数数量、图片或文字输入、必选性、Option 和精确 usage 不同本身不关闭 family。family 只在无法
   形成可信共同业务概念，成员绑定或源码 revision 不可靠，披露集合不完整，或 gate / 权限冲突且无法安全
   绑定成员时关闭。
4. family 的公开 usage 是成员选择与输入种类的聚合概览，例如 `<表情操作> [图片|文字]...`；普通 Matcher
   和被精确命中的 family 成员仍使用当前 Runtime record 确定性渲染的直接调用形式。
5. family 查询先按 annotation capability ID 去重，同一 family 不用多个成员占满结果。检索候选由共同
   name、summary、search terms 和当前成员命令共同构成；不同插件的相似 family 保持分离，无法确定用户意图
   时返回多个候选或要求澄清，不合并成员列表。
6. 精确命中成员命令或 alias 时，Answer 使用 family 共同说明加该成员由 `command.arguments / components`
   重建的精确 usage；普通 family 查询只使用聚合 usage，不能把任意代表成员误当成用户指定成员。
7. 固定备选的展示阈值统一改为：一至三项直接在 usage 枚举；四至六项在 usage 使用概念槽，并在 summary
   完整说明；七项及以上使用概念槽，summary 只说明类别。该规则同时适用 family、单个 Matcher 的命令头、
   alias、Option 与固定参数。开放式自然语言输入不按枚举数量处理，由 summary 描述支持范围。

## 为什么这样选

- Runtime Matcher 已经拥有当前成员身份和 parser 结构；生成时保留它们比要求模型从共享源码猜成员更可靠。
- 成员事实只进入内部请求和查询组合，不形成第二套持久化目录，源码或 Runtime 更新后不会留下过期成员表。
- 将 family 共同语义与成员精确调用分层后，参数异构不再扩大 fail-closed 范围，未知 gate 和披露边界仍然
  保持安全关闭。
- family 级去重避免一个大 family 占满检索 limit；精确成员匹配与普通语义匹配分开后，不会把随机代表
  Matcher 的参数回答给用户。
- `≤3 / 4–6 / ≥7` 只决定文本展示密度，不再决定成员知识是否保存或 family 是否有资格发布。

## 没有采用的方案

### 为 meme 增加成员目录 Schema

不新增插件专属的图片数、文字数、模板选项或成员目录。成员调用结构来自通用 Runtime record，family 共同
语义继续使用现有公开 entry 字段。

### 为每个成员单独调用模型

成员共享 Handler 和共同业务概念时，逐成员调用会重复阅读相同源码并增加成本。精确调用形式由确定性层
负责；只有共同语义交给一次 family 分析。

### 继续要求所有成员共享同一参数结构

这会关闭大量真实工厂能力，并把展示压缩限制错误升级为安全资格条件。参数差异能够绑定到当前成员时不影响
共同业务语义。

## 带来的影响

- family 请求和 fingerprint 会包含完整成员调用投影，成员变化会触发该 teaching unit 重生成；
- 大 family 的初始请求会增加一份紧凑 Runtime 成员 Evidence，但不会重复共享函数源码；
- Help 继续显示一条聚合 family 命令，Answer 可以在精确成员查询时给出成员参数；
- Prompt 升为 v40、request revision 升为 v4；v13 的 Prompt v39 / request v3 held-out 只保留历史结论，
  不能继承给新合同。

## 落实与确认

- `CapabilityAnalysisRequest.family_members` 保存内部成员调用合同并进入 fingerprint 与模型 payload；
- family adapter 将全部当前成员投影为 `runtime_family_members` Evidence；
- usage validator 与 Runtime alias/Option 渲染采用三项显式枚举阈值；
- capability shadow 查询按 family 去重，另行记录查询是否精确选择成员，并为精确成员重建 canonical usage；
- 领域、adapter、Prompt、usage 与查询回归测试覆盖异构参数、三项阈值、family 去重和成员精确回答。

## 替代关系

- 替代 [ADR-0094](0094-simplify-the-public-capability-teaching-contract.md) 第 10–12 项关于 family 成员输入、
  参数异构关闭和四项展示阈值的决定；ADR-0094 的公开字段、Requirement、Help/Answer 投影和隐私边界继续有效。
- 细化 [ADR-0082](0082-group-parameterized-matchers-only-by-runtime-handler-code-identity.md)：Runtime Handler
  代码身份仍只决定 family 候选，模型必须结合完整成员调用事实判断是否存在共同业务语义。

## 相关文档

- [能力影子索引流程](../architecture/flows/capability-shadow-index.md)
- [可选帮助数据源与复用边界](../architecture/help-source-adapters.md)
