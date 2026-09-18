# ADR-0147：入口冷却默认 5 分钟并豁免 SUPERUSER

| 状态 | 决策日期 |
|---|---|
| 已采纳 | 2026-09-17 |

## 问题

统一 triage 入口冷却（[ADR-0045](history/0045-use-one-triage-cooldown-and-localstore-capability-cache.md)
决策 2）默认 2 秒，主要用于拦截同一用户在同一会话内的重复 `triage` 发送。2 秒窗口对真实重复滥用抑制太
弱：普通用户在一次模型往返后立刻再发一轮仍可能被放行。同时部署维护者（SUPERUSER）在调试、复测或追问时
也会被同一窗口打断，但维护者本身就是模型费用与会话管理的受信方，冷却对它的边际收益很低。

## 决策

- `NBTRIAGE_COOLDOWN_SECONDS` 默认值从 2 秒改为 300 秒（5 分钟）；配置键、取值范围和作用域不变：
  同一 `adapter + Bot + conversation + actor` 在窗口内再次进入 `triage` 或 `triage 报错查询` 会被拒绝。
- SUPERUSER 不消耗该入口冷却：`_support_request_allowed` 先做模型外 SUPERUSER 鉴权，命中直接放行，
  不写入限流账本；`triage` 主入口与 `triage 报错查询` 共用同一豁免。代价是每轮 `triage` 都会评估一次
  SUPERUSER（此前只有维护者路径才评估），普通用户仍按该值之后的限流结果放行或拒绝。
- 非 SUPERUSER 仍按原 scope 消费同一窗口；账本仍是单进程内存固定窗口，重启清空，不替代跨进程配额、
  费用预算或持久封禁。
- `triage 刷新帮助`、`triage 开始新对话` 与 `triage 停止` 本来就是 SUPERUSER-only 命令，不受此变更影响。

## 原因与影响

- 5 分钟默认对应“一次真实求助主动再次发起”的节奏：正常澄清和 Thread 续问仍由 scope Thread 的最多
  两次补充额度承接，不依赖冷却窗口；冷却只拦截同一 actor 的快速重复入口。
- SUPERUSER 豁免以 NoneBot 既有 `SUPERUSER` 鉴权为准，不引入新的身份存储；豁免逻辑集中在
  `_support_request_allowed` 单一入口，主入口与维护子命令行为一致。
- 成本：普通用户误触后需等 5 分钟才能重新发起 `triage`；部署者可按需要把
  `NBTRIAGE_COOLDOWN_SECONDS` 调回更短窗口。
- 这是配置默认值与入口鉴权顺序的变更，不是破坏性配置变更：旧值 2 的部署升级后行为变为 300，属预期
  行为变化，README 已同步说明。

## 替代关系

- 部分替代 [ADR-0045](history/0045-use-one-triage-cooldown-and-localstore-capability-cache.md)
  决策 2 的“默认 2 秒”条款；单一入口冷却、scope、内存账本和“不建第二份限流账本”的决定继续有效。

## 落实与确认

- `NBTriageConfig.nbtriage_cooldown_seconds` 默认改为 300；README 配置表更新默认值与 SUPERUSER 豁免说明。
- `handlers._support_request_allowed` 改为 async，先做 SUPERUSER 鉴权再做 HMAC 限流；`handle_support` 与
  `handle_query` 两个调用点同步改为 await。
- 回归：普通用户在窗口内第二次 `triage` 仍被拒绝；SUPERUSER 在同窗口内的第二次 `triage` 被正常处理；
  配置默认 5 分钟与环境变量覆盖由 units 测试覆盖。

## 相关文档

- [支持入口分流](../../architecture/flows/support-intake-routing.md)
- [README 配置说明](../../README.md)