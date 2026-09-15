# YetAnotherPicSearch 复现环境

这是独立的最小 NoneBot 测试项目，用于复现搜图故障和取得真实证据。目标插件为
[YetAnotherPicSearch](https://github.com/lgc-NB2Dev/YetAnotherPicSearch)，初始基线使用
PyPI 发布版 `2.0.10`、Python 3.12 和 OneBot V11；这不是对原 Bot 环境的还原。
依赖由本目录的 `pyproject.toml`、`uv.lock` 和 `.venv` 管理，不加入 triage 的 workspace。

## 安装与加载检查

在本目录执行：

```powershell
uv sync --locked
uv run --locked python bot.py --check-load
```

加载检查使用明确的占位 SauceNAO Key，加载实际插件后退出，不启动服务器、连接 Bot 或发起搜索。
通过此检查只证明依赖和插件可以导入，不证明搜图正常，也不代表已复现故障。

## 真实复现

1. 复制 `.env.example` 为 `.env`，填入 SauceNAO Key 和测试连接的访问令牌。
   插件加载只校验 Key 非空，默认 `/搜图` 使用 SauceNAO，因此该路径需要有效 Key。
   如果专门测试 `/搜图 --a2d`，可以填写明确的非空占位值；Ascii2D 分支不使用这个 Key。
   不要把占位 Key 引起的 SauceNAO 失败算作原始故障。
2. 准备独立的 OneBot V11 测试连接，将反向 WebSocket 地址设为
   `ws://127.0.0.1:18080/onebot/v11/ws`，两端使用相同令牌。服务默认只监听本机。
3. 执行 `uv run --locked python bot.py`，在测试会话中发送 `/搜图 -h` 检查帮助，
   再发送或回复测试图片，使用实际出错的搜图指令。
4. 记录完整指令、原始图片或可复核来源、消息时间、实际回复和预期行为，保留对应日志。
   每次启动的插件日志写入 `artifacts/<UTC时间>/bot.log`；OneBot 实现自己的日志需另外保留。

## NoneBug 模拟测试

无需配置 `.env`、真实 Key 或 OneBot 连接，在本目录执行：

```powershell
uv run --locked --group test pytest -q
```

测试加载已安装的真实插件，通过 NoneBug 输入模拟群消息并检查 Bot 发送操作。
目前有两个消息入口测试：

- 发送 `/搜图 -h`，验证插件回复自带的帮助图片并结束本次处理。
- 发送 `/搜图` 并附图，模拟附件下载和 SauceNAO 的成功 HTTP 响应，执行真实的消息解析、
  默认搜索分发、依赖库响应解析和结果生成，检查回复中的来源、标题及回复消息关联。
  测试只把搜索进度提示的后台撤回改为等待完成，避免清理任务跨越测试上下文。

配置使用占位 Key，插件缓存写入测试临时目录，不使用真实复现的缓存。
这些测试不访问外部搜图服务，也不验证消息是否能真正送达聊天平台。

另有两个源码问题刻画测试（`tests/test_saucenao_limits.py`）：

- 依赖库将 HTTP 状态码保存在 `status_code`，正文状态保存在 `status`。合成响应的 HTTP 状态
  为 429、正文状态为 -1 时，插件绕过重试判断，直接返回 Ascii2D 回退。
- 人为令正文状态也等于 429，触发插件现有重试条件后，实际协程等待链中出现两个包装层持有
  同一个锁，内层停在 `Lock.acquire`，仅发出一次 HTTP 请求；测试最后取消任务以释放锁。

这两个测试通过表示已观察到这些代码行为，不表示 Bug 修好了。所有限流响应均为合成输入，
特别是正文 `status=429` 仅用于检查分支，尚未证明真实服务会返回这种正文，也没有确认它们
就是用户原来遇到的故障。不要把这组结果直接作为“真实 Bug 调查通过”的评测结论。

后续可以只模拟平台事件和发送 API，保留搜图服务的真实 HTTP 请求；也可以回放固定 HTTP 响应，
检查插件解析和错误处理。两类结果应分别记录，人工构造的服务错误不能充当已发现的真实故障。

## 捕获死锁与检查 Bug 工具

在本目录执行：

```powershell
uv run --locked --group test pytest -q tests/test_deadlock_capture.py -s
```

用 NoneBug 发送默认 `/搜图` 和模拟图片附件，记录插件实际发送的进度提示。
HTTP 层提供明确标注的合成限流响应，实际执行依赖解析和插件重试。
确认同一把锁上的递归等待后，在取消任务前保存以下内容：

- `capture.json`：模拟用户消息、实际 Bot 回复、关联 ID、HTTP 条件、协程等待快照、环境版本。
- `plugin.log`：该消息处理窗口内实际输出的 INFO 及以上日志。
- `source/`：本次执行的插件 Python 源码快照，每个文件记录 SHA256。

输出路径为 `artifacts/deadlock-<UTC时间>/`。采集后显式取消被 NoneBot 保护的处理任务并清理；
清理导致的撤回和完成日志不放入故障观察窗口。窗口内未抛异常时，不编造异常堆栈。

回到 triage 仓库根目录，使用仓库维护环境检查工具接线（将路径替换成此次输出）：

```powershell
uv run python -m tools.nbtriage_maintainer.bug_repro_replay evals/repro/search-image/artifacts/deadlock-<UTC时间>/capture.json
```

该入口校验文件哈希，并将捕获数据绑定到一次 Bug 工具箱。它通过实际 Pydantic AI Agent 调度
`read_runtime_evidence`、`read_correlated_logs`、`read_conversation_context`、
`read_deployment_context` 和插件源码读取工具，结果写入同目录的 `tool-access.json`。
会话只包含这次模拟群的三条消息；Bot 文本来自实际发送调用，用户命令及报障问句为合成输入。

这是评测回放接线：普通日志由回放 loader 提供，未修改正式程序主要保存异常的日志缓冲区；
运行快照由测试观察器采集，未增加线上协程采集功能。源码读取只开放此次插件快照，未启用
外部依赖跳转。`current` 表示证据对该冻结复现有效，不表示旧事件仍在当前线上运行。

工具检查使用 FunctionModel 指定调用，不消耗远程模型额度、不评判诊断结果，也不验证正式
意图路由。尚未装配公开教学资料或设计 RAG，因此不能把工具接线通过等同于完整 Bug 调查通过。

## 真实模型调查

使用 triage 仓库的维护环境运行（不要在搜图子项目的虚拟环境里安装 triage 依赖）：

```powershell
uv run python evals/repro/search-image/run_investigation.py evals/repro/search-image/artifacts/deadlock-<UTC时间>/capture.json --preflight
uv run python evals/repro/search-image/run_investigation.py evals/repro/search-image/artifacts/deadlock-<UTC时间>/capture.json
```

`--preflight` 只检查数据、版本和模型密钥是否存在，不发出模型请求。正式运行使用维护环境的
`DEEPSEEK_API_KEY`，与搜图服务的 SauceNAO Key 无关。

该脚本使用 `fixtures/public-teaching.json` 中现成的教学注释作为公开合同 mock，不读取 README。
注释原文来自 2.0.4 的全量生成数据，被测插件为 2.0.10；按本次测评约定假设这份公开教学适用，
在输入和审计中明确标注 mock、两个版本及原数据哈希。注释不加入已知死锁或修复说明。
这验证的是给定教学资料后的调查与记录能力，不验证注释生成、跨版本适用性或前序意图路由；
设计 RAG 仍为空。之前使用 README 的运行保留为历史结果，不覆盖。

每次运行在捕获目录下创建独立的 `investigation-<UTC时间>/`，保存公开资料、输入与版本、
真实模型请求和响应、工具证据、程序校验后的结论以及实际格式化回复。默认沿用此前调查评测
的 DeepSeek V4 Flash / High、300 秒、12 次通用工具、60 万累计 token 上限，传输不自动重试。
不得把结构化判断正确直接等同于“已向用户解释清楚原因”；应检查 `formatted_reply`。

完整入口的 `report.json` 还保存 Bug Agent 每次 Provider 请求的工具名称列表和完整工具定义
SHA-256，不保存工具 Schema 正文。它用于核对同一调查中工具前缀是否稳定，并与 Provider 返回的
cache token 对照；哈希稳定本身不等于缓存一定命中。

真实调查结果还可以通过正式记录构建器和 ORM 写入隔离测试数据库，不再次调用模型：

```powershell
$env:NBTRIAGE_REPRO_INVESTIGATION = (Resolve-Path 'evals/repro/search-image/artifacts/deadlock-<UTC时间>/investigation-<UTC时间>/result.json').Path
uv run pytest tests/bug/test_bug_workflow.py::test_captured_real_investigation_records_and_reads_back -q
Remove-Item Env:NBTRIAGE_REPRO_INVESTIGATION
```

该检查验证真实标题、摘要和插件级范围能够建档、重复写入幂等、维护查询可读，在调查目录保存
`recording.json` 和 `maintenance.md`。它们的编号只属于测试数据库，不代表已向线上登记。
普通测试不设置该变量时跳过这一项；数据库迁移与历史兼容另有固定回归测试。

验证两次独立复现是否沿用同一问题编号时，分别设置 `NBTRIAGE_REPRO_INVESTIGATION` 和
`NBTRIAGE_REPRO_OTHER_INVESTIGATION` 为两次真实调查的 `result.json`，运行
`tests/bug/test_bug_workflow.py::test_independent_real_investigations_reuse_the_problem_id`。
它通过正式建档路径写入同一个隔离数据库，不再调用模型，并在第二次调查目录保存 `grouping.json`。
验收要求编号相同。最初两个样例因证据 revision 与 subject 被当作聚合身份而失败；随后由显式
等待路径指纹和稳定插件范围修复，两份旧调查在当前记录路径的归组回归通过。旧模型输出不改写，
不把记录回放通过表述为新增真实模型调查。

## 正式服务到记录回执的贯通测试

在维护环境显式开启付费测试，并指定已有捕获与全新输出目录：

```powershell
$env:NBTRIAGE_LIVE_REPRO_RECORDING = '1'
$env:NBTRIAGE_LIVE_REPRO_CAPTURE = (Resolve-Path 'evals/repro/search-image/artifacts/deadlock-<UTC时间>/capture.json').Path
$env:NBTRIAGE_LIVE_REPRO_OUTPUT = Join-Path (Get-Location) 'artifacts/bug-repro-runtime-<本次运行名>'
uv run pytest tests/integration/test_bug_repro_recording_live.py -q
Remove-Item Env:NBTRIAGE_LIVE_REPRO_RECORDING
Remove-Item Env:NBTRIAGE_LIVE_REPRO_CAPTURE
Remove-Item Env:NBTRIAGE_LIVE_REPRO_OUTPUT
```

测试运行真实联合 Low、公开初检、Runtime 服务、源码工具、Bug Agent、证据校验、记录构建器、ORM 与
Handler，每个场景保存 `report.json` 和独立的 `workflow.sqlite3`。确认场景使用已校验的冻结等待栈；
生产观察场景通过正式 Reply 索引读取真实插件死锁时由生产 `NoneBotRuntimeObserver` 捕获的生命周期，不注入
测试协程栈或回放日志；证据不足场景保留公开合同和会话，撤掉 runtime/log/deployment 与源码。
生产 observer 能捕获入口开始但尚不能取得具体等待栈，因此这份生命周期快照本身不能确认 Bug。生产观察场景
使用 24 次扩展评测额度；必须继续读取入口可达源码，实际证明存在与报告现象相符的不终止等待后才能确认 Bug。
旧教学 mock 仅适配到当前对象形状，条目正文保持不变，不是生产注释迁移。
普通测试默认跳过这两条付费题；`test_bug_recording_failure.py` 用无网络故障注入验证保存失败回执。

2026-09-14 一次真实贯通通过：问题/发生/报告/调查各一条，保存后发送带编号回执并关闭 Thread。
复现适配器把冻结现场时间作为 runtime/log Evidence 的显式元数据；正式记录构建器只从实际引用的
这两类现场继承最早观察时间。没有可信现场时间时 `Occurrence.observed_at` 保持未知，不用登记时间补造。

2026-09-15 真实完整入口的正反场景通过。两条均由联合 Low 识别 `bug_assessment` 并选择搜图插件，公开
初检返回 `investigate`。完整现场由 Bug Agent 判为 `implementation_contradicts_contract` 并建档；撤掉
内部现场和源码后返回 `insufficient_evidence`，五张表均零写入，用户收到缺少实际执行现场的说明，Thread
关闭。初次使用较短的报告原文时，Answer 合理追问图片与指令的发送关系；最终评测输入明确同条附图和
提示后的操作经过，不虚构等待时长。

同日补充生产观察场景。真实插件进入死锁时，当前生产 observer 在取消前记录 `event_received` 和
`matcher_started`，没有 `matcher_completed`、`event_completed` 或异常日志；正式 Reply 索引能够把后续报障
关联到这组证据。Prompt v23 明确：缓冲未丢记录不代表业务操作已经结束，有限时刻只有 started 不能单独证明
Bug；入口可达源码独立证明与报告现象相符的实现缺陷时，可以确认实现 Bug，但不能把单次历史事件的原因写成
已经证明。真实模型在 24 次扩展评测额度内读取默认模式、搜索源注册、锁实现和 SauceNAO 429 自递归，确认
`implementation_contradicts_contract` 并建档；报告保留本次事件是否命中限流分支的不确定性。冻结等待栈正例仍
确认 Bug 并建档。评测回放会平移生产观察时间并保留原始相对间隔，避免固定 900 秒生产保留期使旧捕获失效。

同日固定本轮 Bug 工具定义后再次只跑生产观察场景：12 次 Bug 请求的工具名称与完整定义哈希全部一致；
累计输入 347,919 tokens，cache read 321,536（92.4%），未命中 26,383。改动前 v8 分别为 357,049、
269,696（75.5%）和 87,353，支持“动态移除工具破坏部分前缀缓存”的判断，但两次模型轨迹不同，不能据此
计算独立节省比例。此次模型虽读到锁、装饰器和限流递归源码，最终仍返回 `unknown / insufficient_evidence`，
因此严格正例测试失败且没有建档；该结果保留为调查判断稳定性的缺口，不放宽通过条件。

## 文件与复现边界

- `pyproject.toml`、`uv.lock`、启动脚本和配置示例纳入版本管理。
- `.venv/`、`.env`、插件缓存和 `artifacts/` 保留在本地。运行日志可能包含真实消息和图片地址，
  应复核、脱敏后再选择评测证据。
- 插件加载的直接或间接依赖仍属于真实运行环境；不加载 nonemigut 或 triage 插件。
- 独立虚拟环境隔离 Python 依赖，不提供操作系统级沙箱。
- 锁文件固定依赖版本，不固定第三方搜图服务的状态。网络、凭据和服务变化应如实记录，
  不能把这些失败自动判定为插件 Bug。
- 每次复现需要记录使用的配置和已有缓存状态；新日志目录不代表插件缓存已经清空。
  如需验证旧版本，明确改动依赖并重新锁定，保留该次使用的版本记录。
