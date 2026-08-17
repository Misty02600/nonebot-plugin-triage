# ADR-0062：用结构化用法、要求与交互表达教学注释

| 状态 | 决策日期 |
|---|---|
| 已采纳 | 2026-08-15 |

## 当时遇到了什么

教学注释已有功能摘要、单条 `display_pattern`、同义词、操作对象、输入要求、行为边界和扁平约束。
真实 dogfood 前的字段审查发现，这个结构同时服务两种不同消费者时会丢失重要语义：Migut Help 需要短而
稳定的直接展示文案，Answer Agent 则需要多种完整调用形式、检索词、结构化角色、多个限流和必要的交互信息。

原结构还有三个具体问题：

- 单条 `display_pattern` 无法同时表达 `搜图 [图片]` 与 `[回复图片] 搜图`；
- 角色、场景、限流和功能开关落入缓存后全部退化成字符串，Migut Help 无法可靠投影 `permission` 或
  `has_cd`；
- 通用 ast-grep 规则曾按 `cooldown`、`limiter`、`rate_limit` 等名称猜测限流，第三方代码中的同名符号
  没有稳定语义来源，容易产生错误公开说明。

人工维护的 Migut Help YAML 将作为后续离线评价源，用来观察功能分组、用法压缩、文风和详略，但不是
绝对事实，也不得进入冷启动生成 Prompt。当前 ADR 只确定生成合同和投影边界，不确定评分标准。

## 决策

1. 教学注释 schema 升级为以下公开字段：
   - `summary`：简洁说明功能用途，也可保留必须直接告诉用户的特殊说明；不得重复用法；
   - `usages`：一至四条有序、完整调用形式，第一条是默认展示形式；
   - `synonyms`：用户可能用来询问同一能力的名称，只用于检索和 Answer Agent，不默认显示；
   - `supported_subjects`：最多八个、每项不超过二十字符的自由名词或名词短语，只用于检索和 Answer
     Agent；
   - `input_requirements` 与 `behavior_boundaries`：保留用户必须提供的输入和可观察行为边界；
   - `requirements`：保留类型的公开角色、场景、限流、功能状态和其他前置要求；
   - `interaction`：可选的 `single_turn`、`bot_guided` 或 `multi_turn` 及必要步骤。
2. `usages` 使用 Migut Help 现有可读约定：`(A|B)` 表示触发形式，`<参数>` 表示必填，`[参数]` 表示
   可选或前置上下文，`...` 表示多值，`@bot` 表示需要提及 Bot。回复前置上下文写成 `[回复图片]`、
   `[回复表情包]`，不追加“消息”。命令前缀必须服从 runtime 的 `command_start` / Alconna 前缀事实，
   不得默认补 `/`；`to_me()` 可投影为 `@bot`。
3. 角色枚举只保留 `all`、`admin`、`owner`、`superuser`、`custom`。Uninfo `MEMBER()` 表示普通成员且
   可能排除管理员或群主，因此确定性映射为 `custom`，公开文字说明“仅普通成员可用”，不把它解释成最低
   权限。只有 Matcher 入口鉴权或可证明的门控控制流才能产生角色 requirement；handler 内仅用于分支行为的
   角色判断不升级为入口要求。
4. 一项能力可以有多条 `rate_limit` requirement。每条同时保留：
   - `policy`：`cooldown`、`quota`、`concurrency` 或 `custom`；
   - `scope`：`user`、`scene`、`bot`、`global`、`custom` 或 `unknown`；
   - 有 Evidence 支持的自然语言 `text`。
   精确时长、额度和作用域只能来自当前 runtime 配置投影或可引用证据。
5. 删除按通用符号名称猜测 limiter 的 ast-grep 规则。首版只有 NoneBot 官方核心及官方 Adapter、Alconna、
   Uninfo 的稳定语义表可以产生确定性框架约束；其他第三方依赖保持未知。目标插件自己的有界源码和安全配置
   投影可以交给模型解释，但不能仅凭相似名称发布限流或鉴权事实。
