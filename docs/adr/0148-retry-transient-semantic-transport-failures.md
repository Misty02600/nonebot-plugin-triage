# ADR-0148：语义 assessment 对瞬时传输故障进行一次有界重试

| 状态 | 决策日期 |
|---|---|
| 已采纳 | 2026-09-17 |

## 问题

[ADR-0037](0037-make-semantic-assessment-the-default-triage-path.md) 决策 6 规定“项目与 SDK 自动重试均为零”，
决策 7 把请求期超时、Provider 错误和非法输出统一收敛为本轮 abstain。真实使用中，Provider 对同一请求返回
429 或 5xx 通常只是瞬时繁忙，一次重试即可成功；而普通用户失败后只能手动重发，还会被入口冷却
（[ADR-0147](0147-use-five-minute-entry-cooldown-with-superuser-exemption.md)）挡住。零重试把一次瞬时故障
放大为整轮报废且无法立即补救。

## 决策

- 语义 assessment 服务（`SemanticAssessmentService`）对**传输期可恢复故障**允许且只允许一次重试：异常链中
  出现 Pydantic AI `ModelHTTPError` 且状态码为 429 或 5xx 时，创建新的 client 重试一次（默认退避 1 秒）。
- 不重试的失败类别：客户端自身整体预算超时（`asyncio.TimeoutError`）、HTTP 4xx（含 408）请求拒绝、
  结构 / 领域非法输出（结构修复仍由模型客户端内的一次 output retry 承担）、模型前策略与凭据命中、未配置
  transport。
- 两次尝试共享同一个总预算（`NBTRIAGE_MODEL_TIMEOUT_SECONDS`），不因重试把最坏等待时间翻倍。失败类别
  只按异常链的类型与状态码判断，不读取 provider 错误消息，也不把请求正文、目录或配置写入日志。
- 预算资格 profile 从 `joint-structure-repair-once-v1` 更新为
  `joint-structure-repair-and-transport-retry-v1`，旧资格不能平移给带传输重试的新行为。

## 原因与影响

- 语义 assessment 是一切后续动作的准入闸，瞬时故障直接报废整轮求助；一次有界重试能显著提升完成率，
  成本上限是额外一次请求，且 429 / 5xx 时 Provider 通常未计费。
- 429 / 5xx 是“服务器明确表示暂不可用”的信号，重试语义明确；超时重试无收益且会加倍等待，4xx 重试会
  掩盖真实配置或鉴权问题，故都排除。
- 诊断已就绪：`http_diagnostics.py` 的 attempt 索引会自动为第二次尝试记录 `attempt_index=2`，生命周期事件
  与 trace 无需改动；服务层新增“重试已调度”与最终 abstain 的结构化日志（只含异常类名链）。

## 没有采用的方案

- **在 httpx 传输层统一重试**：会给 Bug assessment、维护者对话和公开教学的全部模型请求无差别加重试，突破
  它们各自显式的请求 / 证据预算账本，不可接受。
- **重试超时与连接错误**：共享预算内重试整体超时会把最坏等待翻倍，且超时通常意味着服务级问题；连接类
  错误目前没有高频失败的证据，先不扩大范围。

## 替代关系

- 窄范围部分替代 [ADR-0037](0037-make-semantic-assessment-the-default-triage-path.md) 决策 6、7 与
  “每轮最多调用 client 一次且不重试”的落实条款。abstain、失败关闭、不建立 incident、不猜测意图、
  “不重放用户请求”继续有效，只允许 429 / 5xx 的一次有界**传输**重试。

## 落实与确认

- `SemanticAssessmentService` 新增 `max_transport_attempts`（默认 2 次尝试，即 1 次重试）与
  `retry_backoff_seconds`（默认 1.0）构造参数；按异常链分类是否重试。
- 单测覆盖：429 / 5xx 重试后恢复、持续 5xx 恰好两次尝试后失败关闭、4xx / 408 不重试、整体预算超时与
  通用异常不重试，以及既有“每次成功只调用一次 client”的投影合同。
- `SUPPORT_SEMANTIC_BUDGET_PROFILE` 与维护者评测工具同步更新；入口冷却、Thread 生命周期和确定性路由不变。