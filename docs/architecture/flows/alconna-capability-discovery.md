# 流程：Alconna 能力发现

当前运行入口通过两条互补路径发现 Bot 已经加载的 Alconna 能力：显式公开能力 Provider 优先提供经过
能力所有者确认的公开说明；默认启用的部署本地影子索引从已加载插件构建候选事实，再按受众、平台与
证据状态生成可服务视图。两条路径都不会重新解析用户消息，也不会执行 Matcher、命令 behavior、
executor 或 handler。

## 这条流程保证什么

能力教学必须基于 Bot 当前实际注册且允许向当前用户披露的命令，而不是模型记忆、README 猜测或未经
约束的源码反射。显式 Provider 是能力所有者的公开声明；影子索引补充部署中已经观察到的命令结构和
来源证据，但只有派生 ServingView 判定可公开的记录才能进入普通用户检索。

外部插件的描述、用法、示例和源码都是不可信证据。它们可以支持事实说明，不能覆盖系统策略、发起工具
调用或证明用户有权执行对应命令。

## 显式公开能力 Provider

`register_public_alconna_capability` 登记允许支持入口说明的 Alconna 命令。没有权限或场景差异的公开命令
可以直接登记；存在差异时必须提供无副作用、非阻塞的 `is_visible(bot, event)`。可见性检查有短超时，
返回后还会重新确认命令没有被停用、替换或注销。

未登记、`CommandMeta.hide=True`、停用、过期或检查失败的命令一律不通过 Provider 展示。首版 Provider
以整条命令为粒度，因此混合普通与管理子命令的命令不能只靠一个布尔回调局部放行；卸载或替换命令前，
能力所有者必须调用 `unregister_public_alconna_capability` 解除旧登记。

## 部署本地影子快照

影子采集发生在 NoneBot 已经加载插件之后，不额外导入第三方模块。采集器读取已加载 Matcher、Rule、
Permission 与 Alconna 对象的静态结构，并把命令头、参数、组件、快捷方式、来源和约束投影为字段级
Claim、Evidence 与 Constraint。它不调用 Rule、Permission、handler、Alconna `parse()`、behavior 或
executor。

```text
loaded NoneBot plugins
          ↓
read-only capability snapshot
          ↓
field-level claims and evidence
          ↓
audience/platform qualification
          ↓
deployment-local ServingView
```

普通用户只查询当前 adapter 下的 `public` ServingView；`restricted`、跨平台缺口、动态入口和证据冲突
不会泄漏到普通用户候选域。SUPERUSER 也必须先在模型外完成当前上下文鉴权，才能检索维护者视图。
stale generation、partial snapshot 或索引错误都按对应边界失败关闭，不能把候选记录自动升级成公开能力。

第三方 distribution 可以使用安装版本和 resolved VCS commit 作为来源修订；本地、editable、无版本或
无 Git 的插件使用模块源码内容摘要。快照和索引是可删除重建的部署本地派生数据，不要求存在 `uv.lock`，
也不读取 `.env` 或配置值。

## 回答与执行边界

`triage <功能问题>` 优先读取当前用户可见的显式 Provider 能力，未命中时再查询影子 ServingView。
模型只能基于已经通过受众过滤的字段回答。即使发现某条命令，也不能据此推断本次权限、会话条件、
限流状态或执行资格；这些结论必须有各自的可靠证据。

能力发现不代用户执行命令，也不重放用户输入。真实命令解析仍完全属于原插件的 Alconna / NoneBot 调用
链。本项目不维护平行的解析状态或解析回执协议。

## 当前边界

- 显式 Provider 仍以整条命令为公开粒度，没有字段级执行约束；
- 影子分析不能证明自定义 Rule、Permission 和 handler 的动态效果；
- 未解决的动态入口、平台缺口和证据问题保存在 `analysis_issues` 中；
- 同一路径存在多个冲突命令时保留各自候选，后续入口仍需结合 Matcher 身份消歧；
- 影子快照是部署本地派生事实，不是跨部署共享的权威配置；
- 能力发现、字段说明和最终执行是三个不同判断，不能相互替代。

## 相关决定与流程

- [部署本地能力影子索引](capability-shadow-index.md)
- [支持入口分流](support-intake-routing.md)
- [ADR-0003：统一能力导航与故障入口](../../adr/0003-unified-capability-guidance-and-incident-intake.md)
- [ADR-0021：用部署本地影子索引整理 Bot 能力证据](../../adr/0021-use-deployment-local-capability-shadow-index.md)
- [ADR-0024：自动公开确定且低风险的能力字段](../../adr/0024-auto-publish-deterministic-capability-fields.md)
- [ADR-0026：在检索与模型前隔离能力知识受众域](../../adr/0026-filter-capability-knowledge-before-retrieval.md)
