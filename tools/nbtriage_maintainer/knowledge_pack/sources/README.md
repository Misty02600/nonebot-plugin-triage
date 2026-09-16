# 默认知识来源边界

构建命令只消费已经固定 revision 的本地 checkout 或不可变快照；本目录不保存上游正文，也不把可变的
`main`、`latest` 写成版本。默认库存只保留一个已验证、冻结的来源：

- NoneBot2：插件支持范围内的官方用户/API 文档、迁移说明以及官网 Alconna/UniSeg 教学；实际运行源码由
  部署本地、绑定安装 revision 的只读 FileSystem / Jedi 证据工具按需读取，不在知识包重复维护完整副本。
NapCat、Uninfo、OneBot Adapter 和 OneBot v11 不进入默认知识来源，也不维护对应采集器或专项检索评测。

source policy 里的 `distribution` 只有 `redistributable` 和 `local_only`。它是来源级发布约束，不进入每条
检索证据。来源未完成许可复核时使用 `local_only`。

## 本地检索实践

索引采用 SQLite FTS5。每次从已验证快照全量构建一个临时数据库，运行 SQLite 与 FTS 完整性检查后才原子
替换旧索引；失败时旧索引保持可用。查询先按组件、目标版本和来源类型过滤，再用 FTS5 `bm25()` 排序。
小型公共语料不做增量写索引：全量重建可以自然清除上游删掉的页面，也避免新旧 revision 混合。

格式 2 查询保留原查询前两名，第三位最多补一个片段。`identifier-context-v2` 优先检查这两条的同来源、同文件
直接父节，只有父节在可见字符范围内包含未覆盖的中文问题词及相关 API 才补入。否则从标识符拆词查询的前
20 个候选中，优先选择标题对应主 API、正文同时提及其他所问 API 的片段；没有这样的候选时保留拆词前三名
内选首个未重复片段的规则。完整短符号保留，拆出来的 on/get/arg 等短词不用于联系 API；共享词根的名称合组。
这是词法相关性规则，不推断调用关系，也不维护 API 含义或题号映射。所有查询保持版本、组件和来源过滤，
不改写索引。`evaluate --strategy bm25` 可复放原排序；格式 1 不启用此增强。

## 教学文档检索评测

复用本目录 `evaluate` 入口和生产 `KnowledgeIndexReader`，确保维护评测与产品检索使用同一实现。
`evals/datasets/fixtures/framework-docs-v106-dev.json` 是人工复核的 36 条冷测真实查询开发集；它已参与诊断，
不能称为 held-out。只保存公开 API 查询及定位/短事实，不含用户消息、凭据、配置或插件源码。

```powershell
uv run --group maintainer python -m tools.nbtriage_maintainer.knowledge_pack evaluate --index <冻结索引路径> --fixtures evals/datasets/fixtures/framework-docs-v106-dev.json --strategy bm25 --report reports/rag/baseline.json
uv run --group maintainer python -m tools.nbtriage_maintainer.knowledge_pack evaluate --index <同一冻结索引路径> --fixtures evals/datasets/fixtures/framework-docs-v106-dev.json --report reports/rag/candidate.json
```

Schema 1 兼容原先的章节定位合同。Schema 2 默认对齐教学工具：`user_docs`、精确版本、前 3 条、每条 1800 字符。
每题 `answer_groups` 中每组至少命中一个候选，且候选的**完整 locator 相同、所有 required_text 都在实际片段中**，
才算覆盖答案；同组为可替代材料，多组表示需共同提供的事实。`answer_mrr` 采用覆盖全部组所需的最末名次。
这验证策展事实是否被返回，不替代自然语言答案正确性或因果评估。

`answerable` 的已执行问题进入答案命中率分母；`corpus_gap`、`unjudged` 分别统计，不从返回无关片段推断有答案。
`budget_blocked` 的原始调用不执行回放、不算检索失败。报告含分类指标、查询、实际片段、Fixture/语料/代码摘要及
有效排序修订；语料摘要不符时拒绝运行，避免旧的缺失标签套用到新增文档。真实重复查询保留，类别指标同时报告，
总分按调用统计，不代表均衡的 API 能力覆盖。

2026-09-14 同索引回放：18 条已标注且执行的可回答查询，答案 Hit@3 从 BM25 的 9/18，经首轮拆词补充的 15/18，
提升到上下文补充的 18/18；regex 输入表示子类为 2/10 → 8/10 → 10/10。Q25/Q31 在原金标下补回；Q16 返回了
会话控制父节中明确的“事件处理流程按照事件处理函数添加顺序执行”，因此在保持两组事实要求的前提下，
将该父节加入顺序事实的等效答案。用更新后的同一金标复评，原 BM25 仍为 9/18，上一轮保存片段仍为 15/18。
这些结果只说明已参与开发的查询回归集得到修复，不能声称未知查询也全对。
另有 5 条确认语料缺失、9 条未完成答案标注、4 条预算阻止。源码导航回退已有提示约定，当前不新增 API 解释库、
reranker 或预算。生成质量的有／无文档对照按用户要求暂缓，见
[PLAN-0021](../../../../docs/plans/todo/PLAN-0021-compare-teaching-without-framework-docs.md)。

