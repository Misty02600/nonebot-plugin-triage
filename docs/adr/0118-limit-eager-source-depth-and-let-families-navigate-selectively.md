# ADR-0118：限制首包源码深度并允许 family 选择性导航

| 状态 | 决策日期 |
|---|---|
| 已采纳 | 2026-08-23 |

## 当时遇到了什么

教学请求在模型运行前会沿 Handler 和 helper 的普通函数调用做三层 BFS，并对到达深度边界的调用先执行
Jedi、再丢弃不再展开的插件内定义。真实插件首次生成或代码变更后的重建因此为大量最终没有进入 Evidence 的
普通调用支付静态导航成本。

另一方面，complete family 虽然已经取得完整成员 manifest、共享 Handler 和静态 Callable Evidence，模型适配器
仍硬编码不为 family 创建任何工具运行时。这一限制早于请求内 `navigation_ref`，使模型发现少量共同语义缺口时
只能关闭知识；完全开放文件浏览又会诱导模型逐成员阅读，重新放大 token、延迟和工具调用。

## 决定

1. Handler 与本地 helper 的普通函数调用只在首包自动展开两层。到达第二层后，不再为普通调用执行 Jedi；
   已展示源码中的高价值调用仍通过请求内 `navigation_ref` 留给模型按需打开。
2. gate、静态参数 `Depends(provider)` 和静态 family Callable 等确定性执行边继续按各自合同闭合。它们不因
   普通调用深度缩短而退化为模型猜测，也不把被打开的外部函数递归扩展成整棵依赖树。
3. complete family 的完整成员 manifest 仍必须在初始 Evidence 中无损提供并完整复核。family 只获得
   `python_open_definition(navigation_ref)`，不获得目录枚举、全文搜索、任意文件读取或文档检索工具。
4. family 工具只用于选择性补读证明共同业务语义或缺失参数含义所需的少量定义。Prompt 与工具说明明确告知
   模型工具预算有限，禁止逐成员打开定义，也禁止用源码导航代替完整成员 manifest 复核。
5. 每教学单元证据工具上限从 5 调整为 7，请求上限从 8 调整为 10；最终结构化输出工具仍单独预留一次计数。
   160k total token、16,384 单次 output token、0.05 美元和 300 秒边界不变。
6. Prompt 更新为 v70，请求更新为 v32；旧 held-out 与真实插件诊断不能继承到新合同。

## 为什么这样选

- 首包继续提供两层常用 helper，减少模型为基本语义反复导航；边界处不再执行后丢弃，直接消除无效 Jedi 成本。
- 请求内位置句柄已经把路径、列和 revision 留在服务端，family 可以获得窄而安全的按需能力，不需要恢复整个
  文件工具集。
- 完整成员事实与选择性源码补证承担不同职责：前者防止遗漏成员，后者只补共同语义；工具不能替代 manifest。
- 7 次工具为按需深读留出空间，同时仍不足以让大型 family 逐成员浏览；10 次请求容纳工具往返、最终提交和
  至多一次结构或投影纠错，而不提高 token 与费用总额。

## 没有采用的方案

### family 继续零工具

这会把首包的任何局部缺口直接升级为整项关闭，也没有利用 ADR-0115 已建立的安全位置句柄。

### family 开放完整文件工具

目录、搜索和任意读取会让模型尝试遍历成员或源码树，违背 family 聚合分析的成本边界。

### 只把三层改成两层但仍在边界执行 Jedi

这只减少进入 Evidence 的定义数量，不能消除已经定位后又被丢弃的主要耗时。

## 带来的影响

- 首次生成与源码 revision 变化后的真实重建会减少普通调用的预建 Jedi 数量；具体收益需用相同真实插件复测。
- 普通单元可用原文件工具和位置导航继续补证；family 只能沿初始或动态 Evidence 已标注的位置向下打开定义。
- 模型若没有选择真正需要的深层 helper，可能省略细节或关闭知识；这是以固定预加载成本换取按需判断的明确取舍。
- 缓存命中仍可跳过模型和请求构建，但不作为本决定的性能依据。

## 落实与确认

- `capability_analysis_adapter.py` 把普通调用首包深度降到二层，并在边界调用 Jedi 前停止。
- `capability_model_adapter.py` 为 family 创建有界工具运行时，预算更新为 10 请求 / 7 次证据工具。
- `capability_analysis_tools.py` 对 family 只返回带选择性阅读说明的 `python_open_definition`。
- 回归覆盖边界不导航、family 工具可见且没有文件/搜索工具，以及 family Agent 确实取得工具运行时。

## 替代关系

- 替代 [ADR-0104](0104-preload-one-hop-python-dependency-source-for-teaching.md) 第 5 项中 complete family
  完全不开放源码工具的决定；一层外部函数预载和不递归依赖边界不变。
- 替代 [ADR-0106](0106-follow-static-parameter-dependencies-for-teaching-evidence.md) 与
  [ADR-0114](0114-follow-static-gate-bindings-and-bind-jedi-to-request-evidence.md) 对普通调用统一沿用三层深度的部分；
  gate 与参数依赖的确定性闭合继续有效。
- 扩展 [ADR-0115](0115-open-python-definitions-through-request-bound-navigation-handles.md)，把请求内位置句柄用于
  complete family 的窄按需导航。

## 相关文档

- [能力影子索引流程](../architecture/flows/capability-shadow-index.md)
- [模型与 Provider 支持矩阵](../architecture/model-provider-support.md)
