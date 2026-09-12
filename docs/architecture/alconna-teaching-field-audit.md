# Alconna 字段与教学职责核对

核对日期：2026-09-11。依据当前安装的 `arclet-alconna 1.8.44`、
`nonebot-plugin-alconna 0.62.1` 源码及 Triage 工作树。本文是覆盖现状与取舍记录，
不是新配置层的实施计划；“候选”不表示已经采集、已经支持或决定实施。

## 结论

剩余字段不适合统一附加“字段、生效值、字段含义”。现有确定性 usage、派生数量说明、
披露过滤和 Evidence 已经各自负责一部分语义；再复制解释会造成重复、维护分叉，甚至让模型
把框架开关解释成不存在的使用限制。判断单位应是能改变的教学输出，而不是库的字段数量。

少量尚未覆盖的简单解析事实可以按需进入现有 Evidence，并附带已有框架语义结构中的说明。
这不需要新的公开字段、模型查询工具或通用配置解释器，但仍有生效值提取、版本核验、
缓存指纹与输出验证的成本。没有具体教学收益时不增加。

本文覆盖命令及节点、参数、CommandMeta、Namespace、ShortcutArgs、NoneBot Alconna Config
和主要注册参数；不声称穷尽所有 BasePattern 子类、Extension API、任意回调或业务自定义配置。

## 决策状态与回查方式

- **已支持（限定范围）**：按下表已有路径处理，边界外不自动获得支持。
- **暂不处理**：当前不新增该项专门支持；已明确讨论的项目在理由中注明，不再作为遗漏反复提出。
- **沿已有路径**：交给现有源码、Evidence 或披露规则，不增加字段专用解释；不表示任意写法均已支持。
- **待具体用例判断**：尚无实施决定，不是待办或承诺；须先证明它会改变哪项教学输出。

暂不处理的项目只有出现具体教学错误、可证明的必要用法遗漏，或原有成本 / 支持边界发生实质变化时，
才重新评估；库中存在该字段、尚未采集它或内部机制复杂，本身不构成重开理由。
以下“已有职责”均属限定范围的现有支持；“展示、会话与运行配置”按其列出的取舍维持现状，
不是待实现清单。普通语法覆盖和局部取舍直接维护本文，不为每个参数单独建立 ADR。

## 已有职责：保留原路径，不重复增加字段释义

| 字段或结构 | 当前处理 | 边界 |
|---|---|---|
| 命令名、节点名、aliases、Args / Option / Subcommand 层级 | 快照与确定性 usage 投影 | 支持范围内固定结构由代码决定；不是任意层级组合均支持 |
| prefixes、Namespace headers；use_cmd_start / alconna_use_command_start | 读取注册后的命令事实，遵循已有命令头展示策略 | canonical 命令正文不擅自添加全局 COMMAND_START；不再让模型解释环境变量优先级 |
| 根、节点与 Arg 的 separators；use_cmd_sep / alconna_use_command_sep | 按运行时生效边界生成固定分隔字符 | 无安全展示字符等结构仍拒绝；不复制全局、Namespace、局部来源值 |
| CommandMeta / Namespace compact、Option compact | 现有结构事实及模板支持边界 | 有限制的支持不等于完整解析器复刻；不重新列为待补字段 |
| requires | 已支持限定范围的顶层固定前置词 | 嵌套、冲突、特定 compact / 分隔组合仍拒绝，详见主文档 |
| Arg optional / OPTIONAL、has_default、MultiVar flag | usage 的必填、可选及重复结构 | 默认值存在与默认值内容不是同一件事 |
| MultiVar length | 有限上限绑定具体槽位，由代码派生 behavior_boundaries | 隐藏有限参数、压缩后丢失槽位及 aggregate family 等仍拒绝 |
| 无参数 Option 的原生 count | 快照 repeatable＋完整 Option 组后的 `...` | 仅动作等于原生 count 且值为整数 1；不扩展压缩列表、别名概念槽位或连续简写 |
| 带普通参数 Option 的原生 append | 复用 repeatable，生成 `[--tag <标签>]...` 等结构 | 不扩展无参数、自定义动作、隐藏 / MultiVar / 关键字参数组合或压缩展示 |
| Arg hidden / HIDDEN | 已有参数投影边界 | 不能据此声称任意隐藏参数均有完整教学支持 |
| description、usage、example、help_text、notice | 声明类 Evidence | 帮助文本属于证据，不自动覆盖当前语法事实 |
| 快捷指令 command、args、prefix、compact、fuzzy、humanized 等 | 已有 shortcut 事实与展示证据 | shortcut 的 fuzzy 表示尾随参数处理，不能套用命令 fuzzy_match 的解释；wrapper 仍需实际源码 |
| CommandMeta.hide、命令 enabled、权限及 Rule | 现有披露、约束及来源分析路径 | 不把隐藏、禁用状态改写成普通用户的操作步骤 |
| AlconnaMatcher.dispatch | 运行时路由事实＋自动附带的 framework_semantics | 已说明是解析路径分支，不凭它新增角色或业务限制 |

