# ADR-0073：使用 NoneBot ORM 保存权威 Bug 工作流状态

## 状态

| 状态 | 决策日期 |
|---|---|
| 已采纳；首个四表 ORM 纵切、Alembic 迁移与运行接线已实现，merge / alias 暂缓 | 2026-08-15 |

## 背景

[ADR-0023](0023-defer-orm-until-durable-business-state.md) 曾明确推迟 ORM，要求等到运行入口真正需要跨重启、
跨 Worker、事务一致性或长期查询的权威业务状态时再重新评审。[ADR-0054](0054-store-reviewed-bug-problems-in-localstore.md)
因此先采用维护者单写、在线只读的 LocalStore JSON snapshot。

现在边界已经改变：[ADR-0068](0068-treat-qualified-agent-bug-verdicts-as-operational-decisions.md) 允许合格 Agent
的正式 Bug 和完成调查的深度 `unknown` 在线持久化；[ADR-0070](0070-separate-bug-reports-occurrences-and-problems.md)
要求分离 Report、Occurrence 与 Problem；[ADR-0071](0071-group-bug-problems-with-versioned-evidence-fingerprints.md)
和 [ADR-0072](0072-use-opaque-problem-ids-and-minimal-maintainer-lifecycle.md) 又要求自动聚合、公开 ID alias、人工改判、
解决与回归。一次终局只有在这些相关记录全部成功后才能向用户承诺“已记录”或“已关联”。

现有 `LocalConfirmedBugProblemRepository` 原型通过模块级 `RLock` 读取并整文件重写
`runtime-confirmed-bug-problems.json`。它只提供单进程单写者语义，不能用数据库唯一约束处理并发建档，也不能
可靠地把 Report、Occurrence、Problem 与 alias 作为一个事务提交。继续为它添加跨文件日志、锁、索引、迁移
和恢复规则，等同于在项目内重新实现一套数据库基础设施。

## 决定

### 存储所有权

1. 权威 Bug 工作流状态改用 `nonebot-plugin-orm` 管理。Triage 将
   `nonebot-plugin-orm[sqlite]>=0.8,<0.9` 声明为直接运行依赖；不得依赖宿主项目恰好通过其他插件传递安装 ORM。
2. 首个支持后端为 SQLite。未显式配置 SQLAlchemy URL 时，沿用 `nonebot-plugin-orm` 由 LocalStore 管理的默认
   `db.sqlite3`；不增加 `NBTRIAGE_*_DATABASE_PATH`、数据库启用开关或 Triage 自有连接池配置。
3. ORM 只拥有不可重建且需要事务的 Bug 业务状态，包括：
   - Report、Occurrence、Problem 及其关系；
   - 中性公开 ID、canonical Problem 与历史 alias；
   - Agent verdict、人工 review / override 和 Problem lifecycle 所需的持久字段。
4. 活动 Thread、限流、运行观察缓冲继续留在有界内存；trial 审计继续使用轮转 JSONL；能力影子继续使用可重建
   cache SQLite。不得借本决定把生命周期不同的状态统一迁入 ORM。

### 领域与事务边界

5. `nbtriage` 领域层继续定义 Report / Occurrence / Problem、ProblemSignature、Repository 与 Unit of Work 协议，
   不依赖 SQLAlchemy、NoneBot 或 ORM Model。`nonebot_plugin_triage` 适配层拥有 ORM Model、Session、查询、迁移和
   NoneBot 生命周期接线。
6. 一次可持久化终局必须在一个数据库事务中完成：
   - 幂等创建或复用 Report；
   - 幂等创建或复用 Occurrence；
   - 新建 Problem 或按版本化 Evidence 指纹关联既有 Problem；
   - 创建公开 ID / alias，并写入 verdict、review 与 lifecycle 变化。
7. 并发请求通过数据库唯一约束、事务和冲突后的模型外重查收敛。不能先生成两个 Problem，再依赖 LLM 或后台
   清理进行最终去重。`report_count` 与 `occurrence_count` 继续从关系记录派生，不恢复为可任意递增的字段。
8. 只有事务提交成功后才能发送 ADR-0072 的“已记录 / 已关联”固定回执。事务回滚、连接失败或约束无法安全
   收敛时使用固定写入失败回执；不得返回尚未持久化的公开 ID。

### 迁移与部署

9. Triage 随发行包提供 `nonebot-plugin-orm` / Alembic 迁移。部署者安装或更新含 schema 变化的版本后执行
   `nb orm upgrade`，可再用 `nb orm check` 验证；不通过关闭启动检查或在业务请求中临时建表来隐藏迁移缺失。
10. 当前 nonemigut 已锁定 `nonebot-plugin-orm==0.8.3`、SQLite extra 与 `aiosqlite`，但 Triage 仍需自己的直接
    依赖声明。当前部署没有 `runtime-confirmed-bug-problems.json` 或 `reviewed-bug-problems.json` 实际数据，因此
    首个实现不需要迁移线上记录。
