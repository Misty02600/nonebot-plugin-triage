# ADR-0056：用 Serena 可选增强 Bug 源码导航

| 状态 | 决策日期 |
|---|---|
| 已采纳；插件内符号导航纵切已实现，跨依赖 source view 待接入 | 2026-08-14 |

> 后续 ADR-0057、0059 已把通用依赖定义职责交给共享 Direct Jedi 工具并移除项目自有 Griffe reader；
> Serena 继续只保留本 ADR 的 opt-in Bug 插件内导航职责。模型 extra 随共享 Harness 接入已从下文当时的
> `pydantic-ai-slim[mcp]==2.27.0` 升级并精确锁定到 `2.28.0`。

## 当时遇到了什么

Bug Agent 已有的 `BoundedSourceReader` 只能在已批准插件根中做文本命中和整文件分块。它能守住路径、
字节数和不执行源码的边界，但不能可靠回答符号定义、跨文件引用和调用位置。项目自己的 Griffe / AST 层
负责确定性框架证据与 Matcher 形状，不应继续扩张成第三套面向开放式 Bug 调查的完整语言服务器。

Serena 提供基于语言服务器的符号查找、声明跳转和引用查找，并能通过 stdio MCP 接入现有 Pydantic AI
运行栈。它同时也默认提供编辑、Shell、memory 和项目切换工具，不能直接暴露给 Bug Agent。

## 决策

1. 保留 `bounded-text` 为默认源码后端；只有部署者显式设置
   `NBTRIAGE_BUG_SOURCE_BACKEND=serena` 时，插件才有权启动本机 Serena 子进程。
2. 插件的 `serena` extra 只安装 `pydantic-ai-slim[mcp]==2.27.0`。Serena 1.7.0 本体使用
   `uv tool install -p 3.13 serena-agent==1.7.0` 隔离安装，不进入 Bot 虚拟环境。Serena 固定
   `PyYAML 6.0.2`，与本插件要求的 `PyYAML >=6.0.3` 不能在同一解析环境共存。
3. Serena 在第一次实际源码查询时延迟启动，本轮协调结束后关闭；插件启动、历史 catalog 命中、公开合同
   短路、模型不可用或 subject 不明确时都不启动进程。缺 extra、缺 executable、握手失败、超时或结果非法
   时退回原有有界文本搜索，不阻止 Bot 启动。
4. Serena 使用随 wheel 分发的固定 context，只暴露 `find_symbol`、`find_referencing_symbols`、
   `find_declaration`、`get_symbols_overview` 和 `search_for_pattern`；不暴露编辑、Shell、memory、onboarding
   或项目切换。Pydantic AI 不把 MCP toolset 直接交给模型，Bug Agent 仍只调用 Triage 自己的源码工具。
5. 为 Serena 设置 LocalStore cache 下的隔离 `SERENA_HOME`，清空默认 editing modes，不信任宿主或被分析
   源码附带的 `.serena/project.yml`。批准根存在项目级 Serena 配置时，首版拒绝启用语义后端并退回文本
   搜索，避免其中的 activation command 或工具配置改变运行边界。
6. MCP 结果必须解析为允许字段，所有路径重新验证为批准根内的真实 `.py` 文件；绝对路径、`..`、未知根、
   未知字段和超长正文不能直接进入模型。Evidence ID 绑定工具、当前 source revision 与净化后内容摘要，
   Evidence revision 使用当前 capability 的 source revision；所有结果仍经过现有秘密清理、8 次工具调用、
   120k 字符预算和 reconciler。
7. 首个运行纵切让现有 `search_source_code` 优先按 Serena 符号查找，未命中再做文本搜索，不增加模型可见
   工具或扩大源码数据类别。后续若新增声明 / 引用专用 Agent tools，必须单独更新 Prompt、预算和模型资格。
8. 免费 LSP 后端不能直接穿透普通 `site-packages`。真实探针证明：把已批准且绑定 revision 的 NoneBot
   源码放入项目内受控 source view 后，插件到 `get_plugin_config` / `Matcher.finish` 的定义跳转和反向引用
   均成功。该 source view 尚未接入产品；在此之前，Serena 只承诺插件自身符号导航，框架源码继续由
   `installed_sources` 提供。

## 为什么这样选

- Serena 复用成熟语言服务器能力，适合 Bug Agent 按案件导航符号关系；
- 原有文本读取继续作为稳定后备，Serena 不会成为 Bot 启动或基础 wheel 的硬依赖；
- 显式配置把“安装了某个包”与“授权插件执行第三方本机进程”分开；
- 隔离进程和固定只读 context 避免为接入语义导航而放弃现有 Evidence 与副作用边界；
- 先验证插件内收益，再由真实失败样本决定 source view 范围，避免一开始把整个 `site-packages` 或仓库交给
  语言服务器和模型。

## 没有采用的方案

### 直接把 Serena MCPToolset 挂给 Bug Agent

这会绕过 Triage 的 Evidence ID、源码根、字符预算、工具次数和最终 reconciliation，也会把 Serena 默认的
编辑与 Shell 工具暴露给模型。

### 把 Serena 本体放进插件 extra

这会与当前 PyYAML 约束冲突，并把 GUI、Web、Anthropic 等大量与运行时源码导航无关的依赖装进 Bot 环境。

### 用 ast-grep 替代 Serena

[ADR-0055](0055-use-ast-grep-for-matcher-source-shape-extraction.md) 的 ast-grep 规则负责确定性 Matcher 源码
形状提取；它不提供跨文件符号身份、声明跳转或类型解析，不能替代 Bug 调查的语言服务器导航。

## 当前限制

- Serena MCP 初始化信息的 `version` 当前报告 MCP SDK 版本而不是 `serena-agent` 包版本；运行时只能复核
  官方 server identity、网站和固定工具面，精确 1.7.0 由部署安装命令和部署 lock 负责。
- 首次启动可能由 Serena 通过 uv 下载其固定的 Pyright；要求完全离线的部署必须预热该工具缓存。
- 动态 import、monkey patch、依赖注入实际值和具体消息是否执行某分支仍需运行时证据，静态符号关系不能
  独立证明因果。

## 相关文档

- [ADR-0039：已安装公共框架源码证据](0039-use-griffe-for-installed-public-framework-source-evidence.md)
- [ADR-0050：有界 Bug Agent](0050-use-a-bounded-agent-for-user-bug-assessment.md)
- [ADR-0053：相关源码与日志正文](0053-allow-relevant-source-and-log-bodies-for-bug-assessment.md)
- [模型 Provider 支持矩阵](../architecture/model-provider-support.md)
