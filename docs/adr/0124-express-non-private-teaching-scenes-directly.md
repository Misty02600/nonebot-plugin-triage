# ADR-0124：直接表达非私聊教学场景

| 状态 | 决策日期 |
|---|---|
| 已采纳 | 2026-09-11 |

## 背景与决定

源码有时只证明排除私聊。强迫模型展开允许场景全集会引入 Adapter 推测、额外导航或仅填写群聊的缩窄。
作者确认用一个常见场景谓词解决此缺口，不建设通用排除集合或权限表达式系统。

- `TeachingScene` 增加 `non_private`，意为非私聊，不是与六种原子场景互斥的第七种类型。
- 独立 scene 的 `allowed_scenes`、Permission 的共同 `allowed_scenes` 和 scene alternative 均可保存它。
  集合内 OR、共同条件与 alternatives 为 AND、独立 requirements 为 AND 的关系不变。
- 只能根据 Evidence 生成；独立依赖或其他 gate 的更窄限制仍须保留，局部排除不能提升为全局限制。
- 不把其他角色或授权条件扩展成否定枚举，不新增 `excluded_scenes` 或自动场景推断校验器。
- Answer 保留条件说明，Migut Help 不新增场景投影或 description 附加文字。实际鉴权仍由目标插件执行。

## 代价与边界

仅增加一个有直接源码依据的常见谓词，避免通用正反集合的维护成本；代价是场景值不再全部为原子，
未来做场景匹配时必须按谓词解释，不能只比较字符串或把未知场景当作非私聊。
该表达扩展不保证模型每次都能正确理解所有执行路径；旧的语义评测不自动升级为通过。

## 替代关系与验证

- 部分替代 [ADR-0119](history/0119-store-direct-scene-requirements-as-atomic-sets.md) 第 1–3 项的
  “仅允许原子场景、禁止 non_private”边界；原子场景、集合结构和其他决定保留。
- Schema 13 / Prompt v112 / request v79，旧 schema 缓存不迁移。
- 复用现有模型适配、持久化、Help / Answer 和 Prompt 测试覆盖新值；不添加真实插件专属示例。
- 当前消费者与布尔关系详见[能力影子索引流程](../architecture/flows/capability-shadow-index.md)。
