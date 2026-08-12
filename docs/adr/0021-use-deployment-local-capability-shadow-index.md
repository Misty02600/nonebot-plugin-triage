# ADR-0021：用部署本地影子索引整理 Bot 能力证据

| 状态 | 决策日期 |
|---|---|
| 已采纳 | 2026-08-11 |

## 当时遇到了什么

要求第三方插件逐个向 Triage 登记能力，能得到很安全但几乎没有覆盖率的帮助列表。直接信任 README、
帮助图或运行时反射又会把过期说明、管理命令和第三方隐性判断包装成确定事实。

部署者还不一定维护 Bot 版本、Git 提交或 `uv.lock`。本地插件可能不改版本号，第三方插件的真实行为也会
受到当前加载状态、配置、群聊、用户权限、限流和 handler 内部分支影响。

## 决策

1. 增加默认关闭的部署本地影子索引。启用后只读取 Bot 已经加载的 Plugin、Matcher 和 Alconna 对象，结合
   已安装 distribution 信息、本地源码摘要、PluginMetadata 与可选帮助数据，生成字段级 Claim、Evidence
   和 Constraint；不为建索引额外导入或执行第三方插件。
2. 显式公开 Provider 继续保留，但只作为高价值的披露声明和上下文可见性入口，不再是第三方能力进入影子
   目录的唯一方式。普通 Matcher、被动功能和第三方文档可以自动进入 `review` 候选层。
3. 任何来源都只证明自己观察或声明的字段。运行时结构、源码、人工帮助数据和 README 可以互相补充或
   冲突；`verified` 只表示结构与来源校验通过，不表示用法永远正确，也不表示当前用户一定能执行。
4. 能力披露与执行资格分开。持久快照只使用 `public / review / restricted` 三种披露策略：`public` 是可供
   普通用户检索的声明，`review` 是尚待维护者复核的候选，`restricted` 保存代表部署开发 / 维护者的 `SUPERUSER`、
   `CommandMeta.hide=True` 或明确的内部管理能力。`restricted` 会进入本地索引，但默认检索不返回；只有
   先在模型外按当前 Bot、事件、场景和身份完成鉴权的路径才能读取。平台或场景等结构条件和无法安全求值的
   `opaque` 约束仍随能力保存；当前权限、限流和外部状态只能在请求时单独判断，未知不能伪装成允许。
5. 快照修订由实际已加载插件集合、运行时命令结构、distribution 版本或 VCS commit、以及可变源码内容
   摘要共同组成；`uv.lock`、Git 和语义版本都只是存在时的补充，不是前置条件。`.env`、配置值、日志、
   数据库、上传目录和运行数据不得进入源码摘要或索引；Token、配置原文和私密日志不属于能力证据。
6. 第一阶段只生成本地 SQLite FTS5 检索索引和覆盖报告，不接管 `triage` 回复。检索默认只返回 `public`；
   `review` 必须由维护者显式请求；`restricted` 必须经过模型外鉴权后才能请求。维护者 CLI 的
   `--include-restricted` 只表达调用者已在带外完成授权，不自行证明身份；群聊 `SUPERUSER` 尚未接入影子
   检索。模型、网络和向量服务都不是构建前置条件。
7. 构建使用同目录临时文件和原子替换。索引是可删除重建的部署本地派生数据，不进入 wheel、sdist、Git 或
   公共知识包；失败不能破坏已经存在的索引。
8. 影子采集不得调用任意 Matcher Rule、Permission、handler、Alconna `parse()`、behavior 或 executor。
   自定义权限、限流和 handler 分支只能记录为 `opaque`，等待未来专用、无副作用的 evaluator 或真实回执。
9. Help 插件和运营者 overlay 通过通用可选来源接口接入。核心不依赖 Migut 路径、某一种 YAML schema，
   也不要求部署者安装帮助图插件。
10. 不再设置 `hidden` 能力态。部署者若要让某项能力完全不被采集，使用独立的 operator exclude policy 在
    持久化前排除；这是一条源头采集策略，不是披露级别，也不能与 `restricted` 混用。

## 为什么这样选

