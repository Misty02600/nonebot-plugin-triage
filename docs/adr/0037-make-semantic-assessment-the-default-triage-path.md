# ADR-0037：把语义 assessment 作为 triage 的正式默认路径

| 状态 | 决策日期 |
|---|---|
| 已采纳 | 2026-08-13 |

> OpenCode Go 的 `support-semantic-v2` 传输形式已由
> [ADR-0041](0041-qualify-opencode-go-tool-output-for-support-semantics.md) 部分替代为唯一、不可执行的
> output tool；本 ADR 的默认 assessment、零可执行工具、零重试与失败关闭继续有效。
> 当前目标 / 现象 / 维护深度 schema 与资格 revision 已由
> [ADR-0043](0043-separate-support-goals-observations-and-maintenance-depth.md) 替代。
> [ADR-0090](0090-configure-pydantic-ai-provider-base-urls-at-deployment.md) 已窄范围替代本 ADR 保留的
> custom Base URL 禁令；默认 assessment、零可执行工具、零重试与失败关闭继续有效。

## 当时遇到了什么

`triage <自然语言>` 已经是统一支持入口，但过渡实现曾用症状词、功能问法词表和全文固定话术
猜测用户意图。这些隐藏语法无法稳定覆盖同义改写、否定、假设和复合请求，还会把“报告现象”
误当作“授权建立 incident”。一旦继续扩写词表，入口就会同时存在一套不可见的规则分类器和
一套计划中的语义分类器。

ADR-0011 又把模型装配表达成产品级 `enabled` 开关，并规定 Matcher 不消费模型服务。这与
“每轮 `triage` 都先得到受限语义 assessment”的产品边界冲突。未配置 transport 或请求期失败应表现为
本轮 abstain，而不是另一种产品模式；部署者显式填写不完整或未准入的 transport 身份仍属于启动配置错误。

## 决策

1. 每个通过指令 framing、空输入、长度、频率和必要模型前安全守门的非空 `triage` 请求，
   无论是首轮还是 Thread 续问，都把版本化语义 assessment 作为正式默认理解路径。
2. 删除产品级 `NBTRIAGE_MODEL_ENABLED` / `nbtriage_model_enabled` 开关。不提供“绕过 assessment，
   改用本地词表”的运行模式，也不使用固定话术作为建单协议。
3. assessment 只返回项目版本化 schema 允许的受限需求信号：用户需求可多选，并可明确表示不确定或
   abstain。它只报告用户是否描述现象；可信运行失败必须由模型外确定性边界从 Reply correlation 和运行证据
   得出。assessment 不直接建立 incident、读取 restricted 证据、执行工具或选择维护动作。
4. 空输入、限流、身份与场景、Reply / Thread 归属、披露域、配置和秘密投影、预算以及最终
   disposition 和副作用授权仍由确定性代码掌握。不得把 Prompt、用户原文或模型输出直接升级为
   工具调用。
5. transport 按 `Provider + API 族 + 精确 model/profile + task` 分别准入。某组合能用于 B1
   诊断或 B4 tool calling，不自动证明它能用于支持入口 assessment。未在当前 task 资格表中的
   组合不得发起请求。
6. assessment 使用 Provider 原生 JSON Schema，不暴露 function tool、native tool、output tool、
   MCP 或内置工具；项目与 SDK 自动重试均为零，不 fallback 到提示词 JSON、其他模型或词表。
7. 未配置 transport，以及已成功装配的合格 transport 在请求期超时、返回 Provider 错误或未通过结构 / 领域
   校验时，本轮统一按 abstain 处理：给出不过度承诺的澄清回复，不猜测意图、不读取额外证据、不建立
   incident，也不重放请求。部署者若只配置 backend/model 之一、选择未准入组合，或为已选择组合缺少依赖 / 密钥，
   则在启动装配时明确失败，不能静默伪装成正常的未配置状态。

## 原因与影响

- 用户只需要自然表达需求，不需要学习一组未公开的建单口令；
- 多意图、否定、疑问和现象报告可以在同一个 schema 中表达，不再由多处规则各自解读原文；
- 不保留产品开关可以防止未来两套语义路径长期并存；transport 资格门和失败关闭仍防止
  不受控请求；
- 代价是 transport 不可用时，入口只能澄清或明说暂时无法判断，不会用低质量本地规则维持表面命中率。

## 没有采用的方案

