# 流程：triage 自然语言支持入口

## 当前入口

普通用户发送 `triage <求助内容>`，也可以写成 `@Bot triage <求助内容>`。`triage` 必须出现；插件不处理
其他普通消息。Reply 可选，只提供关联消息和运行证据，不决定用户意图。

```text
triage + request text + optional Reply
                 ↓
场景、长度、入口限流和最小上下文边界
                 ↓
目标意图边界 → strict IntakeSignals
   ├─ capability_guidance → 显式 public 命中 → 说明用法；不建 incident
   │                      └─ SUPERUSER + 影子索引 → 带披露标签的候选；不建 incident
   ├─ usage_error         → 解释错误或追问；不建 incident
   ├─ suspected_incident  → 可选 Reply 关联 → LiveIncident + 窄回执
   ├─ out_of_scope        → 说明范围；不建 incident
   ├─ unsafe              → 拒绝；不调用工具
   └─ 不确定              → 只追问一个关键问题
```

当前已接入 `triage` 自由文本参数、确定性首轮意图和公开能力说明。首轮实现只可靠区分
`capability_guidance`、`suspected_incident` 和“不确定”；上图中的 `usage_error`、`out_of_scope` 与
`unsafe` 是统一 Agent 入口的目标分流，尚未接入当前 Matcher。模型资格表仍为空，因此尚未启用模型 Agent；
不明确、否定或假设性请求会得到一次澄清，而不是被强行记成故障。

## 输入与数据边界

- 当前请求文字只在入口和意图适配层瞬时使用；`IntakeSignals`、`LiveIncident`、trial 和运行证据不保存原文；
- 文字、插件元数据和 Reply 都是不可信证据，不能直接变成命令执行、工具调用或维护动作；
- Reply 只读取第一个结构化 `id`，不读取 `msg` / `origin`；
- Reply 缺失或引用过期不妨碍求助；疑似故障会明确标记为未关联运行证据，不猜测其他消息；
- 所有求助先经过不保存平台身份的轻量 HMAC 限流；疑似故障再经过独立的建单限流；
- 普通用户能力说明采用显式公开 Provider。未登记、`CommandMeta.hide=True` 或停用的 Alconna 命令不展示；
  SUPERUSER 在模型外鉴权通过后可检索影子的 `public / review / restricted`，但必须保留候选和执行资格未知提示；
  影子字段在回显前中和 mention 与 Unicode 控制字符；两条路径都不会为回答问题重新执行命令 `parse()`、
  behavior、executor、Rule、Permission 或 handler；
- 私聊当前拒绝；普通用户不能读取 incident 摘要，查询、反馈和统计仍由 `SUPERUSER` 权限保护。

## 领域分流顺序

`route_intake` 继续按固定优先级处理严格信号：安全拒绝优先，其次是冲突补问、无关请求、命令错误、运行
失败或显式故障、能力请求，最后才是信息不足补问。`runtime_status=succeeded` 不能证明用户观察到的行为正确；
命令少参数或权限不足也不能直接升级成插件故障。

只有 `suspected_incident` 后续进入技术责任层。能力说明和用法纠错不会污染 incident、trial 或失败聚类。

## 相关决定

- [ADR-0003：统一能力导航与故障入口](../../adr/0003-unified-capability-guidance-and-incident-intake.md)
- [ADR-0020：triage 自然语言入口与可选 Reply](../../adr/0020-use-triage-command-for-natural-language-support.md)
- [ADR-0022：SUPERUSER 能力影子候选检索](../../adr/0022-limit-capability-shadow-guidance-to-superusers.md)
- [Alconna 能力与解析回执](alconna-capability-and-parse-receipts.md)
- [运行观察入口](runtime-observation-intake.md)
