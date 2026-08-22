# ADR-0107：在显式维护诊断中保存 Provider HTTP 错误

| 状态 | 决策日期 |
|---|---|
| 已采纳 | 2026-08-21 |

## 背景

显式单插件诊断已经保存 assistant 文本、thinking、工具往返和成功 Provider 响应，但失败 HTTP 调用
只留下稳定错误码。维护者无法区分网关错误、请求限流或上游服务故障。

## 决定

1. 只有显式启用 `--capture-model-output` 时，捕获 Pydantic AI 暴露的 `ModelHTTPError`：请求序号、
   HTTP 状态、模型名、重试等待时间、少量追踪/限流响应头，以及响应正文。
2. 不保存请求体、认证头或 Cookie。响应正文中的敏感键和疑似凭据先脱敏，单条正文最多保存 16,384 字符。
3. 普通生产 trace、公开报告与教学缓存继续不保存 HTTP 正文。Provider 没有返回正文或 request ID 时不猜测。
4. 本地诊断文件 schema 升为 3；它仍只能写入维护者显式选择的 Git ignored 路径。

## 影响

- HTTP 失败可以与同一单元的成功响应按请求序号对应，减少事后猜测；
- 诊断文件仍可能含上游错误细节，继续按敏感本地工件管理；
- 该诊断不改变重试、发布、Provider 资格或模型质量结论。

## 替代关系

- 接续 [ADR-0097](0097-capture-complete-capability-model-output-in-explicit-maintenance-runs.md) 的显式维护捕获，
  不改变其生产默认隐私边界。
