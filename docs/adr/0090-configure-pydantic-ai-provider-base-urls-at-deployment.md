# ADR-0090：在部署端配置 Pydantic AI Provider 地址

| 状态 | 决策日期 |
|---|---|
| 已采纳；已实现 | 2026-08-17 |

## 背景

Pydantic AI 使用 `provider:model` 识别 Provider 与模型，例如 `alibaba:qwen-max`。同一个 Provider
可能因区域、私有网关或本地服务而使用不同 API 地址；中国大陆百炼与国际站 DashScope 就是一个实际例子。
如果 Triage 为每个区域继续增加 `alibaba-cn:` 一类项目私有前缀，就会复制 Pydantic AI 的 Provider 注册表，
并迫使项目长期维护厂商、区域与地址组合。

另一种极端是把任意 URL 当作新的 OpenAI-compatible Provider。这样会丢失原 Provider 的 `ModelProfile`、
模型能力修正和密钥约定，也会把 Triage 扩张为网关或协议兼容层。Pydantic AI 2.28.0 已提供公开的
`infer_model(..., provider_factory=...)`，允许应用继续使用标准 Provider，同时只改变 Provider 的构造参数。

## 决策

1. `NBTRIAGE_MODEL_BACKEND=pydantic-ai` 继续要求 `NBTRIAGE_MODEL_NAME` 使用 Pydantic AI 标准的
   `provider:model`。不再维护 `alibaba-cn:` 等项目私有 Provider 前缀。
2. 新增可选的 `NBTRIAGE_MODEL_BASE_URL`。它只覆盖所选 Pydantic AI Provider 的部署端地址，不改变
   Provider 名称、模型名称、API 族或 `ModelProfile`。Triage 通过 Pydantic AI 原生
   `infer_model(..., provider_factory=...)` 构造 Provider，不改用通用 OpenAI 模型冒充原 Provider。
3. Base URL 只能来自部署者信任的 NoneBot 配置。模型、Prompt、RAG、插件源码和只读文件工具都不能选择或
   修改它；专用 backend 不接受该字段。Provider 构造器不支持 `base_url` 时失败关闭，不静默忽略。
4. 地址必须是有界的 HTTP(S) URL，不得包含用户名、密码、query 或 fragment。外部地址必须使用 HTTPS；
   HTTP 只允许 `localhost` 或字面 loopback 地址。字面 link-local、unspecified、multicast 和 reserved 地址拒绝。
5. API Key 仍不进入 `NBTriageConfig`，只按所选 Provider 的标准进程环境变量读取。Base URL 不能携带凭据，
   也不能代替 Provider 的密钥配置。
6. 自定义地址不继承默认地址或其他地址的 held-out 质量结论。它可以按 ADR-0086 正常运行，但标记为未验证；
   若以后发布精确质量结论，连接地址 revision 必须成为该结论的一部分。
7. 规范化地址的 SHA-256 身份进入教学注释 generation revision 和脱敏 Agent trace。更换地址会使教学缓存
   失效；轨迹能够区分连接，但不保存完整地址。API Key、URL 正文和请求内容仍不进入轨迹。
8. 本决定不增加 Provider 自动发现、模型列表同步、路由、fallback、负载均衡或集中密钥管理，也不引入
   LiteLLM 等网关依赖。若以后需要这些能力，应把它们作为独立网关边界重新决策。

## 为什么这样选

- Pydantic AI 继续拥有 Provider 注册、模型 profile 和官方依赖，Triage 不维护第二套厂商枚举；
- 地址与 Provider 身份分离，既能使用区域 endpoint，也不会因“协议兼容”丢失原 Provider 语义；
- 显式部署配置比不断增加区域专用环境变量和项目私有模型前缀更稳定；
- 地址哈希足以隔离缓存和定位调用链，又不扩大本地轨迹中的敏感配置面；
- 保留失败关闭和未验证标签，避免把“可以连接”误写成“已经验证质量与兼容性”。

## 影响

- 中国大陆百炼使用 `alibaba:qwen-max`，另将
  `NBTRIAGE_MODEL_BASE_URL` 设为 `https://dashscope.aliyuncs.com/compatible-mode/v1`；
- 使用 Provider 默认地址时不配置 Base URL，现有 `provider:model` 行为保持不变；
- 从预发布的 `alibaba-cn:<model>` 配置迁移时，需要改回 `alibaba:<model>` 并新增 Base URL；不保留兼容别名；
- 自定义地址或 Provider 初始化失败只使模型增强不可用，不阻断插件导入和确定性能力索引。

## 替代关系

- 窄范围替代 [ADR-0008](0008-pydantic-ai-controlled-model-adaptation.md) 中运行时只能使用固定 endpoint 的部分；
  Provider / Model / Profile 分层、参数门和失败关闭继续有效；
- 窄范围替代 [ADR-0011](0011-expose-disabled-qualified-model-configuration.md) 与
  [ADR-0037](0037-make-semantic-assessment-the-default-triage-path.md) 中禁止 custom Base URL 的部分；密钥、
  惰性客户端、默认 assessment、零自动重试与启动降级边界继续有效；
- 补充 [ADR-0086](0086-treat-model-evaluation-as-a-quality-label.md)：自定义连接可运行但默认未验证；
- 补充 [ADR-0089](0089-persist-redacted-pydantic-ai-agent-traces.md)：轨迹只新增安全的连接地址哈希身份。

## 落实与验证

- `nonebot_plugin_triage.config` 规范化并校验可选 Base URL，Pydantic 错误隐藏输入值；
- `nonebot_plugin_triage.task_model_runtime` 通过 Pydantic AI Provider factory 构造标准 Provider，并生成不含
  URL 正文的连接 revision；
- 教学注释 revision 与 Agent telemetry resource 都绑定连接 revision；
- 测试覆盖百炼国内地址、默认连接、危险地址拒绝、密钥缺失、缓存隔离和轨迹脱敏。

## 参考

- [Pydantic AI Models and Providers](https://ai.pydantic.dev/models/overview/)
- [Pydantic AI Alibaba Provider](https://ai.pydantic.dev/models/alibaba/)
- [Pydantic AI `infer_model`](https://ai.pydantic.dev/api/models/base/#pydantic_ai.models.infer_model)
