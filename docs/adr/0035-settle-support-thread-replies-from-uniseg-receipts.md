# ADR-0035：用经校验的 UniSeg Receipt 结算支持 Thread 出站引用

| 状态 | 决策日期 |
|---|---|
| 已采纳；Thread 续接点结算由 ADR-0060 部分替代 | 2026-08-13 |

## 当时遇到了什么

ADR-0031 已确定所有支持轮次都通过显式 `triage` 和结构化 Reply 进入，ADR-0033 又用一次性 Reply
Claim 与单 Thread lease 约束并发和失败关闭。但原实现把“本轮 Triage 回答属于哪个 Thread”交给
OneBot V11 全局 API hook：只有群发送返回 `message_id` 时，适配器 Provider 才能从 Matcher state 结算
Thread。这让当前支持 Matcher 拥有的发送事务依赖 OneBot 专属 hook，也使 Discord 已具备 UniSeg 发送与
Reply 往返能力时仍不能续接 Thread。

OneBot 出站 Provider 还承担另一项独立职责：把 Matcher 内 Bot 输出关联到当前运行证据 correlation ID。
运行证据关联需要观察适配器 API；支持 Thread 只关心 Triage 当前这一次回答。两种状态的所有者、索引和
失败语义不同，不应继续由同一个全局 hook 结算。

锁定的 Alconna / UniSeg 发送接口会为当前 `AlconnaMatcher.send(UniMessage)` 返回 `Receipt`，其中包含
当前 Bot、发送上下文、exporter 和平台原始发送结果。它比全局 hook 更接近 Thread 发送事务，但
`Receipt.get_reply()` 本身不会替插件验证 Bot、场景、结果数量或平台返回结构，因此不能直接作为可信边界。

## 决策

1. ADR-0031 的显式 `triage`、精确 Reply 选择 Thread、不使用 Waiter，以及 ADR-0033 的一次性 Claim、
   单 Thread lease、发送失败关闭全部保持不变。本 ADR 只替换支持回答的出站引用结算来源。
2. 任何需要建立下一条 Thread 续接点的回答，都由当前 Alconna Matcher 执行“发送并结算”：发送前从
   Matcher state 一次性取走本轮 binding，调用 `AlconnaMatcher.send(UniMessage)`，只用该次调用返回的
   UniSeg `Receipt` 结算，然后以不携带消息的 `finish()` 结束 Matcher，避免第二次发送。run
   postprocessor 只兜底关闭未被发送边界取得的 binding。
3. `Receipt` 只是发送结果载体，不是信任根。结算前必须确认：对象确为 `Receipt`；`receipt.bot` 是当前
   Bot；exporter adapter 与当前 Bot adapter 相同；恰好只有一个平台发送结果；Receipt 上下文可解析为与
   当前 `MsgTarget` 完全相同的稳定会话 scope；exporter 可构造结构化 `Reply`；消息 ID 非布尔、非空且
   有界。多条发送结果没有唯一可回复答案，必须失败关闭，不能任取第一条。
4. 当前验证范围只接受 OneBot V11 与 Discord 的平台原始发送结构。OneBot V11 必须返回含合法
   `message_id` 的 Mapping，并且 UniSeg `Reply.id` 与它一致；Discord 必须返回真实 `MessageGet`，其
   `id` 为正 Snowflake、`channel_id` 与当前 Target 一致，并且 `Reply.id` 与 `id` 一致。其他 exporter
   即使能构造 Reply，也要在完成往返合同测试后才能获准建立 Thread 续接点。
5. 默认仍只读取入站 `Reply.id`。仅当 adapter 协议必须区分直接回复与其他引用种类时，入口可以瞬时读取
   结构化 origin 的引用类型与消息 ID，并要求 origin ID 与统一 `Reply.id` 等值；不能读取 origin 正文、作者
   或其他字段，也不能保存 origin。Discord `MessageReferenceType.FORWARD` 不是直接回复，不能恢复 Thread；
   带显式 `triage` 时按新请求处理。
6. 发送抛错、任务取消、Receipt 缺失或畸形、Bot / adapter / Target 不一致、多结果、平台结构校验失败或
   Thread 提交失败，都会终止 reservation / lease 并关闭 Thread；已经消费的旧 Reply 不复活。回答可能
   已经到达平台但无法登记续接点时，不重发消息，用户仍可发送新的完整 `triage` 请求。
7. OneBot V11 全局出站 Provider 继续只负责运行证据 correlation 关联，不再读取、提交或清理 Thread
   binding。运行证据 HMAC 索引与 Thread HMAC 索引保持隔离。
