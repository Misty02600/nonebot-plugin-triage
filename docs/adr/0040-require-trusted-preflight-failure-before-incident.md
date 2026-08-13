# ADR-0040：只有可信初检仍失败才进入 incident

| 状态 | 决策日期 |
|---|---|
| 已采纳；Incident 专用限流阶段由 ADR-0045 部分替代 | 2026-08-13 |

> [ADR-0043](0043-separate-support-goals-observations-and-maintenance-depth.md) 进一步收紧当前条件：
> 除现象与可信失败外，还必须识别到用户明确的 `incident_intake` 目标。

## 当时遇到了什么

[ADR-0037](0037-make-semantic-assessment-the-default-triage-path.md) 已把语义 assessment 限制为需求信号，
但“用户报告了现象”和“系统已确认存在需要诊断的故障”仍容易在实现或测试中被混用。如果模型仅凭
`reported_observation` 就能触发建单，那么普通抱怨、误用、权限不足、引用内容或提示词注入仍可能把请求
升级为 incident，进而进入后续 Agent 工作流。

项目已有模型外的 Reply correlation 和最小运行观察，可以先验证同一条被回复消息是否真的出现失败。
解析、权限、场景和能力回执尚未完整接入在线入口，因此当前不能把“未来会有更多初检信号”写成已经具备。

## 决策

1. `SupportSemanticAssessment v2` 只报告闭集需求信号；`reported_observation` 永远表示尚未验证的用户陈述。
   schema 不包含 lifecycle、action、disposition、authorization 或自由文本回答。
2. 确定性 router 只有同时收到 `reported_observation` 与模型外可信失败证据时，才可选择 `OPEN_INCIDENT`。
   安全拒绝仍具有最高优先级；可信失败一旦成立，则优先于同轮的指导或原因需求进入故障受理。
3. 当前在线唯一可升级来源是：结构化 Reply 精确关联到同一 Bot / 场景 / 消息，且近期
   `RuntimeEvidenceBundle` 至少包含一条 `FAILED` observation。没有 Reply、引用未命中、只有成功生命周期、
   空证据或读取异常都保持未验证并澄清。
4. router 只为本轮精确 `LiveReportRequest` 签发不可复制、不可序列化、一次性消费的进程内授权。
   `LiveReportService` 在建单限流、生成 incident ID 和写状态前，必须再次解析同一 Reply 并确认失败仍成立；
   证据丢失或不一致时失败关闭。
5. 未来接入公开用法、参数解析、权限、场景或能力启用回执时，应先在专用 handler 中解释可确定的误用；
   只有可信回执表明调用有效且结果仍失败，才可扩展为新的模型外升级来源。新增来源需要类型化合同和独立测试，
   不能把用户原文、模型摘要或模型选择的 span 当作可信回执。
6. 澄清续问本身没有原始失败消息的运行关联，因此不能仅凭第二轮再次报告现象建单。用户需重新发起带有
   可验证 Reply 或未来结构化回执的 `triage` 请求。

## 原因与影响

- LLM 只负责窄意图/需求识别，不掌握 incident 或 Agent 的入口能力，提示词注入最多影响候选信号；
- 无证据请求不会消耗建单限流、创建 incident、启动 trial 或进入后续 Agent；
- 同一 Reply 在 handler 初检与服务提交前复核，牺牲一次本地内存读取来换取更清晰的副作用边界；
- 当前召回率会更保守：没有可关联失败回执的真实问题只能得到澄清，而不能先建一个“无证据 incident”。

## 没有采用的方案

- **让模型输出 `open_incident`**：概率性输出不能成为权威生命周期授权。
- **用户说“报错”就建单**：固定文字和语义分类都不能证明正确调用仍失败。
- **先建无证据 incident、之后再补证**：会让闲聊、误用和注入提前产生状态、限流与 Agent 成本。
- **成功生命周期也按行为问题建单**：Matcher 完成不证明用户观察正确，但也不构成可信失败。

## 替代关系

- [ADR-0045](0045-use-one-triage-cooldown-and-localstore-capability-cache.md) 只替代本 ADR 在
  `LiveReportService` 中执行第二层建单限流的阶段；可信失败复核、精确请求绑定和一次性授权继续有效。
- 收紧 [ADR-0037](0037-make-semantic-assessment-the-default-triage-path.md) 的 incident 可达条件；其每轮
  assessment、零工具、失败关闭和模型外最终决策继续有效。
- 收紧 [ADR-0003](0003-unified-capability-guidance-and-incident-intake.md) 与
  [ADR-0020](0020-use-triage-command-for-natural-language-support.md) 中“报告问题”进入疑似故障的历史表述。

## 落实与确认

- `SupportSemanticAssessment v2` 已删除 lifecycle 字段，legacy `incident_lifecycle_request` 会被闭合 schema 拒绝；
- handler 只在 assessment 包含 `reported_observation` 后读取 Reply 对应的运行证据；读取失败按无可信证据处理；
- support router 只对 `reported_observation + trusted_runtime_failure` 签发绑定请求的一次性授权；
- `IntakeSignals v1` 的历史 wire 值 `report_problem` 继续兼容，但在无运行失败时只表示
  `REPORTED_FAILURE_UNVERIFIED` 并进入澄清；
- `LiveReportService` 只接受仍能复核为 `RuntimeStatus.FAILED` 的关联 Reply，并在复核后才写状态；每轮已在
  统一 `triage` 入口限流，不再执行 Incident 专用二次限流；
- 单元与 Matcher 集成测试覆盖无 Reply、引用未命中、成功/空回执、可信失败、授权换请求和重放。

## 相关文档

- [支持入口分流](../architecture/flows/support-intake-routing.md)
- [跨平台 triage 支持入口](../architecture/flows/cross-platform-report-intake.md)
- [运行观察入口](../architecture/flows/runtime-observation-intake.md)
