# ADR-0003：用统一入口承接能力导航、指令纠错与故障报障

| 状态 | 决策日期 |
|---|---|
| 已采纳；入口触发细节被 [ADR-0020](0020-use-triage-command-for-natural-language-support.md) 替代，显式 Provider 唯一接入策略被 [ADR-0021](0021-use-deployment-local-capability-shadow-index.md) 部分替代 | 2026-08-08 |

## 当时遇到了什么

独立 Bot 安装多个插件后，普通用户往往不知道能力存在、不会写指令，或只能描述“我想让 Bot 明天提醒
我”。即使知道指令，也可能因为前缀、参数、权限、群聊 / 私聊场景或插件开关输入失败。若 NoneBot Triage Agent
把所有“Bot 没反应”直接送进插件故障诊断，会把正常的使用问题误报为 Bug，浪费模型、维护者和上游社区
时间。

相邻社区已经普遍提供帮助列表和单命令语法，AstrBot 也支持模型通过 Function Calling 代用户调用工具；
但本轮官方资料核查没有发现成熟的通用闭环，能按当前 Bot 已安装能力、平台、会话和权限教学，在真实解析
失败后纠错，并在正确指令仍失败时无缝转入运行证据诊断。“列帮助”“代执行”“教会并纠错”是三个不同
产品能力。

## 决策

NoneBot Triage Agent 使用同一个**显式支持入口**承接能力导航和故障报障。首版仍由用户 `@Bot` 或回复具体消息
主动触发，不静默分析普通群聊，也不把每次未匹配消息都视为报障。

> 后续修订：ADR-0020 将触发方式收敛为必选 `triage` 指令，`@Bot` 与 Reply 均改为可选；本 ADR 的五类
> disposition 与统一入口目标继续有效。

入口先产生独立于技术责任层的 `IntakeDisposition`：

1. `capability_guidance`：用户在问 Bot 能否完成某件事或怎样使用；
2. `usage_error`：已知或疑似指令存在语法、参数、前缀、权限、会话场景或插件启用问题；
3. `suspected_incident`：正确用法下仍出现无响应、异常、错误行为或外部依赖失败；
4. `out_of_scope`：与当前 Bot 能力和运行无关；
5. `unsafe`：试图获取秘密、任意日志、越权执行或通过报障文本控制维护工具。

`IntakeDisposition` 不加入现有 `ResponsibilityLayer`。前者回答“现在该帮助、纠错、诊断还是拒绝”，后者
只在 `suspected_incident` 分支回答“技术问题可能属于环境、插件、框架、适配器、平台还是外部服务”。
这避免把“用户少写了参数”伪装成插件所有权结论。

## 稳定流程

```text
explicit @ / reply report
        ↓
permission, rate limit and minimal-context gate
        ↓
untrusted-text safety guard
        ↓
IntakeDisposition
   ├─ capability_guidance → capability registry → scene/permission filter → explain one command
   ├─ usage_error         → parser/permission receipt → corrected example → user retries
   │                                                              ├─ success → close as guidance
   │                                                              └─ still fails → suspected incident
   ├─ suspected_incident  → correlate runtime evidence → responsibility hypotheses → minimal evidence
   ├─ out_of_scope        → explain boundary
   └─ unsafe              → refuse without diagnostic or execution tools
```

能力导航的事实来源应优先是结构化命令注册表、插件元数据、当前启用状态、适配器 / 会话类型和权限判定；
LLM 负责理解意图、排序候选、用自然语言解释和逐项追问，不能凭 README 记忆编造指令。Alconna 能提供
命令、参数、选项与帮助元数据；普通 NoneBot Matcher 若没有结构化命令元数据，只能通过显式能力提供者接入
或标记低置信，不能声称完整发现全部 Bot 能力。

指令纠错应使用实际解析或权限结果，例如未知命令、缺少参数、参数类型错误、前缀错误、权限不足、当前
群聊不支持或插件未启用。若没有结构化失败证据，Agent 只能给候选建议，不能把猜测说成真实失败原因。

## 为什么不是先判断“这到底是不是真的 Bug”

