# 跨平台 triage 支持入口

## 当前可运行流程

```text
任意 UniSeg 支持的消息事件
    ├─ NoneBot event pre-hook → correlation ID → Matcher / API 最小运行观察
    └─ UniSeg target + message ID → HMAC 引用索引

[可选 @Bot] triage <自然语言> [可选 Reply]
    │
    └─ on_alconna + MultiVar(str, "*")
         ├─ MsgTarget → 入口 HMAC 限流 → 统一意图分流
         ├─ OriginalUniMsg → 只取第一个 Reply.id
         └─ 非空文字 → required assessment service → pure router

未配置与已配置的合格 transport 共用控制流，但可达动作不同：

非空自由文本 → 版本化语义 assessment（默认路径，无产品启用开关）
              ├─ 未配置 / 请求期失败 → abstain → 单次澄清
              ├─ 功能 / 用法 signal → router → 公开证据域 → UniMessage
              └─ incident goal + observation signal + 模型外可信失败 → router 签发精确请求授权
                   ├─ Reply 命中且再次确认失败 → LiveIncident + 窄回执
                   │                              ├─ 活动 cluster
                   │                              └─ observe trial → 本地轮转 JSONL
                   └─ 无 Reply / 未命中 / 无失败 → 澄清，不建单且不猜测消息
```

续问仍通过同一个 Alconna `triage` 入口：用户发送 `triage <续问>` 并精确 Reply 到 Triage 已登记且未过期的
最近回答时，独立 HMAC Thread 协调器原子消费该 Reply，并为同一 Thread 取得单个 active turn lease；不读取
被回复正文。Alconna / UniSeg 负责提供统一 Reply / Target，Thread 协调器负责 Triage 特有的归属、作用域、
TTL、latest-only 和并发判断。新回答通过当前 Matcher 返回的单条 UniSeg Receipt 严格校验 Bot、adapter、
Target 与平台结果后才登记新的续接点，处理、取消或发送失败不会恢复旧 Reply。

`@Bot` 由 NoneBot / 适配器预处理，入口本身不要求 `to_me()`。`triage` 在每轮都必选，所以插件不会把普通
群聊或任何只有 Reply 的消息交给意图层。Reply 未命中 Thread 时，显式请求仍按新的 `triage` 处理。

被回复消息如果是入站事件，通用引用桥已经登记其运行证据引用。Bot 主动输出的运行证据 correlation 仍需
适配器出站 Provider 回填，当前只实现 OneBot V11 群发送；支持 Thread 则直接结算当前 Triage Matcher 的
UniSeg Receipt，不再依赖该全局 Provider。引用失败时仍处理求助，只是不续接旧 Thread 或运行证据。

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
| `triage` + 精确回复 Triage 回答续问 | 群聊 / 私聊 Receipt 合同测试通过；每轮重新限流 | Discord 频道 / 私聊合同测试通过；真实网关待 smoke；其他平台待验证 |
| 公开结果发送 | `UniMessage` 支持 | 由对应 exporter 转换 |

## 数据边界

- 当前请求文字只用于本次意图判断和回答，不写入 `LiveIncident`、trial 或运行证据；v5 远端请求合同闭合为
  `schema_version + request_text`，其中 `request_text` 必须是当前单条规范化文字。OpenCode Go Agent adapter
  已按这个闭合投影序列化，并由 Pydantic AI `output_type` 生成唯一不可执行 output tool；Matcher / runtime
  已接 required
  service，未配置 transport 时 unavailable service 不会发送该对象；
- Reply 默认只读结构化 `id`；为排除 Discord Forward 只瞬时读取结构化引用类型和消息 ID，并与统一 ID
  交叉校验；不读取或保存正文、作者或其他 origin 字段；
- adapter、Bot、Target、actor 和 message 标识只瞬时参与 HMAC；
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
| OpenCode Go `Agent(output_type=SupportSemanticAssessment)` 单 output-tool client、一次性失败关闭与确定性授权路由；授权绑定精确 `LiveReportRequest` 且只可消费一次 | `src/nbtriage/opencode_go_semantic_adapter.py`、`src/nbtriage/support_semantic_model_adapter.py`、`src/nonebot_plugin_triage/semantic_runtime.py`、`src/nonebot_plugin_triage/semantic_assessment.py`、`src/nbtriage/support_routing.py` |
| SUPERUSER 鉴权后的行为探索候选（取证待接） | `src/nonebot_plugin_triage/handlers.py` |
| 通用入站引用与 Target scope | `src/nonebot_plugin_triage/universal_references.py` |
| OneBot V11 运行证据出站引用 Provider | `src/nonebot_plugin_triage/onebot_v11_references.py` |
| Thread 状态、精确回复与 Receipt 结算 | `src/nbtriage/support_threads.py`、`src/nonebot_plugin_triage/thread_references.py`、`src/nonebot_plugin_triage/support_responses.py` |
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
