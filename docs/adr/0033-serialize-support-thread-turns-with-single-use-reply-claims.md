# ADR-0033：用一次性 Reply Claim 串行化支持 Thread 处理轮

| 状态 | 决策日期 |
|---|---|
| 已采纳 | 2026-08-13 |

## 当时遇到了什么

ADR-0031 已经要求每轮显式发送 `triage`，并用精确 Reply 选择短期 Thread；但原实现先查询
Reply 引用、再读取 Thread，随后独立完成检索、更新上下文、发送回答和登记下一条消息引用。单个索引方法
虽然有锁，整个处理轮却不是一个事务。

这留下两个确定问题：同一回答可能被两个并发请求同时解析为可续接，两个处理轮会并行更新同一个 Thread；
发送失败时，旧引用仍可能继续恢复一个已经产生不确定状态的 Thread。入口的两秒冷却只能限制频率，不能代替
处理中的排他所有权。

## 决策

1. 显式 `triage` 带精确 Reply 时，入口在业务处理前执行一次原子 Turn Claim。Claim 同时校验 HMAC
   作用域、Thread 状态、TTL 和 latest-only 引用，并在成功时立即消费旧 Reply 引用。
2. 每个 Thread 同时最多存在一个 active turn lease。相同 Reply 或同一 Thread 的并发 Claim 返回
   `BUSY`，不创建新 Thread、不进入检索、模型或报障分支；用户应等待当前处理轮完成。
3. 未知、过期、旧回答和跨 adapter、Bot、场景或 actor 的 Reply 仍返回 `NOT_FOUND`，按 ADR-0031
   作为新的显式 `triage` 请求处理。已被当前 active lease 消费的精确引用，以及仍能解析到同一 active
   Thread 的其他有效引用，都返回 `BUSY`。Claim 内部异常返回独立 `ERROR` 并失败关闭，不能降级成
   `NOT_FOUND` 后继续创建新 Thread 或进入业务分支。
4. Lease 使用不透明随机 token；进程内只保存 token、Reply 和作用域的带密钥摘要，不保存原始平台身份、
   消息 ID 或聊天正文。错误 token 不能完成或终止其他 Thread 的处理轮。
5. Handler 取得 lease 后只计算下一份结构化上下文，不提前写回 Thread。只有适配器确认新回答发送成功并取得
   合法 message ID 后，协调器才在同一临界区提交新上下文、绑定新的 latest Reply，并释放 lease。
6. 明确取消、澄清耗尽或转为故障受理时，入口消费 lease 并关闭 Thread，终局回复不再成为续接点。
   Handler 异常、发送失败、发送结果缺少 message ID、绑定失败、未支持的发送路径或 lease 到期也都失败关闭；
   已消费的旧 Reply 不恢复。
7. 当前 active lease 的默认有效期是 120 秒。它只保护一次处理轮，不延长 Thread 的 idle 或 absolute TTL；
   完成、终止、失败和 Matcher run postprocessor 都必须幂等释放或关闭。
8. 该协调仍是单进程、有界、可丢失的运行时状态，不提供跨进程锁或重启恢复。若未来引入多 worker 或持久
   Agent run，必须按 ADR-0023 重新评审共享事务与 lease 存储，不能把当前内存锁描述为分布式保证。

## 影响

- 回复一旦成功 Claim，就不能重复使用；即使本轮发送失败，用户也要重新发送完整 `triage`，而不是依赖旧
  Reply 恢复可能不一致的上下文。
- 同一 Thread 的第二个并发续问会得到“上一轮仍在处理”的窄提示。它不会排队，也不会取消或覆盖第一轮。
- 每轮入口限流仍在 Claim 前执行；限流负责防刷，Turn Claim 负责同一 Thread 的并发正确性，两者不能互换。
- 首轮 Thread 从创建到发送回执结算受同期限的 pending reservation 保护，避免处理中被 idle TTL 或容量
  淘汰；absolute TTL 仍是不可延长的硬上限。首轮回答仍在成功发送后才建立第一条引用，一次性消费只影响
  已经存在的续接点。

## 替代关系

- 部分替代 [ADR-0031](0031-require-triage-for-support-thread-continuation.md) 中“成功发送新回答后旧引用才失效”
  的提交时机；保留显式 `triage`、Reply 选择 Thread、HMAC 作用域、每轮限流与不使用 Waiter 的决定。
- 细化 [ADR-0030](0030-continue-support-thread-by-exact-reply.md) 保留下来的 latest-only Thread 生命周期；
  不恢复其已经被 ADR-0031 替代的免命令入口。
- 服从 [ADR-0023](0023-defer-orm-until-durable-business-state.md) 的短期内存策略。

## 落实与确认

- `src/nbtriage/support_threads.py` 的 `SupportThreadTurnCoordinator` 负责一次性 Claim、单 Thread lease、
  原子 complete 与失败关闭；领域测试覆盖作用域隔离、TTL、错误 token 和并发争用。
- `src/nonebot_plugin_triage/thread_references.py` 把 Target 转为协调器 scope，并用 Matcher state 携带首轮绑定或
  续问 lease；run postprocessor 兜底关闭未提交的处理轮。
- `src/nonebot_plugin_triage/onebot_v11_references.py` 只在成功群发送取得 message ID 后提交新的续接点；API
  异常、畸形结果和未消费 state 都失败关闭。
- 集成测试应覆盖 `BUSY` 不进入业务分支、成功发送后只能回复新回答、发送或绑定失败不复活旧引用，以及
  OneBot Reply 预处理失败分支仍可由显式 Alconna `triage` 命中；当前落实证据以本 ADR 提交时的测试结果
  为准。

## 相关文档

- [triage 自然语言支持入口](../architecture/flows/support-intake-routing.md)
- [跨平台 triage 支持入口](../architecture/flows/cross-platform-report-intake.md)
- [OneBot V11 引用关联](../architecture/flows/onebot-v11-reply-reference-correlation.md)