用户观察到的异常可能真实存在，但原因既可能是用法，也可能是配置、插件、平台或外部服务。系统不采用
一次 LLM 二分类决定“真 / 假问题”，而是建立可修订的支持假设：先排除明确越权与无关请求，再用能力注册
表、解析回执和运行证据逐步收缩。即使没有异常堆栈，“正确指令触发了错误行为”仍可以是有效事件；即使
有用户抱怨，“当前会话没有权限”也不应进入上游 Bug 报告。

## 安全与隐私边界

- 被回复消息和用户文字始终是不可信证据，不能包含对 Agent、工具或维护者身份的可执行指令；
- 在意图判断前先做权限、频率、字段和秘密守门，普通用户不能借“帮我排查”读取任意日志或配置；
- 只获取被引用消息、当前显式请求和已有不透明关联标识，不默认扩张到整段群聊；
- 能力导航只展示当前用户在当前场景可见的能力，不能泄露隐藏管理命令；
- 给出正确指令不等于代用户执行。写配置、管理群、订阅、删除等有副作用能力仍进入动作授权策略；
- 恶意、无关和证据不足是不同状态：恶意请求拒绝，无关请求说明边界，证据不足只问一个高价值问题。

## 没有采用的方向

### 每条未匹配消息都让 LLM 判断是否需要帮助

会恢复为全群静默监听，扩大隐私、费用和误触发，也会与 ADR-0001 的显式报障入口冲突。

### 先让 LLM 阅读全部插件 README 再自由回答

README 可能过期、缺少当前启用状态与权限信息，也难以可靠解释实际解析失败；适合作为补充材料，不应成为
命令事实源。

### 把指令输错直接归到 `plugin` 责任层

会污染技术所有权标签和上游 Issue 路由。使用问题应先由入口分流，只有正确用法仍失败才进入责任层诊断。

### 自然语言请求默认直接代用户执行

虽然交互更短，但会混淆教学与授权，尤其无法安全覆盖管理、删除、订阅和配置类命令。首版应先证明导航与
纠错价值，再按动作风险讨论代执行。

## 已确认事项

### D-001：采纳统一显式入口和独立 `IntakeDisposition`

- 影响范围：真实用户入口、SupportCase 建模、评测标签和后续插件交互。
- 确认内容：能力导航、指令纠错、疑似故障、无关和不安全请求先分流；只有疑似故障进入技术责任层。
- 确认结果：已确认；只有疑似故障进入技术责任层。

### D-002：首版只教学和纠错，不代用户执行有副作用指令

- 影响范围：用户授权、安全边界、命令包装和会话状态。
- 倾向：首版可以给出可复制指令和逐项补参，但不自动执行；未来只对类型化、已标注风险的能力增加确认后
  代执行。
- 确认结果：已确认；未来代执行需要按动作风险另行设计授权。

### D-003：能力注册表先覆盖 Alconna

- 影响范围：首批可发现命令比例、插件接入成本和演示完成度。
- 建议：先完整支持 Alconna 的结构化元数据，再定义普通 NoneBot Matcher 的显式能力提供协议；不要通过
  反射和 README 猜出一个看似完整但不可验证的注册表。
- 确认结果：已确认；普通 Matcher 后续通过显式能力提供协议接入。

> 后续修订：ADR-0021 保留显式 Provider 作为披露和可见性声明，同时允许普通 Matcher、运行时结构、源码
> 与可选帮助数据进入部署本地的 `review` 影子索引；影子候选不因此获得执行资格。

## 落实与确认

- 实施情况：确定性领域分流契约、`triage <自然语言>` 入口和窄的显式公开 Alconna Provider 已完成。
  当前 Matcher 已删除自然语言词表快判，只区分空输入与待澄清请求；固定话术不直接建立 incident，完整
  语义 assessment 尚未接入。
- 实施证据：[支持入口流程](../architecture/flows/support-intake-routing.md)、
  [ADR-0020](0020-use-triage-command-for-natural-language-support.md)。

## 相关文档

- [ADR-0040：只有可信初检仍失败才进入 incident](0040-require-trusted-preflight-failure-before-incident.md)
- [ADR-0001：QQ 群显式报障与本机运行证据](0001-qq-group-report-linked-runtime-evidence.md)
- [ADR-0002：分级自治与所有权感知修复](0002-tiered-autonomy-and-ownership-aware-remediation.md)
- [产品定位与同类能力](../architecture/product-positioning.md)
