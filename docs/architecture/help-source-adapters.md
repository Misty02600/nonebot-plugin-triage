# 可选帮助数据源与复用边界

这份说明记录 Triage 如何看待 NoneBot 生态中的帮助菜单、帮助图和命令自动发现插件。结论不是“选一个
帮助插件当真值”，而是把它们当成不同质量的证据来源，再由部署本地影子索引统一标注来源、时效和限制。

## 当前实现

第一阶段已经直接读取 NoneBot 的已加载 Plugin / Matcher 和 Alconna 命令管理器，只生成部署本地快照与
FTS5 索引。它不调用 Matcher 的 Rule、Permission、handler 或 Alconna `parse()`，也尚未接入任何第三方
帮助插件。

普通 Matcher 的命令识别会读取 NoneBot 2.5 的 `Rule.checkers` 结构。这不是稳定的跨版本公共协议，因此
只能放在版本适配层：遇到未知 checker 或结构变化时保留未知约束并失败关闭，不能猜成公开、可执行能力。

## 调研后的复用分级

以下结论核对于 2026-08-12。第三方项目版本和许可证可能变化，真正接入前仍需重新确认。

| 来源 | 可以利用什么 | 接入方式 | 明确不做什么 |
|---|---|---|---|
| NoneBot / Alconna 运行时 | 已加载插件、`Plugin.matcher`、命令结构、disabled、shortcut、命令与 Matcher 关联 | 第一阶段直接读取公共接口；内部 checker 结构由版本适配器隔离 | 不执行 Rule、Permission、handler、parser 或 executor |
| [PicMenu Next](https://github.com/lgc-NB2Dev/nonebot-plugin-picmenu-next) | 已整理的插件说明、旧 PicMenu `menu_data`、结构化 overlay 合并思路 | 以后可选地消费“已经加载并初始化”的只读快照，逐字段复制到 Triage 模型 | 不主动导入插件，不调用 `refresh_infos()`、formatter、mixin、模板或渲染器 |
| [TreeHelp](https://github.com/he0119/nonebot-plugin-treehelp) | 从 `Plugin.matcher` 与已知 checker 识别普通命令的思路 | 参考算法后按当前 NoneBot 版本重写 | 不复制永久缓存和依赖内部对象形状的原实现 |
| [nonebot_plugin_help_baize](https://github.com/sangonomiya249/nonebot_plugin_help_baize) | AST 字面量提取、来源位置和搜索文本设计 | 以后作为低置信静态证据源 | 不把正则结果当语法真值，不改写已安装插件源码 |
| PicMenu `menu_data`、结构化 YAML / JSON / TOML、部署者帮助图数据 | 人工整理的公开意图、名称、说明、示例 | 通用结构化文件 profile；部署私有 schema 留在部署层 adapter | 不要求固定文件路径，不假设所有部署者都有 overlay，不用文件存在证明插件已加载或当前可执行 |

当前没有必要把第三方项目的 Python 代码复制进核心。NoneBot、Alconna 和上述可参考项目大多允许按其许可
复用，但协议重写更容易保留 Triage 的来源模型与安全边界。AGPL / GPL 实现、未声明许可证的私有实现或
素材不复制进 MIT 核心；会动态执行 Python / Jinja / JavaScript 模板、第三方回调或全局预处理器的路径也
不接入。把 README 或源码发送给远端模型生成帮助的方案不符合部署本地 RAG 边界。

## 后续来源接口

通用 `HelpPluginSource` 仍是待实现的适配层，不是对第三方插件的强制注册协议。它应满足这些约束：

- 由部署配置或 entry point 显式启用，默认关闭；
- 探测只检查已加载插件、版本和显式配置路径，不主动 import / `require()` 目标插件；
- 输出 Triage 自有的 Claim、Evidence 和 Constraint，不把第三方模型对象交给核心；
- 结构化文件使用安全解析器，并限制根目录、文件大小、数量、嵌套深度和符号链接；
- 只接受允许的字符串、数字、布尔、列表和字典字段，不接受 callable、模板或自定义可执行对象；
- 某来源失败只让该来源成为 partial，不把缺失结果解释为“没有能力”。

建议的可选适配顺序是：PicMenu Metadata `extra.menu_data`、通用结构化帮助文件、已加载 PicMenu Next 的
只读快照，最后才是部署私有 adapter。Migut 的帮助 YAML 属于最后一类；它可以提供维护者整理过的披露和
用法证据，但不是公开插件的默认路径，也不能取代运行时、源码、配置和当前上下文判断。

## 与执行资格的关系

帮助数据只参与 `discover` / `teach`。即使某字段由人工检查过，也不能证明当前用户、群、adapter、配置和
限流状态允许执行。普通用户、维护者和受限能力必须先在模型外完成披露过滤；真正执行仍由原插件自己的
Matcher、Permission、Rule 和 handler 决定。

## 相关资料

- [部署本地能力影子索引](flows/capability-shadow-index.md)
- [Alconna 公开能力与解析回执](flows/alconna-capability-and-parse-receipts.md)
- [ADR-0021：用部署本地影子索引整理 Bot 能力证据](../adr/0021-use-deployment-local-capability-shadow-index.md)
