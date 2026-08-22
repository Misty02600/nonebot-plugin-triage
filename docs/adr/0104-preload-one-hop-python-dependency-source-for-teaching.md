# ADR-0104：为教学分析预载一层 Python 依赖源码

| 状态 | 决策日期 |
|---|---|
| 已采纳 | 2026-08-21 |

## 当时遇到了什么

教学请求已在首包中提供 Handler 和本地 helper，但函数切片不包含模块 import。当插件函数直接调用第三方
依赖时，模型只能看到未归属的符号。真实 nonebot_plugin_memes 运行中，模型因此在目标插件、Python
依赖和 LocalStore 之间反复搜索 search_memes，没有使用已有 Jedi 导航。

继续增加 Prompt 文字不能消除这个接口缺口：正确路径需要模型自己拼出 root、文件、行、列和 revision，
而文本搜索的参数更简单。反过来，把整个依赖树都放入首包会放大 token、延迟、依赖 revision 闭包和
不可信源码面积。

## 决定

1. 当前解释器的 purelib、platlib，以及当前 `sys.path` 中实际生效的 `site-packages` / `dist-packages`
   自动成为 Python-only 只读导航根。这覆盖 `uv --with` 等 overlay 环境；它是文件系统安全边界，不是人工
   维护的包 allow-list，部署者无需逐包批准。
2. 模型外 AST 切片遇到直接 Python 调用时，继续用 Jedi 定位定义。只有结果唯一、类型为模块顶层 function、
   位于当前解释器上述根中且 revision 一致时，才预载该外部函数的一层完整切片。类方法和嵌套函数保留给
   按需导航，避免把通用框架 API 批量塞入首包。Jedi 只能识别编译扩展、没有实现文件时，可以在同一批准
   包根中静态恢复唯一的公开 `.pyi` 签名；该 stub 只生成导航记录，不作为行为 Evidence，typeshed 标准库
   stub 不进入首包。
3. 外部函数作为 python_dependency_function Evidence，可支持最终结论，但不进入本地 helper BFS，不递归
   预载它继续调用的依赖树。单函数 8,000 字符和单教学单元 32,000 字符首包预算继续生效。
   其文件 revision 在发布前和缓存复用时都会重新核对。
4. 唯一定义过长、无法切片或只有签名 stub 时，首包只附加 external_dependency_navigation：它包含
   target_plugin 调用坐标与依赖根的精确 read target。该记录只导航、不可引用；有实现源码时，模型必须用
   对应根的 read_file 获得可引用且可复核 revision 的动态 Evidence；只有 stub 时只能据其确认签名，不能
   推断依赖的业务行为，也不应继续在目标插件或 LocalStore 中搜索实现。
5. 普通教学单元仍可以沿已知坐标自主补读依赖，并可使用 Jedi 继续转到定义。依赖根不暴露全局 glob/search；
   不执行、不 import 第三方代码。complete family 继续要求首包证据闭合，不开放额外源码工具；其首包只有导航
   记录而缺少可引用实现时，模型必须省略无法证明的细节或关闭知识，不能把 marker 当作行为证据。
6. 歧义定义、非函数、解释器根外位置或 revision 漂移继续 fail-closed，不猜测定义归属或行为。
7. 单文件插件若直接安装在依赖根，静态导航与运行时统一使用 `target_plugin` 这个共享 Python-only 根，且只
   暴露按已知路径读取和文件信息工具；若单文件插件直接位于 Bot 项目根，则保留 `bot_project` 的真实语义，
   不把整个宿主目录别名成目标插件源码。

## 带来的影响

- 直接外部调用的常见语义可在首包中闭合，减少目标插件文本搜索、LocalStore 试探和错误 root 切换。
- 依赖切片与精确导航记录改变首包 Evidence 合同，教学 request revision 升为
  capability-teaching-request-v17；旧 held-out 不能继承。
- 首包仍不会为了“也许有用”而枚举整个依赖环境。

## 没有采用的方案

- 不维护插件或 Python 包级的人工 allow-list。
- 不加入另一套外部依赖搜索工具，也不为每个调用动态生成闭包工具。
- 不递归预载整棵依赖树。

## 替代关系

- 细化 [ADR-0059](0059-share-read-only-evidence-access-across-agent-flows.md) 的依赖导航合同：依赖根仍受共享
  只读门禁约束，但教学首包可预载一层唯一外部函数。

## 相关文档

- [能力影子索引流程](../architecture/flows/capability-shadow-index.md)
- [模型 Provider 支持矩阵](../architecture/model-provider-support.md)
