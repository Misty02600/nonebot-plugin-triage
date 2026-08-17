# PLAN-0017：收敛多条目教学注释的生成与评测合同

| 状态 | 最后更新 |
|---|---|
| 已完成 | 2026-08-16 |

## 背景

[ADR-0080](../../adr/0080-model-capability-teaching-as-multiple-public-entries.md) 已把一次能力分析改为多个公开
教学条目，移除 `{command}` 和结构化 `interaction`，并用全新的 v3 forward-heldout 做了一次真实 Provider
资格评测。领域、Evidence 闭合、公开投影、工具预算和源码提取均通过，但安全合规率为 0.9167、语义合规率
为 0.3333，只有 8/24 用例通过，因此当前任务组合没有取得生产资格。

冻结 fixture、Provider 输出和报告继续作为历史证据，不修改结果后重跑。本计划用于区分评测误判与真实生成
缺陷，先收紧模型外合同，再建立新的开发回归集和全新 forward-heldout；不通过反复改写同一正式数据集追分。

## 当前设计与缺陷

### 相关实现与当前行为

- 领域与注释合同：`src/nbtriage/capability_analysis.py`、`src/nbtriage/capability_annotations.py`
- 模型 Prompt 与结构化输出：`src/nbtriage/capability_model_adapter.py`
- Runtime / 源码 Evidence 适配：`src/nonebot_plugin_triage/capability_analysis_adapter.py`
- Help 与 Answer 投影：`src/nonebot_plugin_triage/capability_help_display.py`、
  `src/nonebot_plugin_triage/capability_teaching_outputs.py`
- 评测器：`tools/nbtriage_maintainer/capability_teaching_evaluation.py`
- 冻结 fixture：`evals/datasets/fixtures/capability-teaching-v3-forward-heldout.json`
- 本地报告：`reports/capability-teaching-v3-forward-heldout-20260816.json`，属于忽略的本地工件，不发布。

当前请求先提供模型外确定的 entry ID、调用锚点、Runtime 事实、源码 Evidence、允许的运行时配置投影和相关
框架资料；模型返回完整 entry 文案。每条 output 已经过 schema、Evidence 引用闭合、公开投影和预算校验，但
调用形式仍主要依赖自然语言 Prompt 与简单字符串检查，离“确定语法不被模型改写”还有距离。

### 缺陷机制、证据与影响

| 类别 | 已确认问题 | 证据与影响 |
|---|---|---|
| 评测误判 | `required_usage_patterns` 使用 `re.fullmatch`，但多条规则只写了开头锚点；合理的 Option 展开、有限 family 枚举和等价占位词也被单一正则拒绝 | `听歌识曲 [音频]`、`订阅 添加 <主题> [-q]`、`$(复古\|锐化\|黑白) [图片]` 等有效输出被判失败，当前 0.3333 不能直接解释为模型真实正确率 |
| 确定调用条件遗漏 | `to_me` 已作为 requirement 说明，但 usage 仍为 `延迟`，没有模型外投影为 `@bot 延迟` | 帮助图可能给出不能在群聊中直接触发的写法 |
| 参数可选性漂移 | 模型把可选目标语言输出为 `<目标语言>`，或把可选数字展开为一个裸常量示例 | 用户可能把可选参数误解为必填，或把示例值误解为固定语法 |
| 命令正文所有权过宽 | anchored 校验按字符串包含判断；尚未显式区分 canonical command body 与同一 entry 的合法 alias | `头像查看` 可因包含 `头像` 而误过校验，不能证明它来自 Runtime 已确认 alias |
| 框架符号泄漏 | `OWNER`、`MEMBER` 出现在面向用户的 requirement 文本 | 公开文案泄漏实现词汇，且不符合 Migut Help 人工文案风格 |
| 安全门禁不确定 | 第三方 `CooldownGuard` 只有结构候选时，旧实现会在模型调用前直接关闭整个 `海报` 知识 | AST 只能证明存在疑似控制点，不能证明它真的限制用户；ADR-0083 改为先读取定义、框架事实或运行配置，只有补证后仍未知才关闭 |
| baseline 断言过严 | 旧稿原本没有 behavior boundary，新模型在证据支持时补了一条，评测仍要求字段完全不变 | “尽量少改”被错误等同于“所有旧字段不可增加”，会阻止合法补充 |

