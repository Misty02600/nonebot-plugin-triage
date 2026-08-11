# 流程：Alconna 能力快照与解析回执

> 当前状态：仓库级实验，代码位于 `tools/nbtriage_maintainer/alconna_capabilities.py`，不进入插件 wheel，
> 也尚未接入 NoneBot 运行入口。下述内容定义未来接入前需要保留的行为和安全边界。

## 这条流程保证什么

未来接入的能力教学和指令纠错必须基于 Bot 当前实际注册的命令，而不是模型记忆、README 猜测或源码反射。
`snapshot_alconna_capabilities` 从 Alconna `command_manager` 读取结构化命令 AST；
`adapt_alconna_parse_result` 只消费现有 NoneBot / Alconna 调用链已经产生的 `Arparma`，再把结果压缩为入口
路由可用的固定状态。两者都不执行 Matcher 或命令 handler，也不向模型、网络或外部工具发起调用。

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

默认过滤 `CommandMeta.hide=True` 和命令管理器已停用的命令。调用方可以显式纳入二者用于维护者视图，但
这不等于普通用户有权使用；真正的用户 / 群权限、适配器支持、会话场景和插件所有权仍需后续适配器过滤。
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

- 没有注册真实 NoneBot extension、rule hook 或 QQ 事件入口；
- 没有把能力元数据转换为群内自然语言，也没有代用户执行命令；
- 没有推断 `prefix_error`、权限、会话上下文、适配器不支持或能力停用回执；
- 同一路径存在多个冲突命令时仍保留各自快照，未来入口需要结合 NoneBot 冲突策略和 Matcher 身份消歧；
- 能力快照是进程内即时事实，不是跨重启持久化配置。

## 相关决定与计划

- [ADR-0003：统一能力导航与故障入口](../../adr/0003-unified-capability-guidance-and-incident-intake.md)
- [显式支持入口分流](support-intake-routing.md)
