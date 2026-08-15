# ADR-0058：用确定性证据与有界源码导航生成教学注释

> 后续关系：模型 transport 不合格时拒绝插件启动的策略已被
> [ADR-0063](0063-keep-plugin-startup-independent-from-model-enhancements.md) 替代；本 ADR 的证据与导航边界继续有效。

| 状态 | 决策日期 |
|---|---|
| 已采纳 | 2026-08-14 |

> 后续 [ADR-0057](0057-select-source-analysis-tools-by-evidence-stage.md) 已选择 Direct Jedi，
> [ADR-0059](0059-share-read-only-evidence-access-across-agent-flows.md) 已实现共享只读 FileSystem / Jedi
> 领域工具与路径策略。确定性首包和共享工具现已接入教学模型；当前还会把宿主实际安装的 Uninfo
> 六项常用便捷 Permission 确定性投影为角色 / 场景约束，避免每个插件重复导航依赖源码。真实 Provider
> held-out 与重新资格尚未完成。
>
> 后续 [ADR-0062](0062-structure-capability-teaching-usages-requirements-and-interactions.md) 已细化教学
> 输出 schema、Uninfo 角色投影和可信限流来源；本 ADR 的 Evidence Pack 与有界导航编排保持不变。

## 当时遇到了什么

教学注释需要理解本轮成功注册的 Matcher、实际命令语法和插件内实现，最终同时服务于公开帮助展示和
后续 Answer Agent。当前实现由程序预先选择 runtime handler、少量配置引用函数和允许投影的配置，再让
无工具的模型一次生成结构化注释。这个边界可控，但普通跨文件 helper、service、Rule、Permission、限流
和框架符号可能没有进入请求；模型无法发现并补读缺失证据。

另一端的纯 Agentic 源码检索也不适合作为起点。让模型从空仓库自行搜索，可能遗漏 Matcher 注册与运行时
命令事实，相同源码下选择的上下文也可能漂移。LSP、Serena 或其他符号工具能够改善跨文件导航，却不能证明
插件本轮已经加载、Matcher 当前注册、Rule / Permission 已通过或某个运行时分支实际执行。

首版还不要求精确判断“某次源码变化只影响哪些 Matcher”。项目作者接受一个更简单、保守的稳定性合同：
正常启动时，插件受管源码没有变化就复用已有教学注释；插件任意受管源码变化时，重新分析该插件当前可
服务的全部教学注释。这样不依赖首版静态关系图必须完整，避免因为漏掉依赖边而错误复用旧结论。

## 决策

1. 教学注释采用“确定性 Evidence Pack + 有界 Agentic Source Navigation”的混合编排，不采用纯预选上下文，
   也不让模型从空仓库开始调查。
2. 模型调用前，Triage 必须主动提供模型外确认的初始 Evidence Pack，至少包含：
   - 本轮成功注册并通过普通教学披露门的 capability / Matcher 身份；
   - runtime 读取的命令头、别名、前缀、Args、Option、Subcommand 和其他已确定调用结构；
   - ast-grep 提取的 Matcher 注册点、handler 绑定和有界源码锚点；
   - 当前实际安装版本中、与本能力直接引用符号对应的 NoneBot、Adapter、Alconna、Uninfo 框架事实；
   - 经过秘密与 restricted-config 策略过滤的配置引用和允许投影值。
3. 初始证据不足时，教学 Agent 可以通过 Triage 拥有的只读 `SourceNavigator` 按需补读。领域工具只提供
   批准根内的文件发现 / 搜索 / 读取和依赖定义跳转；模型不能直接调用 Serena MCP、LSP、Shell、编辑、
   项目切换、根外路径、未受策略约束的 glob 或自行提交 ast-grep 规则。具体只读工具面由 ADR-0059 收窄。
