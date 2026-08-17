# ADR-0057：选择 Direct Jedi 导航依赖定义

| 状态 | 决策日期 |
|---|---|
| 已采纳；Griffe 已退出，Direct Jedi 已接入教学链，真实模型资格待完成 | 2026-08-14 |

本 ADR 决定依赖定义导航只选择 Direct Jedi 作为语义后端，以受控 `glob` / 文本搜索和按文件读取作为
永久兜底。项目不为这一职责同时维护 Griffe、Jedi、MultiLSPy 和 Serena；后续 ADR-0059 已落实共享只读
领域接口并移除项目自有 Griffe reader，产品 Agent 接线仍需独立资格。

本决定不改变运行中 Bot 对 Matcher 与 handler 注册事实的权威地位，也不改变 ast-grep 的 Matcher 源码
形状提取职责。后续 ADR-0085 已撤销 ADR-0056 的 Serena opt-in Bug 源码纵切；当前语义导航只保留
Direct Jedi，Bug 仍使用有界文本读取。

[ADR-0058](0058-use-deterministic-evidence-and-bounded-navigation-for-teaching-annotations.md) 已经先行
定案教学注释的上游编排：确定性 Evidence Pack 之后允许经 Triage 只读领域工具按需补证，并采用插件级
源码失效。本文定案其中“依赖定义导航”的后端，不重新讨论教学链是否允许有界 Agentic 导航，也不把
这项选择自动扩张到全部 Bug 深度导航。

## 为什么需要一项跨阶段工具定位决定

项目已经出现三个需要源码事实、但精度和生命周期不同的消费者：

1. 能力与帮助索引需要从本轮成功注册的 Matcher 周围提取稳定、可缓存、可精确失效的结构事实；
2. Bug assessment 需要从 Matcher、traceback 或运行证据出发，按案件继续调查跨文件乃至跨组件实现；
3. 公共框架证据需要绑定宿主实际安装的 NoneBot、Adapter、Alconna、Uninfo 版本与源码 revision。

当前仓库已经同时出现低级文件枚举与文本命中、Griffe、ast-grep-py 和 Serena。它们有部分重叠，但没有
一个工具能够同时负责源码归属、结构提取、符号语义、运行时可达性和 Bug 因果证明。继续按局部需求增加
后端，会产生多套源码根、revision、缓存和失败语义；过早统一成单一工具，又会把不同消费者的要求错误
合并。

因此需要先决定每一层的权威职责，再决定是否保留多种实现。

## 当前项目事实

| 工具或机制 | 当前状态 | 当前用途 |
|---|---|---|
| `glob` / `rglob` + 文本扫描 | 已使用 | `BoundedSourceReader` 在已批准插件根中枚举 Python 文件、做有界文本命中和整文件读取；不理解符号身份 |
| Griffe | 已从项目自有实现与直接依赖移除 | 历史来源绑定与 revision 安全合同由共享 inventory 保留，不再提供符号 reader |
| `ast-grep-py==0.45.1` | 已采纳并替换 Matcher 手写 AST 形状提取 | `capability_source_evidence` 用固定只读规则识别 handler 装饰器、直接调用、Rule、Permission、limiter 和配置读取等 CST 候选形状 |
| Direct Jedi | 领域 `go_to_definition` 已实现；产品接线待资格 | 从已批准使用位置执行 cursor-aware `goto`，定位实际安装依赖定义；结果仍须经过项目来源门禁 |
| Serena 1.7.0 | 已由 ADR-0085 移除 | 不再作为 Bug 或依赖定义导航后端 |
| MultiLSPy | 不采用 | 它是多语言 LSP 生命周期封装，Python 仍依赖外部 language server；没有为本项目增加比 Direct Jedi 更有价值的定义语义，却增加进程、协议和版本层 |

静态源码关系只能证明“当前 revision 的代码可能如何工作”，不能证明某个插件本轮成功加载、某条 Rule / Permission
已经通过、某个分支在本次消息中执行或某个运行时值为何。运行时注册、correlation、traceback、配置投影与日志
仍是独立证据，不能由本 ADR 中任何源码工具替代。

## 各工具适合与不适合承担的职责

### `glob` / `rglob` 与文本扫描

适合：

- 在批准根内发现候选文件；
- 为没有语义后端、语义后端失败或用户只提供文本片段时提供最低成本兜底；
- 读取已经由其他证据定位的完整文件或有界片段。

不适合：

- 区分代码、注释和字符串中的同名文本；
- 解析 alias、定义、引用、继承或类型；
- 独立构建调用图或判断条件分支。

最终定位：保留为源码 inventory 的低级实现细节和永久文本兜底，不把它称为语义导航器。

