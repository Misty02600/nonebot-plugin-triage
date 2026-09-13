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
批准根只读 glob/search/read + 定义导航转到定义 + 版本限定文档检索
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

教学请求单独保留 `on_keyword` 的 `keyword` 模式与完整字面关键词集合，不把索引用的代表 header 当作独立
命令词。usage 只校验真实关键词与必要的 `@bot`，不强加词边界、句首位置或 mention 紧贴关键词；完整调用
结构仍由 Handler Evidence 支持。关键词不转换为正则，普通命令与 Alconna 的结构校验不变。

教学规划按插件构建一次已准入单元的入口索引，向各单元提供其他入口的触发文字、别名和已确认的 Handler
导航位置；family 只保留代表成员及成员数。索引不包含其他单元的生成结果，不作为可引用 Evidence，也不
自动推断共享权限或业务关系。模型按需使用现有 `python_open_definition` 稳定读取后才能引用源码，继续
使用本单元预算，无需等待其他单元生成。索引进入请求 fingerprint，定义位置绑定源码 revision；不能唯一
确认的位置不生成导航句柄。现有流水线并发及发布边界不变。
当前 Evidence 已证明且直接影响使用的跨入口关系应在对应字段保留，包括共享限制、操作影响和必要输入的
获取方式；不展开其他入口的完整教学或继承其权限。family 可选择性打开相关入口，但不遍历成员或索引；
缺少辅助输入获取指引不关闭已能可靠说明的当前能力。调用方式与用途仍需相应 Runtime 或实现 Evidence。

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

当前有效教学注释只能进一步收紧披露：某个 entry 存在独立全局 `role=superuser`，或全局
`condition_group.alternatives` 按结构去重后仅有 `role=superuser` 时，普通检索、Answer、确定性帮助及帮助导出
均排除该 entry；含其他允许分支的 OR 不单独触发隐藏，但不能抵消另一条独立的超级用户角色要求。
同单元的其他公开 entry 保留，全部排除时该记录不进入公开候选，教学补召回也遵守前述能力白名单。
行为边界中的局部权限文字不参与此判断。原始 Runtime disclosure、保存的注释与执行鉴权不变，维护者仍可
查看原始记录；这不是对所有自定义权限的完整静态证明。

自动教学注释沿用同一门禁，并且必须由当前 runtime 记录反向定位已经加载的模块。它不会遍历静态制品并把
“源码存在”解释成“Bot 当前可用”；加载失败、`not_observed`、restricted、平台未知或带 issue 的能力即使留有
旧注释 cache，本轮也不会提供。注释无需逐条人工审核，但仍不能绕过运行时注册、披露、平台和 Evidence
闭包。插件不提供独立的教学注释开关；只要模型 transport 技术可用就组装注释任务。缺少模型配置、
Provider SDK、密钥、网络、任务传输能力或输出校验不可用时跳过模型增强，确定性能力索引与插件启动不受影响；
仅缺少 held-out 评测记录不会跳过模型调用。

