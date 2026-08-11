# ADR-0010：用有界证据获取循环验证 Agent 能力

## 状态

已采纳

## 日期

2026-08-09

## 当时遇到了什么

当前 B1 是一次结构化模型调用，B3 则由确定性策略选择补证动作。它们已经证明异步模型边界、会话状态、
审批和评测，但模型没有根据环境 observation 自主选择工具；B1 held-out 的缺失证据 micro-F1 仍为 0，
继续增加固定编排不能单独证明 Agent runtime 能力。

经典 ReAct 的核心价值是交替推理、行动与环境反馈，不是把同一 Prompt 重复调用。直接复刻自由文本
`Thought/Action` 协议、引入多 Agent 或开放高权限工具，会增加注入、费用、状态与错误归因风险，也无法证明
相对现有 B1/B3 的真实收益。

## 最后决定

1. 下一阶段先建立单 Agent 的有界证据获取循环：模型通过原生、结构化 tool calling 动态选择下一步，
   项目策略层仍拥有工具授权、参数复核、执行、状态和停止权；
2. 首批动作只覆盖白名单化运行证据读取、本地支持知识检索和结构化补证请求。Shell、任意文件/HTTP、
   配置修改、重启、代码执行与外部写入不进入首个 Agent Gate；
3. 每个模型步骤只允许一次供应商请求且零自动重试；一个会话可以有多个步骤，但必须同时受最大 turn、
   tool call、token、deadline 和费用预算约束，具体数值由离线评测冻结后再成为产品配置；
4. 复用 `SupportSession` 的事件、暂停、审批和恢复边界。补证请求形成 human-in-the-loop interruption，
   高权限动作未来即使加入也必须逐次显式审批；
5. 不持久化或要求模型暴露私有 Chain-of-Thought。审计轨迹只保存结构化动作、规范化 observation、简短
   决策摘要、证据引用、用量、停止原因和最终 outcome；
6. 在接入 NoneBot 前建立 B4 离线 Gate，在同一输入/Gold 隔离与预算下比较 B1 one-shot、B3 确定性策略和
   B4 bounded agent。只有任务结果、安全、步骤效率或缺失证据指标出现可复核收益，才允许插件的精确报障
   入口集成 Agent 路径；
7. 不因为引入 Agent 立刻增加多 Agent、MCP 或 evaluator-optimizer。只有单 Agent 在已定义工具与评测上
   出现稳定、可归因的能力瓶颈，才另行决策。

这项决定不替代 ADR-0008。ADR-0008 继续约束底层 Provider/Model 适配；本 ADR 只定义更高一层的 Agent
循环、授权和评测边界。

## Skill 与 Agent 的描述契约

未来仓库内出现可复用 Skill 时，其描述至少说明：解决的问题与适用触发、所需输入和前置证据、输出契约、
允许的读取或副作用、禁止事项、失败语义，以及何时停止或交还调用者。工具名列表不能替代这些语义。

Agent 描述在上述基础上还必须说明：目标和完成条件、可自主选择的 Skill/工具、策略层保留的权限、状态与
记忆所有权、turn/tool/token/deadline 预算、暂停/审批/升级条件，以及 trajectory 与 outcome 的评测方式。
“Agent”名称本身不授予工具、数据出站或副作用权限。

## 为什么这样选

- 现有 B3 已经具备事件化状态、补证回执与审批，复用它比另建通用 graph runtime 更能暴露真正的状态、
  恢复和权限问题；
- 技术支持天然需要根据新证据修订判断，同时结果可以用引用、环境状态和既有 Gold 验证，适合受控 Agent；
- 有 baseline、trajectory 和 outcome 的对照能形成可复盘的工程证据；单纯 ReAct 或多 Agent Demo 只能证明
  框架调用，不能证明产品收益或安全性；
- 主流工程实践同样建议从简单可组合模式开始，在确有可测收益时增加 Agent 复杂度，并为循环设置
  ground truth、停止条件、人工介入和评测。

## 没有采用的方案

- **继续只做固定编排**：可预测且安全，但当前项目已充分覆盖这类能力，不能补齐模型自主选择行动的证据；
- **经典自由文本 ReAct**：解析和审计脆弱，也不应记录私有思维链；采用原生 typed tool calling；
- **直接使用多 Agent**：当前没有单 Agent 工具过载或指令冲突证据，先引入只会扩大成本和故障面；
- **直接接入生产插件再 dogfood**：缺少离线 trajectory Gate，会把真实数据、费用和安全风险变成调试手段；
- **让框架拥有全部工具调度与状态**：项目仍需独立掌握授权、白名单 observation、会话和评测语义，避免
  框架升级改变产品边界。

## 带来的影响

- 有利：项目将同时证明模型适配、动态工具选择、持久状态、HITL、安全门和 Agent eval；
- 代价：需要新的多步 Fixture、trajectory schema、预算账本和失败恢复测试；
- 风险：模型循环会增加费用、延迟和复合错误，必须通过明确上限和 B4 Gate 控制；
- 非目标：本决定不授权真实 API、Bot 自动诊断、高权限工具、自由文本入口或外部写入。

## 落实与确认

- 2026-08-09：维护者明确采纳“有界证据诊断 Agent，而非传统 ReAct Demo 或多 Agent 堆叠”的方向；
- 2026-08-09：已完成领域 runner、单步 deferred tool 适配器、暂停恢复和脚本多 trial Gate；
  框架职责边界由 ADR-0012 冻结；
- 实施情况：脚本 Gate 只证明控制流与安全边界，真实模型多 trial 仍待单独授权；插件只允许集成通过
  真实 Gate 的策略。

## 相关文档

- [ADR-0008：采用 Pydantic AI 的受控模型适配层](0008-pydantic-ai-controlled-model-adaptation.md)
- [ADR-0012：让 Pydantic AI Deferred Tools 位于领域 Agent runtime 之后](0012-use-pydantic-ai-deferred-tools-behind-domain-runtime.md)
- [有界 Agent 单步与恢复流程](../architecture/flows/bounded-agent-step.md)
- [Anthropic：Building effective agents](https://www.anthropic.com/engineering/building-effective-agents)
- [Anthropic：Demystifying evals for AI agents](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents)
- [OpenAI：A practical guide to building agents](https://openai.com/business/guides-and-resources/a-practical-guide-to-building-ai-agents/)
- [ReAct](https://arxiv.org/abs/2210.03629)
