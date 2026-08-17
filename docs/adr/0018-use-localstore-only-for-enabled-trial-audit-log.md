# ADR-0018：只用 LocalStore 保存显式启用的 trial 审计日志

> 2026-08-17：[ADR-0089](0089-persist-redacted-pydantic-ai-agent-traces.md) 窄范围替代本 ADR 第 8 条中
> “模型 trace 不写入 LocalStore”的决定；trial 审计、原始运行状态和自动遥测边界保持不变。

## 状态

已采纳

## 日期

2026-08-10

## 当时遇到了什么

插件的大部分诊断状态当前位于单进程内存：运行观察、消息引用索引、`LiveIncident`、活动 trial、限流窗口
和临时关联都会按容量与 TTL 淘汰，并在进程重启后失效。这些状态包含短期关联语义，持久化会扩大隐私面，
而重启后随机 HMAC 密钥和框架上下文已经变化，直接恢复也不一定有效。

但 `NBTRIAGE_TRIAL_MODE=observe` 已经是一个真实的插件运行时文件写入：
`nonebot_plugin_triage.trials.create_trial_service()` 当前把轮转 JSONL 写到配置
`nbtriage_trial_log_path`，默认相对路径是 `logs/nbtriage-trials.jsonl`。NoneBot 2.5 的插件发布要求是：插件
只要在本地写数据、配置或缓存，就应通过 `nonebot-plugin-localstore` 获取路径。当前项目尚未声明或使用该
依赖。

## 决定

1. 引入 `nonebot-plugin-localstore` 作为基础运行依赖，而不是评测或 MLflow extra；实现时选择与
   Python 3.11–3.14 和 NoneBot 2.5 验证通过的 `0.7.x` 范围并更新 lockfile。
2. 只有显式启用 `observe` 时，NoneBot 适配层才创建持久 sink。默认文件使用
   `nonebot_plugin_localstore.get_plugin_data_file("trial-events.jsonl")`；它属于部署者拥有、不可从其他来源
   重建的枚举反馈与最小审计事件，因此使用 data 目录，不使用 cache 目录。
3. 删除尚未公开发布的 `nbtriage_trial_log_path` 配置。部署者如需更换位置，使用 LocalStore 的
   `LOCALSTORE_PLUGIN_DATA_DIR` 配置统一覆盖插件数据目录，避免插件再发明一套跨平台路径和权限规则。
4. 继续保持以下状态只在内存中，不写 LocalStore：`RuntimeObservationBuffer`、
   `PlatformMessageReferenceIndex`、`LiveIncidentBuffer`、活动 `LiveTrialService` 状态、rate limiter、原始
   Event / Matcher / API 对象及任何聊天正文、平台身份、API 参数 / 返回值和异常文本。
5. JSONL 继续使用大小轮转。现有默认 `10 MiB`、5 个备份意味着活动文件加备份名义上最多约 `60 MiB`；
   活动 trial 的 TTL 不等于磁盘日志按时间删除。若以后需要按天留存，必须另加显式清理策略和测试，不能
   假设 LocalStore 会自动删除数据。
6. 迁移后的 maintainer `summarize-trials` 不猜测部署路径，要求显式 `--log-path`。默认布局可从
   `nb localstore data` 显示的 base data dir 定位插件子目录；使用插件级目录覆盖时以
   `LOCALSTORE_PLUGIN_DATA_DIR` 配置值为准。插件内现有 `SUPERUSER` 统计命令继续只返回活动聚合。
7. LocalStore 数据不会自动上传到项目维护者的 MLflow。需要分享时必须由部署者主动导出、在本地脱敏并
   检查后再提交；自动遥测或中央回传不属于本 ADR。
8. MLflow experiment、离线评测报告、模型 trace、`data/`、`artifacts/` 和 `evals/` 都不是插件实例状态，
   不写入 LocalStore。

## 为什么这样选

- 只修正已经存在的插件文件写入，符合 NoneBot 插件的跨平台路径约定；
- trial JSONL 的所有者是部署者，LocalStore 不会把它误变成项目维护者的中央审计库；
- 保持短期诊断关联在内存中，避免把重启后已经失去密码学和框架上下文的状态恢复成看似有效的数据；
- 使用 LocalStore 的统一覆盖配置后，插件不再维护重复的路径配置、UNC 特例和工作目录假设。

## 没有采用的方案

- **把所有运行状态都序列化到 LocalStore**：会扩大敏感数据面，并需要恢复、迁移、并发、损坏和删除协议。
- **继续默认写 `logs/` 相对路径**：实现简单，但路径随工作目录变化，也不符合 NoneBot 公开插件文件存储
  约定。
- **把 trial JSONL 放 cache 目录**：缓存应可安全重建；维护者枚举反馈和审计事件丢失后无法重建。
- **直接把 LocalStore 数据同步到 MLflow**：混淆部署者与项目维护者的数据所有权，并引入未经同意的遥测。

## 带来的影响

- 基础安装会增加一个很小的 LocalStore 运行依赖；`off` 模式仍不创建 trial 文件；
- 首次公开发布前可直接删除旧路径配置，不需要公开迁移兼容；已有本地开发日志由维护者自行保留或删除，
  不自动搬运、合并或读取，以免把未知内容复制进新数据目录；
- LocalStore 的插件 data dir 是当前 Bot 进程解析出的部署路径，不是多进程共享日志协议；多 worker 必须让
  每个进程解析到各自独占的数据目录，或改由宿主集中日志系统承接结构化事件；
- 单元测试应给路径解析注入临时目录，避免依赖开发机 LocalStore；NoneBot 集成测试再验证调用者插件目录、
  `off` 零写入和 `observe` 轮转边界。

## 落实与确认

- 实施情况：已完成。基础依赖固定为 `nonebot-plugin-localstore>=0.7.4,<0.8`；
  `src/nonebot_plugin_triage/trials.py::create_trial_service` 只在 `observe` 分支解析固定
  `trial-events.jsonl`，默认 `off` 不调用 resolver，也不创建 LocalStore 目录；路径解析或目录创建失败时
  `observe` 插件初始化失败关闭，不回退到旧相对路径。尚未发布的 `nbtriage_trial_log_path` 已从插件配置
  删除；若配置仍出现该旧键，初始化会给出不回显旧值的迁移错误，避免部署者误以为新事件仍写入旧位置。
  维护者汇总 CLI 也要求显式 `--log-path`；旧 `logs/nbtriage-trials.jsonl` 不迁移。

## 相关文档

- [观察优先的生产试运行](../architecture/flows/observation-first-trials.md)
- [运行观察受理流程](../architecture/flows/runtime-observation-intake.md)
- [NoneBot 数据存储](https://nonebot.dev/docs/best-practice/data-storing)
- [NoneBot 插件发布要求](https://nonebot.dev/docs/developer/plugin-publishing)
