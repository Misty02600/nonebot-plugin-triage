# ADR-0059：跨 Agent 链路共享只读证据访问工具

| 状态 | 决策日期 |
|---|---|
| 已采纳；教学 Agent 已接线，Bug 复用与真实模型重新资格仍待完成 | 2026-08-14 |

## 当时遇到了什么

教学注释需要从本轮已加载 Matcher 出发，补读插件源码、依赖定义和与行为直接相关的配置；Bug 分析以后也
会读取更广的源码与日志。如果每条链路分别实现路径解析、文件搜索、Jedi、LocalStore 和配置读取，就会产生
不同的 `.env` 边界、源码 revision、失败语义和模型工具面。

项目作者同时明确了几项产品要求：

- 文件发现和正文读取采用 Pydantic AI Harness 的 FileSystem 原语；
- 依赖定义采用 Direct Jedi，Griffe 直接退出项目自有实现；
- `.env` 永远不能被源码或文件 Agent 读取；
- LocalStore 目录可以成为可查询根，不要求部署者逐文件维护 allow-list；
- 当前解释器环境内安装的 Python 依赖源码可以被导航；
- 日志不能全局永久封禁，因为 Bug 分析会消费与案件关联、经过清理的日志；
- 工具必须由教学、Bug 和后续链路复用，不能只写在某个 Agent Prompt 中。

## 决策

### 一套共享领域接口，多个任务策略

1. 新增与教学、Bug 产品语义无关的 `nbtriage.readonly_tools`。它统一拥有：
   - 逻辑根身份、相对 locator、真实路径 containment、源码 revision 和失败类型；
   - FileSystem 只读工具投影；
   - Jedi `go_to_definition`；
   - 全局硬拒绝与任务级拒绝的合并规则。
2. 教学、Bug 和以后新增的 Agent 只选择任务策略和本轮批准根，不得各自绕过共享门禁或直接使用第三方
   默认工具面。
3. 第三方后端返回的位置、正文或错误不是项目 Evidence。只有重新经过根归属、revision、预算和正文净化
   后，才能被上层冻结为 Evidence。

### 路径采用“显式根 + 根内拒绝规则”

4. 不把整个磁盘作为一个根。宿主先显式建立本轮可用的逻辑根；根内文件采用硬拒绝、任务拒绝和部署者
   附加拒绝 pattern，而不是要求部署者逐文件批准。
5. 可建立的根包括：
   - Bot 项目根，用于获准的项目源码和普通文本配置；
   - NoneBot `pyproject.toml` 中声明的 `plugin_dirs`；
   - 当前成功加载插件经 `module.__path__` / `__file__` 反查、并由声明目录、editable 元数据或安装分发归属
     复核后的单插件源码根；
   - LocalStore 解析出的 config、data、cache 根；
   - 当前 Bot 解释器由 `sysconfig` 给出的 `purelib` / `platlib` 中的 `.py` / `.pyi` 依赖源码。
6. `plugin_dirs` 是可复用的声明边界，但不是唯一来源。nonemigut 当前把本地插件作为 uv workspace 的 PEP
   660 editable distribution 安装且 `plugin_dirs=[]`，因此必须同时支持“已加载模块 + 当前环境 distribution /
   editable 根”的复核；workspace 中存在但本轮未加载的插件不能因此进入普通教学能力。
7. `.venv` 不是交给模型自由遍历的单一文件根。“允许全部依赖代码”指当前解释器 `purelib` / `platlib`
   中的 Python 源码均有资格成为 Jedi 定义结果；这些根只加入 navigation profile，不加入 FileSystem
   profile。模型仍须从一个已批准的使用位置导航，不能用 glob 枚举整个环境、读取 Scripts、元数据、
   凭据或任意非 Python 数据文件。

### 永久硬拒绝与任务拒绝分开

