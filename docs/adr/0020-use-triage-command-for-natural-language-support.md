# ADR-0020：用 triage 指令承接自然语言求助，Reply 只补充上下文

| 状态 | 决策日期 |
|---|---|
| 已采纳；续问触发由 ADR-0031 恢复并细化，动态入口配置与两级限流由 ADR-0045 部分替代 | 2026-08-11 |

## 当时遇到了什么

首个真实入口只接受精确 `@Bot 报错`。把 Reply 改成可选后，无 Reply 请求虽然能得到编号，却仍不能表达
“某个功能怎么使用”，也会把普通帮助错误地计入 incident 和 trial。

插件需要一个明确入口，避免抢占 Bot 的其他功能；入口后的内容又必须允许自然语言，不能把“报错”误当作
唯一产品意图。

## 决策

1. 普通用户入口是 `triage <自然语言求助>`。`triage` 必须出现，`@Bot` 可选；不监听其他普通消息。
2. `triage` 后的文字是主要输入，可以询问能力、描述用法错误或报告异常。文字始终是不可信证据，不能直接
   升级为命令执行、工具调用或维护动作。
3. 入口按 ADR-0003 分流为能力说明、用法纠错、疑似故障、无关请求和危险请求。只有疑似故障建立
   `LiveIncident`、受理编号、trial 和失败聚类；普通帮助直接回答或只追问一个关键问题。
4. Reply 是可选上下文，不决定意图。有 Reply 时只读取第一个结构化 `Reply.id`，并且只在疑似故障分支
   尝试关联最小运行证据；不读取或保存 Reply 的正文与 origin。
5. Reply 未命中近期引用时仍继续处理当前求助，并明确说明没有关联到运行记录；系统不按时间猜测其他消息。
6. 维护者查询、反馈和统计仍是独立的精确 `SUPERUSER` 指令，并保持现有 `@Bot` 规则。
7. 所有 `triage` 请求先经过轻量 HMAC 限流；疑似故障另有限制建单频率的独立限流。短期内存、无自动
   Issue、无自动执行和无修复副作用等边界不变。

## 当前实现边界

Alconna 已接收 `triage` 后的任意数量文本参数。入口已删除自然语言词表快判，过渡期只确定性区分空输入和
待澄清请求。能力说明
优先读取插件显式登记的公开 Alconna 能力，未命中时还可读取当前 adapter 中自动确定为 `public` 的影子
能力；`CommandMeta.hide=True`、停用、带阻塞 `analysis_issues` 或 `restricted` 的能力不会进入普通用户候选，
也不会重新运行命令解析或 handler。

语义 assessment 已确定为每轮 `triage` 的默认目标路径，不设置产品级启用开关；当前专用分类器尚未接线，
真实 transport 资格表也为空。现阶段所有非空自由文本都会安全降级；首次请求会
建立不含正文的短期澄清 Thread，用户可发送带精确 Reply 的新 `triage` 请求来补充意图。固定话术不会在首轮
或续问中直接建单；澄清续答只能转入功能教学或终止态，教学回答则可在 Thread TTL 内通过最近一次已登记
回答继续相关问答，但每轮仍要求显式 `triage`。后续模型意图边界必须继续输出受限的 `IntakeSignals`，
`REPORT_PROBLEM` 只作为确定性副作用闸门的输入，不能让原文直接控制工具。

ADR-0021 已加入默认关闭的部署本地能力影子索引，自动收集已加载插件的候选证据。ADR-0024 后，普通用户
还可查询当前 adapter 的自动 `public` 能力；`SUPERUSER` 在模型外完成当前 Bot / Event 鉴权后，可通过
`triage` 检索带具体 `analysis_issues` 的未解决能力和 `restricted` 能力。两条影子路径只复述已有字段，
不启用模型或运行第三方 Permission、Rule、handler。

## 替代关系

- [ADR-0045](0045-use-one-triage-cooldown-and-localstore-capability-cache.md) 部分替代本 ADR 的动态命令、
  Matcher 优先级、入口长度配置和第 7 条两级限流；命令与边界保留为固定产品合同，每轮只消费统一入口冷却。
- “疑似故障”的可达条件已由 [ADR-0040](0040-require-trusted-preflight-failure-before-incident.md) 收紧：
  用户报告只形成未验证信号；没有模型外可信初检失败时不得建单。
- [ADR-0031](0031-require-triage-for-support-thread-continuation.md) 恢复并细化本 ADR 的显式入口：Thread 续问
  同样要求 `triage`，精确 Reply 只负责选择可续接 Thread；该决定替代 ADR-0030 的免命令例外；

- 部分替代 [ADR-0003](0003-unified-capability-guidance-and-incident-intake.md) 的 `@Bot` / Reply 触发细节；
  统一入口、五类 disposition 与只有疑似故障进入技术责任层的决定继续有效；
- 部分替代 [ADR-0006](0006-cross-platform-alconna-entry-and-reference-providers.md) 的精确 `报错`、必须
  `to_me()` 和不读取当前请求文字约束；跨平台外壳、Reply ID、引用 Provider 与隐私边界继续有效；
- 部分替代 [ADR-0014](0014-use-observation-first-production-trials.md) 的精确 Reply 入口门槛；只有疑似故障
  进入 trial，普通能力问答不进入；
- 落实 ADR-0003 的统一显式支持入口。

## 相关文档

- [支持入口分流](../architecture/flows/support-intake-routing.md)
- [跨平台支持入口](../architecture/flows/cross-platform-report-intake.md)
- [观察型生产 trial](../architecture/flows/observation-first-trials.md)
