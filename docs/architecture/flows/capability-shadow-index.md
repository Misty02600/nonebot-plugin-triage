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
按插件 JSON cache：capability-annotations/<module_name>.json
                ├─→ plugin_source_revision
                └─→ units/<teaching-unit>/{last_good,last_attempt}
                ↓ 当前输入校验或重生成
仅内存 staging（候选注释、fingerprint 与插件缓存更新）
                ├─→ 单元失败：只关闭无当前可信结果的单元
                └─→ SOURCE_CHANGED：作废该插件全部 staging
                ↓ 共享可信度与输出校验通过
不可变 generation + 状态 manifest + 单一 current.json 原子活动指针
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
用精确源码位置解析其唯一外层工厂；同一工厂产生的公开成员共享一次分析。请求同时提供全部当前成员的
anchored 命令、alias、Runtime parser 参数结构和对应 Evidence；共享 Handler 与源码依赖只提供一次。参数数量、
图片或文字输入、必选性和精确 usage 不同保留为成员事实，不会单独关闭 family。工厂源码无法唯一定位、同一
工厂含未准入成员、源码 inventory 不完整、共同业务概念不可信，或 gate 冲突且无法安全绑定成员时，整个工厂
`knowledge_enabled=false`。
全局消息、通知、请求和没有确定公开触发形式的被动监听器仍不进入第一阶段教学分析。
查询先按 annotation capability ID 把同一 family 收敛为一个候选，不让多个成员占满检索 limit；不同插件的相似
family 保持分离。精确命中成员命令或 alias 时，Answer 组合 family 共同知识与该成员从 Runtime record 确定性
重建的完整 usage；普通 family 查询只使用聚合 usage。一至三项固定备选在 usage 枚举，四至六项改用概念槽并在
summary 完整说明，七项及以上只说明类别。该规则也适用于单个 Matcher 的多固定命令头、别名、Option 和固定参数值。

教学缓存位于 Triage LocalStore cache 的 `capability-annotations/`：每个安全插件模块名直接对应一个
`<module_name>.json`，文件内用稳定 teaching unit ID 保存 `last_good` 与 `last_attempt`。`last_good` 是最近
一次通过完整校验的公开结果，`last_attempt` 只记录最近真实尝试的状态、阶段、请求 fingerprint 和脱敏失败
原因；失败尝试不会抹掉仍精确匹配当前输入的 `last_good`。分片还绑定实际发布它的
`published_generation`，与当前 `current.json` 不一致时只能作为编辑基线，不能直接复用或继承失败重试状态。
缓存不保存源码正文或配置值，动态 Evidence 只
保存 ID、相对位置与 revision 清单。文件名不使用 hash fallback，也没有 module 到文件名 manifest；无法
安全直接落盘或在当前轮发生大小写折叠冲突的 module name 只关闭相关插件的教学增强。

一次可发布刷新把同一份内存 staging 投影成两类一插件一文件的数据：紧凑的
`help-display/<module>.yml` 和供 Answer 使用的 `answer-knowledge/<module>.md`。文件先写入 LocalStore data
下 `capability-teaching/objects/<generation>/`，manifest 同时记录每个 teaching unit 的状态和每个插件的
`active / eligible` 覆盖量；全部文件完成校验后才原子替换 `capability-teaching/current.json`。指针是唯一
活动真值，切换成功后才提交 Answer 内存视图；缓存、内存 staging、`last-refresh.json` 和未被指针选中的
generation 都不是 active teaching contract。源码、Evidence、配置值、指纹和审核状态不会进入公开文件。
当前版本没有草稿或人工审核流程，也没有把该目录接入 Migut Help，所以 YAML 目前只供部署者观察生成效果。

## 状态与失败语义

- 制品版本、VCS commit、有界相对路径与文件摘要用于部署清单和诊断，不构成逐能力源码身份合同。
- `.env*`、日志、数据库、缓存和运行数据不参与摘要，索引不保存原始配置值。
- 新索引在临时文件完整写入并校验后替换目标；构建失败保留最近可用索引。
- LocalStore 路径只在启动刷新阶段解析；解析失败、cache 不可写或版本不兼容时记录稳定错误类型并降级，
  不阻止插件加载、`triage` 或模型语义分流。
