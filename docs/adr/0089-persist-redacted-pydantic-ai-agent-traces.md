# ADR-0089：持久化脱敏的 Pydantic AI Agent 调用轨迹

| 状态 | 决策日期 |
|---|---|
| 已采纳；已实现 | 2026-08-17 |

## 背景

生产教学注释已经能在日志中标出插件、分析单元、阶段与粗粒度失败原因，但 `http`、`budget` 和
`output_validation` 仍不足以回答一次 Agent 实际发起了多少模型请求、是否发生工具调用或输出重试、哪一层
耗时，以及 token 用量如何累积。Pydantic AI 已原生提供 OpenTelemetry instrumentation；此前各 Agent
适配器却显式关闭它，也没有配置 span exporter，因此不存在可持久复盘的调用轨迹。

直接保存 `capture_run_messages()` 或完整 OpenTelemetry attributes 会复制 Prompt、插件源码、运行配置、模型
原始回答、工具参数与工具返回正文，明显扩大秘密和隐私面。只打开 instrumentation 而不配置 exporter 也不会
产生部署者能够找到的本地记录。

## 决策

1. 生产 Pydantic AI Agent 共用同一套项目级 `InstrumentationSettings`。语义分类、公开能力回答、教学注释、
   Bug assessment 与 B4 Agent step 都通过该设置产生 Agent run、模型请求和工具执行 spans；离线 B1 Direct
   Request 不在本轮范围。
2. instrumentation 固定使用 `include_content=false`、`include_binary_content=false` 和
   `include_model_request_parameters=false`。Triage 的 exporter 再执行一次字段白名单，只保存 trace/span/parent
   ID、span 名称与类型、起止时间、耗时、状态、Provider/model、token、费用、finish reason、工具名、稳定异常
   类型，以及 Triage 主动提供的 task、plugin module 和 capability ID。
3. 不持久化源码正文、Prompt / message history、模型原始回答、工具参数或返回正文、完整工具 schema、异常
   message / stacktrace、任意 metadata、配置值或环境变量。Pydantic AI 即使新增其他 attributes，也不会因为
   前缀相似而自动进入文件，必须显式加入白名单并重新评审。
4. 轨迹默认随已配置的模型 transport 启用，可用 `NBTRIAGE_AGENT_TRACE_ENABLED=false` 关闭。启用时固定写入
   `nonebot-plugin-localstore` 为本插件解析出的 data 目录下 `agent-traces.jsonl`；LocalStore 目录覆盖继续使用
   其统一配置，不新增任意文件路径配置。
5. JSONL 按大小轮转：活动文件 10 MiB、5 个备份，名义上最多约 60 MiB。当前保存全部脱敏 spans，不区分成功
   与失败采样；容量轮转不承诺精确保留 7 天或 14 天。将来接入支持 tail sampling 的 Collector / 后端时，才
   考虑成功采样、失败全留和按天保留。
6. 使用 OpenTelemetry SDK `BatchSpanProcessor` 异步批量导出，并在 NoneBot shutdown 时 flush / shutdown。
   exporter、LocalStore 或 telemetry 初始化失败只关闭轨迹并记录安全错误类型，不阻断插件启动或任何模型任务。
7. 原始内容调试不作为普通配置开放。以后若真实故障证明必须查看正文，应另行设计显式、短时、单任务或
   单插件范围的维护模式及删除协议；不能把生产默认切换为完整内容采集。

## 为什么这样选

- 复用 Pydantic AI 原生 Agent / model / tool spans，避免项目维护第二套调用链协议；
- 字段白名单让轨迹足以定位并发、重试、预算和 Provider 故障，同时不复制用于生成的敏感材料；
- LocalStore data 给部署者一个跨平台、可直接定位的本地文件，又不引入中央遥测或自动上传；
- 第一版全量保存小型结构化 spans 比本地实现尾采样更简单，轮转已经给出明确磁盘上限。

## 影响

- 启用模型 transport 的部署会新增一个本地轮转 JSONL；禁用模型或显式关闭 trace 时不解析路径、不创建文件；
- 日志仍负责即时告警，trace 负责串起一次 Agent run 内的模型请求、工具与用量，两者不会互相替代；
- trace 不保存足以重放模型调用的原始输入输出，因此它适合故障归因和容量分析，不适合逐字复现模型回答；
- 多进程部署仍需为每个进程提供独占 LocalStore data 目录，当前文件 sink 不是跨进程集中写协议。

## 替代关系

- 窄范围替代 [ADR-0018](0018-use-localstore-only-for-enabled-trial-audit-log.md) 第 8 条中“模型 trace 不写入
  LocalStore”的决定；ADR-0018 对 trial 审计、原始运行状态和自动遥测的其他限制继续有效。

## 落实与验证

- `nbtriage.agent_telemetry` 提供共享 Pydantic AI instrumentation、字段白名单 exporter、轮转与生命周期；
- `nonebot_plugin_triage.agent_telemetry_runtime` 只在模型已配置且 trace 启用时解析固定 LocalStore data 文件；
- 所有生产 Agent adapter 从同一运行时设置取 instrumentation，教学注释 run 额外绑定安全的 capability 上下文；
- 测试使用真实 Pydantic AI `TestModel` 验证 spans 可写、trace ID 可关联、Prompt / 模型输出 / 未批准 metadata /
  完整消息不落盘，并覆盖轮转、关闭时零路径解析和静态类型检查。

## 相关资料

- [Pydantic AI instrumentation](https://ai.pydantic.dev/capabilities/instrumentation/)
- [Pydantic AI Logfire 与 OpenTelemetry](https://ai.pydantic.dev/logfire/)
- [OpenTelemetry GenAI semantic conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/)
