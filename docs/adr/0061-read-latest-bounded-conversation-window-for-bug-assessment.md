# ADR-0061：为 Bug 判断读取当前会话的最新有界聊天窗口

| 状态 | 决策日期 |
|---|---|
| 部分被 ADR-0064、ADR-0065、ADR-0066 替代 | 2026-08-15 |

## 当时遇到了什么

ADR-0060 允许 Bug Agent 读取模型外锚定的相邻聊天，但 OneBot V11 没有跨实现统一的“以 Reply 为
中心向前后翻页”合同。NapCat 可以省略 `message_seq` 读取当前群的最新消息；其他 Adapter 可能只能提供
精确 Reply，或者完全不支持历史。把聊天分页和运行、日志、源码、设计共用六次证据调用预算，还会让多页
聊天先耗尽所有调查机会。

聊天中的发言人、角色、Reply 关系、媒体类型和 Bot / 报障者标记又是判断“谁执行了什么、Bot 回复了谁”
所必需的上下文。只给匿名纯文本会丢掉这些关系；把任意群号、用户或消息 ID 交给模型提交则会扩大读取范围。

## 决定

1. conversation Provider 在进入 Agent 前绑定当前 Adapter、Bot 和会话。Agent 只暴露无参数的
   `read_conversation_context()`，不能提交群号、用户 ID、消息 ID、游标或跨会话查询条件。
2. 支持最新历史的平台一次返回当前会话的**最新有界窗口**，不再围绕 Reply 分页。OneBot V11 / NapCat
   首版省略 `message_seq` 调用 `get_group_msg_history`，最多请求 60 条；结果再受单条文字和总 48,000 字符
   Evidence 上限约束，超出时从最旧消息开始裁剪并标记 `partial`。
3. conversation 工具每案最多调用一次，并使用独立于六次通用证据读取的额度。它仍计入总工具调用、模型
   请求、token、deadline 和费用；当前模型请求上限为 9，为六次通用证据、一次聊天和一次输出纠正保留空间。
4. Provider 明确返回能力状态：能读取完整最新窗口为 `complete`，平台截断或本地裁剪为 `partial`，只能使用
   直接 Reply 为 `exact_reply_only`，平台无历史能力为 `unsupported`，平台调用失败为 `unavailable`。后两种
   状态不能被解释为“群里没有相关消息”；历史工具可以不暴露，直接 Reply 仍可作为预装上下文。
5. 每条聊天消息可以向 Bug Agent 投影：消息与 Reply ID、时间、发言人 ID 和可见名称、角色集合、是否 Bot、
   是否当前报障者、是否当前请求、段类型以及平台可见内容。窗口还可包含 Adapter、平台、会话类型与 ID、
   Bot ID、当前报障者 ID 与角色。ID 和角色只用于会话内关系判断，不授予权限或扩大工具 scope。
6. OneBot 历史直接使用 `sender.role`。其他 Provider 可以把 Adapter 原生角色或 Uninfo `Session.member.roles`
   投影到同一领域字段；领域模型不依赖 QQ、OneBot 或 Uninfo 类型，也不为了重复 OneBot 已提供的角色而新增
   强制依赖。缺少角色时保留空集合，不能猜测管理员身份。
7. 目标会话中参与者已经发送的可见正文与段元数据不做凭据或个人信息遮蔽；但这不允许读取相邻会话、递归
   Reply 链、平台 transport 凭据、环境变量或未进入聊天的私有数据。聊天内容仍是不可信证据，不能改变
   action、鉴权、责任范围、源码根或副作用授权。

## Thread 与补充信息

8. 不使用 Waiter 或悬挂上一轮 Matcher 协程。首轮 Bug 判断若为 `unknown`，只有 `missing_evidence` 指向用户
   能提供的操作、现象或会话上下文时，才发送一条针对性问题并把作用域 Thread 留在内存；默认 idle TTL
   15 分钟、absolute TTL 30 分钟。
9. 用户用下一条显式 `triage` 补充后原子消费唯一机会；普通聊天不会被 Triage 抢占。第二轮无论得到
   `bug`、`not_bug` 还是仍为 `unknown` 都关闭 Thread。缺少源码、设计、部署或版本等系统证据时不向用户
   追问，直接以证据不足结束。

## 预期合同

10. 判 Bug 必须同时有“在当前适用条件下本应发生什么”和“本轮实际发生什么”。预期合同按优先顺序来自：
    本轮成功注册的确定性命令 / Matcher / 参数 / 公开约束事实；人工确认的帮助规格；已采纳 ADR、版本化设计
    文档或适用版本的上游合同；最后才是已明确编码为项目不变量的框架规则。