8. `.env`、`.env.*`、`.envrc`、私钥和明确凭据文件属于不可由部署配置解除的全局硬拒绝。Pydantic AI
   Harness 的 `protected_patterns` 只禁止写入，不能阻止精确 `read_file`，所以这些路径必须进入
   `denied_patterns`，并由 Triage 在 realpath 后再次校验。
9. 部署者可以追加拒绝 pattern，不能移除内置硬拒绝。模型不能修改 pattern，也不能把绝对路径、`..`、
   symlink 外跳或未登记根变成新的查询范围。
10. 日志不进入全局硬拒绝。教学注释任务默认拒绝日志、聊天记录、用户上传、人工 Migut Help YAML、评测
    Gold 和本任务生成的 help-display；Bug 任务只能通过其已有的 correlation、脱敏、subject 和预算门禁读取
    与案件相关的日志正文。允许 Bug 读取日志不等于允许任意 Agent 遍历全部日志目录。
11. SQLite、WAL/SHM、二进制缓存等不通过文本文件工具直接发送给模型；需要时由领域 repository 查询后投影
    有界结构化事实。

### FileSystem 只保留读取工具

12. 复用 Pydantic AI Harness `FileSystem` 的 containment、symlink 解析、binary detection 和有界读取，但在
    交给 Agent 前过滤为：`list_directory`、`find_files`、`search_files`、`read_file`、`file_info`。
13. `write_file`、`edit_file`、`create_directory` 永不进入这个共享工具集；Shell、进程、项目切换、memory
    和任意规则提交也不属于该接口。
14. 多个逻辑根分别创建受控 FileSystem，并使用稳定根 ID 前缀工具名，避免同名冲突。上层仍只把本任务真正
    需要的根装配给 Agent。

### Jedi 只提供转到定义

15. Jedi `Script.goto(follow_imports=True)` 的领域名称固定为 `go_to_definition`：从 ast-grep、runtime、
    traceback 或已读源码给出的精确文件、行、列跳到定义。首版不开放 `infer`、`get_references`、
    `Project.search`、`Interpreter` 或任何 refactor API。
16. Jedi 解释器固定为当前 Bot 进程的 `sys.executable`，关闭 unsafe extension loading 与 smart sys.path；
    模型和普通配置不能指定解释器。返回路径必须再次命中批准插件根或当前解释器的 Python 依赖源码根，
    并绑定定义文件实际字节 revision；无结果、多个结果、越界、revision 冲突和后端异常分别保留失败语义。

### 当前有效配置从内存实例读取

17. 文件中声明的配置只能形成静态候选。当前有效值优先来自运行进程中插件已经构造并实际持有的 Pydantic
    配置实例。共享读取器只接受确定性分析预先登记的 `reference_id`，在读取前校验 owner module、精确
    config type、字段 alias 和 source revision，再复用 `ConfigValuePolicy` 与 `project_config_values()`。
18. 读取器不得枚举配置、重新调用 `get_plugin_config()`、运行 validator / property / serializer、调用
    `model_dump()`、读取 `.env` 或枚举 `os.environ`。值只进入本次获准模型上下文，不持久化、不写日志、
    不进入教学 YAML；找不到已构造实例时返回 unknown，而不是重建配置。

### Griffe 退出

19. 删除项目对 `griffelib` 的直接依赖、`installed_sources.reader` 和其专属符号/关系快照。已加载插件源码根
    继续复用 distribution/RECORD 归属、runtime binding、editable 校验和越界拒绝；共享工具继续统一绑定
    源码字节 revision。这些是 Triage 的安全合同，不属于 Griffe。
20. `pydantic-ai-slim` 自身仍可能传递依赖 Griffe；这不表示 Triage 继续把它作为源码导航后端。验收标准是
    项目不直接声明、import 或调用 Griffe。

## 为什么这样选