- 普通第三方插件无需改代码即可进入覆盖率评估，显式 Provider 仍可覆盖最敏感的公开边界；
- 版本、源码和运行时结构共同决定 generation，本地插件不改版本号也能在重启后产生新快照；
- 字段级来源与 `opaque` 约束允许系统诚实表达“文档上存在，但当前能否执行未知”；
- 先观察快照和检索质量，可以在不改变群聊行为的情况下调整字段、来源优先级与失效策略。

## 没有采用的方案

- **要求全部插件实现 Triage Provider**：安全但第三方接入成本过高，无法覆盖现有生态。
- **每次求助实时扫描源码和所有权限逻辑**：延迟与副作用不可控，也无法安全执行任意第三方判断。
- **只信帮助图或 README**：适合作为说明证据，不足以证明当前注册语法、配置和执行资格。
- **把发现到的命令全部公开给模型**：受限能力会在过滤前泄露，提示词不能代替模型外鉴权。
- **丢弃所有 SUPERUSER 或 `hide=True` 能力**：开发维护者同样需要准确帮助；应保留为 `restricted`，在
  鉴权后的查询路径中使用，而不是把“普通用户不可见”误写成“系统不存在”。
- **把部署索引作为 ADR-0019 的公共知识包发布**：它包含安装实例事实，生命周期和所有者都不同。

## 带来的影响

- 基础 wheel 增加传输无关的快照、源码指纹和本地检索代码，以及 NoneBot 运行时只读采集适配层；
- 启用者需要显式提供本地索引路径；默认安装不创建文件、不改变 `triage` 回复；
- 初版按启动时已加载状态生成，运行中热加载需重新生成或重启；
- `restricted` 增加了本地敏感元数据的保护责任：任何模型或检索器看到它之前都必须由确定性上下文鉴权守门；
- 索引结果仍需影子评测，达到泄露、陈旧和检索质量门槛后才能讨论接入用户回复。

## 落实与确认

- 基础切片已落实：领域快照、本地 FTS5 / 短词检索、NoneBot 已加载对象采集、默认关闭的启动构建、
  `public / review / restricted` 读取过滤和维护者检索命令均已有实现。
- 自动测试确认源码摘要排除 `.env` 与运行数据、不保存绝对本机路径；普通 Matcher 与 Alconna 可被发现但
  不执行；SUPERUSER、`hide=True` 与停用 Alconna 作为 `restricted` 持久化但不进入默认检索；短中文功能
  问法可以命中目标能力；构建发布失败会保留旧索引并区分 observed / served generation。
- 尚未落实：通用 HelpPluginSource、operator exclude policy、热加载增量失效、普通用户 review 审批，以及把
  影子检索结果接入模型 Agent。ADR-0022 已把确定性候选回复接到群聊 `SUPERUSER` 的 `triage` 入口。
- 已完成帮助插件生态复核，确认第一阶段直接使用 NoneBot / Alconna 只读运行时信息；PicMenu、TreeHelp 与
  结构化帮助文件只作为可选适配或算法参考，不引入会执行模板、回调或第三方命令逻辑的采集路径。

## 替代关系

- 第 6 条的“第一阶段不接入回复”和群聊 SUPERUSER 尚未接入边界，已被
  [ADR-0022](0022-limit-capability-shadow-guidance-to-superusers.md) 部分替代；普通用户仍只读取已批准
  public，review / restricted 的证据与执行资格边界不变。
- 部分替代 [ADR-0003](0003-unified-capability-guidance-and-incident-intake.md) 的 D-003：显式 Provider 不再是
  普通 Matcher 的唯一接入方式，但其安全披露职责保留。
- 补充 [ADR-0016](0016-keep-maintainer-evaluation-tooling-out-of-install-surface.md)：运行时所需代码进入
  `src/`，仓库维护者检查命令仍留在 `tools/`。
- 补充 [ADR-0019](0019-distribute-rag-corpus-as-versioned-knowledge-pack.md)：部署本地派生索引不属于可分发
  的公共知识包。

## 相关文档

- [部署本地能力影子索引](../architecture/flows/capability-shadow-index.md)
- [Alconna 公开能力与解析回执](../architecture/flows/alconna-capability-and-parse-receipts.md)
- [可选帮助数据源与复用边界](../architecture/help-source-adapters.md)
