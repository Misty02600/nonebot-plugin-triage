# PLAN-0015：评测并选择本地知识包与 Context7 设计知识后端

| 状态 | 最后更新 |
|---|---|
| 进行中 | 2026-08-15 |

## 背景

Bug Agent 当前通过 `search_design_rag` 查询框架与项目设计证据，运行时只接入经过 SHA-256 和 manifest
校验、缓存到 LocalStore 的本地版本化知识包。Context7 已收录 NoneBot 2，并提供按 `libraryId + query`
查询文档片段的远端 API；官方 API 支持版本化 library ID，但具体库是否存在与当前部署完全匹配的版本仍需
在资格评测时确认，不能从 `Latest` 页面推断。

本计划用于比较两种后端并保留部署选择空间。项目作者已确认：最终可能同时保留本地知识包与 Context7，
也可能只采用其中一个；采用组合模式时的顺序，以及先实施哪一条路径，后续根据验证结果再决定。本计划
不把任一方案写成已采纳架构，也不授权真实 Context7 请求、API 费用或生产数据出站。

这两种后端只提供“设计文档与上游公开合同”证据。源码、关联日志、运行回执和安全部署投影仍是不同证据
类别，不由知识后端配置切换，也不能用文档检索结果证明某段代码实际执行或某次故障已经发生。

## 当前设计与缺陷

### 已有实现

- `src/nbtriage/bug_agent.py::search_design_rag` 是 Agent 当前唯一的设计知识工具，底层来源没有暴露给模型。
- `src/nonebot_plugin_triage/bug_assessment_runtime.py::BugAssessmentRuntimeService.assess` 内部的
  `design_loader` 只读取本地 `KnowledgePackService`，再调用 `BugDesignIndexReader.search`。
- `src/nonebot_plugin_triage/knowledge_pack_runtime.py::KnowledgePackService` 下载并校验配置的知识包，使用
  LocalStore cache 保存索引；安装或校验失败时退化为无知识模式。
- `src/nonebot_plugin_triage/config.py::NBTriageConfig` 只暴露
  `NBTRIAGE_KNOWLEDGE_PACK_URL` 与 `NBTRIAGE_KNOWLEDGE_PACK_SHA256`，尚无 Context7 或后端选择配置。
- `src/nbtriage/bug_design.py::BugDesignIndexReader.search` 已支持 `component`、`version` 与 `limit`，并区分
  `snapshot_only`、`exact_version` 和 `declared_range`。

### 已确认缺口

1. **本地精确版本检索已接通。** NoneBot 2.5.0 官方版本化文档已经进入默认 source inventory；运行时从
   已安装 `nonebot2` distribution 取得精确版本，并以模型外 `component=nonebot2 + version` 查询知识包。
   版本不匹配时不会把其他版本文档冒充当前合同，未版本化的 `snapshot_only` 资料仍作为独立回退域。
2. **缺少后端选择边界。** 当前 `search_design_rag` 与本地 SQLite 实现直接绑定，不能在不修改运行接线的
   情况下切换 Context7，也没有显式表达“只用本地、只用远端、按固定顺序回退或完全关闭”。
3. **Context7 尚未达到项目证据门槛。** 仍需验证 NoneBot 2.5.0 及其他组件是否存在真正可查询的精确版本，
   返回结果能否稳定提供来源与版本依据，以及限流、超时、费用、结果漂移和不可用时的失败语义。
4. **远端 query 需要单独投影。** Context7 官方说明会接收构造后的 `query`、`libraryId`、认证与客户端元数据，
   并可能使用外部模型重排及保存匿名查询用于质量改进。用户原文、源码、日志、配置、身份和关联 ID 不能
   直接复用为 Context7 query。
5. **两种检索结果不能直接混排。** 本地 FTS 排名和 Context7 返回顺序没有共同分数含义；若采用组合模式，
   必须由配置决定固定调用顺序，并分别保留 provider、component、version、locator 和 freshness。

## 技术路线

### 稳定入口与证据合同

- Agent 继续只调用 `search_design_rag(query)`，不得获得 `search_local_rag`、`search_context7` 等平行工具，
  也不得自行选择后端。
- 在模型外根据部署中实际安装的 distribution 与版本解析 `component + exact_version`；用户文字和模型输出
  不能覆盖版本或任意构造 Context7 library ID。
- 两种后端均返回现有 `BugEvidence`，保留来源、定位信息、版本或 revision、`current` 与 `partial`；无法
  证明版本匹配时不得标记为当前部署证据。
- 文档证据只用于建立预期合同。最终 Bug 判定仍需按现有 reconciler 与源码、日志、运行事实等证据边界
  处理，不因某个文档后端命中就自动升级结论。

### 候选配置语义

后续若决定同时支持多个后端，优先采用一个明确表达顺序的选择字段，而不是让模型动态选择：

```text
NBTRIAGE_DESIGN_KNOWLEDGE_MODE=local_pack | context7 | local_then_context7 | off
```

- `local_pack`：只使用经过校验的本地知识包；不可用或无命中时返回无该类证据。
- `context7`：只使用 Context7；精确版本不存在、凭据缺失、限流或失败时返回 unavailable，不静默改用
  `Latest` 或本地后端。
- `local_then_context7`：先查本地精确版本证据；只有本地不可用或确定性零命中时才查询 Context7。两端
  结果不按分数混排，也不因模型判断“答案不好”而动态切换。