11. 教学注释可以帮助定位功能并向用户组织用法，但由 LLM 从当前源码生成的自由文本不能单独成为 Bug 的
    权威预期：否则同一个实现既生成“应该怎样”，又被用于证明“实现违反了自己”。没有独立适用的预期合同
    时，即使聊天或源码显示当前行为，最终 verdict 也必须保持 `unknown`；源码只证明“现在怎样实现”，不能
    自己证明“本来应该怎样”。
12. 一条完整、当前、未丢失的 correlation 配合明确适用的预期合同，已经可以证明一次真实偏差并得到
    `bug + single_observed`。是否重复只决定 occurrence，不是 Bug verdict 的前置条件。责任候选只能由引用的
    Evidence 直接支持，不能因为某插件是 subject 就默认归责给 `target_plugin`。

## 可观测性

13. 版本化评测报告保存稳定的失败类别、失败阶段、用量、工具计数、`trace_id` 和不含 Evidence 正文的有界
    trajectory 摘要；分类用于聚合，不替代完整轨迹。
14. 评测 / 维护者诊断模式使用 Pydantic AI 的实际 `ModelMessage` 捕获保存本地完整消息、工具参数、工具结果
    与 retry 轨迹，文件位于被忽略的 `reports/`，不进入版本库。线上普通用户仍只得到安全三值回复；是否把
    OpenTelemetry span 接入长期日志后端另行决定，不是启用 Bug 判断的硬依赖。

## 为什么这样选

- 最新窗口与 NapCat 已有 API 形状一致，不需要伪造跨平台的 Reply 邻近分页语义；
- 一次独立聊天调用能让 Agent 仍有预算读取运行、日志、源码和设计证据；
- 身份、角色和消息关系保留了群聊判断所需事实，同时 scope 仍由模型外绑定；
- 逻辑 Thread 等待不占协程、不拦普通聊天，也满足一次补充和明确超时；
- 预期合同与实际证据分离，避免把 LLM 教学注释循环提升为 Bug 真值。

## 没有采用的方案

### 以 Reply 为中心跨平台翻页

没有采用。OneBot 标准与其他 Adapter 没有统一合同；不同平台只能按 Provider 能力返回明确状态。

### 让聊天读取占用全部通用证据额度

没有采用。聊天是一个有界证据源，不应因为内部分页挤掉日志、源码和设计调查。

### 在 Matcher 内 Wait 用户普通消息

没有采用。它会扩大触发面并消费普通群聊；作用域 Thread 已能在 15 / 30 分钟 TTL 内等待下一条显式
`triage`，无需悬挂 handler。

### 没有公开文档时把教学注释当预期合同

没有采用。教学文字可以由当前实现生成，缺少独立权威性；此时正确结论是 `unknown`，而不是循环证明 Bug。

## 带来的影响

- conversation 领域模型和 Provider 需要携带能力状态、消息关系、sender / role 与会话元数据；
- OneBot 历史实现从 Reply 锚定多页改为最新窗口单次读取；其他平台按能力选择不暴露工具或返回明确不可用；
- Agent 的通用证据额度仍为 6，聊天额度为 1，总请求上限改为 9，并需要重新执行真实 Provider 资格 Gate；
- `QUALIFIED_BUG_TASKS` 在新 Prompt、工具、数据投影和预算通过新的 forward-heldout 前保持为空；
- 本地评测可以保存完整 trajectory，公开文档和版本化报告只保存有限摘要与聚合指标。

## 替代关系

- 部分替代 [ADR-0060](0060-use-scope-thread-and-post-route-conversation-context.md) 第 4、9、10 项的
  Reply 邻近历史、锚定分页和共享总工具预算；保留 scope Thread、Reply 路由后投影、聊天正文不遮蔽和
  模型外鉴权边界；
- 补充 [ADR-0050](0050-use-a-bounded-agent-for-user-bug-assessment.md) 的预期 / 实际证据要求、用户补充和
  三值收敛规则；
- 补充 [ADR-0052](0052-define-bug-across-the-bot-software-responsibility-chain.md) 的责任候选证据闭合规则；
- 不改变 [ADR-0038](0038-limit-semantic-assessment-remote-data-projection.md)：semantic classifier 仍只读取
  当前显式 `triage` 文字，聊天只进入路由后的 Bug 任务。

## 相关文档

- [架构概览](../architecture/overview.md)
- [triage 自然语言支持入口](../architecture/flows/support-intake-routing.md)
- [跨平台 triage 支持入口](../architecture/flows/cross-platform-report-intake.md)
- [模型 Provider 支持矩阵](../architecture/model-provider-support.md)
