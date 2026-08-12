# 流程：triage 自然语言支持入口

## 当前入口与已采纳目标

普通用户每轮都发送 `triage <求助内容>`，也可以写成 `@Bot triage <求助内容>`。Reply 是可选的结构化
上下文，不是独立触发器：只有 Reply、没有 `triage` 的消息不会进入本插件。显式请求精确回复 Triage 最近
一次仍有效的回答时，插件尝试续接同一 Thread；未知、过期、旧回答或跨作用域 Reply 则按新请求处理。
当前实现已经允许私聊、群聊和频道请求进入入口，再由各意图分支执行自己的合同。

```text
triage + request text + optional Reply
                 ↓
场景、长度、入口限流和最小上下文边界
                 ↓
目标请求理解（需求信号可多选）+ 模型外受众 / 披露域
   ├─ 询问公开能力 / 用法 → public 事实 → capability_guidance；不建 incident
   ├─ 报告现象 / 询问原因 → reported_failure_unverified
   │                         → public 用法、权限、场景与安全回执初检
   │                            ├─ 可解释 → capability_guidance / usage_error
   │                            ├─ 证据不足 → 只追问一个关键问题
   │                            └─ 正确且允许的调用仍失败 → suspected_incident
   ├─ 已鉴权开发者明确请求部署内部解释
   │                         → behavior_exploration → 原会话解释；不建 incident
   ├─ 明确请求受理故障     → suspected_incident → 可选 Reply 关联 → LiveIncident + 窄回执
   ├─ out_of_scope          → 说明范围；不建 incident
   └─ unsafe               → 拒绝；不调用工具
```

每轮正常回答后 handler 都结束，不使用 Waiter 悬挂协程。教学或澄清回答会建立不含正文的短期内存 Thread，
首次回答发送期间用短期 pending reservation 防止该 Thread 被 idle TTL 或容量淘汰，但不延长 absolute TTL；
适配器出站 Provider 将回答
message ID 通过 HMAC 索引绑定到 adapter、Bot、场景和 actor。下一条显式
`triage` 由同一个 Alconna Matcher 接收；每轮先经过入口限流，随后由 UniSeg 提供结构化 Reply / Target，
Thread 协调器再原子 Claim 仍可续接的上下文。Claim 会立即消费旧 Reply，并保证每个 Thread 同时只有一个
active turn；并发续问返回“上一轮仍在处理”，不进入业务分支。默认 idle 15 分钟、
absolute 30 分钟且
不跨重启。当前只有 OneBot V11 群发送实现了 Bot 出站 Thread 引用 Provider，其他 adapter 未命中时按新请求
处理，而不是监听其普通 Reply。

Claim 内部异常与 `NOT_FOUND` 分开：异常只返回“上下文暂时不可用”并失败关闭，不把已部分处理的续问再次
当作新请求。入口限流拒绝发生在 Claim 前，因此不会消费 Reply。

澄清 Thread 只接受一次明确关联的续答；续答可转入教学。教学或澄清 Thread 中，用户明确请求受理故障时，
会沿用现有报障服务建立一次 incident 并立即关闭 Thread。仍无法分类、空输入、超长输入或显式取消都会终止
本次澄清，不进入第二次追问。教学 Thread 可在 TTL 内继续相关问答；旧回答在处理轮 Claim 时立即失效，
只有新回答成功发送并取得 message ID 后才建立新的续接点。发送、绑定或处理失败不会复活旧引用。续问转报障
时不会把 Bot 的教学回答当作用户故障证据。

当前已接入 `triage` 自由文本参数、确定性首轮意图和公开能力说明。首轮实现只可靠区分
`capability_guidance`、`suspected_incident` 和“不确定”；上图中的 `usage_error`、
`behavior_exploration`、`out_of_scope` 与 `unsafe` 是统一 Agent 入口的目标分流，尚未接入当前 Matcher。
其中 `behavior_exploration` 继续消费 `triage` 后的自然语言，不要求额外子命令；它回答插件为什么在当前部署
中这样表现，并在模型外鉴权后才读取受限行为证据。模型资格表仍为空，因此尚未启用模型 Agent；不明确、
否定或假设性请求会得到一次澄清，而不是被强行记成故障。

上图的多选需求信号和 `reported_failure_unverified` 也是目标概念，不是现有 `IntakeSignals v1` 或第六种
`IntakeDisposition`。正式实现需要版本化支持入口的语义 assessment，再把经过初检的单一生命周期处置交给
权威 router；不能让入口分类器和领域服务各自根据原文重复判断。

请求需求、生命周期处置和证据披露是三条正交轴。“刚才没响应”只表示用户报告了一个尚未验证的现象，
不证明正确调用仍失败，也不自动建立 incident；“哪个配置控制”只表示请求了维护细节，不自动授权读取配置。
普通用户先在 public 证据域内获得用法、角色、场景、限流和非秘密启用前提的解释。配置字段、原始或当前值、
内部后端和源码位置不进入普通回答；若某项设置由已经公开的群内管理指令承接，回答只引用该公开指令合同、
允许披露的角色要求和用户可观察结果，而不是底层配置。hidden、SUPERUSER-only 或 restricted 管理入口仍与
不存在保持不可区分。`SUPERUSER` 身份只扩大 behavior 分支的可读证据域，不会把用户疑惑自动改成故障。

