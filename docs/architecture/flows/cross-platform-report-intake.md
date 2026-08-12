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
         └─ 当前确定性首轮意图边界
              ├─ 功能 / 用法
              │    ├─ 显式 public 命中 → 公开能力说明 → UniMessage
              │    ├─ 当前 adapter 的 public 影子 → 已过滤能力说明 → UniMessage
              │    ├─ SUPERUSER + 已就绪影子 → 带披露标签的候选 → UniMessage
              │    └─ 其余 → 公开能力 fallback 或澄清 → UniMessage
              ├─ 不确定     → 单次澄清 → UniMessage
              └─ 疑似故障
                   ├─ Reply 命中 → capture 最小运行证据
                   └─ 无 Reply / 未命中 → 空证据，不猜测消息
                                      ↓
                              LiveIncident + 窄回执
                                  ├─ 明确失败 → 活动 cluster
                                  └─ observe trial → 本地轮转 JSONL
```

OneBot V11 群聊另有一个窄续问入口：精确 Reply 到 Triage 已登记且未过期的回答时，可以省略 `triage`。
它由独立 HMAC Thread 引用索引和轻量 adapter Provider 解析，作为同一 Thread 的新一轮处理；不读取被回复正文。

`@Bot` 由 NoneBot / 适配器预处理，首次入口本身不要求 `to_me()`。除上述已知 Triage 回答的精确 Reply
外，`triage` 指令始终必选，所以插件不会把普通群聊或未知 Reply 交给意图层。

被回复消息如果是入站事件，通用引用桥已经登记其引用。Bot 主动输出则需要适配器出站 Provider 回填消息
引用；当前只实现 OneBot V11 群发送 Provider。其他适配器引用失败时仍处理求助，只明确说明没有关联证据。

## 已采纳目标与当前差距

ADR-0028 已经部分替代分类前统一拒绝私聊的入口边界：当前实现允许私聊进入与群聊、频道相同的
本地守门和意图分类。公开教学、用法纠错与澄清可以原路回复；行为探索只有在当前 Bot / Event 的请求者
通过模型外 `SUPERUSER` 鉴权后，才能检索 restricted 证据并在原始提问会话返回完整解释。系统不检查会话
其他参与者，也不要求房间 allowlist 或强制转私聊，但仍执行秘密过滤、文本净化和模型外发授权。

这项决定没有开放私聊报障：请求被分到 `suspected_incident` 后，`LiveReportService` 仍按当前合同拒绝私聊。
因此实现时应把入口的全局私聊守门下移到具体分支，而不是删除报障服务自己的场景检查。

## 支持矩阵

| 能力 | OneBot V11 | QQ 官方及其他 UniSeg 适配器 |
|---|---|---|
| `triage <自由文本>`，无 `@Bot` | 已做 Matcher 与服务测试 | 入口无专属类型；尚未逐平台端到端测试 |
| 私聊 `triage <自由文本>` | 已允许进入统一分流；私聊 incident 仍拒绝 | 合同相同；尚未逐平台端到端测试 |
| `@Bot triage <自由文本>` | 依赖 NoneBot 标准 `to_me` 预处理 | 依赖对应适配器标准预处理 |
| Reply / Target | 已用真实事件模型测试 | 取决于对应 exporter 与平台事件 |
| 回复入站消息并关联 | 支持 | exporter 可提供 target 与 message ID 时支持 |
| 回复 Bot 输出并关联 | 当前支持群发送 | 尚未实现出站 Provider |
| 精确回复 Triage 回答后免指令续问 | 当前支持群聊；每轮重新限流 | 未提供轻量入站 Reply Provider，失败关闭 |
| 公开结果发送 | `UniMessage` 支持 | 由对应 exporter 转换 |

## 数据边界

- 当前请求文字只用于本次意图判断和回答，不写入 `LiveIncident`、trial 或运行证据；
- Reply 只读结构化 `id`，不读取或保存 `msg` / `origin`；
- adapter、Bot、Target、actor 和 message 标识只瞬时参与 HMAC；
- 失败聚类只使用白名单化的 lifecycle / subject / exception / stack module 标识；
- trial 默认关闭；能力问答和澄清不进入 trial；
- 所有求助有轻量入口限流；疑似故障另有限制建单频率的独立限流；
- 当前链路不运行用户文字中的命令，不创建 Issue，不修改配置，也不重启 Bot。

## 代码映射

| 边界 | 实现 |
|---|---|
| `triage` Matcher、自然语言首轮分流与公开能力 | `src/nonebot_plugin_triage/handlers.py`、`src/nonebot_plugin_triage/support_intake.py` |
| SUPERUSER 鉴权后的影子候选检索 | `src/nonebot_plugin_triage/capability_shadow.py` |
| 通用入站引用与 Target scope | `src/nonebot_plugin_triage/universal_references.py` |
| OneBot V11 出站与轻量入站 Reply Provider | `src/nonebot_plugin_triage/onebot_v11_references.py` |
| Thread 状态与精确回复引用 | `src/nbtriage/support_threads.py`、`src/nonebot_plugin_triage/thread_references.py` |
| HMAC 引用索引 | `src/nbtriage/message_references.py` |
| 故障组合与窄回显 | `src/nonebot_plugin_triage/live_reports.py` |
| incident、cluster 与 trial | `src/nbtriage/live_incidents.py`、`src/nbtriage/live_trials.py` |

## 相关决定

- [ADR-0006：跨平台 Alconna 入口与引用 Provider](../../adr/0006-cross-platform-alconna-entry-and-reference-providers.md)
- [ADR-0014：观察型生产 trial](../../adr/0014-use-observation-first-production-trials.md)
- [ADR-0020：triage 自然语言入口与可选 Reply](../../adr/0020-use-triage-command-for-natural-language-support.md)
- [ADR-0022：只向 SUPERUSER 接入能力影子候选检索](../../adr/0022-limit-capability-shadow-guidance-to-superusers.md)
- [ADR-0028：允许 triage 私聊并向 SUPERUSER 原会话返回行为解释](../../adr/0028-allow-private-triage-and-superuser-request-context-replies.md)
- [ADR-0030：精确回复续接短期支持 Thread](../../adr/0030-continue-support-thread-by-exact-reply.md)