这次源码提取 12/12 通过，没有证据要求继续扩大 ast-grep、Jedi 或文件工具范围。当前风险集中在
“确定性事实如何交给模型、怎样验证公开输出，以及评测如何承认语义等价”，不是 AST 覆盖率不足。

## 技术路线

### 目标行为与约束

1. 保留 `CapabilityTeachingEntry`、多条 `usages`、结构化 requirements 和 Answer Markdown；不再进行一次新的
   大规模字段重设计。
2. Runtime / parser 能确定的 entry 数量、命令正文、alias、参数必选性、Option 名称、`to_me` 和公开权限
   继续由模型外拥有；模型只负责选择可读写法和补充有 Evidence 的用户说明。
3. 调用形式校验按帮助记法的结构语义执行，不依赖命令正文的普通子串包含，也不要求某一个固定中文占位词。
4. 任何公开输出不得包含源码符号、配置键、Evidence ID 或框架内部权限名。失败时拒绝该次新结果，不静默
   猜测修复。
5. AST 发现的疑似权限、Rule 或执行门禁必须先形成 `constraint`、`no_constraint` 或 `unresolved`
   resolution；只有 `unresolved` 才令整个公开 entry 使用 `knowledge_enabled=false`。关闭项必须进入部署
   日志；`no_constraint` 必须引用候选之外的实际定义、框架事实或运行配置，不能由名称或证据缺失推断。
6. Runtime parser 已确认的参数、子命令和 Option 由模型外压成 canonical usage；同一位置的备选值不超过四个
   时可以枚举，超过四个时使用一个概念槽位。必填的工厂成员槽位使用 `<>`，不使用表示可省略的 `[]`。
7. v3、v4、v5 fixture 与正式报告保持冻结。开发回归集可以吸收已知失败机制；下一次资格必须使用新
   Prompt / 合同、新 bundle ID 和未被开发过程看过的 v6 forward-heldout。

### 实施步骤

| 顺序 | 改动 | 主要实现位置或符号 | 关键约束 | 预期结果 |
|---:|---|---|---|---|
| 1 | 固化失败分类与开发回归集 | `evals/datasets/fixtures/`、`tests/test_capability_teaching_evaluation.py` | 从 v3 失败提炼机制，不把回归集冒充新的 held-out | 可以重复验证已知问题，又不污染下一次正式资格数据 |
| 2 | 增加确定调用合同 | `CapabilityInvocationTarget`、NoneBot adapter | 为 anchored entry 区分 canonical body 与模型外确认的 accepted bodies；记录参数必选性、Option token 和 `to_me` mention policy | alias 不再靠子串误过；`@bot` 与可选参数不再由模型猜 |
| 3 | 收紧公开输出校验 | `capability_annotations.py`、投影层 | 只解析本项目帮助记法的括号、参数、Option 和命令锚点；禁止 `OWNER`、`MEMBER`、`ADMIN`、`Permission` 等内部符号；不做自动文字修补 | 语法和公开边界错误在写 cache / YAML / Markdown 前失败关闭 |
| 4 | 调整模型合同 | `capability_model_adapter.py` | 中文 Prompt 要求逐项解释 gate candidate；模型外校验 candidate / resolution / constraint / Evidence 闭合；旧稿仍被新 Evidence 支持时尽量原样保留 | 可解释的第三方门禁不再误关，真正未知仍安全关闭，并降低无意义文案漂移 |
| 5 | 用结构语义替换脆弱正则 Gate | `capability_teaching_evaluation.py` | 分开硬正确性与编辑质量；比较命令锚点、参数必选性、Option 集、entry ID 和要求类型，参数中文标签允许等价表达 | 合法拆写不再误判，真实语法错误仍稳定失败 |
| 6 | 本地与诊断 Provider 验证 | 领域/adapter/投影/evaluator 测试与非资格诊断集 | 可重复运行；报告记录真实失败，不修改 v3 正式报告 | 所有已知 P0 回归归零，观察剩余模型文案问题 |
| 7 | 冻结并运行 v6 forward-heldout | 新 fixture、Prompt SHA、bundle SHA 和本地报告 | 使用全新案例；正式 Provider Gate 只运行一次；失败仍保留原始报告 | 得到可审计的新资格结论，而不是对旧题调参 |

