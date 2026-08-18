# ADR-0092：删除旧模型 backend 配置兼容

| 状态 | 决策日期 |
|---|---|
| 已采纳；已实现 | 2026-08-18 |

## 背景

[ADR-0091](0091-use-pydantic-ai-model-ids-as-the-public-transport-selector.md) 已把 Pydantic AI
`provider:model` 确立为公开的 transport 选择器，但为旧部署保留了 `NBTRIAGE_MODEL_BACKEND`、四条专用
backend 分支和 OpenCode Go 的 `OPENCODE_API_KEY` 回退。继续保留这些路径会形成两套可以表达同一连接的
配置语法，使校验、错误提示、测试和文档继续承担已无产品价值的分支；插件 runtime 还持有一个没有生产消费方
的旧 B1 `model_service`。

项目尚未发布需要无缝升级的稳定 backend 配置合同，部署配置可以直接迁移。与继续隐藏兼容相比，明确拒绝旧
字段更容易暴露遗漏，也能确保后续 Provider 只通过依赖库原生抽象接入。

## 决策

1. `NBTRIAGE_MODEL_NAME=<provider>:<model>` 是唯一公开的模型 transport 选择器。只要配置了 model，就必须
   包含 Pydantic AI Provider 前缀；不再从 backend 或 URL 推断 API 族。
2. 删除 `NBTRIAGE_MODEL_BACKEND` 字段及 `opencode-go-chat`、`openai-responses`、
   `anthropic-messages`、`pydantic-ai` 四个旧值的运行分支。配置输入中出现该字段会立即报错，并给出
   `provider:model` 迁移提示；不会静默忽略，也没有弃用窗口。
3. 删除插件 runtime 中未被生产流程使用的 `NBTriageModelService`、`model_service` 和相应 factory。
   任务模型统一由 `task_model_runtime` 按需构造，语义、Bug、公开回答和教学注释继续拥有各自的任务合同。
4. `NBTRIAGE_MODEL_BASE_URL` 仍是可选连接覆盖，必须与 `NBTRIAGE_MODEL_NAME` 一同使用，并继续遵守 HTTPS、
   loopback、无凭据、无 query / fragment 等安全校验。它不创建新 Provider，也不改变模型 ID 所选 API 族。
5. 精确的 OpenCode Go 模型与 Base URL 仍可命中项目维护的 settings / profile 预设，但只读取
   `OPENAI_API_KEY`。`OPENCODE_API_KEY` 不再是产品 runtime 的密钥别名。
6. 未命中项目预设的模型交给 Pydantic AI `infer_model()`；带 Base URL 时使用所选 Provider 的原生 factory。
   项目不维护第二份 Provider 注册表、模型发现、自动 fallback、动态路由或 URL 猜测。
7. 维护者评测 harness 中的 `backend` 身份和专用环境变量可以继续存在，因为它们标识冻结评测 transport，
   不是 NoneBot 插件的公开配置，也不参与产品 runtime 兼容。

## 迁移

| 旧配置 | 当前配置 |
|---|---|
| `NBTRIAGE_MODEL_BACKEND=opencode-go-chat`、`NBTRIAGE_MODEL_NAME=deepseek-v4-flash`、`OPENCODE_API_KEY=...` | 删除 backend；改为 `NBTRIAGE_MODEL_NAME=openai-chat:deepseek-v4-flash`、`NBTRIAGE_MODEL_BASE_URL=https://opencode.ai/zen/go/v1`、`OPENAI_API_KEY=...` |
| `NBTRIAGE_MODEL_BACKEND=pydantic-ai`、`NBTRIAGE_MODEL_NAME=<provider>:<model>` | 删除 backend；保留 model 与可选 Base URL |
| `NBTRIAGE_MODEL_BACKEND=openai-responses`、`NBTRIAGE_MODEL_NAME=<model>` | 删除 backend；改为 `NBTRIAGE_MODEL_NAME=openai:<model>` |
| `NBTRIAGE_MODEL_BACKEND=anthropic-messages`、`NBTRIAGE_MODEL_NAME=<model>` | 删除 backend；改为 `NBTRIAGE_MODEL_NAME=anthropic:<model>` |

旧字段不会自动转换。迁移后应重启 Bot，使进程重新读取环境变量。

## 影响

- 配置只有“模型身份、可选部署地址、调用预算”一条主线，不再需要为新服务增加 backend 枚举；
- 旧 `.env` 会在启动校验阶段失败，避免看似加载成功却走到意外 transport；
- OpenCode Go 部署必须把密钥放入标准 `OPENAI_API_KEY`；旧变量可以保留给其他工具，但本插件不再读取；
- 删除旧 B1 plugin service 不删除维护者 adapter、评测数据或任务级质量记录；这些工件仍用于独立验证；
- 未配置 model 时插件仍能启动并提供确定性能力索引；已配置但缺 Provider 依赖、密钥或能力时，仅相应模型任务
  降级，不阻断插件主体启动。

## 替代关系

- 替代 ADR-0091 中“旧 backend 作为迁移输入”和 OpenCode Go 密钥回退的决定；
- 替代 [ADR-0011](0011-expose-disabled-qualified-model-configuration.md) 中旧 backend 字段与
  `NBTriageModelService` 的配置/运行时设计；
- 替代 [ADR-0063](0063-keep-plugin-startup-independent-from-model-enhancements.md) 中依赖 backend 与
  `OPENCODE_API_KEY` 的部署示例，但保留“模型增强不得阻断插件启动”的边界；
- 不改变 ADR-0090 的 Base URL 安全、Provider factory、连接 revision 与质量标签决定。

## 落实与验证

- `NBTriageConfig` 不再公开或输出 backend 字段；仅保留一个不进入 JSON Schema、`model_dump` 或 runtime 的
  输入墓碑，使 NoneBot Settings 不会在校验前静默丢弃旧环境变量，并由 `before` validator 明确拒绝；
- `task_model_runtime` 只保留 `provider:model` 推断、可选 Base URL factory 和精确 OpenCode Go profile；
- `NBTriagePluginRuntime` 不再持有旧 `model_service`；
- 测试覆盖旧字段拒绝、model-only 配置、Base URL 安全、标准密钥和无旧 service 的插件启动；
- 当前 README、架构图谱和 Provider 支持矩阵只说明新的公开配置，历史 ADR 与冻结评测记录保持可追溯。
