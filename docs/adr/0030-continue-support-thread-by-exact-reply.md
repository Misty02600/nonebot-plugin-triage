# ADR-0030：允许精确回复 Triage 回答续接短期支持 Thread

| 状态 | 决策日期 |
|---|---|
| 已采纳 | 2026-08-12 |

## 当时遇到了什么

ADR-0020 用必选 `triage` 避免插件抢占其他普通消息。这个边界适合作为首次入口，但要求用户每次追问都
重复指令，会割裂已经由 Triage 自己回答的主题。另一方面，捕获同一用户的下一条普通消息、为每个会话
创建 Waiter，或者仅靠模型猜测追问归属，都会在活跃群聊中误吞消息，并增加悬挂任务和限流绕过面。

NoneBot 的通用消息 Matcher 会检查每条消息；只有在 Rule 能以低成本证明“这是对 Triage 已登记回答的精确
Reply”时，这种 Matcher 才适合作为窄续问入口。不同适配器提取 Reply 的成本并不相同，因此不能在全局 Rule
里无条件构建跨平台 `OriginalUniMsg`。

## 决策

1. 首次求助仍使用 `triage <自然语言>`。新增唯一免 `triage` 的例外：同一用户在同一 adapter、Bot 和场景中，
   精确 Reply 到 Triage 短期登记的上一条回答，可以续接同一支持 Thread。
2. 插件注册一个常驻普通消息 Matcher，而不是“万能命令”或每 Thread 临时 Matcher。其单一 Rule 只调用已
   验证为无外部读取副作用的 adapter Reply Provider；无 Reply、未知引用、跨用户、跨 Bot、跨场景和过期
   引用立即返回 `False`，handler 不运行，也不阻断其他消息。
3. 第一版仅启用 OneBot V11 群聊 Provider：它读取 adapter 已解析的 `event.reply.message_id`，或在解析正文
   失败时只读原始消息中已有的结构化 Reply ID；两条路径都不调用平台 API 或读取被回复正文。没有轻量入站
   Provider 的 adapter 对免指令续问失败关闭；原有 `triage` 入口仍可使用。
4. 精确 Reply 是显式续问信号，Matcher 优先级为 `NBTRIAGE_PRIORITY - 1`，并在命中时阻断更低优先级响应器。
   NoneBot 无法阻止已经在更高或相同优先级运行的第三方 Matcher，因此不枚举、重跑或猜测全部第三方命令。
5. 每条用户消息仍是独立 Event，handler 每轮正常结束；已回答后的追问在同一 Thread 上启动新处理轮，而不是
   通过 Waiter 悬挂旧 handler。只有明确等待证据的 Agent interruption 才能恢复原 run。
6. Thread 第一版仅存单进程、有界、可丢失的结构元数据：不透明 ID、种类、状态、有限 topic refs 和时间戳。
   默认 idle 15 分钟、absolute 30 分钟、最多 4096 条；不保存用户原文、Bot 回答全文或平台身份，不跨重启。
7. 出站 message ID 与 Thread 的关系使用独立 HMAC 索引，键包含 adapter、Bot、场景、actor 与消息引用；原始
   scope 不进入内存条目。引用索引不能与运行证据 correlation 索引互相冒充。
8. 续问每轮重新经过求助入口限流；需要 SUPERUSER 证据的分支每轮重新鉴权。Thread 不设置总 run 数硬上限；
   未来模型请求、Token、费用、并发和工具额度绑定稳定 actor / scene / deployment 账本，而不是 thread ID。
9. 当前切片只支持功能教学和入口澄清的短期续接。教学或入口澄清 Thread 中，用户明确请求受理故障时可以
   沿用既有入口建立一次 `LiveIncident` 并立即关闭 Thread；这不构成 Bug 多轮补证。
   `SUPERUSER` 深度行为探索、Bug 多轮补证、跨重启 Case、`triage 继续` 跨平台兜底和模型历史摘要另行决策。
10. 澄清 Thread 只消费一次明确关联的续答；仍无法分类、空输入、超长输入或显式取消都会关闭本次澄清。
    教学 Thread 可在 TTL 内继续相关问答，但每次成功发送新回答后，只保留该 Thread 最近一次回答的引用，
    避免回复旧答案时套用已经更新的主题上下文。

## 影响

- 用户可自然回复 Triage 的回答继续提问，不需要每轮看到固定的“还可以继续”尾注；没有追问时 Thread 静默过期。
- 常驻 Matcher 在框架层会看到 message event，但普通消息只执行 adapter 类型与 Reply 存在性检查，不构建
  `OriginalUniMsg`、不检索能力、不调用模型或第三方代码。
- 精确 Reply 表达续问意图；若用户要执行其他插件命令，应不要回复 Triage 的这条回答。
- 进程重启或 Thread 过期后，旧 Reply 不会按时间或语义猜测恢复，用户需要重新发送完整 `triage` 请求。

## 替代关系

- 部分替代 [ADR-0020](0020-use-triage-command-for-natural-language-support.md) 的“每条支持消息都必须出现
  `triage`”触发细节；首次入口、自然语言不可信、Reply 作为故障证据以及只让疑似故障建单的边界不变。
- 保留 [ADR-0006](0006-cross-platform-alconna-entry-and-reference-providers.md) 的 Provider 分层：入口覆盖、
  出站引用覆盖与免指令续问覆盖必须分别声明。
- 服从 [ADR-0023](0023-defer-orm-until-durable-business-state.md) 的短期内存策略；第一版不引入 ORM 或持久会话。

## 落实与确认

- `src/nbtriage/support_threads.py` 提供不保存正文或平台身份的有界 Thread store，以及 latest-only 的 HMAC
  出站引用索引；`src/nonebot_plugin_triage/thread_references.py` 负责适配层引用解析与失败关闭。
- `src/nonebot_plugin_triage/handlers.py` 注册优先级为首次入口减一的常驻 `on_message` Matcher。Rule 只解析
  已知引用并读取 Thread；命中后 handler 才重新限流、分类和回答，且每轮都正常结束。
- `src/nonebot_plugin_triage/onebot_v11_references.py` 已实现 OneBot V11 群聊的轻量入站 Reply Provider，并在
  Triage 成功发送回答后登记出站 message ID。其他 adapter 尚未加入免指令续问支持矩阵。
- 单元与集成测试覆盖 TTL、容量、HMAC scope、旧回答失效、普通消息不触发、跨 actor / 未知 Reply
  失败关闭、教学续问和单次澄清终止。当前实现不使用 Waiter、不保存聊天历史，也未启用模型 Agent。

## 相关文档

- [triage 自然语言支持入口](../architecture/flows/support-intake-routing.md)
- [OneBot V11 引用关联](../architecture/flows/onebot-v11-reply-reference-correlation.md)
