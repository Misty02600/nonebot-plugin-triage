# ADR-0044：语义 assessment 直接使用 Pydantic AI Agent output_type

| 状态 | 决策日期 |
|---|---|
| 已采纳 | 2026-08-13 |

## 当时遇到了什么

语义 assessment 已经使用 Pydantic AI `ModelProfile` 判断模型是否支持 tools、Native JSON Schema 以及
默认结构化输出方式，但通用 client 仍自行构造 `OutputObjectDefinition`、`ToolDefinition`、
`ModelRequestParameters`，并手工解析 Text / ToolCall part 后再调用领域 parser。这里重复实现了 Pydantic AI
2.27.0 已经提供的结构化输出编排。

Pydantic AI `Agent(output_type=<Pydantic model>)` 会把 Pydantic model 转为 `AutoOutputSchema`，在请求前通过
`ModelProfile.default_structured_output_mode` 选择 Native、Tool 或 Prompted Output，并使用同一个 Pydantic
model 校验最终结果。继续维护项目内平行 schema、output tool 名称和响应 part 解析，会形成第二个框架能力
实现，也让库升级时更容易漂移。

## 决策

1. `support-semantic-v4` 直接构造
   `Agent[object, SupportSemanticAssessment](output_type=SupportSemanticAssessment)`。领域 Pydantic model 同时是
   Agent 最终输出类型和唯一结构化校验合同。
2. 结构化输出形式继续由 Pydantic AI `ModelProfile` 在请求前决定。项目不再自行构造
   `OutputObjectDefinition`、`ToolDefinition` 或解析 Text / ToolCall part，也不维护 output tool 名称。
3. OpenCode Go 的显式 Profile 保持 `supports_tools=true`、`supports_json_schema_output=false`、
   `default_structured_output_mode="tool"`。因此 Pydantic AI Agent 为本轮生成一个 output tool；它只是框架的
   最终结构化返回通道，不是可执行的业务工具。
4. Agent 仍然没有依赖、业务 tools、内置 tools、MCP、handoff 或持久消息历史。每轮最多一次 Provider 请求，
   output / tool correction retry 与 SDK retry 都为零，instrument 关闭，失败后不切换输出方式或模型。
5. 项目只保留 Pydantic AI 不负责的边界：闭合出站请求投影、精确 task qualification、Prompt / schema /
   evaluation revision、隐私、预算、Provider / model 身份、usage / finish 审计，以及模型外确定性 routing 和
   副作用授权。
6. 当前 task 只资格化 Native 与 Tool Output。若 Profile 选择 Prompted Output，仍在发起请求前失败；以后需
   独立评测和决定，不能静默回退。
7. 由于 output tool 由项目手写名称改为 Pydantic AI Agent 生成，既有线上资格需要重新跑 held-out。新 Gate
   必须继续满足 schema 合法率与 status 准确率 100%、全部字段精确匹配至少 90%。

## 原因与影响

- 项目不再复制 Pydantic AI 的结构化输出 schema、tool 定义、模式选择和结果解析；
- `SupportSemanticAssessment` 字段变化会由 Pydantic model 自动传播到 Agent 的 Native / Tool schema，减少
  双份结构漂移；
- 采用 Agent 不等于开放 Agent 自主循环。一次请求、零业务工具、零重试和模型外副作用授权仍是硬边界；
- B1 的原生 JSON Schema Direct Request 继续保持原决定。本 ADR 只改变支持入口语义 assessment 的实现；
- 新 Provider 仍必须先核对锁定版本 Pydantic AI 的原生能力，再只为项目特有边界写薄适配。

## 没有采用的方案

- **继续维护手写 Direct Request 输出定义**：功能可用，但重复 Pydantic AI 的 Agent output schema 与结果校验，
  并要求项目维护 tool 名称和 part 解析。
- **新增 `SemanticTransportCapabilities`**：会复制 `ModelProfile` 的 supports flags 与默认输出模式，已由
  ADR-0042 排除。
- **把模型输出直接当 routing action**：Pydantic 校验只能证明结构合法，不能替代身份、证据、披露和
  incident 副作用授权。
- **允许 Agent 自动重试纠正输出**：会增加数据外发、请求数和费用，也会破坏既有单请求资格。

## 替代关系

- 部分替代 [ADR-0042](0042-use-pydantic-ai-model-profile-for-structured-output.md) 的手写 Direct Request
  输出定义；`ModelProfile` 作为传输能力唯一真相源和 Prompted Output 未资格化的决定继续有效。
- 仅对支持入口语义 assessment 部分替代
  [ADR-0008](0008-pydantic-ai-controlled-model-adaptation.md) 的“不采用 Agent”实现选择；B1 Direct Request、
  Provider / Model / Profile 分层和零业务工具边界不变。
- 继续遵守 [ADR-0037](0037-make-semantic-assessment-the-default-triage-path.md) 的默认 assessment、一次请求、
  零重试与失败关闭，以及 [ADR-0038](0038-limit-semantic-assessment-remote-data-projection.md) 的最小出站投影。

## 落实与确认

- `PydanticAISupportSemanticClient` 已改为以 `SupportSemanticAssessment` 作为 Agent `output_type`；已删除手写
  output object、output tool、模式解析和响应 part 解析；
- 离线测试分别验证 Native Profile 产生 Native schema、OpenCode Go Profile 产生 Pydantic AI `final_result`
  output tool，且两者都没有业务 function / native tools；
- 假 HTTP 测试验证 OpenCode Go wire 仍为 required 单 output tool、一次请求、零 SDK retry 和闭合请求投影；
- 2026-08-13 重新运行全新 36 条 held-out：schema 合法率 1.000、status 准确率 1.000、全字段精确匹配
  1.000、失败 Case 0；46,409 input / 4,094 output token，归一费用 1,340 microUSD；资格 revision 更新为
  `opencode-go-heldout-36-20260813-v4-agent-output-type`。

## 相关文档

- [模型 Provider 支持矩阵](../architecture/model-provider-support.md)
- [架构总览](../architecture/overview.md)
- [支持入口分流](../architecture/flows/support-intake-routing.md)
