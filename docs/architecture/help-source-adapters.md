# 可选帮助数据源与复用边界

这份说明记录 Triage 如何看待 NoneBot 生态中的帮助菜单、帮助图和命令自动发现插件。结论不是“选一个
帮助插件当真值”，而是把它们当成不同质量的证据来源，再由部署本地影子索引统一标注来源、时效和限制。

## 当前实现

第一阶段已经读取标准 `pyproject.toml` 声明、安装制品 revision、NoneBot 的已加载 Plugin / Matcher 和
Alconna 命令管理器，生成部署本地快照与 FTS5 索引。它不调用 Matcher 的 Rule、Permission、handler 或
Alconna `parse()`，也尚未接入任何第三方帮助插件。

Alconna 命令头直接读取已注册解析器的 `command_header`，以 `command.header_match` 保存原始声明、
编译后的固定文字集合或正则及 flags / 捕获组、compact 匹配信息和捕获转换目标类型。编译内容已包含
前缀，其正则类型不等于原始命令必定是正则。自定义 Pattern 与复杂前缀组合保留为 opaque，不执行匹配、
转换或从对象字符串猜语法；不新增跨版本兼容层。这些事实进入普通 Runtime Evidence 与 family 成员材料。
request v91 / Prompt v116 为可解释的模式命令头使用内部 `pattern` 入口；公开 Schema v13 不变。
当前已编译为正则且保留字符串声明的头部走此路径；固定文字仍使用原模板，opaque 不自动降级，
也不通过捕获模板错误来选择宽松模式。输入 `usage_structure` 复用既有路径和参数渲染，以 `{command}`
代替整个头部，保留 Args、Option、子命令与 dispatch 结构；匹配规则和业务源码由模型解释为完整 usage。
它不是 `canonical_usages`，不逐字对齐，不从公开文字反推头尾，也不要求模型返回额外头部字段。
普通输出及重读投影保留公开语法、Evidence 与独立门禁校验；参数遗漏、错误备选等模式表达语义不由
模板校验保证。有限参数数量仍在 Runtime 事实中，由模型解释；此路径不反推公开槽位名自动追加数量说明。
没有足够事实解释的自定义模式、既有不支持的参数/dispatch 结构仍保留明确原因，不据此声称支持全部语法。

普通 Matcher 的入口识别会读取 NoneBot 2.5 的 `Rule.checkers` 结构。当前版本适配层识别 Command、
Startswith、Endswith、Fullmatch、Keywords、Regex 与 IsType Rule；这不是稳定的跨版本公共协议，因此遇到
未知 checker 或结构变化时保留未知约束并失败关闭，不能猜成公开、可执行能力。

当前 schema v2 把每个已观察命令或 Matcher 保持为独立记录，不从 handler 源码推断用户输出、共享状态
读写、Matcher 角色或跨 Matcher 支撑关系。Matcher、Rule、Permission、命令结构和源码位置仍属于运行或
代码事实，不天然等于一项用户可观察能力；动态或被动入口若缺少确定展示字段，就保留具体 issue 并退出
普通 ServingView。

## Matcher 与 Capability 的边界

适配器先形成带来源和 revision 的记录，再由模型外 ServingView 按披露、平台、记录状态、完整性和 issue
过滤。被动 Matcher 只有在 trigger 和展示标签都能由确定证据安全投影时才可公开；否则维护者仍可查看该
独立记录及其问题。LLM 可以提出引用 Evidence ID 与 revision 的效果描述，但不能凭语义相似度合并记录、
决定披露或平台、声称精确语法，或清除 issue。

## Handler 形参与用户语法的边界

普通单元与 family 共用注册关联逻辑：先确定插件根内的实际文件、核对摘要，再按 Runtime 注册位置缩小候选。
位置缺失或同一行有多个调用时，继续结合入口与同文件 Handler 绑定；所有可用依据仍不能唯一确定时才拒绝猜测。
变量同名或共享 Handler 不构成拒绝理由，也不能据此合并注册 gate。文件身份不靠任意路径后缀或相同摘要猜测，
版本或绑定冲突不回退到较弱匹配；动态入口未知也不能当成与当前入口不同。request v88 收敛此边界，不做通用数据流分析。
request v89 移除单函数/单份 Evidence 8,000 字符、初始源码总字符数、目标函数数和已解析模块数的独立门槛。
Handler、wrapper、注册材料及直接 gate/参数依赖优先完整保留；普通调用与静态 family Callable 的实现是可选
预载。每次发送前复用 Harness 文本估算，并补计当前工具与结构化输出 Schema。request v92 将 64k 改为首包
可选预载的整理阈值，不作为单次输入硬上限或模型容量声明。首包超出时只移除未被结构化事实引用的可选预载；
必要材料及后续历史即使仍超出估算阈值也不拒绝、不截断。维护 capture 保存逐请求估算、整理阈值和移除数量，
与 Provider 实际用量分开。明确的 Provider 上下文超限记录为 HTTP/context_length_exceeded，未知 400 不猜测；
正式刷新不自动重跑整个单元。已完整到达的最终候选仍可校验。192k 单元累计预算、时间/请求/工具限制和本地文件/AST
资源保护保留。自动普通调用展开仍为两层，不因取消字符门槛而递归展开整个依赖树。

request v94 在既有时间、请求、工具和累计 75% 收尾条件之外，增加历史规模信号：剩余累计预算不超过
最近一次 Provider 实际输入用量的两倍时停止源码补证，只保留输出工具并要求提交。进入收尾后不重新开放
导航；没有实际输入用量时不使用此信号。两倍只是提前收尾的启发式，不保证剩余预算够两轮完整请求，
也不替代实际用量硬上限或结果校验；不因这一信号删除 Evidence 或直接判定教学失败。

