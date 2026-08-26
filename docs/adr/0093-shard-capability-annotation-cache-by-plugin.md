# ADR-0093：按插件分片教学注释缓存并按单元部分发布

> 后续关系：[ADR-0096](0096-bound-capability-annotation-concurrency-by-unit.md) 替代本 ADR 沿用的“不同插件并发、
> 同一插件内顺序分析”调度边界；本 ADR 的插件 cache shard、单元状态、staging 与全局原子 pointer 继续有效。
> [ADR-0121](0121-checkpoint-completed-teaching-units-before-atomic-publication.md) 进一步允许把已完成但未发布的
> 单元候选作为 cache checkpoint 持久化；本 ADR 的活动指针与原子发布边界不变。

| 状态 | 决策日期 |
|---|---|
| 已采纳 | 2026-08-19 |

## 当时遇到了什么

教学注释已经按插件有限并发、在插件内按确定顺序分析，但旧缓存仍把全部插件的结果放在同一逻辑边界里，
活动视图也要求整轮所有分析单元都成功。一项暂时性的 Provider 或输出校验失败因此会阻止其他已经取得
有效结果的单元进入新 generation；全局缓存读写还会把互不相关插件的失败与重写范围耦合在一起。

另一方面，失败重试需要同时回答两个不同问题：“最近一次可以信任的完整教学结果是什么”和“最近一次生成
尝试发生了什么”。只保存一个当前值会让失败覆盖可用结果，或者让运维侧看不到刚发生的失败。把候选直接
写成活动状态又会让 Answer 内存视图与 YAML / Markdown 文件在发布失败时来自不同轮次。

项目仍接受 [ADR-0058](0058-use-deterministic-evidence-and-bounded-navigation-for-teaching-annotations.md)
规定的保守首版源码失效边界：只要一个插件的受管源码 revision 变化，就重生成该插件当前全部教学单元，
不依赖尚不完整的逐单元调用图判断哪些旧结果可能继续有效。

## 决定

### 持久化缓存按插件分片

1. 教学注释缓存使用 Triage LocalStore cache 下的 `capability-annotations/` 目录，每个插件恰好对应一个
   `<module_name>.json`。文件记录 `schema_version`、原始 `module_name`、单一
   `plugin_source_revision`、实际发布它的 `published_generation`，以及以稳定 teaching unit ID 为键的
   `units` 对象。只含失败诊断、尚无任何 `last_good` 的分片允许 `published_generation=null`。
2. 每个 teaching unit 在同一插件文件内分别保存：
   - `last_good`：最近一次通过 schema、Evidence、公开投影与 fingerprint 校验的完整结果；合法的
     `knowledge_enabled=false` 也属于一个完整的保守关闭结果，但不会成为活动知识；
   - `last_attempt`：最近一次真实生成尝试的状态、阶段、请求 fingerprint、稳定失败原因与有界尝试次数。
     它只用于诊断和刷新决策，不是公开能力事实。
3. 失败的 `last_attempt` 不覆盖 `last_good`。只有分片的 `published_generation` 与当前 `current.json` 精确
   一致，且 `last_good` 的请求 fingerprint、插件源码 revision 与动态 Evidence manifest 都仍匹配本轮输入时，
   才可以作为缓存结果继续进入候选；不匹配的旧结果最多按
   [ADR-0077](0077-use-previous-generated-teaching-content-as-a-non-evidentiary-baseline.md) 作为
   `previous_annotation` 编辑基线，不能继续服务或充当 Evidence。
4. 单个插件 JSON 通过临时文件、同步落盘和 `os.replace` 原子替换。各插件缓存是可删除重建的派生状态，
   不与其他插件缓存或活动 generation 组成跨文件事务；缓存缺失或落后只会导致后续重算，不能改变已经由
   `current.json` 选中的活动内容。指针切换后若缓存写入失败或进程退出，旧分片会因 generation 不匹配而失效，
   不会在重启后把旧教学内容重新发布。

### 候选只在内存 staging，按 teaching unit 部分发布