4. `SourceNavigator` 固定在当前批准的插件源码根和批准的公共框架组件内。每项结果必须重新绑定 component、
   source revision、相对 locator、内容摘要、Evidence ID、完整度和预算状态；后端返回的候选关系不能直接
   升级为运行时因果。
5. Agent 只能选择还要读取哪些已准入源码证据，不能拥有或修改以下真值：Matcher 是否注册或启用、精确
   命令语法、adapter / platform、public / restricted、模型外已确认的 Permission / Rule，以及当前用户是否
   可执行。模型可以提交有 Evidence 引用的更严格使用前提或风险候选，但是否降级 disclosure / platform /
   ServingView、收紧教学投影或拒绝发布，仍由模型外 reconciler 决定；任何模型结果都不能扩大权限、可见
   范围、支持平台或调用形式。
6. 最终生成前冻结本轮实际使用的全部 Evidence。注释中的实质结论必须引用该集合；引用未知证据、证据
   不完整、源码 revision 冲突、工具失败、预算耗尽或结构化输出校验失败时，本能力不发布新的教学注释，
   回退到确定性基础说明。单项失败不得隐藏其他完整能力。
7. 使用插件级 `plugin_source_revision` 作为源码变化的首版失效边界：
   - revision 至少覆盖批准插件源码根内、能够归属到该插件的全部 Python 源文件，而不是只覆盖当前 Matcher
     或上一次模型实际读取的证据闭包；任何会进入教学分析的其他源码种类也必须纳入；
   - inventory 无法证明完整、源码归属不唯一或 revision 计算 partial 时，不得把插件判为“未变化”后复用
     旧注释；`.pyi`、editable、symlink 和生成文件的具体枚举规则留给实现与真实部署验证收敛；
   - 在同一部署事实和 generation contract 下，revision 未变化时逐字复用该插件现有注释，不调用教学模型
     或源码导航；
   - revision 变化时，使该插件全部教学注释失效，并重新分析本轮仍可服务的 capability；
   - 不以首版调用图或上一次 Agent tool trace 判断无关文件变化，也不要求精确到单个 Matcher；
   - runtime 未观察到、披露门变化或平台不匹配可以停止 serving，但不能用旧注释恢复一个当前未注册能力。
8. generation contract、框架事实 revision、允许配置投影和部署 runtime facts 继续作为独立的显式失效输入；
   “插件源码未变即复用”指这些输入也未变化的正常启动，不允许用稳定性要求绕过安全、版本或语义合同升级。
9. 本 ADR 不选定 `SourceNavigator` 的具体实现。Griffe、Jedi、Serena / LSP 或其他后端可以在保持同一领域
   接口、Evidence 门禁和失败语义的前提下替换；具体选型与是否共享 Bug 导航进程继续由 ADR-0057 评审。
10. 代码正文检索不建立向量 RAG 真值层。符号导航、结构检索和受控正文读取负责源码取证；版本化设计文档
    或长篇官方说明如需检索，仍属于独立知识包 / RAG，不得替代 runtime 与源码 Evidence。
11. 常见、稳定的框架便捷语义应在首包复用，不让每条能力重复调查同一依赖。宿主安装 Uninfo 后，静态层
    临时解析 import 绑定，确认 `MEMBER / ADMIN / OWNER` 或
    `PRIVATE / GROUP / GUILD` 确实来自 Uninfo 后，分别投影为公开角色或场景约束；import 来源不进入最终
    约束记录。这组语义作为 Triage 维护的稳定知识长期保留，不以精确安装版本作为启用门，依赖版本变化
    本身也不使教学注释失效；只有语义表或生成合同被主动修改时才更新。其他同名本地符号以及带部署特定
    ID / 回调的高级 Permission 保持 opaque，必要时才由 Agent 补读，不能只按符号名字猜测。当前语义已用
    nonemigut 的 Uninfo 0.11.1 源码复核。

## 为什么这样选

