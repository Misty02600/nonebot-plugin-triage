# ADR-0054：使用 LocalStore 保存已审核 Bug 问题记录

| 状态 | 决策日期 |
|---|---|
| 已被 ADR-0068、ADR-0073 替代；旧 JSON catalog 已删除 | 2026-08-14 |

## 当时遇到了什么

[ADR-0050](0050-use-a-bounded-agent-for-user-bug-assessment.md) 要求 Bug assessment 在读取日志、源码或调用
Agent 之前，先查询完全匹配、仍适用且经过人工审核的历史 verdict。现有 `LiveIncidentBuffer` 只保存短期
失败观察，没有人工 verdict、适用 revision、撤销状态或跨重启恢复，不能承担这个权威来源。

最初候选是部署者另外维护只读 YAML / JSON。项目作者确认问题记录应由 `nonebot-plugin-localstore` 保存，避免
再暴露一个外部路径，并让部署迁移、备份和删除都落在已有插件数据目录约定内。

## 决策

1. 首版使用 `nonebot-plugin-localstore` 的插件 **data** 区保存经过人工审核的 `BugProblemRecord` 和 catalog
   snapshot；它是权威业务数据，不得放入可删除重建的 cache 区。
2. `BugProblemRecord` 至少包含 schema version、稳定 record ID、`BugCaseFingerprint`、适用 subject / component、
   plugin / source / help-contract / deployment revision、verified verdict、责任候选、审核 revision、创建与审核
   时间、撤销状态和失效条件。原始日志、源码正文、Token、秘密配置和其他用户身份不进入该记录。
3. 第一阶段由维护者工具生成并审核 catalog snapshot，再以临时文件写入和原子替换发布到 LocalStore data。
   在线 Bug assessment 只读取已发布 snapshot，不在用户请求路径中修改、追加或自动审核记录。
4. 只有状态为 `verified`、verdict 为 `bug` 或 `not_bug`、没有撤销、所有适用 revision / generation 和 fingerprint
   字段完全匹配且不存在冲突的记录可以短路。`unknown`、未审核记录、模型候选、相似 cluster、字段缺失、陈旧
   revision 和损坏 catalog 都不得短路。
5. 每个 snapshot 带独立 catalog revision、内容 hash 和完整性校验。加载失败、schema 不兼容、重复主键、hash
   不符或原子更新中断时，verified source 记为 unavailable，继续后续公开初检 / Agent 或返回 `unknown`；不得
   阻断 Bot 启动，也不得复用 last-good 冒充当前 catalog。
6. `nbtriage` 领域核心只定义记录、匹配和只读 repository 协议；LocalStore 路径解析、原子文件替换、文件权限
   和 NoneBot 生命周期属于 `nonebot_plugin_triage` 适配层。领域模型不得依赖 LocalStore 或 NoneBot 类型。
7. LocalStore 只决定受管数据位置，不自动解决事务、并发、迁移或业务生命周期。若后续允许在线上报、多人审核、
   跨 Worker 写入、主动通知或复杂查询，必须重新执行 [ADR-0023](0023-defer-orm-until-durable-business-state.md)
   的 ORM / 事务评审；不能把首版单写者、在线只读 snapshot 永久扩张成文件数据库。
8. 后续上报功能可以在新的决定下把未审核问题写入同一 LocalStore data 所有权域，但本 ADR 不授权用户请求
   自动持久化、不定义 reviewer UI，也不改变“首版只判断、不上报”的范围。

## 为什么这样选

- LocalStore 已是插件的受管 data / cache 路径所有者，复用 data 区比新增部署路径更容易迁移、备份和清理；
- 首版只有维护者单写、在线只读和精确 key 查询，用版本化 snapshot 与原子替换即可满足一致性，不需要为尚未
  存在的在线审理流程提前引入 ORM；
