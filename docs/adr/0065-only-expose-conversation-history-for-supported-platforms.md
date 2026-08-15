# ADR-0065：只为明确支持的平台提供 Bug 会话历史工具

- 状态：已采纳
- 决策日期：2026-08-15

## 背景

ADR-0064 曾决定用 Bot 进程内的最近消息窗口，为没有原生历史接口的 Adapter 模拟跨平台会话历史。
这种做法虽然能提高部分请求的上下文召回，却把“本进程恰好观察到过一些消息”包装成了平台历史能力，
还引入每个活跃会话的常驻内存、启动时事件采集和额外的完整性语义。

当前产品更需要诚实表达证据能力：平台没有可验证的历史读取实现时，Agent 不应看到一个只能返回局部缓存或
`unsupported` 的会话工具。当前 `triage` 请求和显式 Reply 已经能覆盖最相关的用户选择上下文；剩余证据
不足时，`unknown` 是允许且正确的结果。

## 决定

1. 删除 Triage 自建的跨平台最近消息缓冲，不在 Adapter 入站阶段为 Bug 调查维护每个会话的滚动窗口。
2. 只有当前 Adapter 已绑定并实现了会话历史 Provider 时，`BugAssessmentToolbox` 才向 Agent 暴露
   `read_conversation_context()`；没有 Provider 时，这个工具必须从本轮工具定义中完全消失，而不是返回空页
   或 `unsupported`。
3. 首版历史 Provider 仍是 OneBot V11 / NapCat 群历史。每案最多调用一次、最多读取最新 30 条，并继续受
   单条、总字符、模型请求、token、费用与 deadline 预算约束。
4. 已支持平台的历史 API 调用失败时返回 `unavailable` / `partial` 证据，不回退到本地滚动窗口，也不能把
   失败解释成“没有相关聊天”。
5. 当前显式 `triage` 文字与精确 Reply 是独立的预装上下文。Reply 能否取得不决定历史工具是否存在；Reply
   取不到、历史工具又不存在时，Agent 只使用其余运行、日志、源码、设计和部署证据，证据不足则收敛为
   `unknown`。
6. Uninfo 仍可在一个真实历史 Provider 已经存在时补充参与者身份或角色，但不负责读取消息，也不能让原本
   不支持历史的平台获得 conversation 工具。没有历史 reader 时，这条链不会调用 Uninfo。
7. 不增加平台统一历史抽象的假实现，不要求其他 Adapter 为了表面一致而返回空数据。将来只有在某个平台
   Provider 的真实行为、分页和完整性边界经过验证后，才为该平台注册工具。

## 为什么这样选

- 工具是否存在直接表达平台能力，避免模型把空结果误读成“没有聊天”；
- 不再为所有群维护常驻消息窗口，移除与 Bug 调查无关的内存和事件采集成本；
- 当前请求和精确 Reply 保留高相关上下文，历史只是可选增强，不是得到三值结论的伪前提；
- `unknown` 明确表示证据不足，比不同平台用不等价的本地缓存伪造一致行为更可靠；
- 新平台只需实现自己的 Provider，不需要修改领域模型或 Agent Prompt。

## 没有采用的方案

### 所有 Adapter 都维护本地最近 30 条

没有采用。它只能覆盖进程启动后被当前 Bot 观察到的消息，无法证明窗口完整，还会扩大内存驻留和数据处理
范围。

### 总是提供工具并返回 unsupported

没有采用。Agent 没有办法从工具名字预先区分“值得调用”和“平台根本不支持”，会浪费一次独立聊天额度，
也可能把不可用状态错误归纳为内容结论。

### 因缺少历史而阻止 Bug 判断

没有采用。运行 correlation、日志、源码、适用合同和精确 Reply 仍可能足以判断；只有证据闭合失败时才返回
`unknown`。

## 当前实现状态

- `BugAssessmentRuntimeRequest.conversation_reader` 为 `None` 时，Toolbox 不注册 conversation loader；
- Pydantic AI 的动态工具准备会从本轮 Agent 工具列表移除 `read_conversation_context`；
- OneBot V11 群聊继续绑定 NapCat 历史 reader，并可选择经 Uninfo 补充身份；
- Triage runtime 不再注册跨平台消息观察器，也不保存每会话最近 30 条；
- 当前 Bug Agent 资格集合仍为空，本决定本身不把未通过 Gate 的模型组合提升为在线资格。

## 替代关系

- 部分替代 [ADR-0064](0064-refine-bug-conversation-evidence-and-verdict-contract.md) 第 4、5 项以及对应的
  本地缓冲实现状态；保留 30 条上限、精确 Reply、Uninfo 可选身份补充、独立聊天额度和三值收敛；
- 进一步收窄 [ADR-0061](0061-read-latest-bounded-conversation-window-for-bug-assessment.md) 第 4、6 项：
  不支持历史的平台不再返回 conversation capability state，而是根本不暴露工具；
- 不改变 [ADR-0060](0060-use-scope-thread-and-post-route-conversation-context.md) 的 scope Thread 与路由后
  Reply 投影；
- 不改变 [ADR-0038](0038-limit-semantic-assessment-remote-data-projection.md)：semantic classifier 仍只读取
  当前显式 `triage` 文字。

## 相关文档

- [架构概览](../architecture/overview.md)
- [triage 自然语言支持入口](../architecture/flows/support-intake-routing.md)
- [跨平台 triage 支持入口](../architecture/flows/cross-platform-report-intake.md)
- [模型 Provider 支持矩阵](../architecture/model-provider-support.md)
