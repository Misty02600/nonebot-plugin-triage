# ADR-0130：在生产 Agent 硬预算耗尽前显式收尾

| 状态 | 决策日期 |
|---|---|
| 已采纳 | 2026-09-16 |

## 当时遇到了什么

Capability Teaching、Bug assessment 与维护者对话都已有 request、tool、token、deadline 和费用止损，
但这些限制的行为并不一致：Teaching 会按累计输入 token 提前隐藏导航工具；Bug 在证据额度耗尽后仍可能
继续请求已经移除的工具；维护者对话达到请求上限时没有专门的最终回答机会。缓存命中的历史输入会被累计
token 限制重复计算，因此它也不能代表当前单次模型上下文或未缓存成本。

Bug assessment 原有一次 conversation 加六次通用证据的共享额度，还小于各通用证据工具局部上限之和。
这会迫使 Agent 在运行、日志、源码、设计和部署证据之间提前放弃两个合法动作。旧 Prompt v8 与 held-out
资格是在旧预算下得到的，不能直接继承给新策略。

## 决策

1. 三条生产多步 Agent 统一使用 `running → checkpoint → finalizing` 语义，但分别确定阈值；checkpoint
   最多向模型出现一次，普通阶段不持续显示剩余次数。
2. `finalizing` 通过 Pydantic AI 动态 `tool_choice="none"` 禁用函数工具。Teaching 在 ModelProfile 支持
   `json_object` 时显式使用 prompted output，取消 `final_result` output tool，保留稳定的导航工具定义以
   维持请求前缀；Bug 仍在运行阶段动态移除已经耗尽或不适用的证据工具。
3. Teaching 保留十次请求与十次导航：第八次导航或第七次已完成请求进入 checkpoint；导航耗尽、已完成
   八次请求或进入最终时间窗口后 finalizing，为最终输出和一次纠正保留请求。生产默认不再设置累计
   `total_tokens_limit`，也不再向每个工具结果附加剩余导航次数。
4. Bug assessment 调整为十二次请求、八次通用证据和一次独立 conversation：第六次通用证据或第九次
   已完成请求进入 checkpoint；无可用证据动作或已完成十次请求后 finalizing，为最终判断和一次输出纠正
   保留请求。八次通用额度等于 runtime、logs、两次 source search、一次 source read、两次 design search
   和 deployment 的局部上限之和。
5. Bug Prompt 升级为 v9，预算合同升级为 v4。旧 v8 held-out 继续作为历史证据，但不得晋级新合同；新配置
   必须重新运行 development / forward-heldout 多 trial，确认新增调用取得不同证据而不是重复搜索。
6. 维护者对话保留十五次请求与六十次工具保险丝：完成第十三次请求后 checkpoint；完成第十四次请求后
   finalizing，使第十五次请求只能直接回答。六十次工具额度保持模型不可见。
7. Bug 的 120k 与维护者对话的 512k 累计 token 上限暂时只作隐藏 emergency fuse，不参与 checkpoint。
   单次真实上下文上限、历史压缩和 tool-result 限制另行决策，本 ADR 不新增相关实现。

## 原因与影响

- request/tool/deadline 继续提供确定的循环止损，最终阶段不再依赖模型自行猜测还剩多少预算；
- checkpoint 按任务可完成的动作组合设置，而不是统一套用百分比；
- 支持 `json_object` 的 Teaching transport 不因最终阶段删除导航工具定义而改变缓存前缀；Bug 的动态工具列表仍准确表达证据动作可用性；
- Bug 获得更完整的证据域覆盖，但新 Prompt/预算在重跑前保持未验证，不继承旧资格；
- 累计 token 与单次上下文、缓存成本被明确分开，避免用错误指标提前结束任务。

## 替代关系

- 替代 ADR-0061 与 ADR-0064 中“一次 conversation + 六次通用证据、九次请求”的具体预算；它们的会话
  绑定、独立 conversation 额度、证据安全与模型外协调边界继续有效。
- 延续 ADR-0050 的有界 Bug Agent、确定性 reconciler 和副作用边界。
- 不改变 ADR-0010 / ADR-0012 的 B4 离线领域 runner；B4 不属于本次生产预算统一范围。

## 落实与确认

- 生产实现使用共享的 Pydantic AI capability 在请求边界提供一次 checkpoint，并在 finalizing 动态设置
  `tool_choice="none"`；
- Teaching、Bug 与维护者对话均有边界单测；并行 Teaching 导航继续由原子 claim 限制；
- Bug v8 冻结 Fixture 明确因 Prompt 与预算合同不匹配而失去当前晋级资格；
- Bug v9 已增加十条 development 边界 Fixture 与两轮默认运行的评测入口，记录 checkpoint、
  finalizing 后调用、重复工具参数、第七/八次证据价值、逐请求 cache/token/cost 与异常消耗；
  该 development Gate 只用于迭代，不产生资格；
- 单次上下文上限与工具结果压缩尚未实施。

## 相关文档

- [ADR-0050：用有界只读 Agent 完成 Bug 判断](0050-use-a-bounded-agent-for-user-bug-assessment.md)
- [ADR-0061：读取最新有界会话窗口](history/0061-read-latest-bounded-conversation-window-for-bug-assessment.md)
- [ADR-0064：细化 Bug 会话证据与结论合同](history/0064-refine-bug-conversation-evidence-and-verdict-contract.md)
- [支持入口分流](../architecture/flows/support-intake-routing.md)
- [架构总览](../architecture/overview.md)