- 显式逻辑根阻止模型从项目需求扩张到整个磁盘，根内 blacklist 又避免维护海量逐文件 allow-list；
- `.env` 双重硬拒绝解决 Harness 默认“只保护写入、精确读取仍可读”的边界；
- Direct Jedi 与“从已知使用位置跳到依赖定义”同构，比维护第二套 API 对象树更适合当前需求；
- 内存配置实例最接近插件真正使用的值，同时可以沿用已验证的 restricted-config 和秘密投影策略；
- 日志按任务授权，既不污染普通教学，也不堵死 Bug Agent 已经采纳的运行证据路线；
- 共享领域接口让后端、预算或 pattern 调整只发生一次，并让每个消费者继续拥有独立披露和模型资格。

## 没有采用的方案

### 只维护全局 allow-list 到每个文件

部署者需要持续追踪每个插件和 LocalStore 文件，维护成本高且容易陈旧。项目只要求根身份显式批准，根内再
使用内置与部署者附加的拒绝 pattern。

### 只维护 deny-list 并允许任意磁盘路径

新出现的敏感文件名无法被预知，且模型可以把问题扩张到 Bot 之外。deny-list 不是根 containment 的替代品。

### 把 `.venv` 整棵作为 Agent 文件根

它会同时暴露 Scripts、dist-info、二进制和与当前问题无关的数百个包。当前环境依赖源码通过 distribution
归属和 Jedi 定义导航按需进入证据，不自由漫游。

### 使用调试器或通用 Python 对象检查器读取配置

这会允许属性访问、函数执行和任意内存枚举。项目只读取宿主已登记的 Pydantic 实例存储字段，并沿用现有
值策略。

## 带来的影响

- `pydantic-ai-harness==0.20.0` 与 `jedi==0.20.0` 固定为模型 extra 的依赖；为满足 Harness
  的兼容边界，模型栈统一升级并精确锁定到 `pydantic-ai-slim==2.28.0`。零模型基础插件不会因此安装
  Agent 工具栈；
- Pydantic AI Harness 仍为 0.x，项目必须精确锁版本并由薄适配吸收 API 变化；
- 建立根与运行配置 reference 的权力只属于宿主适配层，不能交给模型；
- 教学 Agent 已补工具预算、出站投影、动态 Evidence revision 清单和本地合同测试；真实 Provider held-out
  通过前仍只用于受控 dogfood。Bug Agent 尚未切换到共享 Jedi/FileSystem 后端；
- 以后可增加只读引用或调用关系工具，但必须继续经过相同根、revision、Evidence 和任务披露边界。

## 与既有决定的关系

- 落实 [ADR-0057](0057-select-source-analysis-tools-by-evidence-stage.md) 的 Direct Jedi 选择和 Griffe 退出；
- 具体化 [ADR-0058](0058-use-deterministic-evidence-and-bounded-navigation-for-teaching-annotations.md)
  的 `SourceNavigator` 边界，但不改变教学 Agent 尚需重新资格的事实；
- 延续 [ADR-0029](0029-control-model-config-values-with-deployment-deny-list.md) 的配置读取前 deny-list 与
  `.env` 禁止；
- 不扩大 [ADR-0053](0053-allow-relevant-source-and-log-bodies-for-bug-assessment.md) 的日志准入，Bug 仍须
  correlation 与秘密清理；
- 不改变 [ADR-0055](0055-use-ast-grep-for-matcher-source-shape-extraction.md) 的 Matcher CST 职责。

## 官方能力依据

- [Pydantic AI Harness FileSystem](https://pydantic.dev/docs/ai/harness/filesystem/)：固定根 containment、
  symlink 处理、读写工具和 pattern 的精确语义；
- [Pydantic AI Toolsets](https://pydantic.dev/docs/ai/tools-toolsets/toolsets/)：toolset 过滤、组合与前缀；
- [Jedi API Overview](https://jedi.readthedocs.io/en/latest/docs/api.html)：`Script.goto`、Project 和环境参数；
- [Jedi Security](https://jedi.readthedocs.io/en/stable/docs/features.html#security)：unsafe extension 与静态分析
  的安全边界。
