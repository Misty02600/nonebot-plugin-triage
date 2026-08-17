# ADR-0077：把上一版机器生成教学内容作为非证据的最小改写基线

| 状态 | 决策日期 |
|---|---|
| 已采纳；已实现，首轮真实 Provider held-out 未通过 | 2026-08-15 |

## 当时遇到了什么

教学注释在源码、运行结构或允许配置变化后需要重新生成。如果每次都让模型从空白开始写，即使功能语义
没有实质变化，摘要、用法顺序和措辞也可能大幅漂移；但若把旧注释当成事实继续沿用，又可能保留已经被
新 Matcher 结构或源码推翻的权限、参数和行为说明。

因此需要同时满足两个目标：让新一轮尽量保留仍然正确的旧文案，并且保证旧稿不能与当前 Evidence 争夺
事实所有权。

## 决定

1. 教学模型重生成时接收两个明确分离的输入区块：
   - `current_evidence` 保存本轮 Runtime Matcher 结构、源码、允许配置投影与只读工具补充 Evidence，是
     唯一事实来源；
   - `previous_annotation` 保存上一版 Triage 生成的公开文字，只作为编辑基线，不属于 Evidence。
2. `previous_annotation` 不携带可供新输出引用的旧 Evidence、配置值、源码或内部分析。人工维护的
   Migut Help YAML 继续只作离线评价源，不能成为生成基线；本任务生成的帮助投影也不能回灌为事实证据。
3. 在 generation contract、Runtime / 配置输入、源码 revision 与动态 Evidence revision 均未变化时，
   自动刷新逐字复用缓存，不调用模型。这是“不发生文案漂移”的强保证。
4. 输入变化或维护者主动强制刷新时，如果存在上一版机器生成内容，就把它作为基线交给模型；强制刷新
   表示重新分析，不表示全部重写。没有上一版时按冷启动生成。
5. Prompt 要求：旧陈述仍被当前 Evidence 支持时尽量逐字保留；只有当前 Evidence 使其错误、不完整、
   不安全，或新增了必须公开的使用信息时才能修改、删除或增加。模型返回完整新结果，不返回 diff，也不
   建立字段级自动 merge。
6. 所有新 claim、constraint 和 interaction 仍必须引用本轮允许的 Evidence ID；配置引用必须来自本轮允许
   投影。旧稿不能证明任何事实，也不能覆盖 Runtime / 确定性层拥有的注册状态、调用锚点、命令结构、
   public / restricted、平台、权限或场景事实。
7. 模型外继续校验结构化 schema、Evidence / 配置引用闭包、公开文字安全、usage 格式以及可确定的 Matcher
   结构一致性。能机械发现的冲突拒绝本轮对应结果，不对命令或前缀做猜测性修复。
8. 现有校验只证明“新陈述引用了当前存在的证据”，不能一般性证明自然语言陈述一定被证据语义蕴含。
   项目明确接受这一边界：自然语言忠实度通过模型资格、后续离线评测和人工 Migut Help 字段对比观察，
   不为此增加第二个判定模型、逐字段审批或人工 merge 流程。
9. ADR-0069 的双输出实现后，上一版 `help-display` 公开字段和上一版 `answer-knowledge` Markdown 分别作为
   对应输出的编辑基线；两者都必须根据同一轮冻结 Evidence 重新生成和校验。任一必要输出失败时不切换到
   一半新、一半旧的活动 generation。

## 为什么这样选

- 输入不变时直接复用，能够真正消除无意义重写和额外模型调用；
- 输入变化时提供旧稿，比从空白生成更容易保持人工可读的文风、详略和顺序；
- 把旧稿从 Evidence 集合中隔离，并要求所有新陈述重新引用当前 Evidence，可避免把历史结论自动升级为
  当前事实；
- 完整结果替换比模型生成 patch、字段级合并和冲突状态机更简单，也更符合当前单维护者的试用阶段；
- 承认自然语言语义校验的能力边界，避免用“Evidence ID 存在”冒充已经证明中文陈述正确。

## 没有采用的方案

### 每次都从空白生成

它最少受到旧稿影响，但会导致没有事实变化的文风和详略漂移，也浪费已经生成的高质量公开文字。

### 把旧注释作为 Evidence

旧注释是模型派生结果，不是 Runtime、源码或当前配置事实；这样做会形成自我引用，并可能让陈旧错误长期
存活。

### 让模型生成 diff 或建立字段级自动合并

它需要额外的 patch schema、冲突判断和部分发布语义，却仍不能证明自然语言修改正确。当前阶段采用完整
结构化结果重新校验并原子切换更清楚。

## 带来的影响

- 自动缓存命中与强制刷新必须区分：前者不调用模型，后者调用模型但仍默认携带上一版基线；
- 基线本身不参与当前事实 fingerprint，否则模型输出变化会反过来造成连续失效；
- 双输出生成器需要保存可定位的上一活动 generation，供下一次生成提取公开编辑基线；
- 后续评测应分别观察事实错误、结构错误和非必要文案 churn，人工 Migut Help 只能在生成完成后参与比较。

## 落实与确认

- 当前 `CapabilityAnalysisRequest.previous_annotation` 已使用独立的 `CapabilityAnalysisBaseline`，只包含上一版
  公开文字。
- 当前 Prompt 已声明旧注释仅用于稳定措辞；`CapabilityAnalysisService` 会拒绝本轮 Evidence / 配置允许
  集合外的引用，公开投影还会校验 usage 与实现细节泄漏。
- 当前自动刷新在 fingerprint 一致时逐字复用缓存；fingerprint 变化时才把上一版注释加入模型请求。
- ADR-0069 的 YAML + Markdown 双输出和 `triage 刷新帮助 [plugin_module]` 强制刷新已经接线；同
  fingerprint 的强制调用也会携带上一版基线。两种输出写入同一不可变 generation，并由一个原子指针
  切换；文件发布失败时关闭本轮内存注释视图，避免 Answer 与文件来自不同分析轮次。
- 2026-08-16 的首轮 16 条真实 Provider forward-heldout 包含一条“当前证据仍完全支持上一版”的最小改写
  用例；该用例没有形成有效模型结果，所以本轮没有证明真实模型能够稳定保留基线。整个教学 Gate 同时因
  schema、公开投影、Evidence 闭包、语义与工具门失败，任务继续使用本地 provisional evaluation ID，不能
  把其他任务的 Provider 资格继承到本合同。

## 与既有决定的关系

- 补充 [ADR-0058](0058-use-deterministic-evidence-and-bounded-navigation-for-teaching-annotations.md) 的
  重生成稳定性与 Evidence 所有权合同；
- 补充 [ADR-0069](0069-separate-help-display-from-answer-knowledge-and-bound-static-analysis.md) 的双输出
  重生成与发布一致性合同；
- 不改变人工 Migut Help 只作后续离线评价源的既有边界。
