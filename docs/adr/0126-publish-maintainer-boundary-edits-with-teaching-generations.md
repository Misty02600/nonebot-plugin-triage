# ADR-0126：将维护者边界修订与教学 generation 一起发布

| 状态 | 决策日期 |
|---|---|
| 已采纳 | 2026-09-13 |

## 背景与决定

维护者需要微调已经生成的行为边界。只改 Markdown 无法影响核心教学合同；只改可删除缓存又会丢失人工劳动。
采用已有不可变 generation 与唯一活动指针，把结构化注释及本次编辑记录纳入同一次发布，不新增覆盖数据库。

- 维护命令仅 SUPERUSER 可用，只精确替换一个 entry 已有的原始边界；绑定当前 generation 和完整旧文字。
- 编辑前核对选中单元的入口、源码、配置与 Evidence 当前性。自动派生边界、usage、权限和场景不开放修改。
- 编辑不调用模型；结构合法不等于语义正确，维护者负责文字事实。生效后可被 Answer 和教学预检使用。
- 原始模型诊断不改写。编辑记录保存操作者、时间、来源版本及前后文字，随本地 generation 持久化，不向公开帮助展示。
- 人工文字进入已有非证据 baseline 合同：后续模型可依据当前 Evidence 明确替换或删除，不建立永久覆盖或审核状态机。
- 发布快照可以恢复已发布原始注释；缓存继续承担可删除加速和未发布 checkpoint，不成为人工修改的唯一副本。

## 取舍与边界

不采用“每次微调都重新请求模型”，避免把局部措辞修订变成付费生成；也不采用永久人工覆盖，以免源码改变后
继续强制保留失效事实。代价是 generation 多保存一份结构化注释，人工文字错误仍可能影响 Answer 的解释。
本地数据备份必须保留该文件；包含操作者及内部身份的编辑材料不能直接作为公开 Help 数据分享。

发布、取消收尾和缓存恢复复用现有锁、原子指针和原生缓存类型。旧 generation 仍可读取，但没有结构化材料时
须先完成正常刷新才可编辑。已有源码、请求指纹与 Evidence 失效规则不放松。

## 落实与确认

- [教学服务](../../src/nonebot_plugin_triage/capability/teaching/annotations.py) 暂存精确替换；
  [发布器](../../src/nonebot_plugin_triage/capability/teaching/outputs.py) 负责结构化持久化和恢复。
- [生命周期回归](../../tests/capability/test_capability_annotations.py) 覆盖版本拒绝、发布失败、取消收尾、
  缓存丢失恢复及重生成保留 / 替换；入口权限与其他插件保留复用对应测试文件。
- 用户确认采用此范围；未引入人工审批、永久锁定或语义自动审核。

## 相关决定

- 补充 [ADR-0093](0093-shard-capability-annotation-cache-by-plugin.md)：缓存仍可删除，人工修订由 generation 保管；
  其中仅靠重算恢复已发布结构化内容的安排由本决定局部替代，原子发布边界不变。
- 保留 [ADR-0121](0121-checkpoint-completed-teaching-units-before-atomic-publication.md) 的未发布 checkpoint 复用。
- 当前行为见 [能力影子索引流程](../architecture/flows/capability-shadow-index.md)。
