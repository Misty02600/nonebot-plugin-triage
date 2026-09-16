# ADR-0128：用原生消息快照保存维护者自由对话

| 状态 | 日期 |
|---|---|
| 已采纳；已实施 | 2026-09-15 |

## 背景

维护者希望像普通 Agent 一样围绕项目持续对话，让 Agent 使用已有只读工具查看代码、配置投影和日志证据，
并在工作过程中反馈进展。整个插件部署始终只有一个项目对话，SUPERUSER 从私聊、群聊或其他支持的入口进入
都继续该对话；会话不限总轮数，重启后可以继续。“开始新对话”删除旧会话，不提供历史会话列表、归档或
分支。用户已明确不需要本地会话加密，也不按 SUPERUSER 或聊天入口拆分上下文。

ADR-0101 的加密、最小化 Behavior Workspace 与 LangGraph checkpoint 服务于更严格的行为调查合同。
此次目标已经重开：以消息和摘要维持普通会话连续性，工具和模型外权限边界继续复用。

## 技术选型

采用 **Pydantic AI Core + Harness SummarizingCompaction + LocalStore 单文件当前会话快照**。

| 职责 | 所有者 |
|---|---|
| Agent 循环、模型与 Provider、工具协议、消息类型、序列化、用量 | Pydantic AI 原生能力 |
| 历史压缩、摘要生成与工具调用/结果配对 | Harness `SummarizingCompaction` |
| 当前会话的加载、原子替换与整体重置 | 项目的薄文件存储，路径由 `nonebot-plugin-localstore` 提供 |
| 入口鉴权、全局运行准入/取消、当前场景注入、原路回复 | NoneBot 适配层 |
| 代码与日志读取、事实新鲜度、写操作权限 | 已有领域工具与模型外策略 |

当前依赖为 Core 2.43.0、Harness 0.31.0，`nonebot-plugin-localstore` 也已引入。
新自由对话链路不启用 LangGraph 或 Harness StepPersistence；Pi 作为会话设计参考，不引入 Node/RPC。
现有 Python 工具、Provider 适配与资格验证可以直接复用；不建立第二套消息、摘要或工具执行框架。

## 已确认的产品边界

1. **全局共享范围**：整个插件部署只有一个当前对话。所有 Bot、Adapter、聊天入口和已鉴权 SUPERUSER
   共享上下文；任何 SUPERUSER 执行新对话都会为所有人重置。
2. **每轮场景**：每个 Run 都从当前可信 Event/Bot 注入本轮所在的 Adapter、Bot 和群聊/私聊/频道等场景，
   用于理解当前问题和确定回复位置。场景不是持久会话分区键，不按 SUPERUSER 身份拆分会话。
3. **触发方式**：每轮继续要求显式 `triage <内容>`，不监听群聊或私聊中的其他普通消息。自由对话指内容和
   工具循环不受固定意图模板限制，不表示 Bot 接管 SUPERUSER 的全部聊天。
4. **运行中输入**：已有 Run 未结束时，新的普通 `triage` 请求既不排队也不注入当前工作，立即返回“当前
   会话正在处理中”。显式“停止”和“开始新对话”仍是控制操作，分别取消当前 Run，或取消后全局重置。
5. **原生内容持久化**：快照原样保存下一轮需要的 Pydantic AI 消息上下文，包括用户/assistant 正文、工具
   参数与结果，以及 Provider 续接可能需要的 thinking/signature 元数据。会话存储层不增加脱敏、字段裁剪
   或本地加密，也不把这些内容额外复制到日志或进度消息。

## 会话和状态

1. 整个插件部署只有一个当前会话。每条请求仍独立检查 SUPERUSER；进入位置只决定本次进度和最终回答发往
   哪里，不决定消息历史。SUPERUSER 换私聊、群聊或 Adapter 后继续同一个项目上下文。
