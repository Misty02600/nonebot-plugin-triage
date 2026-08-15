# 跨平台 triage 支持入口

## 当前可运行流程

```text
任意 UniSeg 支持的消息事件
    ├─ NoneBot event pre-hook → correlation ID → Matcher / API 最小运行观察
    └─ UniSeg target + message ID → HMAC 引用索引

[可选 @Bot] triage <自然语言> [可选 Reply]
    │
    └─ on_alconna + MultiVar(str, "*")
         ├─ MsgTarget → 入口 HMAC 限流 → scope Thread Claim
         ├─ OriginalUniMsg → 第一个 Reply 的可见正文 + 独立 correlation lookup
         └─ 当前非空文字 → required assessment service → pure router

非空自由文本 → 版本化语义 assessment（默认路径，无产品启用开关）
              ├─ task 未资格 / 请求期失败 → abstain → 唯一一次澄清
              ├─ Guidance → public facts + 路由后 Reply / Thread context → UniMessage
              ├─ Bug / observation → reviewed catalog / bounded Agent → 三值结论
              └─ Behavior → 模型外 SUPERUSER 鉴权；受限取证尚未接通
```

续问仍通过同一个 Alconna `triage` 入口。只有首轮未解决时，独立 HMAC Thread 协调器才在
`adapter + Bot + conversation + actor` 作用域等待下一条显式 `triage`；不要求 Reply，且最多消费一次补充。
新回答成功发送即可提交等待状态，不再依赖 Receipt message ID。并发 Claim 返回 `BUSY`，处理、取消、发送
失败、TTL 或第二轮结束都会关闭 Thread。

`@Bot` 由 NoneBot / 适配器预处理，入口本身不要求 `to_me()`。`triage` 在每轮都必选，所以插件不会把普通
群聊或任何只有 Reply 的消息交给意图层。Reply 不选择 Thread，只在 router 选出 action 后提供可见上下文。

被回复消息如果是入站事件，通用引用桥已经登记其运行证据引用。Bot 主动输出的运行证据 correlation 仍需
适配器出站 Provider 回填，当前只实现 OneBot V11 群发送。引用失败时仍处理求助，只是不能取得该条消息的
运行证据；它不影响 scope Thread 的归属。

## 已采纳目标与当前差距

ADR-0028 已经部分替代分类前统一拒绝私聊的入口边界：当前实现允许私聊进入与群聊、频道相同的
本地守门和意图分类；需要权限的分支都按当前 Bot / Event 的请求者执行相同鉴权。公开教学、用法纠错与
澄清可以原路回复。已采纳的行为探索目标要求在读取
restricted 证据前，先对当前 Bot / Event 的请求者执行模型外 `SUPERUSER` 鉴权；鉴权后可在原始提问会话
返回完整解释，不检查其他参与者，也不要求房间 allowlist 或强制转私聊，但仍执行秘密过滤、文本净化和
模型外发授权。当前 router 已能产生 behavior candidate，Matcher 会在分类后鉴权并按结果给出有界回执；
restricted 取证与解释编排尚未实现。分类本身不消费身份。

这项决定没有开放私聊报障：即使 router 未来签发 `OPEN_INCIDENT` 授权，`LiveReportService` 仍按当前合同
拒绝私聊。报障服务自己的私聊场景检查继续保留。

## 支持矩阵

| 能力 | OneBot V11 | QQ 官方及其他 UniSeg 适配器 |
|---|---|---|
| `triage <自由文本>`，无 `@Bot` | 已做 Matcher 与服务测试 | 入口无专属类型；尚未逐平台端到端测试 |
| 私聊 `triage <自由文本>` | 已允许进入统一分流；鉴权规则与群聊一致；私聊 incident 仍拒绝 | 合同相同；尚未逐平台端到端测试 |
| `@Bot triage <自由文本>` | 依赖 NoneBot 标准 `to_me` 预处理 | 依赖对应适配器标准预处理 |
| Reply / Target | 已用真实事件模型测试 | Discord 频道 / 私聊事件模型已做合同测试；其他平台待验证 |
| 回复入站消息并关联 | 支持 | exporter 可提供 target 与 message ID 时支持 |
| 回复 Bot 输出并关联运行证据 | 当前支持群发送 | 尚未实现运行证据出站 Provider |
| 同 scope 下一条 `triage` 补充 | 群聊 / 私聊合同测试通过；Reply 可选；每轮重新限流 | 领域合同与 Adapter 无关；真实网关待 smoke |
| 路由后直接 Reply 正文 | OneBot V11 事件模型已覆盖 | 取决于 UniSeg Builder 是否提供 Reply 正文；不可用时明确降级 |
| Bug 最新聊天窗口 | NapCat 群历史 Provider 已实现；省略 `message_seq` 一次读取最新最多 30 条；精确 Reply 独立预装 | 尚无跨 Adapter 历史 Provider；不暴露历史工具，只使用当前请求、可用的精确 Reply 和其他证据 |
| 公开结果发送 | `UniMessage` 支持 | 由对应 exporter 转换 |

## 数据边界

- 当前请求文字只用于本次意图判断和回答，不写入 `LiveIncident`、trial 或运行证据；v7 远端请求合同闭合为
  `schema_version + request_text`，其中 `request_text` 必须是当前单条规范化文字。OpenCode Go Agent adapter
  已按这个闭合投影序列化，并由 Pydantic AI `output_type` 生成唯一不可执行 output tool；Matcher / runtime
  已接 required
  service，未配置 transport 时 unavailable service 不会发送该对象；
