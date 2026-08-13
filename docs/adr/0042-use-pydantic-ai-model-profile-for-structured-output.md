# ADR-0042：由 Pydantic AI ModelProfile 决定结构化输出方式

| 状态 | 决策日期 |
|---|---|
| 已采纳；手写 Direct Request 输出定义由 ADR-0044 部分替代 | 2026-08-13 |

> [ADR-0044](0044-use-pydantic-ai-agent-output-type-for-support-semantics.md) 改用 Pydantic AI
> `Agent(output_type=SupportSemanticAssessment)` 消费本 ADR 确认的 `ModelProfile`；传输能力所有权、
> 请求前唯一选择、Prompted Output 未资格化和零动态 fallback 决定继续有效。

## 当时遇到了什么

语义 assessment 已经使用 Pydantic AI `Model` 和 `ModelRequestParameters`，但通用 client 仍额外接收
项目自定义的 `output_mode="native" | "tool"`。OpenCode Go 一边声明 `supports_tools` 与
`supports_json_schema_output`，一边再传入 `output_mode="tool"`；任务资格键还把 `tool-output` 编进项目
profile 名称。这形成了两份可能漂移的模型传输能力事实。

Pydantic AI 2.27.0 的 `ModelProfile` 已拥有 `supports_tools`、`supports_json_schema_output` 和
`default_structured_output_mode`，`Model.prepare_request()` 会根据它选择并校验结构化输出方式。项目只需
决定某个精确组合是否通过当前任务的质量、安全与费用门，不应复制框架已经拥有的能力模型。

## 决策

1. Pydantic AI `ModelProfile` 是模型传输能力和默认结构化输出方式的唯一真相源。通用语义 client 不接受
   项目自定义 `output_mode`，也不定义 `SemanticTransportCapabilities` 或等价结构。
2. 通用 client 同时提供 Pydantic AI 原生的 output object 与 output tool 定义，并保持
   `ModelRequestParameters.output_mode="auto"`。`Model.prepare_request()` 根据
   `default_structured_output_mode` 选中一种形式并清除另一种定义；项目不在请求失败后猜测、切换或重试。
3. OpenCode Go 使用显式 `OpenAIModelProfile`：`supports_tools=true`、
   `supports_json_schema_output=false`、`default_structured_output_mode="tool"`。因此它在请求前稳定选择唯一、
   不可执行的 Tool Output，而不是依赖 Pydantic AI 的全局默认值。
4. 当前语义 task 只资格化 Tool Output 与 Native Output。若某模型 Profile 选择 Prompted Output，请求前直接
   拒绝；未来只有单独评测并形成新决定后才能支持，不能回退到其他方式。
5. 项目任务资格只记录 Pydantic AI 不负责的维度：Provider、API 族、精确模型、task、schema revision、
   Prompt revision、隐私策略、预算 profile 与评测 revision。资格表不重复记录 supports flags、Tool/Native
   选择或其他 ModelProfile 字段。

## 原因与影响

- 新增 Provider 时只需正确构造 Pydantic AI Model/Profile，并单独完成项目任务资格，不再同步两套能力结构；
- 结构化输出选择在网络请求前唯一确定，保留一次请求、零重试、无动态 fallback 的既有边界；
- 项目仍负责输出 schema、响应的领域二次校验、Provider/model 身份、隐私、预算和资格；这些不属于
  `ModelProfile` 的职责；
- Direct Request 继续使用 Pydantic AI 的低层原生请求参数，不引入 Agent loop 或可执行工具。

## 没有采用的方案

- **保留显式 `output_mode` 作为保险**：会继续产生第二个传输能力真相源，并允许 factory 与 Profile 漂移。
- **根据请求错误动态试 Native 或 Tool**：会增加请求数、费用和数据外发，并破坏资格与失败关闭合同。
- **把 task 资格全部交给 ModelProfile**：ModelProfile 不负责项目的语义质量、数据政策、预算或 Prompt/schema
  revision，因此不能替代项目资格门。

## 替代关系

- 细化 [ADR-0041](0041-qualify-opencode-go-tool-output-for-support-semantics.md) 的实现所有权：OpenCode Go 的
  Tool Output 结论不变，但由显式 Pydantic AI ModelProfile 表达；任务资格不再保存重复传输 profile。
- 继续遵守 [ADR-0037](0037-make-semantic-assessment-the-default-triage-path.md) 的一次请求、零重试、失败关闭，
  以及 [ADR-0038](0038-limit-semantic-assessment-remote-data-projection.md) 的最小出站投影。

## 落实与确认

- 通用语义 client 已删除 `output_mode` 参数，使用 `output_mode="auto"` 和已解析 ModelProfile；
- OpenCode Go 已显式声明 Tool Output、支持 tools 且不支持 Native JSON Schema；
- `QUALIFIED_SEMANTIC_TASKS` 只保存任务、schema、Prompt、隐私、预算和评测等项目资格维度；
- 单元测试覆盖 Native、Tool、未资格化 Prompted Output、OpenCode Go 显式 Profile、任务资格字段和零动态
  fallback。

## 相关文档

- [模型 Provider 支持矩阵](../architecture/model-provider-support.md)
- [架构总览](../architecture/overview.md)
- [支持入口分流](../architecture/flows/support-intake-routing.md)