`platform_scope` 在上述模型外门禁中完成路由，不进入教学模型 Evidence，也不投影成公开 requirement。
公开字段中，`role` 只描述调用者本人身份；`access` 只描述用户、群或场景已经取得的、由高权限主体控制的
脱敏使用资格；没有 Evidence 证明授权者角色时只写“需授权”或“需已开放”。独立 `scene` requirement 必须携带
完整 `allowed_scenes` 条件集合；原子值为 `private / group / guild / channel_text / channel_category /
channel_voice`；另支持 `non_private` 谓词：排除私聊，不将其展开为当前原子值的枚举，
也不能按互斥标签与具体场景直接比较。只证明非私聊时不要求追查 Adapter 场景全集；另有群消息依赖等
更窄条件时必须保留，不用同一 OR 集合中的 `non_private + group` 表达收窄。
教学条件按实际语义分类，不按 Permission、Rule 或 Handler 来源分类；简单条件优先使用独立的
`role / scene / access / rate_limit`。`condition_group` 表达有限 OR 分支，每条场景 alternative 保存一个场景条件。
条件组还可使用 `allowed_scenes` 保存 Evidence 已明确证明的共同场景：集合内为
OR，与非空 `alternatives` 为 AND；空集合只表示不附加共同场景，不表示整个 entry 适用所有场景，也不代替
未知门禁。场景自身构成替代允许路径时仍使用 scene alternative；不把外层可绕过的场景提升成共同条件。
同一注册表达式内的复合 Permission 仍合并为一个候选；一个 gate 在每个受影响 entry 中保持唯一公开归属，
可以是独立条件、条件组、行为边界或已有规则允许的一组 usage。requirements 之间为 AND，不能拆 OR 为 AND。
单分支组也合法，不强求最大化简；固定事实只有单一角色或场景时直接输出原子条件。
已证明的嵌套角色 OR 可展开，但不展开权限系统内部的默认授予关系，也不依据平台等级推测角色集合。
不新增通用布尔树或复杂权限的关闭政策。Migut Help 不投影混合组、带共同场景或额外独立角色的原生
权限标签，也不把独立 admin 扩成 admin OR owner；不追加权限文字到最小 description，Answer 保留完整条件。
共同场景不会改变仅超级用户分支的披露限制，实际鉴权仍由原插件执行。
当前 Schema 15 / Prompt v128 / request v105 不自动迁移旧格式。旧 generation 与人工文案保留，但不能直接
恢复为有效新注释或继续编辑；升级后先完成正常全范围刷新，再使用新格式恢复与编辑。
Schema 15 将可选槽位统一为 `[<参数>]`，`[文字]` 只表达可选固定文字；旧记法无法仅凭字符串可靠区分
这两种含义，因此不自动加括号。模板、请求与分析版本一同更新，旧结果不作为新记法的缓存或编辑基线复用。
当前没有按教学场景过滤调用者的确定性消费者；不为这个新增值建立通用场景匹配器或权限计算器。
若以后添加场景过滤，须按谓词含义判断 `non_private`，未知上下文不能仅因不等于 `private` 就当作已知非私聊。
本次边界见 [ADR-0124](../../adr/0124-express-non-private-teaching-scenes-directly.md)。
条件来源与结构的分离见 [ADR-0127](../../adr/0127-separate-teaching-condition-shape-from-gate-origin.md)。
当前配置未知不等于规则未知：源码已证明设置条件与公开效果时，可引用源码作条件性边界，不推断当前值或
启用状态；已确认关闭的分支仍省略。入口始终检查的授权资格仍归 `access`，已确认生效的全局限流仍归
`rate_limit`，不以用户能否修改配置决定是否说明其公开影响，也不为此读取任意运行数据。
业务数据或其他准备状态进入 `behavior_boundary`。边界可说明全局行为或特定分支；局部条件或结果须在
同一条说明中保留适用条件，不泛化为全局，也不要求穷举所有业务分支。已发现 gate 的覆盖要求不变，
不重复 usage 已表达的参数结构或 constraints 已表达的全局条件。当前 Handler 提到另一条命令的提示文字
不能单独证明该命令的当前详细用法，必须同时存在目标命令当前的 Runtime 或实现 Evidence。

