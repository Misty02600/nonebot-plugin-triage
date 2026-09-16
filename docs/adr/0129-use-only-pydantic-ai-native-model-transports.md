# ADR-0129：只维护 Pydantic AI 原生模型传输

## 状态

已采纳

## 日期

2026-09-15

## 当时遇到了什么

项目已经用 Pydantic AI 的 `provider:model`、`infer_model()`、Provider factory、`ModelProfile`、
`ModelSettings` 和统一 usage 类型承载模型任务，但仍为 OpenCode Go 维护了一套专属 Provider 身份、模型
Profile、Chat 请求字段重排、thinking 参数、费用归一化、客户端工厂和任务资格。这个分支把单一兼容服务的
行为带回项目层，要求代码和测试持续追随其非标准请求细节，也使历史 OpenCode 评测容易被误读为当前
Pydantic AI 原生 Provider 的质量证明。

与此同时，实际教学冷运行表明 192k 单元累计 token 上限处在正常分布内部，而非异常保护边界。预算应作为
宽松止损上限：单次生成上限约束 Provider 输出，累计上限防止失控循环，两者不应频繁中断正常任务。

## 最后决定

1. 产品和维护 CLI 只接受 Pydantic AI 可解析的 `provider:model`，模型构造统一经过 `infer_model()`；不再
   维护 OpenCode Go backend、Provider、Profile、请求改写、费用算法、密钥别名或专属客户端工厂。
2. `NBTRIAGE_MODEL_BASE_URL` 仍可覆盖 Pydantic AI Provider 构造器明确支持的地址参数。任意兼容端点都只按
   其显式 Pydantic AI provider 前缀运行并标为未验证；项目不再按 URL 识别厂商或套用连接预设。
3. Provider/API 能力、结构化输出选择、输入/缓存/输出 usage 和统一 thinking 设置由 Pydantic AI 拥有。
   项目层只保留任务资格、隐私、安全、Prompt/Schema revision、预算与副作用授权。
4. 思考强度按任务设置：语义分流、公开 Guidance 和 Bug assessment 关闭思考；维护者对话与教学分析使用
   `ModelSettings.thinking="high"`。Provider 对统一设置的实际支持和转换由 Pydantic AI 决定。
5. 当前预算按任务分离：语义单次输出 240，公开 Guidance 2048，Bug 800；维护者对话单次输出默认 8192、
   单轮累计 512k；教学单次输出 32768、单元累计 384k。请求数、工具数、费用和时间上限继续独立生效。
6. 历史 OpenCode ADR、冻结 fixture 和已有报告继续作为不可改写的历史证据保留，但不进入当前资格集合，
   不再要求产品适配器或专属回归测试。新的评测必须显式声明 Pydantic AI 模型 ID、连接、设置和价格合同。

## 为什么这样选

- 让唯一依赖版本实际拥有 Provider 和协议差异，减少项目内重复抽象与单厂商兼容负担；
- 同一套任务代码可以直接消费 Pydantic AI 的模型、消息、usage、结构化输出和思考设置；
- 历史质量证据与当前可运行能力分开，不会把“曾经通过某网关”升级成另一 Provider 的资格；
- 放宽累计预算后，正常长输入和缓存计入的总 token 不再频繁触发止损，真正的失控仍由请求、工具、费用、
  时间和累计 token 多重上限约束。

## 没有采用的方案

- 保留 OpenCode 适配器但标记 deprecated：仍需维护自定义 HTTP、Profile、费用和测试，不能消除重复边界。
- 禁止所有自定义 Base URL：会一并失去 Pydantic AI 已明确支持的区域 endpoint、自建兼容服务和部署覆盖。
- 删除历史评测数据：会损失可追溯性，也无法解释旧资格为何不再适用于当前运行。

## 带来的影响

- 有利：产品运行时和维护 CLI 只剩一条 Pydantic AI 模型解析路径；任务预算和思考设置更清晰。
- 代价：OpenCode Go 的历史专属质量标签不再生效；仍想连接该地址的部署只能把它视作未验证的通用兼容
  endpoint，项目不承诺其非标准字段或费用语义。
- 风险：某些 Provider 可能不支持统一 thinking 强度或给出不同 token 统计；这种差异由 Pydantic AI profile
  与任务评测暴露，不在项目层增加静默补丁。

## 落实与确认

- 删除 `nbtriage.opencode_go_contracts` 与 `nbtriage.opencode_go_semantic_adapter`；
- `task_model_runtime` 和维护评测目标只使用 Pydantic AI 原生推断与 Provider factory；
- 删除固定 DeepSeek endpoint、模型和私有请求字段的旧 `evaluate-b1-deepseek` 直接 SDK 客户端；
- 删除 OpenAI、Anthropic 与 DeepSeek 的项目专属评测 factory；`evaluate-b1` 和 `evaluate-b4-real` 统一消费
  `provider:model`、`infer_model()`、原生 ModelProfile 与通用 B1 / Agent step 客户端；
- 删除 Qwen 3.6 专属 `enable_thinking` / 并行工具 / 温度设置与 settings revision，不再为
  Pydantic AI 尚未建模的厂商组合补齐请求字段；
- semantic、Guidance、Bug、维护者对话和教学运行时分别绑定已决定的思考与预算；
- 当前 semantic、Bug 与 capability 资格集合为空；历史 evaluator 只保留冻结合同读取能力。

## 替代关系

- 部分替代 [ADR-0091](0091-use-pydantic-ai-model-ids-as-the-public-transport-selector.md) 中按已知 URL 选择项目
  兼容性预设、OpenCode 配置与迁移的决定；`provider:model` 和可选 Base URL 继续有效。
- 部分替代 [ADR-0084](0084-install-pydantic-ai-control-plane-by-default-and-keep-providers-and-adapters-optional.md)
  中 OpenCode 专属依赖说明；Provider SDK 仍由部署者按 Pydantic AI extra 按需安装。
- 终止 [ADR-0103](history/0103-enable-opencode-go-thinking-and-capture-maintenance-reasoning.md) 的当前运行效力；
  该文件只保留历史设置与诊断证据。

## 相关文档

- [模型 Provider 支持矩阵](../architecture/model-provider-support.md)
- [架构总览](../architecture/overview.md)
- [README 配置说明](../../README.md)
