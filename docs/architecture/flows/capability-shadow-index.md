# 流程：部署本地能力影子索引

## 这条流程保证什么

影子索引用来回答“当前 Bot 有哪些可说明的能力证据”，不回答“这个用户现在一定能执行什么”。它默认启用，
SQLite 位置由 LocalStore 插件 cache 管理而不是部署配置；普通用户只能检索当前 adapter 域内通过确定性门禁
的公开记录，SUPERUSER 可以查看带 issue 或受限记录。

```text
pyproject 声明 + 制品摘要 + 已加载模块
                ↓
       deployment inventory（完整性门）

已加载 Plugin / Matcher / Alconna + PluginMetadata
                ↓
字段级 Claim / Evidence / Constraint + trigger entries
                ↓
disclosure + PlatformScope + analysis_issues + RecordState
                ↓
       原子构建本地 SQLite FTS5 索引
                ↓
当前 adapter 的 ServingView / 鉴权后的维护者域

后台教学注释（只接收当前已注册 public 记录）
                ↓
runtime 命令事实 + ast-grep Matcher 结构 + 内存配置投影
                ↓ 首包不足时
批准根只读 glob/search/read + Jedi 转到定义
                ↓
公开教学注释 cache（无正文/配置值；保留动态 Evidence revision 清单）
                ├─→ 当前问题 + 公开事实 → Answer Agent → 上下文相关教学回答
                │                              └─→ 失败时确定性注释模板
                └─→ 当前 runtime 命令事实 + 单一规范展示形式
                                                ↓
                         LocalStore data/help-display/<module>.yml
```

采集器不额外导入插件，也不执行 Matcher、Rule、Permission 或 handler。keyword / regex 等可确定入口保存
`trigger.factory` 与 `trigger.entries`；动态或被动入口保留 `dynamic_entry`，不通过 AST 猜测 handler 效果、
Matcher 角色或跨 Matcher 支撑关系。

## 普通查询门禁

普通 ServingView 在召回前要求：

- snapshot generation 已发布、新鲜且 `partial == false`；
- 本轮 deployment inventory 成功且完整；
- `disclosure == public`，当前 adapter 在 `platform_scope` 内；
- `analysis_issues` 为空，`RecordState` 为 `VERIFIED / CANDIDATE`；
- 记录可以投影出经过观察的 command header，或有界、安全的 keyword / regex 入口。

能力 ID 白名单在 FTS 排名和 `limit` 前应用，结果反序列化后再次执行 ServingView 检查。`restricted`、平台不
匹配和带 issue 的记录不会先进入模型再被隐藏。维护者域必须先在模型外完成 SUPERUSER 鉴权。

自动教学注释沿用同一门禁，并且必须由当前 runtime 记录反向定位已经加载的模块。它不会遍历静态制品并把
“源码存在”解释成“Bot 当前可用”；加载失败、`not_observed`、restricted、平台未知或带 issue 的能力即使留有
旧注释 cache，本轮也不会提供。注释无需逐条人工审核，但仍不能绕过运行时注册、披露、平台和 Evidence
闭包。插件不提供独立的教学注释开关；只有合格模型 transport 可用时才组装注释任务，缺少模型配置、密钥或
任务资格时跳过模型增强，确定性能力索引与插件启动不受影响。

教学工具不能读取 `.env*`、凭据、数据库、日志、Migut Help 人工 YAML、评测 Gold 或本任务生成的
help-display。Bot 项目、目标插件及其 LocalStore config/data/cache 是按任务批准的文件根；当前解释器的
依赖 Python 源码只进入导航 profile，不允许在整个依赖环境自由 glob。Jedi 只提供从已知文件位置转到定义，
定义位置本身不能作为结论，必须再经受控 `read_file` 取得可引用 Evidence。

完整刷新后，插件还会把当前公开注释投影为一插件一文件的最小帮助展示 YAML，写入 LocalStore 管理的
`data/help-display/`。每条命令只有一个 `display`，模型只能围绕确定性的命令头补充参数或回复上下文；源码、
Evidence、配置值、指纹和审核状态都不会进入文件。当前版本没有草稿、审核或发布流程，刷新会直接更新本
生成器标记的文件；该目录也没有接入 Migut Help，所以这些文件目前不会成为用户可见帮助。

## 状态与失败语义

- 制品版本、VCS commit、有界相对路径与文件摘要用于部署清单和诊断，不构成逐能力源码身份合同。
- `.env*`、日志、数据库、缓存和运行数据不参与摘要，索引不保存原始配置值。
- 新索引在临时文件完整写入并校验后替换目标；构建失败保留最近可用索引。
- LocalStore 路径只在启动刷新阶段解析；解析失败、cache 不可写或版本不兼容时记录稳定错误类型并降级，
  不阻止插件加载、`triage` 或模型语义分流。
- 自动注释按能力串行生成并独立失败；某个插件的源码不可读、模型输出无效或请求失败时，该能力退回确定性
  元数据说明，其他能力和基础 SQLite 索引继续可用。
- 插件受管 Python 源码 inventory 不完整、含未处理 symlink 或分析期间 revision 改变时，不发布新注释；
  插件源码任意变化会让该插件的全部教学注释重算。源码与其他生成输入均未变化且动态 Evidence revision
  仍匹配时，逐字复用缓存并不调用模型。
- 展示 YAML 只在完整 snapshot 和注释刷新成功后更新；partial snapshot 保留现有文件，完整刷新会删除本生成器
  标记、但本轮已不再对应任何公开 runtime 能力的陈旧文件，不删除同目录中的其他文件。
- deployment 未刷新、刷新失败、snapshot / deployment 任一 partial 或索引 stale 时，普通查询失败关闭；维护者
  仍可读取最近快照并看到 partial / stale 标记。
- `opaque` Permission、Rule 和 handler 条件只表示无法静态求值；能力说明不等于执行授权，实际执行仍由原
  插件裁决。
- 所有第三方文本在进入消息前都会折叠空白、限制长度、移除控制字符并中和 mention。

## 相关决定

- [ADR-0021：用部署本地影子索引整理 Bot 能力证据](../../adr/0021-use-deployment-local-capability-shadow-index.md)
- [ADR-0026：在检索与模型前隔离能力知识受众域](../../adr/0026-filter-capability-knowledge-before-retrieval.md)
- [ADR-0032：分离能力受众、平台范围与分析问题](../../adr/0032-separate-capability-audience-analysis-and-platform-status.md)
- [ADR-0036：保持能力影子确定且以记录为单位](../../adr/0036-keep-capability-shadow-deterministic-and-record-oriented.md)
- [ADR-0045：统一 triage 冷却并用 LocalStore 管理能力 cache](../../adr/0045-use-one-triage-cooldown-and-localstore-capability-cache.md)
- [ADR-0058：用确定性证据与有界源码导航生成教学注释](../../adr/0058-use-deterministic-evidence-and-bounded-navigation-for-teaching-annotations.md)
- [ADR-0059：跨 Agent 链路共享只读证据访问工具](../../adr/0059-share-read-only-evidence-access-across-agent-flows.md)