### 评测分层

| 层 | 评价内容 | 资格要求 |
|---|---|---|
| 硬安全与合同 | schema、Evidence 闭合、entry ID、命令/Option 不虚构、必选性、权限不扩大、内部符号不披露、预算与工具边界 | 1.000，且不得存在高严重度单例失败 |
| 语义正确性 | 功能摘要、输入前提、多个限流、行为边界、工厂共同说明、未知时正确 abstain | 至少 0.900，并单独列出每个失败原因 |
| 编辑质量 | 参数名是否更简洁、Option 是合并还是分行、summary 详略、与人工 Migut Help 的字段级差异 | 先报告，不作为安全 Gate；由后续专门评价标准决定 |

人工 Migut Help YAML 只在生成完成后做字段级比较，观察功能分组、usage 记法、文案长度和特殊说明；它不进入
生成 Prompt，也不作为绝对事实。比较前必须绑定对应插件源码 revision，避免把版本差异记成模型错误。

## 完成标准与验证

| 验收项 | 覆盖条件或输入 | 预期结果 | 验证方式 |
|---|---|---|---|
| anchored 所有权 | canonical body、合法 alias、正文包含关系和未知 alias | 只有模型外确认的完整命令正文可进入 usage | 领域与投影单测 |
| `to_me` | 群聊提及、私聊直接使用 | 公开合同保留场景语义，默认帮助写法不遗漏 `@bot` | NoneBot adapter 与 Help 投影测试 |
| 参数语法 | 必填、可选、变长、Option alias、Alconna 子命令 | `<>` / `[]` 与 Option token 不被模型改变；等价中文标签可通过 | 结构化 usage 校验测试 |
| 公开文字 | Uninfo `ADMIN` / `OWNER` / `MEMBER` 与源码符号 | 结构化角色保留，用户文字只出现可读中文 | 安全投影测试 |
| 安全 unknown | 疑似门禁分别具有可读放行定义、明确限制定义和不可读动态实现 | 前两者分别保留无约束知识或生成公开约束；最后一项关闭，并在刷新日志列出关闭单元 | 模型合同、Jedi/源码 Evidence 与服务回归用例 |
| baseline | 旧字段仍受支持、证据新增必要说明、证据推翻旧说明 | 无关字段尽量逐字保留；合法新增或修正不被评测误判 | baseline 回归用例 |
| 评测可信度 | 等价写法与真实错误的成对 near-miss | 前者通过、后者失败，并输出具体失败轴 | evaluator 单测 |
| 新 Provider Gate | 全新 v6 forward-heldout | 硬安全 1.000、语义至少 0.900、身份与 bundle 校验通过 | 一次正式评测报告 |
| 仓库质量 | 全部改动 | 定向/全量 pytest、Ruff、BasedPyright、`git diff --check` 通过 | 本地命令 |

## 非目标

- 不扩大被动监听器、通知、请求或全局消息 Matcher 的公开教学范围。
- 不为参数化工厂建立插件特化 catalog、向量索引或成员数据库。
- 不因为这次评测继续扩大 LocalStore、`.venv`、日志或配置文件读取范围。
- 不让模型决定 entry 数量、插件归属、文件路径、当前注册状态、public / restricted 或最终权限。
- 不在本计划内制定人工 Migut Help 的完整评分权重、LLM judge 或自动发布阈值。

## 当前进度

- 已把 Runtime Alconna 参数、叶子子命令和 Option 规范化为模型不可改写的 canonical usage；普通
  `on_command` 的业务参数仍由模型在 Evidence 内解释。
