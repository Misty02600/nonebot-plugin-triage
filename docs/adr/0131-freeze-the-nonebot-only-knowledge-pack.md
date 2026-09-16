# ADR-0131：冻结只含 NoneBot 文档的产品知识包

| 状态 | 决策日期 |
|---|---|
| 已采纳 | 2026-09-16 |

## 当时遇到了什么

首个正式知识包 `knowledge-v2026.08.1` 实际只包含固定 revision 的 NoneBot 2.5.0 官方文档，已经满足
教学预载和 Bug design RAG 的主要资料需求。仓库随后增加了 Uninfo、NapCat 等来源设想、通用 OpenAPI / TypeScript
分块器和独立 Release 工作流，但这些能力没有进入首个正式资产，也会持续产生来源、许可、依赖、评测和发布维护成本。

产品当前仍需要已有知识包为新安装提供可校验的公开文档，并需要保留最小生产代码解释和复现该资产；但项目
不再承诺持续刷新多组件知识库，也不需要为了未来可能的更新长期维护自动发布链。

## 决策

1. 产品知识包只维护现有的 NoneBot 2.5.0 官方版本化文档。Alconna / UniSeg 教学作为 NoneBot 官网文档的一部分
   保留；Uninfo、NapCat、OneBot Adapter 和 OneBot v11 不再作为知识包来源。
2. 保留固定 NoneBot revision 的采集器、Markdown 分块、SQLite 索引构建、检索评测、打包和完整性验证代码，
   使当前资产的构建合同仍可解释，并为明确重开维护时提供最小基础。构建器不再支持 OpenAPI、TypeScript、
   Release Notes 或源码快照；旧资产的逐字节重建仍以其不可变 tag 中的原构建代码为准。
3. `knowledge-v2026.08.1` 和 `knowledge-stable/catalog.json` 保持冻结。运行时继续在启动后恢复缓存并检查静态
   catalog，使新安装仍能取得该资产；精确组件版本过滤、固定 URL / SHA pin、失败保留旧包和 no-knowledge
   降级合同不变。
4. 删除自动知识包发布工作流。以后若明确重新开启知识包维护，应从保留的最小构建器建立当次发布流程，并把
   新资产视为新的 Evidence revision，重新执行检索评测和冷测；当前代码不构成持续发布承诺。
5. Uninfo 的确定性语义 profile、插件源码分析、安装源码导航和会话参与者读取不属于产品知识包，继续按各自
   现有边界维护。

## 为什么这样选

- 冻结已发布资产能保持当前冷测和部署证据稳定，又不影响新安装通过静态 catalog 取得知识包；
- 保留小型构建器比只留下二进制 ZIP 更容易核验来源、分块和 manifest，也避免未来必须从 Git 历史恢复整套工具；
- 删除未发布的多来源能力和自动工作流，能把维护面收敛到实际产品正在使用的 NoneBot 文档；
- 知识包更新本来就会改变片段身份、正文或检索排名，应显式进入新一轮评测，而不是承诺跨语料 revision 输出不变。

## 没有采用的方案

- **继续维护多组件知识包和通用构建器**：保留扩展性，但没有当前产品收益，仍需持续处理上游变化和发布验证。
- **只保留发布 ZIP、删除全部生产代码**：维护面最小，但当前资产会成为难以复现和解释的黑盒。
- **手工裁剪 NoneBot 的少数页面**：运行时本来只注入选段或检索命中，进一步维护页面 allowlist 节省有限，
  反而增加遗漏和漂移风险。
- **关闭默认 catalog 检查**：可避免一次启动网络请求，但全新安装将无法自动取得冻结资产；现有 fail-open、
  固定 pin 和显式关闭已经覆盖离线部署。

## 带来的影响

- 同一冻结索引上的固定选段和检索保持可重复；固定 URL / SHA 可用于严格冷测复现；
- 后续 NoneBot 版本没有精确匹配知识包时继续进入 no-knowledge，而不会近似使用 2.5.0 文档；
- Uninfo 文档不再预载或通过 `framework_search_docs` 检索，相关事实依赖确定性语义和安装源码证据；
- 若未来发布新包，需要显式重开来源复核、构建、发布和全轮冷测，不能沿用已删除的自动发布能力。

## 替代关系

- 部分替代 [ADR-0019](0019-distribute-rag-corpus-as-versioned-knowledge-pack.md) 中持续使用独立 tag / Release
  工作流发布新知识包的安排；独立资产、manifest、LocalStore、许可和完整性边界继续有效。
- 部分替代 [ADR-0067](0067-refresh-knowledge-pack-from-stable-catalog-at-startup.md) 中发布工作流更新 stable catalog
  的安排；启动恢复、静态 catalog 发现、精确版本过滤和失败回退继续有效。

## 落实与确认

- 默认库存只包含固定 revision 的 NoneBot 2.5.0 文档；
- 知识包构建器只接受 Markdown / MDX，TypeScript 解析依赖已移除；
- Uninfo 采集器、知识预载和检索组件选择已移除；
- 独立知识包 Release 工作流已删除，现有公开 Release 与 catalog 不被改写。

## 相关文档

- [架构总览](../architecture/overview.md)
- [知识来源与最小复现入口](../../tools/nbtriage_maintainer/knowledge_pack/sources/README.md)