5. 一次刷新继续持有全局 refresh lock。不同插件按既有限制并发，同一插件内按确定顺序分析；现有发布锁与
   缓存写入串行化边界保持不变。刷新得到的候选注释、fingerprint、capability 到 teaching unit 的映射和
   待写插件缓存先组成仅存在于进程内的 staging，不创建持久化 staging 文件或目录。
6. 发布资格按 teaching unit 判断：
   - 本轮生成成功，或 `last_good` 仍精确匹配当前输入的单元，可以进入候选活动视图；
   - `failed`、`stale`、`skipped` 或合法关闭的单元不提供公开知识；
   - 一个单元失败不再阻止其他插件或同插件其他仍可信单元发布。generation manifest 必须记录单元状态及
     每个插件的 `active / eligible` 覆盖量，使部分发布可被观察而不会伪装成完整知识。
7. 若任一单元在准备或 Agent 运行期间报告 `SOURCE_CHANGED`，本轮该插件已经产生和复用的全部 staging
   候选立即作废，插件内后续单元不再继续分析。本轮不能混合该插件变化前后的源码 Evidence；其他插件的
   候选仍可按各自结果发布。只有会破坏共享 snapshot、Provider / schema 身份或输出发布可信度的全局失败，
   才继续丢弃整轮 staging 并保留上一活动 generation。
8. 同一份内存 staging 同时生成 help-display YAML、answer-knowledge Markdown 和 manifest，并先写入新的
   不可变 generation。全部文件校验完成后，只通过原子替换全局 `capability-teaching/current.json` 激活；
   指针切换成功后才提交对应 Answer 内存视图。文件写入、generation 校验或指针切换失败时丢弃 staging，
   上一 `current.json` 与上一内存活动视图继续有效。当前已没有可发布单元时，空 generation 也是合法结果，
   用于让已经删除或失效的旧教学内容退出活动视图。
9. `current.json` 是教学知识唯一的活动指针。插件缓存中的 `last_good`、`last_attempt`、内存 staging、
   `last-refresh.json` 和未被指针选中的 generation 都不是
   [ADR-0066](0066-use-active-teaching-contract-as-bug-precheck.md) 所说的 active teaching contract。

### 插件变化与文件名失败关闭

10. 首版继续使用插件级源码 revision：缓存文件中的 `plugin_source_revision` 与当前值不同时，该插件所有
    teaching unit 的 `last_good` 都不得直接复用，当前仍符合准入条件的单元必须全量重生成。实现不按上次
    读取文件、调用关系或单元内容 hash 猜测“未受影响”的旧结果。
11. 缓存文件直接使用安全的 `module_name.json`。安全名称必须是有界的点分 Python module name，每一段都是
    非关键字 identifier，且不能命中平台保留文件名；文件内 `module_name` 还必须与请求名称一致。非法或
    当前平台无法安全表示的 module name 只让该插件的教学缓存与输出失败关闭，不影响其他插件、确定性能力
    索引或 Bot 启动。同轮若两个名称在大小写不敏感文件系统上对应同一文件名，则两者都失败关闭，不改写成
    hash 文件名，也不阻止无冲突插件发布。

## 为什么这样选

- 插件已经是并发、源码 revision、强制刷新、日志和两类输出文件的共同边界，沿用它分片不需要引入另一套
  持久化身份；
- `last_good` 与 `last_attempt` 分离后，短暂失败既不会抹掉仍然精确有效的结果，也不会被一个“看似成功的
  当前值”隐藏；
- teaching unit 是现有 Evidence、fingerprint、参数化 family 与失败关闭的不可拆分边界，按它部分发布比按
  整轮失败更符合已有领域模型；
- 内存 staging 与单一活动指针把“计算出候选”和“已经对用户服务”明确分开，同时保留 YAML、Markdown 与
  Answer 内存视图的一致切换；
- 缓存是可重建加速层。允许缓存与活动 generation 在崩溃后短暂不同步、再通过重算恢复，比为派生文件引入
  跨文件事务更容易验证和运维。

## 没有采用的方案

### 为非法 module name 使用 hash 文件名或维护 module 到文件名 manifest

