# ADR-0121：在原子发布前持久化已完成教学单元

| 状态 | 决策日期 |
|---|---|
| 已采纳 | 2026-08-25 |

## 当时遇到了什么

一次插件刷新会并发分析多个 teaching unit，但候选原先只存在于 `refresh()` 的内存 staging。进程被取消、
Provider 长连接未结束或维护命令异常退出时，已经付费并通过完整投影校验的单元也会随进程丢失；下一次刷新
只能重新调用模型。显式维护诊断同样只在整个插件结束时汇总写文件，中断后可能只剩脱敏 trace，看不到已经
返回的正文、thinking、工具往返和错误。

这不应改变既有发布安全边界：一个单元完成不等于插件教学合同已经发布，Help、Answer 和内存视图仍必须由
同一不可变 generation 与 `current.json` 原子切换。

## 决定

1. 插件缓存 schema 增加 teaching unit 级 `pending`：它保存已经通过 Schema、Evidence、usage、权限和公开
   投影校验，但尚未由活动 generation 发布的完整注释。`last_good` 仍只表示当前活动 generation 对应的
   最近可用结果，`last_attempt` 仍只记录最近真实尝试。
2. 每个单元结束后立即在插件 cache shard 中原子写入 `pending` 与 `last_attempt`。同一插件的并发写入由进程内
   checkpoint lock 串行化；文件继续使用临时文件、`fsync` 和 `os.replace`，不新增每单元文件、SQLite 或
   跨文件事务。
3. 重启或重试时，只有插件源码 revision、请求 fingerprint 和动态 Evidence manifest 全部仍匹配的
   `pending` 才能作为本轮候选复用，并免除该单元的 Provider 调用。它可以作为非证据 baseline，但在发布前
   不进入活动视图，也不成为 Bug 预检合同。
4. 插件输出全部完成并原子切换 `current.json` 后，候选统一提升为 `last_good` 并清除 `pending`。发布失败或
   全局停止时保留已完成 `pending`，下一轮只补缺失或失败单元；失败尝试不抹掉更早且仍匹配的 pending。
5. 最终源码复核发现插件 revision 变化时，恢复刷新前的插件分片或删除本轮新分片，不保留变化前生成的
   checkpoint。
6. 显式维护 capture 另使用与目标输出同目录的追加式 `.partial.jsonl` journal。每个完整单元记录追加后
   `fsync`；正常结束时汇总成原有 `model-output.json` 并删除 journal。journal 仅用于本地诊断恢复，不参与
   候选复用或公开发布。
7. 缓存 schema 由 2 升至 3，旧分片完全失效并按当前源码重建，不提供只读迁移。

## 为什么这样选

- 付费边界与发布边界分离：完成的计算可以恢复，但未完成插件不会半发布；
- `pending` 复用继续绑定现有 revision、fingerprint 与 Evidence currentness，不建立第二套事实真值；
- 单插件 JSON 原子替换已经是成熟持久化边界，只增加一个字段和一把短时写锁；
- 原始响应 journal 与注释 checkpoint 职责分离，前者用于复盘，后者只保存通过完整校验的候选。

## 没有采用的方案

- 不把单元成功立即写入 Help / Answer 输出；这会破坏插件输出和全局活动指针的一致性。
- 不只依靠原始 Provider response 恢复候选；原文没有经过最终公开投影校验，不能直接升级成教学合同。
- 不实现跨插件 WAL、SQLite staging 或两阶段提交；插件 cache 仍是可删除重建的加速层。

## 带来的影响

- 中断后的重复模型费用缩小到尚未完成或失败的单元；
- 分片可能包含未发布 `pending`，运维读取时必须明确区分它与 `last_good`；
- 单元完成时多一次小型同步落盘；相比 Provider 延迟和 token 成本，该开销可接受；
- 只有 `current.json` 选中的 generation 仍是 active teaching contract。

## 替代关系

- 部分替代 [ADR-0093](0093-shard-capability-annotation-cache-by-plugin.md) 中“候选只存在于内存、不持久化
  staging、崩溃用重复计算恢复”的决定；其插件分片、单一活动指针和原子发布边界继续有效。
- 扩展 [ADR-0097](0097-capture-complete-capability-model-output-in-explicit-maintenance-runs.md) 的维护诊断：
  正常最终文件格式不变，中断恢复由本地 journal 补足。

## 相关文档

- [能力影子索引流程](../architecture/flows/capability-shadow-index.md)
