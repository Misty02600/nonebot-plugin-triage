# ADR-0020：用 triage 指令承接自然语言求助，Reply 只补充上下文

| 状态 | 决策日期 |
|---|---|
| 已采纳 | 2026-08-11 |

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

Alconna 已接收 `triage` 后的任意数量文本参数，入口也已区分功能问法、明确故障和待澄清请求。能力说明只
读取插件显式登记的公开 Alconna 能力；未登记、`CommandMeta.hide=True` 或停用命令不会展示，也不会重新
运行命令解析或 handler。

插件模型资格表当前仍为空，因此还没有启用模型 Agent。现阶段使用确定性首轮分流，无法判断的请求只追问
一次；后续模型意图边界必须继续输出受限的 `IntakeSignals`，不能让原文直接控制工具。

ADR-0021 已加入默认关闭的部署本地能力影子索引，自动收集已加载插件的候选证据。按 ADR-0022，普通用户
仍只读取显式公开 Provider；`SUPERUSER` 在模型外完成当前 Bot / Event 鉴权后，可通过 `triage` 检索带
披露标签的 `public / review / restricted` 候选。该路径只复述已有字段并提示执行资格未知，不启用模型或
运行第三方 Permission、Rule、handler。

## 替代关系

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
