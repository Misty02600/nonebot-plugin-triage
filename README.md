<div align="center">

<a href="https://v2.nonebot.dev/store">
  <img src="https://raw.githubusercontent.com/fllesser/nonebot-plugin-template/refs/heads/resource/.docs/NoneBotPlugin.svg" width="310" alt="NoneBot Plugin">
</a>

# NoneBot Triage Agent

[![License](https://img.shields.io/github/license/Misty02600/nonebot-plugin-triage.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11--3.14-blue.svg)](https://www.python.org/)
[![NoneBot](https://img.shields.io/badge/NoneBot-2.5+-ea5252.svg)](https://nonebot.dev/)
[![CI](https://github.com/Misty02600/nonebot-plugin-triage/actions/workflows/ci.yml/badge.svg)](https://github.com/Misty02600/nonebot-plugin-triage/actions/workflows/ci.yml)

接收私聊、群聊或频道中的显式求助，并按需关联 NoneBot 本机运行证据。

</div>

## 介绍

发送 `triage <求助内容>` 即可调用插件，`@Bot` 可选。`triage` 后可以直接写自然语言，例如询问功能用法或
描述遇到的问题。回复近期消息时，插件还会尝试关联这条消息在本机产生的运行记录。
续问同样需要发送 `triage <内容>`；若同时精确回复 Triage 最近一次仍有效的回答，插件会尝试续接原 Thread。
只有 Reply、没有 `triage` 的消息不会触发该入口。Triage 现在用当前 Alconna Matcher 返回并经严格校验的
UniSeg Receipt 登记下一条续接点；本地合同测试覆盖 OneBot V11 群聊 / 私聊和 Discord 频道 / 私聊。
未验证的平台、畸形回执或未命中 Reply 会按一次新的 `triage` 请求处理；真实 Discord 网关仍待 smoke。
同一 Thread 的上一轮仍在处理时，新的续问不会排队或并行执行；请等待后重新发送 `triage`。Reply 一旦被
当前处理轮接受就会失效，若处理或发送失败，也请重新发送完整请求。

每次非空 `triage` 现在都默认经过版本化语义 assessment service。未配置 transport 时，runtime 装配
unavailable service并让本轮 abstain；配置已准入的 OpenCode Go 精确组合后，首轮与续问会各发起一次受限
语义请求，且不会回退到词表或固定话术。
语义模型只输出四类目标和现象陈述，确定性 router 才决定 action；它不能回答、鉴权或直接建单。router
选择公开能力指导后，插件会从显式 Provider 或能力影子构造只含公开事实的闭合请求，再调用独立的 Answer
Agent 组织自然语言回答；因此一轮 guidance 最多产生两次远端请求。Answer Agent 没有工具，不能读取受限
能力、源码、配置或运行证据，失败、超时、引用未知事实或输出非法时会退回确定性模板。
只有语义模型同时识别到明确 `incident_intake` 目标与 `reported_observation`、模型外 Reply correlation
对应的可信运行证据明确失败，router 才会为精确
`LiveReportRequest` 签发进程内授权；报障服务一次性消费该授权后才能进入故障受理。插件不执行求助文本里的
命令，也不会自动创建 Issue。没有 Reply、引用未命中、只有成功生命周期或空回执时，报告保持未验证并
进入澄清，不创建 incident、trial，也不进入后续 Agent。

## 安装

```bash
git clone https://github.com/Misty02600/nonebot-plugin-triage.git
cd nonebot-plugin-triage
uv sync --all-extras --group dev
```

宿主按实际适配器安装对应 extra，例如 `nonebot-plugin-triage[onebot]` 或
`nonebot-plugin-triage[discord]`。使用当前已准入的语义 transport 时还需安装
`nonebot-plugin-triage[openai]`；OpenCode Go 复用 Pydantic AI 的 OpenAI Provider，不再声明内容重复的
专用 extra。插件不会替宿主注册适配器。

在宿主 NoneBot 项目中加载插件：

```toml
[tool.nonebot]
plugins = ["nonebot_plugin_triage"]
```

## 配置

`triage`、`报错查询`、`报错反馈`、`报错统计`、Matcher 优先级和 2000 字入口上限是当前版本固定的产品合同，
不再通过环境变量改写。以下各项的“含义”直接说明其控制对象、默认行为、作用域与失败边界。

| 配置项 | 默认值 | 含义 |
|---|---:|---|
| `NBTRIAGE_COOLDOWN_SECONDS` | `2` | 同一适配器、Bot、会话和用户每次进入 `triage` 后，在该秒数内再次发送任何 `triage` 请求都会被拒绝；首轮、续问、空输入、超长输入、教学、澄清和报障共用窗口。窗口只在当前进程内存中，重启清空，不是跨进程配额或模型费用预算。 |
| `NBTRIAGE_RATE_LIMIT_MAX_SCOPES` | `4096` | 当前进程入口限流表最多保留的不同 `适配器 + Bot + 会话 + 用户` scope 数；容量满时淘汰最旧 scope 并累计 drop 计数。它限制内存键数量，不提高单个用户频率，也不提供跨进程协调。 |
| `NBTRIAGE_CAPABILITY_VISIBILITY_TIMEOUT_SECONDS` | `0.25` | 收集显式 Alconna 能力时，等待单个第三方 Provider 异步可见性判断的最长秒数；超时、异常或返回 false 的能力不会进入本轮公开说明。它不控制模型请求或能力影子后台刷新。 |
| `NBTRIAGE_CAPABILITY_ANNOTATION_MODE` | `off` | `off` 只使用运行时元数据与已有公开事实，不把能力源码或配置值发送给模型；`auto` 在启动后的后台任务中，对本次已成功注册、明确公开且证据完整的命令读取有界 handler 源码与策略允许的相关配置值，直接生成无需人工审核的公开教学注释。加载失败、未观察到、受限、平台未知或有分析问题的能力不会生成或提供注释；单项失败只让该能力退回原说明。 |
| `NBTRIAGE_OBSERVATION_MAX_ENTRIES` | `10000` | 当前进程最多保留的 NoneBot 生命周期观察记录数，用于 Reply 关联后的可信失败复核；容量满时旧记录被淘汰，原始 API data/result 不会存入该 buffer，重启后清空。 |
| `NBTRIAGE_OBSERVATION_RETENTION_SECONDS` | `900` | 生命周期观察记录可参与故障证据关联的最长秒数；过期记录不能再支持 Incident，同时也界定受理服务接受证据的时间范围。它不是日志保存期。 |
| `NBTRIAGE_REFERENCE_MAX_ENTRIES` | `4096` | 当前进程最多保留的出站消息引用索引数，用于把用户 Reply 的 `message_id` 精确关联到近期 Bot 运行；容量满时旧引用被淘汰，索引保存 HMAC scope 而不是平台身份原文。 |
| `NBTRIAGE_REFERENCE_RETENTION_SECONDS` | `900` | 出站引用可以被 Reply 命中的最长秒数；过期 Reply 按无可信引用处理，不能据此建立 Incident。它不控制 Thread 总寿命。 |
| `NBTRIAGE_THREAD_IDLE_SECONDS` | `900` | Thread 在没有新一轮成功续接时可以保持空闲的秒数；命中续问会延长空闲期限，但不能越过绝对期限。 |
| `NBTRIAGE_THREAD_ABSOLUTE_SECONDS` | `1800` | Thread 从创建起不可延长的总寿命；到期后的 Reply 按新的 `triage` 请求处理。该值不能短于空闲期限。 |
| `NBTRIAGE_THREAD_MAX_ENTRIES` | `4096` | 当前进程最多保存的短期 Thread 数量；容量满时旧状态会被淘汰，重启后全部清空，不是持久会话存储。 |
| `NBTRIAGE_INCIDENT_MAX_ENTRIES` | `256` | 当前进程最多保存的短期 Incident/活跃 trial 数量；容量满时旧项被淘汰，查询可能返回未找到。它不是数据库容量或持久工单上限。 |
| `NBTRIAGE_INCIDENT_RETENTION_SECONDS` | `86400` | Incident 与活跃 trial 在当前进程中可查询、反馈和汇总的最长秒数；过期或重启后维护命令不再命中。已写入 observe JSONL 的最小审计事件不由该值删除。 |
| `NBTRIAGE_TRIAL_MODE` | `off` | `off` 不创建 trial sink、不解析 LocalStore data 路径，也不写观察型事件；`observe` 把受理、查询、反馈和统计所需的最小审计事件写入 LocalStore data。它不启用模型，也不放宽 Incident 证据门。 |
| `NBTRIAGE_TRIAL_LOG_MAX_BYTES` | `10485760` | `observe` 模式下单个 `trial-events.jsonl` 文件轮转前允许的最大字节数；达到上限后按备份数量轮转。`off` 时不使用该值。 |
| `NBTRIAGE_TRIAL_LOG_BACKUP_COUNT` | `5` | `observe` 模式下轮转 JSONL 最多保留的历史备份数；超出的最旧备份被轮转策略删除。它不改变内存 Incident 的数量或寿命。 |
| `NBTRIAGE_KNOWLEDGE_PACK_URL` | 未设置 | 与 SHA-256 成对指定经过发布审核的 HTTPS knowledge pack 资产；未设置时启动后明确进入 no-knowledge 模式，不下载内容。配置后在后台下载到 LocalStore cache，安装失败会降级且不阻止 Bot 启动。 |
| `NBTRIAGE_KNOWLEDGE_PACK_SHA256` | 未设置 | 与 URL 成对固定 knowledge pack 压缩包的 64 位十六进制 SHA-256；下载内容不匹配时拒绝安装并继续 no-knowledge 模式。它校验制品身份，不表示制品来源或许可证已自动获准。 |
| `NBTRIAGE_MODEL_BACKEND` | 未设置 | 与 `NBTRIAGE_MODEL_NAME` 成对选择语义模型 transport；未设置时每轮语义判断保守 abstain。当前已准入值为 `opencode-go-chat`。 |
| `NBTRIAGE_MODEL_NAME` | 未设置 | 与 backend 成对选择已通过精确任务资格和评测的模型；当前已准入值为 `deepseek-v4-flash`，其他名称会在启动时拒绝。 |
| `NBTRIAGE_MODEL_TIMEOUT_SECONDS` | `60` | 单次语义、公开能力回答或自动教学注释请求的最长等待时间；当前 OpenCode Go 运行合同要求精确为 60 秒，三类请求都不自动重试。语义请求超时会 abstain，回答或注释超时会退回确定性能力说明。 |
| `NBTRIAGE_MODEL_MAX_OUTPUT_TOKENS` | `240` | 单次模型结构化输出的 token 上限；语义 assessment、Answer Agent 与自动教学注释都使用该值。默认值即当前 OpenCode Go 运行合同值，不匹配时启动失败。它不限制用户输入长度，也不是累计费用预算。 |
| `NBTRIAGE_RESTRICTED_CONFIG` | `[]` | JSON 数组，列出禁止把实际值交给能力分析模型的 NoneBot 顶层配置键；键名大小写不敏感，`FOO__BAR` 等嵌套写法按顶层 `foo` 整项限制。命中后在读取实际值前拒绝；它不会删除 NoneBot 配置、禁止分析公开 schema/源码，也不表示未列出的整份 `.env` 会被发送。 |

OpenCode Go 配置示例：

```dotenv
NBTRIAGE_MODEL_BACKEND=opencode-go-chat
NBTRIAGE_MODEL_NAME=deepseek-v4-flash
NBTRIAGE_MODEL_TIMEOUT_SECONDS=60
NBTRIAGE_MODEL_MAX_OUTPUT_TOKENS=240
NBTRIAGE_CAPABILITY_ANNOTATION_MODE=auto
```

密钥只从进程环境变量 `OPENCODE_API_KEY` 读取，不写入 `NBTriageConfig`。语义 assessment 只发送当前单条、
规范化并通过秘密守门的 `triage` 请求文字；公开能力 Answer Agent 另发送同一问题与本轮已经过滤为 public
的能力名、描述、用法或示例。这两类在线回答请求都不会发送 Reply / Thread 历史、身份、配置、环境变量、
日志、源码、运行证据、证据位置或 restricted 能力。每个 Agent 各最多一次请求、零自动重试，不切换模型。
语义失败会 abstain；回答失败或非法引用会退回确定性模板。语义客户端使用 Pydantic AI
`Agent(output_type=SupportSemanticAssessment)`；当前
OpenCode Go Profile 以 `final_result` output tool 承载闭合结果，该 tool 只用于结构化返回，插件不会把它
升级为业务工具或副作用授权。

Answer Agent 使用 `Agent(output_type=PublicGuidanceAnswer)`，输出最多 1000 字回答及实际使用的公开事实 ID；
它不执行命令，也不能把模型文本升级为工具或授权。该回答任务已经完成闭合 schema、隐私守门、假 HTTP
wire、Handler 回归和一次最小真实 Provider smoke，可用于当前 Bot 的受控在线测试；尚未完成独立真实模型
held-out 回答质量 Gate，不是广泛生产承诺。

当前语义字段的中文对应为：`guidance`（公开能力指导）、`behavior_exploration`（行为探索）、
`incident_intake`（故障受理）、`feature_feedback`（功能建议）；另有
`reported_observation`（用户陈述当前或过去真实发生过 Bot 行为）。公开能力、语法、参数、公开角色、场景和
前提由 guidance 回答；需要源码、内部配置、环境、版本、调用流或运行证据的内部原因进入 behavior
exploration。分类不接收身份，选中行为探索后才执行模型外 `SUPERUSER` 鉴权。

`observe` 模式的审计事件固定写入 LocalStore 为本插件解析出的 data 目录下
`trial-events.jsonl`；部署者如需更换目录，使用 LocalStore 的 `LOCALSTORE_PLUGIN_DATA_DIR`，插件不再提供
独立日志路径配置。旧 `NBTRIAGE_TRIAL_LOG_PATH` 会在初始化时给出迁移错误；既有
`logs/nbtriage-trials.jsonl` 不会自动迁移、合并或读取。

`NBTRIAGE_RESTRICTED_CONFIG` 的 JSON 数组格式示例：

```dotenv
NBTRIAGE_RESTRICTED_CONFIG='["DISCORD_BOTS", "PLUGIN_COOKIE"]'
```

`NBTRIAGE_CAPABILITY_ANNOTATION_MODE=auto` 是另一条显式选择的数据边界：后台注释任务每轮最多分析 16
个当前能力，并从当前 runtime
snapshot 出发，只读取已经加载模块中与公开命令直接关联的有界函数源码，并在读取配置值前应用
`NBTRIAGE_RESTRICTED_CONFIG`；它不会扫描整份 `.env`、完整 Config、日志、消息、用户身份或进程环境。
模型输出必须引用本轮 Evidence，且在写入 LocalStore cache 前删除 Evidence ID、源码位置和配置符号；cache
只保存公开教学文本与证据指纹。旧 cache 只有在能力仍于当前 runtime 成功注册且指纹一致时才能提供，因此
插件加载失败或本轮未观察到的能力不会成为普通用户可见的“幽灵帮助”。该自动注释任务目前仅按受控
dogfood 合同开放，尚未完成独立真实模型 held-out 质量 Gate。

## 使用

普通用户入口可以在私聊、群聊或频道直接发送，也可以 `@Bot` 后发送；三种会话使用相同分流和调用者
鉴权规则。私聊目前不能建立故障记录，维护命令仍需要 `@Bot`。未配置合格 semantic transport 时，下表的
`triage` 场景会保守澄清；配置上述 OpenCode Go 精确组合后，router 才能按模型 signals 进入已实现分支。

| 指令                                              | 权限      | 说明                           |
| ------------------------------------------------- | --------- | ------------------------------ |
| `triage 某个功能怎么使用`                         | 所有人    | 说明当前平台确定公开的功能     |
| 回复 Triage 回答并发送 `triage <续问>`            | 所有人    | 有效 Reply 命中时续接短期 Thread |
| `triage <公开能力问题>`                            | 所有人    | 检索当前平台可安全说明的能力   |
| `triage <内部行为探索问题>`                        | SUPERUSER | 鉴权后进入行为探索候选；完整取证仍在实施 |
| `@Bot 报错查询 <受理编号>`                        | SUPERUSER | 查看短期运行摘要               |
| `@Bot 报错反馈 <受理编号> <有用\|不完整\|不正确>` | SUPERUSER | 为观察型试运行记录反馈         |
| `@Bot 报错统计`                                   | SUPERUSER | 查看当前试运行统计             |

跨平台命令、结构化 Reply / Target 与回复发送由 Alconna / UniSeg 提供；Thread 是否可续接仍由插件自己的
HMAC 索引校验作用域、有效期和最近回答。Thread 出站结算只接受当前 Matcher 的单条、同 Bot / adapter /
Target 且平台结构合法的 Receipt；当前验证范围为 OneBot V11 与 Discord。OneBot 的全局出站 Provider 仍只
负责运行证据 correlation，不再结算 Thread。其他适配器可以提交 `triage` 求助，但尚不保证续接。

### 本地能力影子索引

能力影子默认启用，并使用 LocalStore 管理的插件 cache；部署者不需要也不能通过 Triage 配置指定 SQLite
路径。它是可删除重建的派生索引，不是用户数据或权威状态；文件位置、文件名和数据库格式不属于公开合同。

启动钩子只调度后台刷新，不等待制品扫描或索引构建；实际工作通过线程执行，不阻塞 Bot 启动关键路径。
后台任务读取标准 `pyproject.toml` 的 NoneBot 声明、安装制品 revision 和实际已加载模块做
`registered / not_observed / runtime_only` 协调，再从已加载的 Plugin、Matcher、Alconna 结构、插件元数据
和轻量本地源码摘要原子生成全文索引。同一轮 deployment 构建只枚举一次 distribution package map。每个
命令或 Matcher 保持独立记录；基础索引不分析 handler 效果、推断跨 Matcher 角色，也不做逐文件模块源码对齐。它不调用
第三方 Rule、Permission、handler 或命令解析，也不读取
`.env`、日志和运行数据。每条记录分别保存 `public / restricted` 受众、`all / explicit / unknown`
平台范围、具体 `analysis_issues`、执行约束和记录状态。确定命令入口、当前 adapter 在范围内、没有分析问题、
记录状态不是 `conflicted / stale`、快照明确完整且索引新鲜时才进入普通用户检索。启用自动教学注释时，
源码分析只补充满足这些运行时条件的现有记录，不会把静态扫描结果升级成“当前可用”事实。动态、被动、冲突或证据
不足的能力会保留对应 issue，不要求部署者逐命令审核。代表部署开发 / 维护者的 `SUPERUSER`、
`CommandMeta.hide=True`
或停用能力会以 `restricted` 写入本地索引，但普通检索不会返回。只有先在模型外确认
当前调用者有权查看的路径，才能检索这部分能力。Token、配置原文和私密日志不是能力，始终从采集源排除；
部署者以后也可以通过独立的 operator exclude policy 在持久化前完全排除某些能力。

源码仓库中的维护命令可以检索该索引：

```bash
just maintainer search-capabilities "搜图怎么用" \
  --index data/nbtriage-capabilities.sqlite3 \
  --include-unresolved
```

本地维护者已经在模型外确认自己有权查看当前部署的内部能力时，可以额外使用 `--include-restricted`。CLI
开关只是声明带外授权，不自行检查身份；语义 router 选中行为探索后，私聊、群聊和频道中的 `triage` 都会
对当前 Bot / Event 的请求者执行同一 NoneBot `SUPERUSER` 检查。OpenCode Go transport 已可让 guidance、
behavior exploration 与 incident signals 进入 router；behavior candidate 的鉴权已接入，但取证和解释编排
仍未实现，因此当前只返回有界状态，不读取 restricted 索引。

这条检索链不依赖模型、网络或向量服务。首次后台构建尚未发布可服务 generation 时，普通用户继续回退
显式 Provider；发布后也只检索派生 ServingView 中符合上述条件的能力。
维护者 CLI 还可以显式查看带具体 `analysis_issues` 的未解决能力和 `restricted` 能力；维护者结果报告实际 issue，
不把它们笼统称为待审核候选。索引缺少可靠用法或存在
不透明规则时不会补写参数，也不会把“发现到”宣称为“当前一定能执行”。启动刷新失败但仍有上一份成功构建
索引时，维护者回复会明确标记快照陈旧；第三方说明中的 mention 和 Unicode 控制字符会在发送前中和。

## 许可证

本项目使用 [MIT License](LICENSE)。
