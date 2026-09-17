# ADR-0145：把可配置 Bug 预算与预算前收尾机制组合使用

> 2026-09-17：[ADR-0146](0146-remove-cumulative-budgets-from-bug-and-maintainer-agents.md) 替代本决定中的
> Bug 累计 token 配置；其他离散边界、稳定工具定义与收尾机制继续有效。

| 状态 | 决策日期 |
|---|---|
| 已采纳 | 2026-09-16 |

## 问题

ADR-0136 已决定放宽 Bug 调查预算并交由部署者配置，后续又把生产默认提高为 12 次通用取证、15 次模型请求，
同时固定单次调查的工具定义以改善缓存。ADR-0130 随后引入 `running → checkpoint → finalizing` 收尾机制，
但又把 Bug 数值写回 120 秒、800 单次输出、8 次通用取证、12 次请求、120k total token 与 0.50 美元，
并恢复动态移除工具。整合两个分支时，这些较窄条款被误读为对 ADR-0136 的整体替代。

## 决策

- 保留 ADR-0130 的通用阶段机制：接近硬上限时最多提示一次 checkpoint；进入 finalizing 后以
  `tool_choice="none"` 禁止继续取证，为最终结构化判断和一次输出纠正预留请求。
- Bug 专属预算继续以 ADR-0136 的最终状态为准，并通过 NoneBot 配置暴露：默认总超时 300 秒、单次输出
  16,384 tokens、累计输入输出 300,000 tokens、12 次通用证据调用；模型请求上限由通用额度加 3 推导，
  默认 15 次。默认不设美元费用上限，也不再从单次输出乘请求次数派生累计输出限制。
- 运行期 function tool 定义由本轮初始证据范围确定。已经下发的工具不会因单类或总额度耗尽而从后续
  schema 动态消失；执行层仍返回结构化不可用结果并严格拒绝超额调用。进入 finalizing 时工具定义保持稳定，
  由 `tool_choice="none"` 禁止调用。
- Provider 有可信原生元数据且未使用自定义 Base URL 时，把模型 context window 扣除单次输出额度后作为
  下一请求输入上限；未知或自定义 endpoint 不猜测。预算字段及已知 context window 一并进入资格指纹，
  任一变化都不能复用另一预算组合的质量资格。
- Semantic、Guidance、Teaching 和维护者对话的预算不受本决定影响；正式 Bug 入口是否开放、副作用授权、
  证据准入与三值 reconciler 边界也不改变。

## 替代关系

本决定仅替代 ADR-0130 中 Bug assessment 的固定数值、动态移除工具及 120k / 0.50 美元条款；ADR-0130 的
阶段收尾机制继续有效。它重申并继续 ADR-0136 的可配置预算与稳定工具定义，因此 ADR-0136 不是历史废案。

## 落实与确认

- 配置默认值、环境变量覆盖、正数与有限超时校验由单元测试覆盖；
- Agent 工厂向 SDK usage limits、Toolbox 和资格指纹传递同一组配置；不同预算不会命中同一资格；
- 回归验证超过旧累计输出与费用值仍可完成，工具 schema 在 running / checkpoint / finalizing 请求间稳定，
  finalizing 仅通过 `tool_choice="none"` 禁止调用；
- 完整回归、格式、静态检查与锁文件一致性在整合提交前重新执行。

## 相关文档

- [ADR-0130：在生产 Agent 硬预算耗尽前显式收尾](0130-finalize-production-agents-before-hard-budget-exhaustion.md)
- [ADR-0136：由部署者配置 Bug 调查预算](0136-configure-bug-investigation-budgets.md)
- [支持入口分流](../architecture/flows/support-intake-routing.md)
- [架构总览](../architecture/overview.md)