- 确定性首包保证模型一定先看到当前注册事实、命令 grammar 和安全下限，不依赖模型自己想到正确起点；
- 有界导航允许模型在遇到跨文件 helper 或 service 时继续取证，弥补当前固定片段召回不足；
- Triage 拥有工具、源码根、revision、预算与 Evidence 身份，能够在更广的读取能力下继续保持可审计边界；
- 插件级整体失效牺牲部分增量效率，换取不依赖不完整调用图的保守正确性，也满足未变化插件零重写的首版
  稳定性目标；
- 后端中立接口允许先完成产品合同，再根据真实 Matcher 与 Bug 样例调整 LSP / Serena / Jedi 选型，不把
  当前试验实现冻结成长期依赖。

## 没有采用的方案

### 继续只由程序预选全部源码

它最可复现，但当前只选择 handler 和少量配置相关函数，无法可靠覆盖普通跨模块 helper、service 和更深的
行为条件。继续为所有 Python 关系编写专用闭包规则，会让 Matcher 索引逐渐变成不完整的通用源码分析器。

### 让 Agent 从空仓库开始搜索

它能探索更多文件，却可能漏读注册点、命令结构或关键权限入口，并扩大检索漂移、延迟和源码外发面。
Agentic 导航只用于补充确定性首包，不能成为 runtime 与静态锚点的替代品。

### 默认上传整个插件源码

整包上传会包含大量无关能力与实现，增加上下文噪声、费用和隐私面，也让每个请求与插件总体规模绑定。
插件源码可以在本地参与 revision 计算和受控导航，但正文只在本轮确有需要时作为 Evidence 读取。

### 首版按 Matcher 精确失效

精确失效要求静态关系图完整覆盖跨文件调用、动态注册、共享配置和框架依赖。当前实现尚未达到这个条件；
漏边会让已经过时的教学注释继续服务。若后续评测证明闭包完整且增量收益值得维护成本，可以用 successor
ADR 重新选择该合同。

## 带来的影响

- 教学任务将从一次无工具请求升级为带有受限源码工具的独立 Agent 合同；Prompt、工具预算、资格 profile、
  出站隐私投影和 held-out Gate 都必须重新评审，不能继承当前无工具 dogfood 资格；
- 插件源码变化会重算该插件全部教学注释，后台延迟与模型费用高于精确增量方案，这是首版明确接受的代价；
- LSP / Serena 等后端仍然可以保持可选、延迟启动并失败回退，不成为 Bot 启动或基础能力影子的硬依赖；
- 公开帮助只消费通过模型外 disclosure、Evidence 引用和输出校验的教学投影，不展示源码、符号、配置键或
  内部实现细节；SUPERUSER 行为探索和 Bug assessment 继续走各自独立的授权与披露链；
- 后续可以调整预取字段、导航后端、工具预算和候选排序，只要继续满足混合编排、字段所有权、只读门禁、
  插件级失效与失败关闭合同。改变这些上游合同需要新的 successor ADR。

## 落实与确认

- 项目作者于 2026-08-14 初步同意按本 ADR 实施，并保留根据真实试运行与评测结果用后续 ADR 调整路线的
  权利；“已采纳”表示当前实施基线，不表示具体后端或全部参数已经永久冻结。
- `capability_analysis_adapter` 现会把 runtime 命令事实、ast-grep Matcher 结构、已加载 handler、插件级
  source revision 和当前内存配置投影装配成首个 Evidence Pack。
- `PydanticAICapabilityAnalysisClient` 现可在首包不足时挂载 ADR-0059 的只读 FileSystem 与 Direct Jedi；只有
  成功 `read_file` 的正文获得可引用 Evidence ID，Jedi 定义位置和 glob/search 结果只作导航。
- 动态 Evidence 正文与配置值不持久化；cache 只保存公开注释、请求指纹及动态 Evidence 的 ID、相对 locator
  和 revision。插件源码 inventory partial、symlink、分析中漂移或缓存 Evidence 失效都会拒绝复用或发布。
