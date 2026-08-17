# 可选帮助数据源与复用边界

这份说明记录 Triage 如何看待 NoneBot 生态中的帮助菜单、帮助图和命令自动发现插件。结论不是“选一个
帮助插件当真值”，而是把它们当成不同质量的证据来源，再由部署本地影子索引统一标注来源、时效和限制。

## 当前实现

第一阶段已经读取标准 `pyproject.toml` 声明、安装制品 revision、NoneBot 的已加载 Plugin / Matcher 和
Alconna 命令管理器，生成部署本地快照与 FTS5 索引。它不调用 Matcher 的 Rule、Permission、handler 或
Alconna `parse()`，也尚未接入任何第三方帮助插件。

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
共享只读 FileSystem 在批准根内 glob/search/read、由 Jedi 从已读 Python 标识符转到当前环境依赖定义，或
查询当前版本对应的 NoneBot 公开文档索引。源码只补充已注册记录：加载失败、未观察到或只在静态制品中
存在的插件不会进入普通用户帮助；一轮中任一分析单元失败都不会激活半套新教学视图，基础索引仍可用。

插件源码中的 Matcher 注册、handler 装饰器、配置引用及 Rule / Permission / 限流候选由项目内固定、只读的
ast-grep 规则提取；部署配置和模型都不能提交规则，也不开放 fix 或 rewrite。它只提供静态语法位置和候选
关系，仍由 runtime snapshot 决定能力本轮是否存在，由 Triage 负责路径、预算、revision、Evidence 和
partial / opaque 边界。官方直接 `on_*` 入口均可形成源码锚点；`CommandGroup` / `MatcherGroup` 方法只有
在构造来源和接收者绑定可证明时才识别，普通业务对象的同名方法不会命中。

宿主安装 Uninfo 时，静态首包还会临时解析 Permission 表达式的 import 绑定，把已
确认来自 Uninfo 的 `MEMBER / ADMIN / OWNER` 与 `PRIVATE / GROUP / GUILD` 直接投影为角色 / 场景约束。
最终索引不保存 import 来源；同名本地符号不会套用该语义。模型被要求直接使用这些稳定事实，不再为每个
插件重复打开 Uninfo 源码；实际安装版本既不作为启用门，也不单独触发教学注释失效。高级动态 Permission
继续保持 opaque，必要时才走 Jedi / 文件补读。当前映射已用 nonemigut 的 0.11.1 源码复核。

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
还会投影成独立的展示 YAML 和 Answer Markdown。LocalStore cache 只保存公开
文本、请求指纹，以及动态 `read_file` Evidence 的 ID、相对位置和 revision 清单；它不保存源码正文或配置
值。教学文件策略硬拒绝 `.env*`、凭据与数据库，并额外拒绝日志、Migut Help 人工 YAML、评测 Gold 和本任务
生成的 help-display，避免秘密外发与评价数据泄漏。

一次能力分析可以包含多个由模型外固定 ID 的公开 entry：普通命令通常只有一项，确定性的 Alconna 叶子
子命令分别成为独立项，同一功能的 Option、别名、回复输入和参数变体仍保留在该项的有序 `usages` 中。模型
直接输出完整命令正文，不再使用 `{command}`；普通 entry 必须包含 runtime / parser 给定的 anchored 正文，
参数化工厂则由模型基于 Evidence 给出一种完整、确定正确的聚合调用形式。插件把 Migut Help 最小字段 YAML
与 Answer Markdown 写入 LocalStore
`capability-teaching/objects/<generation>/{help-display,answer-knowledge}/`，并只用一个原子
`current.json` 切换两类输出。目录与 Migut Help 配置相互独立，当前没有导入或监听接线；生成文件用于观察
首版效果，SUPERUSER 可用 `triage 刷新帮助 [plugin_module]` 主动重生成。

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
- [ADR-0027：用事实输出合同约束能力帮助](../adr/0027-constrain-guidance-with-facts-not-fixed-wording.md)
- [ADR-0029：由部署者 deny-list 控制相关配置值进入模型](../adr/0029-control-model-config-values-with-deployment-deny-list.md)
- [ADR-0032：分离能力受众、平台范围与分析问题](../adr/0032-separate-capability-audience-analysis-and-platform-status.md)
- [ADR-0034：区分 Matcher 事实与用户可观察能力](../adr/0034-distinguish-matchers-from-user-observable-capabilities.md)
- [ADR-0058：用确定性证据与有界源码导航生成教学注释](../adr/0058-use-deterministic-evidence-and-bounded-navigation-for-teaching-annotations.md)
- [ADR-0059：跨 Agent 链路共享只读证据访问工具](../adr/0059-share-read-only-evidence-access-across-agent-flows.md)
- [ADR-0080：把一次能力分析投影为多个公开教学条目](../adr/0080-model-capability-teaching-as-multiple-public-entries.md)
