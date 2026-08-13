# ADR-0039：用 Griffe 静态读取已安装公共框架源码

| 状态 | 决策日期 |
|---|---|
| 已采纳；基础领域切片已实现，尚未接入产品运行入口 | 2026-08-13 |

## 当时遇到了什么

未来的 Bug 诊断和已鉴权行为探索不仅需要插件 Matcher 事实，还需要理解当前部署安装的 NoneBot、适配器、
Alconna、Uninfo 等公共框架或工具库。它们通常以 wheel、VCS 或 editable 分发形式安装在 Bot 所使用的
Python 环境中，并保留可读 `.py` / `.pyi`；直接使用这些源码可以比预先打包多版本框架源码更准确地对应
当前部署。

已有 `capability_source_evidence` 面向插件能力发现，从 Matcher 注册位置提取 handler、配置、Rule 和
Permission 候选。公共框架源码阅读的目标则是提供包级 API 树、签名、docstring、公开 alias、定义位置和
有限的源码关系，供多个诊断目的复用。两者的数据语义、受众和失效范围不同，不能通过不断扩张 Matcher
提取器来实现。

自行实现完整 Python API 模型、别名解析、`.pyi` 合并和各种源码布局会重复成熟第三方能力；反过来，直接
import 或 introspect 第三方包会执行初始化代码，不符合只读证据边界。

## 决策

1. 新增独立的 `nbtriage.installed_sources` 领域子系统。它只处理批准清单中的公共框架或工具分发包，不并入
   capability shadow，不改变 Matcher 能力事实 schema，也不扫描整个 `site-packages`、Bot 项目或相邻仓库。
2. 使用 `griffelib` 作为 Python API 静态读取器，负责模块、类、函数、属性、类型别名、签名、docstring、
   源码范围和包内 alias。产品依赖精确固定到经过验证的版本；loader 必须设置
   `allow_inspection=False`、`force_inspection=False`，不得因源码缺失自动转为 runtime introspection。
3. 分发包定位和内容 revision 仍由本项目控制：从当前 Bot 解释器的 `importlib.metadata.Distribution` 读取明确
   distribution 的文件清单；普通安装直接读取清单中现有的 `.py` / `.pyi`。版本号、VCS commit 和
   `RECORD` 只作来源证据，不能替代对实际源码字节逐文件计算的 SHA-256。
4. 若目标顶层包已经加载，只读取 `sys.modules[import_name].__spec__` 的 `origin` 或
   `submodule_search_locations` 来核对当前进程真实使用的位置；不读取运行对象源码，也不调用
   `inspect.getsource()`。运行位置与 distribution 文件入口一致时标为 `runtime_bound`，不一致时标为
   `conflicted` 并失败关闭。未加载的普通 wheel / VCS 仍可标为 `installed_only`；不会为定位而主动 import。
5. editable 文件清单没有源码时，只允许“顶层包已经加载、运行位置位于 `direct_url.json` 记录的项目根”这条
   可验证路径。未加载时不猜 `src/<包名>`、`.pth`、代理模块或符号链接布局，返回 `unresolved` 并退回公共
   文档知识。`direct_url.json` 中的本机 URL 不进入模型、日志或持久结果。
6. 文件清单缺失、源码不可读、native / bytecode-only、namespace 多位置、路径歧义、越界、大小超限和解析
   错误都失败关闭或标为 partial，并退回公共文档知识，不阻断 Bot。
7. Griffe 之外只实现诊断所需的最小静态关系：包内 `contains`、已解析 alias 和 AST 可见的直接调用候选。
   调用关系区分 `precise / candidate / opaque`，不声称构建完整 Python 调用图，不解释反射、动态 import、
   monkey patch、元类或运行期派发。
8. 读取接口只暴露稳定 symbol / evidence ID、组件、distribution、版本、相对路径、源码范围和内容摘要。模型
   只能搜索符号、查看已返回的 symbol Evidence、展开白名单关系，不能提交任意绝对路径或 glob 请求。
9. 这些公共框架源码对所有请求者都属于可公开知识，但仍按请求目的选择最小片段；“可公开”不等于把整份
   包发送给模型。若安装源码被本地修改、来自未知 fork 或以后纳入私有部署源码，必须由独立外发策略处理，
   不能沿用公共框架默认值。
