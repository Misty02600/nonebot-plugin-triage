# PLAN-0020：在教学中确定性保留 Alconna 可变参数数量上限

| 状态 | 最后更新 |
|---|---|
| 已完成 | 2026-09-11 |

## 背景

用户要求确定方案后明确授权实施，现已完成工作树修改与验证。目标是在原生 MultiVar 对位置参数设置有限数量时，即使模型未主动查定义或未写出
限制，完整教学仍明确告知上限。公开教学继续使用已有 usages 和 behavior_boundaries；不新增公开字段。

本地核查版本为 arclet-alconna 1.8.44、nonebot-plugin-alconna 0.62.1，不代表已经验证其他版本。
获授权合成诊断中，`Args(Arg("items", MultiVar(str, 3)))` 经一次有效模型请求成功发布
`pack <内容>...`，没有证据工具调用，也没有数量限制。初始证据未包含完整注册行，因此这个结果不能归因于
模型看到构造行却误解参数。原生 parser 对同样参数的命令接受 1、3 项，拒绝 0、4 项。

遵循 [ADR-0094](../../adr/0094-simplify-the-public-capability-teaching-contract.md) 的字段所有权：
usage 已表达必填、可选与重复性，新增说明只补它没有表达的有限上限，不重复推导最少项数。
这属于已有教学合同下的正确性补全，不新建 ADR。

## 当前设计与缺陷

1. `discovery/snapshot.py::_alconna_arguments` 保留 variadic、flag、required、keyword 等信息，
   没有保留原生 `MultiVar.length`。根与组件参数共用这条快照路径。
2. 原生 `typing.py::MultiVar` 将字符串 `+` / `*` 的 length 设为 -1；大于 1 的整数成为有限上限；
   其他整数最终归一为 length=1。读取运行对象即可，不需要重新解释构造表达式或环境配置。
3. `teaching/_projection.py::_structured_usage` 按统一 slot_indexes 生成根与 Option 槽位；
   `_render_arguments` 把可变参数统一写成 `...`，有限与无限形式在当前模板中没有区别。
4. 核心 `annotations.py::validate_capability_usage_template` 已得到匿名槽位到公开名称的唯一对齐结果，
   但只返回规范化 usage。它应成为数量说明复用参数名称的依据，不能另建一套槽位正则。
5. 教学服务把模型注释保存到缓存和 active view，`_analysis_baseline` 会沿用 behavior_boundaries。
   若直接把代码生成的限制混入这份基线，后续上限变化可能留下过期措辞。
6. `teaching/outputs.py::_render_answer_markdown` 展示 behavior_boundaries；Help 是已有的有损简表，
   不展示该字段。因此“完整教学包含限制”不等于“本次 Help 简表也新增一行”。

## 技术路线

### 生效事实与入口绑定

- 在内部 AlconnaArgument 快照增加 `variadic_length`：原生 MultiVar 保留整数 length，-1 表示已确认无限，
  正整数表示有限；未知为 None。排除 bool、0 和其他非法值，不能把未知当成无限。普通非可变参数不要求此值。
- 继续使用既有关键字参数识别边界；KeyWordVar、MultiVar(KeyWordVar) 不因本次改动获得支持。
- 模板生成时，在分配 slot ID 的同一次遍历中收集有限上限，绑定到 entry ID、模板和 slot ID。
  内部采用框架无关的槽位上限事实，核心不导入 Alconna；不复制 MultiVar 的解析实现。
- 该事实进入 request fingerprint 和 family shape。旧可变参数快照缺少字段时要求重新采集；旧缓存不能
  作为当前完整结果复用。实施时提升 request revision；若调整模型可见请求说明，同步提升 Prompt revision，
  公开 Schema 保持不变。

### 确定性说明与缓存所有权

- 从已验证的完整标准 usage 提取唯一槽位名称；保留现有 validator 的公开返回契约，把内部对齐结果供共同
  helper 复用。别名、mention 和回复变体不能成为第二套命名来源。
- 生成形如 `用法「pack <内容>...」中，显式填写“内容”时最多提供 3 项。` 的固定说明，归入
  behavior_boundaries。说明带标准用法上下文，避免同名参数或多个入口混淆；不推导默认值的项数。
- 根、Subcommand 和 Option 中已有明确标准槽位的普通位置 MultiVar 使用同一路径。Option 的上限描述
  限定为每次填写该 Option 的该参数，不声称是重复调用 Option 后的累计上限。
- 模型注释缓存、pending / last-good 和编辑基线保留模型原始投影。代码生成的数量说明在本轮准备完成后，
  统一构造供 get / get_pending 返回的教学视图；active view 内用于复用和基线的原始注释保持独立。
  这份视图由当前已验证的原始注释和本轮槽位事实派生，不新增持久缓存或第二个发布存储。
- 在候选视图构造时完成合并与公共长度/成员数校验，不能把异常推迟到消费者调用 get 时。
  新生成、缓存命中、合法 fallback 和发布路径均使用同一组装函数；发布失败保持既有原子切换语义。
