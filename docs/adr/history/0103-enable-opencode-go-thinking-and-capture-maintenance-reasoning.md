# ADR-0103：启用 OpenCode Go 思考并在显式维护诊断中保存 reasoning

| 状态 | 决策日期 |
|---|---|
| 已采纳 | 2026-08-21 |

## 当时遇到了什么

项目此前对 OpenCode Go `deepseek-v4-flash` 固定发送 `thinking.type=disabled`。真实教学运行只能从
工具调用和最终输出推断模型为什么重复导航或误读源码；即使维护者已经显式选择保存完整模型输出，
`ThinkingPart` 也会在诊断序列化时被丢弃。

网关实测还表明，仅发送 `thinking.type=enabled` 虽然不会报错，但不会产生 `reasoning_content`；同时发送
`reasoning_effort` 后才返回可见 reasoning。Pydantic AI 2.28 能把该字段映射为 `ThinkingPart`，并在带
工具的下一轮请求中原样回传，实际两轮工具 smoke 没有触发 DeepSeek 的缺失 reasoning 400。真实插件诊断
又表明 `max` 容易在已经形成合理候选后继续自循环并耗尽输出，因此最终默认挡位收敛为 `high`。

## 决定

1. 精确 OpenCode Go profile 固定使用 `thinking.type=enabled`、`openai_reasoning_effort=high`、
   `parallel_tool_calls=false`、`temperature=0` 和既有 tool choice；设置身份为
   `opencode-go-thinking-high-v1`。
2. 该设置由任务 runtime 和维护评测 target 共同复用。旧 non-thinking held-out 只属于旧 settings revision，
   semantic、Bug、guidance 和教学任务均不能把历史资格继承给当前 thinking 组合。
3. 只有维护者显式启用 `--capture-model-output` 时，诊断文件保存 assistant 的 `ThinkingPart` 正文、ID、
   signature、Provider 名和 Provider details，并继续保存可见文本、tool call/result 与 correction。
4. 普通生产 telemetry 继续只记录 part kind、字符数、用量和稳定错误码；不保存 reasoning、assistant 正文、
   Prompt 或源码。显式诊断仍不复制初始 system/user Prompt，但工具返回与 reasoning 都可能包含敏感信息，
   文件只能写入维护者选择的 Git ignored 本地路径。
5. 不承诺 reasoning 是正确、忠实或完整的因果解释。质量判断仍以实际输入、工具轨迹、Evidence 和最终输出
   为准；reasoning 只用于诊断模型如何形成候选。

## 带来的影响

- OpenCode Go 当前运行会产生更多 completion token、延迟和费用；`high` 相比 `max` 降低自循环风险，
  既有每任务 token、请求、成本与 timeout 止损继续生效。
- `support-semantic` 的 240-token 生产 factory 已用真实结构化 smoke 验证能在该设置下返回合格结果；这不是
  新 held-out，也不恢复旧 non-thinking 资格。
- 教学 Prompt、request 与 Evidence 合同还在变化，正式 held-out 必须在这些边界全部冻结后，以新的
  settings revision 独立运行。

## 没有采用的方案

- 不只发送 `thinking.type=enabled`；实测没有 reasoning。
- 不使用 `max` 作为默认挡位；真实 family 诊断曾把完整输出预算耗在重复思考中。
- 不把 `low` 或 `medium` 当成独立 DeepSeek 挡位；上游会将其映射到 `high`。
- 不从普通 production trace 保存思考正文，也不把 thinking 自动上传到报告。
- 不把 reasoning 当作 Evidence 或模型正确性的证明。

## 关系

- 部分替代 [ADR-0097](../0097-capture-complete-capability-model-output-in-explicit-maintenance-runs.md) 第 3 项：
  显式维护捕获现在包含 thinking；不保存初始 Prompt、只写本地忽略路径和生产默认脱敏的边界继续有效。
- 不改变 [ADR-0089](../0089-persist-redacted-pydantic-ai-agent-traces.md) 的普通生产 trace 合同。

## 相关文档

- [模型 Provider 支持矩阵](../../architecture/model-provider-support.md)
- [能力影子索引流程](../../architecture/flows/capability-shadow-index.md)
