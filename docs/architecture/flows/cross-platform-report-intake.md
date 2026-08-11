# 跨平台显式报障入口

## 当前可运行流程

```text
任意 UniSeg 支持的消息事件
    │
    ├─ NoneBot event pre-hook ─→ correlation ID ─→ Matcher / API 最小运行观察
    │
    └─ UniSeg get_target + get_message_id ─→ HMAC 引用索引

用户回复消息并 @Bot 报错
    │
    └─ on_alconna
         ├─ OriginalUniMsg → 只取第一个 Reply.id
         ├─ MsgTarget → 拒绝私聊，生成稳定会话 scope
         ├─ Event.get_user_id → 只瞬时参与 HMAC 限流
         └─ 引用索引命中 correlation ID
                └─ capture 最小运行证据
                     └─ 确定性 reply-report 路由
                          └─ 短期 LiveIncident
                               ├─ 明确失败 → 活动 TTL 内稳定 cluster 聚合
                               ├─ observe trial → 本地轮转 JSONL + 维护者反馈
                               └─ UniMessage 窄回执
```

被回复消息如果是用户或其他入站事件，通用入站桥已经登记其引用。如果消息是 Bot 在 Matcher 内主动发送的
输出，则需要该适配器的出站 Provider 从发送结果中回填消息引用。当前只实现并集成测试 OneBot V11 群发送
Provider；其他适配器没有 Provider 时不会按时间猜测，而是返回近期记录不可用。

## 支持矩阵语义

| 能力 | UniSeg 支持的适配器 | OneBot V11 | QQ 官方及其他适配器 |
|---|---|---|---|
| Alconna 命令匹配 | 由 Alconna exporter 支持 | 已做加载与结构映射测试 | 入口代码无专属依赖；尚未逐平台端到端测试 |
| 结构化 Reply / Target | 由 `OriginalUniMsg` / `MsgTarget` 提供 | 已用真实事件模型测试 | 取决于对应 exporter 与平台事件 |
| 回复入站消息并关联 | 通用 `get_target` / `get_message_id` | 支持 | exporter 可提供两者时支持 |
| 回复 Bot 输出并关联 | 需要适配器出站 Provider | 当前支持群发送 | 当前未实现，不宣称支持 |
| 公开结果发送 | `UniMessage` | 支持 | 由对应 exporter 转换 |

“插件入口跨平台”不等于“每个平台的 Bot 出站引用都已完成”。发布说明和后续测试必须始终区分这两层。

## 数据边界

- 不读取或保存 Reply 的 `msg` / `origin`，只读取结构化 `id`；
- Target 的事件 `source` 不进入稳定会话 scope；adapter、Bot、Target、actor 和 message 标识只瞬时参与 HMAC；
- 引用索引只保存摘要、correlation ID 和时间，`LiveIncident` 不保存平台身份或聊天正文；
- 失败聚类只使用已白名单化的 lifecycle / subject / exception / stack module 标识，不使用 observation /
  correlation ID、时间或异常消息；cluster 只统计显式报障，不代表底层异常总数；
- 私聊、无 Reply、跨 scope、过期、未知引用和内部错误都有固定窄结果；
- trial 默认关闭；observe 只记录已受理 incident 的脱敏生命周期，写入失败不改变公开回执；
- 当前链路不调用 DeepSeek，不运行 Probe，不创建 Issue，不修改配置，也不重启 Bot。

## 代码映射

| 边界 | 实现 |
|---|---|
| Alconna Matcher 与依赖注入 | `src/nonebot_plugin_triage/handlers.py` |
| 通用入站引用与稳定 Target scope | `src/nonebot_plugin_triage/universal_references.py` |
| OneBot V11 出站 Provider | `src/nonebot_plugin_triage/onebot_v11_references.py` |
| HMAC 引用索引 | `src/nbtriage/message_references.py` |
| 限流、报障组合与窄回显 | `src/nbtriage/rate_limits.py`、`src/nonebot_plugin_triage/live_reports.py` |
| 短期事件记录 | `src/nbtriage/live_incidents.py` |
| observation-first trial 与轮转日志 | `src/nbtriage/live_trials.py`、`src/nonebot_plugin_triage/trials.py` |

稳定失败签名、活动 cluster 的 TTL / 容量与查询语义见
[短期显式报障聚类](incident-clustering.md)。
生产试运行的任务成功标准、日志边界、SUPERUSER 反馈与 observe → shadow → canary 顺序见
[观察型生产 trial](observation-first-trials.md)。

## 决策与实施记录

- [ADR-0006：跨平台 Alconna 入口与引用 Provider](../../adr/0006-cross-platform-alconna-entry-and-reference-providers.md)
- [ADR-0014：观察型生产 trial](../../adr/0014-use-observation-first-production-trials.md)
