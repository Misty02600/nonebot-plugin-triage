# ADR-0119：用原子场景集合保存直接场景要求

| 状态 | 决策日期 |
|---|---|
| 已采纳 | 2026-08-23 |

## 当时遇到了什么

教学合同原本只允许一个 `scene` 值，并把场景压缩成 `private / group / guild_or_channel`。真实插件的
`ensure_group` 明确允许群聊、频道和频道文字三种场景；模型即使正确读懂源码，也只能漏填结构字段，或把完整
文字错误地配成单一 `group`。Uninfo 自身则稳定区分六种 `SceneType`，项目自造的组合值既不能表达任意允许集合，
也会让结构元数据比公开文字更窄。

## 决定

1. 核心 `TeachingScene` 使用六个与 Uninfo 语义对应、但不依赖其运行时类型的原子值：`private / group /
   guild / channel_text / channel_category / channel_voice`。
2. 非 Permission 的直接 scene requirement 使用非空 `allowed_scenes` 集合，完整保存执行逻辑允许的原子场景；
   不新增 `non_private`、`guild_or_channel` 等组合枚举。
3. 同一个 NoneBot Permission 的 alternatives 继续表示 OR。每个 scene alternative 只保存一个原子场景；Uninfo
   `GUILD` 这类稳定复合 Permission 由模型外展开为 `guild` 和三种 channel alternative。
4. `platform_scope` 继续由 Runtime 记录拥有，不进入 scene requirement。Migut Help 暂不投影 scene；Answer 仍使用
   requirement 文字与结构集合。
5. Handler 源码 Evidence 保留其装饰器，使 `parameterless=[Depends(provider)]` 与已预载 provider 定义的执行
   关系对模型可见，而不是只提供一个失去归属的 helper。
6. 公开 schema 更新到 10，Prompt 更新到 v71，请求更新到 v35。旧缓存完全失效，不迁移旧单值 scene。

## 为什么这样选

- 集合直接表达源码允许范围，不需要为每一种组合扩充枚举；
- 原子值与 Uninfo 已有场景模型一致，模型和消费者都能稳定理解；
- 直接 scene 条件与 Permission OR 的布尔所有权保持分离，不把多个独立条件误读成 AND；
- Help 当前没有 scene 投影需求，不为这次修正扩大其最小展示合同。

## 没有采用的方案

### 增加 `non_private` 等组合值

每发现一种组合就新增一个枚举会持续膨胀，而且组合值之间容易重叠，不能形成稳定的原子语义。

### 只保留自然语言 statement

这会让 Answer 看似正确，却使结构消费者无法可靠过滤或比较场景，也无法发现 statement 与元数据不一致。

### 把直接场景集合拆成多个独立 requirement

多个 requirement 的默认关系是同时满足；把允许场景拆开会把 OR 错误表示成 AND。

## 带来的影响

- 模型可以把 `group + guild + channel_text` 一次完整提交，不再被迫选择最接近的单值；
- 固定 Uninfo `GUILD` Permission 的公开 alternatives 会增加为四个原子分支，但语义不变；
- schema 9 的本地教学缓存不再读取；当前模型合同需要新的 held-out，不能继承历史资格。

## 替代关系

- 替代 [ADR-0113](../0113-separate-routing-authorization-and-business-readiness-in-teaching.md) 第 6 项中的单值
  scene 元数据；其 platform、access 与业务准备状态边界继续有效。
- 细化 [ADR-0108](0108-preserve-permission-disjunctions-in-teaching-requirements.md) 的 scene alternative，明确
  alternative 只保存原子场景，稳定复合 Permission 由模型外展开。

## 相关文档

- [能力影子索引流程](../../architecture/flows/capability-shadow-index.md)
- [模型与 Provider 支持矩阵](../../architecture/model-provider-support.md)
