# ADR-0048：用公开事实驱动受控能力 Answer Agent

| 状态 | 决策日期 |
|---|---|
| 已采纳 | 2026-08-14 |

## 当时遇到了什么

`triage` 已经用真实模型做语义 assessment，但 guidance 分支最终仍由固定模板回复。即使能力影子已收集
YetAnotherPicSearch 的公开用法“使用指令 `搜图 -h` 查看帮助”，模板也只读取顶层 usage Claim，于是用户
看到“当前索引还没有可靠的完整用法”。这让“模型已经接入”和“模型实际回答问题”产生了误解。

ADR-0027 已决定让模型根据公开事实组织语言，但当时明确没有授权新的模型调用。维护者现在要求开始实施，
并把当前 Bot 用于在线测试。

## 决策

1. 保留 `SupportSemanticAssessment` 作为第一段分类 Agent。它只产出 signals，不生成回复、不鉴权、不授权
   副作用。
2. 只有确定性 router 选择 `SHOW_GUIDANCE`，且显式 Provider 或能力影子提供可服务 public 事实时，调用独立
   `public-guidance-answer-v1` Answer Agent。一轮 guidance 因而最多调用模型两次。
3. Answer Agent 的闭合输入只含当前单条规范化问题，以及当前 adapter、public、完整且非 stale 的能力名、
   描述、用法和示例。Reply / Thread 历史、身份、配置、环境变量、日志、源码、运行证据、证据位置与
   restricted 记录均不进入请求；问题或事实疑似含凭据时网络前拒绝。
4. 使用 Pydantic AI `Agent(output_type=PublicGuidanceAnswer)` 与 Provider `ModelProfile`。输出只含有界回答和
   至少一个事实 ID；未知事实 ID、非法 schema、超时或 transport 失败都退回原确定性说明。
5. Answer Agent 没有业务工具，不执行第三方 Matcher、Rule、Permission、handler 或用户文字，也不能生成
   incident authorization。
6. OpenCode Go / `deepseek-v4-flash` 当前只获得受控 dogfood 准入：60 秒、240 output token、一次请求、零
   retry，已完成假 HTTP wire、Provider 身份、隐私和 Handler 回退测试。它没有继承 semantic assessment 的
   held-out 资格；完成独立真实回答质量 Gate 前，不标记为稳定支持。

## 原因与影响

- 用户获得的将是真正由 LLM 根据部署本地公开事实组织的答案，而不是分类后仍输出固定模板；
- 检索、披露和副作用仍由模型外代码控制，Answer Agent 只能在已经允许表达的事实集合内措辞；
- 对 guidance 的远端请求数从一次增加到最多两次，延迟与费用也相应增加；当前统一 cooldown 不是全局费用
  预算，受控试用期间仍不应直接面向无限制公开群；
- 引用 ID 只能证明回答声明了依据，不能自动证明每句话语义完全忠实；独立 held-out 与人工 groundedness
  评测仍是从 dogfood 晋级稳定支持的门槛。

## 落实与确认

- 已实现闭合领域 schema、秘密守门、模型适配器、OpenCode Go runtime、显式 Provider / public shadow 事实
  投影、Handler 接线和确定性 fallback；
- 已用 Pydantic AI 2.27.0 的 `Agent(output_type=Pydantic model)`、`UsageLimits(request_limit=1)` 与框架重试
  设置实现一次请求边界；
- 已覆盖未知引用拒绝、秘密零请求、唯一 output tool、零 SDK retry、Provider/model 身份与搜图元数据用法
  投影；
- 已用纯公开问题“搜图功能怎么使用？”与两条公开事实执行一次真实 OpenCode Go smoke，单次请求成功返回
  “发送 `搜图 -h` 查看帮助”的自然语言回答并只引用对应 usage 事实；未发送群消息、身份或内部证据；
- 尚未执行真实 Provider held-out 回答质量 Gate，也未增加跨用户全局费用预算或熔断。

## 关系

- 落实并细化 [ADR-0027](0027-constrain-guidance-with-facts-not-fixed-wording.md) 的模型自由措辞与事实边界；
- 不改变 [ADR-0037](0037-make-semantic-assessment-the-default-triage-path.md) 的语义 assessment / router 职责；
- 扩展 [ADR-0038](0038-limit-semantic-assessment-remote-data-projection.md) 之外的一项独立出站数据合同，不能把
  Answer Agent 的 public facts 反向加入 semantic assessment 请求；
- 结构化输出继续遵循 [ADR-0042](0042-use-pydantic-ai-model-profile-for-structured-output.md) 和
  [ADR-0044](0044-use-pydantic-ai-agent-output-type-for-support-semantics.md) 的框架原生抽象方向。

## 相关文档

- [支持入口分流](../architecture/flows/support-intake-routing.md)
- [模型 Provider 支持矩阵](../architecture/model-provider-support.md)
- [架构概览](../architecture/overview.md)