- 把 verified catalog 与 LiveIncident、模型候选分开，可以证明“完全匹配已确认问题”才发生零日志、零源码、
  零 Agent 的确定性短路；
- 明确 LocalStore 不等于数据库，避免后续在出现并发业务写入后继续堆叠不可靠文件状态。

## 没有采用的方案

### 在仓库中提交人工问题 YAML

真实部署问题、责任候选和审核历史属于部署本地业务数据，不应跟随源码仓库发布；仓库也无法自然表达每个
部署 generation 的适用性。

### 直接复用 LiveIncidentBuffer

它是短期观察缓冲，没有人工审核、撤销、revision 适用性和跨重启合同。相似失败不等于已确认相同 Bug。

### 现在立即引入 ORM

首版没有在线写入、多人并发审理或跨 Worker 事务，只有维护者原子发布 snapshot 和在线只读查询。先冻结领域
repository 和业务 schema，等真实写入生命周期出现后再决定 ORM，风险更小。

## 带来的影响

- 需要新增传输无关的 `BugProblemRecord`、catalog snapshot、精确 matcher 与只读 repository 协议；
- 插件适配层需要用 LocalStore data API 延迟解析路径，并提供原子发布 / 加载实现；导入模块时不得创建目录；
- 测试必须覆盖精确命中零后续调用、撤销、revision 漂移、损坏 / 不兼容、重复记录、原子替换失败、LocalStore
  路径失败和 Bot 启动降级；
- 备份或删除 LocalStore data 会移除 verified history；能力索引 cache 删除则不应影响这份业务记录；
- 本决定不实现上报、在线审核、通知或持久原始日志。

## 落实与确认

- **已确认**：项目作者确认已审核问题使用 LocalStore 保存；
- **已有基础**：项目已依赖 `nonebot-plugin-localstore>=0.7.4,<0.8`，并分别使用 data 区保存 trial 审计、cache 区
  保存可重建能力影子；
- **已实现**：领域层已经实现 `BugProblemRecord`、带内容 hash 的 `BugProblemCatalog`、严格解析与精确
  fingerprint matcher；插件层延迟解析 LocalStore data 下的 `reviewed-bug-problems.json`，启动加载失败
  fail-open，在线只读，并提供维护者离线原子发布函数。损坏、不兼容、撤销和 revision 漂移均不会短路。
- **当前限制**：首版只有程序化离线发布 API 和测试，没有交互式审核 CLI、在线写入、多人并发或跨 Worker
  事务；后续引入这些生命周期前仍须重审 ADR-0023。

## 关系

- 第 1、3、6、7 项的文件型权威存储已由
  [ADR-0073](0073-use-nonebot-orm-for-authoritative-bug-workflow-state.md) 替代：在线 Report / Occurrence / Problem、人工复核与
  Decision 改由 ORM 事务保存，旧 `reviewed-bug-problems.json` 与读写 Repository 已删除；
- 第 3、8 项已由 [ADR-0068](0068-treat-qualified-agent-bug-verdicts-as-operational-decisions.md)
  替代：合格 Agent 的 `bug` 直接成为运行判断，人工通过追加式 Decision 事后确认或改判；
- 补充 [ADR-0050](0050-use-a-bounded-agent-for-user-bug-assessment.md) 的 verified verdict repository 所有权；
- 落实 [ADR-0023](0023-defer-orm-until-durable-business-state.md) 要求的首次权威业务状态评审，并把首版限定为
  单写者、在线只读的 LocalStore snapshot；
- 遵循 [ADR-0018](0018-use-localstore-only-for-enabled-trial-audit-log.md) 的 data 目录所有权，但不复用 trial JSONL；
- 区分 [ADR-0045](0045-use-one-triage-cooldown-and-localstore-capability-cache.md) 的可重建 cache：verified catalog
  位于 data 区，不能按 cache 失败语义随意删除重建。

## 相关文档

- [支持入口分流](../architecture/flows/support-intake-routing.md)