request v93 / Prompt v117 允许 `usage` 沿用 claim 的 `gate_candidate_ids` 关联调用结构条件，
同一 entry 的多条 usage 可共同承接一个 gate；不要求为覆盖检查追加重复的 behavior_boundary。
其余按字段规则属于行为边界的条件由 behavior_boundary 承接，不限于业务准备状态；全局身份、场景、
授权和限流仍使用结构化约束。gate resolution 的 `constraint` 表示存在实际限制，不指定公开字段。
同一 gate 不跨 usage、边界和结构化约束重复关联；名称、摘要和检索词不允许关联。候选及实现 Evidence、
受影响 entry 的覆盖和公开用法校验保持不变；关联合法不等于已证明任意 Python 条件的语义覆盖。
公开 Schema v13 不变，不增加字段或通用条件树，也不改变消费者的文档结构。

Prompt v133 / request v113 区分控制规则与当前状态：可选配置、名单或外部控制的逻辑已明确，但是否接入或
生效状态未知时，仅以条件性 `behavior_boundary` 说明有教学价值的作用范围和公开效果，不据此关闭知识或
断言当前允许、拒绝、未接入或已满足。默认值和搜索未命中不是当前状态证明；某个用户、群或某次调用的
结果不能推广到整个部署。已确认始终执行的授权资格仍归 `access`，当前主体是否满足资格未知不改变其归属。
若存在对应 gate candidate，规则已明确的条件仍用 `constraint` 并关联该边界；仅当前状态未知不属于
`unresolved`，也不能以默认放行解释为 `no_constraint`。范围一致的当前证据确认未接入且默认放行，或内部
开关已满足时，才省略其公开前提；已确认关闭的路径继续按原规则省略，不改写成假设开启。

Prompt v134 / request v114 区分输入组合与执行效果：两个并列可选槽位须有分别提供与同时提供均合法的证据，
不表示效果相互独立。`usage` 保留输入方式及组合结构，`behavior_boundary` 解释目标选择、优先级、覆盖关系与
前置检查。先保证已证实用法覆盖，再无损压缩；组合合法性不明时不强行合并，也不因效果有关联就删除一种输入。
提交前核对说明中已提及的本次调用输入是否在用法中表达；回复接入、实现证据和模板对齐要求仍然保留。
这是模型生成规则，不能由语法校验或提示词存在性测试证明所有输入方式已经得到完整覆盖。

教学文件工具默认读取 300 行，但允许显式扩大范围；该任务不再将默认行数强制用作行数上限。定义导航尽量
返回完整定义，单次可引用读取最多 32,000 字符，按整行截断并给出续读位置；`python_open_definition` 的
`offset` 相对定义开头，普通 `read_file` 的 `offset` 相对文件开头。超长单行无法容纳时明确报错，不假装读全。
根目录、revision、稳定读取和只读权限边界不变；其他 Agent 的文件读取策略不受此项调整影响。

request v107 / prompt v129 使用 LibCST 1.9.0 共用源码结构查询，处理定义范围、条件名称导航及模块顶层赋值切片。
普通调用仍自动预载两层，并为已选函数的条件表达式补一层唯一的同文件顶层赋值；不求值、不继续自动追踪赋值
右侧，不把多个绑定合并成运行时结论。新增赋值作为 `python_assignment` 可选 Evidence，统一接受现有输入预算
裁剪，不另设条数或字符配额。定位、跨文件跳转继续由 ty 负责。

request v110 将按需导航的名称发现扩展到调用参数、返回值、格式化字符串等表达式，使用 LibCST 作用域绑定
去重；参数和当前定义内的局部变量不因普通读取生成入口，嵌套函数/类的内部引用在打开其自身时处理。
自动预载仍限于原有两层调用和一层条件赋值，不自动追踪赋值右侧。导航沿用原有数量预算，并将同文件中能
确认已提供的定义后置，不为排序执行额外 ty 查询。普通名称导航仍可返回歧义或失败，不解释为运行时唯一值。
方法导航返回 `definition_not_found` 且接收者能唯一关联到带简单类型标注的参数时，提供不可引用的
`related_definitions` 类型标注入口；后续可沿类型别名查找源码，原方法仍保持未解析状态，不按方法名猜实现。

request v112 修正 ty 搜索路径：运行时的 stdlib / platstdlib 根不进入高优先级 `extra-paths`，避免真实
`typing.py` 遮蔽 typeshed 后将 `Annotated` 接收者降为 Unknown；保留 site-packages 和宿主源码路径。
直接及跨模块 `Annotated` 参数的方法继续由 ty 原生定位，类型标注后备仅用于实际未解析的调用。

定义读取优先取完整语句。外层控制结构以 `enclosing_contexts` 提供带位置的原始头部线索及展开句柄，头部本身
不可引用，须打开后取得 Evidence；不默认载入整个外层块。无法识别语法范围时，兜底窗口前后合计最多 300 行，
可沿 `adjacent_windows` 向前、向后读取；`offset` 相对此次句柄范围开头。所有展开继续核对批准根与源码摘要，
依赖源码也能使用这条受控上下文读取路径。片段读完、未预载某个定义都不代表语义完整或没有使用限制。

Python handler 的函数形参通常描述 NoneBot 如何注入运行上下文，不等同于用户要输入的命令参数。例如
`Bot`、`Event`、`Matcher`、`T_State`、`UniMessage` 和 `MsgTarget` 只让 handler 取得当前事件、消息或目标。
`CommandArg()`、`ShellCommandArgs()`、正则组等 parameterless 依赖可以证明 handler 消费了哪一类输入，
但除非它们背后有 Alconna、`argparse` 等结构化解析器，否则仍不能单靠函数签名还原允许的选项和组合。

用户可见语法的证据按下面的边界提取：

