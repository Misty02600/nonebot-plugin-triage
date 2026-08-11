# ADR-0014：先用观察型生产 trial 建立可评测闭环

## 状态

已采纳；入口与 trial 门槛部分被 [ADR-0020](0020-use-triage-command-for-natural-language-support.md) 替代

## 日期

2026-08-10

## 当时遇到了什么

项目已经具备精确回复报障、最小运行证据、短期 incident 聚类、SUPERUSER 查询、离线 Agent Gate 与受控
模型适配边界，但还没有可以在真实 Bot 中持续收集“这次受理是否被查看、结论是否有用”的生产反馈闭环。
如果等待模型、Prompt、完整 Agent 工作流和所有 Provider 资格一次完成，首次投入使用会被长期推迟；如果
直接把未资格化模型放入普通用户链路，又会同时引入费用、隐私、延迟和错误结论曝光风险。

Agent 系统包含模型、Prompt、工作流、工具、权限、记忆、评测、监控和交互等多层。当前产品问题不是这些层
是否都存在，而是能否先证明最窄任务的成功标准、安全停止条件和真实反馈。因此需要先冻结一个不依赖模型的
生产 trial 边界，再让后续模型与工具能力沿同一审计生命周期逐层加入。

## 最后决定

1. 第一版生产 trial 采用 `observe` 模式。只有普通用户已经通过精确 Reply、群聊场景、限流和近期引用门后
   形成的 `LiveIncident` 才会建立 trial；后台异常本身不会自动创建 incident 或 trial；
2. 当前策略版本固定为 `intake-v1`，成功标准是 incident 被正确受理、证据可由维护者查询、同形失败可聚类，
   并取得 `有用 / 不完整 / 不正确` 三值反馈。无 Reply、私聊、过期引用、限流和内部错误继续在模型前停止；
3. trial 状态位于传输无关领域核心，包含不透明 trial / incident ID、活动 cluster、运行状态、查询次数、反馈
   revision 和稳定 sequence；不保存平台用户、群、消息正文、API 参数、异常消息或私有思维过程；
4. `observe` 模式必须配置审计 sink。首个 sink 是单进程、本地、按大小轮转的 JSONL；每行只包含固定 schema、
   最小失败形状、计数、延迟和枚举反馈。写入失败不改变已受理报障，但必须计入审计事件丢弃数；
5. 查询曝光、反馈和统计命令只允许 NoneBot `SUPERUSER`。反馈值不接收自由文本，统计只返回活动 TTL 内聚合
   计数，不提供任意日志检索或导出；
6. trial 默认 `off`。启用 `observe` 不会启用模型、读取 Provider 密钥、执行工具或增加外部写入；日志路径、
   文件上限和备份数由部署者显式配置，默认相对路径必须在宿主项目忽略或放到外部日志目录；
7. 后续只有积累足够真实 incident 与人工反馈后，才讨论模型 shadow trial。届时用新的 schema 版本记录
   workflow / Prompt / model / tool trajectory、预算和停止原因；不在当前 schema 填充大量永远为空的字段；
8. shadow 输出先只供维护者对照，不随机改变普通用户响应。只有离线 Gate、真实 shadow 质量、安全、延迟、
   费用和日志健康同时达到预先冻结门槛，才另行决定 canary 或用户可见诊断。

## 为什么这样选

- 先获得真实受理、查询和反馈分母，能够判断后续模型或 Prompt 改动是否真的改善产品，而不是只比较 demo；
- trial 与模型解耦，允许插件先投入实际使用，同时保持当前零模型 Matcher 和空资格注册表不变；
- 枚举反馈、固定事件 schema 和本地轮转日志可直接聚合，也不会把日志文件变成聊天正文或异常消息后门；
- 观测写入 fail-open 保证诊断插件故障不会反过来阻断 Bot 报障入口；明确 drop counter 又避免静默丢证据；
- 先 observation、再 shadow、最后 canary 的顺序把可逆体验优化放后，把安全、状态和评测基础放前。

## 没有采用的方案

- **直接在普通用户报障后自动调用模型**：路径最短，但当前无插件合格模型组合，也缺真实费用与数据出站门；
- **一开始做随机 A/B 或 canary**：没有稳定 candidate 和反馈分母时，随机化只会扩大状态空间和解释成本；
- **保存原始群聊或完整异常日志供以后分析**：信息更丰富，但违反数据最小化并扩大泄漏面；
- **只依赖现有控制台文本日志**：无法稳定关联 trial 生命周期、策略版本、反馈和 drop rate；
- **现在一次定义完整 Agent telemetry schema**：会把尚未验证的模型、工具和记忆结构过早冻结为产品契约。

## 带来的影响

- `LiveTrialService` 成为后续生产 shadow/canary 的传输无关生命周期边界，但当前只承担 observation-only 状态；
- `LiveReportService` 在 incident 已受理后尝试建立 trial；trial 观测失败不改变公开结果；
- 部署者启用试运行时会产生本地 JSONL 写入，需要为单进程日志设置路径、轮转大小、备份数和宿主忽略规则；
- 维护者可用精确命令记录枚举反馈和查看活动聚合；普通用户入口、模型资格、工具权限和外部副作用不变；
- trial 日志是本地运营工件，不是公开数据集。发布、上传或用于模型训练前仍需单独做来源、隐私和许可证复核。
- 离线汇总器只输出轮转窗口计数、覆盖率、入口延迟、时间范围、mode 与策略版本；它严格拒绝未知事件语义并把损坏行
  计数，不把原始事件或任何不透明标识复制到终端输出。

## 相关文档

- [观察型生产 trial 流程](../architecture/flows/observation-first-trials.md)
- [跨平台显式报障入口](../architecture/flows/cross-platform-report-intake.md)
- [短期显式报障聚类](../architecture/flows/incident-clustering.md)
- [ADR-0010：用有界证据获取循环验证 Agent 能力](0010-use-bounded-evidence-seeking-agent-loop.md)
- [观察型生产 trial](../architecture/flows/observation-first-trials.md)