2. 首版只有一个 LocalStore data 文件 `maintainer-conversation.json`，顶层保存 `schema_version`、
   `session_id`、`updated_at` 和 `messages`。`session_id` 使用新会话 UUID，兼作旧任务写入隔离标识。
   模型与工具配置每次从当前运行时加载。
3. `messages` 保存 Pydantic AI 原生 `ModelMessage` 序列：用户消息、回答、已完成工具调用/结果、摘要与
   保留的近期消息。使用 `ModelMessagesTypeAdapter` 原样序列化和验证，不复制原生消息字段定义，也不建立
   会话专用内容投影。
4. 每次保存替换整个文件中的当前上下文快照。压缩后的旧原文从会话数据中移除；首版不另存完整 transcript
   或事件树。
   `result.new_messages()` 不是压缩结果的增量协议，不采用“旧历史加 new_messages”的存储方式。
5. Agent 会话不复制整个业务数据库或旧 Behavior Claim/Artifact 聚合。需要最新项目事实时重新使用工具读取，
   消息中的旧结论不会成为当前权限或执行授权。

## 保存和恢复边界

- 使用一个薄 Pydantic AI `AbstractCapability` 接入存储。在压缩及其他上下文改写能力之后执行
  `before_model_request`，从 `request_context.messages` 保存快照；最终在 `after_run` 使用
  `result.all_messages()` 保存完成状态。每次写入都等待原子替换完成。
- 前一个工具批次完成后、下一次模型调用前，工具调用和结果已一起进入快照。初次模型调用前同样保存用户
  输入。当前模型请求、流式片段及尚未完成的工具批次不作为独立恢复点。
- 重启后读取并验证原生消息，传入新 Run 的 `message_history`。默认等待用户继续，不自动复活后台任务。
  故障发生在两个保存点之间时，回到最近成功保存点；期间只读工作可能重做。
- 不承诺恢复 Python 调用栈、正在等待的网络流、任意 capability 内部状态或写工具 exactly-once。首版工具
  仍以只读为边界。恢复授权每次按当前配置检查，不从历史中恢复权限。
- 文件写入先在同目录创建临时文件，写完并刷新后使用 `os.replace()` 替换正式文件。读取只接受完整、版本匹配
  且能通过原生消息校验的正式文件；崩溃遗留的临时文件不参与恢复。
- 序列化不兼容或替换失败时报告会话存储错误，保留上一份成功快照；不静默清空历史，也不宣称保存成功。

## 新对话、并发和删除

1. 首版支持单插件进程。所有入口共用一个运行锁，一次只执行一个 Run；运行期间到达的普通请求直接返回
   `BUSY`，不进入内存队列。
2. “开始新对话”作为一个全局操作：停止旧 Run、清掉属于旧 session 的待处理输入、等待正在进行的文件写入
   结束，再以带新 `session_id` 的空快照原子替换正式文件。旧内容不归档；失败时不报告重置成功。
3. 每个 Run 捕获启动时的 `session_id`。保存、最终回复和进度发送都在同一会话协调器下再次核对当前 ID；
   reset 完成后，旧 Run 不能替换新文件或继续输出。
4. 不向外提供独立的“软重置”和“删除历史”两套产品操作。已发到聊天平台的消息由平台留存，本地新对话
   不撤回它们。

## 自然对话和长任务

- 沿用显式 triage 入口与原路回复；显式管理子命令优先处理。已鉴权 SUPERUSER 的项目自然对话由普通 Agent
  承接，输出自然语言，不强制每次生成调查工单或结构化解释产物。普通用户的支持路由保持现有合同。
- 单个 Run 的模型请求上限采用用户指定的 15；对话总轮数不限。达到运行预算时结束当次工作并说明，已有
  上下文可以继续使用，不要求重开会话。
- Harness 按上下文比例触发压缩，初始配置采用 `max_fraction=0.8, keep_messages=20`。上下文窗口必须与
  实际部署模型匹配，自定义/代理模型使用已核对的显式窗口；不依靠对话轮数限制历史。