- Alconna 的 Args、Option 和 Subcommand 可以从结构化命令对象读取；
- 普通 `on_command` 的运行时对象通常只能可靠提供命令字、别名和空白规则；
- handler 若再对命令余项执行 `split()`、正则、手写循环或状态机，精确语法必须沿实际解析代码、配置、
  测试和插件自带帮助交叉提取，不能把 handler 的依赖注入形参当成用法；
- 多轮等待、Reply、图片段等输入前提同样属于 handler 行为，而不是普通函数签名能够表达的参数表。

Alconna 教学用法读取根命令、Subcommand、Option 和 Arg 各自已经生效的 `separators`。环境变量解析与
全局 / 局部覆盖由 NoneBot 和 Alconna 完成；Triage 不重新读取环境或推导优先级。代码按实际消费边界生成
匿名 canonical 模板，模型依据证据命名槽位并保留固定标点。例如根使用逗号而 Arg 保持空格时生成
`probe,<slot:0> <slot:1>`；根和 Arg 都使用逗号时生成 `probe,<slot:0>,<slot:1>`。
可选参数的分隔符随参数一起省略，例如 `probe,<slot:0>[,<slot:1>]`；Help / Answer 保留这一结构。

公开模板在各边界优先选择普通空格，否则从有界安全可见字符中稳定选择一个；原始分隔事实仍完整进入
Evidence、family shape 和缓存指纹。仅有控制字符或用法元字符、缺失分隔事实、
超出下述支持范围的 `requires`，以及无法一致省略的可选参数 / Option 边界，均在准备阶段记为 `unsupported_syntax`。
这会关闭对应 teaching unit；family 不删除失败成员后继续发布，同插件的其他独立单元仍可工作。
分隔规则变化后重新生成失败也不会恢复旧用法。显式 Provider 没有声明 usage 时，共享确定性渲染边界。
本轮语义核验针对 `arclet-alconna 1.8.44` / `nonebot-plugin-alconna 0.62.1`；不覆盖全部 Alconna 配置、
动态 Extension 或具体参数值的任意引用 / 转义。原生 parser 回归只使用无业务回调的合成命令。

祖先节点的普通位置参数与子命令共存时，模板沿声明路径保留各层参数和 Option，槽位连续编号；
例如 `probe <slot:0> child <slot:1>`，不会把参数删除后伪装成连续命令头 `probe child`。
这类调用以根命令作为固定锚点，子命令及其别名保留在模板中。普通根 Matcher 同时保留父参数入口；
已有 dispatch 范围的 Matcher 不据此新增父入口。祖先变长参数，以及可选祖先参数省略前后无法保持同一
分隔边界的组合仍停止生成。

普通路径存在型 dispatch 可定位声明中的位置参数、Option、Subcommand 及其嵌套参数；参数定位复用
Alconna 的 `extract_arg`，不执行解析或业务回调。分派目标没有默认值时，相关参数 / Option 在该分支
模板中必须出现；已有默认值（包括 `False`）不被当作缺失。内置 Help / Completion / Shortcut 节点沿用
现有过滤规则，避免与业务节点的 dest 重名造成误判。`additional` 回调进入既有 Rule 证据链，不能当作
纯路由忽略，采集阶段不执行回调。

未指定 value 的 `or_not=True` 保留主入口（没有 Option / 子命令）与目标路径的并集，两者属于同一个
子 Matcher 教学目标；快照另存主入口参数，避免目标路径提升必填性后污染主入口。渲染结果精确共享
完整主入口时，用整段可省略的模板替换已覆盖的展开形式，如 `probe [help]`，不重复追加模板，
也不放宽原有结构校验。分派到中间子命令时，同时保留该节点自身入口与后续路径；按声明路径逐层合并，
例如普通分派为 `probe group [a|b]`，带主入口并集时为 `probe [group [a|b]]`。
未命中的上级节点不会因合并而新增入口；无法安全合并的形式仍受现有模板数量和参数结构限制。
父子 Matcher 仍独立分析，不因入口重叠合并业务，也不默认互斥。仅有框架内部传递 Handler 的根 Matcher
没有本插件业务 Handler 引用，沿用现有资格规则排除；存在业务 Handler 时根据自身 Evidence 分析。
当前不建立父级执行链或全局可达性分析，不承诺识别任意提前结束 / 阻断传播的交互。

值比较（包括带 value 的 `or_not`）、节点默认结果、被分派目标为重复 Option、隐藏或变长参数以及无法唯一定位的动态路径目前不做
完整投影。这些已知的不支持保留在记录的 `command.projection_issue`，同时标记证据不足并排除教学
生成；不会使整个快照成为 partial。真正的采集异常仍保持全局错误，不通过宽泛捕获隐藏故障。
request v86 使旧请求重新验证；Prompt v115 / 公开 Schema v13 不变。这不是完整 Alconna 查询解析器。

上述三个 dispatch 支持缺口的具体含义：

- `value` 比较约束的是解析后的值，不能仅凭值为字符串就当作固定输入替换槽位；带 value 的 `or_not`
  接受值相等或查询路径不存在，不等同于无 value 时的主入口并集。
- Option / Subcommand 的节点默认结果可能在用户未输入该节点时仍形成查询结果；不能用“路径存在”
  推导该节点必须显式输入。这不等于普通参数默认值都不支持。
- 分派到追加或计数型重复 Option，需要同时保留目标必须出现与整组选项可重复的结构；当前明确拒绝
  该组合，不代表所有普通重复 Option 都不支持。

合并后的模板仍受每条 160 字符、最多四条等既有合同约束；分支多而不能在边界内无损表达时仍可能停止
生成，不能把模板去重修复理解为任意命令树都能合并。这些是当前支持限制，不是插件注册错误。