- 公开查询现会把命中的教学注释投影进 Answer Agent 的闭合 public facts；Answer 成功时结合当前问题组织语言，
  失败时才回退确定性注释模板。展示 YAML 继续由同一注释独立投影，未接入 Migut Help。
- 教学 Agent 当前最多进行五次只读补证、八次 Provider 请求，整轮最多使用 120,000 tokens，专用结构化
  输出上限为 4,096 tokens；工具预算耗尽后会隐藏导航工具并要求立即提交结构化结果。模型输出和最终公开
  投影分别校验实现细节、Evidence 闭包与展示语法，低风险文案中的单条坏项可以丢弃，角色、场景和限流
  等安全 requirement 仍整份严格失败。
- Runtime handler 带闭包自由变量时，单 Matcher 请求不再只发送相同函数正文后猜测每个实例的行为，而是
  以 `parameterized handler requires family-level analysis` 跳过。`nonebot_plugin_memes` 实测 445 条公开
  候选中 437 条因此不再逐项调用模型；通用 catalog / family 教学条目尚未实现，不能用任意请求上限或代表
  Matcher 冒充。
- 仓库维护入口 `analyze-capability-teaching` 只分析明确点名的宿主与插件；它在无 Adapter 连接的 NoneBot
  进程内建立 runtime snapshot，复用当前缓存并把预览写入 Triage LocalStore data。选定插件刷新不会删除
  其他插件预览，也不会写入 Migut Help 配置。
- 当前已完成本地结构、工具循环、Evidence 闭包、缓存失效和宿主接线测试；任务仍保留受控 dogfood 状态，
  在新的真实 Provider held-out 通过前不升级为稳定资格。

## 与既有决定的关系

- 延续 [ADR-0032](0032-separate-capability-audience-analysis-and-platform-status.md) 与
  [ADR-0036](0036-keep-capability-shadow-deterministic-and-record-oriented.md) 的模型外 ServingView、受众与
  当前 runtime record 真值；
- 消费 [ADR-0039](0039-use-griffe-for-installed-public-framework-source-evidence.md) 保留下来的版本、来源与
  revision 安全合同，并由 ADR-0057 / 0059 的 Jedi 工具读取定义；不把静态符号关系提升为运行因果；
- 保持 [ADR-0055](0055-use-ast-grep-for-matcher-source-shape-extraction.md) 的窄 CST 形状职责；
- 不扩大 [ADR-0056](0056-use-serena-for-optional-bug-source-navigation.md) 已采纳的 Bug-only Serena 配置。
  教学链若复用 Serena，必须通过本 ADR 的 `SourceNavigator` 边界并另行完成实现与资格；
- [ADR-0057](0057-select-source-analysis-tools-by-evidence-stage.md) 已选择 Direct Jedi；
  [ADR-0059](0059-share-read-only-evidence-access-across-agent-flows.md) 已决定跨消费者共享的文件、路径和
  运行配置证据工具边界。

## 实践参考

- [Aider Repository Map](https://aider.chat/docs/repomap.html)：先提供受预算约束的仓库结构地图，再按需
  加入具体文件，而不是每次上传整个仓库；
- [Sourcegraph Cody Context](https://sourcegraph.com/docs/cody/core-concepts/context)：组合关键词搜索、代码图
  和上下文选择；
- [Sourcegraph Agentic Context Fetching](https://6.6.sourcegraph.com/cody/capabilities/agentic-context-fetching)：
  允许模型在初始上下文后按需补充取证；
- [Serena Tools](https://oraios.github.io/serena/01-about/035_tools.html) 与
  [Security Considerations](https://oraios.github.io/serena/02-usage/070_security.html)：符号导航能力与默认
  编辑 / Shell 工具面的安全假设，支持由 Triage 再封装只读白名单而不是直接暴露 MCP。