11. 两个 JSON repository 在 ORM 纵切接通前只视为未接线原型。若实施前发现真实 JSON 数据，必须提供显式、
    可重复且有冲突报告的一次性导入；不能在每次启动时静默合并，也不能让 JSON 与 ORM 同时成为权威写源。
12. SQLite 是当前单机 Bot 的默认后端，不被描述成无限扩展的多机数据库。若未来多个主机共享写入或写并发超出
    SQLite 适用范围，保留相同领域 Repository / Unit of Work，改用 ORM 支持的 PostgreSQL 等后端并单独验证。

## 理由

- `nonebot-plugin-orm 0.8.3` 已提供异步 Session、事务、模型注册、数据库绑定和 Alembic / CLI 生命周期，复用它
  比项目自行管理锁、连接、schema 版本和迁移更符合仓库的依赖复用规则；
- Report、Occurrence、Problem 和 alias 是一个业务事务，不是四份可以最终一致的缓存文件；
- SQLite 对当前单机 Bot 足够轻量，默认路径已经由 LocalStore 管理，部署者无需理解 Triage 内部文件布局；
- 直接依赖与正式迁移可以让“更新插件后还需执行什么”成为显式合同，避免宿主传递依赖消失或 schema 漂移后
  才在线上请求中暴露数据损坏；
- 保留领域 Repository / Unit of Work 后，存储实现可以替换而不把 SQLAlchemy 类型传进 Agent、Handler 或评测。

## 带来的影响

- `pyproject.toml` 和 lock 将新增 Triage 对 `nonebot-plugin-orm[sqlite]` 的直接运行依赖；nonemigut 当前已解析同一
  依赖，不代表其他部署可以省略；
- 需要设计最小 ORM schema、唯一约束、迁移升降级和事务型 Repository，并用 SQLite 覆盖创建、关联、并发冲突、
  rollback、alias 与回归；
- 插件更新流程增加 `nb orm upgrade`，schema 不匹配时启动应明确失败，而不是以旧表继续写入；
- JSON 原型和已审核 snapshot 的精确短路消费者需要在 ORM repository 上重新接线，不能长期维护两套真值；
- 数据库不可用不会把 Bug 改判成 `not_bug`，只会使本次持久化失败并返回固定失败回执。

## 没有采用的方案

### 继续扩展单个 JSON snapshot

没有采用。它仍需整文件重写、进程内锁和人工 schema 迁移，无法为多实体写入、alias 唯一性和并发建档提供
数据库事务保证。

### 直接使用标准库 `sqlite3`

没有采用。虽然不增加数据库库依赖，但项目仍需自行解决异步调用、Session / 连接生命周期、迁移、模型映射和
NoneBot 启停接线；这些正是 `nonebot-plugin-orm` 已经提供的能力。

### 把 ORM 做成可选存储模式

没有采用。在线 Bug 记录与固定“已记录”回执是同一个核心合同；允许同一版本根据可选 extra 静默退回文件写入，
会形成两套一致性和迁移语义。无法使用 ORM 时应明确不完成持久化，而不是换成较弱真值。

## 与既有决定的关系

- 2026-08-16 已删除未接线的 `ConfirmedBugProblem`、`LocalConfirmedBugProblemRepository` 与
  `runtime-confirmed-bug-problems.json` 写入合同；线上仍在使用的只读 reviewed catalog 在 ORM 替换前保留；

- 接续并部分替代 [ADR-0023](0023-defer-orm-until-durable-business-state.md)：该 ADR 设定的 ORM 触发条件已经满足；
  其余内存、JSONL、可重建 cache 分层仍有效；
- 部分替代 [ADR-0054](0054-store-reviewed-bug-problems-in-localstore.md) 的 JSON snapshot 适配：data 所有权、精确
  适用性和损坏时不得错误短路继续有效，权威读写改由 ORM；
- 落实 [ADR-0068](0068-treat-qualified-agent-bug-verdicts-as-operational-decisions.md)、
  [ADR-0070](0070-separate-bug-reports-occurrences-and-problems.md)、
  [ADR-0071](0071-group-bug-problems-with-versioned-evidence-fingerprints.md) 和
  [ADR-0072](0072-use-opaque-problem-ids-and-minimal-maintainer-lifecycle.md) 的持久化事务边界。
- [ADR-0074](0074-preserve-append-only-problem-decisions.md) 进一步规定事务中的 Decision 历史和 Problem 当前投影。

## 相关文档

- [NoneBot 数据库最佳实践](https://nonebot.dev/docs/best-practice/database/)
- [ADR-0023：按状态语义分层存储，推迟业务 ORM](0023-defer-orm-until-durable-business-state.md)