顶层 Option / Subcommand 的常规 `requires` 已支持；多词节点名由 Alconna 拆出的前置词同样处理。
快照按运行时原顺序保存，不排序或去重；固定前置词由代码加入 canonical 模板和子命令完整路径，
别名只替换节点名。Option 前置词包含在同一个可选组中，如 `probe [manage group (enable|on)]`，
省略 Option 时不会遗留前置词。顶层子命令之后仍可有不带 requires 的普通子命令。

本次边界限于：根与该节点选择同一种安全分隔字符（覆盖普通空格与统一逗号等），根及该节点不启用
compact；前置词为非空的字母 / 数字（含中文）、连字符或下划线组合，不能以连字符开头，也不能包含
根或节点的分隔字符。重复词、与同层节点名称 / 别名冲突、同层节点入口重名、嵌套 requires，以及需要
把该 Option 列表或节点别名压缩成概念槽位的情况继续拒绝。原生 1.8.44 的合成测试已观察到嵌套前置词
按直觉拼接也可能不匹配，因此不扩大承诺。已有祖先位置参数等限制保持不变。
这次只补快照和确定性投影，request 升为 v73；Prompt v110 和公开 Schema v12 不变。

关键字参数当前采取识别后停止生成的边界，暂不实现完整教学支持。快照用内部 `keyword` 标记保留
`KeyWordVar` 及其 `MultiVar` 包装的语法身份，覆盖固定键、可变键值对和函数签名生成的布尔关键字参数，
不因值类型同为 `str` / `bool` 就把它当位置参数。根、Option、Subcommand 任一参数命中时，准备阶段
以 `unsupported_syntax` 停止整个对应教学单元；family 不删除该成员后继续发布。确定性兜底用法与
未声明显式 usage 的 Provider 路径同样拒绝生成猜测模板。

旧 Alconna 参数事实缺少该标记时要求重新采集，不能默认成普通参数；request v74 使旧教学缓存重新验证。
受影响单元不会恢复过时用法，同插件其他独立单元仍可工作。此处只补识别与拦截，未支持 `key=value`
模板、任意键名、布尔简写或回复替参，也不新增公开字段或 Prompt 规则。后续有具体插件需求时，再按
其结构确定支持范围；不以实现整个关键字参数系统为当前目标。

### 配置纳入教学的判断标准

`AntiPattern` 只作类型事实纠正与按需概念说明：快照保留 `nepattern.base.AntiPattern` 身份，
不再把继承的 `origin` 投影为允许输入类型；已知 MultiVar 包装和 Union 成员沿用此识别，Union 中
正向成员仍保留原类型。确定性兜底使用中性槽位，聚合类型检查不强制补入被反转的基础类型。
仅当前 runtime 参数结构或 family shapes 的 pattern_type 出现此身份时，自动加入已核对
`nepattern 0.7.8` 的框架概念 Evidence；源码导入、普通文字或仅安装 Alconna 不触发。
该身份不是完整基础规则，具体槽位名及必要的输入限制由模型结合当前源码证据判断；不翻译任意模式、
不执行回调，也不扩展原有语法支持范围。request v78 隔离旧类型事实缓存，Prompt v111 / Schema v12 不变。

无参数且动作等于原生 `count`（整数值 1）的 Option 由快照保存 `repeatable`，在能够独立展示的
完整 Option 可选组后生成 `...`，例如 `probe [--verbose|-v]...`；逗号边界保留为
`probe[,--verbose|-v]...`。标记只表达重复输入，计数的业务用途仍由 Handler 证据解释。
带参数 COUNT、自定义动作值、压缩的 Option 列表或别名概念槽位不新增重复标记；不扩展连续简写。
request v76 使旧教学缓存重新验证，Prompt v110 / Schema v12 不变，不新增框架释义或模型工具。

request v77 将同一重复标记扩展到带普通参数的原生 `append` Option，例如
`probe [(--tag|-t) <标签>]...`。重复的是整个选项组，不是同一次选项后的参数槽位；
原生解析器会按参数名累积各次输入，模型依据 Handler 解释这些值的用途。
无参数 append、自定义动作值、含隐藏 / MultiVar / 关键字参数的组合不新增组后标记，
压缩 Option 列表及别名概念槽位维持原展示。复用现有结构校验与快照指纹，不新增公开字段。

完整的字段归属与剩余范围见 [Alconna 字段与教学职责核对](alconna-teaching-field-audit.md)。
已有模板、固定说明与 Evidence 负责的语义不重复增加字段释义；剩余字段也不统一纳入。
少量必要的简单事实可复用现有 framework_semantics Evidence，复杂语法与回调仍需各自的支持边界。

普通位置 `MultiVar` 的有限数量上限由代码保留：快照读取生效 `variadic_length`，-1 表示已确认无限，
正整数为上限；缺失或非法值不猜成无限。模板生成的同一次槽位分配绑定 entry 与 slot 上限，核心复用标准
usage 的唯一对齐结果取得公开参数名称，在派生教学视图中补充“每次显式填写该参数时最多提供 N 项”。
同名槽位按在用法中的出现顺序区分；说明只约束显式输入，不限制默认值集合，也不重复 usage 的必填性。

根、Subcommand 和 Option 的明确标准槽位使用同一路径；有限隐藏参数、被压缩的 Option 列表，以及当前
无法保留具体槽位的 aggregate family 均以 unsupported syntax 停止相应单元。无限形式不额外生成说明。
request v75 纳入槽位上限与快照事实，Prompt v110 / Schema v12 不变。固定数量说明在新生成、缓存复用和
合法 fallback 时统一重建；模型原始注释仍用于 pending / last-good 与编辑基线，不保存代码生成的说明。
get / get_pending 返回经过字段校验的完整派生视图，Answer Markdown 展示这些 behavior_boundaries，
Help 继续维持原有简表投影。上限变化后旧指纹结果不能恢复为当前知识，也不会把旧代码说明带进模型基线。

