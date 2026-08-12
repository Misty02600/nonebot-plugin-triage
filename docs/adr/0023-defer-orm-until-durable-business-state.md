# ADR-0023：按状态语义分层存储，推迟业务 ORM

| 状态 | 决策日期 |
|---|---|
| 已采纳 | 2026-08-12 |

## 当时遇到了什么

插件已经同时存在短期运行关联、trial 审计事件和可重建的 SQLite FTS5 能力索引，未来还可能增加
`SupportCase`、`Approval`、`RepairPlan`、`ChangeSet` 与 `Verification` 等长期业务实体。它们的恢复、
查询、并发和隐私语义不同，不能因为插件功能较重就统一放入关系数据库，也不应由项目自行重写连接池、
Session 和迁移基础设施。

## 决策

1. 当前不引入 `nonebot-plugin-orm`，也不建设统一业务数据库；继续按状态的生命周期和所有权选择存储。
2. 运行观察、消息引用、incident、活动 trial 和限流等短期关联继续使用有界内存，不承诺跨重启恢复。
3. 显式启用的 trial 最小审计事件继续按 ADR-0018 使用轮转 JSONL，并落实到 LocalStore data 目录。
4. 能力影子继续使用可删除重建的专用 SQLite FTS5 索引，不为它增加 ORM 映射或业务迁移。
5. 当运行入口首次需要跨重启、跨 Worker、具有事务一致性或长期查询的权威业务状态时，再评审并优先复用
   `nonebot-plugin-orm` 提供的异步 Engine、Session、事务和 Alembic / CLI 生命周期。
6. 即使采用 ORM，`nbtriage` 领域核心仍只拥有领域模型、Repository / Unit of Work 协议、幂等与并发规则；
   ORM Model、Session、迁移和 NoneBot 生命周期集成留在 `nonebot_plugin_triage` 适配层。业务 schema、
   隐私、保留、删除和恢复有效性仍由本项目负责。
7. PostgreSQL、SQLite、独立服务或其他部署形态不在本 ADR 中提前确定，应根据届时的并发、共享、运维和
   脱离 NoneBot 访问需求另行决策。

## 为什么这样选

- 当前持久化对象的用途不同：短期关联不宜恢复，trial 是追加审计，能力索引则可由部署事实重建；
- 推迟 ORM 可以避免在没有权威业务状态时扩大安装依赖、迁移操作和敏感数据面；
- 真正需要关系型持久化时复用成熟基础设施，比项目自行维护 Engine、连接池、Session 和迁移系统更可靠；
- Repository 边界保持领域核心可独立测试，也避免 NoneBot 事件 / Matcher 作用域的 Session 进入领域契约。

## 没有采用的方案

- **现在把全部状态迁入 `nonebot-plugin-orm`**：混淆短期关联、审计日志和可重建索引的恢复语义，并增加
  当前没有业务收益的迁移与运维成本。
- **自行实现统一数据库基础设施**：项目仍需承担连接池、事务、迁移和多后端兼容，却没有形成差异化价值。
- **永久只用文件和进程内存**：未来权威业务状态需要跨重启恢复、并发协调或复杂查询时将不再足够，因此
  本决定只推迟 ORM，不排除后续采用。

## 带来的影响

- 当前实现不因本 ADR 增加依赖、数据库表或迁移脚本；
- ADR-0018 的 LocalStore 迁移已作为独立工作落实，没有等待或引入 ORM；
- 第一个权威业务状态进入运行面前，需要重新确认数据模型、事务边界、保留 / 删除策略、后端和兼容测试；
- 若采用 `nonebot-plugin-orm`，必须验证本项目支持的 Python、NoneBot、数据库驱动和迁移升降级组合。

## 落实与确认

- 当前分层存储已由内存缓冲、轮转 JSONL 和专用 SQLite FTS5 索引分别实现；ADR-0018 的 LocalStore 路径
  迁移已落实，trial 审计文件固定由 LocalStore 解析插件 data dir。
- 当前没有 `nonebot-plugin-orm` 运行依赖、业务 ORM Model 或迁移脚本。

## 相关文档

- [分级自治和长期业务实体](0002-tiered-autonomy-and-ownership-aware-remediation.md)
- [单发行包与双命名空间边界](0007-single-distribution-dual-namespace.md)
- [只用 LocalStore 保存 trial 审计日志](0018-use-localstore-only-for-enabled-trial-audit-log.md)
- [部署本地能力影子索引](0021-use-deployment-local-capability-shadow-index.md)
- [架构概览](../architecture/overview.md)