## 尚未完整覆盖：只有部分适合简单事实与语义

| 字段或结构 | 决策状态 | 对教学可能产生的影响 | 核对结论与理由 |
|---|---|---|---|
| CommandMeta.strict / Namespace.strict | 暂不处理 | 未知输入是否允许保留为额外解析参数 | 本轮已讨论并因教学收益偏低而跳过。1.8.44 实际 extra_allow 为两处 strict 任一为假；不能只取 meta，也不能推断 Handler 会使用额外内容 |
| keep_crlf | 待具体用例判断 | 解析输入是否过滤换行 | 仅在具体多行输入教学有需要时考虑；不能单凭它保证端到端多行行为 |
| fuzzy_match、fuzzy_threshold | 暂不处理 | 模糊匹配及纠错输出 | 通常不改变推荐的规范写法，维持默认不加入；也不能把近似拼写都写成可执行别名 |
| skip_for_unmatch | 沿已有路径 | 解析失败与 Handler 的衔接 | 本轮已决定不新增专门投影，结合 Handler 与实际处理链分析；不能单字段推断错误输入总会进入 Handler |
| auto_send_output / alconna_auto_send_output | 待具体用例判断 | 解析失败或帮助信息的自动输出 | 有实际错误反馈教学需要时，与解析和 Handler 衔接成组分析；不能推断所有回复都自动发送 |
| use_origin / alconna_use_origin | 暂不处理 | 消息解析来源选择 | 已决定不新增专门投影；必须结合消息提供、改写和适配器事实，不能单独推导 mention 或回复替参 |
| response_self / alconna_response_self | 暂不处理 | 是否响应机器人自身发送的消息 | 常规人类用户指令教学收益低，维持默认不加入 |
| Arg.value / pattern、ANTI、转换及验证规则 | AntiPattern 已支持（限定范围）；其余沿已有路径 | 允许的值、反向匹配、转换后的值 | 本轮选择保留 AntiPattern 身份并按需附带概念，不建立完整模式翻译或取值解释系统；具体规则与槽位名由模型结合源码判断，不把反向基础类型直接改写成固定槽位名，不重启已搁置的取值列表项目 |
| KeyWordVar 及其 MultiVar 包装 | 已识别；语法支持暂不处理 | 固定键、键值对、布尔简写等真实输入语法 | 本轮接受先识别并拒绝相应教学单元，暂不扩展复杂模板；附带定义不会解除语法支持限制 |
| 节点 action、Action.type / value | count / append 已支持（限定范围）；其余待具体用例判断 | 选项重复、累积、计数及结果存储 | 无参数原生 count 与带普通参数原生 append 已按上述边界生成重复标记；不复制完整 Action 模型，次数或累积结果的业务用途仍依赖 Handler，其余动作不自动套用 |
| 节点 dest | 沿已有路径 | 解析结果的目标键 | 必须结合 Handler 如何取值解释，不能直接当用户参数名或额外语法 |
| 节点 default、Field.default / alias | 暂不处理新增说明 | 缺省解析结果及默认值的展示 | has_default 已用于可选性；具体值可能是复杂对象或被 Handler 加工，不能直接公开。Field.alias 是默认值展示别名，不是命令别名；本轮已讨论后决定不新增，不继续作为下一轮候选 |
| Option.priority | 待具体用例判断 | 选项解析顺序 | 只有真实歧义时才可能有教学意义；与 Matcher.priority 不同，不默认增加解释 |
| context_style / alconna_context_style | 暂不处理 | 上下文插值形式 | 本轮确认普通输入不因设置此项就必须加括号后，决定不新增教学支持。可用上下文与插值后的结果还需证据；只有具体插件的必要插值用法出现遗漏时才重新评估 |
| Namespace.to_text / converter；shortcut.wrapper | 沿已有路径 | 输入转换、重写 | 可调用对象的名字与通用说明不足以表达结果；沿现有源码证据分析，不在发现阶段调用 |
| behaviors、executors、before / after rules | 沿已有路径 | 解析或执行前后附加行为 | 当前已有 opaque 约束或源码入口；不能用库的通用定义证明具体业务行为 |
| extensions、exclude_ext、alconna_global_extensions | 沿已有路径 | 消息、解析、权限等扩展行为 | 需知道实际选中并执行的实现；不把名称列表直接当完整教学说明 |

