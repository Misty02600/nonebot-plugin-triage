# PLAN-0019：按生效的 Alconna 分隔规则生成教学用法

| 状态 | 最后更新 |
|---|---|
| 已完成 | 2026-09-10 |

## 背景

用户已认可：NoneBot / Alconna 负责解析环境变量与局部覆盖，Triage 读取最终运行时对象，由代码确定
分隔符和用法结构；模型依据 Evidence 命名匿名槽位、解释功能，不推导配置优先级或决定分隔符。
用户随后明确授权实施；已完成工作树修改、相关回归、自审与文档同步。

本计划只补充分隔符对现有教学合同的影响。前缀、compact 与本次作为组合验证条件，不借此重新设计
前缀展示政策；不扩展到所有 Alconna 配置、Extension 语义、插值或任意引用与转义系统。
保持现有公开字段、模型完整 usage 输出、Evidence 约束、权限规则及不可拆分的 family 分析单元。

核查基线为本地安装的 `nonebot-plugin-alconna 0.62.1`、`arclet-alconna 1.8.44`。
`pyproject.toml` 的 Alconna 插件约束是 `>=0.62,<0.63`，不能把一次精确版本验证表述成整个范围均已验证。
工作树已有导航和回复用法等未提交改动；后续实现必须基于届时工作树重新核对交叉位置，保留既有修改。

## 实施前设计与缺陷

### 数据流与已有决定

1. `src/nonebot_plugin_triage/capability/discovery/snapshot.py` 的 `_alconna_candidate` 读取已注册命令，
   但把 `separators` 写成空元组；`AlconnaArgument` 和 `AlconnaComponent` 尚未保留各层分隔符。
   `_core_record` 会省略空列表，因而没有生成对应的 Alconna 根分隔符事实。
2. `src/nonebot_plugin_triage/capability/teaching/_projection.py` 的 `_command_path_body`、
   `_structured_usage`、`_render_options` 主要按空格 / compact 渲染。`_family_parser_shape`
   保存参数、组件、compact 与模板，未显式包含根分隔符；`_family_syntax_fidelity` 对 Alconna
   无条件给出 `parser_exact`。
3. `src/nbtriage/capability/teaching/annotations.py::validate_capability_usage_template`
   已经固定模板字面量、允许模型命名槽位；无需再建第二套槽位命名协议。
   但 `_usage_pattern` 会归一化空白；回复槽位省略使用 `literal.removesuffix(" ")`；重复槽位后的
   标点还受 `validate_capability_usage_pattern` 中的空白边界规则限制。
4. `src/nbtriage/capability/teaching/usage.py::usage_command_body_pattern` 只识别结尾、空白或槽位开头。
   模型校验与公开投影中的 alias 替换、`@bot` 检查等共同消费它，不能只修渲染器。
5. `src/nbtriage/capability/teaching/annotations.py::capability_analysis_fingerprint` 已包含
   canonical usages 和 Evidence revision；刷新器还区分当前指纹、编辑基线、last-good 和发布代次。
   需要扩展输入事实并验证失效路径，不另建缓存。

相关约束来自 [ADR-0094](../../adr/0094-simplify-the-public-capability-teaching-contract.md)、
[Parser 模板所有权的历史决定](../../adr/history/0081-close-unknown-teaching-gates-and-freeze-parser-owned-usages.md)
和 [Runtime aliases 的历史决定](../../adr/history/0087-validate-and-factor-runtime-command-aliases-for-teaching-usages.md)。
本计划是已有所有权下的正确性补全，不新建 ADR。

### 已确认的上游语义

- 当前版本的节点和 Arg 最终 `separators` 是字符串，表达可用分隔字符；不能把 `",;"` 当成必须连续输入的
  两字符分隔串。需要保留各层实际值，不能把父节点设置递归覆盖给所有子节点。
- NoneBot 插件 `AlconnaRule.__init__` 在有效 `use_cmd_sep` 开启时修改根 `command.separators`，
  不会同步修改每一个 `Arg.separators`。
- 根命令头后的分隔、子命令 / Option 自己的分隔、参数之间的分隔分别由上游读取流程处理。
  普通位置参数通过 `analyse_args` 按当前 Arg 的分隔符消费文本；不能一律使用“后一个参数”的分隔符。
