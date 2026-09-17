# ADR-0146：移除 Bug 与维护者 Agent 的累计预算

| 状态 | 决策日期 |
|---|---|
| 已采纳 | 2026-09-17 |

## 问题

生产 Bug 调查曾把 Provider 在多次请求中报告的输入与输出相加，并通过公开配置设置累计 token 上限；
维护者对话还同时设置累计 token、由单次输出乘请求次数得到的累计输出以及美元费用上限。多轮 Agent 会
重复携带消息历史，Provider 也可能把缓存输入计入 usage，因此这些累计数值既不等于当前单次上下文，也不
等于稳定可比较的实际成本。不同 Provider 的费用元数据可用性也不一致。

项目已经分别拥有请求次数、工具次数、总墙钟、单次输出、证据正文和可信模型单次 context window 等边界。
累计预算与这些离散边界叠加后，会在合法长任务中提前终止，却不能代替 Provider 对单次硬窗口的检查。

## 决策

- 生产 Bug 调查不再设置累计 token 或美元熔断。删除公开配置
  `NBTRIAGE_BUG_TOTAL_TOKENS_LIMIT`；旧键继续出现时配置加载明确失败，并提示部署者删除。
- Bug 继续保留总墙钟、单次输出、通用工具次数、由工具额度推导的请求次数、证据正文限制和可信
  `ModelProfile` 的单次输入窗口保护。checkpoint / finalizing 与稳定工具 schema 不变。
- 维护者对话不再设置 512k 累计 token、单次输出乘请求次数得到的累计输出或 1 美元费用上限。每轮仍保留
  15 次请求、60 次工具保险丝、单次输出配置、总墙钟、上下文压缩和最终回答阶段。
- 教学生产运行落实 ADR-0130 已决定的“无累计 total-token 上限”；10 次请求、10 次导航、单次输出、
  0.05 美元费用上限、单次窗口准备与最终提交预留继续有效。该费用上限不扩展到 Bug 或维护者对话。
- usage 中的输入、缓存输入、输出与可得费用仍用于诊断、评测和容量观察，但不再被解释为 Bug 或维护者
  生产熔断。显式维护评测或 live 诊断可以为一次隔离运行传入额外上限；它们不构成产品默认或公开配置。
- 删除或改变累计预算会改变任务合同身份。Bug 预算指纹和教学 request / budget profile 必须同步升级，旧
  held-out 或历史真实运行不能自动晋级为新合同资格。

## 原因与影响

- 离散边界直接对应可控动作，能在 Provider usage 口径不同的情况下提供一致的循环和副作用止损。
- 单次窗口保护继续防止下一轮请求超过可信模型能力；移除累计 token 不等于允许无限上下文。
- 长任务不会再因为缓存历史被重复计数而提前失败，代价是生产运行不再提供近似总 token 或美元封顶。
- 部署者若需要严格财务上限，应在 Provider、网关或独立执行环境设置；项目继续记录可得 usage 供观测。
- 这是公开 Bug 配置的破坏性变更，升级时必须删除旧环境变量或配置键。

## 替代关系

- 部分替代 [ADR-0129](0129-use-only-pydantic-ai-native-model-transports.md) 的任务累计 token 数值；原生
  Provider、Profile、thinking、usage 与单次输出决定继续有效。
- 部分替代 [ADR-0130](0130-finalize-production-agents-before-hard-budget-exhaustion.md) 的 Bug 和维护者累计
  token 条款；阶段收尾、请求、工具和时间边界继续有效。
- 部分替代 [ADR-0136](0136-configure-bug-investigation-budgets.md) 与
  [ADR-0145](0145-combine-configurable-bug-budgets-with-finalization.md) 的 Bug 累计 token 配置；其他 Bug
  离散边界、资格指纹与稳定工具定义继续有效。

## 落实与确认

- 产品配置移除旧字段并对残留键给出迁移错误；README 同步删除配置项并记录升级要求。
- Bug 生产工厂不再把累计 token 传入 Agent；维护者 Agent 的 `UsageLimits` 只保留请求与工具次数。
- 教学 request revision 和 budget profile 升级，且不再声称 384k / 64k 累计输入目标。
- 回归覆盖默认与扩展 Bug 配置均不设置累计预算、维护者运行不设置累计预算，以及维护诊断不恢复已删除的
  教学预载目标。

## 相关文档

- [模型 Provider 支持矩阵](../architecture/model-provider-support.md)
- [支持入口分流](../architecture/flows/support-intake-routing.md)
- [架构总览](../architecture/overview.md)
- [README 配置说明](../../README.md)
