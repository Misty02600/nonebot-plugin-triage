# ADR-0137：结合首轮问题和实际追问理解一次补充

> 当前联合输入、插件选择与输出修复预算局部由 [ADR-0138](0138-combine-support-intent-and-plugin-selection.md) 替代；下文保留原决定。

| 状态 | 决策日期 |
|---|---|
| 已采纳 | 2026-09-10 |

> 2026-09-11：一次补充及补充后结束提示的规则由 [ADR-0143](0143-share-two-supplements-across-support-and-bug-assessment.md) 局部替代；其余决定继续有效。

## 问题

Bot 询问翻页间隔和中途消息后，用户回答“十秒内发的，中间没发别的”。作用域 Thread 已正确续接，
但 Semantic 只收到这个短句，因此再次判为意图不明，关闭补充机会，后续公开用法初检无法执行。
同样的问题也影响“这个怎么用”之后补充功能名称。

## 决定

1. 首轮仍只投影当前规范化请求。仅在同作用域成功领取一次待补充 Thread 时，Semantic 输入可以增加
   `supplement_context`，包含首轮 `request_text` 和实际成功发送的 `question`。每段沿用 8000 字领域上限；
   当前入口仍限制 2000 字。只在发送成功后保存追问，不读取任意历史、递归 Reply 或其他会话。
2. 当前答复确实回答追问或明确延续原任务时，模型结合首轮意图与观察理解它，不要求用户重复完整问题。
   因此可以补充想获得的结果、功能名称或实际操作条件。明确的新任务、更正和否认以当前文字为准，
   新任务不继承旧目标与旧观察；没有回答追问的含糊短句仍可以澄清失败。
3. 沿用一次 Semantic 调用、v7 输出与确定性 router，不增加分类步骤、意图枚举或强制复用旧路由。
   Prompt 更新为 `support-semantic-v7-prompt-v7-zh`，数据策略为
   `current-request-and-pending-supplement-v1`；旧 Prompt / 数据策略的评测资格不能继承。
4. 新增上下文与当前文字一样是不可信输入，执行 Semantic 既有秘密守门。身份、scope、correlation、
   原始 Reply、其他历史、能力索引和内部证据仍不进入 Semantic；模型外鉴权和工具边界不变。
5. 路由后的 Guidance / Bug 上下文也包含实际追问，使时间、数字、肯定或否定答复有明确指向。
   沿用同 scope、显式 `triage`、一次补充、过期关闭、发送失败关闭和第二轮不再等待的机制。

## 取舍与替代关系

这是短期任务上下文，不引入聊天记忆或新的会话管理层。是否回答了追问仍由模型判断，需要用真实补充
和明确换题样例检查；功能测试中的固定分类结果只证明信息传递与路由，不能证明模型理解质量。

局部替代 [ADR-0038](0038-limit-semantic-assessment-remote-data-projection.md) 的“只能外发当前单条文字”，
以及 [ADR-0060](0060-use-scope-thread-and-post-route-conversation-context.md) 第 3、6、7 项的
“补充文字必须独立产生意图、Semantic 完全不看 Thread”限制。其他边界保持有效。
[ADR-0066](0066-use-active-teaching-contract-as-bug-precheck.md) 的观察门禁仍保留，
但有效补充中的 `reported_observation` 可以来自首轮已经报告的异常，不要求再次复述。