10. 源码只形成静态实现证据，不证明当前分支执行、配置生效、Permission / Rule 通过或外部 API 成功。Bug
   判断仍需与版本适用的文档合同、部署环境和运行观察交叉；行为探索必须把静态推导与已观察行为分开。
11. 首版不引入 SCIP、Jedi、LibCST、CodeQL 或向量源码索引。只有本地评测证明 Griffe + 有界 AST 无法达到
   必要的跨文件导航或数据流质量时，才把 SCIP 或 CodeQL 作为可替换的离线增强重新评审，不进入普通 Bot
   热路径。

## 为什么这样选

- Griffe 已经提供稳定的 Python API 对象模型、静态加载、alias、签名、docstring 和源码范围，避免重复实现
  Python 文档工具链；
- 明确关闭 introspection 后可以读取安装源码而不执行 NoneBot、适配器或插件初始化；
- 本项目继续掌握文件归属、内容修订、边界、预算和 Evidence ID，避免第三方 loader 的默认搜索或动态回退
  扩大数据面；
- 公共框架阅读与插件 Matcher 抽取隔离后，两者可以分别演进和复用，不会把框架源码伪装成某项能力已经在
  当前部署注册或执行；
- 有界 AST 足以补足首批直接调用路径，而复杂 Python 派发本来就不能安全地提升为当前运行事实。

## 没有采用的方案

### 扩张现有 Matcher 源码提取器

它会把“插件入口和能力候选”与“公共框架 API 导航”混成同一 schema，导致受众、缓存、失效和证据性质都
难以区分。

### 完全自行实现 Python 包 API 索引

需要重复处理 re-export、alias、stub、签名、docstring、源码位置和多种布局，维护成本高且收益有限。

### 通过 import / inspect 获取最准确信息

这会执行第三方顶层初始化和动态对象构造，可能联网、注册 Matcher、启动任务或产生其他副作用。

### 第一版直接采用 SCIP 或 CodeQL

SCIP 更适合高精度跨文件代码导航，CodeQL 更适合控制流和数据流深查；两者均会显著增加构建环境、工具链
和索引成本。当前最小用途尚未证明需要这些能力。

## 带来的影响

- 基础安装面新增精确固定的 `griffelib` 依赖；
- 领域核心新增可独立测试的安装源码 resolver、静态 reader、符号搜索、Evidence 查看和关系展开；
- 知识包不再需要为每个受支持 Python 框架版本复制完整 runtime 源码，但仍须包含官方文档、迁移说明和
  源码不可用时的回退证据；
- 运行时 LocalStore 缓存、后台构建、ServingView、Agent typed action、启动提示和 BugFinding 引用 pin
  尚未接入，不能把本 ADR 描述为已上线的用户功能。

## 落实与确认

- `src/nbtriage/installed_sources/` 已实现独立模型、distribution 文件归属、实际源码字节 revision、Griffe
  静态 API 读取、符号检索、受控 Evidence 和有限关系；
- `tests/installed_sources/` 覆盖静态 API/调用关系、同版本源码变化失效、未解析外部 alias、越界路径拒绝和
  editable URL 不泄露、已加载位置绑定、shadowing 冲突和未加载 editable 不猜路径，以及当前 NoneBot /
  Alconna 真实安装；
- 尚未实现 NoneBot 启动适配、LocalStore 持久索引、Uninfo 实际部署 fixture、Agent 工具接线、公共知识包
  构建器和 TypeScript / NapCat reader。

## 相关决定

- 补充 [ADR-0019](0019-distribute-rag-corpus-as-versioned-knowledge-pack.md) 的 Python 框架源码边界；
- 延续 [ADR-0021](0021-use-deployment-local-capability-shadow-index.md) 的本地、可删除重建和失败保留边界，但
  不把本索引并入 capability shadow；
- 落实 [ADR-0025](0025-explain-plugin-behavior-from-deployment-evidence.md) 中源码只作静态证据、不得执行
  第三方逻辑的决定；
- 不改变 [ADR-0026](0026-filter-capability-knowledge-before-retrieval.md) 的检索前受众隔离和模型数据准入。
