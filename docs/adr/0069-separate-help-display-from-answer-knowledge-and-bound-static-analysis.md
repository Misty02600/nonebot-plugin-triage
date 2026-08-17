# ADR-0069：分离帮助展示与 Answer 知识，并让静态分析只界定证据范围

| 状态 | 决策日期 |
|---|---|
| 已采纳；首个纵切已实现，源码级 Provider held-out 仍未通过 | 2026-08-15 |

## 当时遇到了什么

教学注释最初用同一份结构化数据同时服务 Migut Help 展示和 Answer LLM。继续分析工厂生成的 Matcher
后发现，两类消费者需要的内容并不相同：帮助图只需要少量、统一且适合直接阅读的重点功能；Answer 则可能
需要一个插件中大量成员命令、特殊参数和公开行为说明。把后者继续塞进帮助 YAML，会迫使 Migut Help 理解
不需要展示的数据，也让维护者难以聚焦真正会进入帮助图的内容。

讨论中还一度准备让 ast-grep / Runtime 为模型生成 `member_count`、`common_prefixes`、
`runtime_member_headers` 和逐 Matcher 兼容性摘要。项目作者进一步明确：静态分析的核心价值是限定模型可以
调查的入口、源码范围和公开边界，不是替模型理解源码。若为了工厂聚合建立 Permission、平台、前缀和命令树
的完整语义比较器，会重新形成一套复杂而脆弱的专用分析器；复杂情况宁可不生成知识。

## 决定

1. 一次受控教学分析产生两个彼此独立的公开投影：
   - `help-display/<plugin_module>.yml` 保存面向 Migut Help 的紧凑、结构化重点功能；
   - `answer-knowledge/<plugin_module>.md` 保存只供 Answer LLM 使用的自由 Markdown，可以按插件实际内容使用
     标题、段落、列表或表格，不建立插件无关的成员 catalog schema。
2. Answer LLM 同时消费两种投影，而不是二选一。模型外协调器先把帮助投影转换为规范化公开事实，再附加
   Answer Markdown：
   - 帮助投影拥有功能名、标准用法、简短说明和结构化公开要求的优先权；
   - Answer Markdown 只能补充成员命令、公开特殊行为和更详细的教学说明，不能覆盖帮助投影中的规范入口；
   - 模型接收的是合并后的公开教学视图，不取得物理文件路径，也不自行决定读取其他插件文件。
3. 人工维护的 Migut Help YAML 继续只作后续离线评价源，不进入教学生成 Prompt、RAG 或冷启动基线。由
   Triage 本轮生成的帮助投影可以在回答阶段进入公开教学视图，但不得回灌为下一轮生成证据。
4. 不为 Answer 成员知识新增专用 SQLite、向量索引或 `MemberCatalog` 领域模型。既有 capability shadow
   继续保留每个 Runtime Matcher 的确定性记录；查询命中后由记录的插件 owner / module 选择对应
   `answer-knowledge` 文件。文件较小时读取整份，超出预算时只做有界文本查找并读取命中标题附近正文。
5. ast-grep / CST 静态分析只拥有以下职责：
   - 找到 Matcher 或工厂注册入口、handler / helper 锚点和当前插件源码边界；
   - 建立模型可以读取的批准源码范围、Evidence locator、revision 和缓存身份；
   - 在模型调用前执行既有 public / restricted、主动入口、秘密文件和路径准入；
   - 识别多个 Runtime Matcher 是否可作为同一工厂候选，但不解释共同业务语义。
6. 静态分析不再为模型构造 `member_count`、`common_prefixes`、`compatible_permissions`、
   `runtime_member_headers` 等帮助理解的语义摘要，也不实现逐 Matcher Permission、平台、参数树或动态前缀
   的完整兼容性比较器。Runtime 记录继续证明本轮实际注册和当前性，并可在模型外验证已知确定事实，但不
   代替模型阅读工厂源码。
7. 教学 Agent 在上述受控范围内自行阅读工厂源码、handler、获准的当前内存配置、按需文件和版本化框架
   文档，并判断是否存在可靠共同说明。输出保留单一 `knowledge_enabled`：
   - `true` 表示可以生成本工厂的帮助条目和 Answer Markdown；
   - `false` 表示证据不足、成员语义不一致或无法形成不误导用户的共同说明，两种公开投影均不生成；
   - 模型不能用 `true` 绕过模型外当前性、披露、路径、秘密或 Evidence 引用门禁。
