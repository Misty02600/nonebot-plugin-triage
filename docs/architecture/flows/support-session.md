# 流程：可审计支持会话

## 这条流程保证什么

B1 预测不能直接升级为用户问卷或外部执行。控制面把预测路由映射为固定动作：`needs_evidence` 先由
确定性策略从模型候选中批准当前轮唯一槽位，`run_oracle` 则只有在人类显式批准后，才能关联一个已经由
NoneBot Triage Agent 版本边界校验的 Oracle 结果。状态与顺序事件持久化；当前流程不运行第三方代码、不写
GitHub，也不调用模型。

## 外部参与者和触发条件

- 操作者提供一个冻结 B1 报告和其中唯一的 `case_id`；
- B1 报告是只读决策来源，Issue 正文不会复制进会话；
- 补证提交者提供绑定会话、Case 和当前槽位的 JSON；只接收已脱敏结构摘要、内容哈希与大小；
- 审批者以显式 `actor` 批准执行型动作；
- Runtime validator 读取已有 `SupportCase` 与 schema v2 Oracle 结果，重算 Case / Oracle 规范化版本和
  受信根内 Probe 原始字节 SHA-256，再核对故障 / 修复引用和两侧断言。

## 稳定的状态变化

```text
B1 needs_evidence → select one candidate → request_evidence → awaiting_evidence
                                                              │
                                                    valid redacted receipt
                                                              ↓
                                           remaining candidate? ── yes ──┐
                                                    │ no                  │
                                                    ↓                     │
                                      ready_for_reassessment     select next one
                                                                          │
                                                                          └─→ awaiting_evidence
B1 escalate       → escalate         → escalated
B1 abstain        → refuse           → refused

B1 verify → run_oracle → awaiting_approval
                            │
                      explicit approval
                            ↓
                      ready_for_result
                            │
                validated / failed / blocked result
                            ↓
                    completed / blocked
```

每次成功变化追加一个连续序号事件。会话文件使用临时文件替换实现单文件原子写入；已有 session ID 不会被
创建命令覆盖。事件重放验证 route、状态、动作、回执和 Runtime 结果的内部一致性，但不提供本地文件的
防篡改真实性；签名或受信存储需要另行确定密钥与运维策略。来源报告的 SHA-256 用于发现报告被替换，不把
报告正文重复存入会话。

单步补证优先级按故障阶段冻结。策略不会新增模型未提出的槽位；完整 B1 候选仍保存在预测摘要中用于审计，
动作只保存当前轮选择。validation-only 离线评测确认平均问题数由 `4.125` 降至 `1.000`，但该优先级仍需
新的前向隐藏集验证，现有 held-out 不再参与调参。

回执 schema 对九类证据分别限制字段和类型。日志只保留异常类型、栈模块名和行数；配置只保留键名且必须
声明值已脱敏；其他槽位同样拒绝任意额外字段。每条回执保存内容 SHA-256 与字节数用于关联原始材料，但
控制面不读取或保存原始材料。接收后，从 B1 原候选中移除已接收槽位并重用单步策略；剩余集合为空时只
进入 `ready_for_reassessment`，不推断已解决。

## 失败时的语义

- 未批准的 `run_oracle` 拒绝附加任何结果，原会话不变；
- `needs_evidence` 没有候选时拒绝创建会话；持久化的补证动作与冻结策略不一致时拒绝加载；
- 回执未声明脱敏、含疑似 secret、字段不完整 / 越界、会话或槽位错绑、ID / 槽位重复时拒绝接收；
- 持久化回执的顺序、请求动作或事件元数据与冻结策略不一致时拒绝加载；
- Case ID、Case / Oracle 版本、故障 / 修复引用、Probe 路径 / 字节摘要或 Oracle 断言无效时，结果不能进入会话；
- 旧 Oracle schema v1 与旧会话 schema v2 显式拒绝加载，不在原始 Case 或 Probe 可能已变化后猜测迁移；
- `failed` 和 `blocked` 的合法结果把会话置为 `blocked`，保留 Probe 与阻塞原因；
- 损坏的 schema、非连续事件或 route / action / status 不一致会在加载时显式失败，不猜测修复；
- 该切片没有并发写入协调；文件存储只用于单用户本地 MVP，服务端持久化需要独立决定。

## 相关决定

- [架构概览](../overview.md)
