# ADR-0016：将维护者评测工具排除在插件安装面之外

## 状态

已采纳

## 日期

2026-08-10

## 当时遇到了什么

ADR-0007 已决定让一个 `nonebot-plugin-triage` wheel 同时包含 `nonebot_plugin_triage` 插件入口和
`nbtriage` 传输无关领域核心。这个依赖方向仍然成立：插件运行时直接使用 `nbtriage` 的观察、报障、
限流和 Agent 领域契约，不能为了缩小发行面删除整个 `nbtriage` 命名空间。

当前 `[project.scripts]` 同时把 `nbtriage.cli:main` 安装为每个插件使用者都能看到的命令。该 CLI 的
`collect`、`discover`、`enrich-*`、`evaluate-*`、`session-*` 与 `publish-evaluation-mlflow` 等命令服务于
数据策展、离线评测、付费 Gate 和维护者审计，不是 Bot 部署者使用插件所需的公开接口。`tracking-mlflow`
也因此作为公开 optional extra 出现在包元数据中。源码公开不等于这些维护命令必须随插件安装。

当时的 source distribution 还会收录 `evals/` 中的历史冻结报告；wheel 虽未包含 `evals/`，但只检查
wheel 不能证明发布面已经收紧。

## 最后决定

1. 保留单仓库、单发行包和双命名空间：`nonebot_plugin_triage` 仍依赖 `nbtriage` 领域核心；本 ADR 不拆分
   第二个 PyPI 包，也不改变 ADR-0007 的依赖方向。
2. 插件的安装接口只包含 NoneBot 插件入口、配置和运行所需领域核心。删除默认发行包的 `nbtriage`
   console script；所有现有 `nbtriage` CLI 子命令统一归类为 maintainer tooling，不承诺为插件使用者提供
   稳定命令兼容。
3. 仓库内建立不进入 wheel 的 `tools/nbtriage_maintainer/`，承载 CLI 装配、采集 / 策展流程、评测
   orchestrator 和 MLflow 发布器。维护者从仓库使用
   `uv run python -m tools.nbtriage_maintainer <command>`；常用入口可由 `just` recipe 缩短，但 recipe 只转发，
   不复制参数语义。
4. 只有插件运行时实际依赖的领域模块留在 `src/nbtriage/`。移动模块前以
   `src/nonebot_plugin_triage/` 的真实导入图和领域模块间依赖为准；`RuntimeObservationBuffer`、
   `LiveIncidentBuffer`、`LiveTrialService`、有界 Agent 状态及其依赖不得为追求目录整齐而移出 wheel。
5. `tracking-mlflow` 从公开 optional extra 移到维护者开发依赖。插件安装、导入和启动不安装、导入或探测
   MLflow；维护者命令继续要求显式 Tracking URI，并保持当前发布前的脱敏、终态 audit 和幂等检查。
6. wheel 和 source distribution 都不得包含维护者 CLI、`evals/snapshots/`、本地报告、MLflow 状态或运行
   artifacts。`evals/` 中仍可版本化的是评测代码依赖的 catalog、合成 Fixture、split、rubric、人工策展
   判断和经过来源复核的 Oracle 合同；完整机器输出和历史运行报告默认进入本地 MLflow。
7. 对外需要说明性能或准入事实时，只在 README、架构文档或 Release 中写人工复核的聚合摘要，并记录源码
   revision、评测合同哈希、模型身份、日期和已知限制；不把完整逐 Case 输出重新复制回 Git。

## 具体怎么实施

1. 创建 `tools/nbtriage_maintainer/`，先迁移 `src/nbtriage/cli.py`、`src/nbtriage/__main__.py` 和
   `src/nbtriage/mlflow_tracking.py`，再按插件运行时导入图逐个迁移只被维护流程使用的 collector、curation、
   discovery 与 evaluation orchestrator。可复用的领域契约继续留在 `src/nbtriage/`。
2. 删除 `pyproject.toml` 的 `[project.scripts]` 与公开 `tracking-mlflow` extra，把 MLflow 锁定项移入开发
   依赖；新增 `just` recipe 作为仓库维护者入口。
3. 为 Hatch sdist 显式排除 `evals/snapshots/`、`tools/`、`data/`、`artifacts/`、`reports/`、`logs/` 和
   MLflow 状态；`tools/` 仍在 Git 仓库公开，但不进入 PyPI 源码包或 wheel。
