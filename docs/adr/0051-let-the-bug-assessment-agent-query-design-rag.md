# ADR-0051：允许 Bug 判定 Agent 查询受控设计 RAG

| 状态 | 决策日期 |
|---|---|
| 已采纳；首个只读知识包消费者已实现；正文投影由 ADR-0053 部分替代 | 2026-08-14 |

## 当时遇到了什么

[ADR-0050](0050-use-a-bounded-agent-for-user-bug-assessment.md) 已决定由有界 Agent 在历史判定未命中、公开
合同初检仍不能解释现象后，动态协调运行记录和当前 revision 源码事实。但源码只说明当前实现，日志只说明
本次观察；两者都不能独立说明维护者原本承诺或设计了什么。仓库已经有架构说明、Accepted ADR、人工确认
帮助合同和版本匹配的上游资料，并已通过 [ADR-0019](0019-distribute-rag-corpus-as-versioned-knowledge-pack.md)
定义可选版本化知识包的来源、manifest 和不可信证据边界。

因此 Bug Agent 还需要一个受控的设计 RAG 工具，才能把“预期行为、当前实现、本次运行”作为三类不同证据
交叉比较。直接让 Agent 搜索整个仓库或把任意 Markdown 当成系统指令，会绕过来源、revision、披露和 Prompt
注入边界，不能采用。

## 决策

1. `BugAssessmentAgent` 的只读工具集合增加 `retrieve_design_evidence`。它检索已经批准、带 manifest、可验证
   revision 和披露等级的设计知识包，不提供任意文件读取、仓库遍历、网络搜索或动态下载能力。
2. 设计知识包可以包含当前有效的架构决定、Accepted ADR、人工确认帮助合同、项目事实和与部署版本精确
   匹配的上游行为文档。讨论稿、未审核报告、聊天记录、源码注释、Superseded ADR 和无法判断适用版本的
   文档不得冒充当前设计合同；需要保留决策历史时只能标为 historical，不参与当前 verdict。
3. 检索必须发生在 verified verdict 精确短路和模型外公开合同初检之后。精确历史结论命中或公开合同已经
   足以确定 `not_bug` 时，不调用设计 RAG、日志、源码或 Bug Agent。
4. Agent 可以根据中间证据决定是否查询设计 RAG，以及怎样在当前 subject、capability、adapter、scene 和
   revision 范围内形成检索问题。首版每轮最多调用一次；模型外协调器固定知识包、受众域、最大命中数、
   chunk 长度、超时和费用，Agent 不能扩大语料域或自行切换知识包。
5. 工具只返回闭合的 `DesignEvidenceUnit`，至少包含 Evidence ID、知识包和源文档 ID、document revision、
   applicability、authority kind、披露等级、受控命题或有界摘录，以及 stale / conflict 状态。绝对路径、任意
   相邻正文、构建机信息和未准入 metadata 不进入结果。
6. 设计文档继续是不可信证据。检索结果作为带来源的数据传给 Agent，其中出现的命令、指令、工具名、角色
   声明或“忽略此前要求”等文字都不能改变 system instruction、工具权限、调用预算或最终路由；模型输出仍须
   经过 Evidence ID 闭包和确定性 reconciliation。
7. 设计证据只证明匹配版本和适用范围内的预期行为。它不能证明当前源码已经实现该设计，也不能证明本次
   运行经过了某条路径。`bug` 通常需要设计合同与当前实现或可信运行事实之间有被证据支持的不一致；
   `not_bug` 需要当前上下文能够正面匹配适用的设计或公开行为条件。没有检索结果、语料覆盖不完整或只有
   陈旧资料时继续其他取证或返回 `unknown`，不得作 absence claim。
8. 检索前按受众、来源和披露级别建立独立域。普通用户触发 Bug 判定并不意味着其可读取设计知识；Agent
   只获得完成三值判定所需的准入证据，最终回复仍按 ADR-0050 投影，不显示设计文档标题、内部 ADR、摘录、
   Evidence ID 或 restricted 事实。
9. 允许 Agent 查询 RAG 不等于允许把所有命中内容发送给远端模型。只有标记为可进入该精确
   Provider / API / model / task 的字段和摘录才能出站；restricted、部署私有或未完成数据策略审批的设计证据
   保持本地不可投影。未获准时工具必须返回安全派生命题、拒绝该来源或让 verdict 降级为 `unknown`，不能
   通过 SUPERUSER 身份或普通用户请求自动扩大模型数据域。
10. 知识包缺失、损坏、不兼容或检索失败不阻断 Bot 启动，也不触发在线下载。该证据源记为 unavailable 或
    partial，Bug 判定继续使用其余已授权证据；如果剩余证据不足，最终返回 `unknown`。

