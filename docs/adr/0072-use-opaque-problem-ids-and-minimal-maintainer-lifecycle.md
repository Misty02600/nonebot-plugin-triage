# ADR-0072：使用中性公开问题编号与最小维护生命周期

## 状态

| 状态 | 决策日期 |
|---|---|
| 已采纳；Bug 公开编号与最小维护生命周期已实现，merge / alias 与 unknown 编号暂缓 | 2026-08-15 |

## 背景

[ADR-0068](0068-treat-qualified-agent-bug-verdicts-as-operational-decisions.md) 已经把 Agent verdict、人工复核和
问题生命周期分开；[ADR-0070](0070-separate-bug-reports-occurrences-and-problems.md) 与
[ADR-0071](0071-group-bug-problems-with-versioned-evidence-fingerprints.md) 又定义了 Report / Occurrence /
Problem 和自动聚合。普通用户与维护者仍需要一个稳定、简短的公开引用，以便证明记录成功、查询问题和在
问题合并后继续引用旧回执。

此前示例使用连续 `B-102`。它同时暴露问题数量，并把 `B` 误读成永远不会改判的 Bug；深度 `unknown` 后来
可能被确认成 `not_bug`，人工 override 也可能改变 verdict，因此公开 ID 不应编码当前结论。维护命令此前还
提出过“继续观察”和“忽略”，但前者不改变任何状态，后者在首版问题量尚小且没有归档需求时可以暂缓。

## 决定

### 中性公开 ID

1. 每个持久 Problem / 深度 unknown 调查记录获得稳定的公开 ID，使用中性 `P-` 前缀和不连续的大写安全字符，
   例如 `P-7K2M9Q4D`。具体字符表和默认长度由实现选择，但必须：
   - 不包含 verdict、时间、组件、用户、场景或累计数量；
   - 不由用户文字、ProblemSignature 或 source revision 直接推导；
   - 在 LocalStore 写入前检查冲突并在冲突时重新生成；
   - 对聊天显示和手工输入清晰，避免容易混淆的字符。
2. 内部主键、ProblemSignature 与公开 ID 分离。Problem merge 后，旧公开 ID 作为 alias 永久解析到 canonical
   Problem；不能因为合并让用户已经收到的编号失效。Problem split 时保留原 ID 指向原 Problem，被拆出的新
   Problem 使用新 ID，具体 occurrence 迁移另行留下审计记录。
3. 普通用户可以看到公开 ID，但 `报错查询` 和所有维护动作继续要求 SUPERUSER。公开 ID 本身不授予查询、
   源码、日志、配置或责任信息访问权。
4. Triage 自己发送的回执在本地绑定 public problem ID、canonical internal ID 和当前 revision。用户 Reply 回执
   时可以精确恢复问题引用，但 Reply 不恢复已关闭 Thread，也不绕过当前权限和 ServingView。

### 固定事务回执

5. 以下终局回复使用模型外固定模板，不交给 Answer LLM 自由组织：
   - 新 Bug：`确认这是一个 Bug，已记录（编号 {problem_id}），请等待主人解决。`
   - 已有 Bug：`确认这是已记录的问题（编号 {problem_id}），本次发生已经关联，请等待主人解决。`
   - 深度 unknown：`暂时无法判断是不是 Bug，已记录（编号 {problem_id}），请等待主人确认。`
6. 只有 Report、Occurrence 和 Problem / 深度 unknown 已成功原子写入后才能发送“已记录”或“已关联”。写入
   失败时固定回复：`已经完成判断，但问题记录暂时失败，请等待主人处理。`，同时写维护者可见的结构化错误
   日志 / 统计；普通用户不承担联系和转述责任。
7. `not_bug / teach_correction` 不使用上述事务模板，继续通过公开 Guidance 解释正确指令和公开条件。任何
   普通用户回复都不得包含 ProblemSignature、内部主键、源码、日志、配置键值、Evidence ID 或责任候选。

### 首版维护命令

8. 首版只增加下列维护动作，沿用固定命令头 `报错查询` 和每轮 SUPERUSER 鉴权：
   - `报错查询 <编号>`：查看问题、发生次数、适用版本与复核 / 生命周期状态；
   - `报错查询 <编号> 确认Bug`：复核 Agent Bug，或把深度 unknown 人工裁决为 Bug；
   - `报错查询 <编号> 确认非Bug`：人工 override Agent Bug，或把深度 unknown 裁决为非 Bug；
   - `报错查询 <编号> 解决`：把已经是 Bug 的 Problem 标记为已解决。
