# 流程：部署本地能力影子索引

## 这条流程保证什么

影子索引用来回答“当前 Bot 有哪些能力证据”，不回答“这个用户现在一定能执行什么”。它默认关闭；配置后
会为 SUPERUSER 的定向 `triage` 能力问题提供带披露标签的候选，普通用户仍只读取显式公开 Provider。

## 外部参与者和触发条件

部署者显式配置本地 SQLite 路径后，插件在 NoneBot 启动完成时读取已经加载的 Plugin、Matcher 和 Alconna
对象。采集器不会为了补全目录再导入插件，也不会调用命令解析、权限、规则或 handler。

```text
已加载 Plugin / Matcher / Alconna
        + distribution 版本或 VCS commit
        + 可变源码内容摘要
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
   默认 public / SUPERUSER 鉴权后 review + restricted
```

## 稳定的状态变化

- PyPI 安装优先记录 distribution 名称与版本；VCS 安装在可用时记录 resolved commit；本地、editable、无
  版本或无 Git 的来源使用排序相对路径与文件内容计算摘要。
- `.env*`、日志、数据库、缓存、运行数据和上传目录不参与源码摘要。索引不保存原始配置值。
- Alconna 结构和普通 `CommandRule` 是运行时观察；PluginMetadata、README、注释和帮助图文字是带来源的
  说明。相同字段可以有不同证据性质，不能给整个文件一个统一“真值分数”。
- 自定义 Permission、Rule、限流器和 handler 判断只记录存在性与来源，`evaluability=opaque`。
- 披露态只有 `public / review / restricted`。普通用户可检索的声明为 `public`；未确认的自动发现能力为
  `review`；代表部署开发 / 维护者的 `SUPERUSER`、`CommandMeta.hide=True` 和明确内部管理能力为
  `restricted`。
- 三种能力都可以写入 SQLite。默认检索只返回 `public`；维护者可显式纳入 `review` 检查候选；
  `restricted` 只有在模型外根据当前上下文完成鉴权后才会进入候选集，不能先交给模型再让模型决定是否隐藏。
- Token、`.env` 原文和私密日志不是能力，采集器从源头排除。需要完全不保存某项真实能力时，由独立的
  operator exclude policy 在生成记录前排除；系统没有 `hidden` 披露态。这个按能力排除接口尚未实现，
  当前不能用 `restricted` 代替它。
- 新索引在临时文件中完整写入并通过完整性检查后替换目标；生成失败时不破坏旧文件。

## 失败时的语义

- 某来源失败时快照标记 `partial` 并记录稳定错误码，不能把缺失结果解释为“该插件没有能力”。
- `partial` 随索引 metadata 保存；旧索引缺少该字段时在线回复标记完整性未知，不推断为 `false`。
- 版本、源码或运行时结构变化后，旧 generation 只能视为历史派生数据；启动刷新失败但保留上一份成功构建的索引
  时，维护者回复必须标为 stale。初版通过重启重新生成，不承诺热加载自动刷新。
- `review`、`restricted`、`opaque`、文档声明或过去回执都不能升级为当前执行授权。当前回复会在模型上下文
  之外完成披露过滤与 `restricted` 鉴权，但不会求值第三方 Permission、Rule、handler 或当前执行资格。
- 群聊 `triage` 只在 NoneBot `SUPERUSER` 检查通过后读取全部披露层；回复会区分已登记公开、未审核候选和
  维护者可见受限能力。普通用户不会读取 `review` / `restricted`，模型也不会在过滤前看到它们。
- 影子字段是第三方不可信文本；进入群消息前会折叠空白、限制长度、移除 Unicode 控制字符并中和 mention。

## 相关决定

- [ADR-0021：用部署本地影子索引整理 Bot 能力证据](../../adr/0021-use-deployment-local-capability-shadow-index.md)
- [ADR-0019：将 RAG 语料作为独立版本化知识包分发](../../adr/0019-distribute-rag-corpus-as-versioned-knowledge-pack.md)
- [ADR-0022：只向 SUPERUSER 接入能力影子候选检索](../../adr/0022-limit-capability-shadow-guidance-to-superusers.md)
- [可选帮助数据源与复用边界](../help-source-adapters.md)
