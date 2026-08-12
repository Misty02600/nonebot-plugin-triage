# ADR-0031：支持 Thread 续问仍要求显式 triage

| 状态 | 决策日期 |
|---|---|
| 已采纳；部分被替代 | 2026-08-12 |

## 当时遇到了什么

ADR-0030 允许用户只通过精确 Reply 续接 Triage Thread。为此插件必须注册常驻普通消息 Matcher，并且只在
已经证明 Reply 提取不会执行外部读取的适配器上开放。这使同一种支持交互出现了两套入口：首次请求使用
Alconna `triage`，续问则绕过命令解析并由适配器专用 Rule 接管普通消息。它还引入 Matcher 优先级竞争、
普通 Reply 与第三方命令冲突，以及不同适配器具有不同触发语义等额外边界。

本项目已经选择显式支持入口来避免监听普通聊天。续问重复一次 `triage` 的交互成本可以接受，不值得用常驻
普通消息 Matcher 和适配器专用入站入口换取省略命令头。

Alconna / UniSeg 已经提供跨适配器的命令、`OriginalUniMsg`、结构化 `Reply`、`MsgTarget` 和 `UniMessage`
接口，但这些接口只负责表示当前消息和引用。它们不知道该引用是否由 Triage 发出、属于哪个 Thread、是否
仍然有效，也不能代替本地状态和权限判断。

## 决策

1. 首次请求和后续支持轮次都必须使用 `triage <自然语言>`，`@Bot` 仍可选。只有 Reply、没有 `triage` 的
   消息不是 Triage 入口，不创建或恢复 Thread，也不阻断其他 Matcher。
2. 支持入口继续使用 `on_alconna(Alconna(...))`，不改用 NoneBot `on_command`，也不注册捕获所有消息的
   `on_message` 续问 Matcher。Alconna 负责识别命令头、解析自由文本，并通过 UniSeg 向入口提供结构化 Reply、
   Target 和跨适配器发送能力。
   入口在 Alconna 真正解析前先用纯文本检查配置的完整命令头；已确认是求助命令后，命令解析阶段只构造不带
   Reply 的 `UniMessage`，handler 再通过 `OriginalUniMsg` 提取一次结构化 Reply。这样普通消息不会触发 Reply
   构建，显式请求也不会因为默认 message provider 与 handler 各自 `attach_reply()` 而重复读取平台。
3. 一条显式 `triage` 请求带 Reply 时，入口只读取 Alconna / UniSeg 已提供的第一个结构化 `Reply.id`，不读取
   `msg`、`origin` 或被回复正文。该 ID 同时可以作为运行证据引用；是否续接 Thread 则必须由独立 Thread
   引用索引判定。
4. Thread 索引用 HMAC 绑定 adapter、Bot、场景、actor 和消息引用，并校验 Thread 状态、TTL 与 latest-only
   回答。只有精确命中仍可续接的 Thread 时，当前请求才作为同一 Thread 的新处理轮；未知、过期、旧回答、
   跨作用域或内部错误都按普通的新 `triage` 请求处理，不按文字、时间或相邻消息猜测。
5. Alconna 的结构化 Reply API 是通用输入接口，不代表每个平台都已完成“Bot 出站回答 ID → 用户入站
   Reply ID”的精确往返。Thread 续接仍要求适配器具有经过验证的出站引用 Provider；当前只对 OneBot V11
   群发送建立该映射。其他 UniSeg 适配器仍可使用 `triage`，未命中 Thread 时按新请求处理。
6. 每条消息仍是独立 Event，handler 每轮正常结束，不使用 Waiter 悬挂旧 handler。Thread 只保存单进程、
   有界、可丢失的结构元数据；默认 idle 15 分钟、absolute 30 分钟、最多 4096 条，不保存用户原文、Bot
   回答全文或平台身份，也不跨重启。
7. 每轮显式 `triage` 都重新经过入口限流；需要 SUPERUSER 证据的分支每轮重新鉴权。Thread 不承载跨轮
   限流账本，也不恢复已经结束的 Agent run。