8. 插件当前已经加载的、经过 deny-list 与 Secret 类型过滤的配置实例可以主动进入初始证据。LocalStore
   文件正文默认不预取；只有源码表明确有需要且内存配置不足时，Agent 才通过共享只读工具按需读取，`.env`、
   凭据、数据库、原始日志、人工 Migut Help 和已生成 help-display 继续拒绝进入教学分析。
9. 能由 Runtime 或确定性配置关系确认的前缀集合继续保留为事实，标准展示前缀按空前缀、`/`、`.`、来源
   声明顺序、最后稳定排序的顺序选择。只有复杂代码逻辑才能解释前缀时，模型只生成一种有 Evidence 支持的
   完整用法；不能可靠确认时设置 `knowledge_enabled=false`，不得自行宣称完整合法前缀集合。
10. 第一阶段继续排除没有确定公开触发形式的全局消息、通知、请求和其他被动监听器。本决定不授权把它们的
    内部分析或完整源码投影给普通 Answer。

## 为什么这样选

- 帮助图与 Answer 知识分开后，Migut Help 和维护者只需关注真正会直接展示的短数据；
- 自由 Markdown 能保留插件特有的公开信息，不需要不断给通用 schema 增加专用字段；
- Answer 同时取得规范帮助和补充知识，既不会丢失标准用法，也不必把详细成员目录渲染出来；
- 复用现有 Runtime Matcher 搜索和有界文件读取，避免为工厂成员再建一套索引；
- 让静态分析负责范围和安全、让模型负责受控范围内的代码理解，可以减少专用规则增长；
- `knowledge_enabled=false` 为异构工厂、动态 Python 构造和证据不足提供统一的保守退出路径。

## 没有采用的方案

### 只让 Answer 读取 Answer Markdown

这会丢失帮助投影中已经规范化的标准名称、首选用法和结构化要求，也可能让补充文案覆盖正式展示合同。

### 把 Answer 详细知识继续放进帮助 YAML

这会把 Migut Help 永远不展示的数据混入其文件命名空间，并迫使插件无关 schema 表达大量插件特有信息。

### 为工厂成员设计统一 catalog 与专用检索索引

现有 capability shadow 已能按实际 Matcher 命中插件；额外 catalog、字段和索引会增加同步、失效与维护成本。

### 用静态分析完成工厂语义兼容性判定

Permission、动态前缀、闭包值和成员特有分支很快会使比较器接近完整代码语义分析。首版接受少生成知识，
不为罕见异构工厂扩张专用实现。

## 带来的影响

- 单一结构化教学注释已经演进为一份受控分析结果和两个公开投影；
- Answer 协调层已经建立“规范帮助优先、Answer Markdown 补充”的公开教学视图；
- 参数化 Handler 由外层工厂源码锚点形成一个分析单元，不再把相同 Handler 源码重复发送数百次；
- 普通能力继续使用 `{command}` 用法合同，工厂能力使用模型直接给出的完整、可引用用法；
- RAG 检索优化由独立任务处理，本 ADR 只规定教学链可以主动获得相关框架文档并按需继续查询；
- 以后若要把被动能力、内部行为或 SUPERUSER 源码知识加入 Answer，必须另行决定受众隔离与投影合同。

## 落实与确认

- `capability_analysis_adapter.py` 以闭包 Handler 的精确源码位置解析唯一外层工厂；同一工厂的公开 Runtime
  Matcher 合并为一个 `command_family` 分析单元。工厂成员只要混入未准入记录、源码位置歧义或 inventory
  不完整，就跳过整个工厂，不让模型自行跨越披露边界。
- 工厂请求只主动提供工厂源码、插件已声明的公开教学信息和能由现有静态引用证明的安全内存配置；成员数、
  命令样本、共同前缀和 Permission 兼容摘要没有加入模型输入。Agent 仍可在批准根中按需读取文件、用 Jedi
  转到定义，并通过版本限定的知识索引检索 NoneBot 文档。
- `CapabilityTeachingAnnotation` 已加入 `knowledge_enabled`、完整用法模式和自由 `answer_markdown`；关闭结果会
  缓存并阻止两种公开投影生成。
