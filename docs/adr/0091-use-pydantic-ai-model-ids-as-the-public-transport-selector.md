# ADR-0091：用 Pydantic AI 模型 ID 作为公开传输选择器

| 状态 | 决策日期 |
|---|---|
| 已采纳；兼容迁移部分由 ADR-0092 替代 | 2026-08-18 |

> [ADR-0092](0092-remove-legacy-model-backend-configuration.md) 已删除本 ADR 保留的旧 backend
> 迁移输入与 `OPENCODE_API_KEY` 回退。`provider:model`、可选 Base URL、ModelProfile 和连接预设的
> 其余决定继续有效。

## 背景

项目早期用 `NBTRIAGE_MODEL_BACKEND` 区分 OpenCode Go Chat、OpenAI Responses、Anthropic Messages
和 Pydantic AI 通用入口。继续沿用该方式会要求每接入一家兼容服务、一个区域 endpoint 或一种 API 族就新增
项目枚举、配置说明和分支，最终在 Triage 内复制 Pydantic AI 已经维护的 Provider / Model 注册表。

用户自定义模型和 OpenAI-compatible endpoint 是常见部署需求。Pydantic AI 2.28.0 已把字符串模型 ID 定义为
`provider:model`，并以 `infer_model()`、Provider factory 和 `ModelProfile` 拥有模型类、API 族、结构化输出能力
与 Provider 默认行为。类似地，LiteLLM 的自定义 OpenAI-compatible 配置也把协议前缀、模型名、`api_base`
和 `api_key` 分开，而不是要求调用应用为每个后端创建枚举。

## 决策

1. 新部署只通过 `NBTRIAGE_MODEL_NAME=<provider>:<model>` 选择模型 transport。该字段直接交给 Pydantic AI
   解析；Triage 不维护平行的公开 Provider 注册表。
2. `NBTRIAGE_MODEL_BASE_URL` 继续作为可选部署连接覆盖。若服务只有 OpenAI-compatible Chat 接口，部署者
   显式使用 `openai-chat:<model>`，再填写服务提供的 Base URL 和 `OPENAI_API_KEY`。若使用 OpenAI Responses，
   则使用 Pydantic AI 的 `openai:<model>`；API 族不从 URL 或模型名字猜测。
3. `NBTRIAGE_MODEL_BACKEND` 不再是新配置的必填项，也不再为新服务增加值。现有
   `opencode-go-chat`、`openai-responses`、`anthropic-messages` 和 `pydantic-ai` 继续作为弃用的迁移输入读取；
   旧专用配置仍按原有密钥和模型名语义运行。
4. 精确命中项目已知连接的 `provider:model + Base URL` 可以在模型外选择已有兼容性预设。例如
   `openai-chat:deepseek-v4-flash + https://opencode.ai/zen/go/v1` 复用 OpenCode Go 的非思考、串行工具、
   零温度、Provider 身份和已发布 endpoint revision。该识别只复用已经验证的精确合同，不建立通用 URL
   猜测或 Provider 自动发现。
5. 未命中项目预设的 `openai-chat:<model> + Base URL` 使用 Pydantic AI 的 `OpenAIChatModel` 和
   `OpenAIProvider`。它可以运行，但连接和任务质量默认标为未验证；兼容协议不能证明模型支持工具、结构化
   输出、参数语义或项目 held-out 质量。
6. 已知 Pydantic AI Provider 继续保留自己的 Provider 类和 `ModelProfile`；Base URL 只覆盖其构造参数。
   项目层只维护任务资格、Prompt/schema revision、隐私、预算、安全与已知连接预设，不复制 Provider 能力字段。
7. Base URL 的 HTTPS / loopback、无凭据、无 query / fragment 等安全校验，以及连接哈希、缓存隔离、
   脱敏轨迹、失败关闭和标准密钥环境变量边界保持不变。
8. 本决定不增加模型列表发现、动态路由、fallback、负载均衡、健康检查、集中密钥管理或 LiteLLM 网关依赖。
   如果部署需要这些能力，应由独立网关承担；Triage 只连接部署者明确选择的单一模型 endpoint。

## 配置示例

OpenCode Go：