### Griffe

适合：

- 不 import 目标包地读取 Python API 与源码对象模型；
- 给已安装公共框架建立 distribution、version、源码 revision、符号与 alias 证据；
- 为帮助索引和 Bug 调查提供可缓存、确定性的定义与签名事实。

不适合或尚未证明：

- 完整反向引用、数据流、控制流和 Python 动态派发；
- 同一作用域内重复命名为 `_` 的 handler；Griffe 2.1.0 对象模型实测只保留后一个成员，不能独立维护
  Matcher handler 的源码位置身份；
- 任意 local/editable 插件、动态装饰器和跨文件 alias 的实际命中率；
- 当前每组件独立 snapshot 且 alias 解析 `external=False`，不能作为“插件跳到 NoneBot 定义”的现有
  baseline；这需要共享 ModulesCollection 或受控多组件 inventory 的新对比实现；
- 单独证明某分支在本次运行中执行。

最终定位：现有实现仅作为迁移基线。distribution inventory、版本、revision、路径准入和 Evidence 合同继续
由项目持有；Griffe 不再扩展为普通插件或依赖定义的长期导航器。

### ast-grep-py

适合：

- 用固定 pattern / kind / relation 查询语法树结构，而不是匹配表面文字；
- 从已定位文件中确定性识别 NoneBot Matcher 注册、handler、Rule、Permission、limiter、直接调用和配置
  访问的语法候选或形状；
- 生成可测试、可版本化的项目专用提取规则。

不适合：

- Python 名称绑定、类型推断、跨文件定义与反向引用；
- 公共框架 API 版本语义；
- 开放式 Bug 根因调查或完整调用图。

最终定位：限定在 Matcher 与项目专用源码形状提取，不扩张为通用导航器。

### Jedi

当前 Triage `.venv` 安装源码上已经验证的能力：

- 显式绑定 Project、`.venv` Python 与 `src` 后，`Script.goto(follow_imports=True)` 可以直接从插件跳到
  `nonebot.get_plugin_config` 与 `Matcher.finish` 的 `site-packages` 定义，不需要 Serena source view；
- `get_references(scope="project")` 能返回 NoneBot 声明、转发导出、插件 import 和调用位置；
- 作为比外部 LSP 进程更轻的 Python 专用定义与引用后端。

当前限制与未知：

- `goto` 与 `infer` 不是同义证据；本轮 `goto` 能准确找到 `Matcher.finish`，`infer` 却落到 typeshed 的
  `classmethod.__get__`，因此定义导航应以 `goto` 为主，`infer` 只能形成可能不完整的类型候选；
- Jedi 官方明确说明引用过于复杂时 `get_references` 会停止，返回结果不能宣称是完整引用图；
- Script 静态分析不会 import 被分析插件，但 Environment 为取得 `sys.path` 会启动指定 Python，常规环境
  求值会经过 `site.py`；仍需验证 `.pth`、环境变量和解释器启动边界，并保持
  `load_unsafe_extensions=False`；
- 返回位置可能来自插件根、`site-packages`、typeshed 或 uv cache，必须重新经过批准组件、source revision
  和相对 locator 门禁；
- 对 nonemigut 的 workspace 插件、Git/wheel 依赖、Misty Uninfo fork、Alconna 动态注册写法的真实准确率；
- project references 在真实插件规模下的耗时、内存、并发和中断语义；
- 迁移实现仍须补齐现有 inventory / Evidence / revision 合同与回归覆盖。

最终定位：作为唯一的 Python 依赖定义语义后端。首要操作是从源码使用位置调用
`Script.goto(follow_imports=True)`；`infer` 与 references 只能作为有完整度标记的增强，不能替代定义结果或
被宣称为完整调用图。

### Serena

适合：

- Bug assessment 已经确定 subject 后，按案件做符号搜索、声明 / 定义、引用和跨文件位置导航；
- 通过语言服务器提供 Griffe 当前不承诺的反向引用和更开放的交互式导航；
- 作为显式 opt-in、延迟启动、失败可回退的高级后端。

当前限制与成本：

- 需要独立 Serena / Pyright 进程、额外安装、冷启动、缓存和进程回收；
- 当前运行纵切只消费插件根内 `find_symbol`，尚未让 Bug Agent 使用声明或反向引用工具；
- 当前 Serena 1.7 + Pyright 探针及产品项目配置未能从插件根解析普通 `site-packages`；加入绑定 revision
  的受控 source view 后可以跳到 NoneBot 定义。这是当前配置的实测边界，不是 Serena / Pyright 的普遍
  能力断言；
- 静态符号关系仍不能证明运行因果。

