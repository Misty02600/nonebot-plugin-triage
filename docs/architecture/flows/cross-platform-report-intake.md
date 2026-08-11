# 跨平台 triage 支持入口

## 当前可运行流程

```text
任意 UniSeg 支持的消息事件
    ├─ NoneBot event pre-hook → correlation ID → Matcher / API 最小运行观察
    └─ UniSeg target + message ID → HMAC 引用索引

[可选 @Bot] triage <自然语言> [可选 Reply]
    │
    └─ on_alconna + MultiVar(str, "*")
         ├─ MsgTarget → 入口 HMAC 限流 → 拒绝私聊
         ├─ OriginalUniMsg → 只取第一个 Reply.id
         └─ 当前确定性首轮意图边界
              ├─ 功能 / 用法 → 公开能力说明或澄清 → UniMessage
              ├─ 不确定     → 单次澄清 → UniMessage
              └─ 疑似故障
                   ├─ Reply 命中 → capture 最小运行证据
                   └─ 无 Reply / 未命中 → 空证据，不猜测消息
                                      ↓
                              LiveIncident + 窄回执
                                  ├─ 明确失败 → 活动 cluster
                                  └─ observe trial → 本地轮转 JSONL
```

`@Bot` 由 NoneBot / 适配器预处理，入口本身不要求 `to_me()`。`triage` 指令始终必选，所以插件不会把普通
群聊或发给其他插件的消息交给意图层。

被回复消息如果是入站事件，通用引用桥已经登记其引用。Bot 主动输出则需要适配器出站 Provider 回填消息
引用；当前只实现 OneBot V11 群发送 Provider。其他适配器引用失败时仍处理求助，只明确说明没有关联证据。

## 支持矩阵

| 能力 | OneBot V11 | QQ 官方及其他 UniSeg 适配器 |
|---|---|---|
| `triage <自由文本>`，无 `@Bot` | 已做 Matcher 与服务测试 | 入口无专属类型；尚未逐平台端到端测试 |
| `@Bot triage <自由文本>` | 依赖 NoneBot 标准 `to_me` 预处理 | 依赖对应适配器标准预处理 |
| Reply / Target | 已用真实事件模型测试 | 取决于对应 exporter 与平台事件 |
| 回复入站消息并关联 | 支持 | exporter 可提供 target 与 message ID 时支持 |
| 回复 Bot 输出并关联 | 当前支持群发送 | 尚未实现出站 Provider |
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
| 通用入站引用与 Target scope | `src/nonebot_plugin_triage/universal_references.py` |
| OneBot V11 出站 Provider | `src/nonebot_plugin_triage/onebot_v11_references.py` |
| HMAC 引用索引 | `src/nbtriage/message_references.py` |
| 故障组合与窄回显 | `src/nonebot_plugin_triage/live_reports.py` |
| incident、cluster 与 trial | `src/nbtriage/live_incidents.py`、`src/nbtriage/live_trials.py` |

## 相关决定

- [ADR-0006：跨平台 Alconna 入口与引用 Provider](../../adr/0006-cross-platform-alconna-entry-and-reference-providers.md)
- [ADR-0014：观察型生产 trial](../../adr/0014-use-observation-first-production-trials.md)
- [ADR-0020：triage 自然语言入口与可选 Reply](../../adr/0020-use-triage-command-for-natural-language-support.md)
