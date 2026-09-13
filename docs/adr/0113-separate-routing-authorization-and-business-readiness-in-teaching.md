# ADR-0113：分离教学中的平台路由、使用资格与业务准备状态

| 状态 | 决策日期 |
|---|---|
| 已采纳；Permission 来源强制条件组的规定被 [ADR-0127](0127-separate-teaching-condition-shape-from-gate-origin.md) 局部替代 | 2026-08-22 |

## 当时遇到了什么

真实插件教学生成把三类不同事实混进了 `access`：

- 插件只支持某个 Adapter 的 `platform_scope`；
- 当前用户、群或场景是否已经取得受高权限主体控制的使用资格；
- 能力所需的业务数据是否已经准备好，例如是否已经绑定个人课表。

这些文字本身可能有源码依据，但混用会让消费者得出错误结论。平台路由不是用户授权；业务数据已准备也不等于
获得管理员授权。另一个真实问题是，当前 Handler 的错误提示提到了另一条命令和旧导入方式，模型便把这段提示
升级成了目标命令的当前详细用法，尽管本轮没有目标命令的 Runtime 或实现 Evidence。

## 决定

1. `platform_scope` 继续由 `CapabilityRecord` 确定性保存和执行路由过滤，不进入教学模型的 Runtime Evidence，
   也不进入公开教学注释。Help、Answer 或其他消费者需要平台信息时，直接读取当前记录，不能要求模型复述。
2. `role` 只表达当前调用者本人必须具备的身份，例如 `superuser / admin / owner`。
3. `access` 只表达当前用户、群或场景必须已取得的使用资格。该资格可以由 SUPERUSER 或其他高权限主体通过
   白名单、授权或开放范围控制，但调用者本人不必是授权者。公开文字只保存脱敏效果，不保存名单、ID、配置键，
   也不判断当前主体是否实际命中名单。只有 Evidence 明确证明授权者角色时才公开称呼该角色；单纯部署配置只写
   “需授权”或“需已开放”，不猜测由群管理员、管理员、超级用户还是维护者开启。
4. 能力所需的业务数据、初始化状态或其他业务准备条件属于 `behavior_boundary`，不属于 `access`。
5. 当前 Handler 中指向另一条命令的提示文字，只能证明当前 Handler 会给出该提示。除非本轮同时拥有目标命令
   当前的 Runtime 调用事实或实现 Evidence，不得据此生成目标命令的详细用法、参数或导入方式；最多保留当前
   Handler 直接证明的通用业务前提。
6. `scene` 独立 requirement 必须像 Permission alternative 一样携带
   `private / group / guild_or_channel` 结构元数据；不能只保存自然语言，也不能让 Prompt 要求该字段而输出
   Schema 拒绝它。
7. 同一注册表达式中的多个未解析 Permission 符号属于同一个执行控制点，合并为一个 gate candidate；模型用
   一条 `permission` requirement 的 OR alternatives 完整解释，不能猜测多个候选分别属于哪个 Handler 条件。
8. 不新增 requirement kind、平台字段、关键词黑名单或插件专属解析器。公开 schema 更新到 9；Prompt 更新到
   v59，请求更新到 v25。真实限额可以公开有 Evidence 支持的豁免对象，不能用“包含不受限制”等短语扫描把它
   误判成整体无约束。旧缓存完全失效，不读取迁移成当前结果。

## 为什么这样选

- 平台范围已有确定性所有者，再交给模型只会产生重复和错分；
- `role` 与 `access` 分离后，可以准确表达“普通用户可用，但需先由管理员授权”；
- 业务准备状态对 Answer 有价值，但映射成“需授权”会误导 Help、Bug 预检和后续消费者；
- 跨命令提示可以保留为当前命令失败原因的证据，却不能代替目标命令当前合同；
- 收紧字段所有权和模型输入即可解决问题，不需要维护脆弱的业务词表或第二套权限模型。

## 没有采用的方案

### 在教学注释中新增 platform requirement

平台适配是部署与路由事实，不是用户可满足的使用前提。新增字段会复制 `CapabilityRecord.platform_scope`，并产生
两份可能漂移的真值。

### 把所有“先绑定、导入、初始化”写成禁止词

这些动作在其他能力中可能确实是授权流程或当前命令用法。按词语拒绝既会过拟合，也会重现动态公开文本黑名单
的问题；本决定只界定事实所有权和 Evidence 充分性。

### 为课表插件识别特定命令

规则只依赖“当前 Handler 是否拥有目标命令的当前 Evidence”，适用于任何过期错误提示或跨命令帮助，不绑定
插件名、命令名或文件格式。

## 带来的影响

- 有利：平台限制不再被 Answer 误写成 access 或 scene；
- 有利：白名单授权与业务准备状态拥有不同、稳定的公开语义；
- 有利：独立场景条件和复合 Permission 都有无歧义的结构所有者；
- 有利：旧错误提示不能再单独证明另一条命令的详细能力；
- 代价：消费者若要展示平台范围，必须把教学注释与当前 `CapabilityRecord` 组合，而不能只读注释文件；
- 代价：schema 9 / Prompt v59 / request v25 需要重新进行模型质量评测，旧 held-out 资格不能继承。

## 替代关系

- `role / access` 的分类条件已由
  [ADR-0116](history/0116-classify-role-and-access-by-the-executed-gate.md) 收紧为“入口直接身份判断 / 可配置资格查询”；
  本 ADR 的 platform routing 与业务准备状态边界继续有效。

- 部分替代 [ADR-0094](0094-simplify-the-public-capability-teaching-contract.md) 第 3、6 项中对 `access` 与
  `behavior_boundary` 的所有权定义；公开字段集合、脱敏边界和 Help / Answer 分层继续有效。
- 延续 [ADR-0032](0032-separate-capability-audience-analysis-and-platform-status.md) 对平台范围的确定性所有权。
- 延续 [ADR-0112](history/0112-do-not-blacklist-dynamic-source-symbols-in-public-teaching-text.md)，不恢复动态关键词黑名单。

## 相关文档

- [能力影子索引流程](../architecture/flows/capability-shadow-index.md)
- [架构总览](../architecture/overview.md)
- [模型与 Provider 支持矩阵](../architecture/model-provider-support.md)