- 固定说明每轮重建，只做确定文本的去重。模型原有语义说明仍走现有 Evidence 和输出校验；
  本次不承诺识别任意自然语言中的数字矛盾，也不新增通用语义冲突检测器。

### 支持和失败边界

- 有限上限无法唯一绑定公开槽位时，在准备阶段停止对应教学单元，沿用 unsupported syntax 原因类别。
  例如有限参数被隐藏、Option 被压缩为 `[可选参数]` 或 family 聚合丢失具体槽位；不能静默丢掉限制。
- 已支持的分隔符、别名和常规 requires 路径保持现有规则；本次不扩大 compact、关键字参数或动态包装支持。
- family 仍不可拆分：不同成员上限或结构不能当成同一共同上限；无法准确保留时拒绝该 family，不能只发布
  支持的成员。独立普通教学单元不受牵连。
- 无上限形式不额外生成说明。默认值或可选参数仍保留既有 optional usage；说明只约束显式填写的项目。
- 核心完整 entry 和 Answer knowledge 保留限制；Help 简表维持现有有损投影，不扩展 description 或语法记号。
- 不修改 Provider 接入，不强制模型逐个查定义，不改变初始注册源码供给策略，不运行目标插件 Handler。

## 完成标准与验证

| 验收条件 | 验证方式与预期 |
|---|---|
| 模型完全遗漏上限仍正确发布 | 复用 fake-client 教学刷新与输出用例：模型只返回 `pack <内容>...`，get/get_pending 与 Answer 仍有最多 3 项说明；原始缓存与 baseline 没有该代码说明。 |
| 原生语义一致 | 合成 parser 覆盖 length=1、3，0/1/3/4 项边界，以及 `+`、`*` 无限形式；不执行 Handler。 |
| 可选/默认参数不误教必填 | 复用参数测试，验证可省略输入及显式提供 N / N+1 项；说明不写“至少 1 项”，不限制默认值集合长度。 |
| 入口与参数关联准确 | 根、Subcommand、Option、同名参数以及不同分隔符中，说明绑定正确标准 usage 和槽位；Option 重复使用按每次填写界定。 |
| 信息缺失不静默发布 | 有限隐藏参数、被压缩 Option、未知 length 和不能精确保留的 family 在准备阶段跳过；不调用模型，不以旧结果恢复当前错误知识。 |
| 缓存和基线不会残留旧值 | 上限 3→5→无限分别改变指纹和当前说明；热缓存与冷加载一致；改后失败不能回退到旧上限；旧可变参数事实要求重新采集。 |
| 展示和发布合同保持 | Answer 展示边界，Help 保持既定简表；成员数超限正常拒绝，发布失败不激活半份新视图。 |

优先扩展现有 `tests/capability/test_alconna_separators.py`、`test_capability_analysis_adapter.py`、
`test_capability_annotations.py`、`test_capability_annotation_cache.py` 和 `test_capability_teaching_outputs.py`，
按风险复用夹具，不为每个直观转换另建测试。运行相关 pytest、Ruff 与涉及模块的类型检查；区分已有告警。
上述离线回归足以验收确定性保证，不以追加真实模型请求或模型主动阅读成功为完成条件。

实施只准备和验证工作树，未暂存、提交或推送。architecture 和本地学习记录已同步，计划按仓库约定归档。

## 实施结果与自审

- 快照、槽位绑定、上限投影及派生视图已实现。request v75，Prompt v110 / Schema v12 不变；
  上限映射用于内部指纹与投影，不增加模型输出字段或定向查定义提示。
- 复用模板对齐得到公开名称；别名表达式先按既有 selector 规则验证，再匹配标准模板。同名公开参数按
  在用法中的出现顺序区分。回复或 shortcut 不能替代有限参数的完整标准槽位。
- 模型原始注释仍用于缓存、checkpoint 和 baseline，派生视图用于 get/get_pending 与发布。冷加载重建
  上限说明，3→5→无限不残留旧代码文字；改变指纹后的失败不能恢复旧上限，同指纹合法 fallback 保留限制。
- 有限默认参数可省略，即使默认集合含 4 项而显式上限为 3，也不能把默认结果误判为非法。已用原生 parser
  对照这一边界；代码说明仅表达每次显式填写的上限。候选数量说明与已有模型边界统一规范排序、去重，
  超出公开成员数上限时正常拒绝候选，不在 getter 中延迟抛错。
- 12 个相关测试文件联合通过：371 passed，59 个既有依赖告警，117.46 秒。Ruff check/format 与 diff
  空白检查通过；核心 annotations 与 projection 的类型检查通过。扩大类型检查仍有工作树既有的
  config-value narrowing 2 项、刷新状态 Optional narrowing 6 项与 snapshot redundant-cast 2 项，
  不宣称全仓类型检查通过；前两项在 HEAD 源码副本中也可复现。未修改这些无关逻辑。
- 本次没有追加真实模型请求，也没有改变 Help 简表、Provider 接入或其他 Alconna 配置支持。
