# ADR-0076：删除旧 Trial 反馈与统计聊天命令

## 状态

| 状态 | 决策日期 |
|---|---|
| 已采纳；聊天入口、元数据与当前文档已删除 | 2026-08-15 |

## 背景

`报错反馈 <incident-id> ...` 和 `报错统计` 原本服务 observation-first trial。现行 semantic v7 已不再产生
Incident action，用户 `triage` 无法进入该闭环；两个维护 Matcher 因而只面向一条当前不可达、未来若恢复也
需要新 ADR 的旧流程。

[ADR-0075](0075-register-problem-maintenance-under-triage-subcommand.md) 已将新的 Problem 查询与改判收口到
`triage 报错查询` 子命令。把旧 trial 命令一起迁为新子命令会让维护者误以为它们属于新的 Report /
Occurrence / Problem 工作流，也会继续维持没有在线数据来源的聊天 API。

## 决定

1. 删除顶层 `报错反馈` 和 `报错统计` Alconna Matcher、Handler、产品常量、PluginMetadata usage 和公开文档入口。
2. 不把这两个命令迁移为 `triage` 子命令，也不提供旧命令 alias。
3. 保留底层 trial event schema、轮转 JSONL、反馈解析 / 格式化领域函数和离线 `summarize-trials` 工具；删除
   聊天入口不等于删除已有本地 trial 工件或离线分析能力。
4. 兼容期内的旧 `报错查询 <incident-id>` 暂时保留，直到 ADR-0075 的 `triage 报错查询 <problem-id>` 在同一次
   命令迁移中接管；它不代表 feedback / stats 仍然可用。
5. 已移除的历史配置键 `NBTRIAGE_FEEDBACK_COMMAND`、`NBTRIAGE_TRIAL_STATS_COMMAND` 继续被明确拒绝，但迁移
   提示应说明对应聊天能力已经删除，不能再描述为固定命令。
6. 若未来重新启用 observation trial 的在线入口，必须重新决定反馈、统计和运维表面；本 ADR 不预留命令名。

## 理由

- 当前没有可达的 incident / trial 创建入口，保留聊天反馈和统计只会形成看似可用的死界面；
- 新 Problem 工作流已经拥有独立 verdict、review 和 lifecycle，旧 trial feedback 枚举不能复用为 Problem 改判；
- 离线汇总仍可检查已有轮转文件，无需为了少量本地工件维持 Bot 聊天命令；
- 直接删除比迁移到 `triage` 子命令更能准确表达产品范围，也避免长期兼容未使用的顶层命令。

## 带来的影响

- 发送 `报错反馈 ...` 或 `报错统计` 不再匹配 Triage；
- 旧 trial 文件不会被删除，维护者仍可显式运行离线汇总；
- 集成测试只继续覆盖兼容查询和新的 Problem 子命令，不再验证两个已删除 Matcher 的权限；
- README 与 observation-first trial 流程明确标记聊天入口已删除。

## 没有采用的方案

### 一起迁移为 triage 子命令

没有采用。它们属于当前不可达的旧 trial 生命周期，不属于新 Problem 维护模型。

### 保留隐藏 alias

没有采用。隐藏 alias 仍会维持死代码、权限面和错误的产品预期，却没有实际兼容收益。

## 与既有决定的关系

- 部分替代 [ADR-0045](0045-use-one-triage-cooldown-and-localstore-capability-cache.md) 的固定维护命令集合；
- 补充 [ADR-0075](0075-register-problem-maintenance-under-triage-subcommand.md)：Problem maintenance 进入
  `triage` 子命令，旧 trial feedback / stats 则直接删除。

## 相关文档

- [观察型生产 trial](../architecture/flows/observation-first-trials.md)
- [ADR-0075：把问题维护注册为 triage 子命令](0075-register-problem-maintenance-under-triage-subcommand.md)
