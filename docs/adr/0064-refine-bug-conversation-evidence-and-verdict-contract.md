# ADR-0064：收窄 Bug 会话证据与结论合同

- 状态：已采纳；本地跨平台会话缓冲由 ADR-0065 替代，教学合同边界由 ADR-0066 部分替代
- 决策日期：2026-08-15

## 背景

ADR-0061 建立了最新会话窗口、精确 Reply、独立聊天额度和预期/实际证据闭合，但首版仍有四处边界过宽或
过严：最新 60 条超过当前试运行所需；“六次共享额度”容易被误读为框架标准；唯一用户补充没有明确是
分类与 Bug 共用；预期合同被限制为 README / 设计等外部文字，无法表达代码内部可以独立验证的明显矛盾。

Prompt v7 的真实 forward-heldout 还暴露了 occurrence 定义错误：四个案例都已确认本案发生了一次，模型却因
无法证明“历史上恰好只发生一次”而输出 `unknown`。这个条件超出了 `single_observed` 想表达的范围。

## 决定

### 会话证据

1. 支持最新历史的平台每案最多读取一次、最多 30 条。单条与总 Evidence 字符预算仍可进一步裁剪并标记
   `partial`。
2. 当前显式 Reply 是独立预装证据，不属于最新 30 条窗口。只要 Adapter / UniSeg 已取得 Reply 正文，即使
   被回复消息早于窗口也继续提供；取不到时明确标记不可用，不用任意消息 ID 扩大读取。
3. conversation Provider 继续投影判断群聊关系所需的消息 ID、Reply ID、时间、sender ID / 可见名称、角色、
   Bot / 当前报障者标记、消息段和可见正文。ID 只能表达同一会话内关系，不能扩大 scope 或授权。
4. 领域模型保持跨平台。Triage 在 Adapter 事件进入时把当前 Bot 实际观察到的消息写入有界内存窗口，因而
   没有平台历史 API 的 Adapter 也能提供启动后观察到的最近消息；平台原生历史 Provider 只负责补齐进程启动
   前或本地窗口以外的内容。窗口不持久化，未观察满 30 条时必须标记 `partial`，不能声称已经取得完整历史。
5. Uninfo 只在 Bug 调查真正启动时作为可选身份补充层：用上游公共 `get_session()` 取得当前报障者身份，
   再对缺少平台原生角色的唯一历史 sender 调用 `Interface.get_member()`。消息发生时的平台角色继续保存在
   `sender_roles`，调查时查询到的当前角色单独保存在 `sender_current_roles`，二者不能互相覆盖。OneBot 历史
   已返回 `sender.role` 时不重复查询。Triage 不新增 Uninfo 基础依赖，也不使用 fork-only Ref / resolve API；
   未安装、Adapter 不支持或查询失败时保留空值并继续调查。

### Agent 预算

6. Pydantic AI 的 request、tool、token、cost 上限是整次 Agent run 的通用安全机制；它不规定不同证据源必须
   如何分配次数。本项目保留“一次 conversation + 六次通用证据”的当前策略，并继续给 runtime、log、source、
   design、deployment 设置各自上限。这是经过评测的项目策略，不宣称为行业固定数字。
7. 六次通用额度共享可以让 Agent 按案件选择证据，避免为每个工具预留后长期空置；若 trajectory 指标显示
   某类工具稳定饿死其他关键证据，再用阶段预算或模型外预取调整。当前 Gate 的 budget 指标已经通过，不能
   把 occurrence 失败归因于额度不足。

### Thread 与用户补充

8. 每个 support Thread 总共只有一次用户补充机会，不为“意图前澄清”和“Bug 调查”各发一张票。机会由最早
   真正缺少用户可提供信息的阶段消费：首轮尚未形成可路由 action 时可澄清；首轮已经是 Bug action 时可追问
   操作、对象或现象；若分类阶段已经用掉，Bug 阶段不能再追加第三个用户回合。
9. Bug Agent 本身不悬挂等待用户，也不把“向用户提问”暴露为工具。它只返回结构化 `missing_evidence`；协调器
   判断证据是否能由用户提供并决定是否使用 Thread 的唯一补充机会。系统日志、源码、设计和部署缺口不能
   转嫁给用户。

### 预期证据与明显代码缺陷

10. README、帮助规格、ADR、上游合同、运行时命令结构和代码都属于证据，不因文件名自动可信。每项预期主张
   必须校验来源、适用版本 / revision、是否与当前部署匹配、是否明确，以及是否独立于正在被怀疑的实现分支。
