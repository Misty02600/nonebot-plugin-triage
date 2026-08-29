# ADR-0110：为 Family 预载静态成员 Callable

| 状态 | 决策日期 |
|---|---|
| 已采纳 | 2026-08-21 |

## 背景

数据驱动命令工厂常让多个 Matcher 共享同一闭包 Handler。Parser shape 能证明参数结构，却不能说明同为 `str`
或 `float` 的槽位分别表示尺寸、比例、倍速或角度。若 Handler 通过闭包成员的 callable 字段执行不同本地函数，
只提供共享 Handler 会遗漏已经静态可达的成员业务语义。

## 决定

1. 仅当 family 来自静态工厂表、Handler 确实访问闭包成员属性、该属性对应构造记录中的 Callable 字段，且字段值是
   唯一可解析的目标插件本地函数符号时，把这些函数切片加入同一个 family 的初始 Evidence。
2. Callable 函数按定义去重；单函数继续受 8,000 字符限制，并与 Handler、config、gate 和 helper 共用 32,000
   字符初始源码预算。
3. Callable 切片不作为新的递归 BFS seed，不继续展开其整棵调用树，也不为每个成员单独运行 Agent。
4. 动态属性、运行时替换、间接容器、歧义定义和插件根外 Callable 不猜测；无法闭合的业务细节由 family 省略或
   fail-closed。
5. 不新增 `members / variants / catalog` 持久化字段；Runtime family 成员与 Parser shape 仍是唯一成员清单来源。

## 影响

- 静态数据驱动 family 能在一次请求中理解各成员参数的业务含义；
- 成本随“唯一 callable 定义数”增长，而不是随 Matcher 成员数重复增长；
- 动态工厂保持保守，不把任意 `obj.func` 升级为源码导航协议。

## 替代关系

- 接续 [ADR-0098](0098-deduplicate-complete-family-member-manifests.md) 与
  [ADR-0102](0102-keep-family-aggregate-parameters-actionable.md)，补齐 Parser shape 之外的静态业务语义。
- 保持 [ADR-0058](../0058-use-deterministic-evidence-and-bounded-navigation-for-teaching-annotations.md) 的有界源码原则。
