# 流程：最小化运行观察进入有界缓冲

## 这条流程保证什么

NoneBot 2.5 运行观察器已经能把事件、Matcher 和平台 API 生命周期转换为传输无关的结构化观察，但不能
把框架对象、消息正文、用户 / 群 ID、API 参数或返回值带入领域核心。观察器必须由调用方显式注册；核心
继续只负责 schema 守门、显式容量与 TTL 淘汰、关联过滤和证据包生成，当前不持久化这些观察。

## 输入与状态变化

```text
NoneBot 2.5 public hooks
        │
        ├─ event_preprocessor creates correlation_id in event state
        ├─ run preprocessors read copied Matcher state
        └─ API hooks read current Matcher state
        │
        ↓
NoneBotRuntimeObserver keeps only safe identifiers
        ↓
strict RuntimeObservation schema
        ├─ unknown / raw field ─→ reject
        ├─ invalid subject/outcome ─→ reject
        └─ valid minimal identifiers
                     ↓
        explicit-capacity, explicit-TTL buffer
             ├─ expired / capacity eviction ─→ increment dropped_count
             └─ capture(correlation_id) ─→ sorted RuntimeEvidenceBundle
```

事件观察只能携带适配器与事件类限定名；Matcher 观察携带由模块、Matcher 类型和优先级组成的标识，可选
携带插件 ID；API 观察只能携带规范化 API 名。只有失败的完成观察能携带异常类限定名与最多 32 个去重栈
模块。异常消息、Traceback 正文、文件路径和局部变量不属于 schema。

事件预处理生成的关联 ID 写进 NoneBot Triage Agent 专用 state key；NoneBot 把事件 state 复制给命中的 Matcher，
Matcher 内部 API 调用再通过公开的 `current_matcher` 上下文取回同一 ID。Matcher 外的启动期、后台任务等
API 调用没有可证明的事件关系，因此当前直接忽略，不猜测归属。事件后处理钩子不提供事件级异常，所以
`event_completed/succeeded` 只表示分发流程到达后处理；实际 Matcher 或 API 失败由各自完成观察记录。

缓冲允许异步钩子以非时间顺序提交，容量淘汰按提交顺序，输出按实际时间点与 `observation_id` 稳定排序。
TTL 从进入缓冲的时间计算，避免未来时钟偏移让观察超过最长驻留时间；已经早于 TTL 的迟到输入也会直接
拒绝。证据包暴露全局 `buffer_dropped_count`；该值大于零时，消费者必须把链路视为可能不完整，不能据此
排除未观察到的 Matcher 或异常。

## 失败语义与边界

- schema 版本、字段集合、时区、标识符、kind / outcome 或主体组合不合法时拒绝整条观察；
- 容量与 TTL 没有默认值，构造缓冲时必须显式给出；当前上限分别是 1,000,000 条和 7 天，但这不是推荐的
  生产默认值；
- 已过 TTL 的输入不会重新进入缓冲，会增加丢弃计数并返回未接收；
- 观察器的 hook 捕获采集异常并增加本地 `dropped_count`，不把异常抛回 NoneBot；buffer 拒绝、容量或 TTL
  淘汰另由 buffer 计数，诊断界面未来必须同时展示两类损失；
- 注册必须显式调用；导入模块没有副作用，同一观察器重复注册会拒绝。NoneBot 没有对应的公共注销入口，
  当前也不支持热切换观察器；
- 当前实现只面向单进程内存，不提供跨 Worker 关联、崩溃恢复、并发数据库写入或管理员导出；
- `correlation_id` 是本地生成的有界不透明标识，不编码 QQ 用户、群或消息 ID；尚未实现 QQ 回复消息到
  correlation ID 的绑定、报障 Matcher、权限与群内告知策略，也未选择生产容量 / TTL 默认值。

## 相关决定

- [ADR-0001：QQ 群显式报障与本机运行证据](../../adr/0001-qq-group-report-linked-runtime-evidence.md)
- [ADR-0002：分级自治与所有权感知修复](../../adr/0002-tiered-autonomy-and-ownership-aware-remediation.md)
