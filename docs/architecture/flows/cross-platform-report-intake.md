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
              ├─ Behavior（非准入场景不执行）→ reported_observation 转 Bug，否则拒绝
              └─ 私聊 + SUPERUSER → 语义前直接进入全局维护者原生消息会话
                           → Capability Shadow / 项目只读文件 → 自然语言回答
```

续问仍通过同一个 Alconna `triage` 入口。只有首轮未解决时，独立 HMAC Thread 协调器才在
`adapter + Bot + conversation + actor` 作用域等待下一条显式 `triage`；不要求 Reply，且最多消费一次补充。
新回答成功发送即可提交等待状态，不再依赖 Receipt message ID。并发 Claim 返回 `BUSY`，处理、取消、发送
失败、TTL 或第二轮结束都会关闭 Thread。

这段一次补充合同只描述普通 Support Thread。Behavior 另有部署内唯一的长期维护者会话，但只准入
`私聊 + SUPERUSER`：私聊中每条显式 `triage` 都在语义前创建一次新 Run（不经过意图分类），Run 结束后原生
消息历史仍保留；群聊、频道和私聊非 SUPERUSER 不进入维护者会话，语义 router 若输出 `behavior_exploration`
则带 `reported_observation` 的转入 Bug 判定，否则明确拒绝。全局并发 Run
返回 `BUSY`。`triage 停止` 取消运行并保留快照，`triage 开始新对话` 取消后清空全局会话。

`@Bot` 由 NoneBot / 适配器预处理，入口本身不要求 `to_me()`。`triage` 在每轮都必选，所以插件不会把普通
群聊或任何只有 Reply 的消息交给意图层。Reply 不选择 Thread，只在 router 选出 action 后提供可见上下文。

被回复消息如果是入站事件，通用引用桥已经登记其运行证据引用。Bot 主动输出的运行证据 correlation 仍需
适配器出站 Provider 回填，当前只实现 OneBot V11 群发送。引用失败时仍处理求助，只是不能取得该条消息的
运行证据；它不影响 scope Thread 的归属。

## 已采纳目标与当前差距

行为探索/维护者会话只准入 `私聊 + SUPERUSER`：准入后任何 `triage` 内容都在语义路由前直接进入全局维护者
会话；其他场景不进入维护者会话，语义输出 `behavior_exploration` 时带 `reported_observation` 的转入 Bug
判定，否则明确拒绝。Agent 可读取 Capability Shadow 和项目根目录的只读文件；每次模型请求和工具执行前都会
重新鉴权，文件工具继续硬拒绝凭据类路径。分类本身不消费身份；私聊的准入检查在限流后、语义前执行，不扩大
Semantic / Guidance / Bug payload。

## 支持矩阵

| 能力 | OneBot V11 | QQ 官方及其他 UniSeg 适配器 |
|---|---|---|
| `triage <自由文本>`，无 `@Bot` | 已做 Matcher 与服务测试 | 入口无专属类型；尚未逐平台端到端测试 |
| 私聊 `triage <自由文本>` | 已允许进入统一分流；私聊 + SUPERUSER 在语义前直接进入维护者会话，其余走与群聊一致的鉴权规则 | 合同相同；尚未逐平台端到端测试 |
| `@Bot triage <自由文本>` | 依赖 NoneBot 标准 `to_me` 预处理 | 依赖对应适配器标准预处理 |
| Reply / Target | 已用真实事件模型测试 | Discord 频道 / 私聊事件模型已做合同测试；其他平台待验证 |
| 回复入站消息并关联 | 支持 | exporter 可提供 target 与 message ID 时支持 |
| 回复 Bot 输出并关联运行证据 | 当前支持群发送 | 尚未实现运行证据出站 Provider |
| 同 scope 下一条 `triage` 补充 | 群聊 / 私聊合同测试通过；Reply 可选；每轮重新限流 | 领域合同与 Adapter 无关；真实网关待 smoke |
| 全局维护者会话 | Handler 合同覆盖鉴权、自然对话、停止与新对话；原生消息文件重建后的恢复由 runtime 测试覆盖 | 单插件进程协调；真实 Bot 进程重启与长上下文压缩仍需部署 smoke |
| 路由后直接 Reply 正文 | OneBot V11 事件模型已覆盖 | 取决于 UniSeg Builder 是否提供 Reply 正文；不可用时明确降级 |
| Bug 最新聊天窗口 | NapCat 群历史 Provider 已实现；省略 `message_seq` 一次读取最新最多 30 条；精确 Reply 独立预装 | 尚无跨 Adapter 历史 Provider；不暴露历史工具，只使用当前请求、可用的精确 Reply 和其他证据 |
| 公开结果发送 | `UniMessage` 支持 | 由对应 exporter 转换 |

## 数据边界

- 当前请求文字只用于本次意图判断和回答，不写入运行证据；v7 远端请求合同闭合为
  `schema_version + request_text`，其中 `request_text` 必须是当前单条规范化文字。当前客户端由 Pydantic AI
  原生 Provider 序列化该投影，并通过 `output_type` 生成唯一不可执行 output tool；Matcher / runtime
  已接 required
  service，未配置 transport 时 unavailable service 不会发送该对象；
- 直接 Reply 的可见正文可以在路由后进入 Guidance / Bug；正文不做凭据或个人信息遮蔽。Bug 最新窗口还可
  投影判断群聊关系所需的会话 / 消息 / 发言人 ID、角色、Reply 关系与段元数据；这些字段不授予权限。
  平台 transport envelope、scope 和 correlation 不进入模型；
- 运行引用索引中的 adapter、Bot、Target、actor 和 message 标识只瞬时参与 HMAC；OneBot Bug conversation
  Provider 可在单次 assessment 生命周期内持有当前 Bot 与群的原始调用 scope，但 Agent 只能调用无参数工具，
  不能提交或切换会话；窗口字段不持久化；
- 所有求助只在统一 `triage` 入口经过一次轻量 HMAC 限流；
- 当前链路不运行用户文字中的命令，不创建 Issue，不修改配置，也不重启 Bot。

## 代码映射

| 边界 | 实现 |
|---|---|
| `triage` Matcher、每轮 assessment / routing 与公开能力组件 | `src/nonebot_plugin_triage/handlers.py`、`src/nonebot_plugin_triage/support/intake.py`、`src/nonebot_plugin_triage/runtime.py` |
| 版本化 assessment 请求投影、需求信号与失败状态合同 | `src/nbtriage/support/semantics.py` |
| Pydantic AI `Agent(output_type=SupportSemanticAssessment)`、一次性失败关闭与确定性 action 路由 | `src/nbtriage/support/_model_adapter.py`、`src/nonebot_plugin_triage/support/semantic_runtime.py`、`src/nonebot_plugin_triage/support/semantic.py`、`src/nbtriage/support/routing.py` |
| SUPERUSER 鉴权后的全局维护者对话、原生消息快照、Harness 压缩与只读工具 | `src/nbtriage/behavior/conversation_agent.py`、`src/nonebot_plugin_triage/behavior/conversation_store.py`、`src/nonebot_plugin_triage/behavior/contracts.py`、`src/nonebot_plugin_triage/behavior/service.py`、`src/nonebot_plugin_triage/behavior/runtime.py`、`src/nonebot_plugin_triage/handlers.py` |
| 通用入站引用与 Target scope | `src/nonebot_plugin_triage/universal_references.py` |
| OneBot V11 运行证据出站引用 Provider | `src/nonebot_plugin_triage/onebot_v11_references.py` |
| scope Thread、一次补充与发送成功结算 | `src/nbtriage/support/threads.py`、`src/nonebot_plugin_triage/support/threads.py`、`src/nonebot_plugin_triage/support/responses.py` |
| Bug Reply / OneBot 群历史上下文 | `src/nbtriage/bug/conversation.py`、`src/nonebot_plugin_triage/bug/onebot_v11_conversation.py` |
| HMAC 引用索引 | `src/nbtriage/message_references.py` |

## 相关决定

- [ADR-0006：跨平台 Alconna 入口与引用 Provider](../../adr/0006-cross-platform-alconna-entry-and-reference-providers.md)
- [ADR-0020：triage 自然语言入口与可选 Reply](../../adr/0020-use-triage-command-for-natural-language-support.md)
- [ADR-0022：只向 SUPERUSER 接入能力影子候选检索](../../adr/history/0022-limit-capability-shadow-guidance-to-superusers.md)
- [ADR-0028：允许 triage 私聊并向 SUPERUSER 原会话返回行为解释](../../adr/0028-allow-private-triage-and-superuser-request-context-replies.md)
- [ADR-0030：免命令精确回复续问（已替代）](../../adr/history/0030-continue-support-thread-by-exact-reply.md)
- [ADR-0031：支持 Thread 续问仍要求显式 triage](../../adr/history/0031-require-triage-for-support-thread-continuation.md)
- [ADR-0033：用一次性 Reply Claim 串行化支持 Thread 处理轮](../../adr/history/0033-serialize-support-thread-turns-with-single-use-reply-claims.md)
- [ADR-0035：用经校验的 UniSeg Receipt 结算 Thread 出站引用](../../adr/history/0035-settle-support-thread-replies-from-uniseg-receipts.md)
- [ADR-0037：把语义 assessment 作为 triage 的正式默认路径](../../adr/0037-make-semantic-assessment-the-default-triage-path.md)
- [ADR-0038：限定语义 assessment 的远端数据投影](../../adr/0038-limit-semantic-assessment-remote-data-projection.md)
- [ADR-0044：语义 assessment 直接使用 Pydantic AI Agent output_type](../../adr/history/0044-use-pydantic-ai-agent-output-type-for-support-semantics.md)
- [ADR-0046：统一行为探索目标](../../adr/history/0046-merge-internal-reasoning-into-behavior-exploration.md)
- [ADR-0060：用作用域 Thread 承接一次补充并在路由后投影会话上下文](../../adr/0060-use-scope-thread-and-post-route-conversation-context.md)
- [ADR-0061：为 Bug 判断读取当前会话最新有界聊天窗口](../../adr/history/0061-read-latest-bounded-conversation-window-for-bug-assessment.md)
- [ADR-0064：收窄 Bug 会话证据与结论合同](../../adr/history/0064-refine-bug-conversation-evidence-and-verdict-contract.md)
- [ADR-0065：只为明确支持的平台提供 Bug 会话历史工具](../../adr/0065-only-expose-conversation-history-for-supported-platforms.md)
- [ADR-0128：用原生消息快照保存维护者自由对话](../../adr/0128-use-native-message-snapshots-for-maintainer-conversations.md)
