# ADR-0116：按能力入口实际执行的 gate 区分 role 与 access

| 状态 | 决策日期 |
|---|---|
| 已采纳 | 2026-08-23 |

## 当时遇到了什么

可配置权限系统可以把某项资格默认授予 SUPERUSER、管理员或其他身份，也可以再由 ACL 动态授予用户。
如果教学模型把“谁当前通常获得资格”反向展开成能力的角色要求，同一 gate 会同时生成 role 与 access，
甚至把可委派能力错误缩窄成“仅超级用户可用”。

## 决定

1. 字段分类只看能力入口实际执行的判断：直接比较当前调用者身份或角色时使用 `role`；查询可配置权限、
   ACL、名单或开放状态时使用 `access`。
2. 权限系统内部的默认授予、预分配、角色映射和动态 attach 只说明调用者如何取得 access，不展开成目标
   能力的 role alternative。只有能力入口布尔表达式本身直接包含角色分支时，才保存 `role OR access`。
3. 动态授权名单未知不等于 unresolved，也不关闭教学知识。默认开放时只说明资格可能被设置调整；默认关闭
   时只说明使用前需取得对应权限，不查询或固化当前 ACL，也不猜测授权者。
4. 该规则不识别第三方权限插件名称、权限键或数据库 Schema；模型仍从当前 Evidence 理解实际 gate。

## 为什么这样选

- role 与 access 获得唯一语义所有者，不会重复或互相缩窄；
- 权限内部授权关系变化时，目标能力教学合同仍然稳定；
- 不需要让教学 Agent 深入运行时数据库，也不需要为单个 Permission 插件编写解析器。

## 替代关系

- 收紧 [ADR-0113](../0113-separate-routing-authorization-and-business-readiness-in-teaching.md) 中 access 的所有权：
  access 由入口查询的可配置资格决定，不以“由哪个高权限主体控制”作为分类条件。
- 延续 [ADR-0108](0108-preserve-permission-disjunctions-in-teaching-requirements.md) 的单一 Permission OR 容器。

## 落实与确认

- 模型 Prompt 与 permission / constraint JSON Schema description 已写入同一通用不变量；
- Migut Help 继续只投影可无损归约的纯角色组合，不把 access 转换成管理员权限。
