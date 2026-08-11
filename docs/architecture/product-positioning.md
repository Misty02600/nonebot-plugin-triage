# 产品定位与同类能力

调研快照：2026-08-08

## 结论

NoneBot Triage Agent 不把“从群聊识别 Bug”或“用模型总结报障”作为独特卖点。相邻产品已经分别覆盖群聊
Bug 识别、错误追踪、用户反馈关联和 Agent 可观测性；本项目要验证的差异是：用明确的 `triage` 指令统一
承接能力问答与故障求助，并在 Reply 可用时把疑似故障关联到部署者本机的 NoneBot 运行证据。

这份材料是基于公开资料的时间点快照，只表示当时可核验的能力，不证明私有产品或后续版本不存在相同
实现。产品价值仍需通过真实 trial 的关联成功率、首次可行动证据耗时和维护者采纳率验证。

## 相邻产品与边界

| 产品或能力 | 已覆盖的相邻问题 | 本项目仍需证明的差异 |
|---|---|---|
| [AstrBot BugCatcher](https://github.com/Sisyphbaous-DT-Project/astrbot_plugin_bug_catcher) | 监听群聊，批量调用模型识别、分级和去重 Bug，并保存历史供 Dashboard 处理 | 报障与真实 Matcher、插件、平台 API、异常及版本证据的关联；证据不足时的最小补证与动作审批 |
| [NoneBot 错误跟踪实践](https://nonebot.dev/docs/2.4.4/best-practice/error-tracking) | 通过 Sentry 收集异常、breadcrumb、堆栈和运行环境 | 普通用户显式报障与本机事件的关联，以及 NoneBot 框架、适配器、部署和插件责任层路由 |
| [Sentry User Feedback 与 Seer](https://docs.sentry.io/product/issues/issue-details/) | 把用户反馈与错误、Trace 等上下文放入 Issue，并基于遥测和代码辅助根因分析 | QQ / NoneBot 入口、本机最小数据边界和逐动作人工审批 |
| [Jam](https://jam.dev/) / [Marker.io](https://help.marker.io/en/articles/5358889-custom-metadata) | 在用户报障时附带浏览器录屏、console、网络请求和设备信息 | Bot 消息事件、Matcher、插件所有权和 Bot 进程日志语义 |
| [Botpress Traces](https://botpress.com/docs/adk-v1-17/testing/debugging/) | 为会话、workflow、工具和模型请求建立 trace 与结构化日志 | 由群聊用户发起的报障入口和 NoneBot 社区责任路由 |
| [LangSmith Feedback](https://docs.langchain.com/langsmith/attach-user-feedback) | 将反馈绑定 trace，并从生产轨迹形成诊断与回归数据 | 非 LLM Bot 的运行证据、NoneBot 责任层和本地部署隐私默认值 |

## 冻结的产品边界

1. **显式触发**：入口要求 `triage <求助内容>`；`@Bot` 和 Reply 可选，不静默监听或长期保存全群原文。
2. **运行证据关联**：结果必须能追溯到实际事件、Matcher、插件、API 生命周期和规范化失败点，不能只有
   文本摘要。
3. **证据优先**：模型输出是不可信候选；领域策略控制证据白名单、单步补问、停止条件和失败关闭。
4. **责任层定位**：区分框架、适配器、协议实现、部署配置与社区插件，不把所有问题路由给同一维护者。
5. **审计与审批**：扩大采集、执行 Probe、修改配置、发送消息和 GitHub 写回都需要独立授权。

## 应避免的重复建设

- 不重做通用异常聚合和堆栈分组平台；Sentry 等系统可作为后续只读证据源。
- 不为第一版先做通用 Dashboard；群内维护者回执与本地审计工件足以验证闭环。
- 不用长期保存群聊与原始日志换取实现便利。
- 不在证据不足时自动创建公开 Issue、运行第三方代码或生成并应用修复。

## 可证伪指标

| 指标 | 要验证的假设 |
|---|---|
| 自动关联成功率 | 疑似故障带 Reply 时可以稳定找到对应事件与 Matcher / API 链 |
| 首次可行动证据耗时 | 比维护者按时间手工翻日志更快 |
| 责任层 Top-1 准确率 | 运行证据能减少猜错仓库或维护者 |
| 每个 Case 的补问轮数 | 单步补证降低用户流失且不遗漏关键证据 |
| 隐私删除率与误报率 | 显式触发和最小数据默认值适合真实群聊 |
| 维护者采纳率 | 建议的补证、升级或验证动作确实被使用 |

如果 trial 只能改善文本表达，却不能提高证据关联率、缩短定位时间或改善责任路由，应停止扩张入口并
重新评估产品定位。

## 相关决定

- [ADR-0001：QQ 群显式报障与本机运行证据关联](../adr/0001-qq-group-report-linked-runtime-evidence.md)
- [ADR-0002：分级自治与所有权感知修复](../adr/0002-tiered-autonomy-and-ownership-aware-remediation.md)
- [ADR-0003：统一能力教学、指令纠错与故障受理入口](../adr/0003-unified-capability-guidance-and-incident-intake.md)
- [架构概览](overview.md)
