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
                └─→ units/<teaching-unit>/{last_good,pending,last_attempt}
                ↓ 当前输入校验或重生成
单元完成即 checkpoint；插件候选仍在内存 staging
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

`platform_scope` 在上述模型外门禁中完成路由，不进入教学模型 Evidence，也不投影成公开 requirement。
公开字段中，`role` 只描述调用者本人身份；`access` 只描述用户、群或场景已经取得的、由高权限主体控制的
脱敏使用资格；没有 Evidence 证明授权者角色时只写“需授权”或“需已开放”。独立 `scene` requirement 必须携带
完整 `allowed_scenes` 原子集合；原子值为 `private / group / guild / channel_text / channel_category /
channel_voice`。同一注册表达式内的复合 Permission 合并为一个候选并按 OR 分支解释，每条场景 alternative
只保存一个原子值。
业务数据或其他准备状态进入 `behavior_boundary`。当前 Handler 提到另一条命令的提示文字
不能单独证明该命令的当前详细用法，必须同时存在目标命令当前的 Runtime 或实现 Evidence。

教学工具不能读取 `.env*`、凭据、数据库、日志、Migut Help 人工 YAML、评测 Gold 或本任务生成的
help-display。Bot 项目、目标插件及其 LocalStore config/data/cache 是按任务批准的文件根；当前解释器的
purelib / platlib 和生效的 site-packages / dist-packages 自动作为 Python-only 导航根，无需逐包批准，也不允许在整个依赖环境自由 glob/search。
Jedi 从已知调用位置唯一定位的直接外部函数可以在 8,000 / 32,000 字符预算内预载一层，但不递归进入依赖
BFS；过长或不可切片的唯一定义只生成不可引用的精确 read target，必须再经受控 `read_file` 取得可引用
Evidence。只有编译扩展的 `.pyi` 签名同样只作为导航事实，不能支持业务行为结论。教学请求把目标插件根稳定命名为
`target_plugin`，初始源码 locator 与 `target_plugin_*` 工具都使用相对插件包根的路径；`bot_project` 明确只指
宿主部署项目，并且只在目标就是本地宿主插件、单文件插件与宿主根共享目录，或已有宿主 Evidence 时提供，
不应被当作外部目标插件源码的试探入口。
注册 gate 若先唯一定位到目标插件模块级赋值，首包会在相同预算内保存静态绑定链，并从 RHS 唯一到达的一层
外部函数取得实现；它不识别特定权限库，也不递归依赖。初始和动态 Python Evidence 为已展示的直接调用、
装饰器和基类附带请求内位置句柄；模型只用 `python_open_definition(navigation_ref)`，服务端完成 Jedi 跳转、
revision 复核和唯一目标的稳定有界读取，并在同一次调用中返回可引用 Evidence。多个目标只返回候选句柄，
文件变化或句柄失效时 fail-closed。
Handler 或本地 helper 的参数若直接写成 `Annotated[..., Depends(provider)]`、默认值
`parameter: Type = Depends(provider)`，或经 Jedi 唯一定位的插件内类型别名静态展开成前一种形式，provider
函数也沿同一深度、字符和 revision 边界加入初始 Evidence；
该导航不根据 provider、参数或局部变量名模型外推断业务语义，也不会因此读取 LocalStore 动态文件。
Handler 与本地 helper 的普通函数调用只在首包自动展开两层；到达第二层后不再先执行 Jedi 再丢弃结果，
而是只为已展示调用保留请求内 `navigation_ref`。普通单元可以按需继续打开定义；gate、参数依赖与静态
family Callable 仍按各自确定性规则闭合，不受普通调用深度缩短影响。

