# ADR-0019：将 RAG 语料作为独立版本化知识包分发

## 状态

已采纳

## 日期

2026-08-10

## 当时遇到了什么

项目已经能把外部 `bot-docs` 的批准子集构建为本地 SQLite FTS5 索引，但该索引当前只用于独立检索 PoC，
尚未接入 B1、B4 或 NoneBot Matcher。索引是从项目事实、工程配方和精确版本上游 API 文档派生的可重建
数据，不是插件代码，也不是插件实例产生的不可替代业务数据。

把 SQLite、源 Markdown 或预切分语料直接放进 `nonebot-plugin-triage` wheel / sdist，会把插件发布与文档
刷新绑定在一起，增加安装体积、许可证复核和陈旧语料风险。完全离线部署又需要一种不依赖首次联网下载的
交付方式，因此不能只回答“打包”或“不打包”，还必须确定插件、知识包与部署目录各自的所有权。

## 决定

1. 当前检索 PoC 不发布离线知识包。`data/rag/bot-docs.sqlite3` 继续作为维护者本地、Git 忽略、可删除并
   重建的工件；基础插件不因仓库存在索引构建能力就承诺运行时 RAG。
2. `nonebot-plugin-triage` 的基础 wheel 和 sdist 不包含 SQLite 索引、`bot-docs` Markdown、派生 chunk 或
   其他预构建语料。运行时真正需要的检索代码可以进入插件领域核心；语料数据不进入基础安装面。
3. 只有插件已经接入并承诺离线 RAG 功能后，才发布独立、可选、版本化的知识包。默认交付形态是与插件
   Release 并列的单独资产，而不是第二个 PyPI 包；完全离线发行包可以同时携带 wheel 与知识包，但两者
   仍是可独立校验和替换的文件。
4. 知识包使用独立于插件版本的稳定标识和版本。每个包必须附带机器可读 manifest，至少记录知识包 ID、
   索引 schema、retriever ID、语料 SHA-256、构建工具 revision、源文件 revision、精确上游库版本、构建
   时间、完整性摘要，以及来源和许可证复核结果。
5. 插件只接受显式兼容的 schema / retriever / corpus manifest。缺失、损坏、版本不兼容或没有完成来源
   复核的知识包不得被静默加载；具体是拒绝启用 RAG 还是回退到明确标记的非 RAG 行为，在真正接入产品
   入口时另行确定。
6. 知识包不写入 `site-packages` 或插件源码目录。NoneBot 入口未来应把默认安装位置映射到 LocalStore 的
   cache 目录，或接收部署者显式提供的外部路径；传输无关领域核心只接收已解析路径和 manifest，不依赖
   NoneBot / QQ 类型。可重建知识包与 ADR-0018 中不可重建的 trial 审计 data 状态保持分离。
7. 文档内容继续是不可信证据。知识包通过完整性与版本校验，只证明来源和构建身份，不允许其中的自然语言
   直接升级为工具调用、代码执行、配置修改、外部写入或生产操作。
8. 正式知识包在同一仓库使用独立 `knowledge-vYYYY.MM.N` tag 和 Release。tag 指向包含构建规则、来源锁定
   清单和评测合同的项目 commit，不等同于插件版本；插件 `v*` Release 只声明推荐的知识包，不承载会独立
   更新的知识包资产。知识包 Release 不设置为仓库 Latest。

## 为什么这样选

- 文档更新可以独立发布知识包，不必为了语料刷新重发插件 wheel；
- 基础安装保持精简，也不会让尚未启用 RAG 的部署者承担无效体积和许可证风险；
- 完全离线部署仍能提前取得一组经过匹配和校验的 wheel + knowledge pack，不依赖首次运行联网；
- 独立 manifest 让插件、索引格式、检索器和上游文档版本能够分别演进并明确拒绝错误组合；
- LocalStore cache 或显式外部路径符合“可重建派生数据不属于源码安装目录”的所有权边界。

## 没有采用的方案

- **把 SQLite 直接放进基础 wheel / sdist**：安装最简单，但文档刷新会强迫插件重发，也扩大每个安装者的
  体积与再分发审查范围。
- **把整个 `bot-docs` 仓库 vendor 进插件**：保留源文档但复制事实真源，容易漂移，并不能替代索引与
  许可证复核。
- **首次启动时强制在线下载**：无法满足完全离线部署，也把网络可用性引入插件启动路径。
- **现在创建第二个知识库 PyPI 包**：当前没有产品运行时消费者，会提前引入双包版本配对和发布维护成本。

## 带来的影响

- 当前发布只包含插件运行代码；本地 PoC 索引和评测报告继续受 `data/`、`reports/` 忽略规则约束；
- 未来知识包发布器、manifest schema、安装 / 替换流程和兼容测试必须先于产品入口启用；
- package-quality 检查最终应拒绝 wheel / sdist 中出现 `.sqlite3`、源 Markdown 或派生语料目录；
- 发布知识包前仍需逐来源完成许可证、隐私、版本和可再分发复核，语料哈希不能替代这些审查；
- 清理 LocalStore cache 可能移除运行副本，因此完全离线部署者应保留可重新安装的原始知识包资产。

## 落实与确认

- 实施情况：维护工具可以把全部来源均已批准再分发的 SQLite 索引封装为带 manifest 的独立 ZIP，并输出
  归档 SHA-256。插件只有同时配置精确 HTTPS 资产 URL 与 SHA-256 时，才在 NoneBot 启动后创建后台下载
  任务；下载、摘要、manifest 或 SQLite 校验失败均回退到明确的无知识库模式，不阻断 Bot 启动。
- 运行副本写入 LocalStore cache，不进入 `site-packages`；未配置知识包时只记录一次启动警告，不进行网络
  请求。当前仍未把知识检索接入用户回答或模型输入，也未发布正式知识包资产。
- 当前验证覆盖配置成对校验、HTTPS 限制、后台下载、完整性与兼容校验、失败回退、分发许可门和归档成员。
- 独立工作流只发布已经人工创建的 Draft Release：它从 `knowledge-v*` tag checkout 发布合同，复核候选资产
  与 tag commit 的绑定和全部完整性条件，成功后发布 Draft；不会改动插件 `v*` / PyPI 发布链。

## 替代关系

- 补充：[ADR-0015：分离版本化评测合同与本地运行数据](0015-separate-versioned-evals-from-local-runtime-data.md)
- 补充：[ADR-0016：将维护者评测工具排除在插件安装面之外](0016-keep-maintainer-evaluation-tooling-out-of-install-surface.md)
- 补充：[ADR-0018：只用 LocalStore 保存显式启用的 trial 审计日志](0018-use-localstore-only-for-enabled-trial-audit-log.md)

## 相关文档

- [架构概览](../architecture/overview.md)
- [README：bot-docs 本地检索 PoC](../../README.md#bot-docs-本地检索-poc)