最终定位：既有 opt-in Bug 深度导航能力不因本 ADR 自动删除，但它不参与依赖定义导航，不与 Jedi 组成
默认双后端，也不继续扩大跨依赖 source view。

## 决定后的分层

```text
项目拥有的 approved source inventory
（组件身份、批准根、相对 locator、实际字节 revision、预算、完整度）
        │
        ├─ inventory / revision / 路径边界
        │      └─ distribution files / os.scandir / glob 等受控枚举实现
        │
        ├─ 运行事实与项目专用形状
        │      ├─ runtime Bot：本轮成功注册的 Matcher / handler
        │      └─ ast-grep：Matcher、Rule、Permission、配置等源码形状
        │
        └─ 依赖定义导航
               ├─ Direct Jedi goto：唯一语义后端
               ├─ locator 经过 inventory / RECORD / revision 门禁后才形成 Evidence
               └─ glob + 有界文本/文件读取：永久 fallback 与动态字面量路线
```

所有后端必须共享同一套批准源码根、component identity、source revision、相对 locator、Evidence ID、
完整度和 `scope_denied` 语义。Jedi 不拥有源码准入，也不能把候选静态关系升级为运行因果。

## 决策

1. 依赖定义导航采用 Direct Jedi，不再把 Griffe 扩展为长期产品后端。主要输入必须是已经由插件源码、
   traceback 或其他证据给出的文件与光标；主要查询是 `Script.goto(follow_imports=True)`。
2. 保留受控 `glob` / 文本搜索和按文件读取。它负责未知位置发现、字符串字面量、配置键、动态 `getattr` /
   `setattr` / 注册、语义后端失败和无有效 locator 的兜底；它不是临时兼容层，不能被 Jedi 删除。
3. 不把 Griffe、Jedi 与 glob 暴露成三个同级 Agent 工具。领域接口根据问题形态在内部选择 Jedi 或文本路线，
   最终只返回统一的 locator、Evidence、完整度与失败类型。
4. 项目继续拥有 ADR-0039 已建立的 distribution inventory、精确安装版本、VCS commit、实际源码字节
   revision、RECORD 归属和批准根。Jedi 返回的 `module_path` 只有精确命中该清单后才可读取；typeshed、
   stdlib、分析器自身、工作区影子文件和未登记的 `site-packages` 路径全部拒绝或标为 opaque。
5. `infer`、`get_references` 和 `Project.search` 不是默认定义接口。它们没有本项目要求的完整度或内部读取
   预算保证，只能在明确问题中作为有界增强，并对 partial / opaque 单独计量。
6. Jedi 分析环境必须与目标源码分离：使用项目控制的 staged approved root、关闭 auto-import、禁用 unsafe
   extension loading、使用本轮独立缓存，并避免把主 Bot `.venv` 直接当作可执行沙箱。目标插件或依赖不得
   为导航而 import。
7. MultiLSPy 不采用。项目只需要 Python 定义导航，Direct Jedi 已提供所需 API；再套
   `jedi-language-server`、JSON-RPC、server 生命周期和多语言适配层只会增加依赖与故障面。
8. Serena 不参与本职责；后续 ADR-0085 已删除其 Bug-only 纵切，Agent 不再拥有第二个语义后端。
9. runtime Bot 继续提供 Matcher / handler 注册事实，ast-grep 继续提取 NoneBot 专用源码形状。Jedi 只回答
   “这个使用位置指向哪个定义”，不负责解释动态 Matcher、Rule、Permission 或某次运行是否发生。

## 评分对比摘要

2026-08-14 的本地、零网络实验绑定同一 E 盘 `.venv` 中实际安装的 nonebot2 `2.5.0`、
nonebot-adapter-onebot `2.4.6` 和 nonebot-plugin-alconna `0.62.1`。该环境没有锁定或安装
nonebot-plugin-uninfo，因此三包结果不能冒充四包完整覆盖；Nonemigut 的另一个部署后来单独证明 Griffe
能够读取其 VCS Uninfo `0.11.1`，但不改变原环境缺口。

| 指标 | Griffe/AST（20 题） | inventory-bound 词法（同 20 题） | Jedi 0.20.0（10 道新题 smoke） |
|---|---:|---:|---:|
| Definition accuracy | 7/10 | 8/10 | exact 5/6；接受 bound alias 后 6/6 |
| Recall@5 | 14/20 | 20/20 | relation point 6/10；完整结果 9/10 |
| Text / dynamic | 0/4 | 4/4 | 0/2 |
| 错误组件/版本 | 0 | 0 | 结构化题 0；字符串名称搜索 1 |
| 初始化 / stage | 约 10.47 s | 约 0.23 s | 约 0.66 s；另有约 0.31 s 环境绑定探针 |
| 单题热查询中位数 | 约 12.37 ms | 约 27.23 ms | 约 61.42 ms |