普通能力继续一项 Runtime Matcher 对应一个分析单元。Runtime 记录中的 Handler 带闭包自由变量时，适配器
用当前 callable 的模块、qualname、首行和源码 revision 建立代码身份；共享同一 Handler 代码身份的公开成员
共享一次分析。请求仍提供全部当前成员，但把 anchored 命令、alias 与语法可信度放入紧凑成员清单，只有
Runtime adapter 能证明完整的 Parser 参数结构才另存为可复用 shape；当前 Alconna 是 `parser_exact`，联合
输入的成员限定类型也会完整保留，不能把 `Text | Image | At` 退化成 `typing.Any`；普通
`on_command` 是 `anchor_only`，缺少 shape 不表示没有参数。共享 Handler、源码依赖和共同 gate 只提供一次。
若共享 Handler 访问闭包成员的 Callable 字段，且静态工厂表把该字段唯一绑定到目标插件本地函数，首包还会在
8,000 / 32,000 字符预算内按定义去重加入这些 `python_family_callable`；它们不成为递归 BFS seed，也不
触发逐成员 Agent。
参数数量、图片或文字输入、必选性和精确 usage 不同不会单独关闭 family。Handler 代码身份无法唯一定位、同组
含未准入成员、源码 inventory 不完整、共同业务概念不可信，或 gate 冲突且无法安全绑定成员时，整个 family
`knowledge_enabled=false`。family 初始 Evidence 不再另设条目总数上限，因此大型 Runtime family 不会只因
成员数超过 64 而在模型前失败；单条 Evidence 的字节上限与 Agent 的 token、请求、工具和费用预算仍然有效。
family 不再完全关闭源码工具，但只获得 `python_open_definition(navigation_ref)`：它不能枚举目录、全文搜索或
任意读取文件。工具说明明确要求在有限预算内选择性打开支撑共同语义或缺失参数含义的少量定义，不得逐成员
浏览，也不得用源码导航代替完整 manifest 复核。
全局消息、通知、请求和没有确定公开触发形式的被动监听器仍不进入第一阶段教学分析。
查询把 Runtime 索引结果与公开注释统一为一个候选排序：Runtime command 或 alias 精确命中最高，注释 name
精确命中其次，独立 search term 再其次，summary 只作低权重补充；同一个 annotation capability ID 在 limit
前收敛为一个候选，不让 family 成员或低权重主索引结果挤掉更相关的注释词命中。每个 search term 必须是一条
可独立查询的短语，不能用标点拼成长列表。不同插件的相似 family 保持分离。精确命中成员命令或 alias 时，
Answer 组合 family 共同知识与该成员从 Runtime record 确定性重建的完整 usage；普通 family 查询只使用聚合
usage。一至三项固定备选在 usage 枚举，四至六项改用由 Evidence
命名的概念槽并在 summary 说明，七项及以上使用概念槽，可以简单概括共同类别但不逐项解释。该规则也适用于单个 Matcher 的多固定命令头、别名、Option 和固定参数值。
family 的异构输入槽位还必须保持可操作：模型选择 Evidence 支持的最窄共同角色；无法用一个词准确概括时
使用由当前 Evidence 命名的概念槽位；Prompt 不提供固定成品词，“参数”与其他槽位名称使用相同的通用
公开文本和 usage 校验，不设置专门门禁、优先级或强制说明。
Uniseg `At` 属于用户直接提供的 `@用户` 输入，即使 Handler 随后把它转换为头像图片，也不能只写进
behavior boundary 而从聚合 usage 删除。

教学缓存位于 Triage LocalStore cache 的 `capability-annotations/`：每个安全插件模块名直接对应一个
`<module_name>.json`，文件内用稳定 teaching unit ID 保存 `last_good`、`pending` 与 `last_attempt`。
`last_good` 是已由当前 generation 发布且通过完整校验的公开结果；`pending` 是已通过相同校验但尚未发布的
候选，只在 revision、fingerprint 与 Evidence manifest 仍匹配时免调用复用；`last_attempt` 只记录最近真实尝试的状态、阶段、请求 fingerprint 和脱敏失败
原因；失败尝试不会抹掉仍精确匹配当前输入的 `last_good`。分片还绑定实际发布它的
`published_generation`。generation 不匹配的 `last_good` 只能作为编辑基线；匹配当前输入的 `pending` 可以进入
新一轮候选，但在 `current.json` 切换前不能服务用户。
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

- 普通运行只持久化无正文 Agent trace。维护者对一个精确插件运行教学刷新时，可显式把完整 assistant 文本、
  thinking、工具往返与 correction 写入指定的本地 ignored 文件；每个单元先追加并同步到 `.partial.jsonl`，
  正常结束再汇总为目标 JSON。该诊断文件可能包含 reasoning 或工具读取的真实源码，不属于活动
  教学 cache、immutable generation 或公开报告，也不会自动上传。独立维护宿主把目标插件的 LocalStore
  cache/config/data 定向到本次临时目录，避免加载和迁移真实部署数据；Triage 自身输出仍按宿主配置保存。
