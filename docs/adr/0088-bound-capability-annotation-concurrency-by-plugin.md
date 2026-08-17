# ADR-0088：按插件限制教学注释并发并保持插件内顺序

| 状态 | 决策日期 |
|---|---|
| 已采纳；已实现 | 2026-08-17 |

## 背景

教学注释刷新此前把所有缺少有效缓存的分析单元放在一个全局串行循环中。单次 Provider 请求可能接近或超过
一分钟；Bot 安装多个待分析插件时，即使这些插件互不依赖，完整刷新也会线性累加等待时间。直接无限并发则会
同时放大 Provider 限流、网络连接、token 消耗、内存占用和缓存写入竞争，难以在部署侧控制。

同一插件内的多个 Matcher / 参数化工厂可能共享源码、配置和公开语境。当前尚未实现共享 Handler 结果去重，
若插件内也并发，日志顺序和失败定位会更难理解，也会在同一插件上制造突发请求。

## 决策

1. 教学注释刷新先按 `plugin_module` 对待分析单元分组。不同插件可以并发；同一插件内继续按确定性顺序逐项
   `await`，本轮不做共享 Handler 去重或插件内并发。
2. 新增 `NBTRIAGE_CAPABILITY_ANNOTATION_MAX_CONCURRENCY`，表示同时运行的插件分析组上限。默认值为 `4`，
   合法范围为 `1..32`；值为 `1` 时等价于旧的全局串行模型调用。
3. 不新增教学专用 timeout。教学请求继续复用 `NBTRIAGE_MODEL_TIMEOUT_SECONDS`；部署者可以根据 Provider 延迟
   调整同一现有字段。并发数不会改变 Prompt、Schema、Evidence 或模型输出，因此不进入教学注释 fingerprint。
4. 全局 refresh lock、Runtime 准入、插件内顺序、逐项成功 cache、整轮失败不激活半套 Answer 视图，以及
   YAML / Markdown generation 的原子切换语义保持不变。并发任务只负责模型调用和公开投影；cache 写入仍经
   单一异步锁串行化，并按 capability ID 稳定排序。
5. 自动启动刷新与 `triage 刷新帮助 [plugin_module]` 使用相同调度器。指定单个插件手动刷新时自然只有一个
   插件组，不会因全局上限产生插件内并发。
6. Agent 内部的工具轮数、输出 token、证据字节和其他安全预算继续由任务合同固定，不额外暴露为部署配置。

## 为什么这样选

- 插件是现有缓存、输出文件、强制刷新参数和日志定位都已经使用的稳定边界，不需要引入新的调度身份；
- 有限并发能显著缩短多插件冷启动刷新时间，同时给 Provider 限流和本机资源留下明确上限；
- 插件内顺序保留了容易复盘的行为，并避免在尚无共享 Handler 去重时扩大同源重复请求突发；
- 复用现有 timeout 避免同时出现两个含义接近的模型超时配置，真实需要独立控制时再用后续决定增加。

## 带来的影响

- 有利：最多同时推进配置数量的插件，慢插件不再阻塞所有其他插件；
- 有利：单插件手动刷新、失败日志与原子发布语义不变；
- 代价：并发会提高瞬时 Provider 请求数和 token 速率，部署者需要按所用模型服务的限制调低配置；
- 代价：一个拥有很多分析单元的插件仍然串行，且相同 Handler 可能重复提供给模型；该优化明确留到真实成本
  证明值得后再做。

## 落实与确认

- `NBTriageConfig` 增加插件级并发上限并在 Runtime 注册教学注释服务时传入；
- `CapabilityAnnotationService` 按插件分组，通过 semaphore 限制活动插件数，并在每组内顺序执行；
- 单元测试用三个插件、每插件两个分析单元验证：跨插件最大并发为二、同一插件最大活动数始终为一，且组内
  分析顺序稳定；配置测试覆盖默认值与上下界拒绝。

## 相关文档

- [ADR-0058：用确定性证据与有界源码导航生成教学注释](0058-use-deterministic-evidence-and-bounded-navigation-for-teaching-annotations.md)
- [ADR-0069：分离帮助展示与 Answer 知识，并让静态分析只界定证据范围](0069-separate-help-display-from-answer-knowledge-and-bound-static-analysis.md)
- [ADR-0077：把上一版机器生成教学内容作为非证据的最小改写基线](0077-use-previous-generated-teaching-content-as-a-non-evidentiary-baseline.md)
