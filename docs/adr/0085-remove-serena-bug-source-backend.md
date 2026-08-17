# ADR-0085：移除 Serena Bug 源码后端

| 状态 | 决策日期 |
|---|---|
| 已采纳；已实现 | 2026-08-17 |

## 背景

ADR-0056 曾实现一个仅供 Bug 调查显式启用的 Serena MCP 源码导航后端。它需要部署者安装插件的
`serena` extra，并在 Bot 虚拟环境之外维护 Serena 本体、Pyright、MCP 进程、独立缓存和固定只读
context。默认 Bug 路径始终是项目内置的有界文本读取，因此这套后端不是插件启动、教学注释或 Bug
判断的必要条件。

后续 ADR-0057 与 ADR-0059 已经为共享源码工具选择 Direct Jedi 和只读 FileSystem。教学注释已经消费这套
领域工具，而 Serena 仍是仅服务 Bug 的第二套符号后端，需要单独维护依赖、配置、进程生命周期、安全净化
与回退测试。项目作者决定撤销该可选路线，先让 Bug 保持单一、可预测的内置源码读取边界。

## 决策

1. 删除 `serena` optional dependency；基础依赖和 Provider extras 不再安装 Pydantic AI MCP 客户端。
2. 删除 `NBTRIAGE_BUG_SOURCE_BACKEND`。Bug 源码读取固定使用 `BoundedSourceReader`：只在当前已加载
   subject 的批准插件根内搜索或读取 `.py`，继续执行文件数、文件大小、结果数、路径和 symlink 门禁。
3. 删除 Serena MCP runtime、固定 context 和专用测试。插件不会启动 Serena / Pyright 子进程，也不会
   创建 Serena cache。
4. 旧配置 `NBTRIAGE_BUG_SOURCE_BACKEND` 不静默忽略；配置校验明确提示该设置已移除，避免部署者误以为
   符号导航仍在工作。
5. Direct Jedi 和共享只读 FileSystem 继续服务现有教学注释链。本决定不自动把它们接入 Bug；Bug 若需要
   跨文件定义导航，应复用既有共享领域工具并单独验证其工具预算、数据投影和模型资格。
6. 不改变 Bug Agent 已有的公开合同、日志、运行观察、设计知识、聊天证据、Evidence reconciliation 或
   正式 Problem 写入边界。源码导航能力下降时，证据不足仍得到 `unknown`，不能由模型猜测补齐。

## 影响

- 发布包只保留 `openai` 与 `anthropic` Provider extras；安装和升级路径更简单；
- Bug 源码搜索失去 Serena 的符号与引用导航，但保留批准根内的有界文本搜索和文件读取；
- 教学注释的 ast-grep、只读文件工具与 Direct Jedi 不受影响；
- 历史 ADR-0056 继续说明当时为什么尝试 Serena，但不再代表当前产品能力。

## 替代关系

- 替代 [ADR-0056](0056-use-serena-for-optional-bug-source-navigation.md)；
- 收窄 [ADR-0057](0057-select-source-analysis-tools-by-evidence-stage.md) 中“保留既有 Bug-only Serena 纵切”的
  例外；项目当前只有 Direct Jedi 语义导航与有界文本兜底；
- 更新 [ADR-0084](0084-install-pydantic-ai-control-plane-by-default-and-keep-providers-and-adapters-optional.md)
  的 optional dependency 集合，不改变 Pydantic AI 控制层与 Provider SDK 分离原则。

## 验证

- 包元数据只声明 `openai`、`anthropic` 两个 extras，锁文件不再因本项目声明 MCP extra；
- 源码与测试中不存在 Serena runtime、context 或配置字段；
- Bug runtime 回归证明源码工具仍能在批准根中搜索和读取，并继续失败关闭越界路径；
- 插件导入、配置、静态检查与全量测试通过。