- 已按 ADR-0082 把参数化聚合收窄为 Runtime Handler 精确代码身份；删除 AST 外层工厂推断和成员数量、
  成员名、省略标记 Evidence。有限枚举只保留为通用公开输出规则。
- 已按 ADR-0083 把未知门禁从模型前短路改为三值 resolution：实际约束、已证明无约束、仍无法确认；只有最后
  一种关闭知识，刷新状态与汇总日志继续记录 disabled 单元。
- 已加入 v4 开发回归集；v3 正式 fixture 与报告保持冻结，新 Prompt 不再冒充 v3 资格。
- v18 的 4 条真实 Provider 开发诊断曾全部通过：schema、Evidence 闭合、公开投影、安全、语义、预算和
  工具边界均为 1.000；共 6 次请求、14,926 input token、2,218 output token、781 microUSD。ADR-0082 已
  删除该诊断依赖的成员摘要并把 Prompt 升为 v19，因此这组结果只作历史证据。
- v19 的 4 条真实 Provider 开发诊断已通过并保留为历史证据；随后新增四条直接包含合成插件源码的案例，
  首轮暴露可选参数冗余写法、评测正则误判和异构工厂被强行列成菜单三个问题。v21 已用通用结构门禁收口：
  方括号用法不再重复短写，`complete` 聚合的圆括号只允许枚举同一成员槽位，不允许嵌入各自带参数的完整
  命令；没有共同调用结构时关闭知识。
- v21 的 8 条真实 Provider 开发诊断全部通过：schema、Evidence 闭合、公开投影、安全、语义、预算、工具
  和源码提取合规率均为 1.000；四条源码案例覆盖普通可选参数、Uninfo 管理权限、三成员同构工厂和语义异构
  工厂，共 15 次请求、48,499 input token、8,187 output token、3,958 microUSD。该数据集仍是开发回归集，
  缺少正式 forward-heldout 身份和完整覆盖，报告中的资格 Gate 因此保持失败；不得冒充 Provider 资格。
- 冻结 v4 forward-heldout 已运行且未通过：20 条案例、22 次请求，schema、Evidence、公开投影、安全与预算
  合规率均为 0.850，语义合规率 0.650，工具与源码提取合规率 1.000；69,466 input token、11,485 output
  token、5,505 microUSD。报告保留在本地 `reports/capability-teaching-v4-forward-heldout-20260816-v21-run-1.json`，
  fixture 与结果不修改、不重跑，当前任务继续保持 provisional。
- v26 开发回归已扩为 12 条、其中 8 条从合成插件源码开始，覆盖 alias、回复输入、`to_me()`、业务前缀、
  参数化聚合和多个限流条件。原先的 `blocking_unknown_*` 模型前短路已由 ADR-0083 删除，开发案例改为显式
  gate candidate，并由 Agent 返回有 Evidence 的 resolution。此前真实 Provider 诊断不继承到新合同。
- 当前 Prompt 已升为 v31，补充 gate resolution 与未解析依赖注入合同，并继续保留 v28 的 Migut Help 多值记法：`<参数...>` 表示至少一项，`[参数...]` 表示零项或
  多项；Alconna `MultiVar` 仍由 Runtime 快照与 canonical usage 模型外确定，不让模型改写。
- v29 明确第三方权限、限流和执行条件不要求先成为 gate candidate：Agent 可以阅读 handler、helper 和
  已批准的第三方定义主动识别；禁止的是只凭函数名或包名猜测。gate candidate 只强制闭合静态层已经发现的
  未知入口控制点。
- 刷新状态与维护命令现在分别报告参数化能力族的 eligible、disabled 与 failed 数量；先用真实部署关闭率
  判断是否继续增强 family 材料，不为少数失败恢复脆弱的工厂解析器。
- v29 Prompt、v26 generation contract 与 v5 forward-heldout 已被正式运行消费，不能再次作为资格依据。
  下一轮先在开发回归中修正下列已知机制并升版生成合同，再另建模型未见过的 forward-heldout；正式 Gate
  仍只运行一次，失败时冻结原始结果，不在同一 fixture 上反复调参重跑。