`revision` 只记录上游 Git commit；`snapshot_sha256` 只验证本地所选文件没有变化，二者不能互换。
稳定 `source_id` 与片段摘要用于来源追溯，不把摘要当产品版本。Markdown 使用
`markdown-it-py` 的 token/source map 按标题切分。构建器不再支持 OpenAPI、TypeScript、Release Notes 或
源码快照；精确实现证据由部署环境的只读源码导航提供。首版不使用
向量库、运行时联网或任意仓库扫描。

维护入口：

```powershell
uv run --group maintainer python -m tools.nbtriage_maintainer.knowledge_pack.acquire.nonebot --version 2.5.0 --output ... --metadata-out ...
uv run --group maintainer python -m tools.nbtriage_maintainer.knowledge_pack prepare-policy ...
uv run --group maintainer python -m tools.nbtriage_maintainer.knowledge_pack build ...
uv run --group maintainer python -m tools.nbtriage_maintainer.knowledge_pack search ...
uv run --group maintainer python -m tools.nbtriage_maintainer.knowledge_pack evaluate ...
uv run --group maintainer python -m tools.nbtriage_maintainer.knowledge_pack package --index ... --output ... --version ...
uv run --group maintainer python -m tools.nbtriage_maintainer.knowledge_pack verify-package ...
```

`package` 只接受全部来源都标为 `redistributable` 的索引，输出包含 `manifest.json` 和
`index.sqlite3` 的 ZIP，并返回归档 SHA-256。插件默认从冻结的 stable catalog 发现现有资产，也可以同时配置
精确 HTTPS 资产 URL 与该 SHA-256 固定实验或镜像版本；缺失或下载失败只回退到旧包或无知识库模式。

现有正式资产 `knowledge-v2026.08.1` 与 `knowledge-stable/catalog.json` 保持冻结。仓库不再维护自动发布
工作流；本节命令用于核验来源合同、构建同类 NoneBot-only 索引，或在明确重新开启知识包维护时作为最小
构建基础。现有资产的逐字节重建仍以其不可变 tag 中的原构建代码为准。重新发布前仍须复核
`project_revision`、构建器摘要、来源再分发状态、归档/索引摘要和 SQLite 完整性。

NoneBot2 使用仓库内采集器下载固定 commit 的官方 GitHub ZIP，只保留对应版本的 `versioned_docs` 与用于
完整性核对的 sidebar；文件数、关键 API 页面和版本不匹配时直接拒绝。
Alconna/UniSeg 教学随 NoneBot2 的版本化文档采集，不再维护独立文档源；精确实现以部署环境安装源码为准。

教学基础资料由 `_bootstrap_docs.py` 中的文件/标题选择器从同一个索引读取原文，不另存摘要。
NoneBot 固定预载事件处理、响应器组成、依赖注入基础、类型重载、权限和会话控制；Alconna 按 Runtime
分类或 family 解析器事实加入参数、选项、解析结果、响应器和条件控制。资料完整时替换对应旧说明；缺失时
保留原回退行为。Uninfo 的确定性语义与部署源码导航继续存在，但不再从知识包预载或检索其 README。
AntiPattern 的旧说明
目前保留，因为这些上游选段没有提供等价定义。新增工具插件先接入公开文档来源和选段，不能只添加 API 解释表。

预载原文置于任务动态信息之前，参与请求指纹和知识包失效校验；同区块同 revision 已提供的正文再次召回时
返回 `already_available` 和 Evidence ID，更长或不同区块仍返回正文。首包可选源码仍使用 64k 软目标，
完整请求另按模型窗口、输出预留和 10% 余量检查；模型目录不识别窗口时不发送扩大的文档上下文。
维护测评记录稳定前缀摘要、文档清单、估算与 Provider 的 cache read/write tokens；前缀相同不代表实际已命中。

### 2026-09-14 上下文静态审视与保留决定

当前保留较完整的上游基础章节。基础文档帮助模型识别依赖注入、类型约束和执行控制等概念；当前插件的
源码、Runtime 和配置事实决定实际行为；文档检索与源码导航只补充仍未解决的具体问题。不以每轮调用 RAG
或最终直接引用文档作为成功条件，不新增手工概要层、API 含义表或逐字段提示补丁。

- NoneBot 的依赖注入和会话控制选段较宽，但其中的校验、依赖形式与执行控制会影响代码解释，符合先保留
  较大版本的选择，暂不为压缩长度拆除这些内容。
- Alconna 的参数、解析结果、注入及条件控制文档用于解释 handler 行为；Runtime 生成 usage 只解决当前命令
  结构的展示，不能替代这些语义资料。
- Uninfo 等插件语义只使用确定性 profile、当前插件源码和部署环境安装源码，不维护额外上游文档副本。

已知非阻断问题包括 Evidence 分块及来源元数据较多、原文保留 MDX 标记与不同语法示例，以及核心指令和工具
说明存在部分流程提示重复。不同语法示例不直接视为冗余；若以后确需整理，优先合并重复流程提示，暂不为此
重构上下文或加入新的分析指南。

现有真实运行首轮输入约 47k tokens，未逼近当时所用模型目录声明的 1M 窗口，并观察到实际缓存读取；这仅支持
容量与缓存可用性，不能证明每段内容相关或教学质量改善。该轮误用生产累计预算导致提前收尾，不作为否定
原文预载的依据。效果判断优先看框架语义理解和教学结论，再看额外检索是否补足缺口。

本次按静态审视保留现状，不追加专项验证、消融或重构，也不把上述局限转成新增待办；既有暂缓对照计划
继续暂缓。本决定不宣称已完成文档收益的因果验证。
