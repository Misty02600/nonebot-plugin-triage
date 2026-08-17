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
普通 Matcher 按能力分析；闭包 Handler 按唯一外层工厂聚合
                ↓
runtime 命令事实 + ast-grep Matcher / 工厂结构 + 内存配置投影
                ↓ 首包不足时
批准根只读 glob/search/read + Jedi 转到定义 + 版本限定文档检索
                ↓
公开教学注释 cache（只保存公开结果与动态 Evidence revision 清单）
                ↓ 完整轮次成功
不可变 generation + 单一 current.json 原子指针
                ├─→ help-display/<module>.yml → 规范帮助事实
                └─→ answer-knowledge/<module>.md → 公开补充知识
                                      ↓
                      Answer Agent → 上下文相关教学回答
                                      └─→ 失败时确定性注释模板
```

采集器不额外导入插件，也不执行 Matcher、Rule、Permission 或 handler。Command、Startswith、Endswith、
Fullmatch、Keywords、Regex 与 IsType Rule 保存确定的 runtime 入口事实；其中可直接发送的四类字面触发与命令
形成 `invocation.header`。正则、事件类型、空命令及其他动态或被动入口保留 `dynamic_entry`，不通过 CST
猜测 handler 效果、Matcher 角色或跨 Matcher 支撑关系。

## 普通查询门禁

普通 ServingView 在召回前要求：

- snapshot generation 已发布、新鲜且 `partial == false`；
- 本轮 deployment inventory 成功且完整；
- `disclosure == public`，当前 adapter 在 `platform_scope` 内；
- `analysis_issues` 为空，`RecordState` 为 `VERIFIED / CANDIDATE`；
- 记录可以投影出经过观察的 `invocation.header`；当前只包括命令和可直接发送的 startswith / endswith /
  fullmatch / keyword 字面触发。

能力 ID 白名单在 FTS 排名和 `limit` 前应用，结果反序列化后再次执行 ServingView 检查。`restricted`、平台不
匹配和带 issue 的记录不会先进入模型再被隐藏。维护者域必须先在模型外完成 SUPERUSER 鉴权。

自动教学注释沿用同一门禁，并且必须由当前 runtime 记录反向定位已经加载的模块。它不会遍历静态制品并把
“源码存在”解释成“Bot 当前可用”；加载失败、`not_observed`、restricted、平台未知或带 issue 的能力即使留有
旧注释 cache，本轮也不会提供。注释无需逐条人工审核，但仍不能绕过运行时注册、披露、平台和 Evidence
闭包。插件不提供独立的教学注释开关；只要模型 transport 技术可用就组装注释任务。缺少模型配置、
Provider SDK、密钥、网络、任务传输能力或输出校验不可用时跳过模型增强，确定性能力索引与插件启动不受影响；
仅缺少 held-out 评测记录不会跳过模型调用。

教学工具不能读取 `.env*`、凭据、数据库、日志、Migut Help 人工 YAML、评测 Gold 或本任务生成的
help-display。Bot 项目、目标插件及其 LocalStore config/data/cache 是按任务批准的文件根；当前解释器的
依赖 Python 源码只进入导航 profile，不允许在整个依赖环境自由 glob。Jedi 只提供从已知文件位置转到定义，
定义位置本身不能作为结论，必须再经受控 `read_file` 取得可引用 Evidence。

普通能力继续一项 Runtime Matcher 对应一个分析单元。Runtime 记录中的 Handler 带闭包自由变量时，适配器
用精确源码位置解析其唯一外层工厂；同一工厂产生的公开成员共享一次分析。静态层只提供工厂源码锚点、批准
范围和当前性，不生成成员数量、命令样本、共同前缀或业务兼容摘要。工厂源码无法唯一定位、同一工厂含未
准入成员、源码 inventory 不完整，或模型无法形成可靠共同说明时，整个工厂 `knowledge_enabled=false`。
全局消息、通知、请求和没有确定公开触发形式的被动监听器仍不进入第一阶段教学分析。

完整刷新后，插件把同一份有效注释投影成两类一插件一文件的数据：紧凑的
`help-display/<module>.yml` 和供 Answer 使用的 `answer-knowledge/<module>.md`。文件先写入 LocalStore data
下 `capability-teaching/objects/<generation>/`，两类文件和 manifest 全部完成后才原子替换
`capability-teaching/current.json`。源码、Evidence、配置值、指纹和审核状态都不会进入公开文件。当前版本
没有草稿或人工审核流程，也没有把该目录接入 Migut Help，所以 YAML 目前只供部署者观察生成效果。

## 状态与失败语义

- 制品版本、VCS commit、有界相对路径与文件摘要用于部署清单和诊断，不构成逐能力源码身份合同。
- `.env*`、日志、数据库、缓存和运行数据不参与摘要，索引不保存原始配置值。
- 新索引在临时文件完整写入并校验后替换目标；构建失败保留最近可用索引。
- LocalStore 路径只在启动刷新阶段解析；解析失败、cache 不可写或版本不兼容时记录稳定错误类型并降级，
  不阻止插件加载、`triage` 或模型语义分流。
- 自动注释按分析单元串行生成；一轮中任一单元失败时，成功项只写入可复用 cache，不激活半套新视图，也不
  切换文件 generation。确定性 SQLite 索引继续可用，下次完整成功会复用本轮已缓存成功项。
- 插件受管 Python 源码 inventory 不完整、含未处理 symlink 或分析期间 revision 改变时，不发布新注释；
  插件源码任意变化会让该插件的全部教学注释重算。源码与其他生成输入均未变化且动态 Evidence revision
  仍匹配时，逐字复用缓存并不调用模型。
- 需要重算且存在上一版机器生成注释时，只把上一版公开文字作为 `previous_annotation` 编辑基线；它不属于
  Evidence。新 entry 中的 claim、constraint 与 Answer Markdown 仍须引用本轮当前 Evidence。该引用闭包可以阻止旧 Evidence
  或虚构 ID 被继续引用，但不能一般性证明自然语言陈述一定被所引证据语义蕴含；后者由模型资格与离线评测
  观察。
- YAML 与 Markdown 只在完整 snapshot 和整轮注释成功后作为一个 generation 切换；partial snapshot 保留
  `current.json`，文件写入或指针切换失败时同时关闭本轮模型生成的 Answer 内存视图。不可变旧 generation
  可以保留用于恢复，不会被误拼进新输出。
- 自动刷新继续由启动后台任务执行。SUPERUSER 可发送 `triage 刷新帮助 [plugin_module]` 强制重新生成全部
  或指定插件；参数是 NoneBot 插件模块名。手动刷新使用同一分析、日志、失败与原子发布语义，只额外向维护者
  返回简短结果。
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
- [ADR-0069：分离帮助展示与 Answer 知识，并让静态分析只界定证据范围](../../adr/0069-separate-help-display-from-answer-knowledge-and-bound-static-analysis.md)
- [ADR-0077：把上一版机器生成教学内容作为非证据的最小改写基线](../../adr/0077-use-previous-generated-teaching-content-as-a-non-evidentiary-baseline.md)
- [ADR-0080：把一次能力分析投影为多个公开教学条目](../../adr/0080-model-capability-teaching-as-multiple-public-entries.md)
