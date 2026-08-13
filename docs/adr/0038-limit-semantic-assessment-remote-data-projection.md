# ADR-0038：限定语义 assessment 的远端数据投影

| 状态 | 决策日期 |
|---|---|
| 已采纳 | 2026-08-13 |

> 本 ADR 只决定可出站的数据类别。OpenCode Go 的精确 Provider/model/profile、预算与 held-out 资格已由
> [ADR-0041](0041-qualify-opencode-go-tool-output-for-support-semantics.md) 另行确认；其他组合仍不能继承。

## 当时遇到了什么

[ADR-0037](0037-make-semantic-assessment-the-default-triage-path.md) 已决定每轮非空 `triage`
默认经过受限语义 assessment。要把该路径接到远端模型，仍必须明确区分两件事：哪些当前请求数据允许
离开部署环境，以及哪个 Provider / API / model 能实际接收请求并产生多少费用。

维护者已允许把当前单条 `triage` 请求文字用于远端语义 assessment，但没有授权把 Reply、Thread、身份、
部署证据或其他上下文一并发送，也没有借此选定真实 transport 或批准付费调用。若不把两层授权分开，
后续实现很容易把“允许这一类数据出站”误写成“任意模型现在都能调用”。

## 决策

1. 支持入口语义 assessment 可以向通过该 task 精确资格门的远端 Provider 发送当前这一次调用中、经过
   指令 framing、长度限制、规范化和模型前秘密守门后的单条 `triage` 请求文字。
2. 模型可见的用户或部署数据采用确定性 allowlist 投影；唯一获准的用户派生字段是当前单条规范化请求
   文字。固定的 task 标识、Prompt 版本和输出 JSON Schema 属于应用自身的协议控制数据，可以随请求发送，
   但不能借这些字段夹带运行上下文。
3. 投影明确禁止 Reply / origin 正文、以往 `triage` 文字、Thread 或其他会话历史、用户 / Bot / 群组 /
   频道 / 服务器等身份与作用域、配置字段或配置值、环境变量、日志、运行证据、源码、能力索引内容以及
   `restricted` 证据。Reply ID、incident ID、correlation ID 等关联标识也不进入模型请求。
4. `SUPERUSER` 只影响模型外的本地鉴权和回答证据域，不扩大本 ADR 的远端投影。未来行为探索即使在原会话
   可以返回 restricted 本地解释，也不能把这些证据发送给语义 assessment；当前 behavior candidate 仍只澄清，
   不读取 restricted 证据。
5. 请求文字若触发秘密、凭据或其他模型前拒绝规则，则在网络调用前失败关闭；不得先出站再依赖 Provider
   过滤。assessment 不进行检索、不读取工具，也不能自行追取被禁止的上下文。
6. 数据类别授权是必要条件，不是 transport 资格。真实调用仍必须按
   `Provider + API 族 + 精确 model/profile + semantic-assessment task` 建立独立支持行，并另外冻结请求数、
   输入 / 输出 token、deadline、费用和 Provider 数据处理条件。B1、B4 或测试 transport 的资格不能继承。
7. 本决定本身不选择任何真实 Provider、API 或 model，也不批准预算、费用、线上资格试验或单次请求执行。
   这些条件必须由后续独立决定确认；OpenCode Go 已由 ADR-0041 完成该步骤，其他组合仍不得推导许可。
8. 以后若要加入历史文字、Reply 正文、身份、配置、日志、源码、运行证据或 restricted 证据，必须作为新的
   数据边界重新取得明确授权；不能以“提高分类质量”或“请求者是 SUPERUSER”推导许可。

## 原因与影响

- 语义分类只消费完成当前任务所需的最小用户内容，部署环境和会话上下文不会因为接入模型而扩大暴露面；
- 数据授权、Provider 资格、费用授权和单次执行授权相互独立，便于测试和审计时证明没有越界；
- Thread 中的“这个”“刚才那个”等省略表达无法靠发送历史补全，模型信息不足时必须 abstain 或由插件追问；
- 只允许单条文字会降低部分复合场景的分类上限，但避免把支持会话演变成默认上传完整聊天和部署证据。

## 没有采用的方案

- **发送 Reply 正文或 Thread 历史**：能提供指代上下文，但会把未获授权的其他消息和参与者内容带出部署环境。
- **对 SUPERUSER 放宽投影**：本地披露授权不等于远端数据处理授权，两者不能互相替代。
- **先选一个模型试跑再补数据合同**：会在资格、预算和留存条件尚未确认时产生不可撤回的外部请求。
- **把所有入口元数据一起结构化发送**：adapter、会话和身份信息不是当前语义分类的必要输入，也会扩大
  Provider 侧的关联能力。

## 落实与确认

- 2026-08-13：维护者明确允许上述当前单条规范化 `triage` 请求文字出站边界；
- 已实现 v4 `SupportAssessmentRequest` allowlist 合同及解析验证：载荷只含 `schema_version` 和一条已规范化、
  不进入对象 `repr` 的 `request_text`，闭合 schema 从结构上拒绝所有额外字段；单测覆盖代表性的身份、Reply、
  Thread、权限、配置和 tool 字段拒绝。v4 `SupportSemanticAssessment` 已分离目标、现象与维护深度，且不含
  reason、confidence、lifecycle、action 或 authorization 字段；
- 已实现 transport-neutral 的一次性 assessment service：发送前重新规范验证闭合请求、执行凭据守门，
  client 最多调用一次；无 transport 或请求期失败只返回有界 abstain，不回显 Provider 数据；
- 已实现 Pydantic AI outbound adapter：固定 Prompt 与请求正文分离，正文序列化只含
  `schema_version + request_text`；ADR-0044 已让 Agent 直接消费领域 Pydantic `output_type`，OpenCode Go 只
  暴露框架生成的唯一、不可执行 output tool，关闭 instrument，并以 60 秒 / 240 output token 发起最多一次
  Provider 请求。非法 finish、schema 与 transport 错误只产生稳定脱敏错误；
- Matcher / runtime 已经只从当前规范化文字构造闭合请求并调用非可选 assessment service；未配置 transport
  时装配 unavailable service。OpenCode Go 已有独立 semantic-assessment task / API / model / profile / Prompt
  资格表；
- 已用 fake / spy client 和 Agent 模型夹具验证 service 与 adapter 实际序列化后只含允许字段，并证明
  秘密守门或本地结构校验未通过时零请求；真实 Provider 资格与预算接线继续保留同样的网络阻断回归，
  证明 task 资格、费用授权或其他调用前条件未满足时零请求。

## 相关文档

- [ADR-0037：把语义 assessment 作为 triage 的正式默认路径](0037-make-semantic-assessment-the-default-triage-path.md)
- [ADR-0041：准入 OpenCode Go 工具输出式语义 assessment](0041-qualify-opencode-go-tool-output-for-support-semantics.md)
- [ADR-0011：公开按资格门装配的模型配置](0011-expose-disabled-qualified-model-configuration.md)
- [支持入口分流](../architecture/flows/support-intake-routing.md)
- [模型 Provider 支持矩阵](../architecture/model-provider-support.md)
