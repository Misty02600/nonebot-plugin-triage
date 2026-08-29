# ADR-0117：沿参数默认值中的 Depends 补齐教学 Evidence

| 状态 | 决策日期 |
|---|---|
| 已采纳 | 2026-08-23 |

## 背景

[ADR-0106](0106-follow-static-parameter-dependencies-for-teaching-evidence.md) 只识别参数注解或类型别名中的
`Annotated[..., Depends(provider)]`。NoneBot Handler 也可以把依赖写成参数默认值，例如
`target: Target = Depends(get_target)`。这类 provider 没有进入初始 Evidence 时，相同依赖的不同教学单元
只能各自决定是否继续搜索源码，可能对同一会话限制生成不一致结论。

## 决定

1. 静态参数依赖同时识别直接参数默认值 `Depends(provider)`；`provider` 必须是唯一、直接的本地名称或属性
   引用，动态表达式、多个位置参数或运行时包装不猜测。
2. provider 继续复用 ADR-0106 的 Jedi 唯一定位、源码 revision、深度和字符预算，作为当前教学单元的初始
   `python_function` Evidence；不会执行依赖，也不会根据函数名推断语义。
3. 参数注解、参数默认值和类型别名只是到 provider 的确定性导航边。公开 scene、role、access、输入来源或
   其他事实仍由模型根据实际 provider 源码和版本化框架 Evidence 解释。

## 影响

- 同一 `Depends(get_target)` 被多个 Matcher 使用时，每个相关教学单元都会取得同一 provider 定义，不再依赖
  模型是否临时决定进行文本搜索；
- 初始 Evidence 和请求 fingerprint 改变，request revision 升为 `capability-teaching-request-v31`，旧结果不能
  继承当前资格；
- 不新增 NoneBot 插件特例、依赖执行、运行时数据库读取或跨单元注释复制。

## 落实与验证

- `capability_analysis_adapter.py::_function_parameter_dependencies` 对齐位置参数与仅关键字参数的默认值，并
  把直接 `Depends(provider)` 加入现有参数依赖队列；
- 回归测试覆盖默认值 provider 中的私聊判断进入初始源码 Evidence，并保留原有 `Annotated` 路径。

## 相关文档

- [ADR-0106：沿静态参数依赖补齐教学源码 Evidence](0106-follow-static-parameter-dependencies-for-teaching-evidence.md)
- [能力影子索引流程](../../architecture/flows/capability-shadow-index.md)
