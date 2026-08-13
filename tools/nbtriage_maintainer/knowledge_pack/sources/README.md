# 默认知识来源边界

构建命令只消费已经固定 revision 的本地 checkout 或不可变快照；本目录不保存上游正文，也不把可变的
`main`、`latest` 写成版本。每次实际构建在本地生成 source policy，至少采用以下范围：

- NapCat：固定 commit 的当前用户文档、通过版本一致性校验的 OpenAPI、当前推荐 tag 的 TypeScript
  源码和同一支持窗口的 Release Notes。NapCatQQ 源码按当前许可仅可用于本地索引，不能进入分发包。
- NoneBot2：插件支持范围内的官方用户/API 文档和迁移说明；实际运行源码由部署本地
  `installed_sources` 读取，不在知识包重复维护完整副本。
- OneBot Adapter、Alconna（含 UniSeg 文档）、Uninfo、OneBot v11：各自固定 revision 的官方文档。
  Uninfo 是独立知识组件；UniSeg 属于 Alconna 文档范围。

source policy 里的 `distribution` 只有 `redistributable` 和 `local_only`。它是来源级发布约束，不进入每条
检索证据。来源未完成许可复核时使用 `local_only`。

## 本地检索实践

索引采用 SQLite FTS5。每次从已验证快照全量构建一个临时数据库，运行 SQLite 与 FTS 完整性检查后才原子
替换旧索引；失败时旧索引保持可用。查询先按组件、目标版本和来源类型过滤，再用 FTS5 `bm25()` 排序。
小型公共语料不做增量写索引：全量重建可以自然清除上游删掉的页面，也避免新旧 revision 混合。

`revision` 只记录上游 Git commit；`snapshot_sha256` 只验证本地所选文件没有变化，二者不能互换。
稳定 `source_id` 与片段摘要用于来源追溯，不把摘要当产品版本。Markdown 使用
`markdown-it-py` 的 token/source map 按标题切分；OpenAPI 按 operation 切分并校验 `info.version`；
NapCat TypeScript 使用官方 Tree-sitter Python binding 与 TypeScript grammar 按声明切分。首版不使用
向量库、运行时联网或任意仓库扫描。

维护入口：

```powershell
uv run --group maintainer python -m tools.nbtriage_maintainer.knowledge_pack prepare-policy ...
uv run --group maintainer python -m tools.nbtriage_maintainer.knowledge_pack build ...
uv run --group maintainer python -m tools.nbtriage_maintainer.knowledge_pack search ...
uv run --group maintainer python -m tools.nbtriage_maintainer.knowledge_pack evaluate ...
uv run --group maintainer python -m tools.nbtriage_maintainer.knowledge_pack package --index ... --output ... --version ...
uv run --group maintainer python -m tools.nbtriage_maintainer.knowledge_pack verify-package ...
```

`package` 只接受全部来源都标为 `redistributable` 的索引，输出包含 `manifest.json` 和
`index.sqlite3` 的 ZIP，并返回归档 SHA-256。插件只有同时配置精确 HTTPS 资产 URL 与该 SHA-256 时，
才会在启动后后台下载；缺失或下载失败只回退到无知识库模式。

正式发布使用独立 `knowledge-vYYYY.MM.N` tag。候选 ZIP 的文件名必须是
`nbtriage-default-YYYY.MM.N.zip`，旁边放自动生成的 `.sha256`。维护者先创建含这两个资产的 Draft Release，
再手工触发 `Release knowledge pack` 工作流；工作流在 tag 对应提交上复核包内 `project_revision`、构建器
摘要、来源再分发状态、归档/索引摘要和 SQLite 完整性，成功后发布 Draft，并保持插件 Release 的 Latest
标记不变。

NoneBot2 优先复用已批准的 Grounded Docs `nonebot2-git-docs` adapter。NapCat 使用本目录的专用采集器；
如果已经有位于目标 tag、`packages` 干净的官方本地 checkout，传 `--source-checkout` 可避免重新下载大仓库。
Alconna、Uninfo 和 OneBot Adapter 在采集方法完成双次可复现验证前，不用通用 clone 规则猜测文档完整性。
