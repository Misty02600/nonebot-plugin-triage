# PLAN-0016：实现公开教学合同的 Bug 前置检查

| 状态 | 最后更新 |
|---|---|
| 已完成 | 2026-08-17 |

## 背景

[ADR-0066](../../adr/0066-use-active-teaching-contract-as-bug-precheck.md) 已确认：普通用户的疑似 Bug 在开放
聊天、运行、日志、源码、设计与部署工具前，必须先定位唯一的当前公开可教学能力，并确认存在具体观察；系统
实际服务的结构化教学注释作为第一层用法合同，明显误用应回到公开教学纠正，而不是启动正式 Bug Agent。

当前 runtime 在 subject 无法唯一定位时仍会构造不完整案件并启动 Agent；现有
`_PublicContractPrechecker` 也只预载公开合同，没有进行用法判断。本计划用于实现已经确认且当前能够安全落地
的最小纵切，不扩展仍在其他任务讨论的能力注册规则，也不提前决定持久化问题记录与重复报告聚合结构。

## 已确认范围

- Bug subject 只消费当前 `public teachable` ServingView 的结果；被动能力、动态入口和不确定命令是否注册由
  既有能力索引任务决定，本计划不修改该门禁。
- 缺少唯一 subject 或缺少具体观察时，第一次请求使用既有 scope Thread 的唯一补充机会；第二次仍不足则
  关闭，不调用 Bug Agent，也不建立问题记录。
- 只有结构化教学合同和精确操作锚点能够无歧义证明明显误用时，才短路为公开教学纠正；含糊情况不得归咎
  用户，继续正式调查或保持 unknown。
- 教学纠正复用普通 Guidance 的公开事实与回答链路，不向普通用户暴露配置键、群名单、Matcher、Rule、
  handler、源码、日志、Evidence ID 或内部责任候选。
- 当前不新增人工审核、待审、批准、驳回或 `auto / review` 配置；不接入尚未定型的问题数据库和重复报告
  聚合。

## 实施步骤与进度

| 步骤 | 状态 | 内容 |
|---:|---|---|
| 1 | 已完成 | 记录 ADR-0066，并核对当前 Bug runtime、Thread、Guidance 与教学注释接口。 |
| 2 | 已完成 | 增加模型外 Bug intake/readiness 结果；无唯一 subject 或无具体观察时零 Agent 工具调用；索引不可用不会错误追问用户。 |
| 3 | 已完成 | 把当前有效教学注释投影成第一层合同；仅对“精确 Reply 的本人操作消息缺少合同要求的 Reply 上下文”执行明显误用检查。 |
| 4 | 已完成 | Handler 复用一次补充；`teach_correction` 复用公开 Guidance，终局回复不披露底层证据；Uninfo 成员查询改为聊天工具实际使用时惰性执行。 |
| 5 | 已完成 | 相关领域、Agent 适配、runtime、会话与 Guidance 共 49 条测试通过；Handler/Thread 集成 34 条通过；全树 Ruff lint、BasedPyright 与 diff check 通过。 |
| 6 | 已完成 | ADR、overview 与流程文档已同步；并行知识包改动完成后，全树质量门恢复并通过，计划归档。 |

## 当前验证结果

- `uv run pytest -q`：1,273 passed、1 skipped；跳过项为当前 Windows 环境无法创建测试所需 symlink。
- `uv run ruff check src tests tools` 与 `uv run ruff format --check src tests tools`：通过。
- `uv run basedpyright`：0 errors / 0 warnings。
- `uv lock --check`、`git diff --check`：通过。
- `uv build`：成功生成 wheel 与 sdist。

## 完成标准与验证

| 验收项 | 预期结果 | 验证方式 |
|---|---|---|
| subject readiness | 未定位或存在歧义时返回需要补充，Agent 与证据工具调用数均为 0 | runtime 单测 |
| observation readiness | 只有抽象“提交 Bug”而无具体观察时返回需要补充；真实观察可继续 | handler/runtime 测试 |
| 精确误用 | 用户 Reply 的本人操作消息与当前合同存在可机器验证的调用形式冲突时，返回教学纠正 | 合同检查单测与集成测试 |
| 含糊失败闭合 | 没有精确操作锚点、合同缺失或 stale 时不短路为用户错误 | runtime 单测 |
| 普通用户披露 | 教学纠正只使用公开 Guidance 事实，不出现内部配置、源码或 Evidence 信息 | 集成测试 |
| 一次补充 | 首轮不足等待一次；第二轮仍不足关闭且无 Agent 副作用 | Thread 集成测试 |
| 质量门 | 相关测试、Ruff、BasedPyright、`git diff --check` 通过 | 本地命令 |

## 非目标

- 不重新决定哪些被动、动态或不确定 Matcher 应进入能力索引。
- 不实现任意聊天历史回退、跨平台历史模拟或新的 Thread/Waiter 生命周期。
- 不修改 Bug Agent Prompt、资格组合、工具预算或 held-out 评测合同。
- 不设计或接通确认 Bug、unknown、重复报告的长期持久化模型。

## 相关文档

- [ADR-0066：用当前公开教学合同前置筛查普通用户 Bug](../../adr/0066-use-active-teaching-contract-as-bug-precheck.md)
- [支持入口、Thread、Guidance 与 Bug 判定](../../architecture/flows/support-intake-routing.md)
- [项目架构概览](../../architecture/overview.md)
