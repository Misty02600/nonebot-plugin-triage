# ADR-0046：把内部原因与维护证据统一为行为探索目标

| 状态 | 决策日期 |
|---|---|
| 已采纳 | 2026-08-13 |

## 当时遇到了什么

ADR-0043 的 schema v4 同时使用 `behavior_explanation` 目标和
`maintenance_detail_requested` 布尔值。二者试图分别表达“想知道原因”和“需要多深的证据”，但当前产品真正
能执行的边界不是措辞深度，而是答案是否只依赖公开能力合同：

- 公开能力是否存在、命令格式、参数、公开角色、场景和前提可以由 `guidance` 回答；
- 源码、内部配置、环境、依赖或 adapter 版本、调用流和运行证据属于部署内部探索，必须进入独立鉴权分支；
- 把内部请求同时标成 `behavior_explanation + maintenance_detail_requested` 会重复表达同一个下游目标，单独标记
  maintenance 又会产生没有明确动作归属的结果。

`reported_observation` 则不同：它只表示用户声称实际发生过某个 Bot 行为，不能从任何目标中推导，也不能自动
触发 incident，因此继续保持独立字段。

## 决策

1. `SupportSemanticAssessment v5` 保留 `status`、多选 `goals` 和 `reported_observation`。目标固定为：
   - `guidance`（公开能力指导）：回答公开能力、语法、参数、公开角色、场景和前提；
   - `behavior_exploration`（行为探索）：处理需要源码、内部配置、环境、依赖 / adapter / 版本、调用流或运行
     证据才能回答的内部原因；
   - `incident_intake`（故障受理）；
   - `feature_feedback`（功能建议）。
2. 删除 `behavior_explanation` 和 `maintenance_detail_requested`。内部原因与内部证据请求统一标为
   `behavior_exploration`，不再维护重复的证据深度字段。
3. 分类只看当前请求表达的目标和现象，不接收用户身份、Bot / Event、Reply、Thread 或权限。即使文字自称
   SUPERUSER 或普通用户，也不能改变目标含义。
4. router 继续保留所有模型识别出的目标，但每轮只选择一个动作。满足可信运行失败的 incident 仍最优先；
   其余复合请求中 `behavior_exploration` 优先于 `guidance`，避免公开回答吞掉内部证据目标；功能建议与未验证
   incident 仍按确定性顺序处理。
5. 只有分类和确定性 routing 选中 behavior candidate 后，Matcher 才针对当前 Bot / Event 执行模型外
   `SUPERUSER` 鉴权。鉴权失败不读取 restricted 证据；鉴权成功也只表示允许进入行为探索，不扩大远端模型
   投影或秘密披露范围。
6. OpenCode Go 任务资格更新为 `support-semantic-v5`、schema v5、
   `support-semantic-v5-prompt-v1` 和
   `opencode-go-heldout-40-20260813-v5-taxonomy`。结构化输出仍直接使用 Pydantic AI
   `Agent(output_type=SupportSemanticAssessment)` 和显式 ModelProfile，不引入重复 transport capability。
7. 新资格使用冻结后首次发送、未写入 Prompt 的 40 条纯合成 held-out。硬门为 schema 合法率 100%、status
   准确率 100%、全字段精确匹配至少 90%。本次为 40 次请求、schema 1.000、status 1.000、exact 0.975，
   50,197 input / 3,774 output token，归一费用 1,782 microUSD。

## 原因与影响

- 字段现在直接对应下游证据域与动作，不再让两个字段描述同一个内部探索诉求；
- “为什么只能群管理员使用”若答案属于公开角色合同，归 `guidance`；“源码哪个 Rule 做了限制”归
  `behavior_exploration`；
- 多目标仍可表达，例如公开用法加内部调用流会同时保留 `guidance + behavior_exploration`，但本轮只执行
  behavior candidate；
- guidance 路径不再因为请求者是 SUPERUSER 而回退到 restricted 能力影子。身份不会改变分类，只在 behavior
  分支读取内部证据前守门；
- 当前已经实现 behavior candidate 后的 SUPERUSER 鉴权与不同回执，但完整源码 / 配置 / 运行证据检索和解释
  编排仍未接通，不能把鉴权成功写成已经回答了内部原因；
- held-out 唯一错例是“请查本轮运行证据，判断哪个 handler 分支实际执行了”：目标正确为
  `behavior_exploration`，但模型漏标 `reported_observation=true`。该限制保留，不回写或修改 held-out。

## 没有采用的方案

- **保留 maintenance 布尔值**：仍会制造无独立消费者的重复轴，且内部证据请求可能出现 goals 为空；
- **把所有 why 都归行为探索**：会让公开语法、角色、场景和错误合同不必要地要求维护者权限；
- **分类前先看身份**：会让同一句话因发问者不同而获得不同语义标签，并把权限逻辑泄漏进模型输入；
- **多目标同时执行多个生命周期**：会在一次输入中并发产生不同证据读取或副作用，难以原子授权和解释。

## 替代关系

- 替代 [ADR-0043](0043-separate-support-goals-observations-and-maintenance-depth.md) 的 taxonomy、schema、Prompt
  和语义任务资格 revision；其多目标、独立 observation、incident 需要可信模型外失败的原则继续有效。
- 延续 [ADR-0044](0044-use-pydantic-ai-agent-output-type-for-support-semantics.md) 的 Pydantic AI Agent
  `output_type` 实现，不恢复项目内手写输出 schema 或 tool parser。
- 部分替代 [ADR-0022](0022-limit-capability-shadow-guidance-to-superusers.md) 的聊天 fallback：guidance 不再因
  SUPERUSER 身份读取 restricted 影子；维护者 CLI 不变，内部聊天问题改由 behavior exploration 分类后鉴权。
- 落实 [ADR-0025](0025-explain-plugin-behavior-from-deployment-evidence.md) 与
  [ADR-0028](0028-allow-private-triage-and-superuser-request-context-replies.md) 的模型外行为探索鉴权边界。

## 落实与确认

- schema v5、Prompt v5、Agent output schema、OpenCode Go task 资格、评测器、runtime 和 router 已更新；
- guidance 只读取 public 能力合同；behavior candidate 在首轮和续问中均于分类后执行模型外 SUPERUSER 检查；
- development 30/30 全字段精确匹配；全新 held-out 39/40 全字段精确匹配并通过资格门；
- 完整机器报告仍是忽略的本地工件，不进入仓库；版本化 Fixture 只包含人工策展的纯合成输入与预期。

## 相关文档

- [支持入口分流](../architecture/flows/support-intake-routing.md)
- [模型 Provider 支持矩阵](../architecture/model-provider-support.md)
- [架构总览](../architecture/overview.md)