教学工具不能读取 `.env*`、凭据、数据库、日志、Migut Help 人工 YAML、评测 Gold 或本任务生成的
help-display。Bot 项目、目标插件及其 LocalStore config/data/cache 是按任务批准的文件根；当前解释器的
purelib / platlib 和生效的 site-packages / dist-packages 自动作为 Python-only 导航根，无需逐包批准，也不允许在整个依赖环境自由 glob/search。
定义导航从已知调用位置唯一定位的直接外部函数可以在 8,000 / 32,000 字符预算内预载一层，但不递归进入依赖
BFS；过长或不可切片的唯一定义只生成不可引用的精确 read target，必须再经受控 `read_file` 取得可引用
Evidence。只有编译扩展的 `.pyi` 签名同样只作为导航事实，不能支持业务行为结论。教学请求把目标插件根稳定命名为
`target_plugin`，初始源码 locator 与 `target_plugin_*` 工具都使用相对插件包根的路径；`bot_project` 明确只指
宿主部署项目，并且只在目标就是本地宿主插件、单文件插件与宿主根共享目录，或已有宿主 Evidence 时提供，
不应被当作外部目标插件源码的试探入口。
注册 gate 若先唯一定位到目标插件模块级赋值，首包会在相同预算内保存静态绑定链，并从 RHS 唯一到达的一层
外部函数取得实现；它不识别特定权限库，也不递归依赖。初始和动态 Python Evidence 为已展示的直接调用、
装饰器和基类附带请求内位置句柄；模型只用 `python_open_definition(navigation_ref)`，服务端完成定义跳转、
revision 复核和唯一目标的稳定有界读取，并在同一次调用中返回可引用 Evidence。多个目标只返回候选句柄，
文件变化或句柄失效时 fail-closed。
Handler 或本地 helper 的参数若直接写成 `Annotated[..., Depends(provider)]`、默认值
`parameter: Type = Depends(provider)`，或经定义导航唯一定位的插件内类型别名静态展开成前一种形式，provider
函数也沿同一深度、字符和 revision 边界加入初始 Evidence。`Depends(factory(...))` 只在原调用表达式直接可见、
factory 是唯一可定位的纯名称或属性链、且参数为空或全部为静态字面量时，把工厂源码和原表达式作为同一
依赖关系提供；类型别名中的工厂不在未提供别名绑定 Evidence 时单独展开。系统不会执行工厂，动态参数、lambda、
运行时包装和多候选仍不猜测；
该导航不根据 provider、参数或局部变量名模型外推断业务语义，也不会因此读取 LocalStore 动态文件。
Handler 与本地 helper 的普通函数调用只在首包自动展开两层；到达第二层后不再先执行定义导航再丢弃结果，
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
`last_good` 是已由当前 generation 发布的结果（模型候选经完整校验，维护者修订另循下述编辑边界）；`pending` 是经完整校验但尚未发布的
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

### 维护者微调行为边界

SUPERUSER 可查看并精确替换一个教学 entry 已有的原始 `behavior_boundary`。编辑绑定活动 generation、
unit ID、entry ID 和完整旧文字；任何不匹配、入口或 Evidence 失效都拒绝。只重新准备选中单元核对当前性，
不调用模型、不改 usage、requirements、Runtime 或 Evidence；Parser 派生的参数数量边界只读。
人工编辑通过结构校验即发布，语义由维护者负责；它会进入 Answer 和教学预检使用的文字，不标为模型验证通过。

新版 generation 同时保存 `annotations.json`，复用原生注释缓存结构，保存原始注释和本次人工修改的操作者、
时间、旧版本及前后文字。它与 Help / Answer 共用一次原子指针切换，不是独立覆盖文件；指针失败保留旧视图，
发布期间取消需等当前文件写入与内存切换收尾。恢复时以活动 generation 的结构化内容为已发布基线，缓存中的
新 pending 仍按原合同独立验证，不能被旧发布快照覆盖。缓存丢失不丢人工修改，但源码 / 请求 / Evidence
失效规则不变。旧 v2 generation 没有该材料，沿用原缓存，完成正常刷新后才能使用人工编辑。

`annotations.json` 包含内部身份、Evidence 清单和人工记录，不是公开投影；备份教学 data 时保留，公开分享前
脱敏。人工修改是非证据基线，不是永久锁定：重生成可以依据当前 Evidence 明确 replace/remove，未修改时保留。
设计取舍见 [ADR-0126](../../adr/0126-publish-maintainer-boundary-edits-with-teaching-generations.md)。

## 教学刷新与部署变更边界

Python 定义导航使用 `ty==0.0.80`。每次 `CapabilityAnnotationService.refresh` 懒启动一个共享进程，
解析环境一次绑定本轮目标插件源码根与当前解释器导入路径，不由首个单元决定其他插件的解析范围。
首包准备与模型补证均使用同一会话；完成工作区配置握手后才提交定义查询。正常、失败或取消退出时回收，
下轮新建，不自动重启。没有查询时不启动；独立诊断调用自行回收。
`ContentModified` 仅在源码未变时于原时限内最多重发两次，其他错误不重试。路径与 revision 校验仍在
`readonly_tools/python_navigation.py`，进程协议在 `readonly_tools/ty_navigation.py`。见
[ADR-0123](../../adr/0123-use-refresh-scoped-ty-definition-navigation.md)。