## 输入与数据边界

- 当前请求文字只在入口和意图适配层瞬时使用；`IntakeSignals`、`LiveIncident`、trial 和运行证据不保存原文；
- 文字、插件元数据和 Reply 都是不可信证据，不能直接变成命令执行、工具调用或维护动作；
- Alconna / UniSeg 负责提供第一个结构化 `Reply.id` 和 Target；入口不读取 `msg` / `origin`。Thread 索引另外
  判断该 ID 是否属于 Triage、是否绑定当前 actor / Bot / 场景，以及 Thread 是否有效和指向最近回答；
- Reply 缺失或引用过期不妨碍求助；疑似故障会明确标记为未关联运行证据，不猜测其他消息；
- 所有求助先经过不保存平台身份的轻量 HMAC 限流；疑似故障再经过独立的建单限流；
- 同一 Thread 的并发正确性由一次性 Reply Claim 和短期 turn lease 保证；它不替代入口限流，也不提供
  跨进程或跨重启协调；
- 普通用户能力说明优先采用显式公开 Provider，未命中时只检索当前 adapter 域内自动确定 `public` 的已加载
  命令；`CommandMeta.hide=True`、停用、带 blocking `analysis_issues`、`restricted` 和其他 adapter 能力不进入
  普通用户回答。SUPERUSER 在模型外鉴权通过后可检索影子的完整受众、平台范围和具体问题，但必须保留
  问题与执行资格未知提示；
  影子字段在回显前中和 mention 与 Unicode 控制字符；两条路径都不会为回答问题重新执行命令 `parse()`、
  behavior、executor、Rule、Permission 或 handler；
- 当前实现允许私聊进入本地守门和意图分流。公开教学、用法纠错与
  澄清可以原路回复，私聊行为探索仍必须先通过当前 Bot / Event 的模型外 `SUPERUSER` 鉴权；
- 行为探索鉴权通过后，可以在请求者选择的原始私聊、群聊或频道返回完整解释，不按会话成员构成增加
  allowlist、旁观者鉴权或强制转私聊。回答仍受秘密过滤、文本净化与证据分级约束，restricted 证据进入远端
  模型仍需独立部署授权；
- 允许私聊进入 `triage` 不自动开放私聊疑似故障受理；当前报障服务继续拒绝私聊。普通用户不能读取
  incident 摘要，查询、反馈和统计仍由 `SUPERUSER` 权限保护。

## 领域分流顺序

`route_intake` 继续按固定优先级处理严格信号：安全拒绝优先，其次是冲突补问、无关请求、命令错误、可信
运行失败或明确故障受理、能力请求，最后才是信息不足补问。这里的“可信运行失败”来自安全运行回执，不是
正文出现“失败 / 没响应”；“明确故障受理”表示请求者要求进入故障生命周期，也不是一般疑问句。
`runtime_status=succeeded` 不能证明用户观察到的行为正确；命令少参数或权限不足也不能直接升级成插件故障。

只有 `suspected_incident` 后续进入技术责任层。能力说明、用法纠错和行为探索不会污染 incident、trial 或
失败聚类。

## 相关决定

- [ADR-0003：统一能力导航与故障入口](../../adr/0003-unified-capability-guidance-and-incident-intake.md)
- [ADR-0020：triage 自然语言入口与可选 Reply](../../adr/0020-use-triage-command-for-natural-language-support.md)
- [ADR-0022：SUPERUSER 能力影子候选检索](../../adr/0022-limit-capability-shadow-guidance-to-superusers.md)
- [ADR-0025：用多源部署证据解释插件行为](../../adr/0025-explain-plugin-behavior-from-deployment-evidence.md)
- [ADR-0028：允许 triage 私聊并向 SUPERUSER 原会话返回行为解释](../../adr/0028-allow-private-triage-and-superuser-request-context-replies.md)
- [ADR-0030：免命令精确回复续问（已替代）](../../adr/0030-continue-support-thread-by-exact-reply.md)
- [ADR-0031：支持 Thread 续问仍要求显式 triage](../../adr/0031-require-triage-for-support-thread-continuation.md)
- [ADR-0033：用一次性 Reply Claim 串行化支持 Thread 处理轮](../../adr/0033-serialize-support-thread-turns-with-single-use-reply-claims.md)
- [ADR-0032：分离能力受众、平台范围与分析问题](../../adr/0032-separate-capability-audience-analysis-and-platform-status.md)
- [Alconna 能力与解析回执](alconna-capability-and-parse-receipts.md)
- [运行观察入口](runtime-observation-intake.md)