6. Runtime 与确定性提取器拥有注册状态、命令结构、前缀、已解析权限和场景事实；模型不得修改这些事实。
   模型负责 Evidence 支持的摘要、多个用法的可读组织、输入前提、行为边界、检索词、详细限流文字和必要
   交互说明。模型输出仍必须经过 schema、Evidence 引用闭包和公开文字安全门。
7. `interaction` 不自动拼入紧凑帮助描述。`bot_guided` 可以不列步骤；只有后续步骤对正确使用确实重要时，
   才在 `multi_turn.steps` 中保存，供 Answer Agent 或未来扩展的 Migut Help 使用。
8. 首版不记录 denial / 失败提示模式。权限或限流失败是否静默、是否回复以及部分分支是否提示，暂不增加到
   教学 schema；等真实语料证明 Answer Agent 需要后再用后续 ADR 扩展。
9. 独立帮助 YAML 同时输出全部 `usages`，并把第一项兼容投影到现有 `display`；存在任意 `rate_limit`
   requirement 时输出 `has_cd: true`。`supported_subjects` 不再被拼成帮助描述。Answer 候选检索使用
   `synonyms` 与 `supported_subjects` 补召回，但它们不是封闭白名单。

## 影响

- 注释缓存 schema、Prompt revision 和任务资格必须更新；旧缓存不能冒充新结构。
- Migut Help 当前只消费 `display` 与 `has_cd`；额外 `usages` 先保留在 Triage 数据目录中的独立 YAML，后续
  可以在不改变事实层的情况下扩展 Migut Help 渲染器。
- 人工 YAML 的后续评价应按语义字段比较，不比较 YAML 排版、引号或字段顺序；人工内容与生成输入必须隔离。
- 当前结构允许 dogfood 后调整字段详细程度，但删除结构化权限/限流、恢复通用名称猜测或改变 Runtime/模型
  字段所有权时，需要新的 successor ADR。

## 落实与确认

- `nbtriage.capability_analysis` 与 `nbtriage.capability_annotations` 已实现有序 usages、结构化 requirements 和
  interaction，并以新 schema / Prompt revision 失效旧缓存。
- `nbtriage.framework_semantics` 已把 Uninfo `MEMBER()` 映射为 `custom`，ADMIN / OWNER 分别映射为
  `admin` / `owner`；源码提取保留 import provenance，避免本地同名函数被误认。
- `nbtriage.capability_source_evidence` 已删除通用 limiter 名称候选。
- Answer facts、注释同义词 / 主题补召回和独立帮助 YAML projector 已消费新结构。
- Runtime 记录现以独立 `invocation.header` 统一命令头与可直接发送的字面触发锚点；NoneBot 命令同时记录
  当前进程的 `command_start` / `command_sep`，`on_startswith / on_endswith / on_fullmatch / on_keyword`
  可以进入教学，正则、事件类型和无确定触发形式的被动 Matcher 仍失败关闭。
- 用法输出现会拒绝把“后发送页码”等多轮说明写入 `usages`，并要求 `[回复图片]` 等回复上下文位于命令
  之前；省略媒体后 Bot 仍会引导补充时，紧凑用法使用 `[图片]` 而不是 `<图片>`。
- 独立帮助 YAML 的 description 只直出一句 summary，并补充无法由现有展示字段表达的 custom 角色与详细
  限流；场景、交互步骤、输入要求和行为边界仍保留给 Answer Agent，不再全部拼成一段帮助图文字。

## 相关决定

- [ADR-0027](0027-constrain-guidance-with-facts-not-fixed-wording.md)
- [ADR-0032](0032-separate-capability-audience-analysis-and-platform-status.md)
- [ADR-0036](0036-keep-capability-shadow-deterministic-and-record-oriented.md)
- [ADR-0055](0055-use-ast-grep-for-matcher-source-shape-extraction.md)
- [ADR-0058](0058-use-deterministic-evidence-and-bounded-navigation-for-teaching-annotations.md)
- [ADR-0059](0059-share-read-only-evidence-access-across-agent-flows.md)
