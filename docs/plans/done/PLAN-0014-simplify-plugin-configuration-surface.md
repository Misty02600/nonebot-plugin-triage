# PLAN-0014：收敛插件部署配置面并重写 README 配置语义

| 状态 | 最后更新 |
|---|---|
| 已完成 | 2026-08-14 |

## 背景

当前 `NBTriageConfig` 同时暴露产品入口、实现容量、安全边界、模型资格和本地存储位置。README 又把其中一部分
字段以“名称 + 默认值 + 简短标签”的形式列出，部署者难以判断某个值实际改变哪条运行路径、作用于谁、关闭
时发生什么，以及它究竟是不是必须选择的部署策略。

用户已明确要求把配置收敛工作记录为正式 plan，并锁定以下方向：

- `triage` 命令名、Matcher 优先级和 2000 字入口上限不再作为环境配置；
- 查询、反馈、统计命令名不再作为环境配置；
- 两层冷却合并为一个覆盖所有 `triage` 处理轮次的入口冷却；
- `NBTRIAGE_RESTRICTED_CONFIG` 保留；
- README 必须在配置项本身的“含义”中解释真实行为，不能用表后“额外说明”补齐一个本来含糊的标签。

本计划把“移除配置项”解释为取消部署者修改该值的自由度，而不是删除对应功能。`triage`、`报错查询`、
`报错反馈`、`报错统计`、2000 字入口守门和 Matcher 优先级继续作为固定产品合同存在。若未来要删除命令能力
本身，应另行确认产品范围并处理其 ADR、流程和运行时服务，不在本计划中顺带完成。

## 当前设计与缺陷

### 相关实现与当前行为

- `src/nonebot_plugin_triage/config.py::NBTriageConfig` 当前持有四个命令名、两组 Matcher 优先级、入口文字
  上限、两组 cooldown、本地能力影子路径，以及多组容量、TTL、模型和试运行参数。
- `src/nonebot_plugin_triage/handlers.py` 同时用动态命令字段构造 Alconna、前置 Rule、usage、提示语和四个
  Matcher；`src/nonebot_plugin_triage/__init__.py` 还用这些字段生成插件 metadata。
- `handle_support` 在空输入、长度检查、Thread claim、模型语义请求和业务路由之前消费入口限流。当前 scope
  是 `adapter + Bot + conversation + actor`；首轮、Reply 续问、教学、澄清、报障和拒绝路径都会先经过它。
- `src/nonebot_plugin_triage/runtime.py::create_plugin_runtime` 另建一份 30 秒报告限流账本，并注入
  `LiveReportService`。它只在 Reply 重新关联、可信失败证据复核和授权消费之后、Incident 写入之前消费。
- `src/nonebot_plugin_triage/capability_shadow.py::register_capability_shadow` 目前把
  `NBTRIAGE_CAPABILITY_SHADOW_PATH` 同时当作 SQLite 位置和功能启用条件；未配置路径时整个影子服务不存在。
  能力说明分支会先查显式 Provider，随后才查影子的 public 或已鉴权 maintainer 视图。
- `NBTRIAGE_RESTRICTED_CONFIG` 会被规范化为大小写不敏感的 NoneBot 顶层键 deny-list；`FOO__BAR` 会收敛为
  `foo`。`ConfigValuePolicy` 在任何实际配置值投影到能力分析模型之前应用该策略。
- 当前在线 support semantic assessment 只发送本轮规范化求助文字，不发送 NoneBot 配置值；配置 deny-list
  保护的是能力配置解释链，而不是在暗示“未列出的整份配置会自动发送给每次语义分类”。

### 缺陷机制、证据与影响

1. **产品常量伪装成部署能力。** 修改命令名、Matcher 优先级或入口上限并不是当前受控试用需要的产品选择，
   却扩大配置验证、metadata、提示语和测试矩阵；错误调整优先级还可能改变 NoneBot Matcher 调度关系。
2. **同一请求存在两个不同政策的冷却账本。** 每轮 `triage` 先消费 2 秒入口窗口，可信报障随后再消费
   30 秒 Incident 窗口。README 的两句标签没有说明二者的顺序、scope 或一次报障会依次经过两层限制，部署者
   也无法从表中判断正常续问和重复建单的差异。
3. **内部缓存路径被暴露为产品配置。** 能力影子是可删除重建的部署本地派生索引。让部署者选择 SQLite
   路径增加目录权限、迁移、清理和并发负担，却没有表达真正的产品行为；NoneBot LocalStore 已提供插件级
   cache 路径所有权。