- `capability_teaching_outputs.py` 把 YAML 与 Markdown 写入同一不可变 generation，并用一个原子
  `current.json` 指针切换；写入失败不发布半套新结果，Answer 内存视图同时失败关闭。
- 自动刷新继续在启动后后台执行；SUPERUSER 可用 `triage 刷新帮助 [plugin_module]` 强制重新分析全部或指定
  插件。失败只记录异常类型并返回简短失败消息，不覆盖现有文件 generation。
- 本地 schema、路径、原子发布、工厂分组、工具 Evidence 与命令门禁测试已经通过。2026-08-16 冻结的
  `capability-teaching-v1-forward-heldout-16-20260816-a-v12-zh` 首轮真实 Provider Gate 已执行一次：
  schema / Evidence 闭包为 0.750，公开投影与安全门为 0.5625，语义合同为 0.375，工具补证用例为
  0.500；42,331 input / 5,565 output token，可核算费用为 3,064 microUSD。资格身份与 Fixture 哈希检查
  全部通过，但质量门失败，任务继续只属于受控 dogfood。
- 本轮还发现至少一个评测合同缺陷：绘图用例的 Gold 要求 `<描述>`，提供给模型的 Evidence 却没有声明
  该输入。该缺陷会压低语义分数，但不能解释 schema、投影、Evidence 闭包和工具门的失败，因此不改变
  “未获得资格”的结论。同一 held-out 不用于调 Prompt 后重跑；后续需要先形成 development 集合并修正
  评测诊断，再使用全新的 forward-heldout。
- 已消费失败样例只用于非资格诊断。到中文 Prompt v15，公开 Markdown 校验、constraint 条件 schema、
  `{command}` 用法校验、工厂单一聚合用法、有序配置前缀和“限制不存在”过滤等已知根因完成修正；最后
  3 项定向真实 Provider 诊断的 schema、Evidence 闭包、投影、安全、语义、预算与工具指标均为 1.000。
  该结果固定为 `qualification_eligible=false`，不会把 development 样例回写成资格证据。
- 随后冻结全新的 `capability-teaching-v2-forward-heldout-24-20260816-a-v15-zh`：其中 12 项包含真实
  Python Fixture，评测器先运行产品使用的 ast-grep 提取器，再把结构事实与指定源码作为 Evidence 交给模型；
  Fixture JSON 与全部 Python 源码共同绑定 bundle SHA-256
  `1ce297ea7f5b0669fb1e2969ec0c4b9f3735c97efe887691977df9e680ad7299`。
- v2 正式 Provider Gate 只运行一次。资格身份、合同、覆盖与 12/12 源码提取全部通过；24 项中 9 项通过，
  schema / Evidence 闭包 / 公开投影 / 安全 / 预算均为 0.9583，语义为 0.375，工具为 1.000；共
  102,222 input / 16,072 output token，7,074 microUSD。质量门失败，仍不产生任务资格。
- v2 失败不能全部解释成模型语义错误：14 项命中 `usage_contract`，其中多项候选保留了正确语义，但用了
  不同的占位词、把默认与可选形式拆成两条，或补充了 Gold 未列出的合法用法。这暴露了逐字符串正则作为
  usage 主评分器过窄。与此同时，冲突输入证据下模型仍选择一侧并启用知识，以及一个 `@bot` 源码案例在
  结构化输出重试后整体失败，属于需要继续保守处理的真实风险。冻结 bundle 不因这些结论修改或重跑；后续
  若继续资格评测，应先独立设计 usage 结构等价评分，再使用新的 forward-heldout。

## 替代关系

- 部分替代 [ADR-0062](0062-structure-capability-teaching-usages-requirements-and-interactions.md) 中用同一结构化
  schema 同时承载 Answer 详细知识的边界；其帮助字段、公开约束和 Migut Help 投影规则继续有效。
- 细化 [ADR-0058](0058-use-deterministic-evidence-and-bounded-navigation-for-teaching-annotations.md) 的静态
  Evidence Pack 职责：确定性层拥有准入、范围、当前性和验证，不承担工厂业务语义摘要。
- 保持 [ADR-0036](0036-keep-capability-shadow-deterministic-and-record-oriented.md) 的逐 Runtime Matcher
  确定性记录，不把工厂聚合写回能力真值层。

## 相关文档

- [可选帮助数据源与复用边界](../architecture/help-source-adapters.md)
- [能力影子索引流程](../architecture/flows/capability-shadow-index.md)