```dotenv
OPENAI_API_KEY=<OpenCode Go API Key>
NBTRIAGE_MODEL_NAME=openai-chat:deepseek-v4-flash
NBTRIAGE_MODEL_BASE_URL=https://opencode.ai/zen/go/v1
```

任意 OpenAI-compatible Chat 服务：

```dotenv
OPENAI_API_KEY=<服务密钥>
NBTRIAGE_MODEL_NAME=openai-chat:<服务商模型 ID>
NBTRIAGE_MODEL_BASE_URL=https://model.example/v1
```

Pydantic AI 原生 Provider：

```dotenv
DASHSCOPE_API_KEY=<百炼 API Key>
NBTRIAGE_MODEL_NAME=alibaba:qwen-max
NBTRIAGE_MODEL_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
```

## 为什么这样选

- 用户只需要理解 Pydantic AI 已公开的模型 ID、可选 endpoint 和 Provider 标准密钥，不需要先理解 Triage
  内部 backend；
- 新兼容服务不再要求发布新代码或扩展枚举，避免 backend 数量随服务商无限增长；
- `openai-chat:` 明确表达协议和模型类，Base URL 只表达连接地址，不从字符串 URL 猜传输能力；
- 已验证的精确连接仍可复用项目特有 settings 和质量标签，未知连接则保持可运行但未验证；
- 旧部署可以渐进迁移，不因配置字段弃用而立即中断。

## 影响与迁移

- 旧 OpenCode Go 配置从 `NBTRIAGE_MODEL_BACKEND=opencode-go-chat`、
  `NBTRIAGE_MODEL_NAME=deepseek-v4-flash` 迁移为上面的 `openai-chat:` 模型 ID 与官方 Base URL；
- 新式 OpenCode 配置优先读取 `OPENAI_API_KEY`，迁移期兼容 `OPENCODE_API_KEY`；旧 backend 仍只读取
  `OPENCODE_API_KEY`；
- `NBTRIAGE_MODEL_BACKEND=pydantic-ai` 可以直接删除，保留原有 `provider:model` 和可选 Base URL；
- 旧 `openai-responses` / `anthropic-messages` 配置暂时可用；新配置分别改用 Pydantic AI 的
  `openai:<model>` / `anthropic:<model>`；
- 只设置 model 就表示模型 transport 已配置；旧 backend 不再参与“是否启用模型增强”的判断。

## 替代关系

- 部分替代 [ADR-0090](0090-configure-pydantic-ai-provider-base-urls-at-deployment.md) 中必须先设置
  `NBTRIAGE_MODEL_BACKEND=pydantic-ai` 才能使用 `provider:model` 和 Base URL 的限制；ADR-0090 的地址安全、
  Provider factory、连接 revision 和未验证标签继续有效；
- 部分替代 [ADR-0086](0086-treat-model-evaluation-as-a-quality-label.md) 中以 transport/backend 别名作为公开
  模型选择方式的表述；评测只作质量标签的决定不变；
- 不改变 [ADR-0084](0084-install-pydantic-ai-control-plane-by-default-and-keep-providers-and-adapters-optional.md)
  的依赖边界：控制层默认安装，具体 Provider SDK 仍由部署者按需安装。

## 落实与验证

- `NBTriageConfig` 接受无 backend 的 `provider:model`，并继续校验受限 Base URL；
- `task_model_runtime` 使用 Pydantic AI 原生 `infer_model()` / Provider factory，精确 OpenCode 目标只通过
  一个内部兼容性谓词复用既有 adapter；
- semantic、Bug、public guidance、capability annotation 与 telemetry 都以 model 是否存在判断配置状态；
- 测试覆盖 model-only 推断、任意 OpenAI-compatible Chat endpoint、精确 OpenCode 预设、旧 backend 迁移、
  Provider 凭据提示、连接 revision 和插件无私有配置导入。

## 参考

- [Pydantic AI Models and Providers](https://ai.pydantic.dev/models/overview/)
- [Pydantic AI OpenAI-compatible providers](https://ai.pydantic.dev/models/openai/#openai-compatible-models)
- [Pydantic AI `infer_model`](https://ai.pydantic.dev/api/models/base/#pydantic_ai.models.infer_model)
- [LiteLLM custom OpenAI-compatible endpoint](https://docs.litellm.ai/docs/providers/openai_compatible)