4. **配置说明没有解释配置。** 例如“普通用户自然语言入口”“维护者反馈命令”只是给字段换了一个中文名称，
   没有说明设置什么会改变什么行为。README 还遗漏了 `NBTRIAGE_QUERY_PRIORITY`、限流 scope 容量等真实
   字段，并存在模型 token 默认值与唯一 qualified profile 要求不一致等待审计问题。

## 技术路线

### 目标行为与约束

#### 固定产品合同，不再暴露环境变量

以下行为集中为单一来源的内部常量，供 handlers、plugin metadata 和测试共同引用：

| 现有配置 | 固定合同 | 移除配置后的含义 |
|---|---:|---|
| `NBTRIAGE_COMMAND` | `triage` | 用户入口仍是 `triage <自然语言>`，部署者不再改名。 |
| `NBTRIAGE_PRIORITY` | `10` | 主 Matcher 的 NoneBot 调度优先级固定，不再把框架调度顺序交给部署配置。 |
| `NBTRIAGE_REQUEST_MAX_CHARS` | `2000` | 首轮和 Reply 续问仍在进入模型与领域请求前拒绝超长文字；删除的是配置自由度，不是安全边界。 |
| `NBTRIAGE_QUERY_COMMAND` | `报错查询` | 保留 `SUPERUSER` 查询能力，只取消命令改名。 |
| `NBTRIAGE_FEEDBACK_COMMAND` | `报错反馈` | 保留 `SUPERUSER` 反馈能力，只取消命令改名。 |
| `NBTRIAGE_TRIAL_STATS_COMMAND` | `报错统计` | 保留 `SUPERUSER` 试运行统计能力，只取消命令改名。 |
| `NBTRIAGE_QUERY_PRIORITY` | `10` | README 虽未列出该字段，也一并固定并移除，避免留下半套动态命令合同。 |

删除字段后，旧键不得被 Pydantic 静默忽略。配置加载应在不回显旧值的前提下 fail-fast，并告诉部署者该值
已经固定或迁移到哪个新字段。

#### 只保留一个 `triage` 入口冷却

对外只保留 `NBTRIAGE_COOLDOWN_SECONDS`，默认沿用当前入口行为的 `2` 秒。它的完整含义是：同一
`adapter + Bot + conversation + actor` 每次匹配并进入 `triage` handler 后，在该秒数内再次发送任何
`triage` 请求都会被拒绝；首轮、空输入、超长输入、Reply 续问、教学、澄清、报障和安全拒绝共用同一窗口。
账本只存在于当前进程内存，Bot 重启后清空，因此它不是跨进程全局配额、模型费用预算或持久封禁。

`报错查询`、`报错反馈` 和 `报错统计` 是独立的 `SUPERUSER` 维护命令，不属于上述自然语言 `triage`
入口冷却。报告专用 `NBTRIAGE_REPORT_COOLDOWN_SECONDS` 和 `LiveReportService` 的第二份限流账本删除；同一
请求只能在统一入口消费一次，不能把同一个 limiter 在建单阶段再次调用而让本轮必然自拒。默认保留 2 秒而
不是 30 秒，是为了不让正常澄清和 Thread 续问被长时间阻塞；其代价是 Incident 不再拥有额外的 30 秒重复
建单抑制，应由新 ADR 明确替代 ADR-0020 的两级限流及 ADR-0040 的建单限流部分。

#### 保留部署者的模型数据否决权

`NBTRIAGE_RESTRICTED_CONFIG` 继续作为公开部署配置。README 的“含义”必须直接说明：它是 JSON 数组，
列出禁止把实际值交给能力分析模型的 NoneBot 顶层配置键；匹配大小写不敏感，嵌套键按顶层整项限制。
列入该数组不会从 NoneBot 全局配置中删除字段，也不禁止分析公开 schema 或源码语义；未列入也不等于允许
上传整份 `.env` 或完整 Config，系统仍只能瞬时投影与当前候选能力相关的值，原值不得进入索引、日志、
测试快照或 Bot 回复。

#### 能力影子保留功能，移除路径配置

`NBTRIAGE_CAPABILITY_SHADOW_PATH` 不再作为公开配置。能力影子仍是模型外、确定性的本地能力检索层，不是
“大模型影子运行”，也不负责诊断。目标实现复用 `nonebot_plugin_localstore` 的插件级 cache API，以
固定内部文件名保存可删除重建的 SQLite cache；测试通过依赖注入覆盖临时路径，文件名、位置和数据库格式
都不构成公开契约。

