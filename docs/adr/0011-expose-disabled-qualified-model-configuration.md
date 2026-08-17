# ADR-0011：公开默认关闭且按资格门装配的模型配置

> 后续关系：ADR-0086 已把 held-out 评测改为质量标签，不再作为模型运行许可；ADR-0090 已窄范围替代禁止
> 任意 base URL 的决定，改为标准 Provider 上受限的部署端地址覆盖。本 ADR 的密钥、transport 身份与惰性
> 客户端边界继续有效。

## 状态

部分被 [ADR-0037](0037-make-semantic-assessment-the-default-triage-path.md) 替代

## 日期

2026-08-09

## 当时遇到了什么

OpenAI Responses 与 Anthropic Messages 已有相互隔离的 optional extras 和离线 adapter，但 NoneBot 插件
没有模型配置或生命周期边界。若由 Matcher 临时读取环境变量和构造客户端，Provider 选择、密钥、依赖、
预算和支持资格会分散到事件路径；若继续只保留 CLI，则后续插件诊断没有稳定的公开装配契约。

模型配置是发行后的兼容 API。允许任意 Provider/API 拼接、base URL、实验性组合或长期累计调用客户端，
都会绕过 ADR-0008 的逐组合准入和单步骤预算。

## 最后决定

1. `NBTriageConfig` 公开 `nbtriage_model_enabled`、`nbtriage_model_backend`、
   `nbtriage_model_name`、`nbtriage_model_timeout_seconds` 与
   `nbtriage_model_max_output_tokens`；功能默认关闭；
2. backend 使用完整稳定 ID `openai-responses` 或 `anthropic-messages`，不把 Provider 与 API 族开放为
   任意组合；模型 ID 必须显式给出；
3. API Key 只从 `OPENAI_API_KEY` 或 `ANTHROPIC_API_KEY` 读取。`NBTriageConfig` 明确拒绝 API Key 与
   custom base URL 字段，并隐藏验证输入，避免异常文本回显秘密；
4. 禁用时不导入 Provider extra、不读取密钥、不构造 SDK 客户端。安装 optional extra 本身不启用模型；
5. 启用时先核对 code-level `backend + model` 资格注册表，再解析 factory 和密钥。只有支持矩阵状态为
   “支持”且完成插件运行门的精确组合才能登记；当前 OpenAI/Anthropic 均为实验性，所以注册表为空；
6. `NBTriagePluginRuntime` 持有 `NBTriageModelService` 的惰性 client factory，不持有一个会累计耗尽
   `max_calls` 的长期客户端。每个模型 step 创建新客户端并固定 `max_calls=1`；
7. 会话级 turn、tool、token、deadline 与费用预算不在本 ADR 预先公开，由 ADR-0010 的 Agent Gate 和
   后续精确报障集成评审根据评测冻结；
8. 本决定只装配对象，不授权 `handle_report`、`handle_query` 或其他 Matcher 调用模型。

## 为什么这样选

- 默认禁用保持基础安装与现有纯规则入口完全不变；
- 单一 backend ID、空资格表和无 custom endpoint 防止兼容标签绕过逐组合支持矩阵；
- 密钥不进入 Pydantic 配置，使 dump、repr、metadata 和普通配置日志天然不包含秘密；
- step-client factory 同时满足长期 Bot 生命周期和单步骤零重试/一次请求，不把 CLI 评测的总调用计数误用
  为整个 Bot 进程预算；
- 先公开窄配置能稳定后续插件接口，但没有线上证据时仍不能产生真实请求。

## 没有采用的方案

- **继续只保留 CLI**：公开面更小，但会把插件装配问题推迟到诊断入口实施时；
- **任意 OpenAI-compatible base URL**：无法继承 native schema、存储、重试和数据处理资格；
- **允许实验性组合显式 opt-in**：会把未完成的线上门转嫁给部署者，不作为首次插件契约；
- **在 runtime 保存单个 `PydanticAIB1Client`**：它的 `max_calls` 是有限评测生命周期预算，不适合长期 Bot；
- **把 API Key 放进 NoneBot 配置**：会增加 dump、日志、metadata 和错误回显面。

## 带来的影响

- 有利：插件拥有可验证、可扩展但不越权的模型装配边界；
- 代价：每次模型步骤创建厂商客户端，后续如需连接复用必须在不破坏 step 预算的前提下单独测量和设计；
- 风险：公开配置名需要长期兼容，未来变更必须迁移或新增 ADR；
- 当前行为：所有真实组合都会在启动资格门失败，直到某个固定 Provider/API/model 组合完成获授权资格验证。

## 落实与确认

- 2026-08-09：维护者确认公开窄配置、默认关闭、环境变量密钥、无 custom endpoint 与每 step 单请求边界；
- 实施情况：已落实于 `NBTriageConfig`、`NBTriageModelService` 和 `create_plugin_runtime`；
- 验证：默认隔离、字段/预算拒绝、秘密不回显、资格顺序、extra/key 错误、fake factory 生命周期、
  Python 3.11–3.14 与三种 wheel 安装均通过。

## 相关文档

- [ADR-0008：采用 Pydantic AI 的受控模型适配层](0008-pydantic-ai-controlled-model-adaptation.md)
- [ADR-0010：用有界证据获取循环验证 Agent 能力](0010-use-bounded-evidence-seeking-agent-loop.md)
- [ADR-0037：把语义 assessment 作为 triage 的正式默认路径](0037-make-semantic-assessment-the-default-triage-path.md)
- [模型 Provider 支持矩阵](../architecture/model-provider-support.md)
