# ADR-0086：把模型评测作为质量标签而不是运行许可

| 状态 | 决策日期 |
|---|---|
| 已采纳；已实现 | 2026-08-17 |

> [ADR-0090](0090-configure-pydantic-ai-provider-base-urls-at-deployment.md) 已补充部署端连接身份：部署者可
> 为标准 Pydantic AI Provider 配置受限 Base URL；该连接默认未验证，且不能改变 Provider / ModelProfile。
> [ADR-0091](0091-use-pydantic-ai-model-ids-as-the-public-transport-selector.md) 又把 `provider:model` 定为
> 公开 transport 选择器；[ADR-0092](0092-remove-legacy-model-backend-configuration.md) 随后删除旧 backend
> 迁移兼容。

## 背景

项目已经为语义分类、教学注释、公开 Answer 和 Bug 分析维护独立 held-out。过去这些评测结果同时承担了
两项职责：向部署者说明某个精确 Provider / model / Prompt 组合的已知质量，以及决定该组合能否进入产品
运行链。这导致 Pydantic AI 已经能够解析、且满足结构化输出和工具合同的其他模型仍被任务资格表拒绝；Bug
Agent 即使完成了同一套 Evidence reconciliation，未登记的模型也不能写入本地 Problem 工作流。

项目作者希望部署者可以自由选择 Pydantic AI 支持的模型。公开评测仍有价值，但它应回答“这个精确组合经过
了什么验证”，而不是充当封闭的模型白名单。

## 决策

1. held-out 注册表与 Provider 支持矩阵只提供公开质量标签，不再作为语义分类、教学注释、公开 Answer、Bug
   分析或本地 Problem 持久化的运行许可。
2. 部署者可以通过已有 transport 别名，或用 `NBTRIAGE_MODEL_BACKEND=pydantic-ai` 与 Pydantic AI 的
   `provider:model` 标识选择模型。所需 Provider SDK 仍由部署者安装；项目不接受任意自定义 base URL，也不
   把“OpenAI-compatible”自动视为经过支持验证的 Provider。
3. 未测评组合与已测评组合执行相同任务流程、结构化 schema、Evidence 引用、预算、隐私投影和模型外
   reconciler。未测评模型产生的、已经通过这些确定性检查的 Bug verdict 可以与已测评模型一样写入本地
   Problem 工作流；本决定不授权创建外部 Issue、发送外部消息或执行其他副作用。
4. 运行时始终校验实际响应的 Provider 与 model 身份，不因组合未测评而放宽。Problem Decision 记录实际
   provider、model、task、Prompt revision，以及已发布 evaluation ID 或稳定的 `unverified:<task>:<prompt>`
   标签。教学注释缓存同样按 backend / model 与 generation contract 隔离。
5. 仍然失败关闭的条件包括：模型或 Provider SDK 无法解析、密钥缺失、网络或超时失败、ModelProfile 不支持
   当前任务所需的结构化输出或工具、输出 schema / Evidence / 安全校验失败、预算越界，以及模型外当前性、
   权限或披露门禁失败。这些是技术与安全边界，不是 held-out 资格门。
6. 模型未配置或某项模型增强不可用时，继续遵守 ADR-0063：插件可以启动，确定性能力索引继续工作，失败的
   模型任务单独降级并记录不含密钥和原始异常的日志。
7. 支持矩阵继续公布每个精确组合的评测范围、指标和已知缺口。通过评测的组合可以标为“已验证”或推荐；
   未评测组合标为“未验证”，但不会因此无法正常使用。一个任务的评测结果仍不能继承为另一个任务的质量
   结论。

## 影响

- 部署者不必等待项目为每个模型单独登记资格，便可在相同安全合同下试用 Pydantic AI 可解析的模型；
- “可运行”与“项目已验证质量”成为两个明确维度，日志中的未验证提示降为信息而不是故障；
- 模型效果差仍可能导致任务失败关闭或产生较低质量但 schema 合法的结果，因此支持矩阵、离线评测、线上
  观测和人工改判仍然重要；
- 评测注册表继续固定历史证据，不会因为允许自由选择模型而把未运行的组合标成已验证。

## 替代关系

- 替代 [ADR-0011](0011-expose-disabled-qualified-model-configuration.md)、
  [ADR-0041](0041-qualify-opencode-go-tool-output-for-support-semantics.md) 和
  [ADR-0063](0063-keep-plugin-startup-independent-from-model-enhancements.md) 中“任务资格不匹配即不运行”的部分；
- 替代 [ADR-0068](0068-treat-qualified-agent-bug-verdicts-as-operational-decisions.md) 中只有精确已资格模型才能形成
  正式本地 Bug Decision 的限制；Evidence reconciliation、追加式 Decision 与人工事后监督继续有效；
- 补充 [ADR-0084](0084-install-pydantic-ai-control-plane-by-default-and-keep-providers-and-adapters-optional.md)：
  Pydantic AI 控制层仍默认安装，Provider SDK 仍按需安装，缺少 SDK 仍只使所选模型 transport 不可用。

## 验证

- 已测评与未测评组合都能构造同一任务客户端，且都校验实际 Provider / model 身份；
- 未测评 Bug Agent 经 reconciler 接受的 `bug` 会生成正式 `RecordBugCommand`，Decision 保存
  `unverified:` evaluation 标签；
- 缺少配置、SDK、密钥、网络和不合法输出仍按任务失败关闭，不阻断插件导入；
- 支持矩阵明确区分运行能力与项目评测证据。
