# ADR-0015：分离版本化评测合同与本地运行数据

## 状态

已采纳；允许 `evals/snapshots/` 保存冻结机器报告的部分由 [ADR-0016](0016-keep-maintainer-evaluation-tooling-out-of-install-surface.md) 替代

## 日期

2026-08-10

## 当时遇到了什么

仓库原有 `data/` 同时保存可版本化的 catalog、Fixture、split、人工标注与 Oracle 运行结果，以及由采集、
策展流程生成的 raw、Case、候选 Gold 和 discovery 工作数据。根 `artifacts/` 也同时承担公开冻结报告与本地
cache/session。维护者仅看路径无法判断一份文件是否已经完成来源、隐私和许可证复核，新增子目录时还容易
选错忽略规则。

项目后续计划引入 MLflow Tracking。MLflow 的 experiment、run、trace、metric、数据库和 artifact store
属于可查询运行状态；当前 Git 数据则承担可审查输入、策展决定、回归合同与少量冻结摘要。若两者都继续放在
含义宽泛的 `data/` 或 `artifacts/`，将无法清晰定义真源、发布边界与重跑关系。

## 最后决定

1. 新建 `evals/` 作为可以进入版本管理的评测合同边界：
   - `evals/datasets/catalog/` 保存候选池与仓库目录；
   - `evals/datasets/fixtures/` 保存独立合成 Fixture；
   - `evals/datasets/splits/` 保存冻结 split；
   - `evals/curation/annotations/` 与 `evals/curation/batches/` 保存人工策展判断；
   - `evals/oracles/` 保存经过引用校验的 Oracle 运行结果；
   - `evals/snapshots/` 保存经过脱敏、明确挑选的冻结机器报告。
2. `data/` 只保存 `raw/`、`cases/`、`gold/`、`discovery/` 等可重建或需复核的本地工作数据，并在共享
   `.gitignore` 中整体忽略；CLI 仍默认在该目录完成采集和策展中间步骤。
3. `reports/` 与根 `artifacts/` 只保存本地报告、cache、session 或尚未策展的运行产物，并整体忽略。
   需要长期保存的摘要必须经过脱敏和来源复核后显式进入 `evals/snapshots/`，不能用反向包含规则从本地目录
   自动发布。
4. Git 中的冻结数据集和评测配置是当前真源。未来 MLflow run 必须记录相应内容哈希、版本或源码 revision，
   MLflow 不反向成为未经审查的数据集发布源；若生产 Trace 策展改变这一关系，另立 ADR。
5. MLflow 本地运行状态采用 MLflow 上游仓库已经使用的 runtime 忽略项，包括 `mlruns/`、
   `mlartifacts/`、`mlruns*.db`、`mlflow.db` 与 SQLite WAL/SHM 文件。GitHub 的 `github/gitignore` 模板库
   没有 MLflow 专用模板，因此不声称存在可直接套用的 VS Code 整体模板；不复制 MLflow 源码仓库专属的
   protobuf 生成路径，也不采用含义过宽的 `outputs/`。

## 为什么这样选

- `evals/` 描述的是数据集、策展、Oracle、回归与安全 Gate 的共同合同，比只表示固定性能测试的
  `benchmarks/` 更符合当前和计划能力；
- Git 与 Tracking Server 各自只有一个清晰职责：前者保存可审查、可复现的少量合同，后者保存大量可查询的
  运行关系和 telemetry；
- 整体忽略本地目录比持续维护允许/拒绝子目录清单更不容易误发布原始 Issue、私密日志、Token 或中间 Gold；
- 迁移发生在仓库尚无公开 Git 基线时，不需要为已发布路径保留兼容层。

## 没有采用的方案

- **继续在 `data/` 内精确忽略子目录**：迁移量小，但目录语义混合且每个新目录都需要安全判断；
- **统一使用 `benchmarks/`**：无法自然覆盖人工策展、Trace 回流、安全 Fixture、Oracle 与 Provider 资格；
- **把全部评测运行输出提交到 Git**：会快速膨胀仓库，并绕过隐私、许可证与结果选择复核；
- **让 MLflow 成为当前数据集真源**：项目尚未部署稳定 Tracking Server，也会让核心回归依赖外部服务。

## 带来的影响

- CLI 默认路径、测试、README、架构文档、计划和已有工件链接需要同步迁移；
- 本地旧路径不会自动兼容，维护者应在迁移后只使用新命令默认值；
- `.gitignore` 可以按目录整体表达安全边界，`git check-ignore` 将成为防止本地状态误入版本控制的验证；
- 引入 MLflow 时只需配置 Tracking URI 和 artifact store，不再重新决定 Git 目录的职责。

## 相关文档

- [架构概览](../architecture/overview.md)
- [ADR-0016：将维护者评测工具排除在插件安装面之外](0016-keep-maintainer-evaluation-tooling-out-of-install-surface.md)
- [MLflow Evaluation Dataset](https://mlflow.org/docs/latest/genai/datasets/)
- [MLflow Tracking Server](https://mlflow.org/docs/latest/self-hosting/architecture/tracking-server/)
- [MLflow 上游 `.gitignore`](https://github.com/mlflow/mlflow/blob/master/.gitignore)
- [GitHub `.gitignore` 模板库](https://github.com/github/gitignore)
