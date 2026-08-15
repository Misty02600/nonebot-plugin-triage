# 流程：OneBot V11 运行证据引用与独立 scope Thread

> 本文保留早期 OneBot 专属运行证据切片，同时记录 ADR-0060 收紧后的 scope Thread。通用入站绑定与
> 首次用户入口已经由[跨平台显式报障入口](cross-platform-report-intake.md)承接；OneBot Provider 现在只负责
> 群聊运行证据的出站 correlation；Thread 不再依赖 Reply message ID 或 UniSeg Receipt 选择下一轮。

## 目标

让 QQ 群用户后续回复一条已观察消息时，系统能精确恢复该消息对应的本地 `correlation_id`。运行证据引用
子系统不读取或保存 OneBot 消息正文，也不把 Bot / 群 / 用户 ID、原始 `message_id` 或完整 API data / result
写入领域证据；路由后显式 Reply 正文进入 Guidance / Bug 是独立的上下文投影，不属于该引用索引。

## 绑定与解析

```text
NoneBot event_preprocessor
    └─ create correlation_id in event state
               ↓
OneBot group event_postprocessor
    └─ HMAC(adapter, bot scope, group scope, incoming message_id)
               ↓
      bounded in-memory reference index
               ↑
OneBot successful send API in Matcher context
    └─ HMAC(adapter, bot scope, group scope, returned message_id)

later structured reply event
    └─ HMAC(adapter, bot scope, group scope, reply.message_id)
               ↓ exact digest lookup
          correlation_id
               ↓
RuntimeEvidenceBundle → reply-report IntakeSignals → deterministic intake router
```

支持 Thread 使用另一份 scope 索引，不能与运行证据关联互相冒充：

```text
first explicit triage
    └─ HMAC(adapter, bot, target, actor) → active scope Thread + turn lease
               ↓ only when user context is still missing
      successful AlconnaMatcher.send(UniMessage)
               ↓ await one supplement; no message ID required
later explicit triage in the same scope
    └─ atomic claim of the only supplement turn
               ↓ terminal result / second unresolved / failure
      close Thread; later triage starts a new Thread
```

运行证据的入站绑定在事件后处理执行，此时事件预处理写入的 correlation ID 已可从 state 读取。出站绑定只覆盖
Matcher 上下文内成功的 `send_group_msg` / 群 `send_msg`：适配器读取 `group_id`、`message_type` 和结果中的
`message_id` 作为瞬时路由材料，不读取或保存 `message` 与其他结果字段。Matcher 外后台发送不猜测归属。

Thread 续问不注册 OneBot 专用普通消息 Matcher、入站 Provider 或 Thread 出站 hook。Alconna `triage` Matcher
只按当前 `MsgTarget`、Bot 和 actor Claim scope；Reply 不参与归属。首轮直接 Reply 的可见正文与独立解析出的
correlation 可以进入短期 Thread 上下文，路由后分别供 Guidance / Bug 使用。协调器让每个 scope 同时最多有
一个 active turn；只有当前 Matcher 成功发送补充提示后才提交等待状态。OneBot 群聊与私聊共用领域 Thread
路径；原有 OneBot 群 API hook 只保留运行证据职责。API 异常、取消、处理失败或 lease 到期都会关闭 Thread。
只有 Reply、没有 `triage` 的消息不会进入这条流程。

## 隐私与失败语义

- 索引密钥至少 32 字节；原始 scope 和消息引用只参与 HMAC-SHA256，不进入索引 entry、运行 bundle 或
  `IntakeSignals`；低熵数字消息 ID 不能在没有进程密钥时通过枚举摘要反查；
- 容量和 TTL 必须显式配置，容量淘汰和过期都增加索引丢弃计数；进程重启后密钥和索引一起丢失；
- lookup 同时绑定适配器、Bot 和群 scope，跨群、跨 Bot 或跨适配器不会误命中；
- 没有结构化 Reply ID，或引用未知 / 过期时返回 `NOT_FOUND`，只表示运行 correlation 不可用；显式
  `triage` 仍按 scope Thread 规则处理；
- 同一 scope 的并发 `triage` 返回 `BUSY`；它不会排队、覆盖或取消已经运行的处理轮；
- 运行 bundle 有明确失败观察时入口信号为 `failed`；只有成功生命周期时保持 `not_observed`，因为框架完成
  不等于用户观察到的业务行为正确；
- 后续跨平台实现已注册 Alconna 用户入口并冻结首轮命令、场景、限流、窄回显与部署默认值；本文不作为
  当前跨平台支持范围说明。

## 相关决定

- [ADR-0001：QQ 群显式报障与本机运行证据](../../adr/0001-qq-group-report-linked-runtime-evidence.md)
- [ADR-0004：OneBot V11 与带密钥消息引用索引](../../adr/0004-onebot-v11-first-and-keyed-message-reference-index.md)
- [ADR-0006：跨平台 Alconna 入口与引用 Provider](../../adr/0006-cross-platform-alconna-entry-and-reference-providers.md)
- [ADR-0030：免命令精确回复续问（已替代）](../../adr/0030-continue-support-thread-by-exact-reply.md)
- [ADR-0031：支持 Thread 续问仍要求显式 triage](../../adr/0031-require-triage-for-support-thread-continuation.md)
- [ADR-0033：用一次性 Reply Claim 串行化支持 Thread 处理轮](../../adr/0033-serialize-support-thread-turns-with-single-use-reply-claims.md)
- [ADR-0035：用经校验的 UniSeg Receipt 结算 Thread 出站引用](../../adr/0035-settle-support-thread-replies-from-uniseg-receipts.md)
- [ADR-0060：用作用域 Thread 承接一次补充并在路由后投影会话上下文](../../adr/0060-use-scope-thread-and-post-route-conversation-context.md)