影子损坏、不可写、版本不兼容或刷新失败时必须 fail-open：保留上一份可用 generation，或退化到显式
Provider / 澄清，不能阻止插件启动或模型语义分流。索引不得保存 Token、原始私密日志、未脱敏配置、完整
Thread 内容或用户身份；`restricted` 能力资料仍只能在模型外确认 `SUPERUSER` 后读取。是否在删除路径配置
后默认自动启用，见 D-001。

#### 审计其余配置，但不静默扩大删除范围

实施前按以下类别逐项审计 `NBTriageConfig` 与 README：

- 部署者必须选择的产品或数据边界，例如模型 transport 身份、试运行模式和
  `NBTRIAGE_RESTRICTED_CONFIG`；
- 仅高级运维确有需要时才应暴露的容量、TTL、超时和轮转策略；
- 应成为内部安全常量、资格 profile，或交给 LocalStore 管理的实现参数。

本轮未点名的 `NBTRIAGE_RATE_LIMIT_MAX_SCOPES`、capability visibility timeout、各内存 buffer 容量与 TTL、
trial rotation、model timeout / token budget、knowledge pack URL / hash 都进入审计清单，但没有新产品决定前
不得擅自删除。审计后本轮继续保留这些高级运维项，并在 README 逐项解释；模型 max output 默认值从与资格
错位的 `1024` 收敛为唯一 qualified semantic profile 要求的 `240`。

#### README 直接解释字段语义

README 配置表第三列改为“含义”。每个保留字段必须在本行直接回答：

1. 它控制哪条运行行为、由谁使用；
2. 默认值或未设置分别意味着什么；
3. 作用域、边界和失败语义中哪些会影响部署判断；
4. 它不提供什么能力，避免把缓存路径、限流、deny-list 或模型 profile 误解为功能开关。

表后只保留 JSON / dotenv 格式示例、必要的安全边界和组合约束，不再用“额外说明”补齐表中本应存在的
配置含义。

### 实施步骤

| 顺序 | 改动 | 主要实现位置或符号 | 关键约束 | 预期结果 |
|---:|---|---|---|---|
| 1 | 建立完整配置清单并按部署选择、高级运维、内部合同分类 | `NBTriageConfig`、README、部署文档 | 用户已点名项按本计划锁定；其他字段只审计，不先删 | 明确唯一公开配置面与后续决策项 |
| 2 | 新建 ADR，记录统一入口冷却与能力影子存储/启用决定 | `docs/adr/`、ADR 索引 | 不改写已采纳 ADR；明确替代 ADR-0020、ADR-0040 和 ADR-0021 的具体条款 | 架构决定与实现方向一致 |
| 3 | 把命令、优先级和 2000 字上限移为集中产品常量 | `config.py` 或轻量产品合同模块、`handlers.py`、`__init__.py` | handlers 与 metadata 只有一个事实来源；长度守门仍早于模型请求 | 删除无效配置自由度而不删除功能 |
| 4 | 合并 cooldown 并删除 Incident 二次限流 | `config.py`、`runtime.py`、`live_reports.py`、rate-limit tests | 每轮入口只消费一次；保留 HMAC scope 与有界容量 | 所有 `triage` 轮次只有一个可解释冷却政策 |
| 5 | 按 D-001 落实能力影子生命周期 | `capability_shadow.py::register_capability_shadow`、runtime、LocalStore、tests | 可重建 cache；启动后台工作；失败降级；测试路径内部注入 | 部署者不再管理 SQLite 路径 |
| 6 | 保留并强化 restricted config 数据准入合同 | `config.py`、`config_policy.py`、config projection tests | deny-list 先于值读取；不持久化或回显原值 | 模型配置值边界继续由部署者掌握 |
| 7 | 重写 README 配置表并同步稳定文档 | README、architecture overview/flows、operations、相关 ADR 落实说明 | 配置本行解释完整含义；历史 ADR 不伪装成当前事实 | 文档与真实运行行为一致 |
| 8 | 更新配置、Matcher、限流、影子与隔离加载回归 | `tests/`、构建与质量命令 | 删除动态配置测试，保留固定功能合同；旧键 fail-fast | 变更可验证且不会静默漂移 |

## 完成结果

