# ADR-0041：准入 OpenCode Go 工具输出式语义 assessment

> 后续关系：ADR-0086 保留本 ADR 的精确 held-out 作为公开质量证据，但取消其运行白名单职责；其他模型
> 组合可在相同 schema、隐私、预算和模型外路由合同下运行，并标记为未验证。

| 状态 | 决策日期 |
|---|---|
| 已采纳；output tool 定义由 ADR-0044、独立安装 extra 由 ADR-0047 替代 | 2026-08-13 |

> [ADR-0043](0043-separate-support-goals-observations-and-maintenance-depth.md) 已替代本 ADR 的 v2 schema、
> Prompt 与资格评测 revision；Provider、API、model、Profile、Tool Output、隐私、预算、一次请求和
> 零重试边界继续有效。

> 结构化输出能力与任务资格的所有权已由
> [ADR-0042](0042-use-pydantic-ai-model-profile-for-structured-output.md) 细化：Tool Output 结论不变，
> 但传输能力只由 Pydantic AI `ModelProfile` 表达，项目资格不再重复保存传输 profile。

> [ADR-0044](0044-use-pydantic-ai-agent-output-type-for-support-semantics.md) 已替代项目手写
> `return_support_semantic_assessment` 名称和 output tool 定义；OpenCode Go 使用 Tool Output、一次请求、
> 零重试和零业务工具的资格边界继续有效。

> [ADR-0047](0047-reuse-pydantic-ai-provider-extras.md) 已删除独立 `model-opencode-go` extra；OpenCode Go
> 当前复用 `openai` extra。这里保留的旧名称是当时的实施历史，不再是当前安装接口。

## 当时遇到了什么

[ADR-0037](0037-make-semantic-assessment-the-default-triage-path.md) 要求每轮 `triage` 走受限语义
assessment，并曾把 Provider 原生 JSON Schema 与零 output tool 写成唯一传输形式。OpenCode Go 的
OpenAI-compatible Chat endpoint 对 `deepseek-v4-flash` 不支持 native JSON Schema，但支持单一 function
tool call。这个模型不能因此继承 B1 或 B4 的资格，也不能通过提示词 JSON 降级进入生产。

用户已明确授权使用本机 OpenCode Go API，并允许在数美元内完成纯合成资格评测。语义 assessment 的既有
出站合同仍只允许当前单条规范化请求文字，不允许 Reply、身份、配置、日志、源码、运行证据或 restricted
内容出站。

## 决策

1. 只为 `support-semantic-v2` task 准入精确组合：OpenCode Go、Chat Completions、
   `deepseek-v4-flash`、non-thinking、60 秒单请求 timeout、240 output token、
   `support-semantic-v2-prompt-v1`。
2. 该组合可以暴露且强制调用一个 `return_support_semantic_assessment` output tool。它只是结构化返回通道：
   应用不执行函数、不提供 function/native/external/MCP 工具，也不启动 Agent loop。
3. 每轮最多一次 Provider 请求；SDK 与应用自动重试均为零，不 fallback 到文本 JSON、其他模型、词表或
   第二次请求。timeout、transport failure、缺少唯一 tool call 或本地 schema 校验失败都按本轮 abstain。
4. Provider 不支持 strict tool definition，因此 wire 使用 `strict=false`；返回参数仍必须通过闭合 tool schema
   和领域 `SupportSemanticAssessment v2` 二次校验。为规避该模型把 JSON `null` 输出成字符串的问题，wire
   只接受闭集 reason，其中 `none` 在适配层唯一映射为领域 `None`；任意其他非法值拒绝。
5. 资格键绑定 Provider、API 族、精确模型、运行 profile、task schema 和 Prompt revision。B1、B4 或其他
   Prompt 的结果不能复用；任一维度变化都必须重新通过 held-out 资格门。