本轮不增加 hash fallback，也不维护 `module_name -> filename` manifest。两种方案都要额外处理映射文件的
原子性、碰撞、丢失、迁移与人工定位；当前公开教学只接受能够直接安全落盘的 Python module name，非法名
按插件失败关闭更清楚。

### 每个 teaching unit 一个文件

这会把一次插件刷新扩展成大量小文件的创建、清理和孤儿检测，并且仍然需要插件级源码 revision 与单元枚举
元数据。一个插件 JSON 内按 unit 分区已经提供所需的失败隔离与可观察性。

### 持久化 staging、journal 或 SQLite

候选在一次受锁刷新内完成，活动内容由不可变 generation 与原子 pointer 选定。为可重建缓存增加草稿目录、
WAL / journal 或 SQLite 事务会制造第二套恢复协议，却不能替代最终输出 pointer 的发布语义。

### 为插件缓存、双输出和活动指针实现跨文件两阶段提交

本轮不实现跨文件 2PC。崩溃最多让缓存落后并触发重新分析；只要 `current.json` 没有切换，旧活动内容就不变，
而指针已经切换时该 generation 本身已经完整校验。以可恢复的额外计算换取更小的状态机。

## 带来的影响

- 有利：一个 teaching unit 的临时失败不再阻塞所有有效单元，公开文件会显式显示每个插件的部分覆盖量；
- 有利：失败尝试和最后可用结果可以同时保留，维护者能够区分“正在沿用精确匹配的旧结果”和“没有当前
  可服务知识”；
- 有利：`SOURCE_CHANGED` 的爆炸半径限制在单插件，同时继续保证该插件不会混用两代源码；
- 代价：插件任意受管源码变化仍会全量重生成该插件，可能重复调用与变化无关的 teaching unit；
- 代价：缓存不是活动真值，也没有跨插件原子快照；崩溃或缓存写入失败可能增加下一轮模型调用，但不会把
  未完成候选升级为 active teaching contract；
- 代价：无法安全直接表示的 module name 没有兼容文件名，会失去该插件的模型教学增强并留下稳定失败状态。

## 落实与确认

- `capability_annotation_cache.py` 负责插件 JSON schema、`last_good / last_attempt` 校验、安全文件名和单文件
  原子替换；
- `CapabilityAnnotationService` 负责全局 refresh lock、插件内顺序、内存 pending / active 视图、单元状态和
  `SOURCE_CHANGED` 插件级 staging 作废；
- `CapabilityTeachingOutputWriter` 负责不可变 generation、单元与插件覆盖 manifest，以及全局
  `current.json` 原子切换；
- 对应验证边界位于 `test_capability_annotation_cache.py`、`test_capability_annotations.py`、
  `test_capability_shadow.py` 和 `test_capability_teaching_outputs.py`。

## 替代关系

- 部分替代 [ADR-0088](0088-bound-capability-annotation-concurrency-by-plugin.md) 决策第 4 项中“任一单元失败
  都不激活半套 Answer 视图”的整轮失败边界，并具体规定成功缓存的按插件 JSON 形态；ADR-0088 的插件间
  有限并发、插件内顺序、全局 refresh lock、缓存写入锁、Runtime 准入和全局输出 pointer 继续有效。
- 延续 [ADR-0058](0058-use-deterministic-evidence-and-bounded-navigation-for-teaching-annotations.md) 的
  Evidence、导航、插件级源码 revision 与首版全量重生成边界。
- 不改变 [ADR-0066](0066-use-active-teaching-contract-as-bug-precheck.md) 的 active teaching contract；只有
  `current.json` 实际激活且仍通过当前 ServingView 的教学单元获得合同地位。
- 不改变 [ADR-0069](0069-separate-help-display-from-answer-knowledge-and-bound-static-analysis.md) 的
  help-display / answer-knowledge 双投影与单一 generation，也不改变
  [ADR-0077](0077-use-previous-generated-teaching-content-as-a-non-evidentiary-baseline.md) 的非证据基线边界。

## 相关文档

- [能力影子索引流程](../architecture/flows/capability-shadow-index.md)
- [可选帮助数据源与复用边界](../architecture/help-source-adapters.md)
