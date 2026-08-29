# ADR-0108：在教学 Requirement 中保留 Permission 析取关系

| 状态 | 决策日期 |
|---|---|
| 已采纳 | 2026-08-21 |

## 背景

NoneBot `Permission` 可以用 `|` 组合多个允许路径，例如“超级用户、私聊，或群管理员”。旧教学合同把
`role / scene / access` 保存为彼此独立的 requirement；消费者会把多条 requirement 理解为同时满足，模型也曾
把含 `SUPERUSER` 的混合表达式错误缩窄成 `superuser-only`。

## 决定

1. 一个可确定的 Permission 表达式投影为一个 `permission` requirement，内部 `alternatives` 表示 OR；每个
   alternative 只使用通用的 `role / scene / access` 语义。
2. 确定性框架语义识别 NoneBot `SUPERUSER`，以及 Uninfo `ADMIN / OWNER / MEMBER / PRIVATE / GROUP /
   GUILD`。`ADMIN()` 按 0.11.1 实现展开成 `admin OR owner`；公开角色还允许有直接 Evidence 支持的
   `channel_admin`，但不假设每个 Adapter 都产生 `CHANNEL_ADMINISTRATOR`。场景元数据使用
   `private / group / guild_or_channel`；它们描述允许分支，不额外生成隐含 AND。
3. 若同一 Permission 表达式仍含未知自定义分支，不把已知分支单独发布为 fixed requirement；整个表达式继续作为
   一个 gate candidate 交给模型闭合，避免把 OR 的一部分误当成必要条件。
4. `member.role.level > ...` 不能仅凭数值展开成固定角色集合；没有当前 Adapter 完整角色表时保留为
   `custom` 角色分支。只有源码明确使用 `ROLE_IN(...)` 等字面角色集合时，才可逐项展开。
5. Migut Help 只把单一 `SUPERUSER`，或恰好 `admin OR owner` 的管理员组合投影为原生权限字段。含 scene、
   `channel_admin`、`MEMBER` 或 `custom` 的混合 OR 不做有损权限投影，也不追加到最小 description；Answer
   仍可消费完整公开条件。
6. 教学注释 schema 升级并完全失效旧缓存，不提供旧 requirement 结构迁移。

## 影响

- 核心公开合同能准确表达跨角色和场景的“满足任一条件”；
- Migut Help 不再把混合 Permission 缩窄成错误的 `superuser` 或 `admin`；
- 未知分支继续 fail-closed，不会因局部静态识别而放宽或收紧权限；
- 不引入任意布尔表达式树；当前只表达 Permission 的 OR 备选，独立 requirement 仍表示彼此独立的条件。

## 替代关系

- 接续 [ADR-0094](../0094-simplify-the-public-capability-teaching-contract.md) 的通用公开 requirement，补充 Permission
  组合关系；不恢复插件专属字段或 `other` 逃生口。
- 保持 [ADR-0100](0100-keep-migut-help-descriptions-minimal.md) 的最小 description 边界。
