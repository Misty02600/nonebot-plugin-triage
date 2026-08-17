# ADR-0055：用 ast-grep 提取 Matcher 源码结构

| 状态 | 决策日期 |
|---|---|
| 已采纳；直接替换已实现 | 2026-08-14 |

> 后续 ADR-0057、0059 已选择并实现 Direct Jedi 依赖定义导航，同时移除项目自有 Griffe reader；本 ADR
> 的 ast-grep Matcher 形状职责不变。

## 当时遇到了什么

`capability_source_evidence` 需要从插件源码中识别 Matcher 注册、装饰器 handler、同文件调用、配置类与
配置引用，以及 Permission、Rule、限流候选。首版直接用 Python `ast` 编写了大量节点类型和树遍历逻辑；
随着语法形状增加，这部分已经成为一套项目自维护的结构查询器，而且按函数名建字典会把同一模块中常见的
多个 `async def _` 折叠成一项，导致后续 Matcher 丢失 handler 关联。

这些需求中的“某类调用、赋值、装饰器或属性访问在哪里”属于具体语法结构匹配，适合交给现成的 CST 查询
引擎。路径归属、运行时注册门禁、Evidence 身份、内容 revision、预算、权限收窄和是否足以形成结论仍是
Triage 自己的领域责任，不能交给结构搜索工具决定。

## 决策

1. 将 `ast-grep-py==0.45.1` 固定为基础依赖，并直接替代
   `capability_source_evidence` 中用于 Matcher 源码形状识别的手写 Python AST 遍历，不长期保留两套提取器
   或 A/B 路由。
2. ast-grep 只作为进程内、只读的 Python CST 查询后端。规则固定在项目源码中；不读取部署者或模型提交的
   YAML 规则，不调用 CLI，不开放 `replace`、`commit_edits`、fix 或 rewrite 能力。
3. 由 ast-grep 的固定 pattern / kind 查询识别 handler 装饰器、函数内直接调用与属性，并以其 CST field
   读取顶层 Matcher factory 调用、赋值目标、`handlers`、配置类和绑定、配置属性引用，以及 Permission、
   Rule、限流候选的语法位置。已经定位的 Python 字面量只用标准库 `literal_eval` 安全解码，不恢复 AST
   结构遍历。
4. 函数和 handler 以源码位置身份保存，不再以函数名作为唯一键；同一模块中多个名为 `_` 的 handler 必须
   分别关联到各自装饰器所属 Matcher。
5. 保留现有显式源码根、文件和节点预算、相对路径校验、字节 hash、Evidence ID、partial / opaque 语义和
   deterministic ordering。兼容字段 `max_ast_nodes` 在 v2 中统计排除注释的 named CST node，不再与旧版
   `ast.walk()` 数值等价；文件和总字节预算仍作为独立硬边界。ast-grep 匹配只能形成静态候选，不能证明
   插件本轮加载、Matcher 当前注册、Permission / Rule 通过或具体运行分支执行。
6. ast-grep 不负责跨文件符号解析、Python 动态派发、框架 API 语义或完整调用图。公共框架符号仍由
   `installed_sources` / Griffe 处理；将来 Bug Agent 的更广源码导航使用独立只读后端，不把它塞进本提取器。
7. 无法解析、超出预算、关系不唯一或动态构造的结构继续失败关闭为 partial / opaque，不回退到旧 AST
   提取器，也不为得到结果 import 或执行目标插件。

## 为什么这样选

- ast-grep 已提供稳定的 Python CST 节点、结构查询和关系约束，能替代当前重复维护的节点分派代码；
- CST 保留装饰器与每个函数定义的精确位置，能自然区分重复的匿名式 `_` handler；
- 固定只读规则把第三方库限制在“找出语法位置”，不会让它接管 Triage 的安全、当前性和证据判断；
- 直接替代避免两套提取器长期漂移、产生双重结果或增加部署配置。

## 没有采用的方案

### 继续扩张手写 Python AST

它可以完成当前功能，但每增加一种 Matcher 或依赖注入写法都要继续维护遍历、父子关系和去重逻辑，且已经
出现按函数名折叠真实 handler 的缺陷。

### 同时运行 AST 与 ast-grep 并比较结果

这适合短期 PoC，却会把双实现和差异仲裁带进产品路径。项目作者已明确选择直接替代；实施中若无法维持
现有领域合同，应显式报告阻塞，而不是静默保留旧后端。

### 用 ast-grep 解决跨文件语义和 Bug 根因分析

ast-grep 擅长结构形状搜索，不提供 Python 名称绑定、动态调用解析或问题驱动的跨组件调查。扩大到这些职责
会重新制造一套不完整的源码分析平台。

## 带来的影响

- 基础 wheel 新增固定版本的原生 ast-grep Python 扩展，支持的平台受其 wheel 发布范围约束；
- Matcher 提取规则从 Python AST visitor 改为项目内固定 CST 查询，输出领域合同保持不变；
- 重复函数名 handler 不再被静默折叠；
- extractor revision 升级会使旧源码证据 cache 全量失效并按 v2 重建；
- ast-grep 当前仍是 Alpha 分类依赖，升级必须重新核对节点语义和 wheel 支持，不能使用无界版本范围；
- 这项替换不等于完成跨文件行为闭包、Alconna / Uninfo 语义注入或 Bug 源码导航。

## 落实与确认

- 实施位置：`src/nbtriage/capability_source_evidence.py`；
- 依赖位置：`pyproject.toml`；
- 固定规则现已覆盖 NoneBot 官方 `on`、事件类 `on_*`、字面触发、命令、Shell 命令与 `on_type`；
  `CommandGroup.command / shell_command` 和 `MatcherGroup.on_*` 只有在构造类型与接收者绑定可以静态证明时
  才形成注册锚点，业务对象上的同名方法不会被猜成 Matcher；
- 空 `on_command` 不形成教学注册锚点；动态调用、重绑定和无法证明的分组接收者继续保持 opaque 或不命中；
- 首个替换阶段按当时要求未新增或运行 pytest；本次官方入口补全增加了字面触发、分组来源、同名业务方法
  near-miss 与空命令用例，并与 Runtime / 教学消费者定向回归一同通过。

## 相关决定

- 部分替代 [ADR-0039](0039-use-griffe-for-installed-public-framework-source-evidence.md) 中“首版不引入
  ast-grep 等额外源码工具”的范围；Griffe 对公共框架 API 的职责不变；
- 延续 [ADR-0036](0036-keep-capability-shadow-deterministic-and-record-oriented.md) 的确定性能力记录边界：
  静态源码结构只能补充已有消费者，不改变能力事实真值。