Triage 是已加载能力的只读分析与教学服务，不是 Python 热重载器。支持的部署流程是：更新插件代码或依赖后
重启 Bot，待当前插件完成加载，再由启动任务复核缓存或重建注释。未变化也不是无条件复用：Runtime、生成合同、
配置投影和引用的 Evidence 同样必须满足现有校验。

- 运行中允许通过 `triage 刷新帮助 [plugin_module]` 重新生成当前已加载能力的教学知识。该命令重新采集
  Runtime snapshot，但不 reload 模块、重新注册 Matcher、安装依赖或重新读取 `.env`。
- 一轮分析要求已加载对象与磁盘源码属于同一部署版本，并在分析期间保持稳定。只修改文件但不重启，可能使
  内存继续执行旧 Handler，而源码读取到新实现；文件摘要不能证明二者一致，这种使用方式不在支持范围内。
  第三方插件热重载、运行中替换依赖和编辑器未保存缓冲区同步也不属于当前兼容承诺。
- 读取与发布前的 revision 检查是漂移保护，不是持续文件监听或热更新机制。检测到插件源码变化时，作废
  该插件本轮 staging；引用的依赖 Evidence 失效时，相关候选不得发布。其他未受影响的插件仍按原流程处理。
  不追着新文件继续同一轮分析，也不把不再有效的旧注释当作当前知识。
- Triage 不承诺任意磁盘编辑后立即停止全部旧知识服务，也不证明内存代码与磁盘文件永远相同。部署者应在
  更新并重启完成后重新建立可信教学视图；不以此为由取消现有路径、revision、稳定读取和发布复核。

当前刷新入口见 `CapabilityShadowService.refresh_teaching`，Runtime 来源见 `build_capability_snapshot`；
源码漂移复核由 `plugin_source_revision_matches` 与 `_final_source_changed_plugins` 执行。此处明确已有职责，
不新增 watcher、自动重载或代码更新服务。

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
- 请求准备完成后，复用 Evidence 校验检查初始依赖源码；不能验证时在调用模型前记录 `prepare` 阶段失败
  与具体原因。可选源码工具不可用本身不阻止生成；已有 Evidence 仍须可验证。发布前继续复核初始依赖与
  动态 Evidence，不能用准备阶段的检查代替最终时效检查。
- 模型输出通过内部 Schema 与 Evidence 闭包后，还必须投影成公开 entry。投影失败以稳定的
  `projection_*` 错误码反馈给同一 Agent，与其他输出校验共用最多两次纠错额度，并受同一轮时间、token
  和请求预算约束；额度或预算耗尽后记录失败，不从头重跑 Agent。单元状态、cache
  `last_attempt.detail_code` 和警告日志只记录稳定码，不记录真实源码或模型全文。
- 瞬时连接、限流和 5xx 由 Provider SDK 在同一逻辑模型请求内最多重试两次；教学服务不再因此从头重跑
  整个 Agent 单元。每个待分析单元在一次刷新中只启动一次 Agent；后续刷新或维护 `--retry-failed`
  仍可定向重测失败项并复用有效成功结果。历史缓存中的两次尝试记录继续可读。
  显式维护诊断会按 SDK attempt 保存有界脱敏的失败响应，生产 trace 不保存正文。
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
- 已有 Permission profile 同时作为局部框架 API Evidence 供给：初始源码模块的显式绝对导入、已预载依赖
  定义，以及后续稳定源码读取 / 定义导航都可附带相关语义。注册表达式可归属时的 fixed constraint 与这类
  API 文档不同；导入存在不证明该 API 被执行，也不决定自定义 gate 的 AND / OR、场景或业务边界。
  API 文档与源码分开引用，使用同一份现有角色 / 场景映射、去重 ID 和内容 revision；动态引用在复用与发布前
  重新校验。不新增角色表、任意布尔推导或额外定义导航调用，未知 API 仍按源码或既有文档工具补证。
- 请求内 Evidence 统一登记：初始与动态材料同 ID 且全部字段一致时复用，字段不同则拒绝身份冲突；
  工具可以再次返回初始事实供引用，但输出只携带真正新增的 Evidence，不重复登记或静默覆盖初始事实。
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
