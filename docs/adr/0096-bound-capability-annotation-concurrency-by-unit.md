# ADR-0096：按教学单元限制教学注释并发

| 状态 | 决策日期 |
|---|---|
| 已替代 | 2026-08-20 |

## 背景

ADR-0088 把并发槽位分配给插件，并要求同一插件内的教学单元顺序执行。真实插件验证表明，一个插件可以同时
包含多个彼此独立的普通 Matcher 和参数化 family；当单次 Provider 往返接近一分钟时，单插件刷新仍会线性
累加到十几分钟。缓存、重试和发布已经在 ADR-0093 中按 teaching unit 保存状态，因此模型调用继续按插件串行
不再与实际状态边界一致。

直接为全部单元创建无限并发会放大 Provider 限流、token 速率和本机连接压力。源码变化与全局合同错误也仍需
保持既有的插件级或整轮作废语义。

## 决策

1. 所有待生成 teaching unit 进入同一个全局有限并发池；同一插件的不同单元可以并行。
2. `NBTRIAGE_CAPABILITY_ANNOTATION_MAX_CONCURRENCY` 保留原名与 `1..32` 范围，但含义改为同时运行的教学单元
   数量；默认值为 `10`，设为 `1` 时恢复全局串行。
3. 每个单元继续创建独立 Agent client、请求、Evidence 闭包、预算和状态。并发不会共享模型对话或模型生成摘要。
4. 若某单元发现 `SOURCE_CHANGED`，尚未取得槽位的同插件单元直接停止；已经在途的同插件调用可以收尾，但该
   插件本轮全部 staging 仍按既有规则作废。若发生 Provider 身份、Schema 或 HTTP 401 等全局停止错误，尚未
   取得槽位的单元停止，整轮新模型候选不激活。
5. 全局 refresh lock、最终源码与 Evidence 复核、插件 cache shard、不可变 generation 和 `current.json` 原子
   pointer 不变。并发只覆盖单元分析阶段，不把 cache 或发布切换改成并发事务。

## 为什么这样选

- teaching unit 已经是生成、失败、重试、回退与状态展示的最小边界；用同一边界调度更直接；
- 默认十个并发槽位能缩短多单元插件刷新时间，同时保留明确的 Provider 压力上限；
- `SOURCE_CHANGED` 和全局失败继续由 staging 作废保证正确性，不需要为了取消已在途网络请求引入额外状态机；
- 保留配置名避免无意义迁移，只更新其公开含义。

## 带来的影响

- 有利：单个大型插件的独立教学单元不再互相串行阻塞；
- 有利：跨插件与插件内调用共用同一上限，不会出现两层并发相乘；
- 代价：同一插件可能同时产生多个 Provider 请求，部署者需要按服务限流调低并发值；
- 代价：失败日志完成顺序不再稳定，但每条日志继续携带 `refresh_id`、插件、单元 ID、阶段、原因和稳定错误码；
- 代价：源码变化发生时，少量已经在途的同插件调用可能完成后被丢弃，产生无效成本。

## 替代关系

本 ADR 替代 ADR-0088 中“并发槽位按插件分配”“同一插件内顺序执行”和“单插件手动刷新不发生内部并发”的
决定。ADR-0088 的全局锁、有限并发、复用通用 timeout 和模型内预算原则继续有效；ADR-0093 的缓存、staging
和原子发布边界不变。

本 ADR 对配置值施加 `1..32` 固定范围的决定已被
[ADR-0120](0120-remove-the-fixed-capability-annotation-concurrency-ceiling.md) 替代；按 teaching unit 使用同一个
全局 semaphore 的调度边界继续有效。

本 ADR 第 5 项中“并发只覆盖单元分析阶段”的边界已被
[ADR-0122](0122-pipeline-capability-evidence-preparation-and-analysis.md) 替代；缓存与原子发布边界继续有效。

## 落实与确认

- `CapabilityAnnotationService` 为每个待分析单元建立任务，并用一个 semaphore 限制全局活动单元数；
- 同插件双单元测试确认二者可以同时进入 Agent，且全局活动数不超过配置值；
- `SOURCE_CHANGED`、全局失败、插件分片 cache 与原子发布回归继续通过。

## 相关文档

- [ADR-0088：按插件限制教学注释并发并保持插件内顺序](0088-bound-capability-annotation-concurrency-by-plugin.md)
- [ADR-0093：按插件分片教学注释缓存并按单元部分发布](0093-shard-capability-annotation-cache-by-plugin.md)
- [部署本地能力影子索引](../architecture/flows/capability-shadow-index.md)
