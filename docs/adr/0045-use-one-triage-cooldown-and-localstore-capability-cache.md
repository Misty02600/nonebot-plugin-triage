# ADR-0045：使用单一 Triage 冷却与 LocalStore 能力缓存

| 状态 | 决策日期 |
|---|---|
| 顶层维护命令由 ADR-0075、ADR-0076 部分替代；其余决定继续有效 | 2026-08-13 |

## 当时遇到了什么

插件把命令名、Matcher 优先级、入口文字上限和 SQLite 路径等内部合同暴露为部署配置。同时，一次可信
报障先经过所有 `triage` 轮次共有的入口限流，之后又在 Incident 写入前经过独立的报告限流；README 没有
解释两层账本的执行顺序和行为差异。

能力影子已经接入能力说明链，但是否启用取决于部署者是否提供 SQLite 路径。索引本身是可以删除重建的
本地派生数据，文件位置并不是产品能力，普通部署者也不应负责其目录、迁移和清理。

## 决策

1. `triage`、`报错查询`、`报错反馈`、`报错统计`、两组 Matcher 优先级和 2000 字入口上限成为固定产品
   合同，不再作为 NoneBot 部署配置；移除配置不删除对应命令、权限或长度守门。
2. 只保留一个 `NBTRIAGE_COOLDOWN_SECONDS`，默认 2 秒。每次进入 `triage` handler 都按
   `adapter + Bot + conversation + actor` 消费同一个入口窗口；首轮、空输入、超长输入、Reply 续问、
   教学、澄清、报障和策略拒绝共用该窗口。
3. 删除 Incident 专用 30 秒冷却和 `LiveReportService` 的第二份限流账本。同一请求不得在入口和建单阶段
   被统一 limiter 计算两次。该入口账本仍是单进程内存状态，重启清空，不替代跨进程配额或模型费用预算。
4. 能力影子保留并默认启用，但删除 `NBTRIAGE_CAPABILITY_SHADOW_PATH`。SQLite 使用
   `nonebot_plugin_localstore` 的插件级 cache API 解析固定内部路径；位置、文件名和数据库格式不属于
   公开契约，测试可通过内部依赖注入覆盖路径。
5. 能力影子是可删除重建的 cache，不是权威数据。首次构建、刷新、打开或读取失败时，插件必须保留上一份
   可用 generation，或退化到显式 Provider / 澄清；不能阻止插件启动、`triage` 入口或模型语义分流。
6. 能力 cache 不得保存 Token、原始私密日志、未脱敏配置、完整 Thread 内容或用户身份。`restricted`
   能力元数据继续只能在模型外完成维护者鉴权后读取，自然语言索引结果不能直接授权工具调用或副作用。
7. 被移除的旧配置键在加载时明确拒绝并给出迁移方向，不允许因 Pydantic 忽略 extra 而静默失效，也不在
   错误信息中回显旧值。
8. 其余容量、TTL、轮转、knowledge pack 和模型配置继续作为公开高级运维项，并在 README 逐项解释。
   `NBTRIAGE_MODEL_MAX_OUTPUT_TOKENS` 的默认值从与当前资格不一致的 1024 收敛为唯一准入 semantic profile
   要求的 240；部署者仍可配置其他有界值，但不匹配精确任务资格时启动失败。

## 为什么这样选

- 命令词、调度顺序和入口安全上限是同一版本插件应保持一致的合同，不是每个部署都需要重新设计的能力；
- 单一入口冷却既覆盖真实模型请求和每轮 Thread 续问，也避免同一报障在两个账本中产生难以解释的结果；
- 2 秒沿用现有交互入口的节奏，30 秒会显著妨碍正常澄清；额外的建单防滥用以后应以独立、可解释的预算
  或持久策略设计，而不是藏在第二个同用户 cooldown 中；
- LocalStore 已拥有 NoneBot 插件目录生命周期，复用它比暴露 SQLite 路径更少配置、更符合缓存语义；
- 默认启用才能让已加载的第三方插件进入当前 Bot 的能力说明，失败降级则保证这项增强不会成为主链依赖。

## 没有采用的方案

- **保留 support/report 两层 cooldown：** 能继续额外抑制重复 Incident，但同一入口存在两种时间政策，且
  正常用户难以从配置判断本轮在哪一层被拒绝。
- **把同一个 limiter 在入口和建单阶段复用：** 入口已经消费本轮额度，建单阶段再次检查会让同一请求
  必然自拒。
- **保留 `NBTRIAGE_SUPPORT_COOLDOWN_SECONDS` 名称：** 技术迁移较小，但无法准确表达所有 `triage`
  轮次共享窗口的新合同。
- **继续让部署者指定 shadow path：** 暴露内部实现和文件管理负担，没有表达真正的产品选择。
- **删除能力影子或继续默认关闭：** 配置更少，但能力说明只能覆盖显式 Provider，无法满足当前部署中第三方
  插件的确定性导航目标。

## 带来的影响

- 使用旧命令、优先级、文字上限、两组 cooldown 或 shadow path 的部署会在启动时收到明确迁移错误；
- 同一 actor 通过 2 秒入口窗口后可以再次进入 Incident 受理，不再额外受 30 秒报告窗口限制；
- 启动后会调度能力 cache 的后台刷新，并在 LocalStore cache 中持久化公开及受保护的最小能力元数据；
- 单进程入口冷却不解决多 worker 协调、累计模型费用、全局并发或熔断，这些仍是独立运维边界；
- README 只保留真实部署选择，并在配置项本身说明默认、作用域、禁用和失败语义。
- 现有高级运维字段暂不顺带删除；配置表由测试保证覆盖 `NBTriageConfig` 的全部公开字段，避免再次出现
  “实现中存在、README 不解释”的配置。

## 落实与确认

- 2026-08-13：项目作者确认立即实施，并确认 nonemigut 只调整配置、不修改依赖声明或锁文件。
- 实现和验证证据在 PLAN-0014 完成时补充。

## 替代关系

- Problem 查询的独立顶层 `报错查询` 由
  [ADR-0075](0075-register-problem-maintenance-under-triage-subcommand.md) 部分替代；`triage` 根、固定产品合同和
  统一冷却继续有效；
- `报错反馈` 与 `报错统计` 顶层聊天命令由
  [ADR-0076](0076-remove-legacy-trial-feedback-and-stats-chat-commands.md) 删除；底层 trial 与离线汇总不受影响；
- 部分替代 [ADR-0020](0020-use-triage-command-for-natural-language-support.md) 的动态命令和两级限流决定；
- 部分替代 [ADR-0021](0021-use-deployment-local-capability-shadow-index.md) 的默认关闭与显式路径启用决定；
- 部分替代 [ADR-0040](0040-require-trusted-preflight-failure-before-incident.md) 的 Incident 专用限流阶段；可信
  失败证据、请求绑定和一次性授权边界继续有效。

## 相关文档

- [PLAN-0014：收敛插件部署配置面并重写 README 配置语义](../plans/done/PLAN-0014-simplify-plugin-configuration-surface.md)
- [架构概览](../architecture/overview.md)
- [统一支持入口](../architecture/flows/support-intake-routing.md)
- [能力影子索引](../architecture/flows/capability-shadow-index.md)
