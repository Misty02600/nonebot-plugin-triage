# 流程：Alconna 公开能力与解析回执

当前运行入口优先支持显式公开能力 Provider。默认启用、由 LocalStore 管理 cache 的部署本地影子索引会读取已经加载的 Alconna 与
普通 Matcher：普通用户可检索派生 ServingView 中自动确定为 `public` 的命令，SUPERUSER 通过模型外鉴权后
还可读取带具体分析问题或受限标签的记录；解析回执仍是仓库级实验，位于
`tools/nbtriage_maintainer/alconna_capabilities.py`，不进入插件 wheel。

## 这条流程保证什么

能力教学和指令纠错必须基于 Bot 当前实际注册且明确允许公开的命令，而不是模型记忆、README 猜测或源码反射。
`snapshot_alconna_capabilities` 从 Alconna `command_manager` 读取结构化命令 AST；
`adapt_alconna_parse_result` 只消费现有 NoneBot / Alconna 调用链已经产生的 `Arparma`，再把结果压缩为入口
路由可用的固定状态。两者都不执行 Matcher 或命令 handler，也不向模型、网络或外部工具发起调用。

## 当前运行路径

`register_public_alconna_capability` 由能力所有者显式登记可公开的 Alconna 命令。没有权限或场景差异的公开
命令可直接登记；有差异时必须提供无副作用、非阻塞的 `is_visible(bot, event)`。可见性异步检查有短超时，
返回后还会重新确认命令没有被停用、替换或注销。未登记、`CommandMeta.hide=True`、停用、过期或检查失败的
命令一律不展示。

首版 Provider 的粒度是整条命令，因此只有命令及其 description、usage、example 全部适合普通用户公开时
才能登记。混合普通与管理子命令的命令不能只靠一个布尔回调局部放行；卸载或替换命令前，能力所有者必须
调用 `unregister_public_alconna_capability` 解除旧登记。

`triage <功能问题>` 先读取已登记命令的 header、description、usage、example 和主参数，未命中时再查询
影子索引。确定命令入口、当前 adapter 可判定且没有 hidden、SUPERUSER-only、停用等受限信号时会自动
`public`，因此第三方插件不必逐个接入 Provider 才能被普通用户发现；显式 Provider 仍是优先级更高的公开
声明。该路径不会调用任意命令的 `parse()`、behavior、executor 或 handler。所有 `triage` 求助先过轻量
入口限流。

影子索引把未解决的动态入口、平台缺口和证据问题保存为具体 `analysis_issues`，把 `SUPERUSER`、
`CommandMeta.hide=True` 和内部管理命令保存为 `restricted`。普通用户不会读取这些记录；SUPERUSER 可在
显式 Provider 未命中时检索维护者域，但回复只复述已有字段，并明确具体 issue、受限、opaque 和执行资格
未知。普通用户查询在 SQL 召回前把候选限定为当前 adapter 派生 ServingView 中的 `public` 能力，stale
generation 直接失败关闭；未来的源码工具和模型上下文也必须沿用
同一受众域。未命中回复不能提示受限命令或其他平台能力存在。自动分析确认命令为 hidden /
SUPERUSER-only 后，也不把其源码默认发送给 LLM。

目标回答合同不固定未命中、限流或权限话术。普通用户的空候选域只提供“没有命中可说明能力”这一事实；
模型不能获知记录是不存在、受限还是属于其他 adapter。对已经 public 的能力，只有可靠 Evidence 支持时才
说明群聊 / 私聊、群管理员等可观察条件，以及限流是否存在、作用域、额度、窗口或重置方式；静态规则不能
冒充本次已经被限流的运行结论，也不展示 Permission 类名、限流库或配置字段。

## 部署本地影子快照

影子采集发生在 NoneBot 已经加载插件之后，不额外导入第三方模块。Alconna 命令结构与普通
`CommandRule` 是 `observed` Claim；PluginMetadata、README 或可选帮助数据分别作为 `declared` /
`documented` Claim，并保留 Evidence 来源。自定义 Rule、Permission、限流器和 handler 判断只登记为
`opaque` Constraint。影子受众态只有 `public / restricted`，平台范围为 `all / explicit / unknown`，
`analysis_issues` 逐项记录动态入口、平台未知、证据冲突、敏感歧义或证据不足；不另存
`ready / pending / conflicted`。其中 `restricted` 会持久化，但只能在
模型外完成当前上下文鉴权后检索。后续需要完全排除的能力将由独立 operator exclude policy 在持久化前
处理，不是另一种披露态；这个按能力排除接口当前尚未实现。

第三方 distribution 可以使用安装版本和 resolved VCS commit 作为来源修订；本地、editable、无版本或
无 Git 的插件使用模块源码内容摘要。两种路径都不要求存在 `uv.lock`，也不读取 `.env` 或配置值。完整
流程见[部署本地能力影子索引](capability-shadow-index.md)。

## 能力快照边界