- 制品版本、VCS commit、有界相对路径与文件摘要用于部署清单和诊断，不构成逐能力源码身份合同。
- `.env*`、日志、数据库、缓存和运行数据不参与摘要，索引不保存原始配置值。
- 新索引在临时文件完整写入并校验后替换目标；构建失败保留最近可用索引。
- LocalStore 路径只在启动刷新阶段解析；解析失败、cache 不可写或版本不兼容时记录稳定错误类型并降级，
  不阻止插件加载、`triage` 或模型语义分流。
- 自动注释按 teaching unit 进入同一个有限并发池，同一插件内的不同分析单元也可并行；活动单元数由
  `NBTRIAGE_CAPABILITY_ANNOTATION_MAX_CONCURRENCY` 的正整数配置限制；项目不再施加固定数值上限，全局
  refresh lock、发布锁和缓存写入串行化边界
  保持不变。一个 teaching unit 失败时，其他已生成或精确命中 `last_good` 的单元仍可进入 partial generation；
  失败、stale、skipped 与合法关闭的单元不提供公开知识。确定性 SQLite 索引继续可用，单次请求继续复用
  `NBTRIAGE_MODEL_TIMEOUT_SECONDS`，不另设教学专用超时。
- 插件受管 Python 源码 inventory 不完整、含未处理 symlink 或分析期间 revision 改变时，该插件失败关闭。
  缓存中的插件源码 revision 与当前值不同时，首版重生成该插件全部当前 teaching unit，不按调用关系或
  单元 hash 猜测可复用范围。若分析期间出现 `SOURCE_CHANGED`，本轮该插件已生成与已复用的内存 staging
  一并作废；尚未取得并发槽位的同插件单元停止，已经在途的结果完成后也被丢弃；其他插件仍可发布。源码与其他生成输入均未变化且动态 Evidence revision 仍匹配
  时，逐字复用 `last_good` 并不调用模型。
- 需要重算且存在上一版机器生成注释时，只把上一版公开文字作为 `previous_annotation` 编辑基线；它不属于
  Evidence，其中不向模型提供旧 `requirements` 文字；新 constraint 必须完全由本轮 gate、Runtime 和源码
  Evidence 重建。新 entry 中的 claim 与 constraint 仍须引用本轮当前 Evidence；Answer Markdown 不再由模型生成，而是从公开结构字段确定性渲染。该引用闭包可以阻止旧 Evidence
  或虚构 ID 被继续引用，但不能一般性证明自然语言陈述一定被所引证据语义蕴含；后者由模型资格与离线评测
  观察。
- 公开措辞由 Prompt 约束，不根据插件的 Evidence ID、locator、配置源码符号或函数名动态生成字符串黑名单。
  敏感值在输入准入时排除，公开投影继续验证文本、usage、Evidence 引用、配置引用与 requirement 结构；
  Help / Answer 渲染不复制源码正文或定位信息。
- 模型输出通过内部 Schema 与 Evidence 闭包后，还必须投影成公开 entry。投影失败以稳定的
  `projection_*` 错误码反馈给同一 Agent 定向修正一次；第二次仍失败才关闭该单元。单元状态、cache
  `last_attempt.detail_code` 和警告日志只记录稳定码，不记录真实源码或模型全文。
- 瞬时连接、限流和 5xx 由 Provider SDK 在同一逻辑模型请求内最多重试两次；教学服务不再因此从头重跑
  整个 Agent 单元。显式维护诊断会按 SDK attempt 保存有界脱敏的失败响应，生产 trace 不保存正文。
- Runtime 同一 command entry 的完整 literal 集合由模型外拥有。模型只提出可选的紧凑 `display_trigger`；
  Triage 展开后必须与 Runtime 集合完全相等，首次错误定向重试，第二次仍错则使用确定性完整枚举。最终 usage
  才把已校验的触发表达式替换进固定参数结构；别名压缩失败不会关闭原本正确的知识。Alconna 的 `Help`、
  `Completion` 与 `Shortcut` 内建辅助 Option 在 Runtime 适配时确定性过滤，不进入普通教学 canonical usage。
- Parser canonical usage 以 `slot:N` 保存匿名结构槽位；模型依据 notice、声明 usage 与源码 Evidence 命名槽位，
  校验器只允许改槽位文字，不允许改变必选性、顺序、Option、别名或重复性。精确 family 成员的无模型回退
  只使用类型能保证的“图片 / 文本 / 整数 / 数值 / 参数”，不会公开内部 `Arg.name`。
