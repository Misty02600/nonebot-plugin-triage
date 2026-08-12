# 流程：部署本地能力影子索引

## 这条流程保证什么

影子索引用来回答“当前 Bot 有哪些能力证据”，不回答“这个用户现在一定能执行什么”。它默认关闭；配置后
普通用户可在当前 adapter 域检索确定公开的命令，SUPERUSER 还能查看带披露标签的候选和受限能力。

## 外部参与者和触发条件

部署者显式配置本地 SQLite 路径后，Driver startup hook 只创建后台刷新任务并立即返回；标准
`pyproject.toml` 声明、安装制品 revision、已加载 Plugin / Matcher / Alconna 观察与 SQLite 构建都通过
`asyncio.to_thread` 移出 Bot 启动关键路径。采集器不会为了补全目录再导入插件，也不会调用命令解析、权限、
规则或 handler。

```text
标准 pyproject 声明 ─→ DeclaredInventory ─→ ArtifactRevision
实际已加载模块 ───────────────────────────→ RuntimeObservation
                                              ↓
                  registered / not_observed / runtime_only 协调状态
                                              ↓（当前只进入 shadow status）
已加载 Plugin / Matcher / Alconna
        + distribution / VCS / 可变源码摘要
        + PluginMetadata
        + 可选 HelpPluginSource / operator claim
                         ↓
              字段级 Claim + Evidence
              结构化 / opaque Constraint
                         ↓
       public / review / restricted 影子快照
                         ↓
          原子构建本地 SQLite FTS5 索引
                         ↓
   当前 adapter 的 public / SUPERUSER 鉴权后全部披露层
```

另有一条尚未接入在线回答的受控分析支路：

```text
当前 generation 的已加载能力记录
        ↓  bounded handler/config AST（不 import、不执行）
函数 EvidenceUnit + ConfigRef
        ↓  ConfigValuePolicy 先判定，再只读已存在的 Pydantic 实例
有界瞬时配置投影 / unknown
        ↓  无工具、一次调用、严格 JSON schema
语义 Claim / Constraint
        ↓  Evidence ID + projected ConfigRef 闭包复核
库级分析结果（当前不持久化、不进入 Bot 回复）
```

## 稳定的状态变化

- PyPI 安装优先记录 distribution 名称与版本；VCS 安装在可用时记录 resolved commit；本地、editable、无
  版本或无 Git 的来源使用排序相对路径与文件内容计算摘要。同一轮 deployment 构建复用一个标准库元数据
  adapter，因此 `importlib.metadata.packages_distributions()` 只枚举一次；这不是跨轮或持久缓存。
- `.env*`、日志、数据库、缓存、运行数据和上传目录不参与源码摘要。索引不保存原始配置值。
- Alconna 结构和普通 `CommandRule` 是运行时观察；PluginMetadata、README、注释和帮助图文字是带来源的
  说明。相同字段可以有不同证据性质，不能给整个文件一个统一“真值分数”。
- 自定义 Permission、Rule、限流器和 handler 判断只记录存在性与来源，`evaluability=opaque`。
- 披露态只有 `public / review / restricted`。确定命令入口、支持平台可判定且没有受限信号时自动
  `public`；动态、被动或证据不足的自动发现能力为 `review`；代表部署开发 / 维护者的
  `SUPERUSER`、`CommandMeta.hide=True`、停用命令和明确内部管理能力为
  `restricted`。
- 三种能力都可以写入 SQLite。默认检索只返回 `public`；维护者可显式纳入 `review` 检查候选；
  `restricted` 只有在模型外根据当前上下文完成鉴权后才会进入候选集，不能先交给模型再让模型决定是否隐藏。
- Token、`.env` 原文和私密日志不是能力，采集器从源头排除。需要完全不保存某项真实能力时，由独立的
  operator exclude policy 在生成记录前排除；系统没有 `hidden` 披露态。这个按能力排除接口尚未实现，
  当前不能用 `restricted` 代替它。
- 新索引在临时文件中完整写入并通过完整性检查后替换目标；生成失败时不破坏旧文件。
- 标准声明、制品 revision 与运行模块的协调已经实现，但当前只更新服务状态；尚未用它剔除索引记录或驱动
  增量分析缓存。