- **保留默认关闭的产品开关**：会使词表降级路径变成另一种被支持的产品模式。
- **只删除故障固定话术，保留功能问法词表**：仍然是两套意图分类器，并且会让相同请求因表达差异进入
  不同证据和权限路径。
- **transport 失败后回退到正则或其他模型**：无法证明输出合同、请求数、数据边界和费用仍符合当前资格。
- **让 assessment 直接建单或调用工具**：把概率性理解和权威状态变更混为一层，会绕过现有领域闸门。

## 替代关系

- OpenCode Go 的 `support-semantic-v2` 精确组合由
  [ADR-0041](0041-qualify-opencode-go-tool-output-for-support-semantics.md) 部分替代第 6 条的 native-schema-only
  传输限制；不改变 B1 或 B4 的资格。

- incident 可达条件先由 [ADR-0040](0040-require-trusted-preflight-failure-before-incident.md) 收紧，随后由
  [ADR-0043](0043-separate-support-goals-observations-and-maintenance-depth.md) 最终固定为
  `incident_intake + reported_observation + 模型外可信初检失败`；本 ADR 的默认路径与零工具边界继续有效。

- 部分替代 [ADR-0011](0011-expose-disabled-qualified-model-configuration.md) 的产品级 `enabled` 开关、
  禁用时的工厂分支，以及“Matcher 不消费模型服务”约束。ADR-0011 的密钥仅从环境读取、
  无 custom base URL、精确组合资格门、惰性 client factory 和每步一次请求继续有效。
- 落实 [ADR-0020](0020-use-triage-command-for-natural-language-support.md) 中“指令只负责选中插件，后续文字由受控
  意图边界处理”的目标，不改变每轮都必须显式写 `triage` 的 framing 决定。

## 落实与确认

- 已删除本地功能问法词表和全文故障话术；确定性入口适配器现在只规范化文字并识别空输入，
  不再产生任何文本语义枚举；
- `nbtriage_model_enabled` 已从配置 schema 删除，旧字段会明确拒绝；backend 和 model 仍必须成对配置；
- 已实现传输无关的 v5 `SupportAssessmentRequest` 与 `SupportSemanticAssessment` 闭合合同及解析验证。
  请求只接受当前单条规范化文字；结果分离多选 goals 与 `reported_observation`。输出没有
  reason、confidence、lifecycle、action 或 authorization 字段；
  未决状态不能夹带语义信号；
- 已实现一次性异步 assessment service 和传输无关的确定性 router：无 transport、模型前秘密命中、超时、
  transport 失败或非法输出都会收敛为有界 abstain；每轮最多调用 client 一次且不重试。router 不读取原文，
  只有 assessment 同时包含 `incident_intake + reported_observation`，且模型外 Reply correlation 对应的
  可信运行证据明确失败时，
  才决定 `OPEN_INCIDENT`，并签发与精确 `LiveReportRequest` 绑定、不可复制或序列化的进程内
  `IncidentAuthorization`；报障服务在副作用前原子验证并一次性消费它，单独的现象报告只能澄清；
- 已实现固定 Prompt 的 Pydantic AI Agent client：只发送闭合请求 JSON，并直接以领域 Pydantic model 作为
  `output_type`；ADR-0044 已替代手写 output object、tool 定义与响应 part 解析。native 模式仍要求原生
  JSON Schema；OpenCode Go 模式只暴露一个 Agent 生成的 required、不可执行业务动作的 output tool，关闭
  instrument，固定 timeout / 输出 token，并保留身份、usage 与 finish 审计；
- 已把非可选 assessment service 接入插件 runtime，并让首轮和 Thread 续问都构造当前单条请求、每轮调用
  assessment 一次后经过同一个 router。`LiveReportService` 会在读取证据、执行建单限流或写状态前校验
  routing decision 与 `IncidentAuthorization`；无效或伪造授权失败关闭；
- 已实现 OpenCode Go semantic Provider factory、独立 task/profile/Prompt 资格表与 development / held-out
  评测；当前 v4 精确组合已按 ADR-0043 准入。未配置 transport 时 runtime 仍装配 unavailable service并保守澄清。

## 相关文档

- [ADR-0038：限定语义 assessment 的远端数据投影](0038-limit-semantic-assessment-remote-data-projection.md)
- [支持入口分流](../architecture/flows/support-intake-routing.md)
- [模型 Provider 支持矩阵](../architecture/model-provider-support.md)
- [ADR-0008：采用 Pydantic AI 的受控模型适配层](0008-pydantic-ai-controlled-model-adaptation.md)