- Alconna shortcut 不并入 alias，也不替换 Parser canonical usage。快照把当前已注册 shortcut 的 pattern、显式
  `humanized`、目标命令、固定参数、标志与可定位 wrapper 作为有界 Runtime Evidence；模型可据此增加引用闭合
  的可读 usage，证据不足时省略。wrapper 不执行，原始正则和内部符号不进入公开文件；最终仍只发布普通
  `usages`，不新增插件专属字段。
- YAML 与 Markdown 仍只在完整 snapshot 和共享发布可信度成立时作为一个 generation 切换，但 generation
  可以包含 teaching-unit 级 partial 覆盖；两类输出不会在不同 generation 间拼接。partial snapshot、共享
  Provider / schema 身份失败、generation 校验失败或指针切换失败时保留 `current.json`，并丢弃本轮 Answer
  内存 staging。不可变旧 generation 可以保留用于恢复，不会被误拼进新输出。
- 自动刷新继续由启动后台任务执行。SUPERUSER 可发送 `triage 刷新帮助 [plugin_module]` 强制重新生成全部
  或指定插件；参数是 NoneBot 插件模块名。手动刷新使用同一分析、日志、失败与原子发布语义，只额外向维护者
  返回简短结果。
- deployment 未刷新、刷新失败、snapshot / deployment 任一 partial 或索引 stale 时，普通查询失败关闭；维护者
  仍可读取最近快照并看到 partial / stale 标记。
- 已识别的 NoneBot `SUPERUSER` 与 Uninfo 角色 / 场景 Permission 使用一个带 OR alternatives 的公开
  `permission` requirement 表达；若同一表达式仍有未知分支，不把已知分支单独发布成 fixed AND。
- `gate_candidate_ids` 只关联静态层已经发现并解释为 constraint 的候选；Handler/helper Evidence 直接证明的
  其他执行限制仍可形成 requirement 并把该数组留空。没有 gate candidate 不等于没有执行限制。
  `opaque` Permission、Rule 和 handler 条件只表示无法静态求值；能力说明不等于执行授权，实际执行仍由原插件裁决。
- 所有第三方文本在进入消息前都会折叠空白、限制长度并移除控制字符。普通字符串中的 `@用户` 原样保留；它不等于构造平台 At 消息段。

## 相关决定

- [ADR-0021：用部署本地影子索引整理 Bot 能力证据](../../adr/0021-use-deployment-local-capability-shadow-index.md)
- [ADR-0024：自动公开确定且低风险的能力字段](../../adr/0024-auto-publish-deterministic-capability-fields.md)
- [ADR-0026：在检索与模型前隔离能力知识受众域](../../adr/0026-filter-capability-knowledge-before-retrieval.md)
- [ADR-0029：由部署者 deny-list 控制相关配置值进入模型](../../adr/0029-control-model-config-values-with-deployment-deny-list.md)
- [ADR-0032：分离能力受众、平台范围与分析问题](../../adr/0032-separate-capability-audience-analysis-and-platform-status.md)
- [ADR-0036：保持能力影子确定且以记录为单位](../../adr/0036-keep-capability-shadow-deterministic-and-record-oriented.md)
- [ADR-0058：用确定性证据与有界源码导航生成教学注释](../../adr/0058-use-deterministic-evidence-and-bounded-navigation-for-teaching-annotations.md)
- [ADR-0059：跨 Agent 链路共享只读证据访问工具](../../adr/0059-share-read-only-evidence-access-across-agent-flows.md)
- [ADR-0080：把一次能力分析投影为多个公开教学条目](../../adr/0080-model-capability-teaching-as-multiple-public-entries.md)
- [ADR-0093：按插件分片教学注释缓存并按单元部分发布](../../adr/0093-shard-capability-annotation-cache-by-plugin.md)
- [ADR-0094：收敛公开能力教学合同](../../adr/0094-simplify-the-public-capability-teaching-contract.md)
- [ADR-0113：分离路由、授权与业务准备状态](../../adr/0113-separate-routing-authorization-and-business-readiness-in-teaching.md)
- [ADR-0121：在原子发布前 checkpoint 已完成教学单元](../../adr/0121-checkpoint-completed-teaching-units-before-atomic-publication.md)
