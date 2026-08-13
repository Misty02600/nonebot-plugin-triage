# ADR-0026：在检索与模型前隔离能力知识受众域

| 状态 | 决策日期 |
|---|---|
| 已采纳；回答投影由 ADR-0027 细化 | 2026-08-12 |

## 当时遇到了什么

部署能力影子会保存公开候选、待审核记录和受限管理能力；后续还计划用源码、README、配置 schema 与
LLM 补全功能语义。如果先把全部记录交给词法 / 向量检索、源码搜索工具或模型，再要求回答时隐藏，模型
仍可能通过候选排序、工具结果或自然语言暗示受限命令和其他适配器专属能力存在。

普通用户也不需要知道“某功能仅限 SUPERUSER”或“另一个平台存在同名功能”。这些信息本身就是披露内容。
另一方面，同一 adapter 内的群聊 / 私聊只是使用场景：用户可以在私聊询问一个需要在群里使用的公开功能，
系统只需自然说明使用前提。

## 决策

1. 每次普通用户请求先在模型外建立受众域与 adapter 域，再进行能力候选召回。普通用户域只包含当前
   adapter 支持、已经允许 `public`，并通过本轮完整快照与 deployment 门禁的能力。
2. `review`、`restricted`、hidden、SUPERUSER-only、operator deny 和其他 adapter 专属能力不得进入普通
   用户的 FTS、向量召回、Agent 源码搜索或 LLM 上下文。精确询问这些能力时与真正不存在使用同一通用
   未命中回复，不说明权限不足、受限能力或其他平台实现存在。
3. 输出门保留第二层校验：即使模型或工具异常返回了不在当前域的能力，也拒绝其名称、语法、存在性和
   限制原因，不能依赖提示词保密。
4. 已经确定某能力为 hidden / SUPERUSER-only / restricted 后，其源码 EvidenceUnit 默认也不发送给 LLM。
   SUPERUSER 身份只允许进入当前确定性维护者视图；若维护者以后确实需要模型深查受限能力，必须使用独立、
   显式授权的诊断动作，并重新定义数据发送和回答投递边界。
5. adapter / protocol 是帮助披露的硬隔离键；群聊、私聊和 Bot `self_id` 不是。公开能力可以跨同一 adapter
   的会话场景询问，回答只说明“需要在群里使用”等可观察前提，不主动强调 adapter 名称。
6. 对已经可见的 public 能力，权限、配置、限流和输入要求可以投影成自然、面向用户的行为说明。例如
   “请回复一张图片再发送 `搜图`”“刚刚用得有点频繁，稍等一会儿再试”或“这个功能目前还没启用”。普通
   回复不展示底层 Permission、限流库、配置字段、内部后端或证据状态术语。

## 为什么这样选

- 过滤发生在数据进入检索器、工具和模型之前，避免把保密责任交给概率模型；
- 对受限能力与其他 adapter 能力使用不可区分的未命中回复，避免存在性侧漏；
- 保留同一 adapter 内跨场景问法，不会为了披露安全损害正常的指令教学；
- SUPERUSER 的当前确定性审计能力仍可使用，但不会自动扩大未来 LLM 的源码读取范围。

## 没有采用的方案

- **先检索全部能力，再删掉回答中的敏感词**：候选和上下文本身已经泄漏，且无法可靠清除语义暗示。
- **告诉普通用户能力只限 SUPERUSER 或只在其他平台可用**：这仍然披露了受限能力或跨平台能力的存在。
- **SUPERUSER 自动允许受限源码进入模型**：身份鉴权与源码外发是不同授权链，不能互相推导。
- **按群聊 / 私聊完全隔离帮助目录**：会阻止用户在私聊询问公开的群聊功能，不符合指令教学需求。

## 带来的影响

- 普通用户的未命中回复不能区分“没有能力”“能力受限”“只在其他 adapter 存在”；
- ServingView 必须先于词法 / 向量检索和任何 Agent 源码工具构造，不能只作为回答后处理；
- 能力知识的索引可以继续保存受限记录，但普通用户查询面不能读取这些记录及其源码证据；
- 限流、配置和场景提示只对已经 public 的能力生成，并采用用户可理解的自然话术。

## 落实与确认

- **2026-08-12 实现检查点**：普通用户已能检索自动公开候选；当前 adapter 的允许能力 ID 会在 SQL 召回与
  `limit` 前应用，stale generation 失败关闭，`review / restricted` 不进入普通用户候选。受控源码
  EvidenceUnit、默认拒绝受限源码、严格模型输出 schema 与证据引用闭包已有库级实现。
- **2026-08-13 deployment 完整性门**：普通域要求本轮 snapshot 完整且新鲜，并要求 deployment inventory
  成功且完整。受众、adapter、analysis issue 和记录状态白名单在 SQL 排名与 `limit` 前应用；进程重启、刷新
  异常或成功后再次失败都会使普通查询失败关闭，快照索引与维护者检索仍可用。逐记录源码 manifest 对齐由
  [ADR-0036](0036-keep-capability-shadow-deterministic-and-record-oriented.md) 移除。
- **尚未实现**：真实模型语义层、向量召回、Agent 源码搜索、持久语义知识和更完整的字段级 ServingView。
  当前在线帮助仍是确定性格式化，且热加载自动刷新
  尚未实现；不能把库级假模型测试描述成已上线模型行为。
- 本 ADR 记录产品与安全边界，不授权创建实施计划或启动新的模型调用。

## 替代关系

- `review` 作为披露层的历史表达由
  [ADR-0032](0032-separate-capability-audience-analysis-and-platform-status.md) 收敛为具体 `analysis_issues`；普通
  ServingView 由受众、平台范围、issue、记录状态与 generation 新鲜度共同派生；
- 收紧 [ADR-0022](0022-limit-capability-shadow-guidance-to-superusers.md) 的后续模型边界：保留现有维护者确定性
  检索，但 SUPERUSER 不再被视为受限源码默认进入 LLM 的授权；
- 补充 [ADR-0025](0025-explain-plugin-behavior-from-deployment-evidence.md) 的受众与 EvidenceUnit 边界；
- 不改变 [ADR-0020](0020-use-triage-command-for-natural-language-support.md) 的 `triage` 入口和 Reply / @ 可选语义。

## 相关文档

- [架构概览](../architecture/overview.md)
- [部署本地能力影子索引](../architecture/flows/capability-shadow-index.md)
- [可选帮助数据源与复用边界](../architecture/help-source-adapters.md)
- [Alconna 能力与解析回执](../architecture/flows/alconna-capability-and-parse-receipts.md)