- 直接 Reply 的可见正文可以在路由后进入 Guidance / Bug；正文不做凭据或个人信息遮蔽。Bug 最新窗口还可
  投影判断群聊关系所需的会话 / 消息 / 发言人 ID、角色、Reply 关系与段元数据；这些字段不授予权限。
  平台 transport envelope、scope 和 correlation 不进入模型；
- 运行引用索引中的 adapter、Bot、Target、actor 和 message 标识只瞬时参与 HMAC；OneBot Bug conversation
  Provider 可在单次 assessment 生命周期内持有当前 Bot 与群的原始调用 scope，但 Agent 只能调用无参数工具，
  不能提交或切换会话；窗口字段不持久化；
- 失败聚类只使用白名单化的 lifecycle / subject / exception / stack module 标识；
- trial 默认关闭；能力问答和澄清不进入 trial；
- 所有求助只在统一 `triage` 入口经过一次轻量 HMAC 限流；建单服务复核授权和失败证据，但不再执行独立
  Incident 限流；
- 当前链路不运行用户文字中的命令，不创建 Issue，不修改配置，也不重启 Bot。

## 代码映射

| 边界 | 实现 |
|---|---|
| `triage` Matcher、每轮 assessment / routing 与公开能力组件 | `src/nonebot_plugin_triage/handlers.py`、`src/nonebot_plugin_triage/support_intake.py`、`src/nonebot_plugin_triage/runtime.py` |
| 版本化 assessment 请求投影、需求信号与失败状态合同 | `src/nbtriage/support_semantics.py` |
| OpenCode Go `Agent(output_type=SupportSemanticAssessment)` 单 output-tool client、一次性失败关闭与确定性 action 路由；旧 `LiveReportRequest` 授权仅为当前 live semantic 不可达的兼容领域能力 | `src/nbtriage/opencode_go_semantic_adapter.py`、`src/nbtriage/support_semantic_model_adapter.py`、`src/nonebot_plugin_triage/semantic_runtime.py`、`src/nonebot_plugin_triage/semantic_assessment.py`、`src/nbtriage/support_routing.py` |
| SUPERUSER 鉴权后的行为探索候选（取证待接） | `src/nonebot_plugin_triage/handlers.py` |
| 通用入站引用与 Target scope | `src/nonebot_plugin_triage/universal_references.py` |
| OneBot V11 运行证据出站引用 Provider | `src/nonebot_plugin_triage/onebot_v11_references.py` |
| scope Thread、一次补充与发送成功结算 | `src/nbtriage/support_threads.py`、`src/nonebot_plugin_triage/thread_references.py`、`src/nonebot_plugin_triage/support_responses.py` |
| Bug Reply / OneBot 群历史上下文 | `src/nbtriage/bug_conversation.py`、`src/nonebot_plugin_triage/onebot_bug_conversation.py` |
| HMAC 引用索引 | `src/nbtriage/message_references.py` |
| 类型化授权校验、故障组合与窄回显 | `src/nonebot_plugin_triage/live_reports.py` |
| incident、cluster 与 trial | `src/nbtriage/live_incidents.py`、`src/nbtriage/live_trials.py` |

## 相关决定

- [ADR-0006：跨平台 Alconna 入口与引用 Provider](../../adr/0006-cross-platform-alconna-entry-and-reference-providers.md)
- [ADR-0014：观察型生产 trial](../../adr/0014-use-observation-first-production-trials.md)
- [ADR-0020：triage 自然语言入口与可选 Reply](../../adr/0020-use-triage-command-for-natural-language-support.md)
- [ADR-0022：只向 SUPERUSER 接入能力影子候选检索](../../adr/0022-limit-capability-shadow-guidance-to-superusers.md)
- [ADR-0028：允许 triage 私聊并向 SUPERUSER 原会话返回行为解释](../../adr/0028-allow-private-triage-and-superuser-request-context-replies.md)
- [ADR-0030：免命令精确回复续问（已替代）](../../adr/0030-continue-support-thread-by-exact-reply.md)
- [ADR-0031：支持 Thread 续问仍要求显式 triage](../../adr/0031-require-triage-for-support-thread-continuation.md)
- [ADR-0033：用一次性 Reply Claim 串行化支持 Thread 处理轮](../../adr/0033-serialize-support-thread-turns-with-single-use-reply-claims.md)
- [ADR-0035：用经校验的 UniSeg Receipt 结算 Thread 出站引用](../../adr/0035-settle-support-thread-replies-from-uniseg-receipts.md)
- [ADR-0037：把语义 assessment 作为 triage 的正式默认路径](../../adr/0037-make-semantic-assessment-the-default-triage-path.md)
- [ADR-0038：限定语义 assessment 的远端数据投影](../../adr/0038-limit-semantic-assessment-remote-data-projection.md)
- [ADR-0040：只有可信初检仍失败才进入 incident](../../adr/0040-require-trusted-preflight-failure-before-incident.md)
- [ADR-0044：语义 assessment 直接使用 Pydantic AI Agent output_type](../../adr/0044-use-pydantic-ai-agent-output-type-for-support-semantics.md)
- [ADR-0046：统一行为探索目标](../../adr/0046-merge-internal-reasoning-into-behavior-exploration.md)
- [ADR-0060：用作用域 Thread 承接一次补充并在路由后投影会话上下文](../../adr/0060-use-scope-thread-and-post-route-conversation-context.md)
- [ADR-0061：为 Bug 判断读取当前会话最新有界聊天窗口](../../adr/0061-read-latest-bounded-conversation-window-for-bug-assessment.md)
- [ADR-0064：收窄 Bug 会话证据与结论合同](../../adr/0064-refine-bug-conversation-evidence-and-verdict-contract.md)
- [ADR-0065：只为明确支持的平台提供 Bug 会话历史工具](../../adr/0065-only-expose-conversation-history-for-supported-platforms.md)
