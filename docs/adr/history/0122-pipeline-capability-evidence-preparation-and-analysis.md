# ADR-0122：按教学单元流水化 Evidence 准备与分析

| 状态 | 决策日期 |
|---|---|
| 已采纳 | 2026-08-25 |

## 当时遇到了什么

教学单元的模型分析已经并发，但刷新仍会先串行构建全部 Evidence，再统一启动模型请求。大型插件或整项目
首次生成时，Jedi 与源码切片会让第一个已准备单元也等待其余单元，无法尽早分析、checkpoint 或完成。

## 最后决定

1. 先完成轻量的全局单元规划、family 分组、缓存身份检查；不在这一阶段构建完整 Evidence。
2. 每个单元准备完成后立即进入现有模型分析池，不等待其他单元。模型并发仍由
   `NBTRIAGE_CAPABILITY_ANNOTATION_MAX_CONCURRENCY` 控制。
3. Evidence 准备使用内部固定的 4 并发上限。不同插件可以并行准备；同一插件的准备串行使用其共享源码包和
   Jedi 缓存，但已经准备好的同插件单元仍可并行分析。
4. 单元完成后继续按 ADR-0121 即时 checkpoint；只有整轮成功后才原子发布。插件共享源码失败、最终 revision
   变化或全局停止仍会丢弃不应发布的候选。
5. 只使用 Python 标准库 `TaskGroup`、`Semaphore` 和现有线程卸载，不新增队列框架、配置项、公共 Schema 或
   常驻 worker。

## 为什么这样选

- 调度边界仍是现有 teaching unit，不引入新的业务对象；
- 逐单元衔接可以缩短首次可用结果时间，并让已付费结果尽早 checkpoint；
- 同插件准备串行避免并发修改共享源码缓存，模型等待则仍可与后续准备重叠；
- 固定的小型准备池限制本机 Jedi、文件读取和线程压力，Provider 并发仍由已有配置独立控制。

## 带来的影响

- 首个单元不再等待整个插件或项目完成 Evidence 构建；
- Evidence 准备和 Provider 分析可以重叠；
- 某插件较晚发现共享源码失败时，已经开始的同插件分析可能产生费用，但结果会被丢弃且不会发布；
- 完成顺序不稳定，最终缓存与状态仍按稳定单元 ID 排序。

## 落实与确认

- `CapabilityAnnotationService.refresh()` 以有界准备任务逐单元启动分析；
- 回归验证后续单元仍在准备时，首个单元已经进入模型分析；
- 同插件分析并发、checkpoint 恢复、共享源码失败回滚和原子发布测试继续通过。

## 替代关系

- 替代 [ADR-0096](0096-bound-capability-annotation-concurrency-by-unit.md) 中“并发只覆盖单元分析阶段”的边界。
- [ADR-0120](0120-remove-the-fixed-capability-annotation-concurrency-ceiling.md) 的模型分析并发配置语义与
  [ADR-0121](../0121-checkpoint-completed-teaching-units-before-atomic-publication.md) 的 checkpoint / 发布边界不变。

## 相关文档

- [能力影子索引流程](../../architecture/flows/capability-shadow-index.md)
