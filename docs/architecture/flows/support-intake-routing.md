# 流程：显式支持入口的确定性分流

## 这条流程保证什么

未来 NoneBot 插件从用户主动 `@Bot` 或回复消息开始支持流程，但用户文本不能直接决定是否调用诊断工具。
传输、权限、解析和模型前安全边界先产生不含原文与身份的 `IntakeSignals`；领域路由器再按固定优先级选择
教学、纠错、疑似故障、说明范围、拒绝或单步补问。当前已有 Alconna 能力快照和真实解析结果的纯适配器，
但仍不读取真实群聊，也不注册 NoneBot / QQ 钩子。

## 外部参与者与输入边界

- 用户必须使用 `mention` 或 `reply_report` 显式触发；静默监听不是合法 trigger；
- 未来传输边界负责权限、限流与最小上下文提取，只有不透明 `intake_id` / `correlation_id` 进入核心；
- 意图理解边界只能提交 `discover_capability`、`report_problem` 或 `unknown`；
- 命令解析边界提交真实的未知命令、前缀、参数、权限、场景、停用或解析成功状态；
- 模型前安全策略提交 `unsafe_detected`，该字段不是让 LLM 自行决定的普通分类标签；
- 消息正文、命令原文、用户 / 群 ID、Prompt 或任意额外字段被 schema 拒绝。

## 稳定分流顺序

```text
trusted boundaries → strict IntakeSignals
                           ↓
unsafe? ── yes ─────────→ unsafe / refuse
  │ no
  ↓
signals conflict? ─ yes → no disposition / ask_one_question
  │ no
  ↓
explicitly unrelated? ─→ out_of_scope / explain_scope
  │ no
  ↓
command rejected? ─────→ usage_error / explain_command_error / wait for retry
  │ no
  ↓
runtime failed or user reported a problem?
  ├─ yes ──────────────→ suspected_incident / start_diagnosis
  └─ no
      ↓
capability requested? ─→ capability_guidance / show_capability
  │ no
  └────────────────────→ no disposition / ask_one_question
```

`unsafe` 优先级不可被能力询问、解析错误或运行失败覆盖。命令解析错误先于故障诊断，避免把少写参数、权限
不足或插件停用误报给插件维护者。`runtime_status=succeeded` 与 `report_problem`，或明确无关却携带命令 / 运行
结果等矛盾组合只触发一次补问，不强行选择责任层。

## 输出与后续边界

`IntakeDecision` 只保存 disposition、固定动作、固定原因码和是否需要用户继续回复。它不生成自然语言，
不调用模型或工具，也不修改现有 `ResponsibilityLayer`。只有 `suspected_incident` 可以在后续把关联运行证据
转换为 `SupportCase` 并进入技术责任诊断。

`capability_guidance` 与 `usage_error` 已有当前注册命令的结构化 Alconna 能力事实和最小解析回执可用，但
权限 / 场景过滤、群内解释文案和逐项补参尚未实现；MVP 即使以后生成可复制指令，也不能自动代用户执行
有副作用能力。

## 失败语义

- schema 版本、时间、枚举、ID、布尔值、缺失字段或额外字段不合法时拒绝整份信号；
- 手工构造并篡改的 `IntakeSignals` 在路由前重新校验，不能绕过严格 schema；
- 信息不足不等于 `out_of_scope`，信号冲突不等于 `suspected_incident`；二者都停在单步补问；
- 路由器不验证上游安全、意图、解析与运行信号的真实性；真实适配器必须分别提供可审计来源。

## 相关决定

- [ADR-0003：统一能力导航与故障入口](../../adr/0003-unified-capability-guidance-and-incident-intake.md)
- [Alconna 能力与解析回执](alconna-capability-and-parse-receipts.md)
- [运行观察入口](runtime-observation-intake.md)