9. Agent Bug 在“确认Bug”前已经是正式 `bug`；该动作只把 `review_status` 改成已复核。`确认非Bug` 改变
   verdict，并保留原 Agent 决定和人工 override。深度 unknown 在人工裁决前不能执行“解决”。
10. 已解决 Problem 再次出现匹配的技术签名时，新增 Occurrence 并把生命周期标成回归；不能静默保持已解决。
11. 首版不增加“继续观察”：开放 Problem 已经持续记录 occurrence，执行该命令不会改变行为。首版也暂不增加
    “忽略”：不需要处理的记录会继续留在默认待处理集合；真实噪声证明需要归档后再增加有明确恢复规则的
    archive / ignore 生命周期。

## 理由

- 普通用户拿到短 ID 后可以把具体记录转给主人，Reply 也能精确恢复问题引用；只说“已经记录”缺少可追踪
  回执；
- 中性、随机且不编码 verdict 的 ID 可以跨人工改判、版本变化和 Problem merge 保持有效；
- 固定事务模板可验证持久化成功与用户承诺一致，也避免 LLM 在终局回执中泄漏调查细节；
- 首版命令只保留确实改变 review / verdict / lifecycle 的动作，避免为空操作和尚未出现的归档需求提前建立
  状态；
- 写入失败仍由系统内部留下维护信号，普通用户只需等待主人处理。

## 带来的影响

- 需要公开 ID 生成、唯一性检查、alias 解析和回执引用绑定；
- 维护者查询必须按 alias / canonical ID 返回同一 Problem，合并后旧 ID 仍有效；
- 当前旧 Incident 查询命令仍使用相同命令头，接线时需要明确兼容或替代其参数路由，不能让两种编号互相
  误解析；
- “忽略 / 归档”不在首版，因此真实 dogfood 需要观察默认待处理列表是否产生过多噪声；
- 当前文件型单写者模型若不能原子更新 Problem、Occurrence、Report 与 alias，实施时必须重新评审事务存储，
  不能用多个独立文件先后写入伪装成一次成功。

## 没有采用的方案

### 不向普通用户显示 ID

没有采用。用户无法证明和转述具体记录，Reply 也失去最直接的精确问题引用。

### 使用连续 B 编号

没有采用。它泄漏记录数量、编码当前 verdict，并会在 unknown 改判后产生语义漂移。

### 只规定回复文风

没有采用。持久化回执需要与事务结果严格一致，应由模型外固定模板生成。

### 首版增加继续观察和忽略

没有采用。“继续观察”不改变开放记录的行为；“忽略”暂时没有真实噪声和恢复需求支持其生命周期成本。

## 与既有决定的关系

- [ADR-0078](0078-defer-persisting-unknown-bug-assessments.md) 暂缓深度 `unknown` 的公开编号、固定“已记录”
  回执和人工裁决路径；确定 `bug` 的中性编号与事务回执继续有效；
- [ADR-0079](0079-list-pending-problems-with-triage-query.md) 新增无编号的
  `triage 报错查询` 待处理列表；

- [ADR-0074](0074-preserve-append-only-problem-decisions.md) 规定“确认Bug / 确认非Bug”追加 Decision 并保留
  Agent 原判断；
- [ADR-0075](0075-register-problem-maintenance-under-triage-subcommand.md) 把本 ADR 的维护动作改为
  `triage 报错查询 ...` 子命令；独立顶层命令示例不再适用；
- [ADR-0073](0073-use-nonebot-orm-for-authoritative-bug-workflow-state.md) 用 ORM 事务落实公开 ID、alias、固定回执和
  维护生命周期；
- 补充 [ADR-0068](0068-treat-qualified-agent-bug-verdicts-as-operational-decisions.md) 的人工复核和生命周期；
- 补充 [ADR-0070](0070-separate-bug-reports-occurrences-and-problems.md) 的 Problem / Report 公开引用；
- 补充 [ADR-0071](0071-group-bug-problems-with-versioned-evidence-fingerprints.md) 的自动关联和固定回执；
- 保留 [ADR-0045](0045-use-one-triage-cooldown-and-localstore-capability-cache.md) 的固定维护命令名与
  SUPERUSER 边界；本决定只扩展问题记录参数和动作。

## 相关文档

- [ADR-0071：用版本化 Evidence 指纹聚合 Bug Problem](0071-group-bug-problems-with-versioned-evidence-fingerprints.md)
- [ADR-0068：把合格 Agent 的 Bug verdict 作为正式判断并由人工事后监督](0068-treat-qualified-agent-bug-verdicts-as-operational-decisions.md)