- 模型请求与工具分别使用超时；沿用当前可配置 Provider 请求超时，去掉用同一短超时包住整个 Run 的做法。
  多次模型请求和工具调用可以持续数分钟，支持用户取消。
- 复用原生消息和工具事件提供节流的进度反馈及最终回答，不转发模型隐藏推理。回复目标固定为触发本次 Run
  的聊天入口；从另一入口发起后续请求不改变此前消息的发送位置。Agent 不持有任意跨会话发送能力。

## 数据边界

会话正文是维持 SUPERUSER 对话的本地业务数据。持久化层保存 Agent 实际使用的原生消息，不增加会话级
AES、独立密钥管理、脱敏或字段过滤。会话文件与日志/trace 是不同用途；已有日志和 trace 继续遵循各自的
记录合同，不因为会话保存正文就自动打开完整模型 I/O 遥测。

## 为什么这样选

Pi 的参考价值是将会话保存、上下文重建与 Agent 循环清楚组合。其完整事件树解决历史导航和分支；当前项目
始终只有一个正在使用的上下文，没有条件查询、关联、多记录事务或共享写者需求。一个原子替换的 JSON 快照
比 ORM 表更贴合该聚合，也无需处理 JSONL 半行、另一个 Node 进程，或操作第三方 StepStore 的内部表。

自定义层仅补充库没有覆盖的项目会话生命周期：定位文件、覆盖快照、删除重建及旧任务隔离。
压缩、原生消息和模型循环始终由依赖库负责。

## 替代关系与实施边界

替代 ADR-0101 在 SUPERUSER 自由对话中的 Workspace、加密、actor/scope 隔离和 checkpoint 控制要求。
对 ADR-0089 的正文持久化禁区增加“维护者会话业务存储”的明确例外，保留其日志与遥测边界。
本决定已经实施。运行时使用 `maintainer-conversation.json` 和 Pydantic AI 原生消息历史；旧
LangGraph checkpoint 不再属于安装依赖或运行路径。

旧 Behavior checkpoint 不自动转换成缺失的原生消息历史，也不由新运行时读取。升级后第一次维护者对话会
创建新的原生消息文件；旧 SQLite 文件若仍留在 LocalStore 中只是未引用的旧数据，部署者可按自己的保留策略
处理。

## 核对与验收依据

- 已核对当前依赖源码，确认 `before_model_request`、`after_run`、原生消息序列化和独立压缩能力。
- 合成 FunctionModel 探针确认：上述 hook 能保存含配对工具结果的上下文，压缩后的摘要在下一次模型请求前
  进入快照，最终回答也可保存；无需 StepPersistence。先前探针确认压缩后的 JSON 可还原并用于下一轮。
- `Agent.iter()` 的节点切换处不一概是完整快照边界：工具结果可能仍在下一节点的 request 中，未并入
  `all_messages()`。实现应使用本决定确定的 hook，不直接把每个迭代节点当保存点。
- 自动测试覆盖原生消息与 Provider 元数据无投影往返、进程内重建后的继续、跨入口共享、每轮场景覆盖、
  全局忙碌拒绝、取消后全局重置、鉴权前不进入 Agent、无效文件显式报错后可重置，以及真实 Pydantic AI
  Run 的请求前/完成后快照。真实 Provider 下的长会话压缩与 Bot 进程重启 smoke 仍是部署验收项。

## 官方参考

- [Pydantic AI 消息历史](https://pydantic.dev/docs/ai/core-concepts/message-history/)
- [Harness v0.31.0 Compaction](https://github.com/pydantic/pydantic-ai-harness/blob/v0.31.0/docs/compaction.md)
- [Pi 会话格式和上下文重建](https://github.com/earendil-works/pi/blob/8a7b0c03dfb702663acafb6dc29f8acaa4ffe391/packages/coding-agent/docs/session-format.md)
- [Pi SDK](https://github.com/earendil-works/pi/blob/8a7b0c03dfb702663acafb6dc29f8acaa4ffe391/packages/coding-agent/docs/sdk.md)
