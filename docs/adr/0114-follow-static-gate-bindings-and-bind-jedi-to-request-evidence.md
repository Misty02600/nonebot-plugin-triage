# ADR-0114：追踪静态 gate 绑定并把 Jedi 绑定到请求 Evidence

| 状态 | 决策日期 |
|---|---|
| 已采纳 | 2026-08-23 |

## 当时遇到了什么

教学首包已经会从 Handler 或 helper 的直接调用位置预载一层外部函数，但真实插件经常把注册级 gate 先保存为
模块变量：Matcher 引用 `permissions.query_permission`，本地模块再把它绑定到 checker，checker 才调用第三方
权限工厂。Jedi 能正确把 Matcher 引用定位到模块级 statement，适配器却只接受 function，因此首包在第一步就
丢失了确定性导航结果。Agent 随后需要自己拼装 root、path、line、column 和 revision，容易把依赖目录误传给
文件工具，或退回无意义的全文搜索。

## 决定

1. 注册级未解析 Permission / Rule 唯一定位到目标插件模块级 `Assign` / `AnnAssign` 时，首包保存该赋值语句，
   并沿其 RHS 的静态 Python 引用继续导航；只接受当前 revision 下唯一定位的定义。
2. 本地模块级绑定最多沿用现有三层源码切片深度、单片 8,000 字符和单元 32,000 字符预算。动态属性、多个定义、
   非赋值 statement 或不可读源码不猜测。
3. 绑定链唯一到达批准 Python 依赖根中的模块级函数时，复用 ADR-0104 的一层外部函数预载；外部函数不再成为
   BFS seed，也不递归展开依赖树。外部类、模块和常量不会因此整包进入首包。
4. 该规则只认识通用 Python 绑定和定义关系，不认识 `nonebot-plugin-permission`、权限键名或特定插件 Schema。
   模型仍负责解释源码语义，确定性层只负责提供闭合、当前且有界的 Evidence。
5. 模型可见的 `python_go_to_definition` 只接收 `source_ref / line / column`。`source_ref` 使用当前请求初始
   Evidence 或成功 `read_file` 返回的 evidence ID；服务端从请求上下文补齐并复核 root、path 与 revision，底层
   `DefinitionNavigator` 的完整安全合同不变。
6. 导航结果返回对应根的精确 `read_target`。定义位置本身仍不可引用，Agent 必须通过该 read target 取得动态
   Evidence 后才能用新读到的实现支持最终结论。
7. Prompt 更新到 v62，请求更新到 v27；schema 仍为 9。旧 held-out 质量结论不能继承。

## 为什么这样选

- 模块级变量是 Python 数据驱动 gate 的常见组合方式，保留 statement 才能让现有 Jedi 结果真正进入 Evidence；
- 静态唯一导航比让模型在目录、根名和 revision 之间试错更可靠，也不要求维护插件关系表；
- evidence ID 已经是请求内稳定、不可伪造的源码引用，不需要再新增一套 source handle Schema；
- 底层仍执行路径批准、revision 复核和定义结果过滤，薄接口只减少模型参数，不降低安全边界。

## 没有采用的方案

### 为某个权限插件编写专属解析器

这会把第三方 API 和配置命名固化进核心教学适配器，无法覆盖其他数据驱动 gate，也会重复 Jedi 已经提供的定义
语义。

### 向模型暴露完整 root/path/revision

这些字段由当前请求和文件 revision 已经确定，让模型复制只增加格式错误和错误根选择，不能增加分析能力。

### 自动递归整个依赖包

完整依赖图会迅速扩大 token、隐私和版本漂移风险。当前需求只需要一层精确工厂定义；进一步事实仍由有目标的
只读工具补证。

## 带来的影响

- 模块变量包装的第三方 gate 可以在首包中同时看到本地绑定关系和一层外部工厂实现；
- Agent 调用 Jedi 不再需要理解哈希、源码根别名或依赖目录布局；
- 动态 gate 仍会 fail-closed，模型不会因本改动获得依赖目录搜索能力；
- Prompt v62 / request v27 需要重新进行真实插件诊断与 held-out。

## 替代关系

- 普通调用统一沿用三层源码深度的部分已由
  [ADR-0118](0118-limit-eager-source-depth-and-let-families-navigate-selectively.md) 替代；静态 gate 绑定仍按本 ADR 闭合。
- 模型侧 `source_ref / line / column` 与二次读取接口已由
  [ADR-0115](0115-open-python-definitions-through-request-bound-navigation-handles.md) 的请求内位置句柄替代；
  本 ADR 的静态 gate 绑定与一层外部函数边界继续有效。
- 扩展 [ADR-0104](0104-preload-one-hop-python-dependency-source-for-teaching.md) 的直接外部函数入口，加入目标插件
  内静态模块绑定链；其一层依赖与不递归边界不变。
- 延续 [ADR-0059](0059-share-read-only-evidence-access-across-agent-flows.md) 的批准根、revision 和动态 Evidence
  引用闭包。

## 相关文档

- [能力影子索引流程](../architecture/flows/capability-shadow-index.md)
- [架构总览](../architecture/overview.md)
- [模型与 Provider 支持矩阵](../architecture/model-provider-support.md)
