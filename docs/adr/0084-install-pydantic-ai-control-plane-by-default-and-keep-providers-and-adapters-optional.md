# ADR-0084：默认安装 Pydantic AI 控制层，Provider 与 NoneBot Adapter 仍保持可选

> 后续关系：ADR-0086 允许部署者自由使用 Pydantic AI 可解析的模型；Provider SDK 仍按本 ADR 按需安装，
> held-out 状态只表示项目验证质量，不再决定模型能否运行。

| 状态 | 决策日期 |
|---|---|
| 已采纳 | 2026-08-17 |

## 背景

教学注释、公开 Answer、语义分类和 Bug 分析的领域实现已经直接使用 Pydantic AI 的 Agent、结构化输出、
Toolset、Harness 与 usage 类型。此前这些公共运行时依赖只随 `openai` / `anthropic` extra 安装，导致
基础 wheel 在没有任何模型 extra 时导入插件会缺少 `pydantic_ai`。这与“模型未配置时插件仍必须正常加载”
的启动合同冲突。

项目同时有较多 OneBot V11 代码，但它们承担的是群历史、回复引用和出站消息关联等平台增强；跨平台的命令
入口、能力索引、教学注释领域层和通用 Bug 流程并不以 OneBot 为前提。让插件 extra 代替宿主安装 Adapter，
也会把“插件能力”和“Bot 选择并注册的平台协议”混成一个安装责任。

## 决策

1. `pydantic-ai-slim==2.28.0`、`pydantic-ai-harness==0.20.0` 与 `jedi==0.20.0` 成为插件基础依赖。
   它们共同提供所有模型任务复用的控制层、只读文件工具和定义导航，不再归属于某个 Provider extra。
2. `openai` 与 `anthropic` extras 只复用 Pydantic AI 对应的 Provider extra，用于安装该 Provider 的 SDK。
   安装公共控制层本身不启用模型，也不产生网络请求；后续 ADR-0085 已移除 Serena MCP extra。
3. Provider 实现必须延迟导入。任务资格、模型名、预算和评测 revision 放在不依赖 Provider SDK 的合同模块；
   只有实际创建已选择 Provider 的客户端时才导入对应 adapter 和 SDK。
4. 删除 `onebot` 与 `discord` 插件 extras。NoneBot Adapter 由宿主 Bot 按自身平台选择、安装并注册；Triage
   不替宿主安装或注册 Adapter。
5. OneBot / Discord 专属增强继续采用运行时发现和延迟导入。目标 Adapter 不存在时，这些 Provider 不注册，
   通用功能继续工作；不得因专属模块无法导入而使插件加载失败。
6. 测试依赖组仍可安装 OneBot 和 Discord Adapter，用于验证平台增强与跨平台行为。测试依赖不构成发布
   wheel 的运行依赖。
7. 缺少模型配置、API Key、网络或任务资格继续按 ADR-0063 失败关闭对应模型增强；缺少所选择 Provider 的
   extra 应在创建客户端时形成明确依赖错误，不得在插件导入阶段崩溃。

## 影响

- 基础安装会增加 Pydantic AI 公共控制层、Harness 和 Jedi 的体积，但它与当前核心运行代码的真实 import
  边界一致，避免发布一个只能在偶然安装模型 extra 后才可导入的 wheel。
- 使用 OpenCode Go 或 OpenAI Provider 的部署仍安装 `[openai]`；使用 Anthropic 的部署安装
  `[anthropic]`。其他 Provider 不会因基础安装而被强制引入。
- OneBot 不是强制依赖。未安装 OneBot 时，群历史读取、OneBot 出站回复引用等增强不可用；Alconna / UniSeg
  入口、确定性能力索引和其他 Adapter 上的通用链路不受影响。
- Provider 常量与 Provider 实现分离，运行时模块可以在未安装 OpenAI SDK 的环境中构造未启用的服务。

## 替代关系

- 部分替代 [ADR-0047](0047-reuse-pydantic-ai-provider-extras.md)：继续复用上游 Provider extras，但
  Pydantic AI 公共层不再只通过 Provider extra 安装。
- 部分替代 [ADR-0059](0059-share-read-only-evidence-access-across-agent-flows.md)：Harness 与 Jedi 从模型
  extra 移入基础依赖；其只读工具、安全边界和共享职责不变。
- [ADR-0085](0085-remove-serena-bug-source-backend.md) 进一步删除 Serena MCP extra 与 Bug-only 后端；
- 延续 [ADR-0063](0063-keep-plugin-startup-independent-from-model-enhancements.md) 的启动降级合同。
- 不改变 [ADR-0004](0004-onebot-v11-first-and-keyed-message-reference-index.md) 的 OneBot 首个 dogfood
  地位；只明确它不是所有部署的安装前提。

## 落实与确认

- 包元数据测试固定基础控制层依赖与三个公开 extras，并拒绝在基础依赖中声明 NoneBot Adapter；
- 子进程测试拦截 OpenAI、Anthropic、OneBot 与 Discord 导入后加载插件，验证公共启动链不触碰这些可选包；
- 构建基础 wheel，在不安装任何 extra 与 Adapter 的独立环境中执行 NoneBot 初始化和插件加载；
- 构建与锁文件验证 Provider SDK 仍只来自对应 Pydantic AI extra。

## 相关文档

- [模型 Provider 支持矩阵](../architecture/model-provider-support.md)
- [架构总览](../architecture/overview.md)
- [README 安装说明](../../README.md)