```text
installed plugin registers Alconna
                ↓
       command_manager snapshot
                ↓
visible + enabled commands only by default
                ↓
command meta + Args + Option/Subcommand AST
                ↓
       AlconnaCapability schema v1
```

快照保存命名空间、能力标识、展示头、描述、用法、示例、启用状态，以及递归的主参数、选项和子命令结构。
参数只保留路径、名称、是否必填、公开类型显示和注释。Alconna 自动注入的帮助、补全与快捷方式组件不作为
Bot 业务能力重复暴露；`meta.extra`、author、Matcher、behavior、executor 和实际匹配值不进入快照。

这里的仓库级 `snapshot_alconna_capabilities` 实验默认过滤 `CommandMeta.hide=True` 和命令管理器已停用的
命令，调用方可显式纳入二者用于维护者视图；部署本地影子采集则把 `hide=True` 映射为持久化的
`restricted`，而不是丢弃。两种路径都不等于普通用户有权使用；真正的用户 / 群权限、适配器支持、会话
场景和插件所有权仍需后续适配器过滤。
描述、用法和示例来自已安装插件，属于不受信元数据；即使以后进入 LLM 上下文，也只能作为带边界的证据，
不能覆盖系统策略或触发工具。

## 真实解析结果到入口状态

适配器要求 `Arparma.source` 与显式传入的注册命令是同一对象，然后按公开结果字段与冻结异常类型映射：

| Alconna 结果 | `CommandStatus` | 固定原因 |
|---|---|---|
| `matched=True` | `parsed` | `matched` |
| 已触发 Alconna 内置帮助 / 补全等选项 | `parsed` | `builtin_option` |
| 命令头未匹配 | `unknown_command` | `header_unmatched` 或 `fuzzy_header_suggestion` |
| `ArgumentMissing` | `missing_argument` | `argument_missing` |
| `InvalidParam` 且命令头已匹配 | `invalid_argument` | `invalid_parameter` |
| `ParamsUnmatched` | `invalid_argument` | `unmatched_parameter` |
| `UnexpectedElement` | `invalid_argument` | `unexpected_element` |

回执只包含 schema、能力标识、状态、固定原因和命令头是否匹配。`origin`、`error_data`、异常消息、错误输入、
匹配参数和值全部丢弃。来源错绑、命令已注销或未知异常类型直接失败，不把框架 / behavior 问题猜成用户输入
错误。

## 为什么适配器不主动重跑 `parse()`

Alconna 官方接口允许通过 `bind()` 为命令绑定主动 executor；`parse()` 匹配成功后会运行 behavior 和这些
executor，内置快捷方式也可能修改解析器状态。因此 NoneBot Triage Agent 不能为了“确认用户是否输错”重新把原
消息喂给任意注册命令。真实入口必须在原有解析调用链中取得结果，再调用纯适配函数。

NoneBot Alconna 的公开 `Extension.parse_wrapper` 是后续接入候选，但当前版本的规则会在命令头未匹配、
`skip_for_unmatch`、自动帮助输出或权限拒绝时提前返回，所以只依赖全局 extension 无法完整观察未知命令和
全部参数失败。真实 NoneBot 计划必须选择更早的只读观察点，并用测试证明不会改变插件原有匹配和响应顺序。

## 当前未完成部分

- 尚未为丰富解析回执注册真实 NoneBot extension 或只读 rule hook；`triage` Matcher 已是当前运行入口；
- 普通用户只接入当前 adapter 派生 ServingView 中无 blocking issue、记录状态为 `verified / candidate` 的
  `public` 命令；带 issue、`conflicted / stale` 或 `restricted` 的记录仍只进入 SUPERUSER 的确定性维护者
  回复，字段冲突归类工作流尚未实现；
- 真实解析回执尚未接入运行入口；
- 没有代用户执行命令；
- 没有推断 `prefix_error`、权限、会话上下文、适配器不支持或能力停用回执；
- `is_visible` 仍同时承担披露与当前上下文可见性，`PublicCapability` 尚无字段级执行约束；
- 同一路径存在多个冲突命令时仍保留各自快照，未来入口需要结合 NoneBot 冲突策略和 Matcher 身份消歧；
- 能力快照是进程内即时事实，不是跨重启持久化配置。

## 相关决定与计划

- [ADR-0003：统一能力导航与故障入口](../../adr/0003-unified-capability-guidance-and-incident-intake.md)
- [ADR-0022：SUPERUSER 能力影子候选检索](../../adr/0022-limit-capability-shadow-guidance-to-superusers.md)
- [ADR-0026：在检索与模型前隔离能力知识受众域](../../adr/0026-filter-capability-knowledge-before-retrieval.md)
- [ADR-0027：用事实输出合同约束能力帮助](../../adr/0027-constrain-guidance-with-facts-not-fixed-wording.md)
- [ADR-0032：分离能力受众、平台范围与分析问题](../../adr/0032-separate-capability-audience-analysis-and-platform-status.md)
- [显式支持入口分流](support-intake-routing.md)
