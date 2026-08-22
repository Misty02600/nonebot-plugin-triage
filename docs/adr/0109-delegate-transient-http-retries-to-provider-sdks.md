# ADR-0109：把瞬时 HTTP 重试交给 Provider SDK

| 状态 | 决策日期 |
|---|---|
| 已采纳 | 2026-08-21 |

## 背景

教学注释原先在项目层对 timeout、transport、429 和 5xx 重新运行整个 Agent 单元。一次网络故障可能因此重放
已有对话、工具调用和结构化生成；同时 SDK 重试被关闭，无法复用其对连接错误、限流响应和 `Retry-After` 的原生处理。

## 决定

1. 项目显式构造的 OpenAI-compatible 与 Anthropic SDK 客户端统一设置 `max_retries=2`，即首次请求失败后最多
   再进行两次传输级尝试。
2. 教学注释不再因 timeout、transport、429 或 5xx 重启整个 Agent run。项目层只保留结构化输出 / 公开投影的
   correction；业务请求、工具调用和模型语义失败不由网络策略重放。
3. 显式维护诊断在 SDK HTTP 边界记录每次失败尝试的状态码、允许响应头与有界脱敏正文；生产 trace 仍不保存正文。
4. SDK 内部尝试不伪装成新的教学单元 attempt。若所有传输尝试均失败，最终稳定错误仍按原单元失败原因记录。

## 影响

- 瞬时网络错误由依赖库的原生退避与状态码策略处理；
- Agent 工具轨迹不会因为一次 HTTP 故障从头执行；
- 维护者能区分同一次逻辑模型请求中的失败尝试与最终成功响应；
- Provider 可能未返回 usage 的失败尝试仍无法精确计算 token 或费用，诊断不得据此伪造零成本。

## 替代关系

- 更新 [ADR-0107](0107-capture-provider-http-errors-in-explicit-maintenance-diagnostics.md) 中“诊断不改变重试”的旧边界：
  捕获范围扩展到 SDK 内部重试，生产隐私边界不变。
- 不改变 [ADR-0096](0096-bound-capability-annotation-concurrency-by-unit.md) 的 teaching unit 并发上限。
