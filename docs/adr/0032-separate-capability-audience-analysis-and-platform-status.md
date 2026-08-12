# ADR-0032：分离能力受众、平台范围与分析问题

| 状态 | 决策日期 |
|---|---|
| 已采纳 | 2026-08-12 |

## 当时遇到了什么

部署能力影子曾用 `public / review / restricted` 一个字段同时表达普通用户能否得知能力、平台范围是否
明确以及知识是否足以服务。nonemigut 的首次快照因此产生 62 条 `review`：其中 28 条已经有确定命令入口，
只是所属本地插件没有提供可判定的平台元数据；其余 34 条才是需要继续理解触发语义的 message / passive
Matcher。把这些情况都称为“待审核”既无法说明真实缺口，也会制造不必要的逐命令人工审批。

`ready / pending / conflicted` 也不能解决这个问题。它们会把多个可以同时存在的原因再次压缩成单一生命周期，
使平台未知、动态入口和证据冲突互相覆盖，并迫使查询方反推某个状态为何阻塞服务。

## 决策

1. 能力记录持久化四类互不推导的信息：
   - `disclosure` 只回答谁可以知道能力，取 `public / restricted`；
   - `platform_scope` 只回答能力属于哪些 adapter，使用 `all`、`explicit(adapters)` 或 `unknown`；
   - `analysis_issues` 保存当前仍未解决的具体知识问题；
   - `constraints` 保存权限、场景、限流、输入前提和无法静态求值的执行条件。
2. `platform_scope=explicit` 必须含至少一个规范 adapter spec；`all` 和 `unknown` 不携带 adapter 列表。
   `all` 表示已有正向证据支持跨 adapter，`unknown` 表示证据不足，二者不能互相推导。
3. 当前 `analysis_issues` 有以下六种稳定原因，并且都阻止进入普通用户服务视图：
   - `dynamic_entry`：只观察到动态、被动或非结构化入口，尚不能形成可靠用户调用合同；
   - `platform_unknown`：无法把能力归入确定的平台范围；
   - `evidence_conflict`：同一字段的可信证据互相冲突；
   - `sensitive_ambiguity`：公开与受限边界存在无法自动消解的敏感歧义；
   - `evidence_insufficient`：其他必要服务字段缺少足够证据。
   - `capability_mapping_unknown`：Matcher 事实与用户可观察能力的映射仍无法确定，由 ADR-0034 引入。
4. 不持久化 `analysis_status=ready / pending / conflicted`，也不保留 `review` 领域状态。维护者视图直接报告
   `analysis_issues`；空集合表示当前没有这些阻塞问题，不表示能力一定可执行。
5. `RecordState.VERIFIED / CANDIDATE / CONFLICTED / STALE` 保留，但只表达记录结构、证据聚合和新鲜度。
   它不承担披露、平台或分析待办语义，也不能单独决定能力是否可服务。
6. 普通用户 `ServingView` 在模型和召回前派生，而不是持久化。当前准入条件为：

   ```text
   disclosure == public
   AND platform_scope supports current adapter
   AND analysis_issues is empty
   AND record_state in {verified, candidate}
   AND served snapshot is complete (partial == false)
   AND served generation is fresh
   AND current deployment observation is registered
   AND local/editable module source manifest exactly matches snapshot evidence
   ```

   `platform_scope=unknown` 不支持任何普通用户 adapter；`explicit` 只支持列出的 adapter；`all` 支持当前
   运行 adapter。当前索引允许结构已校验的 `verified` 与尚有非阻塞字段待补的 `candidate` 进入；
   `conflicted / stale` 均不进入。普通 Permission、Rule、限流或 handler 条件即使仍为 opaque，也不会仅因
   存在就隐藏整条已公开能力；它们继续作为执行约束，并由原插件在真正调用时裁决。
7. 本地插件若无法判定平台，维护者应在插件根模块补齐 `PluginMetadata` 与 `supported_adapters`，这是本地
   能力进入自动服务视图的最小机器可读信息。Triage 不为本地插件维护逐插件 overlay，也不要求部署者逐条
   批准命令。第三方插件缺少元数据时可以由其他确定性证据源补足，否则保留 `platform_unknown`。
8. SQLite 能力索引升级为 schema v2。它是可删除重建的本地派生数据，不对 v1 做原地迁移；旧读端拒绝
   未知 schema，新代码在下一次刷新时原子重建。维护者 CLI 使用 `--include-unresolved` 纳入带
   `analysis_issues` 的能力，不再使用 `--include-review`。
9. JSON 快照及其嵌套 Claim、Evidence、Constraint、SourceRevision 同样是严格的内部 v2 合同；即使某个嵌套
   对象本轮字段没有变化，v1 也整体拒绝并由确定性扫描重建，不做混合版本读取或隐式补字段。它们尚不是
   对外承诺兼容的稳定交换 API。