## 为什么这样选

- 设计 RAG 能补上“预期行为”这一证据面，让 Agent 不必仅凭源码推测产品意图；
- 通过独立工具、manifest 和 EvidenceUnit 复用现有知识包边界，避免给 Agent 任意文件或网络能力；
- 把文档权威性、适用版本和披露等级放在模型外，可以防止陈旧设计、讨论稿或 Prompt 注入改变工具策略；
- 将设计、源码与运行证据分开，能明确解释“设计了但尚未实现”“实现偏离设计”和“实现正确但本次没有足够
  运行证据”三种不同情况；
- 缺少 RAG 时失败关闭到 `unknown`，不会把可选知识包变成部署或 Bot 启动的硬依赖。

## 没有采用的方案

### 让 Agent 直接读取仓库中的 Markdown

这种方式没有稳定 manifest、版本配对、披露过滤、大小限制和来源复核，也会把未采纳草稿与当前合同混在
一起。

### 把 RAG 检索写死成固定 Workflow 步骤

每次都检索会在历史或公开合同已经足够时浪费费用，也不能根据日志或源码中间结果形成更有价值的问题。
是否检索以及检索什么属于 Agent 的开放式证据选择；可检索哪些语料、最多多少次和如何投影仍属于 Workflow。

### 把设计文档当作最终真值

设计可能尚未实现、已经被替代或只适用于其他版本。它只能证明预期行为，必须与当前源码、运行事实和适用
上下文共同 reconciliation。

## 带来的影响

- 需要定义 `DesignEvidenceUnit`、只读 retriever 端口和 Bug Agent 工具薄适配；
- 需要在知识包 manifest 中冻结可用于 Bug 判定的来源、authority、revision、applicability、披露和模型投影
  策略；
- 需要评测设计检索的 subject / evidence Recall、citation closure、陈旧资料拒绝、Prompt 注入隔离和冲突
  降级；
- 需要用 spy 工具证明历史短路与公开初检命中时零 RAG 调用，RAG unavailable / partial 时不产生否定性
  结论，并验证普通用户回复不泄露内部设计资料；
- 当前 bot-docs 检索 PoC 和未来 help-spec 数据可以成为候选来源，但本决定不表示它们已经接入运行时 Bug
  判定，也不授权发布或下载新的知识包。

## 落实与确认

- **已确认**：Bug Agent 可以在有界只读工具集中查询设计 RAG 文档，用于取得预期行为证据。
- **已实现**：Bug Agent 的 `query_design_evidence` 使用已安装且 ready 的版本化知识包 SQLite FTS，只返回
  受控 `BugEvidence`，没有任意文件读取、在线下载或仓库遍历能力；底层共享只读 reader 先按模型外固定的
  组件、安装版本和文档来源类型返回普通知识命中，Bug 链再转换成内部调查证据。工具每轮最多调用两次，
  并与源码、运行、日志证据保持不同 kind。知识包缺失或检索失败按无证据处理，不阻止 Bot 启动。
- **已验证**：Bug task 的全新 16 条真实 held-out 包含设计合同、源码与运行证据组合，citation closure 为
  1.000；合成测试覆盖缺失知识包与只读检索。当前 reader 消费发布知识包中的受控正文与 revision，NoneBot
  文档由部署侧已安装 distribution 绑定 `component + exact version`，错误版本不会命中；尚未实现针对
  authority / disclosure 的更细字段级检索策略，也没有将外部 bot-docs 工作目录直接接入线上路径。

## 关系

- 补充 [ADR-0050](0050-use-a-bounded-agent-for-user-bug-assessment.md) 的 Agent 证据工具集合；
- 复用 [ADR-0019](0019-distribute-rag-corpus-as-versioned-knowledge-pack.md) 的独立知识包、manifest、LocalStore
  和不可信文档边界；
- 不改变 [ADR-0026](0026-filter-capability-knowledge-before-retrieval.md) 的检索前受众隔离；
- 不扩大 [ADR-0038](0038-limit-semantic-assessment-remote-data-projection.md) 的语义分类出站域；Bug 判定 RAG
  若要出站，必须使用用途专属的数据策略和资格。
- 设计摘录的远端投影由 [ADR-0053](0053-allow-relevant-source-and-log-bodies-for-bug-assessment.md) 部分替代：
  通过独立 Bug task 资格门的相关正文可以出站，但知识包、authority、revision、applicability、Prompt 注入
  和普通用户披露边界继续有效。

## 相关文档

- [支持入口分流](../architecture/flows/support-intake-routing.md)
- [架构概览](../architecture/overview.md)
- [可选帮助数据源与复用边界](../architecture/help-source-adapters.md)
