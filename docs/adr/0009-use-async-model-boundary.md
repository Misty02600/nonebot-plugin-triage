# ADR-0009：模型调用核心采用异步协议并由同步 CLI 在边缘桥接

| 状态 | 决策日期 |
|---|---|
| 已采纳 | 2026-08-09 |

## 背景

`src/nbtriage/rag.py::B1ModelClient.generate`、`B1Runner.predict` 与
`tools/nbtriage_maintainer/evaluation.py::evaluate_b1` 当时都是同步接口，现有
当时位于 `src/nbtriage/providers.py` 的 `OpenAIResponsesB1Client` 也使用同步 OpenAI SDK。这个边界能服务一次性 CLI
评测，但未来若从 NoneBot Matcher 调用，会在 Bot 的活动事件循环里执行阻塞网络请求。

ADR-0008 已决定使用 Pydantic AI Direct Request；其公开异步 `model_request()` 与 NoneBot 的执行模型一致，
同步包装只适合不存在活动事件循环的进程边缘。项目还必须保持 pre-model safety、响应缓存、严格单次调用
预算和确定性评测，不能因为采用异步就默认并发请求或改变调用顺序。

## 决策

1. 项目自有 `B1ModelClient.generate` 改为异步协议；`B1Runner.predict` 与 `evaluate_b1` 沿调用链异步化；
2. 厂商或 Pydantic AI adapter 必须使用真正的异步客户端，不在事件循环内直接运行同步网络 SDK；
3. 同步 CLI 只在命令处理边缘使用一次 `asyncio.run()` 进入异步调用链，不在领域核心创建或嵌套事件循环；
4. 首轮仍按 Case 顺序串行等待模型响应。并发、批处理和跨请求共享限流需要另行设计，不能由异步化自动引入；
5. pre-model safety 命中、缓存命中、调用预算、响应校验和缓存写入顺序保持不变。测试替身同样实现异步协议，
   不通过线程包装掩盖同步实现；
6. 该决定不授权 NoneBot 插件发起真实模型调用，也不改变付费确认、数据准入、工具禁用和外部副作用边界。

## 选择理由

- NoneBot Matcher 原生运行在异步事件循环中，异步领域边界能避免后续再增加第二套调用协议；
- 从 Provider 到核心端到端异步比在插件层使用线程池包装同步 SDK 更容易表达取消、超时和调用次数；
- CLI 在最外层桥接可以保留当前同步命令入口，不把事件循环所有权扩散进领域代码；
- 暂不并发可以保留冻结评测的请求顺序、预算含义和缓存行为，降低迁移变量。

## 没有采用的方案

- 保持同步协议并在 NoneBot 入口使用 `asyncio.to_thread()`：会把取消和超时边界留在线程内，也允许新
  Provider 继续实现阻塞网络调用；
- 同时维护同步与异步两套公开协议：项目尚未发布，没有足以抵消双实现与双测试成本的消费者；
- 在 `B1Runner` 内调用 `asyncio.run()`：活动事件循环中不可用，并让领域对象错误地拥有循环生命周期；
- 立即并发评测所有 Case：会改变调用预算耗尽顺序、Provider 压力和报告可复现性，不属于本次迁移。

## 带来的影响

- 有利：NoneBot、异步 Provider SDK 与 Pydantic AI Direct Request 可以共享一条非阻塞调用链；
- 代价：`B1Runner.predict`、`evaluate_b1`、安全评测替身、CLI 和相关测试需要同步迁移；
- 风险：漏掉一个未 `await` 的调用会产生协程对象而非预测结果，必须用类型检查、单测和 CLI 回归覆盖；
- 限制：异步只解决等待模型 I/O 的执行边界，不等同于允许并发、取消后重试或真实运行时接入。

## 落实与确认

- 2026-08-09：维护者明确确认采用异步接口，并以 OpenAI Responses 作为首个参考 Provider；
- 实施情况：已落实。领域协议、Runner、B1/S3 评测与 Responses adapter 已端到端异步，仓库维护 CLI 只在
  边缘桥接；该 CLI 后续按 ADR-0016 迁入 `tools/nbtriage_maintainer/`，异步边界未改变。
- 2026-08-11：历史 Responses 客户端随维护者评测工具迁入
  `tools/nbtriage_maintainer/providers.py`；它不再属于插件发行面。

## 相关文档

- [ADR-0008：采用 Pydantic AI 的受控模型适配层](0008-pydantic-ai-controlled-model-adaptation.md)
- [架构概览](../architecture/overview.md)
