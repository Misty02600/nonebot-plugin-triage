# ADR-0067：启动后从 stable catalog 刷新知识包

## 状态

| 状态 | 决策日期 |
|---|---|
| 已采纳；已实现 | 2026-08-15 |

## 背景

ADR-0019 已把 RAG 语料从插件 wheel 中拆成独立、版本化、可校验的知识包。首个运行实现要求部署者同时
配置知识包资产 URL 和 SHA-256，因此只能安装一个人工指定的固定包。项目随后把 Bug Agent 的设计检索绑定
到当前安装组件的精确版本；当部署从 NoneBot 2.5.0 升级到 2.5.1 时，旧文档不会冒充新版本合同，但插件也
不会自行发现后来发布的兼容知识包。

知识包是可删除重建的公开数据，不是插件代码或业务状态。项目作者允许插件联网检查官方 stable catalog，
但知识服务的网络、缓存、目录、格式、完整性或版本失败不得阻止插件导入、Matcher 注册或 Bot 启动。

## 决定

1. 未配置固定知识包时，插件默认在 NoneBot 启动钩子中创建后台任务，请求项目维护的 HTTPS stable
   catalog。启动钩子不等待网络、下载或 SQLite 校验完成。
2. catalog 是一个很小的闭合 JSON 文档，只声明 schema、知识包 ID、知识包版本、HTTPS 资产 URL 和归档
   SHA-256。它不包含语料，不允许改变插件代码、模型配置、工具权限或其他运行状态。
3. 后台任务先从 LocalStore cache 恢复并完整校验此前的 active 知识包，再检查 catalog。catalog 不可用、
   内容非法、下载失败、SHA 不符、manifest / retriever / SQLite 不兼容或 active 指针更新失败时：
   - 已有兼容 active 包继续服务；
   - 没有可用包时进入明确的 no-knowledge 模式；
   - 两种情况都只记录稳定错误类别，不传播异常阻断插件加载。
4. 新归档先安装到以 SHA-256 命名的不可变对象目录，完成归档、manifest 与 SQLite 校验后，再以原子替换的
   小型 active 指针切换当前包。旧对象不在更新事务中删除，可供失败回退。
5. stable catalog 指向的知识包应包含当前仍受支持的精确组件版本。运行检索继续由模型外根据本机实际安装
   版本过滤；新包没有该版本时不得回退到最近版本或把 2.5.0 文档当作 2.5.1 合同。
6. 每次启动只检查一次，不增加常驻定时器。依赖版本升级本来需要重启；运行期间周期轮询没有足够收益。
7. 现有 `NBTRIAGE_KNOWLEDGE_PACK_URL` 与 `NBTRIAGE_KNOWLEDGE_PACK_SHA256` 保留为固定 pin，并优先于
   stable catalog。完全离线部署可设置 `NBTRIAGE_KNOWLEDGE_PACK_AUTO_UPDATE=false`；若同时提供固定 pin，
   仍安装该固定包。pin 只配置一项、不是 HTTPS 或 SHA 格式非法时只禁用知识服务并记录稳定类别，不让配置
   错误阻断插件加载，也不在部署者明确尝试 pin 时偷偷回退到 stable catalog。
8. 知识包 Release 继续使用不可变的 `knowledge-vYYYY.MM.N` 标识。发布工作流在版本资产通过既有复核并发布
   后，才原子替换 `knowledge-stable` Release 中的 `catalog.json`；catalog 不把知识包 Release 设为仓库
   Latest，也不改变插件发布版本。

## 理由

- 精确版本检索解决“不能用错文档”，stable catalog 解决“正确文档发布后部署怎样发现”；两者职责不同；
- 先恢复旧包、后更新指针，使外部网络或新制品故障只降低知识新鲜度，不降低插件可用性；
- 单个可变 catalog 加不可变版本资产，比查询 GitHub Latest、猜测文件名或实现完整包管理器更简单；
- 固定 pin 与显式关闭保留离线、内网镜像和可复现实验需要。

## 没有采用的方案

### 每个请求前检查更新

没有采用。它会把知识发布网络加入用户请求延迟和故障面；启动时一次后台检查已经覆盖依赖升级后的刷新。

### catalog 或新包不可用时让插件启动失败

没有采用。RAG 是增强证据，不是 Matcher 注册和基础支持入口的必要条件；外部可用性不能成为插件加载门。

### 自动选择最近的文档版本

没有采用。相邻 patch 版本仍可能改变 API 或行为，近似匹配会破坏 Bug verdict 的证据适用性。

## 带来的影响

- 默认部署启动后会向项目维护的公开 HTTPS catalog 发出一次请求；可通过显式配置关闭；
- 首次尚无 published catalog 或网络不可用时，插件正常加载并保持 no-knowledge；
- 新 stable 包发布后，部署在下一次启动时自动下载并切换，不需要人工更新 URL / SHA；
- LocalStore cache 可能保留多个内容寻址对象；首切不增加自动清理策略。

## 落实与确认

- `KnowledgePackService` 已支持 active 指针恢复、stable catalog、内容寻址安装、校验后切换和失败保留旧包；
- `register_knowledge_pack` 默认创建 stable 更新服务，固定 URL / SHA 优先，显式关闭才进入无网络模式；
- 知识包发布工作流在版本 Release 发布后更新 `knowledge-stable/catalog.json`；
- 首个 stable catalog 已发布并指向 [`knowledge-v2026.08.1`](https://github.com/Misty02600/nonebot-plugin-triage/releases/tag/knowledge-v2026.08.1)；
  公共 catalog 位于 [`knowledge-stable/catalog.json`](https://github.com/Misty02600/nonebot-plugin-triage/releases/download/knowledge-stable/catalog.json)，
  其中资产 URL、版本和 SHA-256 已通过公开下载复核；
- 测试覆盖默认 catalog 安装、固定 pin 优先、显式关闭，以及 catalog 失败仍继续使用旧 active 包。

## 与既有决定的关系

- 部分替代：[ADR-0019：将 RAG 语料作为独立版本化知识包分发](0019-distribute-rag-corpus-as-versioned-knowledge-pack.md)
  中“未显式配置时不联网”和首次固定 URL / SHA 才能发现知识包的策略；独立制品、manifest、LocalStore、
  许可复核与版本隔离继续有效。
- 延续：[ADR-0063：让插件启动独立于模型增强](0063-keep-plugin-startup-independent-from-model-enhancements.md)
  的可选增强不得阻断插件加载原则。

## 相关文档

- [架构概览](../architecture/overview.md)
- [README 配置](../../README.md#配置)
