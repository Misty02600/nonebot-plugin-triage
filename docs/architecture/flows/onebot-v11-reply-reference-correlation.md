# 流程：OneBot V11 回复引用关联运行证据与支持 Thread

> 本文保留早期 OneBot 专属运行证据切片，同时记录 ADR-0030 新增的群聊支持 Thread 引用。通用入站绑定与
> 首次用户入口已经由[跨平台显式报障入口](cross-platform-report-intake.md)承接；OneBot 代码现在负责群聊
> 出站引用 Provider，以及不触发外部读取的入站 Reply ID Provider。

## 目标

让 QQ 群用户后续回复一条已观察消息时，系统能精确恢复该消息对应的本地 `correlation_id`，同时不把
OneBot 消息正文、Bot / 群 / 用户 ID、原始 `message_id` 或完整 API data / result 写入领域证据或模型输入。

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

支持 Thread 使用另一份索引，不能与运行证据关联互相冒充：

```text
Triage guidance / clarification Matcher state
    └─ thread_id + actor scope
               ↓ successful OneBot group send
HMAC(adapter, bot, group, actor, returned message_id) → thread_id
               ↑
later OneBot group Reply
    └─ event.reply.message_id
       or original_message structured reply segment id
               ↓ exact lookup + active Thread check
      continuation Matcher Rule → new turn in the same Thread
```

运行证据的入站绑定在事件后处理执行，此时事件预处理写入的 correlation ID 已可从 state 读取。出站绑定只覆盖
Matcher 上下文内成功的 `send_group_msg` / 群 `send_msg`：适配器读取 `group_id`、`message_type` 和结果中的
`message_id` 作为瞬时路由材料，不读取或保存 `message` 与其他结果字段。Matcher 外后台发送不猜测归属。

Thread 的入站 Provider 优先读取 adapter 已解析的 `event.reply.message_id`；若查询被回复正文失败但
`original_message` 仍有结构化 reply segment，则只读取其中的 `id` 作为后备。它不会调用 `get_msg`，也不会读取
被回复正文。每个 Thread 只保留最近一次成功发送回答的引用；新引用绑定后，旧回答立即不再恢复该 Thread。

## 隐私与失败语义

- 索引密钥至少 32 字节；原始 scope 和消息引用只参与 HMAC-SHA256，不进入索引 entry、运行 bundle 或
  `IntakeSignals`；低熵数字消息 ID 不能在没有进程密钥时通过枚举摘要反查；
- 容量和 TTL 必须显式配置，容量淘汰和过期都增加索引丢弃计数；进程重启后密钥和索引一起丢失；
- lookup 同时绑定适配器、Bot 和群 scope，跨群、跨 Bot 或跨适配器不会误命中；
- 没有 `event.reply` 且原始消息也没有结构化 reply ID、引用未知 / 过期或桥内部错误时返回未命中，不扩大读取
  历史聊天；
- 运行 bundle 有明确失败观察时入口信号为 `failed`；只有成功生命周期时保持 `not_observed`，因为框架完成
  不等于用户观察到的业务行为正确；
- 后续跨平台实现已注册 Alconna 用户入口并冻结首轮命令、场景、限流、窄回显与部署默认值；本文不作为
  当前跨平台支持范围说明。

## 相关决定

- [ADR-0001：QQ 群显式报障与本机运行证据](../../adr/0001-qq-group-report-linked-runtime-evidence.md)
- [ADR-0004：OneBot V11 与带密钥消息引用索引](../../adr/0004-onebot-v11-first-and-keyed-message-reference-index.md)
- [ADR-0006：跨平台 Alconna 入口与引用 Provider](../../adr/0006-cross-platform-alconna-entry-and-reference-providers.md)
- [ADR-0030：精确回复续接短期支持 Thread](../../adr/0030-continue-support-thread-by-exact-reply.md)