2026-09-10 确认：不以覆盖 Alconna 配置表为目标。为配置新增专门的运行时投影或模型理解规则之前，
必须明确它能够改变哪一项用户可观察的教学输出，并有证据支持这种变化。固定输入字符落到 `usages`；
实际使用限制落到 `behavior_boundaries`；权限、场景等使用条件落到相应 `requirements`。
底层机制复杂本身不构成纳入理由，也不为解释配置内部机制新增公开字段。

`ALCONNA_USE_ORIGIN` / `use_origin` 当前不新增专门投影或 Prompt 规则。其布尔值只表达消息来源选择，
单独不足以确定是否必须 mention、能否用回复替代参数，或应该改写哪条 usage；实际结果还依赖适配器、
消息提供与改写扩展等。只有具体插件已出现相关教学错误或存在明确行为证据时，才通过现有证据链解释
用户可观察的后果，并修正对应用法或边界。这不禁止源码证据自然包含该字段，也不把可能性写成已知限制。
分隔符已经有明确的固定字符输出位置，因此与上述未建立输出映射的配置分开处理。

NoneBot `SUPERUSER` 只决定当前事件是否可以读取维护者可见的能力证据。它不会改变 Python 反射结果，也
不会让运行时快照自动取得 handler 内手写的参数语法。部署侧离线分析器能读取已安装源码，是因为它受到
单独的本机路径与数据策略授权，不是因为群聊用户通过了 `SUPERUSER`。

## 调研后的复用分级

以下结论核对于 2026-08-12。第三方项目版本和许可证可能变化，真正接入前仍需重新确认。