- v5 forward-heldout 已按上述规则冻结并只运行一次：20 条案例、12 条源码案例、27 次请求，schema、Evidence
  闭合、公开投影、预算、工具与源码提取均为 1.000，安全为 0.950、语义为 0.800，未取得资格；共使用
  110,213 input token、14,475 output token、7,849 microUSD。报告保留在本地
  `reports/capability-teaching-v5-forward-heldout-20260816-v29-run-1.json`，fixture 与报告不修改、不重跑。
- v5 新增的关键边界均得到真实 Provider 验证：没有 gate candidate 的 handler 深层第三方限流能从 helper
  定义识别为 45 秒用户冷却；始终放行的插件 Permission 能解析为 `no_constraint`；工具补读的第三方门禁能
  形成 20 秒用户冷却；缺少定义的门禁保持 `unresolved` 并关闭知识。
- v5 的四个原始失败已经重新分类：`查物流 [快递单号]` 相对 Gold 的 `[单号]` 是 placeholder 等价误判；
  baseline 只发生句号和 synonym 顺序漂移，属于编辑质量，其中 summary 不加句末句号还更接近当前人工
  Migut Help 风格；参数化图片 family 的 `image: bytes` 并不是 NoneBot 可自动注入的图片输入合同，缺少
  `CommandArg` / `UniMsg` 消息读取、Alconna `Args[..., Image]` 或明确回复读取，因此该 Gold 属于 fixture
  设计缺陷，不能据此断言模型应生成哪一种图片用法；只有当前值为 `true` 的配置 gate 仍被公开成
  feature requirement 是确认的生成缺陷。
- evaluator 已把 `baseline_preserved` 从硬语义 Gate 移出，仍单独报告 `baseline_exact_preservation_rate`
  与 baseline 案例数；Prompt v31 默认让帮助图 summary 不加句末句号，并明确 Handler 形参名称或类型不能
  单独证明用户输入方式。
- Prompt v31 与 generation contract v28 已修正当前放行配置的 gate：完整定义为 `return enabled` 且当前
  `enabled=true` 时必须返回 `no_constraint`，不能发布 feature-state requirement。开发回归新增“形参类型
  不证明图片输入”和“当前配置证明 gate 放行”两条案例。
- 开发集中需要图片的三条正例已改为真实的 Alconna `Args[..., Image]` 与 `AlcMatches` 读取链；只有专门的
  关闭反例改用真实的 `Depends(resolve_image)`，但不提供依赖定义。它验证“已知存在图片依赖”仍不足以判断
  图片来自当前消息、回复还是其他来源，而不再依赖一段无法正常注入的 `image: bytes` Handler。
- v31 的完整 14 条真实 Provider 开发回归全部通过：schema、Evidence 闭合、公开投影、安全、语义、预算、
  工具与源码提取均为 1.000；共 16 次请求、63,311 input token、5,803 output token、3,665 microUSD。
  未解析图片依赖的定向诊断也通过。该结果只证明已知机制回归，不构成 Provider 资格；v5 fixture 与报告
  继续冻结，下一次正式资格必须使用全新 v6 forward-heldout。
- v6 forward-heldout 已冻结并只执行一次正式 Provider Gate：20 条案例、12 条真实源码案例，schema、
  Evidence 闭合、公开投影、安全、预算、工具与源码提取均为 1.000，语义为 0.950，取得精确任务资格；
  共 24 次请求、99,169 input token、12,161 output token、6,308 microUSD。唯一语义失败是参数化边框
  family 漏写源码业务前缀 `^`，未造成安全越界；fixture 与本地报告保持冻结，不修改后重跑。
- 该唯一失败随后触发活动 Prompt v33：`complete` family 输出前必须从实际 Matcher 注册表达式逐字符复核
  成员变量前后的固定字面量；开发回归新增同时含业务前缀与后缀的近邻案例。v33 不继承 v31 的精确资格，
  当前只进入 provisional dogfood；已完成计划与冻结 v6 结论均不因此重开或改写。
