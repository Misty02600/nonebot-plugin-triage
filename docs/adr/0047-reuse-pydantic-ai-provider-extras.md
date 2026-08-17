# ADR-0047：直接复用 Pydantic AI 的 Provider extras

| 状态 | 决策日期 |
|---|---|
| 已采纳 | 2026-08-13 |

> 2026-08-14：为接入共享只读 Harness，Provider extras 已整体从 `pydantic-ai-slim==2.27.0`
> 精确升级到 `2.28.0`；本 ADR 的“直接复用上游 Provider extra、不重复声明 SDK”边界不变。
>
> 2026-08-17：[ADR-0084](0084-install-pydantic-ai-control-plane-by-default-and-keep-providers-and-adapters-optional.md)
> 将 Pydantic AI 公共控制层、Harness 与 Jedi 移入基础依赖；本 ADR 仅继续拥有 Provider SDK extra 的上游
> 复用边界。

## 当时遇到了什么

插件曾分别声明 `model-anthropic`、`model-openai` 和 `model-opencode-go` extras，并在每个 extra
中重复固定 Pydantic AI Provider extra 已经声明的底层 SDK。OpenCode Go 与 OpenAI 的依赖内容完全相同，
但仍暴露两个安装入口；安装者需要理解项目自造的 `model-` 前缀和一组没有依赖差异的名称。

Pydantic AI 2.27.0 已把 Provider 安装边界公开为 `pydantic-ai-slim[anthropic]` 和
`pydantic-ai-slim[openai]`。这些 extras 自己负责声明兼容的 Anthropic / OpenAI SDK 及 Provider 所需传递
依赖，因此插件无需平行复制同一依赖关系。

## 决策

1. 插件只公开两个模型 Provider extras：
   - `anthropic = ["pydantic-ai-slim[anthropic]==2.27.0"]`；
   - `openai = ["pydantic-ai-slim[openai]==2.27.0"]`。
2. 删除 `model-` 前缀，不再公开 `model-anthropic` 或 `model-openai`。
3. 删除依赖内容重复的 `model-opencode-go` / `opencode-go` extra。OpenCode Go 使用 Pydantic AI 的
   OpenAI-compatible Provider，安装者统一安装 `nonebot-plugin-triage[openai]`；运行时 backend/model 配置
   仍负责区分 OpenAI Responses 与 OpenCode Go Chat。
4. 插件 optional dependencies、仓库 `dev` / `maintainer` dependency groups 不再直接重复声明
   `anthropic` 或 `openai` SDK；兼容约束由锁定的 Pydantic AI Provider extra 提供，最终解析版本由
   `uv.lock` 固定。
5. 该简化只改变 Python 包的安装接口和依赖所有权，不改变 Provider/model/task 资格表、API Key 来源、
   固定 endpoint、预算、隐私、零重试和 held-out Gate。

## 原因与影响

- 安装名称与上游 Provider 名称一致，减少一层项目特有命名；
- 同一个依赖栈只声明一次，OpenCode Go 不再伪装成不同的安装能力；
- 插件仍可直接 import SDK 类型，因为这些 SDK 是 Pydantic AI Provider extra 的公开传递依赖；
- 旧安装命令是破坏性迁移：`[model-anthropic]` 改为 `[anthropic]`，`[model-openai]` 与
  `[model-opencode-go]` 均改为 `[openai]`；
- 真实 Provider 资格仍按项目评测结果管理，不能从“依赖可以安装”推导出模型已获准进入运行链。

## 替代关系

- 部分替代 [ADR-0008](0008-pydantic-ai-controlled-model-adaptation.md) 中项目 extra 自行固定底层 SDK 和
  使用 `model-` 前缀的安装决定；其 Pydantic AI Model / Provider / Profile 分层继续有效。
- 部分替代 [ADR-0041](0041-qualify-opencode-go-tool-output-for-support-semantics.md) 中独立
  `model-opencode-go` extra 的决定；OpenCode Go 的精确语义任务资格和运行边界不变。

## 落实与确认

- `pyproject.toml` 只保留 `anthropic` 与 `openai` 两个模型 Provider extras；
- runtime 和维护者 CLI 的缺依赖提示使用新安装名称；
- README、Provider 支持矩阵、架构总览和包元数据测试同步新公开接口；
- `uv.lock` 继续固定实际解析出的 SDK 版本。

## 相关文档

- [模型 Provider 支持矩阵](../architecture/model-provider-support.md)
- [架构总览](../architecture/overview.md)