- 空值必须区分来源：没有采集到字段不等于空格；已观察到的空 Arg 分隔符在 `Argv.next` 中回退到根设置；
  Option 构造时的空分隔符可能已经被归一化为 `compact=True` 和空格。读取生效对象及其已核实的规则。
- 原生 formatter 提供完整帮助展示，包括分隔符说明、默认值、提示等，不能直接作为当前匿名槽位模板。
  复用原生字段和行为作为依据；不调用目标命令的 formatter / parser / executor 来探测语法。

上游证据：[Alconna 1.8.44 节点源码](https://github.com/ArcletProject/Alconna/blob/v1.8.44/src/arclet/alconna/base.py)、
[NoneBot Alconna 配置说明](https://nonebot.dev/docs/next/best-practice/alconna/config)。
同时核对本地 `args.py`、`core.py`、`_internal/_argv.py`、`_internal/_handlers.py`、`formatter.py`
及 `nonebot_plugin_alconna/rule.py`。

### 本次最小复现

通过 `uv run --no-sync python -` 在独立进程中构造无业务回调的合成命令，调用原生 parser，结束时删除
本次创建的注册项。没有加载目标插件业务或读取真实环境变量。

| 条件 | 输入 / 检查 | 观察结果 |
|---|---|---|
| 根分隔符 `,`，两个 Arg 保持空格 | `probe,Beijing tomorrow` / `probe,Beijing,tomorrow` | 前者匹配并得到两个参数；后者失败 |
| 根及两个 Arg 都为 `,` | `probe,Beijing,tomorrow` / `probe,Beijing tomorrow` | 前者匹配；后者失败 |
| 根及 Arg 为 `,;` | `probe;Beijing,tomorrow` | 匹配，说明可以分别使用集合中的字符 |
| 根为 `;`，第一个 Arg 为 `,`，第二个为 `\|` | `probe;A,B` / `probe;A\|B` | 前者匹配；后者失败 |
| 根为空格，Option 分隔符 `=` | `probe --num=3` / `probe --num 3` | 前者匹配整数参数；后者失败 |
| 根为 `;`，子命令分隔符 `:` | `probe;child:A` / `probe;child A` | 前者匹配；后者失败 |
| 当前命令边界正则 | `probe,<city>` | 不能识别 `probe` 的边界 |
| 当前模板校验 | 模板 `probe\t<slot:0>`，输出 `probe <city>` | 错误地接受，Tab 已被归一化为空格 |
| 当前回复省略校验 | 模板 `probe,<slot:0>`，输出 `<回复消息> probe` | 拒绝，逗号没有随槽位一起省略 |
| 当前重复槽位校验 | `probe,<city>...,<day>` | 通用省略号边界校验拒绝，尚未进入模板对齐 |

这些结果确认了库语义与本地校验缺口；没有运行完整教学流水线、模型诊断或完整测试套件。

## 技术路线

### 1. 分层采集生效语法事实

在现有 snapshot 的节点投影上补充根、Subcommand、Option 和 Arg 的分隔信息。复用现有 dataclass 和
JSON 基础值，不引入与上游平行的配置系统或通用 Parser 类型。核心 `nbtriage` 不导入 Alconna / NoneBot。

根层沿用 `command.separators` 事实；组件和参数保留在现有结构中。保留全部分隔字符的事实及稳定顺序，
显示时如何选择与采集分开。同一集合仅顺序不同的情况采用相同的确定性显示字符；Evidence 和 shape
保留原始顺序，保守地避免未经证明的事实合并。缺失字段与已观察到的空值可区分。

实现适配器只读取已加载对象；不读取 `.env` / `os.environ`，不调用 `get_plugin_config` 重建配置，
不执行目标 parser、Pattern 转换器、Extension 或业务 handler。

### 2. 生成有界的确定性结构模板

改造 `_projection.py` 的现有渲染函数，保留从根到叶子的节点上下文和实际分隔边界，不再先用空格把路径
压成一个不可恢复的字符串。首版支持已验证的普通位置参数、现有可选 / 重复参数和 Option / Subcommand
结构中的安全单字符分隔，并覆盖与 compact 的组合；不是重新实现完整解析器。

- 每个实际边界选择一种可证明合法的分隔写法：允许普通空格时优先空格，否则从安全可见字符中稳定选择。
  选择必须满足上游对应位置的读取语义；不能把不同节点的首选字符任意拼成一条命令，不枚举分隔符组合。
- 如果需要 compact，空连接只用于原生允许的位置，不删除整条用法中的所有分隔符。
- 可选部分的分隔符与该部分一起省略。非空格情形例如 `probe,<slot:0>[,<slot:1>]`，不能让用户省略参数后
  留下必需的尾逗号。复用已有“可选组内必填槽位”的表达方式，并检查 Help / Answer 消费者能正确保留它。
  普通空格用法保持现有显示习惯。存在多个可选位置且无法唯一确定分隔归属时保守拒绝精确模板。
- 重复参数仍使用现有槽位外 `...` 记号，不展开无限重复；其后连接字符和可选组边界也必须合法。
- 首版模板只选普通空格或不与当前用法元语法冲突的可见字符。仅有 Tab / 换行 / 控制字符、括号、槽位标记、
  备选符、引号 / 转义等无法保真表达的选择时，判定为不支持；若同一位置存在等价安全选择，优先使用该选择。
- 具体参数值含分隔符的 quoting / escaping 不在本次范围；不新建具体值示例生成器，也不承诺任意字符串可
  直接替换槽位。必要的输入格式说明继续走有证据支持的 behavior boundary。

先使用原生 parser 的合成用例验证边界组合，再启用相应结构支持；不能把单个字符合法等同于整条用法可执行。

### 3. 模型与公开投影共用分隔符感知的校验

保留当前完整 usage 输出合同，模型只根据证据命名槽位。Prompt 只补一条明确规则：保留模板固定标点和
结构，不要求模型解释分隔符配置来源。暂不新增模型输出的槽位映射字段。

修改位置：`src/nbtriage/capability/teaching/{usage,annotations,model_adapter}.py`。

- 已有 `validate_capability_usage_template` 继续作为结构真值校验；先拒绝不支持的原始模板字符，不能先
  归一化再检查，以免 Tab 等被偷偷转换。公开文本的安全限制保持原样。
- 命令边界、alias 替换、`@bot` 检查从已验证模板取得精确位置或边界信息；不把所有标点加入全局正则，
  以免放宽普通 on_command 或把 `probeX` 错当成 `probe`。
- 重复槽位后的合法边界按当前模板验证，不因需要支持逗号就放宽所有非 Parser 用法的省略号规则。
- 回复变体省略槽位时处理与槽位绑定的分隔符；保留现有“必需回复才能满足必填参数”“不省略 Option / 分支
  内参数”“必须唯一对齐”“标准用法必须保留”的约束。不确定的回复变体可省略，不能破坏标准用法。
- 模型输出校验、公开投影、alias 替换使用同一套规则；沿用现有定向纠错次数，不增加全任务重跑。

### 4. 完成消费端、结构去重及缓存闭环

- `_family_parser_shape` 纳入实际分隔信息和渲染结果；相同业务 Handler 但语法不同的成员不得错用同一 shape。
  保留完整 family 成员，不借分隔差异拆分 family 或丢弃成员。
- 不支持精确渲染的 Alconna 不再无条件标为 `parser_exact`，也不能通过留空 `canonical_usages` 让模型自行
  编造结构。标准用法无法确定时复用准备阶段 `CapabilityAnalysisAdapterError` / 跳过路径，停止发布受影响
  teaching unit；family 仍整体处理，不关闭同一插件的其他独立单元。为该原因增加有界诊断区分即可。
- 核对当前发布、last-good、上一版编辑基线及 Help / Answer 路径；语法变化或变为不支持后，旧用法不能仍被
  当作当前可执行知识。旧基线仅用于重新审核，不能绕过新模板。失败处理保持当前原子发布机制。
- 让完整新事实、模板与请求修订进入已有 fingerprint；在实施时递增当时活动 request revision。
  Prompt 变化同步递增其 revision。仅当序列化合同确实变化时才调整相应 schema，不为本次另建缓存层。
- `_observed_command_entries` 中面向源码定位的分隔符归一化不得变成新 alias 或公开用法真值。
- 检查 `outputs.py`、Help / Answer 投影和 alias / family 展示不会重新用空格拼装已校验的用法。
  `discovery/registry.py::_command_usage` 是另一条显式 Provider 帮助路径：若会被本次消费链采用，应共享
  确定性渲染或对不支持结构停止生成猜测用法；不能调用原生 formatter 后直接宣称 parser_exact。

### 5. 验证、文档与变更整合

优先扩展现有高价值用例，将本次复现转成回归测试；只对实际修改范围运行 Ruff / 类型检查及相关测试。
同步 `docs/architecture/help-source-adapters.md` 的真实支持边界；合同修订后的模型质量状态不得继承旧 held-out。
真实模型诊断是观察理解负担的后续证据，不作为当前方案已经验证的事实；使用维护者显式授权的合成案例运行，
记录结构纠错次数、成功率和 token，不能用少量 smoke 宣称完成资格评测。

## 完成标准与验证

| 验收项 | 输入 / 条件与预期 | 主要测试位置 |
|---|---|---|
| 生效配置 | 框架配置对象模拟全局开关和局部覆盖；快照与实际注册命令一致，不读取进程环境 | `tests/capability/test_capability_snapshot.py` |
| 分层语法 | 根逗号 + Arg 空格、全逗号、混合 Arg、Option `=`、子命令 `:`、多种可用分隔符；模板替入简单值后原生 parser 得到预期参数 | `tests/capability/test_capability_analysis_adapter.py` |
| 默认兼容 | 默认空格、compact、别名、叶子、无参命令、可选 / 重复参数保持正确；去掉可选部分不留错误分隔符 | 同上及 `tests/capability/test_capability_annotations.py` |
| 校验闭环 | 标点被改写被拒绝；正确 alias、`@bot`、有依据且唯一对齐的回复形式通过；非 Parser 命令边界没有放宽 | `tests/capability/test_capability_annotations.py`、`test_capability_model_adapter.py` |
| 不支持边界 | Tab-only、用法元字符-only、缺失事实、无法表示的组合不被归一化成空格，也不交给模型猜；普通独立单元继续 | `tests/capability/test_capability_analysis_adapter.py`、`test_capability_teaching_maintenance.py` |
| family | 不同分隔结构不得错合；安全可见写法的选择稳定；不支持成员不被静默删除 | `tests/capability/test_capability_analysis_adapter.py` |
| 缓存 / 发布 | 改分隔符产生新指纹；受影响旧用法不再公开；失败、旧编辑基线及原子发布不恢复过时语法 | `tests/capability/test_capability_annotation_cache.py`、`test_capability_teaching_maintenance.py` |
| 输出 | Help / Answer 保留固定标点、可选组、别名和 mention，不二次改写语法 | `tests/capability/test_capability_teaching_outputs.py` |

原生 parser 测试仅构造项目自己的无业务回调合成命令，不调用目标插件 parser；普通 pytest 使用默认临时目录。
实施阶段先运行本次修改的用例，随后运行上述相关文件；不修改冻结的历史评测 fixture 来伪装新合同通过。

## 自审结果

| 自审发现 | 对原方案的修正 | 证据状态 |
|---|---|---|
| “全局逗号意味着整条命令逗号”不成立 | 分别保留命令 / 节点 / Arg，按原生消费顺序处理 | 原生 parser 复现确认 |
| 只改 renderer 会被旧校验拒绝 | 同步命令边界、alias、mention、重复参数校验 | 边界、重复槽位、别名、mention 与模型校验回归确认 |
| 空白归一化能接受错误分隔符 | 支持边界前置，Tab-only 不替换为空格 | 模板校验复现确认 |
| 可选参数、回复省略可能遗留标点 | 分隔符随可选部分处理；回复继续唯一对齐 | 回复、可选参数和 Help / Answer 回归确认 |
| 无模板被误当作模型自由生成入口 | 复用 unit 准备失败，不能仅设置空模板或低 fidelity | 当前分支源码确认 |
| 错合 family 或沿用旧注释会抵消修复 | 完整事实参与 shape / fingerprint，覆盖 last-good 和发布路径 | 源码与完整刷新回归确认 |
| 原生 formatter 不满足匿名模板合同 | 只复用上游字段与语义，不建立新配置层或解析器 | 当前 formatter 与教学合同源码确认 |

本轮自审已将上述修正纳入路线，无需用户补充新的产品决定。所有“不支持”处置沿用既有公开知识边界；
若实施中需要改变 family 原子性、允许无可靠标准用法仍发布知识，或扩展公开用法语法，则超出本计划，
届时单独明确取舍。本轮实施保持上述边界，完成后归档。


## 实施结果与最终自审

- 快照补齐根、Arg、Option、Subcommand 的生效分隔事实；不添加环境变量读取或配置优先级系统。
- 复用现有 renderer、匿名槽位合同和校验器。各层边界、compact、可选分隔组、重复参数、alias 与 `@bot`
  共用 canonical 模板；回复仍须有 Evidence、保留标准用法并唯一对齐。
- 根仅逗号、Arg 仍空格和全部逗号生成不同模板；Option `=`、多级子命令以及可独立省略的 Option 均有
  原生 parser 合成验证。断言解析成功及实际参数值，避免把“matched”误当作字段消费正确。
- 无法安全表达的分隔符或组合以 `unsupported_syntax` 停止单元准备；family 不静默删除失败成员。
  缺失旧快照字段不被猜成空格。相同首选显示但完整分隔集合不同的 family shape 不合并。
- 同步另一条显式 Provider 的默认帮助渲染；已有显式 usage 仍按原合同处理。目标插件 parser 不执行。
- 语法变化进入现有 fingerprint，重新生成失败或变为不支持时不恢复受影响旧注释；独立稳定单元仍可用。
- Prompt 从实施开始时 v109 升至 v110，request 从 v71 升至 v72，Schema 仍为 v12。
  未修改历史冻结评测 fixture，未运行真实模型 API，不继承此前模型质量资格。

自审收敛的限制：祖先位置参数与子命令并存、节点 `requires` 和不一致的可省略边界会停止生成，避免继续
输出此前不完整的精确结构；不重建整个 Alconna parser。原始分隔字符串完整保留，显示选择稳定；仅顺序变化
可能导致保守刷新，接受这点成本以避免未经证明的归一化合并。参数值的引用与转义仍在本轮范围之外。
子命令路径查找限定节点类型；匿名模板先通过公开语法结构检查，再进入模型命名与严格对齐。

### 验证证据

最终联合回归执行以下 12 个文件，共 **323 passed**：

```text
uv run --no-sync pytest tests/capability/test_alconna_separators.py tests/capability/test_capability_snapshot.py tests/capability/test_capability_annotations.py tests/capability/test_alconna_capabilities.py tests/capability/test_capability_analysis_adapter.py tests/capability/test_capability_model_adapter.py tests/capability/test_capability_shadow.py tests/capability/test_capability_teaching_outputs.py tests/capability/test_model_prompt.py tests/capability/test_capability_annotation_cache.py tests/capability/test_capability_analysis_pipeline.py tests/capability/test_capability_teaching_maintenance.py -q --tb=short
```

随后仅加强合成命令的实际参数值断言，重跑 `test_alconna_separators.py`：**18 passed**。
Ruff check / format 与 `git diff --check` 通过。本次核心 usage、annotations、model adapter、
适配器 projection 和 registry 的 `ty check` 通过。
扩大检查到刷新服务 `nonebot_plugin_triage/capability/teaching/annotations.py` 时存在 6 条原有 Optional
推导诊断；移除本次 enum / 原因映射改动的临时副本仍复现全部 6 条，未混入修复。snapshot 的 2 条
原有 redundant-cast 警告保留。临时类型基线副本已删除。
pytest 警告为 Alembic 既有弃用配置及合成模型缺少价格数据，不影响本次合同回归。

实施中曾误把匿名模板直接送入“公开命名结果对齐”验证，因保留 `slot:N` 被正确拒绝；已改为先验证结构，
再由现有模型输出路径校验命名，最终联合回归通过。未把中途失败或此前的 300 / 42 项结果当成最终验收。
