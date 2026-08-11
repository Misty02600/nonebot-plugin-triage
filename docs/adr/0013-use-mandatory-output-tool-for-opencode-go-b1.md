# ADR-0013：不为一次 OpenCode Go 测试改变 B1 输出契约

| 状态 | 提议日期 | 未采纳日期 |
|---|---|---|
| 未采纳 | 2026-08-09 | 2026-08-09 |

## 背景

为使用维护者本机已有的 OpenCode Go API 测试真实 B4 tool calling，项目增加了一个 evaluation-only Chat
adapter。一次 native JSON Schema 能力探测返回 HTTP 400，随后曾把“为 Go B1 改用唯一不可执行的终端
输出工具”提升为 Proposed ADR。

维护者随即澄清：OpenCode Go 只用于当前测试，不代表产品 Provider、模型网关或发布支持方向；网关选择
可以在以后出现实际需求时再调整。因此原提案的产品前提不存在。

## 决定

当前不改变 ADR-0008 的 B1 native JSON Schema 与零 output tools 契约，不为 OpenCode Go 增加 B1 factory，
也不把这次测试接入插件配置、资格表或产品路由。OpenCode Go adapter 只作为仓库内 evaluation tooling；
其结果必须标记为探索性，不能用于插件晋级。

这不是对 terminal output tool 技术方案的永久否定。未来若产品确实需要选择网关、支持 Chat-only Provider
或建立新的 B1 cohort，应根据当时需求和证据另立 ADR；不复用本编号，也不把本次测试推导为既有决定。

## 影响

- B4-only Go 测试不再被 Go B1 输出契约阻塞；
- native-schema HTTP 400 只保留为该测试 endpoint 的能力事实；
- Go B4 测试可以独立运行，但不得声称与现有同 Provider/model B1/B3 Gate 等价；
- 公开 optional extras、NoneBot backend、资格表和支持矩阵不增加 OpenCode Go 产品入口。

## 相关证据

- [模型 Provider 支持矩阵](../architecture/model-provider-support.md)保留人工复核后的探索结论；完整机器记录仅在维护者本地保留
- [ADR-0008：采用 Pydantic AI 的受控模型适配层](0008-pydantic-ai-controlled-model-adaptation.md)
- [ADR-0012：让 Pydantic AI Deferred Tools 位于领域 Agent runtime 之后](0012-use-pydantic-ai-deferred-tools-behind-domain-runtime.md)
