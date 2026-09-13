# ADR-0125：移除维护者评测的 MLflow Tracking 集成

## 状态

已采纳。局部替代 [ADR-0015](0015-separate-versioned-evals-from-local-runtime-data.md)、
[ADR-0016](0016-keep-maintainer-evaluation-tooling-out-of-install-surface.md) 与
[ADR-0017](history/0017-run-deterministic-evaluations-through-pytest.md) 中保留 MLflow 依赖、发布入口和
Tracking 历史的安排；评测合同、本地工件、插件发行边界与 pytest / CI 决定继续有效。

## 日期

2026-09-13

## 决定与理由

教学 fixture 评测已使用 Pydantic Evals 执行用例、调用领域评分器、重复运行和复评。本地 JSON 保存运行结果
及必要证据，现有其他评测继续使用其领域 evaluator。MLflow 仅承担另存一份实验查询副本，尚未形成需要
持续维护的网页比较或协作审查流程。维护双重存储和专用发布适配的收益不足。

移除维护者 MLflow 依赖、`publish-evaluation-mlflow` 命令、`just mlflow-server` 和专用发布器及其测试。
不另建平台或可选 Tracking 插件；将来出现明确的实验检索、网页审查或协作需求时重新选型。

`evals/` 继续保存版本化评测合同，本地 `reports/` / `artifacts/` 中的 JSON 继续作为结果追溯与复评依据。
项目作者已授权清理本项目的旧 MLflow 数据库和 artifact store；这些数据不再是运行依赖，清理范围不包含
本地评测 JSON、版本化 fixture、人工标注或其他项目。忽略规则继续保护可能残留的历史数据库及工件，
移除依赖不表示允许将其发布。

## 验证边界

确认 CLI 不再导入发布器，项目依赖不再声明 MLflow，教学评测与复评、其他评测及打包回归继续通过。
不要求新建付费模型实验，也不把历史 Tracking 的存在或清理结果当作模型质量证据。