Jedi 的 10 题是未复用 Gold 的初步 smoke，不是与前 20 题同题的统计学 head-to-head，因此本 ADR 不宣称
Jedi 在所有指标上显著优于 Griffe。选择 Jedi 的原因更窄也更直接：本职责是“从实际使用位置跳到依赖
定义”，Jedi 的 cursor-aware goto 与这个问题同构；它正确处理了 nested namespace、wildcard re-export、
跨包继承、签名和 Griffe 当前误定位的 stub-only `.pyi`。三次独立运行的质量结果一致。

真实 Nonemigut 抽样也支持这个边界：Direct Jedi 对四个命名跨文件问题都给出准确 definition；Griffe
适合命名良好的 API 浏览，却会因 Python 最终绑定模型丢失 NoneBot 惯用的重复 `_` handler（`who-at-me`
保留 1/4，`withdraw` 保留 10/33）。另一方面，Jedi 同样漏掉字符串配置键和 `setattr` 动态写入，所以最终
组合必须是 **Jedi-first for known use-site definition，glob/text fallback for discovery and dynamic text**，
而不是 Jedi-only。

## 明确不在本 ADR 中决定

- 不用任何静态工具替代 NoneBot 运行时注册事实、真实 Permission / Rule 执行和关联日志；
- 不构建完整 Python 调用图、控制流图或数据流图；
- 不允许模型、部署者配置或被分析仓库提供 ast-grep 规则、Serena context 或源码 scope；
- 不因为某个工具能读取源码，就自动允许把整个插件或依赖正文发送给远端模型；
- 本 ADR 只记录选择，不在同一次文档变更中增删依赖或把实验后端启用到 nonemigut。

## 与既有决定的关系

- [ADR-0039](0039-use-griffe-for-installed-public-framework-source-evidence.md)：部分替代其 Griffe 后端选择；
  distribution inventory、版本/revision、批准路径和 Evidence 合同继续有效；
- [ADR-0055](0055-use-ast-grep-for-matcher-source-shape-extraction.md)：继续有效；ast-grep 保持 Matcher 形状
  提取职责，不承担通用符号导航；
- [ADR-0056](0056-use-serena-for-optional-bug-source-navigation.md)：其既有 opt-in Bug 纵切已由
  [ADR-0085](0085-remove-serena-bug-source-backend.md) 撤销；
- [ADR-0050](0050-use-a-bounded-agent-for-user-bug-assessment.md) 与
  [ADR-0053](0053-allow-relevant-source-and-log-bodies-for-bug-assessment.md)：继续约束 Bug Agent 的工具预算、
  Evidence 门禁、源码投影与普通用户披露边界。

## 官方能力依据

- [Griffe Loading](https://mkdocstrings.github.io/griffe/guide/users/loading/)：静态加载、alias 解析与禁止动态
  inspection 的边界；
- [Griffe Navigating](https://mkdocstrings.github.io/griffe/guide/users/navigating/)：加载后的 API 对象模型与
  alias 导航；
- [ast-grep Python API](https://ast-grep.github.io/guide/api-usage/py-api.html)：`SgRoot` / `SgNode`、结构
  pattern 与 tree traversal；
- [Jedi API Overview](https://jedi.readthedocs.io/en/latest/docs/api.html)：`Script.goto`、`infer`、
  `get_references` 与 Project / Environment；
- [Jedi PyPI](https://pypi.org/project/jedi/)：版本、Python 版本和依赖元数据；
- [Serena Tools](https://oraios.github.io/serena/01-about/035_tools.html)：`find_declaration`、
  `find_referencing_symbols`、`find_symbol` 等 LSP 工具。

## 最终决策记录

最终选择 Direct Jedi 作为依赖定义语义后端，受控 glob/文本读取作为永久 fallback。项目继续拥有 source
inventory、版本、revision、路径门禁与 Evidence，后端无权扩大范围。后续 ADR-0059 已移除项目自有
Griffe reader 并实现共享只读 FileSystem / Jedi 领域边界；MultiLSPy 不采用；Serena 不进入该职责。

本决定依据 20 题 Griffe/词法同题实验、10 道 Jedi 新题 smoke 和 Nonemigut 实际插件只读抽样。样本足以
做工程选型，不足以主张统计显著性，也没有消除动态 Python 的固有限制。实现完成的最低资格是：真实安装
依赖 definition Gold 不退化、stub locator 正确、错误组件/版本为零、所有结果经过 RECORD/revision 门禁、
目标包零 import、partial/opaque 可观察，以及 glob fallback 回归保持通过。