- `off`：不提供设计知识证据；不得影响源码、日志和运行回执工具。

选择字段只控制调用策略，不取代后端自身配置：本地包继续需要 URL 与 SHA-256；Context7 API key 只从
环境读取，不进入 `NBTriageConfig`、日志、证据正文或 Bot 回复。具体字段名、是否作为 optional extra 以及
缺失配置是启动失败还是逐轮 unavailable，待选定实施方案后冻结。

### 分阶段核查与实施候选

1. 先修复本地 reader 的 `component/version` 接线，并建立本地精确版本检索基线。
2. 为现有 `design_loader` 提取最窄的模型外调用边界；不创建复制 HTTP 客户端或 Pydantic AI 能力的通用
   provider 框架。
3. 使用 Context7 官方 HTTP API 实现只读窄适配，输入只包含经投影的公共文档 query 与可信 library ID；
   输出转换为现有 `BugEvidence`。
4. 用同一份全新 held-out 比较本地知识包、Context7 和候选顺序模式，至少统计版本正确率、检索
   Recall@k、引用闭包、无答案处理、P95 延迟、费用、限流与失败率。
5. 根据评测和部署成本决定最终保留模式及第一实施切片；形成长期部署决定后再创建 successor ADR，并
   同步 README、架构概览和运行流程。

### 非目标

- 不把 Context7 MCP 原始工具面直接交给在线 Bug Agent。
- 不用 Context7 替代源码阅读、Jedi/Serena 导航、关联日志或 RuntimeObservation。
- 不索引或发送私有源码、原始日志、配置值和用户会话数据给 Context7。
- 不在本计划阶段授权 Context7 私有源、企业版、外部模型费用或真实生产流量。
- 不把 `Latest`、master 文档或最近版本自动冒充当前安装版本。

## 待确认事项

### D-001 · P1 需要讨论：最终部署采用哪种知识后端组合

需要在评测后选择 `local_pack`、`context7` 或 `local_then_context7`。本地知识包强调精确版本、离线与可复现；
Context7 强调维护便利和覆盖更新，但增加网络、隐私、版本可用性与服务漂移风险。当前不设置静默默认答案。

## 已确认事项

- 2026-08-15：先实施本地 NoneBot 2.5.0 官方文档采集、知识包构建与精确版本运行接线，再把它作为
  Context7 的稳定比较基线；这不提前决定最终只保留本地还是采用组合模式。

## 实施进度

- 仓库采集器固定官方 NoneBot 2.5.0 revision，只保留 92 篇版本化 Markdown / MDX 与 sidebar；默认
  inventory 将其声明为 `nonebot2 / exact_version / 2.5.0`。
- Bug assessment runtime 已把当前安装的 `nonebot2` distribution 版本传给 `BugDesignIndexReader`；Agent
  仍只看到原有 `search_design_rag(query)`，不能自选组件或版本。
- 一条端到端测试覆盖“采集快照 → 生成 source policy → 构建 SQLite FTS5 → 2.5.0 命中、2.5.1 拒绝”；
  Agent 工具、runtime 版本绑定、reader 与知识包构建相关定向测试共 27 条通过。

## 完成标准与验证

| 验收项 | 覆盖条件或输入 | 预期结果 | 验证方式 |
|---|---|---|---|
| 本地版本接线 | NoneBot 2.5.0 精确文档、错误版本、snapshot-only 资料 | 只返回适用于当前组件版本的证据；错误版本不命中 | `BugDesignIndexReader` 与 runtime loader 单测、集成 fixture |
| Context7 版本资格 | 精确版本存在、不存在、只提供 Latest、重定向 | 只有精确匹配可作为当前合同；其他结果 unavailable 或明确非当前背景 | 假 HTTP 合约测试、受控真实 API 评测报告 |
| 出站最小化 | 用户原文、日志、源码、配置、身份、关联 ID | Context7 请求只含公共检索 query、可信 library ID 和认证元数据 | 网络前 spy/MockTransport 断言 |
| 后端选择 | 四种候选模式、缺凭据、超时、429、5xx、零命中 | 严格按配置调用；未授权回退不发生；失败不触发其他副作用 | router/adapter 单测与 Agent fake integration |
| 证据映射 | 本地与 Context7 返回同主题内容 | provider、component、version、locator、revision/freshness 分离，分数不混排 | `BugEvidence` 合同测试 |
| 对比评测 | 全新 held-out 覆盖框架 API、Rule/Permission、配置、版本拒绝和无答案 | 分后端报告质量、延迟、费用与失败率，足以支持 D-001/D-002 | maintainer eval runner；报告保存在本地 `reports/` |
| 文档收敛 | 用户确认最终后端与顺序 | ADR、配置说明、overview 和 support/Bug flow 与实现一致 | 链接、术语与配置字段检查 |

## 相关文档

- [ADR-0019：使用版本化知识包分发 RAG 语料](../../adr/0019-distribute-rag-corpus-as-versioned-knowledge-pack.md)
- [ADR-0051：允许 Bug 判定 Agent 查询设计 RAG](../../adr/0051-let-the-bug-assessment-agent-query-design-rag.md)
- [架构概览](../../architecture/overview.md)
- [Context7 API Guide](https://context7.com/docs/api-guide)
- [Context7 Data Privacy](https://context7.com/docs/security/data-privacy)