这里的“候选”仍须先指出将改善的具体 usage、behavior_boundaries 或使用条件。
例如 strict=False 只证明解析层允许一定形式的额外内容，不能直接生成“可以随意多填参数”的教学。

## 展示、会话与运行配置：不批量加入教学

| 字段或结构 | 当前取舍 |
|---|---|
| author、CommandMeta.extra | 作者信息不解释指令用法；extra 是任意扩展数据，不通用遍历或序列化 |
| formatter_type、hide_shortcut | 影响 Alconna 自带帮助的格式或显示；不等于 Triage 的公开策略，不能自动转换成权限 |
| disable_builtin_options、builtin_option_name | 影响内置 help / shortcut / completion 入口；应与现有内置节点过滤政策一致，不靠解释配置扩张教学范围 |
| comp_config、alconna_global_completion；tab / enter / exit / timeout / hide_tabs / hides / disables / lite | 属于补全会话。全局配置不代表所有命令开启会话；按此前取舍不新增此项教学支持 |
| Field.completion / unmatch_tips / missing_tips | 补全或错误提示回调；不为了取得文本调用第三方函数 |
| raise_exception、enable_message_cache、alconna_cache_message | 异常传递或缓存机制，默认不进入用户教学 |
| Namespace.name、default_namespace、namespaces、command_max_count | 命名空间归属和管理配置；读取实际注册结果，不向模型重放初始化过程 |
| 全局 remainders | 属于解析特殊边界；本轮未补相应语法支持，不把一个集合释义当成已支持用法 |
| alconna_conflict_resolver、alconna_builtin_plugins | 注册冲突与加载选择；以实际成功注册的命令为发现依据，不教学启动配置 |
| alconna_enable_saa_patch、alconna_apply_filehost、alconna_apply_fetch_targets | 消息发送或启动设施；单独不能说明某条命令的输出，不批量附带 |
| on_alconna 的 rule / after_rule / permission / handlers | 沿既有规则、权限和业务源码证据分析；不复制一份注册参数说明书 |
| on_alconna 的 temp / expire_time / priority / block / default_state | 属于通用 Matcher 生命周期、调度和状态，当前没有全量教学投影；仅在具体行为确需解释时沿该行为分析 |

环境变量不是另一组待解释语义。NoneBot 完成配置读取、Alconna 与注册层完成覆盖和合并之后，
Triage 只在必要时读取最终事实。不同字段的合并规则不完全相同，不能新建统一优先级算法代替框架。

## 复杂度边界与后续准入

现有 `FrameworkFieldSemanticProfile` 和 `framework_semantics` Evidence 已能装载经版本核对的
框架字段说明。`dispatch` 已通过运行时约束触发自动附带说明；模型无需再查询已经提供的含义。
后续若确有必要加入简单事实，应复用这条路径，按事实选取相关说明，不附全量参考表。

新增一项仍需明确：

1. 用户教学的哪句话会因此更准确；现有 usage 或固定说明是否已经表达。
2. 当前安装版本的最终值从哪里取得，是否还依赖其他字段或回调。
3. 事实与含义是否可进入已有 Evidence；公开输出继续使用现有字段。
4. 事实或含义变化如何进入 revision / 指纹，以及怎样验证不会把解析语义夸大成业务承诺。

本轮不新增通用字段扫描器、配置模型、语义注册系统、查询工具或公开注释字段；
也不为凑齐配置表扩大已有不支持语法的范围。后续只在具体案例证明收益后决定单项增量。

## 核验依据

- 依赖源码：`arclet/alconna/{typing,config,base,args,action,core}.py`、
  `_internal/{_analyser,_handlers}.py`；`nonebot_plugin_alconna/{config,model,matcher,rule,_argv}.py`。
- 当前采集契约：[snapshot.py](../../src/nonebot_plugin_triage/capability/discovery/snapshot.py)。
- 当前确定性模板：[投影](../../src/nonebot_plugin_triage/capability/teaching/_projection.py)、
  [数量说明派生](../../src/nbtriage/capability/teaching/annotations.py)。
- 现有语义结构：[framework_semantics.py](../../src/nbtriage/capability/teaching/framework_semantics.py)、
  [Evidence 装配](../../src/nonebot_plugin_triage/capability/teaching/_source.py)。
- 支持范围与已接受取舍：[帮助数据源与复用边界](help-source-adapters.md)。

本次为静态源码和现有契约核对，没有执行第三方 Handler、Extension、formatter 或解析回调，
没有新增运行时测试结论。上述候选项的实际接入仍需对应验证。