8. 领域核心继续只接收 adapter、Bot、场景、actor 与消息引用形成的摘要作用域，不依赖 UniSeg、OneBot
   或 Discord 类型；平台原始发送结果只在适配层当前调用栈内校验，不持久化。

## 为什么这样选

- 当前 Matcher 是 Thread binding 与本次发送的明确所有者，直接消费其返回 Receipt 可避免全局 hook 的
  时序竞争和重复结算。
- 同一条路径复用 UniSeg 已完成的跨平台发送与 Reply 构造，同时仍由插件验证自身的 Thread 归属和失败语义。
- 严格绑定 Bot、adapter、Target、单条结果和平台结构，可防止把错误会话、批量拆分结果或形似消息 ID 的
  任意对象登记为续接点。
- 保留 OneBot Provider 的运行证据职责，不会为了扩展 Thread 续问而削弱既有 correlation 覆盖。

## 没有采用的方案

- 为 Discord 再实现一个全局 API hook Provider：会复制 OneBot 专属结算模式，并继续把当前发送事务交给
  全局观察器。
- 直接信任 `Receipt.get_reply()`：它不能证明当前 Bot、Target、唯一结果和平台原始对象都匹配。
- 只要没有专用 Provider 就禁用 Thread：会忽略当前 Matcher 已返回的发送 Receipt，制造不必要的平台分叉。
- 用 Waiter 保持旧 handler：不会解决出站消息 ID 的可信结算，还会重新引入悬挂任务和普通消息归属问题。

## 带来的影响

- OneBot V11 的支持 Thread 不再局限于群 API hook；群聊和私聊可走同一 Receipt 结算路径。
- Discord 频道与私聊的事件模型、Receipt 结构和 Reply ID 往返已有合同测试；真实 Discord 网关仍需独立
  smoke，不能把本地合同测试描述成线上验证。
- 其他 UniSeg adapter 的普通 `triage` 入口不受影响；未验证或校验失败的 Reply 按新请求处理。
- 一条回答若被 exporter 拆成多条平台消息，不会建立 Thread 续接点。这是为保持“最近一条可精确回复的
  回答”不变量而接受的失败关闭。

## 落实与确认

- `src/nonebot_plugin_triage/support_responses.py` 严格解析 UniSeg Receipt，并在发送边界结算或失败关闭
  Thread binding。
- `src/nonebot_plugin_triage/handlers.py` 让所有可续接教学 / 澄清回答经过统一发送 helper；终局回答继续
  直接结束，不建立新续接点。
- `src/nonebot_plugin_triage/thread_references.py` 提供统一 settle / fail；
  `src/nonebot_plugin_triage/onebot_v11_references.py` 只保留运行证据 correlation。
- 单元与事件模型合同测试覆盖 OneBot / Discord 合法回执、Snowflake 规范化、频道 / 私聊 Target、Discord
  Forward 排除、错 Bot / Target、多结果和畸形平台对象；OneBot Matcher 集成测试另覆盖发送异常 / 取消、
  latest-only 和失败关闭。

## 替代关系

- 部分替代 [ADR-0031](0031-require-triage-for-support-thread-continuation.md) 第 5 项“Thread 续接必须依赖
  适配器专用出站 Provider、当前仅 OneBot V11 群发送”的决定；保留其余显式入口和 Thread 生命周期。
- 细化 [ADR-0033](0033-serialize-support-thread-turns-with-single-use-reply-claims.md) 的“适配器确认发送成功
  并取得合法 message ID”：确认来源改为当前 Matcher 返回并经插件严格校验的 UniSeg Receipt；Claim、
  lease 和提交时机不变。
- [ADR-0060](0060-use-scope-thread-and-post-route-conversation-context.md) 不再用 Receipt message ID 建立 Thread
  续接点；当前 Matcher 拥有发送事务、发送失败关闭，以及运行证据关联与 Thread 状态隔离的边界仍保留。
- 极窄地替代 [ADR-0006](0006-cross-platform-alconna-entry-and-reference-providers.md) 第 2 项和 ADR-0031
  第 3 项绝对不读 Reply origin 的边界：仅允许读取 adapter 的结构化引用类型与消息 ID 并交叉校验；不读取
  正文、作者或其他字段。
  ADR-0006 的运行证据出站 Provider 分层继续保留。
- 服从 [ADR-0023](0023-defer-orm-until-durable-business-state.md) 的单进程短期内存策略。

## 相关文档

- [triage 自然语言支持入口](../architecture/flows/support-intake-routing.md)
- [跨平台 triage 支持入口](../architecture/flows/cross-platform-report-intake.md)
- [OneBot V11 引用关联](../architecture/flows/onebot-v11-reply-reference-correlation.md)
