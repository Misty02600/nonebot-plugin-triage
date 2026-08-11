# ADR-0004：首个 QQ 接入采用 OneBot V11 与带密钥消息引用索引

| 状态 | 决策日期 |
|---|---|
| 已采纳；第 4–5 条由 [ADR-0006](0006-cross-platform-alconna-entry-and-reference-providers.md) 部分替代 | 2026-08-09 |

## 背景

NoneBot 只读运行观察桥已能用本地 `correlation_id` 关联 event、实际 Matcher 和其内部平台 API，但 QQ
用户回复报障时携带的是平台消息引用。若直接把 QQ `message_id` 写进运行观察、SupportCase 或模型输入，
会破坏 ADR-0001 的传输无关与最小身份边界；若只按时间猜测，又会在并发群聊中产生错误责任链。

NoneBot OneBot V11 适配器的 `MessageEvent` 提供 `message_id` 与结构化 `reply`，发送消息 API 返回包含
消息 ID 的结果；NapCat 官方文档也提供与 NoneBot OneBot V11 反向 WebSocket 的直接连接方式。QQ 官方
适配器仍是后续重要目标，但首轮 dogfood 需要先形成一条可重复的群消息引用链。

## 决策

1. 首个 QQ 边缘适配器选择 OneBot V11；NapCat 作为首轮本机 dogfood 的协议实现，不将其特有扩展进入核心；
2. 新增单进程 `PlatformMessageReferenceIndex`，用每次进程启动生成或调用方注入的至少 32 字节密钥，对
   “适配器 + Bot scope + 平台消息引用”计算 HMAC-SHA256；索引只保存摘要、correlation ID 和存入时间；
3. 索引容量与 TTL 必须显式给出，没有生产默认值；重启后索引与进程密钥一起丢失，首版不做持久化；
4. OneBot 适配器在事件后处理绑定入站 `message_id`，在成功的发送 API 后处理绑定出站 `message_id`；
   只瞬时读取消息 ID 与必要的 Bot / 群路由 scope，不复制消息、其他发送参数或完整 API result；
5. 显式报障入口只解析结构化 `event.reply.message_id` 并查索引。命中后向领域层提交 correlation ID 和
   运行证据摘要；未命中时只说明引用过期或不属于当前 Bot，不扩大读取聊天记录；
6. 当前切片不冻结报障命令文本，不实现自然语言意图模型，也不自动创建 Issue 或执行修复。

## 选择理由

- HMAC 索引既能精确关联并发消息，又不把可反查的平台消息 ID留在领域工件、日志或模型边界；
- 入站事件后处理发生在事件预处理完成之后，可以读取运行观察桥已写入 state 的关联 ID；
- 出站发送发生在 Matcher 上下文内，可以沿同一 state 关联 Bot 的实际回复；
- OneBot V11 与 NapCat 当前都有直接文档和稳定的引用字段，适合先完成维护者自己的 Bot dogfood；
- 后续 QQ 官方适配器只需实现引用提取和发送结果提取，不需要修改 `RuntimeObservation` 或入口分流契约。

## 代价与限制

- Bot 重启后不能解析重启前消息的回复引用；这是首版隐私与实现复杂度的主动取舍；
- 后台任务在 Matcher 上下文外发送的消息当前不会绑定到事件，不能据此猜测 correlation ID；
- HMAC 隐藏原始引用但不提供跨进程共享，多个 Worker 需要后续单独设计一致性与密钥管理；
- OneBot V11 首发不代表 QQ 官方适配器优先级降低，也不把 NapCat 行为当作所有 OneBot 实现的保证；
- 群内告知文本、维护者权限、限流和部署侧容量 / TTL 仍需在真正注册报障 Matcher 前冻结。

## 参考

- [NoneBot OneBot V11 事件模型](https://onebot.adapters.nonebot.dev/docs/api/v11/event/)
- [NoneBot OneBot V11 Bot API](https://onebot.adapters.nonebot.dev/docs/api/v11/bot/)
- [NapCat 接入 NoneBot](https://napneko.github.io/use/integration)
- [ADR-0001：QQ 群显式报障与本机运行证据](0001-qq-group-report-linked-runtime-evidence.md)
