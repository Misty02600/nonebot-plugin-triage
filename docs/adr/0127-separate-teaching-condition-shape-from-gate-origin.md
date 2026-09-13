# ADR-0127：按语义选择教学条件结构，不按 gate 来源分类

| 状态 | 决策日期 |
|---|---|
| 已采纳 | 2026-09-13 |

## 背景与决定

NoneBot 的 Permission、Rule 和 Handler 都能承载身份、场景或其他检查。教学合同若要求 Permission 来源
必须包装成 `permission`，会让同一业务含义出现两套分类，也诱导模型把场景条件误当成授权。

教学语义容器改名为 `condition_group`，分支统一使用 `alternatives`。`role / scene / access / rate_limit`
按实际含义使用，不受源码注册方式限制；真实 NoneBot Permission、Runtime gate kind 和 Migut Help 的
原生 `permission` 字段不改名。

- 简单条件优先使用独立类型；确定性固定事实仅在去重后恰好一项且没有共同条件时直接输出原子条件。
- 顶层 requirements 为 AND；条件组的非空 alternatives 为 OR，组内 allowed_scenes 为共同的 AND 条件。
- 保留单分支组，不强求最大化简，不拆同一 gate，不跨 gate 合并，也不生成递归布尔树。
- 一个 gate 在每个受影响 entry 中仍须有唯一公开归属（同组多条 usage 例外保留）；Evidence、完整关联和
  unresolved 关闭规则不变。业务准备状态仍归 behavior_boundary。
- Answer 保留已有条件文字；不新增场景去重或重新概括。Migut Help 仅投影能表达的角色形状，不将
  独立 admin 扩大成 admin OR owner；混合组、共同场景或额外独立角色不压成原生权限标签。

## 取舍与兼容

不再强迫所有条件包装成组，也不通过自然语言推断缺失结构。代价是合同升版，需要重新生成或验证；
改名本身不保证模型更少思考或不再漏条件。

旧 Schema 注释不自动迁移成有效新结果。历史 generation 与人工文案不删除；旧格式结构不能恢复为当前
缓存或继续编辑，升级后先完成正常全范围刷新。新格式仍沿用 generation 恢复人工修订。没有为此次改名
新增迁移系统或永久人工覆盖。

## 落实与确认

- [领域条件](../../src/nbtriage/capability/teaching/analysis.py)、
  [生成适配](../../src/nbtriage/capability/teaching/model_adapter.py) 与固定事实共用上述边界。
- [模型适配回归](../../tests/capability/test_capability_model_adapter.py) 覆盖 Permission 来源的独立条件、
  组、gate 覆盖及关闭规则；Help 和发布回归覆盖披露、保守投影与旧格式拒绝、新格式恢复。
- 本地验证不替代真实模型评测；新 Prompt 的语义质量、推理量与费用需另行验证。

## 相关决定

- 局部替代 [ADR-0094](0094-simplify-the-public-capability-teaching-contract.md) 的教学条件名称，以及
  [ADR-0113](0113-separate-routing-authorization-and-business-readiness-in-teaching.md) 中 Permission
  来源强制使用 permission requirement 的规定；两者其余字段所有权保持不变。
- 保留 [ADR-0124](0124-express-non-private-teaching-scenes-directly.md) 的场景谓词与共同条件语义、
  [ADR-0126](0126-publish-maintainer-boundary-edits-with-teaching-generations.md) 的人工修订发布边界。