11. 没有完善 README 时，仍允许根据代码判 Bug，但只限可以闭合验证的实现矛盾，例如：调用方与被调用方的
    明确合同不一致、框架 / 类型 / API 不变量被违反、同一输入必然进入不可达或无条件异常分支、定义在一处的
    本地不变量被另一处当前实现直接违反。源码在这里既提供不变量证据，也提供违反证据，两者必须使用可区分
    的 Evidence 引用和同一适用 revision。
12. 仅能从实现本身读出一个产品选择时仍不能判 Bug，例如“限流应该是 5 秒还是 10 秒”“功能本来是否只给
    管理员”。这种问题没有独立意图或不变量，必须得到人工规格、适用设计或上游合同，否则保持 `unknown`。
13. LLM 教学注释可以帮助定位能力和解释用户用法，但不能单独成为预期真值。人工确认的帮助规格可以成为
    合同；自动注释必须另有运行时结构、人工规格或独立不变量支持。

### occurrence 与可观测性

14. `repeated` 只在证据明确给出至少两个独立 occurrence 或重复计数时使用。`single_observed` 表示本案至少有
    一个当前、具体 occurrence，且没有重复证据；它不声称整个历史中只发生过一次。只有连本案 occurrence 都
    无法建立，或相关证据 stale / partial 时才使用 `unknown`。
15. Prompt / Schema / qualification revision 变化后，不能用已经据此调试过的冻结 held-out 冒充新资格。先用
    development 案例和本地完整 trace 验证，再策展一组未用于调 Prompt 的 forward-heldout 执行正式 Gate。
16. 维护者本地评测继续使用 Pydantic AI `capture_run_messages` 保存失败与中断 run 的完整模型/工具交换；长期
    运行观测优先使用 OpenTelemetry 的 run、model request、tool span，并默认不把聊天、源码和日志正文写入
    telemetry。版本化报告只保留失败分类、用量、工具计数、trace ID 与有界摘要。

## 当前实现状态

- OneBot 最新窗口默认值已改为 30，并有“精确 Reply 不在窗口内仍可用”的回归测试；
- 跨平台最近消息缓冲已在入口注册；OneBot 优先使用原生历史，本地窗口作为不可用时的回退，其他 Adapter
  至少可以提供本进程已观察到的 `partial` 窗口；
- Uninfo 以可选运行时导入接入，只在 Bug 调查开始后补充当前报障者与缺少原生角色的唯一 sender；
- 一次 conversation + 六次通用证据、单 Thread 一次补充和本地 trace 已存在；
- reconciler 已在候选引用当前完整 runtime / log Evidence 时，把 `unknown` occurrence 模型外归一化为
  `single_observed`；Prompt v7 仍保留旧措辞，历史失败 Gate 不回溯改写；
- “外部预期 + 实际”仍是当前 reconciler 合同，`QUALIFIED_BUG_TASKS` 保持为空；在加入可区分的不变量 / 违反
  Evidence 角色并完成新资格前，不宣称代码内明显缺陷路径已可在线判定。

## 影响

- 当前试运行聊天取证更小，精确 Reply 不会因为缩小窗口而丢失；
- Uninfo 负责统一身份与当前角色，不负责读取消息；本地观察窗口与平台历史 Provider 共同负责聊天内容；
- Bug 用户交互仍为首轮加至多一次补充，生命周期保持 idle 15 分钟、absolute 30 分钟；
- 下一版 Bug Candidate / reconciler 需要区分 expectation 与 actuality Evidence 角色，避免简单地把任意源码
  同时当“应该怎样”和“现在怎样”；
- occurrence 修复必须进入新的 Prompt / 资格 revision，旧失败 Gate 不会被回溯改写。

## 替代关系

- 部分替代 [ADR-0061](0061-read-latest-bounded-conversation-window-for-bug-assessment.md) 第 2、6、8—14 项；
  保留模型外 scope、最新窗口、聊天独立额度、正文不遮蔽和三值失败关闭；
- 补充 [ADR-0060](0060-use-scope-thread-and-post-route-conversation-context.md) 的唯一补充预算所有权；
- 不改变 [ADR-0038](0038-limit-semantic-assessment-remote-data-projection.md)：semantic classifier 仍只读取当前
  显式 `triage` 文本，Reply / 历史只进入路由后的任务。

## 参考

- [Pydantic AI：Usage Limits](https://ai.pydantic.dev/agent/#usage-limits)
- [Pydantic AI：测试与消息捕获](https://pydantic.dev/docs/ai/guides/testing/)
- [Pydantic AI：Logfire / OpenTelemetry 可观测性](https://ai.pydantic.dev/logfire/)