4. 将依赖历史运行报告的测试改成最小合成 Fixture或在测试中确定性重算。测试 Fixture 表达行为合同，不能
   继续充当某次真实模型运行的历史副本。
5. 扩展 package-quality 检查：安装基础 wheel 后断言不存在 `nbtriage` console script 和 MLflow 依赖；
   同时检查 wheel、sdist 成员，拒绝 maintainer CLI、`evals/snapshots/` 和本地状态目录。

## 为什么这样选

- 插件仍能复用传输无关领域核心，不引入第二个发行包的版本配对和双发布成本；
- 维护者可以在公开仓库继续维护完整工具链，而安装者只获得运行插件必需的接口和依赖；
- console script 不能随 optional extra 隐藏。直接保留 `[project.scripts]` 再把 MLflow 设为 extra，仍会让
  每个安装者看到一个部分命令在运行时才报缺依赖的入口；
- wheel 与 sdist 同时检查，避免“wheel 干净但 GitHub Release 附带的 sdist 包含历史评测输出”。

## 没有采用的方案

- **立即拆出 `nbtriage-evals` PyPI 包**：公开接口最清楚，但当前没有第二个真实消费者，会提前引入配对版本、
  双构建和双发布流程。
- **保留 `nbtriage` console script，只在帮助中标注 maintainer-only**：迁移最少，但仍把维护入口安装给普通
  使用者，并把内部命令形状变成事实上的兼容负担。
- **只从 wheel 排除数据**：无法约束同一次 Release 发布的 sdist。

## 带来的影响

- 这是首次公开发布前的维护者命令迁移，不提供旧 console script 兼容层；仓库脚本、测试和文档需要一次性
  改用新模块入口。
- `nbtriage` Python 命名空间仍然公开可导入，但只有插件运行所需领域契约属于发行职责；内部模块不能仅因
  位于该命名空间就自动视为稳定第三方 API。
- 已有 `evals/snapshots/` 在实现本 ADR 时迁出 Git；相关文档改为引用人工摘要或 MLflow run 元数据，不能
  形成断链。

## 落实与确认

- 实施情况：已落实。维护者 CLI、采集 / 策展、离线评测、会话审计、bot-docs 索引和 MLflow 发布器已迁入
  `tools/nbtriage_maintainer/`；DeepSeek 评测适配器、历史 Responses 客户端、单步补证策略和尚未接入插件的
  Alconna capability experiment 也已迁出。`src/nbtriage/` 只保留插件运行路径和其领域依赖。真实导入闭包要求
  `baselines.py` 继续留在发行包，因为 RAG、证据回执和安全守门复用其中的版本与 secret 模式；这不是把
  评测 orchestrator 留在运行面。
- `[project.scripts]`、公开 `tracking-mlflow` 与只服务维护者评测的 `model-deepseek` extra 已删除；MLflow、
  DeepSeek SDK 与对应 Pydantic AI 依赖只存在于 `maintainer` dependency group。
  仓库入口是 `just maintainer <command>`，其完整形式为
  `uv run --group maintainer python -m tools.nbtriage_maintainer <command>`。
- Hatch wheel 只打包 `src/nbtriage` 与 `src/nonebot_plugin_triage`，sdist 继续显式排除 `tools/`、snapshot 和
  本地状态。安装态检查会拒绝 console script、MLflow、维护者模块和测试 Provider；归档成员检查同时审计
  wheel 与 sdist，防止只清理 wheel。
- 历史 `evals/snapshots/` 已迁入被忽略的本地报告目录，依赖历史报告的回归从版本化 Fixture、split、rubric、
  策展标注和 Oracle 确定性重算。
- 验证入口：`tests/test_package_metadata.py`、`scripts/verify_base_wheel.py`、
  `scripts/verify_distribution_contents.py`、`.github/workflows/ci.yml` 和 `.github/workflows/release.yml`。

## 替代关系

- 补充：[ADR-0007：采用单发行包与插件、领域核心双命名空间](0007-single-distribution-dual-namespace.md)
- 部分替代：[ADR-0015：分离版本化评测合同与本地运行数据](0015-separate-versioned-evals-from-local-runtime-data.md)
  中允许 `evals/snapshots/` 保存冻结机器报告的部分；其余目录所有权和 MLflow 本地状态忽略规则继续有效。

## 相关文档

- [架构概览](../architecture/overview.md)
- [ADR-0017：通过 pytest 执行确定性评测回归](0017-run-deterministic-evaluations-through-pytest.md)
