# ADR-0052：把 Bug 定义到整个 Bot 软件责任链

| 状态 | 决策日期 |
|---|---|
| 已采纳；首个责任候选 schema 与评测已实现 | 2026-08-14 |

## 当时遇到了什么

[ADR-0050](0050-use-a-bounded-agent-for-user-bug-assessment.md) 已决定向普通用户返回 `bug`、`not_bug` 或
`unknown`，但没有固定这个 verdict 只判断目标插件，还是判断造成当前 Bot 行为的软件责任链。若提醒功能因
Adapter 或 NoneBot 缺陷失败，只判断目标插件会得到“不是 Bug”，这既不符合用户观察，也会让源码选择、历史
fingerprint 和评测 Gold 失去稳定含义。

## 决策

1. 用户可见 `bug` 表示：当前 Bot 支持范围内的实际行为由软件缺陷造成，不要求缺陷位于用户最先提到的插件。
2. 可归入 Bug 的责任组件包括目标插件、Bot 应用与集成胶水、NoneBot / 框架、Adapter / 协议实现、运行时或
   第三方依赖、部署组件或配置加载实现，以及参与该 Bot 能力的外部服务或客户端实现。只要证据支持软件缺陷，
   Adapter、依赖或部署集成问题也返回 `bug`，不能回答成笼统的 `not_bug`。
3. 内部候选使用可多选的 `responsibility_candidates`，至少能表达 `target_plugin`、`bot_application`、
   `framework`、`adapter`、`dependency`、`deployment_integration`、`external_service` 和 `unknown`。它服务后续
   维护者定位，不改变普通用户三值回答，也不能在证据不足时强行选一个 owner。
4. 用户输入不符合公开合同、公开角色或场景前提不满足、部署者有意选择的配置、已公开的限流，以及没有证据
   表明存在软件缺陷的临时外部服务不可用，不属于 Bug。若 Bot 对已声明的外部失败模式处理错误，错误处理本身
   仍可判为 Bug。
5. 无法区分软件缺陷与用法、配置选择或瞬时外部条件时返回 `unknown`，不得因为“和 Bot 有关”就无证据地把
   所有失败升级为 `bug`。
6. fingerprint、源码与设计检索可以从用户提到的 capability 出发扩展到实际责任链；评测必须覆盖目标插件、
   框架、Adapter、依赖、部署集成、外部服务、非 Bug 条件和责任不明，而不是只测插件自身缺陷。

## 为什么这样选

- 普通用户观察的是 Bot 能力是否按合同工作，不应被要求先知道缺陷属于哪个内部组件；
- 将 verdict 与责任候选分开，可以既给出正确的用户结论，又保留维护者后续路由所需的信息；
- 把预期配置、错误用法和瞬时外部条件排除在 Bug 之外，避免把“Bot 相关”误写成“所有异常都是软件缺陷”。

## 没有采用的方案

### 只判断目标插件自身是否有 Bug

这会让已被证实的 NoneBot、Adapter 或依赖缺陷对用户显示为 `not_bug`，除非额外引入“不是该插件的 Bug”这类
第四种语义，破坏首版三值合同。

### 只返回责任组件，不返回 Bug verdict

责任候选可能有多个或暂时不明，不能替代用户要求的“这是 Bug、不是 Bug、还是没判断出来”。

## 带来的影响

- Bug assessment 的候选与最终 decision 需要保存多选责任候选，但普通用户投影继续只显示三值结论和安全原因；
- 历史 verdict 的 fingerprint 必须包含适用责任链与 revision，不能只按用户文字或目标插件匹配；
- 全新 held-out 资格评测需要按责任组件分片报告，尤其验证 Adapter / 框架缺陷不会被错误判为 `not_bug`；
- 本决定不实施上报、owner 分派、Issue 创建或开发者审理。

## 落实与确认

- **已确认**：项目作者确认“只要是 Bot 责任链中的软件缺陷，都可以判为 Bug”，并接受内部另记责任候选；
- **已实现**：候选与最终 decision 已支持 `target_plugin`、`bot_application`、`framework`、`adapter`、
  `dependency`、`deployment_integration`、`external_service` 和 `unknown`；fingerprint 绑定 subject、adapter、
  source / contract / deployment revision。全新 16 条 held-out 对 10 条适用责任样本的责任候选准确率为
  1.000。
- **当前不变**：普通用户仍只看到三值结论，责任候选不会披露；Bug assessment 不创建 incident，也不执行
  owner 分派、Issue 创建或开发者审理。

## 关系

- 补充 [ADR-0050](0050-use-a-bounded-agent-for-user-bug-assessment.md) 未冻结的 Bug 责任范围；
- 不改变 [ADR-0040](0040-require-trusted-preflight-failure-before-incident.md) 的 incident 副作用门；
- 不把 [ADR-0046](0046-merge-internal-reasoning-into-behavior-exploration.md) 的 SUPERUSER 行为探索并入普通用户
  Bug 判定。

## 相关文档

- [支持入口分流](../architecture/flows/support-intake-routing.md)