8. 保留 ADR-0030 已确定的 Thread 生命周期：澄清只消费一次有效续答；教学可以在 TTL 内继续相关问题；
   每次成功发送新回答后只保留最近一次回答的引用；从教学或澄清转为明确故障受理时只建立一次
   `LiveIncident` 并关闭 Thread。

## 影响

- 所有支持轮次具有同一个显式入口，不再为了省略命令头扫描普通消息，也不需要额外的续问 Matcher 优先级
  或 `block` 语义。
- 用户续问时需要在回复 Triage 最近一次回答的同时写 `triage <内容>`。精确 Reply 负责选择 Thread，命令头
  负责声明本条消息要交给 Triage；二者缺一都不会隐式恢复主题。
- 回复 Triage 回答后发送其他插件命令不会被 Triage 续问入口抢占。Reply 未命中时，显式请求仍能作为新
  `triage` 正常处理，而不是因平台支持差异直接失败。
- Alconna / UniSeg 与 Thread 索引的职责保持分离：前者提供跨平台消息结构，后者拥有 Triage 特有的关联、
  作用域、有效期与状态资格。
- `NBTRIAGE_PRIORITY` 不再需要为“续问 Matcher 使用入口优先级减一”预留数值；入口只使用自身配置的
  优先级。

## 替代关系

- 替代 [ADR-0030](0030-continue-support-thread-by-exact-reply.md) 的免命令续问入口、常驻 `on_message`
  Matcher 和适配器专用入站 Reply Provider 决定；保留其 Thread 生命周期、HMAC 作用域、latest-only、
  每轮限流与不使用 Waiter 的决定。
- 恢复并细化 [ADR-0020](0020-use-triage-command-for-natural-language-support.md) 的显式 `triage` 入口：该要求
  现在同样适用于 Thread 续问，Reply 仍是结构化上下文而不是独立触发器。
- 保留 [ADR-0006](0006-cross-platform-alconna-entry-and-reference-providers.md) 的 Alconna / UniSeg 跨平台
  外壳和适配器出站 Provider 分层。
- 服从 [ADR-0023](0023-defer-orm-until-durable-business-state.md) 的短期内存策略，不引入 ORM 或持久会话。
- 后续 [ADR-0033](0033-serialize-support-thread-turns-with-single-use-reply-claims.md) 把 Reply 的失效时机
  从“新回答发送成功后”提前为“处理轮 Claim 成功时”，并增加单 Thread active lease；本 ADR 的显式
  `triage` 入口和 Reply 选择 Thread 规则保持不变。

## 落实与确认

- `src/nonebot_plugin_triage/handlers.py` 的 Alconna `triage` Matcher 是唯一支持入口；它在同一 handler 中
  解析可选 Reply、尝试恢复 Thread，再进入新请求或续问分支。轻量 before-rule 与消息 Provider 只优化
  Alconna 的解析时机，不取代 Alconna 命令 Matcher 或 UniSeg Reply API。
- `src/nonebot_plugin_triage/thread_references.py` 负责把通用 Reply 和当前 Target / actor 转为 HMAC Thread
  查询；它不承担命令匹配，也不读取 Reply 正文。
- `src/nonebot_plugin_triage/onebot_v11_references.py` 只在成功群发送后把返回的 message ID 绑定到 Thread；
  入站 Reply 由 Alconna / UniSeg 统一注入，不再注册 OneBot 专用续问入口。
- 单元与 Matcher 集成测试覆盖：无 `triage` 的任意 Reply 不触发；`triage` 加有效 Reply 续接；未知、过期、
  旧回答和跨 actor / Bot / 场景 Reply 作为新请求；每轮限流与 SUPERUSER 鉴权不被 Thread 绕过。
- Reply 的一次性消费、并发 `BUSY` 与发送失败关闭现由 ADR-0033 的 Turn Claim 协调器落实。

## 相关文档

- [triage 自然语言支持入口](../architecture/flows/support-intake-routing.md)
- [跨平台 triage 支持入口](../architecture/flows/cross-platform-report-intake.md)
- [OneBot V11 引用关联](../architecture/flows/onebot-v11-reply-reference-correlation.md)