6. 版本化 development 集只用于改进 Prompt，不具备资格效力。未写入 Prompt 的 24 条纯合成 held-out 集要求
   schema 合法率 100%、status 准确率 100%、严格多标签匹配至少 90%、未知 usage/cost 为 0。
7. 2026-08-13 的 held-out 运行达到 24/24 严格匹配，24 次请求共 27,890 input / 2,391 output token，
   归一费用 1,215 microUSD；response ID 24/24 存在，fingerprint 0/24 存在。完整机器报告只保存在忽略的
   `reports/`，仓库只版本化评测合同与本决定中的人工复核摘要。
8. `OPENCODE_API_KEY` 只从进程环境读取；插件配置只选择已准入 backend/model/profile，不接受 API key 或
   custom base URL。安装者需显式安装 `model-opencode-go` extra。

## 原因与影响

- output tool 解决的是 Provider 结构化输出能力差异，不给模型任何可执行能力；
- 强制唯一 tool call 后 development 与 held-out 均达到 100% schema 合法率；
- held-out 与 Prompt 开发集分离，避免用已经写进 Prompt 的反例虚构资格；
- `deepseek-v4-flash` 是滚动别名，当前资格只代表这次精确 task/profile 实测；返回 fingerprint 缺失会限制
  后续可复现性，未来重新评测时必须记录日期、response ID、实际返回模型和可用 fingerprint；
- 没有配置 transport 时 assessment 仍走 unavailable service 并 abstain；这不是恢复产品启用开关。

## 没有采用的方案

- **继续要求 native JSON Schema**：该精确 endpoint 已返回不支持，无法形成可运行产品路径。
- **提示词要求输出 JSON 文本**：服务端不约束结构，失败模式更难与普通文本区分。
- **把 output tool 当作普通函数执行**：语义 assessment 不需要也不授权工具副作用。
- **直接使用 development 集晋级**：Prompt 已吸收其中失败样例，会产生评测泄漏。
- **复用 B4 的测试 transport 资格**：B4 action 选择与 support semantic 多标签输出是不同 task。

## 替代关系

- 仅在 `support-semantic-v2` 的 OpenCode Go 组合上部分替代
  [ADR-0037](0037-make-semantic-assessment-the-default-triage-path.md) 第 6 条的“原生 JSON Schema、零 output
  tool”传输限制；其默认 assessment、零可执行工具、零重试、失败关闭与模型外副作用授权继续有效。
- 不改变 [ADR-0013](0013-use-mandatory-output-tool-for-opencode-go-b1.md) 对 B1 的未采纳结论；本决定属于不同
  task、schema、Prompt 与资格门。
- 继续遵守 [ADR-0038](0038-limit-semantic-assessment-remote-data-projection.md) 的最小出站投影和
  [ADR-0040](0040-require-trusted-preflight-failure-before-incident.md) 的模型外 incident 条件。
- 传输能力与项目任务资格的所有权由
  [ADR-0042](0042-use-pydantic-ai-model-profile-for-structured-output.md) 细化。

## 落实与确认

- wheel 内已有 OpenCode Go Chat adapter、task 级资格表、语义 service factory 与 runtime 接线；
- `model-opencode-go` extra 固定 OpenAI SDK 与 Pydantic AI 版本；
- 假 HTTP 测试核对 endpoint、non-thinking、required 单一 output tool、零 retry、请求投影与本地二次校验；
- development / held-out 评测器逐条审计 schema、identity、usage 与归一费用；单条失败不会丢弃已付费审计，
  但任何结构、状态、费用或 usage 缺口都会让资格门失败；
- 本机部署已选择该精确 backend/model/profile；密钥仍只来自既有进程环境，未写入仓库或测试快照。

## 相关文档

- [模型 Provider 支持矩阵](../architecture/model-provider-support.md)
- [支持入口分流](../architecture/flows/support-intake-routing.md)
- [跨平台 triage 支持入口](../architecture/flows/cross-platform-report-intake.md)
- [架构总览](../architecture/overview.md)