| 来源 | 可以利用什么 | 接入方式 | 明确不做什么 |
|---|---|---|---|
| NoneBot / Alconna 运行时 | 已加载插件、`Plugin.matcher`、命令结构、disabled、shortcut、命令与 Matcher 关联等事实 | 第一阶段直接读取公共接口；内部 checker 结构由版本适配器隔离；后续再派生用户可观察能力 | 不执行 Rule、Permission、handler、parser 或 executor；不把 Matcher 直接当成 Capability |
| [PicMenu Next](https://github.com/lgc-NB2Dev/nonebot-plugin-picmenu-next) | 已整理的插件说明、旧 PicMenu `menu_data`、结构化 overlay 合并思路 | 以后可选地消费“已经加载并初始化”的只读快照，逐字段复制到 Triage 模型 | 不主动导入插件，不调用 `refresh_infos()`、formatter、mixin、模板或渲染器 |
| [TreeHelp](https://github.com/he0119/nonebot-plugin-treehelp) | 从 `Plugin.matcher` 与已知 checker 识别普通命令的思路 | 参考算法后按当前 NoneBot 版本重写 | 不复制永久缓存和依赖内部对象形状的原实现 |
| [nonebot_plugin_help_baize](https://github.com/sangonomiya249/nonebot_plugin_help_baize) | AST 字面量提取、来源位置和搜索文本设计 | 以后作为低置信静态证据源 | 不把正则结果当语法真值，不改写已安装插件源码 |
| PicMenu `menu_data`、结构化 YAML / JSON / TOML、部署者帮助图数据 | 人工整理的公开意图、名称、说明、示例 | 通用结构化文件 profile；部署私有 schema 留在部署层 adapter | 不要求固定文件路径，不假设所有部署者都有 overlay，不用文件存在证明插件已加载或当前可执行 |

当前没有必要把第三方项目的 Python 代码复制进核心。NoneBot、Alconna 和上述可参考项目大多允许按其许可
复用，但协议重写更容易保留 Triage 的来源模型与安全边界。AGPL / GPL 实现、未声明许可证的私有实现或
素材不复制进 MIT 核心；会动态执行 Python / Jinja / JavaScript 模板、第三方回调或全局预处理器的路径也
不接入。

产品仍以 NoneBot 本轮成功注册的 runtime snapshot 作为“当前可用”真相，不用静态扫描替代它。插件启动
后台任务会从其中明确公开、平台已知、无分析问题且有观察到 `invocation.header` 的记录先进入教学分析。
这个调用锚点可以是命令头，也可以是 `on_startswith / on_endswith / on_fullmatch / on_keyword` 的可直接
发送字面量；正则、事件类型与没有确定触发形式的被动监听不进入教学。教学 Agent 会接收 runtime 命令结构、ast-grep
Matcher / 工厂结构、已加载 handler 和内存配置投影组成的确定性 Evidence Pack；仅当首包不足时，才可通过
共享只读 FileSystem 在批准根内 glob/search/read、由定义导航从已读 Python 标识符转到当前环境依赖定义，或
查询当前版本对应的 NoneBot 公开文档索引。源码只补充已注册记录：加载失败、未观察到或只在静态制品中
存在的插件不会进入普通用户帮助。单个 teaching unit 失败只关闭没有当前可信结果的对应单元，其他成功或
精确命中缓存的单元可以进入显式标记覆盖量的 partial generation；共享 snapshot 或发布可信度失败时才保留
上一活动 generation，基础索引始终可用。

文档工具可用时，其附带指引按缺失事实选择证据：框架 API 的一般含义优先查版本匹配文档，查询包含
具体 API 和待确认的问题；当前插件实际行为依据插件源码与 Runtime。文档未命中、未覆盖影响教学的
细节或与源码存在疑问时，核对适用版本并按需导航当前安装框架定义。已有证据足够时不再检索，不要求
两种来源各读一次；取得足够证据或确认无法唯一判断后停止，证据不足的事实保持 unresolved。

family 单元保留选择性源码定义导航，并在知识包可用时提供同一个版本绑定文档检索工具；不开放普通
单元的通用文件遍历工具。维护评测的 `knowledge=required` 对普通与 family 单元采用同一首请求合同：
必须提供文档检索工具，但不强制调用。后续仍可按既有工具预算撤下工具；request v106 隔离此前
family 缺少文档工具的请求与缓存。

维护评测的 `--all` 把宿主声明的插件作为一个批量范围，排除 Triage 自身及仅随依赖加载的非目标插件。
一次刷新共用完整宿主快照和现有并发准备、分析流水线，在同一轮发布与缓存提交中替换目标插件，保留
非目标插件的已发布内容；没有教学单元的目标插件从结果中识别并记录为跳过，不逐插件重建宿主快照。
启动后台仍执行默认全量、非强制刷新；单插件维护入口保持原有范围。

已有 NoneBot 参数类型框架 Evidence 同时说明执行顺序：Handler 先递归预检查依赖及自身的参数类型，
通过后才求解依赖、调用函数。标准 `.got()` 的取参与提示在依赖求解阶段执行，不能被理解成独立于
该 Handler 类型限制的前置提示；其他独立 Handler 仍分别判断。该说明核对 NoneBot 2.5.0 的实际实现，
按已有相关类型注解规则加入请求，不新增全局 Prompt 或场景推断校验器；request v80 隔离旧请求。

插件源码中的 Matcher 注册、handler 装饰器、配置引用及 Rule / Permission / 限流候选由项目内固定、只读的
ast-grep 规则提取；部署配置和模型都不能提交规则，也不开放 fix 或 rewrite。它只提供静态语法位置和候选
关系，仍由 runtime snapshot 决定能力本轮是否存在，由 Triage 负责路径、预算、revision、Evidence 和
partial / opaque 边界。官方直接 `on_*` 入口均可形成源码锚点；`CommandGroup` / `MatcherGroup` 方法只有
在构造来源和接收者绑定可证明时才识别，普通业务对象的同名方法不会命中。

宿主安装 Uninfo 时，静态首包还会临时解析 Permission 表达式的 import 绑定，把已确认来自 NoneBot 的
`SUPERUSER`，以及来自 Uninfo 的 `MEMBER / ADMIN / OWNER` 与 `PRIVATE / GROUP / GUILD`，投影为同一个
`permission` requirement 的 OR alternatives；场景使用 `private / group / guild / channel_text /
channel_category / channel_voice` 原子元数据，`GUILD` 确定性展开为频道与三种 channel 分支。
Uninfo 0.11.1 的 `ADMIN()` 精确展开为 `admin OR owner`；`CHANNEL_ADMINISTRATOR` 只有在实际角色 ID 或
`ROLE_IN(...)` 字面集合提供 Evidence 时才映射为 `channel_admin`，不会根据 `role.level` 或 Adapter 猜测补齐。
同一表达式仍有未知自定义分支时，已知分支不会被单独发布成 fixed AND。
最终索引不保存 import 来源；同名本地符号不会套用该语义。模型被要求直接使用这些稳定事实，不再为每个
插件重复打开 Uninfo 源码；实际安装版本既不作为启用门，也不单独触发教学注释失效。高级动态 Permission
继续保持 opaque，必要时才走定义导航 / 文件补读。当前映射已用 Uninfo 0.11.1 的已安装源码复核。

源码切片中的 Handler 或 helper 实际使用 `Uninfo` / `QryItrface` 类型注解时，首包还会加入一份可引用的
框架语义 Evidence。它覆盖 README 中与源码理解相关的 Session、User、Scene、Member、查询接口与内建
Permission 公共含义，并以 0.11.1 源码补足 README 未展开的 `Session.scene_path` / `Session.id` 计算边界。
其中 `Session.self_id` 是机器人 ID、`Session.user.id` 是触发用户 ID，群聊 `scene_path` 通常按群场景共享。
这些事实只解释框架字段，不替插件推断最终存储、限流或开关作用域；模型仍须结合值是否实际流入执行键的
数据流形成公开结论。

`NBTRIAGE_RESTRICTED_CONFIG` 已实现为顶层 NoneBot 配置键的 JSON deny-list，运行时持有的
`ConfigValuePolicy` 在任何值读取前按大小写不敏感顶层键判定，`__` 嵌套键按顶层整项限制。投影器只读
已经存在、类型与源码 revision 均匹配的 Pydantic 配置实例，并拒绝 restricted、缺失、Secret、嵌套模型、
自定义对象和超限值；不会调用 `get_plugin_config()`、validator、`model_dump()` 或任意属性逻辑。第一版不
追踪绕过标准 Config 链的 `os.getenv()` 等读取；拿不到安全的有效值时保留 unknown。教学注释没有独立开关；
配置了合格模型 transport 时建立该专用后台任务，未配置或模型运行配置不可用时跳过模型增强并保留确定性索引。
生成后直接采用校验通过的注释，不要求人工审核。普通 Handler 按能力分析；带闭包的参数化 Handler 只有在
能唯一定位外层工厂、且同一工厂没有未准入成员时才聚合分析一次。静态层不生成成员列表或共同语义摘要；
Agent 无法形成可靠共同说明时输出 `knowledge_enabled=false`。公开查询会把命中的注释作为
事实交给无工具 Answer Agent 结合当前问题组织回答，只有 Answer 失败时才直接使用确定性注释模板；同一注释
还会投影成独立的展示 YAML 和从结构化公开字段确定性渲染的 Answer Markdown。模型不再生成自由 Markdown。
当前公开 entry 只保存 name、summary、usages、search terms、behavior boundaries 及 requirements；独立
`role / scene / access / rate_limit` 继续表达各自条件，Permission 的组合资格使用带 `role / scene / access`
alternatives 的单一 requirement。LocalStore cache 只保存公开
文本、请求指纹，以及动态 `read_file` Evidence 的 ID、相对位置和 revision 清单；它不保存源码正文或配置
值。缓存按插件写入 `capability-annotations/<module_name>.json`，同一文件内按 teaching unit 分别保留
`last_good`、`pending` 与 `last_attempt`。`last_good` 只表示已由当前 generation 发布的完整结果；通过同一套
公开投影校验但尚未发布的候选在单元完成后原子 checkpoint 为 `pending`，重启后也只有 revision、请求
fingerprint 与动态 Evidence manifest 全部仍匹配时才能免调用复用。`last_attempt` 只记录最近真实尝试，失败
不会抹掉仍匹配的 `last_good` 或更早的 `pending`。分片的 `published_generation` 还必须与当前输出指针一致，
否则 `last_good` 只能作为编辑基线；cache shard 可以保存未发布候选，但不拥有发布指针，只有
`current.json` 选中的 generation 才是活动教学合同。教学文件策略硬拒绝 `.env*`、凭据与数据库，并额外拒绝日志、Migut Help 人工 YAML、评测
Gold 和本任务生成的 help-display，避免秘密外发与评价数据泄漏。

一次能力分析可以包含多个由模型外固定 ID 的公开 entry：普通命令通常只有一项，确定性的 Alconna 叶子
子命令分别成为独立项，同一功能的 Option、别名、回复输入和参数变体仍保留在该项的有序 `usages` 中。模型
直接输出完整命令正文，不再使用 `{command}`；anchored entry 必须包含 runtime / parser 给定的正文，
参数化工厂请求会携带全部当前公开成员，但不再逐成员复制通用 claims 和相同 Parser AST：紧凑成员清单保留
anchored 命令、alias 与语法可信度，只有 Runtime adapter 能证明完整的参数结构才按 `shape_id` 去重保存。
固定头 Alconna 使用 `parser_exact`；模式头成员使用 `parser_with_pattern_header`，保留匹配事实、别名和
后续 Parser shape，不把完整公开用法标为可精确对齐。普通 `on_command` 保持 `anchor_only`，不能把缺少
结构化参数误读为没有参数。
`parser_exact` 只冻结参数顺序、必选性、重复性、Option 和别名；内部 `Arg.name` 会先匿名化为 `slot:N`，
公开槽位名由模型依据 notice、声明 usage 与源码 Evidence 生成，模型外再按匿名模板校验结构。
教学记法将槽位与可选性分开：`确认` 是必填固定文字，`<用户名>` 是必填非字面量槽位，`[确认]` 是可选
固定文字，`[<用户名>]` 是可选槽位。`()` 用于分组，`|` 表示备选；`<图片|文字>` 列举同一槽位接受的输入
形式，`(开启|关闭)` 列举固定文字。`<参数>...` 表示至少一项，`[<参数>]...` 表示零项或多项；依赖前一参数
的可选参数使用 `[<参数甲> [<参数乙>]]`。括号与重复号是教学记号，不作为实际输入。
Parser 可选匿名槽位统一生成为 `[<slot:N>]`；非空格分隔符属于可选输入整体，例如 `[,<slot:N>]` 与
`[,<slot:N>...]`。模型只能替换尖括号内的槽位名称；方括号里的固定文字与 Option 不参与槽位命名或回复省略。
这避免旧 `[图片]` 同时表示固定文字与概念参数的歧义。通用校验检查括号嵌套与重复号，模板校验负责结构
对齐；没有 Parser 模板时，固定文字与概念名称的语义正确性仍依赖 Evidence，不能只靠中文名称判断。
Parser 模板约束解析后的输入，不能单独证明用户必须将全部参数直接附在命令消息中。有标准模板的入口仍须完整保留；
有当前注册及处理实现 Evidence 支持时，可增加独立的前置回复形式：`<回复消息>` 为必需回复，`[<回复消息>]`
为可选回复。必需回复可替代能够唯一对齐的普通参数槽位，可选回复只能省略可选槽位；Option/分支内部参数、
其他保留槽位的顺序、必选性和重复性仍受校验。程序只验证结构对应，回复实际提供哪些内容及数量由模型依据
Evidence 判断，不按扩展名称或公开槽位名称猜测。不确定的回复变体应省略，保留标准用法；不新增 extension
专属解析器或公开输入映射字段。
模型只生成一条共同 family 注释，成员参数数量、图片、文字或 `@用户` 输入、必选性和精确 usage 不同不会因此关闭 family。
family 保留一条标准聚合用法，可增加同一 entry 的必要回复变体，仍受现有 usage 总上限约束；不是逐成员列举。
标准聚合负责完整直接输入类别覆盖，前置回复标记不计入参数槽位，额外回复变体不重复承担输入并集覆盖。
Alconna 联合输入的限定类型完整进入 Parser shape；`At` 是直接输入形式，不能因为后续被转换成头像图片而从
聚合 usage 省略。
若 Handler 访问静态工厂成员的 Callable 字段，且字段值能唯一解析到目标插件本地函数，这些函数按定义去重并
有界加入首包，不递归展开，也不为成员分别运行 Agent。同一位置的一至三项固定
备选在 usage 显式枚举，四至六项使用由 Evidence 命名的概念槽并在 summary 说明，七项及以上使用概念槽，允许简单概括共同类别但不逐项解释；
Prompt 不提供固定的聚合槽位成品词；该规则同时
适用 family 成员与单个 Matcher 的命令头、别名、Option 和固定参数。查询层按 family 去重，精确命中成员时从当前
Runtime record 的 `command.arguments / components` 重建完整 usage，不持久化成员目录或引入插件专属 schema。请求内
manifest 只用于一次完整 family 语义判断，不是新的 serving catalog。插件把
Migut Help 最小字段 YAML 与结构化渲染的 Answer Markdown 写入 LocalStore
`capability-teaching/objects/<generation>/{help-display,answer-knowledge}/`，manifest 记录 teaching unit
状态与每个插件的 `active / eligible` 覆盖量，并只用一个原子 `current.json` 切换两类输出和对应 Answer
内存视图。指针切换前的候选不是 active teaching contract；`SOURCE_CHANGED` 会回滚该插件本轮 checkpoint
并作废整份内存 staging，但不会阻止其他插件发布。插件源码 revision 变化时首版仍全量重生成该插件，不尝试逐 unit hash 复用。
Migut Help 的 description 只投影 summary，用于简短功能说明和必要的参数含义。只有整个 Permission 恰好是
单一 `SUPERUSER`，或 `admin OR owner` 管理员组合时，才投影原生 `permission`；是否存在冷却继续投影
`has_cd`。`channel_admin / MEMBER`、其他混合 OR、scene、access、behavior boundaries 和具体限流文字留给
Answer，不拼入 Help description，也不把复合资格误压成全局 admin。
缓存和输出文件都只接受能够直接安全落盘的 module name；不使用 hash fallback 或 module 到文件名 manifest，
非法名称只关闭对应插件。目录与 Migut Help 配置相互独立，当前没有导入或监听接线；生成文件用于观察首版
效果，SUPERUSER 可用 `triage 刷新帮助 [plugin_module]` 主动重生成。

## 后续来源接口

通用 `HelpPluginSource` 仍是待实现的适配层，不是对第三方插件的强制注册协议。它应满足这些约束：

- 由部署配置或 entry point 显式启用，默认关闭；
- 探测只检查已加载插件、版本和显式配置路径，不主动 import / `require()` 目标插件；
- 输出 Triage 自有的 Claim、Evidence 和 Constraint，不把第三方模型对象交给核心；
- 结构化文件使用安全解析器，并限制根目录、文件大小、数量、嵌套深度和符号链接；
- 只接受允许的字符串、数字、布尔、列表和字典字段，不接受 callable、模板或自定义可执行对象；
- 某来源失败只让该来源成为 partial，不把缺失结果解释为“没有能力”。

建议的可选适配顺序是：PicMenu Metadata `extra.menu_data`、通用结构化帮助文件、已加载 PicMenu Next 的
只读快照，最后才是部署私有 adapter。Migut 的帮助 YAML 属于最后一类；它可以提供维护者整理过的披露和
用法证据，但不是公开插件的默认路径，也不能取代运行时、源码、配置和当前上下文判断。

## 与执行资格的关系

帮助数据只参与能力发现和用法说明。即使某字段由人工检查过，也不能证明当前用户、群、adapter、配置和
限流状态允许执行。模型外策略必须先为当前 adapter 与受众建立独立检索域：普通用户域从源头排除带
blocking `analysis_issues`、restricted 和其他 adapter 能力；维护者域只有在模型外鉴权后才可读取受控受限
记录，且 restricted 源码不因
SUPERUSER 身份自动进入 LLM。真正执行仍由原插件自己的 Matcher、Permission、Rule 和 handler 决定。
能力发现、字段说明和最终执行是三种判断问题，不要求在持久化模型中增加 `discover / teach / execute` 三个
布尔字段。

回答层不为这些来源规定固定句式。来源只产生可验证的事实：公开能力若有可靠证据，可以说明必要输入、
群聊 / 私聊条件、公开角色要求和限流的作用域、额度、窗口或重置方式；只有底层库名、配置字段或源码位置
时，不能直接把实现细节投影给普通用户。整项 hidden / SUPERUSER-only / restricted 能力仍在模型前移除，
不能因为发现了“管理员权限”字样就对普通用户说明该能力存在。

## 相关资料

- [部署本地能力影子索引](flows/capability-shadow-index.md)
- [Alconna 公开能力与解析回执](flows/alconna-capability-and-parse-receipts.md)
- [ADR-0021：用部署本地影子索引整理 Bot 能力证据](../adr/0021-use-deployment-local-capability-shadow-index.md)
- [ADR-0024：自动公开确定且低风险的能力字段](../adr/0024-auto-publish-deterministic-capability-fields.md)
- [ADR-0026：在检索与模型前隔离能力知识受众域](../adr/0026-filter-capability-knowledge-before-retrieval.md)
- [ADR-0029：由部署者 deny-list 控制相关配置值进入模型](../adr/0029-control-model-config-values-with-deployment-deny-list.md)
- [ADR-0032：分离能力受众、平台范围与分析问题](../adr/0032-separate-capability-audience-analysis-and-platform-status.md)
- [ADR-0058：用确定性证据与有界源码导航生成教学注释](../adr/0058-use-deterministic-evidence-and-bounded-navigation-for-teaching-annotations.md)
- [ADR-0059：跨 Agent 链路共享只读证据访问工具](../adr/0059-share-read-only-evidence-access-across-agent-flows.md)
- [ADR-0066：用当前公开教学合同前置筛查普通用户 Bug](../adr/0066-use-active-teaching-contract-as-bug-precheck.md)
- [ADR-0080：把一次能力分析投影为多个公开教学条目](../adr/0080-model-capability-teaching-as-multiple-public-entries.md)
- [ADR-0093：按插件分片教学注释缓存并按单元部分发布](../adr/0093-shard-capability-annotation-cache-by-plugin.md)
- [ADR-0094：收敛公开能力教学合同](../adr/0094-simplify-the-public-capability-teaching-contract.md)
- [ADR-0113：分离路由、授权与业务准备状态](../adr/0113-separate-routing-authorization-and-business-readiness-in-teaching.md)
- [ADR-0121：在原子发布前 checkpoint 已完成教学单元](../adr/0121-checkpoint-completed-teaching-units-before-atomic-publication.md)