## 为什么这样选

- 受众、平台、知识缺口和执行约束分别回答不同问题，拆开后不会因一个缺口隐藏整条记录的其他可靠字段；
- 具体 issue 可以自动消解、按原因统计和分派给静态分析或 LLM，不需要人工猜测 `pending` 的含义；
- 平台元数据由本地插件一次声明、所有能力复用，比逐命令批准或部署 overlay 更稳定；
- 派生 ServingView 能同时执行 ADR-0026 的模型前隔离与新鲜度门禁，又不会把“可说明”误称为“可执行”。

## 没有采用的方案

- **保留 `review`，只增加 review reason**：披露和知识问题仍耦合，普通查询仍需把 review 当特殊受众处理。
- **使用 `ready / pending / conflicted` 三态**：单值无法表达多个同时存在的问题，也会与 `RecordState` 的
  `CONFLICTED / STALE` 重复。
- **缺平台信息时默认当前 Bot 的全部 adapter**：运行进程加载了某 adapter 不证明插件实现支持它，会把
  OneBot 专属能力泄漏到 Discord 等域。
- **部署者逐插件或逐命令批准**：可以临时提高覆盖率，但把可从插件源码维护的最小事实转成长期人工负担。

## 带来的影响

- 明确命令不会仅因为 description、限流细节或其他非阻塞字段未知而进入人工审核；
- 动态入口等候 AST / LLM 分析时保留明确 issue，不会被误称为未公开能力；
- 维护者搜索、报告和统计需要按 `analysis_issues` 展示具体原因；
- v1 SQLite 不能被 v2 读端继续服务，部署刷新失败时仍保留最后一个同 schema 且完整的新鲜索引；
- `RecordState` 与 `analysis_issues` 可能同时显示 conflicted 含义：前者是记录聚合状态，后者必须包含具体
  `evidence_conflict` 原因，维护者输出不能只显示前者。

## 落实与确认

- 2026-08-12：领域记录、快照、SQLite v2、检索过滤和维护者 CLI 已按本 ADR 实现；普通用户查询只从上述
  派生 ServingView 召回，维护者输出直接列出 issue。
- nonemigut 已为首次快照中 28 条明确命令涉及的 9 个本地插件补齐根模块平台元数据，并通过不启动 Driver
  的插件加载与 metadata 断言；2026-08-13 的本地源码快照验证还确认：7 条原动态候选收敛为 5 项用户能力和
  2 条支撑关系，`dynamic_entry=0`、`capability_mapping_unknown=0`，快照完整。该离线验证不替代正式 Bot
  启动后的部署 generation 刷新。
- deployment 对齐已进入普通查询的派生 ServingView：完整刷新才创建同时绑定 snapshot / deployment generation
  的 alignment；逐能力要求当前 `registered`，并对 `local / editable / wheel / vcs` 制品比较快照与部署双方的同域模块
  源码 manifest。未注册、源码变化、证据歧义或缺失均逐条剔除；全局刷新失败或任一快照 / deployment
  partial 则整体失败关闭。wheel / VCS 缺少完整同域 manifest 时不会以版本、commit 或 `RECORD` 摘要
  推断成已对齐。
- 尚未落实：后台 LLM 语义编排、一般动态入口自动消解、字段冲突工作流、operator exclude policy，以及
  更广泛的语义知识接入。有界 handler AST 效果分析与 Matcher 角色归并的首阶段已由 ADR-0034 接入；
  这些其余缺口不改变本 ADR 的持久模型。

## 替代关系

- 落实并部分替代 [ADR-0024](0024-auto-publish-deterministic-capability-fields.md) 第 5 项尚未固定的持久模型；
- 保留 [ADR-0026](0026-filter-capability-knowledge-before-retrieval.md) 的模型前受众与 adapter 隔离，但以派生
  ServingView 取代 `review` 披露层；
- [ADR-0034](0034-distinguish-matchers-from-user-observable-capabilities.md) 进一步把 Matcher 运行事实与派生
  Capability 分开，并以 `capability_mapping_unknown` 扩展具体分析问题；本 ADR 的受众、平台、记录状态与
  ServingView 轴保持不变；
- 不改变 `restricted` 对普通用户不可发现，以及受限源码默认不进入模型的边界。

## 相关文档

- [部署本地能力影子索引](../architecture/flows/capability-shadow-index.md)
- [可选帮助数据源与复用边界](../architecture/help-source-adapters.md)
- [架构概览](../architecture/overview.md)
- [ADR-0034：区分 Matcher 事实与用户可观察能力](0034-distinguish-matchers-from-user-observable-capabilities.md)