| 状态 | 工作项 | 结果 |
|---|---|---|
| 已完成 | 固定产品合同、统一限流、LocalStore 影子与 README 配置清单 | taxonomy v5 合并后重新核查，固定 2000 字、统一 cooldown、模型外影子边界均保留。 |
| 已完成 | 本计划实现与仓库级质量回归 | `uv lock --check`、Ruff lint/format、BasedPyright（0 errors / 0 warnings）、`git diff --check`、1338 tests、wheel/sdist 与 Twine 检查均通过。 |
| 已完成 | 稳定文档与决策同步 | ADR-0045、README、架构概览和限流/影子流程已与最终行为对齐，计划转入 `done`。 |

## 完成标准与验证

| 验收项 | 覆盖条件或输入 | 预期结果 | 验证方式 |
|---|---|---|---|
| 固定入口合同 | 默认加载、旧命令/优先级/长度配置、首轮与续问边界 | 固定四个命令和优先级；2000 字守门保留；旧键带迁移提示 fail-fast | 配置单测、NoneBot 隔离加载与 Matcher 集成测试 |
| 统一冷却 | 首轮、空输入、超长、续问、guidance、incident、拒绝路径 | 每轮只消费统一入口窗口；Incident 不发生第二次自拒 | limiter 单测、handler 与 live report 集成测试 |
| 能力影子生命周期 | 正常 cache、首次构建、旧 generation、不可写、损坏或版本不兼容 | 按 D-001 启用；失败回退显式 Provider/澄清且不阻止启动 | LocalStore 注入测试、影子服务与插件加载测试 |
| 模型数据边界 | 大小写键、嵌套键、受限复合对象、未受限但无关字段 | 受限值读取前被拒；只投影相关允许值；原值不进持久化和回复 | config policy/projection/analysis adapter 测试 |
| README 配置语义 | 每个保留配置及未设置、禁用、组合场景 | 每行直接说明控制对象、默认行为、作用域和边界；无已删除字段 | README 人工复核与配置字段清单测试 |
| 稳定文档同步 | 限流、影子、模型、trial 运维说明 | 当前事实与新 ADR 一致；历史决定保留替代关系 | 文档链接与术语检查 |
| 仓库质量 | 全部代码、测试和文档完成 | lock、lint、format、类型、全回归与构建继续通过 | `uv lock --check`、`uv run ruff check src tests tools`、`uv run ruff format --check src tests tools`、`uv run basedpyright`、`uv run pytest`、`uv build` |

## 已确认事项

- 2026-08-13：删除命令名、Matcher 优先级和入口文字上限的配置自由度，保留固定产品行为。
- 2026-08-13：只保留一个覆盖所有 `triage` 处理轮次的入口 cooldown；查询、反馈和统计仍是独立的
  `SUPERUSER` 维护命令。
- 2026-08-13：保留 `NBTRIAGE_RESTRICTED_CONFIG`，并在 README 配置项本身解释其数据准入语义。
- 2026-08-13：README 不再把简短标签当配置说明，也不依赖表后“额外说明”补齐关键含义。
- 2026-08-13 · D-001：能力影子保留并默认启用，SQLite 作为 LocalStore 管理的可重建 cache；失败时安全
  降级，不阻止 `triage`。
- 2026-08-13：其余高级运维字段本轮保留并完整说明；模型输出默认值与唯一准入 profile 对齐为 240。
- 2026-08-13：nonemigut 仍锁定旧插件提交且用户要求不改依赖，因此新版 OpenCode Go 配置只写入宿主本地、
  不自动加载的候选文件；当前生效环境不提前启用不兼容字段，也不写入密钥。候选已用本工作树
  `NBTriageConfig` 验证通过。

## 相关文档

- [ADR-0020：使用 `triage` 作为自然语言支持入口](../../adr/0020-use-triage-command-for-natural-language-support.md)
- [ADR-0021：用部署本地影子索引整理 Bot 能力证据](../../adr/0021-use-deployment-local-capability-shadow-index.md)
- [ADR-0029：以部署者 deny-list 控制能力相关配置值进入模型](../../adr/0029-control-model-config-values-with-deployment-deny-list.md)
- [ADR-0031：要求通过 `triage` 延续支持 Thread](../../adr/0031-require-triage-for-support-thread-continuation.md)
- [ADR-0040：只让可信预检失败进入 Incident](../../adr/0040-require-trusted-preflight-failure-before-incident.md)
- [架构概览](../../architecture/overview.md)
- [能力影子索引流程](../../architecture/flows/capability-shadow-index.md)
- [观察试运行部署说明](../../operations/observation-trial-rollout.md)
