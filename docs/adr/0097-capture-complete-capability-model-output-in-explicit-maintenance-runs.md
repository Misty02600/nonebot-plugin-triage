# ADR-0097：在显式单插件维护运行中保存完整教学模型输出

| 状态 | 决策日期 |
|---|---|
| 已采纳；thinking 捕获由 [ADR-0103](history/0103-enable-opencode-go-thinking-and-capture-maintenance-reasoning.md) 替代 | 2026-08-20 |

## 背景

生产 Agent trace 只保存调用结构、工具名、用量、耗时和稳定错误码。真实 `nonebot_plugin_memes`
教学刷新暴露出仅靠 `projection_public_text` 无法区分模型误读、Prompt 歧义和模型外校验器冲突；失败响应
已经被丢弃后，也无法从安全摘要恢复具体字段。官方合成 fixture 的诊断例外又不能覆盖真实第三方插件。

## 决策

1. 生产运行继续遵循 ADR-0089，默认 telemetry 不保存 Prompt、源码、模型正文或工具正文。
2. 维护者命令 `analyze-capability-teaching` 可以通过 `--capture-model-output <path>` 为本次精确单插件刷新
   保存完整 assistant 文本、结构化 tool call 参数、tool result 和 correction。捕获在成功与失败尝试后都执行。
3. 诊断文件不保存初始 system/user Prompt 和 thinking；但 tool result 可能包含该插件的真实源码，因此文件
   仍按敏感本地工件处理，只能写入维护者显式选择的 Git ignored 路径，不进入 cache、公开报告或生产 trace，
   也不自动上传。保留和删除由维护者管理。
4. `--unbounded` 只用于同一维护命令，并要求同时提供诊断文件。它移除项目侧 `max_tokens`、请求数、工具数、
   input/output/total token 和成本止损，以便判断失败是否来自项目预算；Provider 自身限制不受项目控制。
5. 无界模式仍保留每次 Agent 尝试的运行超时、有限输出校验重试和最多两次单元级尝试。这里的“无界”只指
   用量止损，不允许维护进程永久悬挂。

## 影响

- 维护者能够逐字复盘真实插件的无效候选和 correction，不再靠粗粒度错误码猜测；
- 明确扩大了显式诊断文件的敏感面，尤其是工具返回的源码；生产默认和普通配置没有改变；
- 无界诊断可能显著增加 Provider 调用、token、费用和运行时间，结果不得继承正式 held-out 资格。

## 落实与验证

- `PydanticAICapabilityAnalysisClient.enable_maintenance_diagnostics()` 在首次调用前开启完整输出捕获，并可移除
  项目侧用量限制；
- maintainer 单插件包装器为每次尝试绑定 `unit_id`，最终原子写入显式 JSON 路径；
- CLI 暴露 `--capture-model-output` 和 `--unbounded`，后者没有诊断路径时失败关闭；
- `nonebot_plugin_memes 0.8.1` 的无界运行保存了 18 次尝试、约 500 KB 诊断内容，并据此确认 canonical usage
  与公开文本内部符号校验存在确定性冲突。

## 替代关系

- 部分替代 [ADR-0089](0089-persist-redacted-pydantic-ai-agent-traces.md) 第 7 项：实现精确单插件、短时、显式路径
  的维护捕获；生产 trace 的无正文决定继续有效。
- 部分替代 [ADR-0094](0094-simplify-the-public-capability-teaching-contract.md) 第 14 项：诊断范围不再只限官方
  合成 fixture，也允许维护者明确选择的真实插件；默认生产隐私边界继续有效。
