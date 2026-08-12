# ADR-0024：自动公开确定且低风险的能力字段

| 状态 | 决策日期 |
|---|---|
| 已采纳 | 2026-08-12 |

## 当时遇到了什么

部署本地能力影子能够自动发现大量第三方插件入口，但如果所有自动候选都默认要求逐项人工批准，维护成本会
随插件数量线性增长，普通用户也长期只能看到主动接入 Triage Provider 的极少数能力。README、帮助图和
模型解释可以丰富语义，却不适合作为每条命令公开与否的唯一门槛。

## 决策

1. 当前运行时或结构化源码能够确定用户调用入口、支持平台可判定，且没有 hidden、SUPERUSER-only、
   operator deny 或停用等受限信号时，命令头、结构化参数等低风险字段自动进入 `public`。
2. 动态入口、仅模型推断的入口、证据冲突、敏感歧义、支持平台未知或证据不足的项继续进入 `review`；
   被动、定时和启动行为默认不因插件存在而自动公开。
3. README、PluginMetadata 和帮助数据用于补充名称、说明、示例、同义词与功能边界。缺少这些来源不是
   自动公开的否决条件；与运行时或源码冲突时，只压制冲突字段并形成 review 问题。
4. 不要求第三方插件逐个注册 Triage Provider。显式 Provider 仍是高置信覆盖和上下文可见性来源，部署者
   仍可用 operator policy 覆盖自动结果。
5. `public / restricted` 回答受众范围，`review` 更接近证据或分析异常状态。是否在持久 schema 中拆成两个
   维度尚未决定，本 ADR 不固定数据库列。

## 为什么这样选

- 已注册且语法确定的用户入口本身是机器可核对的公开面；
- 字段级准入允许确定语法先服务，而不因描述或限流细节未知隐藏整个能力；
- README 与模型继续发挥语义补全价值，但不能单独决定披露策略；
- 人工只处理动态、冲突和敏感异常，不审核生态中的每条普通命令。

## 没有采用的方案

- **所有自动候选默认 review，逐条人工批准**：安全但无法覆盖大量第三方插件，维护成本不可接受。
- **只要 README 提到就自动公开**：自由文本和模型解释不是稳定的受众策略信号。
- **发现任意 Rule、Permission 或限流就隐藏整项能力**：这些通常是公开能力的执行约束，不等于能力存在性受限。

## 带来的影响

- 普通用户能够检索当前 adapter 域内的确定公开命令；
- 自动公开不证明当前用户一定可执行，最终仍由原 Matcher 的权限、场景、配置和运行状态判断；
- 字段冲突与不确定约束需要独立 review，而不是把整个能力退回不可见；
- operator deny 和稳定 selector 仍需要后续实现，才能覆盖项目侧例外。

## 落实与确认

- 2026-08-12：运行时快照已对确定 Alconna / `on_command` 入口实施自动 `public`；hidden、停用和
  SUPERUSER-only 仍为 `restricted`，被动或平台未知入口仍为 `review`。
- 普通用户影子检索已在 SQL 召回与 `limit` 前限定当前 adapter 的 `public` 记录，并对 stale generation
  失败关闭；显式 Provider 仍优先，首次后台构建 ready 前也只使用 Provider。
- operator deny、稳定 selector、字段冲突工作流及持久 schema 双轴拆分尚未实现。

## 替代关系

- 第 5 项尚未确定的持久 schema 已由
  [ADR-0032](0032-separate-capability-audience-analysis-and-platform-status.md) 落实为受众、平台范围、具体分析问题与约束独立轴；
- 部分替代 [ADR-0021](0021-use-deployment-local-capability-shadow-index.md) 中“自动发现候选默认 review”的边界；
- 部分替代 [ADR-0022](0022-limit-capability-shadow-guidance-to-superusers.md) 中普通用户只能读取显式 Provider 的
  过渡实现；SUPERUSER 的未解决问题 / restricted 维护者检索仍保留；
- 回答投影继续由 [ADR-0026](0026-filter-capability-knowledge-before-retrieval.md) 与
  [ADR-0027](0027-constrain-guidance-with-facts-not-fixed-wording.md) 约束。

## 相关文档

- [部署本地能力影子索引](../architecture/flows/capability-shadow-index.md)
- [Alconna 能力与解析回执](../architecture/flows/alconna-capability-and-parse-receipts.md)
- [可选帮助数据源与复用边界](../architecture/help-source-adapters.md)
