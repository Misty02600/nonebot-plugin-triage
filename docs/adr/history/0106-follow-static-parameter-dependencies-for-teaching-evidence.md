# ADR-0106：沿静态参数依赖补齐教学源码 Evidence

| 状态 | 决策日期 |
|---|---|
| 已采纳；参数默认值形式由 [ADR-0117](0117-follow-default-depends-parameter-providers.md) 补充 | 2026-08-21 |

## 背景

NoneBot Handler 常用类型别名把依赖注入藏在参数注解中，例如
`UserId = Annotated[str, Depends(get_user_id)]`。现有源码切片只沿函数体调用展开，因而能看到 Handler
使用 `UserId`，却看不到实际计算该值的 `get_user_id`。模型只能依据参数名猜测语义。

## 决定

1. 源码切片把 Handler 与本地 helper 的参数注解作为一种确定性导航边处理。
2. 只接受直接的 `Annotated[..., Depends(provider)]`，或 Jedi 唯一定位到当前插件 `.py` 文件中的类型别名；
   别名也必须静态展开成上述形式。
3. provider 仍通过现有 Jedi 定义解析、revision 校验、三层深度、单函数 8,000 字符和单单元 32,000 字符
   预算加入 `python_function` Evidence。歧义、动态包装或无法解析时不猜测。
4. 导航只证明 provider 源码属于本轮材料，不根据函数名或参数名模型外推导业务含义。现有框架语义只有在
   provider 源码实际使用对应框架类型时才加入。

## 影响

- `Annotated` 依赖的执行键、访问范围或会话语义可在 complete family 的封闭首包中到达模型；
- 不执行依赖、不读取任意 LocalStore 文件，也不建立 NoneBot 或 Uninfo 专用符号表；
- 初始 Evidence 发生变化，教学 request revision 升为 `capability-teaching-request-v19`，旧 held-out 不能继承。

## 相关文档

- [ADR-0118：限制首包源码深度并允许 family 选择性导航](0118-limit-eager-source-depth-and-let-families-navigate-selectively.md)
- [ADR-0104](0104-preload-one-hop-python-dependency-source-for-teaching.md)
- [ADR-0117](0117-follow-default-depends-parameter-providers.md)
- [能力影子索引流程](../../architecture/flows/capability-shadow-index.md)