- v32 首次两条定向 Provider 诊断复现了 `^` 被删的问题；v33 强化同一表达式的前后缀自检后，模型生成
  `^<风格名>图 <图片>`。首份 v33 报告仍被过窄的“边框/相框/画框”占位词 Gold 误拒；放宽为任意非空
  概念槽位后，r2 两条诊断的 schema、Evidence、公开投影、安全、语义、预算、工具与源码提取均为 1.000。
  这些报告仍因开发集身份而不能晋级资格，正式 v6 结果不重跑。
- v7 forward-heldout 已冻结并只执行一次正式 Provider Gate：20 条全新案例、12 条全新源码案例，schema、
  Evidence 闭合、公开投影、安全、预算、工具与源码提取均为 1.000，语义为 0.950，取得 v33 精确任务
  资格；共 24 次请求、102,382 input token、10,949 output token、5,906 microUSD。新的 `~<主题名>款
  <图片>` family 前后缀案例通过。唯一语义失败是上一版播客注释未逐字段保留：事实与用法正确，但模型
  改写 summary、删除 synonyms / supported subjects 并重排 Answer Markdown；baseline 精确保留率为 0.000，
  按既有决定继续只作编辑稳定性指标，不进入硬安全晋级门。fixture 与报告保持冻结，不修改后重跑。
- Prompt v34 针对 v7 暴露的检索字段丢失增加了 baseline 自检：当前 Evidence 仍支持时，不得把旧 entry 中
  非空的 `synonyms`、`supported_subjects`、`input_requirements` 与 `behavior_boundaries` 无故删空；旧内容仍
  不是 Evidence，保留后的实体陈述必须重新引用本轮 Evidence。开发回归新增“完全保留”“新增边界”和
  “证据冲突允许替换”三类案例。
- evaluator 保留 `baseline_exact_preservation_rate` 作为软编辑指标，并新增忽略规范排序的
  `baseline_member_preservation_rate` 作为硬成员保留合同，避免把模型外 canonical sort 误判为内容丢失。
- v8 forward-heldout 已冻结并只执行一次正式 Provider Gate：20 条全新案例、12 条全新源码案例，schema、
  Evidence 闭合、公开投影、安全、预算、工具与源码提取均为 1.000，语义为 0.950；25 次请求、111,811
  input token、11,318 output token、6,398 microUSD。1 条逐字基线案例与 2 条成员保留案例均为 1.000。
  唯一语义失败来自冻结 Gold 额外要求声明旧行为已经不存在，模型实际已正确删除失效图片语义并生成新用法；
  fixture 与报告保持冻结，不修改后重跑。运行时资格现精确绑定 v34 / v8，v33 / v7 保留为历史证据。
- 收口验证为 1,264 passed、1 skipped；定向 Ruff / format、BasedPyright、正式 fixture 与 Prompt SHA
  一致性及 `git diff --check` 通过。跳过项是当前 Windows 环境不支持创建测试所需的 symlink，与本次
  改动无关。
- v34 / v8 收口回归为 1,269 passed、1 skipped；定向 56 项测试、Ruff / format 与 BasedPyright 通过。
  跳过项仍是当前 Windows 环境不支持创建测试所需的 symlink，与本次改动无关。

## 相关文档

- [ADR-0080：把一次能力分析投影为多个公开教学条目](../../adr/0080-model-capability-teaching-as-multiple-public-entries.md)
- [ADR-0082：参数化 Matcher 只按 Runtime Handler 代码身份聚合](../../adr/0082-group-parameterized-matchers-only-by-runtime-handler-code-identity.md)
- [ADR-0083：先解释未知教学门禁，再决定是否关闭公开知识](../../adr/0083-resolve-unknown-teaching-gates-before-closing-public-knowledge.md)
- [ADR-0077：把上一版机器生成教学内容作为非证据基线](../../adr/0077-use-previous-generated-teaching-content-as-a-non-evidentiary-baseline.md)
- [模型与 Provider 支持](../../architecture/model-provider-support.md)
