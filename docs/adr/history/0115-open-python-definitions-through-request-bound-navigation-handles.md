# ADR-0115：通过请求内位置句柄打开 Python 定义

| 状态 | 决策日期 |
|---|---|
| 已采纳 | 2026-08-23 |

## 当时遇到了什么

ADR-0114 已把模型侧 Jedi 缩减为 `source_ref / line / column`，但真实模型仍要阅读带行号源码、计算列，
并容易退回参数更简单的文件搜索或把依赖目录交给 `file_info`。编辑器中的光标位置本应由客户端提供，
不应继续由模型构造。

## 决定

1. 当前请求的初始 Python Evidence，以及之后稳定 `read_file` / 定义读取结果，附带独立的
   `navigation_targets` sidecar。每项目标只公开请求内 `navigation_ref`、显示名、位置类别和行号；完整根、
   文件、列与 revision 留在服务端注册表。
2. 模型只调用 `python_open_definition(navigation_ref)`。唯一目标在一次调用内完成现有 Jedi 跳转、目标
   revision 复核、稳定有界读取和动态 Evidence 登记；多个定义只返回少量候选句柄，不自动选择。
3. 句柄绑定 Evidence revision 且只在当前请求有效。文件变化、未知句柄、越过批准根或目标不可稳定读取时
   fail-closed，不回退为模糊符号搜索。
4. sidecar 只标记已经展示源码中的直接调用、装饰器和基类等高价值位置；使用 AST 确定位置并设置有界数量，
   不修改 Evidence 文本，不增加 references、call graph 或持久符号索引。
5. Prompt 更新到 v63，请求更新到 v28；schema 仍为 9。旧 held-out 质量结论不能继承。

## 为什么这样选

- 复用现有 `DefinitionNavigator`、批准根和 revision 合同，只把编辑器客户端本应承担的位置绑定移出 LLM；
- 位置句柄比让模型复制哈希和列稳定，也比只传符号字符串更少歧义；
- 唯一目标直接返回可引用 Evidence，减少无价值的目录试探与第二次读取工具调用；
- 请求内、revision 绑定且有界，不建立长期符号身份或扩大源码访问面。

## 替代关系

- [ADR-0118](0118-limit-eager-source-depth-and-let-families-navigate-selectively.md) 把本 ADR 的请求内位置句柄扩展到
  complete family 的选择性定义导航，但不开放 family 的目录、搜索或任意文件工具。
- 替代 [ADR-0114](0114-follow-static-gate-bindings-and-bind-jedi-to-request-evidence.md) 第 5、6 项的模型侧
  坐标接口；其静态 gate 绑定、一层外部函数和不递归边界保持不变。
- 延续 [ADR-0059](../0059-share-read-only-evidence-access-across-agent-flows.md) 的批准根、revision 与动态
  Evidence 闭包。

## 落实与确认

- `CapabilityTeachingToolProvider` 为初始与动态 Python Evidence 注册请求内位置句柄；
- `python_open_definition` 已替代模型可见的坐标版工具，底层 `DefinitionNavigator` 保持不变；
- 动态读取与初始函数 Evidence 均有最小回归测试。
