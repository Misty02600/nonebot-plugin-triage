# 短期显式报障聚类

## 当前流程

```text
已分流为 suspected_incident 且关联运行证据的 triage 求助
    │
    └─ RuntimeEvidenceBundle
         ├─ 没有 failed observation ──────────────→ LiveIncident，无 cluster
         └─ 一个或多个 failed observation
              └─ 去重并排序最小失败形状
                   └─ SHA-256 → 不透明 cluster ID
                        └─ LiveIncidentBuffer 活动 TTL 聚合
                             ├─ report_count
                             ├─ first_reported_at
                             └─ last_reported_at
                                  └─ SUPERUSER 按 incident ID 查询白名单摘要
```

聚类发生在 `triage` 请求已经分流为疑似故障之后。能力说明、用法纠错和澄清不会创建 incident 或 cluster；
运行 hook 产生 observation 时也不会自动创建 incident、调用模型或发送消息；
同一个 cluster 中的每次报障仍有独立 incident ID，因此公开受理回执、限流和精确查询权限不变。

## 签名边界

一个失败形状只包含以下已经通过领域 schema 的字段：

- lifecycle kind 与 adapter 标识；
- event、plugin、Matcher、API 四类 subject 中当前 observation 允许的字段；
- exception type 与有限 stack module 标识。

签名不包含 observation ID、correlation ID、时间、消息正文、账号、会话、API 参数 / 返回值或异常消息。
同一 evidence bundle 中的失败形状先去重和稳定排序，再共同参与 SHA-256；因此 observation 提交顺序和重复
采集不会改变 cluster，不同异常形状仍保持分离。不透明哈希只表示“这些最小字段相同”，不是根因证明。

## TTL、容量与不完整性

- cluster 与 `LiveIncidentBuffer` 使用同一显式 TTL 和容量上限；
- 每次同类显式报障续期 cluster，`report_count` 统计本次活动生命周期内累计报障，因此它是 session window，
  不是逐秒滑动窗口；
- 单个旧 incident 因容量淘汰后不会恢复，但其已计入的活动 cluster 次数可以保留；cluster 本身到期或因容量
  淘汰后不提供历史恢复；
- 运行观察缓冲的 `buffer_dropped_count` 仍独立显示。cluster count 只统计已受理报障，不代表底层异常总数，
  也不能消除采集缺口。

## 代码映射

| 边界 | 实现 |
|---|---|
| 最小运行观察与失败字段 | `src/nbtriage/runtime_observations.py` |
| 稳定签名、活动 cluster 与容量 / TTL | `src/nbtriage/live_incidents.py` |
| SUPERUSER 白名单投影 | `src/nbtriage/incident_queries.py`、`src/nonebot_plugin_triage/incident_queries.py` |
| 显式报障组合 | `src/nonebot_plugin_triage/live_reports.py` |

## 相关决定与完成记录

- [ADR-0001：以显式报障关联本机运行证据](../../adr/0001-qq-group-report-linked-runtime-evidence.md)
- [跨平台显式报障入口](cross-platform-report-intake.md)