- 自动注释按插件分组有限并发，同一插件内的分析单元保持顺序；活动插件数由
  `NBTRIAGE_CAPABILITY_ANNOTATION_MAX_CONCURRENCY` 限制，全局 refresh lock、发布锁和缓存写入串行化边界
  保持不变。一个 teaching unit 失败时，其他已生成或精确命中 `last_good` 的单元仍可进入 partial generation；
  失败、stale、skipped 与合法关闭的单元不提供公开知识。确定性 SQLite 索引继续可用，单次请求继续复用
  `NBTRIAGE_MODEL_TIMEOUT_SECONDS`，不另设教学专用超时。
- 插件受管 Python 源码 inventory 不完整、含未处理 symlink 或分析期间 revision 改变时，该插件失败关闭。
  缓存中的插件源码 revision 与当前值不同时，首版重生成该插件全部当前 teaching unit，不按调用关系或
  单元 hash 猜测可复用范围。若分析期间出现 `SOURCE_CHANGED`，本轮该插件已生成与已复用的内存 staging
  一并作废，后续单元停止；其他插件仍可发布。源码与其他生成输入均未变化且动态 Evidence revision 仍匹配
  时，逐字复用 `last_good` 并不调用模型。
- 需要重算且存在上一版机器生成注释时，只把上一版公开文字作为 `previous_annotation` 编辑基线；它不属于
  Evidence。新 entry 中的 claim 与 constraint 仍须引用本轮当前 Evidence；Answer Markdown 不再由模型生成，而是从公开结构字段确定性渲染。该引用闭包可以阻止旧 Evidence
  或虚构 ID 被继续引用，但不能一般性证明自然语言陈述一定被所引证据语义蕴含；后者由模型资格与离线评测
  观察。
- Runtime 同一 command entry 的完整 literal 集合由模型外拥有。模型只提出可选的紧凑 `display_trigger`；
  Triage 展开后必须与 Runtime 集合完全相等，首次错误定向重试，第二次仍错则使用确定性完整枚举。最终 usage
  才把已校验的触发表达式替换进固定参数结构；别名压缩失败不会关闭原本正确的知识。
- YAML 与 Markdown 仍只在完整 snapshot 和共享发布可信度成立时作为一个 generation 切换，但 generation
  可以包含 teaching-unit 级 partial 覆盖；两类输出不会在不同 generation 间拼接。partial snapshot、共享
  Provider / schema 身份失败、generation 校验失败或指针切换失败时保留 `current.json`，并丢弃本轮 Answer
  内存 staging。不可变旧 generation 可以保留用于恢复，不会被误拼进新输出。
- 自动刷新继续由启动后台任务执行。SUPERUSER 可发送 `triage 刷新帮助 [plugin_module]` 强制重新生成全部
  或指定插件；参数是 NoneBot 插件模块名。手动刷新使用同一分析、日志、失败与原子发布语义，只额外向维护者
  返回简短结果。
- deployment 未刷新、刷新失败、snapshot / deployment 任一 partial 或索引 stale 时，普通查询失败关闭；维护者
  仍可读取最近快照并看到 partial / stale 标记。
- `opaque` Permission、Rule 和 handler 条件只表示无法静态求值；能力说明不等于执行授权，实际执行仍由原
  插件裁决。
- 所有第三方文本在进入消息前都会折叠空白、限制长度并移除控制字符。普通字符串中的 `@用户` 原样保留；它不等于构造平台 At 消息段。

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
- [ADR-0088：按插件限制教学注释并发并保持插件内顺序](../../adr/0088-bound-capability-annotation-concurrency-by-plugin.md)
- [ADR-0093：按插件分片教学注释缓存并按单元部分发布](../../adr/0093-shard-capability-annotation-cache-by-plugin.md)