## 失败时的语义

- 某来源失败时快照标记 `partial` 并记录稳定错误码，不能把缺失结果解释为“该插件没有能力”。
- `partial` 随索引 metadata 保存；旧索引缺少该字段时在线回复标记完整性未知，不推断为 `false`。
- 首次后台刷新尚未发布 served generation 时，影子服务还未 ready；普通用户能力问答继续使用显式 Provider，
  不等待后台任务，也不把“正在构建”冒充未命中能力事实。
- 版本、源码或运行时结构变化后，旧 generation 只能视为历史派生数据；启动刷新失败但保留上一份成功构建的索引
  时，维护者回复必须标为 stale，普通用户查询则失败关闭并回退到显式 Provider。初版通过重启重新生成，
  不承诺热加载自动刷新。
- `review`、`restricted`、`opaque`、文档声明或过去回执都不能升级为当前执行授权。当前回复会在模型上下文
  之外完成披露过滤与 `restricted` 鉴权，但不会求值第三方 Permission、Rule、handler 或当前执行资格。
  未来普通用户模型、词法 / 向量召回和 Agent 源码搜索也必须从源头排除 `review / restricted` 与当前 adapter
  不支持的记录；即使精确问到也表现为未找到，不能暗示受限能力或其他平台实现存在。
- 普通用户 `triage` 先取得当前 adapter 对应的 `public` capability ID，再把该白名单作为 SQL 条件应用于 FTS
  排名和 `limit` 之前；NoneBot `SUPERUSER` 检查通过后才读取全部披露层，
  回复会区分已登记公开、未审核候选和维护者可见受限能力。普通用户不会读取 `review` / `restricted`，
  模型也不会在过滤前看到它们。自动分析
  确认某能力为 hidden / SUPERUSER-only 后，默认不把该能力源码交给 LLM；维护者模型深查需要另行显式授权。
- Handler 函数形参主要是 Bot、Event、State、消息与 Target 等依赖注入，不能当作用户参数；普通 Matcher
  的手写语法需要单独的源码证据分析。SUPERUSER 只扩大可检索披露层，不提高快照的语法提取能力。
- 影子字段是第三方不可信文本；进入群消息前会折叠空白、限制长度、移除 Unicode 控制字符并中和 mention。
- 未来回答层接收的是经过披露过滤的事实视图，不是固定回复模板。对已经 public 的能力，可靠 Evidence
  可以投影必要输入、群聊 / 私聊场景、公开角色要求，以及限流的作用域、额度、窗口和重置方式；模型自行
  组织语言，但不能补写证据没有支持的约束。整项 restricted 能力仍在检索前排除，不能借“管理员限制”
  向普通用户暗示它存在。
- 显式 Provider 的 `is_visible(bot, event)` 仍把披露与当前上下文可见性合在一起，快照中除 SUPERUSER 外的
  大部分 Permission、Rule、限流和 handler 条件仍是 `opaque`。标准 Config 引用、投影和假模型分析闭环
  已实现，但自定义权限 / 限流语义、后台编排、真实 Provider 与持久语义知识尚未实现。

## 相关决定

- [ADR-0021：用部署本地影子索引整理 Bot 能力证据](../../adr/0021-use-deployment-local-capability-shadow-index.md)
- [ADR-0019：将 RAG 语料作为独立版本化知识包分发](../../adr/0019-distribute-rag-corpus-as-versioned-knowledge-pack.md)
- [ADR-0022：只向 SUPERUSER 接入能力影子候选检索](../../adr/0022-limit-capability-shadow-guidance-to-superusers.md)
- [ADR-0024：自动公开确定且低风险的能力字段](../../adr/0024-auto-publish-deterministic-capability-fields.md)
- [ADR-0026：在检索与模型前隔离能力知识受众域](../../adr/0026-filter-capability-knowledge-before-retrieval.md)
- [ADR-0027：用事实输出合同约束能力帮助](../../adr/0027-constrain-guidance-with-facts-not-fixed-wording.md)
- [可选帮助数据源与复用边界](../help-source-adapters.md)
