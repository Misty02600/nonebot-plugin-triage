"""仓库维护者使用的离线模型评测编排。"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections import Counter
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast
from uuid import uuid4

from nbtriage.baselines import B0SearchIndex, extract_version_values, predict_b0
from nbtriage.rag import (
    B1_PROMPT_ID,
    B1Error,
    B1ModelClient,
    B1ModelResponse,
    B1Prediction,
    B1ResponseCache,
    B1Runner,
    TrainCaseRetriever,
    build_b1_request,
    parse_b1_output,
)
from nbtriage.safety import detect_case_safety_risks
from tools.nbtriage_maintainer.evaluation_provenance import (
    EvaluationProvenanceError,
    case_corpus_sha256,
    evaluation_code_revision,
)
from tools.nbtriage_maintainer.strict_json import StrictJsonError, strict_json_loads

ROUTE_BY_MODE = {
    "nonebug_exec": "verify",
    "sandbox_exec": "verify",
    "contract_exec": "verify",
    "diagnose_only": "needs_evidence",
    "escalate": "escalate",
}
GAP_KEYWORDS = {
    "python_version": ("python version", "python 版本", "python版本"),
    "component_versions": (
        "version",
        "versions",
        "版本",
        "lockfile",
        "lock file",
        "dependency set",
    ),
    "operating_system": ("operating system", "windows", "linux", "操作系统"),
    "logs": ("log", "trace", "traceback", "日志", "堆栈"),
    "reproduction_steps": (
        "reproduc",
        "fixture",
        "minimal project",
        "最小复现",
        "复现",
    ),
    "expected_behavior": ("contract", "expected", "documented", "契约", "预期"),
    "configuration": (
        "config",
        "lockfile",
        "proxy",
        "database url",
        "配置",
        "代理",
    ),
    "deployment_topology": (
        "topology",
        "process",
        "connection",
        "concurrent",
        "deployment",
        "进程",
        "连接",
        "并发",
        "部署",
    ),
    "raw_close_evidence": ("close code", "close reason", "关闭码", "关闭原因"),
}
_CASE_ID_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?$")
B1_EVALUATION_ID = "b1-rag-only-v1"
B1_CUSTOM_EVALUATION_ID = "b1-rag-only-custom-unqualified-v1"
_B1_OFFICIAL_SPLIT_ID = "data-gate-v1"
_B1_OFFICIAL_SPLIT_SHA256 = "ce2a95a98665efb012b3b4cdc0bbd4d07e8a82dd6916b80c4f0fa0c843853a24"
_B1_OFFICIAL_CORPUS_SHA256_BY_SCORE_SPLIT = {
    "validation": "1c8cfc15189bb53c526197901c339156539d7311f75212785e5bf87fa2e47482",
    "heldout": "55926a03423f0f012dd5d5e4ee75a1510545b27e1ce7ef739a4598d33755c12b",
}
_B1_OFFICIAL_MAX_OUTPUT_TOKENS = 1024
_B1_OFFICIAL_BUDGET_USD_BY_SCORE_SPLIT = {
    "validation": 0.1,
    "heldout": 0.05,
}
_B1_RESPONSE_MANIFEST_DOMAIN = b"nbtriage-b1-response-manifest-v1\0"
_B1_RESPONSE_MANIFEST_PREFIX = "nbtriage-b1-response-manifest-sha256:"
_B1_FORMAL_PROVIDER_NAMES = {
    "deepseek-responses": frozenset({"deepseek-responses"}),
}
_B1_LIMITATIONS = [
    "B1 uses source-only target input and train-only retrieved cases; "
    "curation and Gold are passed only to the shared scorer.",
    "Model output can reflect pretraining exposure to historical public Issues; "
    "a forward hidden set or counterfactual fixtures are still required.",
    "The v1 corpus has no duplicate root-cause groups or qualified S3 cases, so "
    "duplicate Recall@5 is not applicable and unsafe-refusal coverage is insufficient.",
    "Version metrics compare normalized values, not package-to-version association.",
    "Response-cache content addressing proves internal consistency, not signature "
    "authenticity; a report and forged cache changed together remain outside this gate's "
    "trust claim.",
    "execution_observation is self-reported and unverified; model-call and cache-hit "
    "counts are not part of the reproducible quality claim.",
]
_B1_ROOT_FIELDS = {
    "schema_version",
    "evaluation_id",
    "evaluation_qualification",
    "evaluation_contract",
    "split_id",
    "generated_at",
    "source",
    "summary",
    "execution_observation",
    "metrics_by_split",
    "predictions",
    "limitations",
}
_B1_SOURCE_FIELDS = {
    "cases_dir",
    "split_path",
    "split_sha256",
    "case_corpus_sha256",
    "case_corpus_scope",
    "case_count",
    "official_split_id",
    "official_split_sha256",
    "official_case_corpus_sha256",
    "response_cache_dir",
    "response_manifest_sha256",
}
_B1_SUMMARY_FIELDS = {
    "case_count",
    "train_count",
    "validation_count",
    "heldout_count",
    "provider",
    "model",
    "prompt_id",
    "generation_config",
    "score_splits",
    "declared_budget_usd",
    "provider_response_count",
    "input_tokens",
    "output_tokens",
    "latency_ms",
    "external_tool_calls",
}
_B1_EXECUTION_OBSERVATION_FIELDS = {
    "verification",
    "model_calls",
    "cache_hits",
}
_B1_ROW_FIELDS = {"split", "case_id", "support_level", "gold", "prediction"}
_B1_GOLD_FIELDS = {
    "route",
    "fault_phase",
    "symptoms",
    "candidate_owners",
    "missing_evidence",
    "source_version_values",
}
_B1_PREDICTION_FIELDS = {
    "case_id",
    "baseline_id",
    "version_values",
    "missing_evidence",
    "symptoms",
    "fault_phase",
    "candidate_owners",
    "route",
    "answer",
    "citations",
    "retrieved_evidence",
    "secret_risk_detected",
    "safety_risks",
    "input_tokens",
    "output_tokens",
    "latency_ms",
}
_B1_RESPONSE_REQUIRED_FIELDS = {
    "output_text",
    "input_tokens",
    "output_tokens",
    "provider_request_id",
    "latency_ms",
}
_B1_RESPONSE_OPTIONAL_FIELDS = {
    "cost_microusd",
    "provider_name",
    "provider_model_name",
    "provider_fingerprint",
}


class EvaluationReportPublishError(OSError):
    """终态报告无法发布，但完整结果已保留为可恢复工件。"""

    def __init__(self, recovery_path: Path) -> None:
        self.recovery_path = recovery_path
        super().__init__(
            "evaluation report target could not be published; "
            f"complete report retained at {recovery_path}"
        )


@dataclass(frozen=True)
class EvaluationReportReservation:
    """一次终态报告发布权预留。"""

    report_path: Path
    marker_path: Path
    token: str


class EvaluationError(ValueError):
    pass


class EvaluationPrediction(Protocol):
    @property
    def case_id(self) -> str: ...

    @property
    def version_values(self) -> list[str]: ...

    @property
    def missing_evidence(self) -> list[str]: ...

    @property
    def symptoms(self) -> list[str]: ...

    @property
    def fault_phase(self) -> str: ...

    @property
    def candidate_owners(self) -> list[str]: ...

    @property
    def route(self) -> str: ...

    def to_dict(self) -> dict[str, Any]: ...


@dataclass(frozen=True)
class EvaluationDataset:
    split_id: str
    split_case_ids: dict[str, list[str]]
    cases: dict[str, dict[str, Any]]
    split_raw: bytes
    case_raw_by_id: dict[str, bytes]
    cases_dir: Path
    split_path: Path


@dataclass
class MultiLabelCounts:
    true_positive: int = 0
    false_positive: int = 0
    false_negative: int = 0

    def add(self, expected: set[str], predicted: set[str]) -> None:
        self.true_positive += len(expected & predicted)
        self.false_positive += len(predicted - expected)
        self.false_negative += len(expected - predicted)

    def report(self) -> dict[str, float | int]:
        precision = _ratio(
            self.true_positive,
            self.true_positive + self.false_positive,
        )
        recall = _ratio(
            self.true_positive,
            self.true_positive + self.false_negative,
        )
        f1 = _ratio(2 * precision * recall, precision + recall)
        return {
            "true_positive": self.true_positive,
            "false_positive": self.false_positive,
            "false_negative": self.false_negative,
            "precision": precision,
            "recall": recall,
            "f1": f1,
        }


def evaluate_b0(cases_dir: Path, split_path: Path) -> dict[str, Any]:
    code_revision = _current_evaluation_code_revision()
    dataset = load_evaluation_dataset(cases_dir, split_path)
    train_cases = [dataset.cases[case_id] for case_id in dataset.split_case_ids.get("train", [])]
    search_index = B0SearchIndex(train_cases)
    predictions: dict[str, EvaluationPrediction] = {
        case_id: cast(
            EvaluationPrediction,
            predict_b0(dataset.cases[case_id], search_index),
        )
        for case_id in sorted(dataset.cases)
    }
    report = _build_evaluation_report(
        dataset,
        predictions,
        evaluation_id="b0-checklist-v1",
        run_summary={"model_calls": 0, "external_tool_calls": 0},
        limitations=[
            "B0 uses only Issue title, body, labels and repository identity; "
            "curation and Gold are passed only to the scorer.",
            "data-gate-v1 contains no repeated root-cause group, so duplicate Issue "
            "Recall@5 has no denominator and is reported as not applicable.",
            "S3 has no qualified historical Case in data-gate-v1; unsafe-refusal "
            "scoring requires separate adversarial fixtures before B0-B3 comparison "
            "is complete.",
            "Version metrics compare normalized version values, not "
            "package-to-version association accuracy.",
        ],
        code_revision=code_revision,
    )
    _ensure_evaluation_code_unchanged(code_revision)
    return report


async def evaluate_b1(
    cases_dir: Path,
    split_path: Path,
    *,
    client: B1ModelClient,
    model: str,
    cache_dir: Path,
    provider: str = "injected",
    generation_config: dict[str, Any] | None = None,
    score_splits: tuple[str, ...] | None = None,
    declared_budget_usd: float | None = None,
) -> dict[str, Any]:
    """使用注入的模型客户端运行 B1，正式供应商选择由调用方显式决定。

    Args:
        cases_dir: 生成的 SupportCase 目录。
        split_path: 冻结 split 清单。
        client: 只负责一次文本生成的模型客户端，不具备工具执行能力。
        model: 写入请求哈希与报告的精确模型标识。
        cache_dir: 按完整请求哈希保存响应的本地缓存目录。

    Returns:
        与 B0 使用相同 Gold 映射和分层指标的 B1 报告。

    Raises:
        EvaluationError: split 或 Case 工件无效。
        B1Error: 模型输出或响应缓存无效。
    """
    code_revision = _current_evaluation_code_revision()
    dataset = load_evaluation_dataset(cases_dir, split_path)
    selected_splits = tuple(dataset.split_case_ids) if score_splits is None else score_splits
    unknown_splits = set(selected_splits) - set(dataset.split_case_ids)
    if unknown_splits:
        raise EvaluationError(f"unknown score splits: {sorted(unknown_splits)}")
    if not selected_splits:
        raise EvaluationError("score_splits must not be empty")
    train_cases = [dataset.cases[case_id] for case_id in dataset.split_case_ids.get("train", [])]
    resolved_cache_dir = cache_dir.resolve()
    runner = B1Runner(
        client,
        model,
        TrainCaseRetriever(train_cases),
        B1ResponseCache(resolved_cache_dir),
        provider=provider,
        generation_config=generation_config,
    )
    evaluated_case_ids = {
        case_id for split_name in selected_splits for case_id in dataset.split_case_ids[split_name]
    }
    predictions = {}
    for case_id in sorted(evaluated_case_ids):
        predictions[case_id] = await runner.predict(dataset.cases[case_id])
    model_calls = sum(prediction.model_calls for prediction in predictions.values())
    cache_hits = sum(prediction.cache_hit for prediction in predictions.values())
    provider_response_count = sum(
        prediction.provider_request_id is not None for prediction in predictions.values()
    )
    split_sha256 = hashlib.sha256(dataset.split_raw).hexdigest()
    corpus_case_ids = set(dataset.split_case_ids.get("train", [])) | evaluated_case_ids
    corpus_sha256 = case_corpus_sha256(dataset.case_raw_by_id, corpus_case_ids)
    is_official = _is_b1_official_dataset(
        dataset,
        selected_splits=selected_splits,
        split_sha256=split_sha256,
        corpus_sha256=corpus_sha256,
        provider=provider,
        model=model,
        generation_config=generation_config or {},
        declared_budget_usd=declared_budget_usd,
    )
    evaluation_id = B1_EVALUATION_ID if is_official else B1_CUSTOM_EVALUATION_ID
    report = _build_evaluation_report(
        dataset,
        predictions,
        evaluation_id=evaluation_id,
        score_split_names=selected_splits,
        run_summary={
            "provider": provider,
            "model": model,
            "prompt_id": B1_PROMPT_ID,
            "generation_config": generation_config or {},
            "score_splits": list(selected_splits),
            "declared_budget_usd": declared_budget_usd,
            "provider_response_count": provider_response_count,
            "input_tokens": sum(prediction.input_tokens for prediction in predictions.values()),
            "output_tokens": sum(prediction.output_tokens for prediction in predictions.values()),
            "latency_ms": sum(prediction.latency_ms for prediction in predictions.values()),
            "external_tool_calls": 0,
        },
        limitations=_B1_LIMITATIONS,
        code_revision=code_revision,
    )
    report["schema_version"] = 2
    report["evaluation_qualification"] = (
        "official_frozen_dataset" if is_official else "custom_unqualified"
    )
    report["execution_observation"] = {
        "verification": "self_reported_unverified",
        "model_calls": model_calls,
        "cache_hits": cache_hits,
    }
    report["source"].update(
        {
            "official_split_id": _B1_OFFICIAL_SPLIT_ID,
            "official_split_sha256": _B1_OFFICIAL_SPLIT_SHA256,
            "official_case_corpus_sha256": _B1_OFFICIAL_CORPUS_SHA256_BY_SCORE_SPLIT.get(
                selected_splits[0]
            )
            if len(selected_splits) == 1
            else None,
            "response_cache_dir": resolved_cache_dir.as_posix(),
            "response_manifest_sha256": _b1_response_manifest_sha256(
                dataset,
                selected_splits=selected_splits,
                provider=provider,
                model=model,
                generation_config=generation_config or {},
                cache_dir=resolved_cache_dir,
            ),
        }
    )
    for row in report["predictions"]:
        row["prediction"].pop("provider_request_id", None)
        row["prediction"].pop("cache_hit", None)
        row["prediction"].pop("model_calls", None)
    _ensure_evaluation_code_unchanged(code_revision)
    if is_official:
        validate_b1_evaluation_report(report)
    return report


def validate_b1_evaluation_report(report: dict[str, Any]) -> None:
    """严格重放一份 B1 正式报告及其本地响应缓存。

    此校验只建立报告、冻结输入和内容寻址缓存之间的内部一致性，不提供供应商签名真实性。
    """
    _require_exact_fields(report, _B1_ROOT_FIELDS, "B1 report")
    if (
        report.get("schema_version") != 2
        or report.get("evaluation_id") != B1_EVALUATION_ID
        or report.get("evaluation_qualification") != "official_frozen_dataset"
    ):
        raise EvaluationError("B1 report identity is invalid")
    source = _require_object(report.get("source"), "B1 source")
    summary = _require_object(report.get("summary"), "B1 summary")
    execution_observation = _require_object(
        report.get("execution_observation"), "B1 execution observation"
    )
    _require_exact_fields(source, _B1_SOURCE_FIELDS, "B1 source")
    _require_exact_fields(summary, _B1_SUMMARY_FIELDS, "B1 summary")
    _require_exact_fields(
        execution_observation,
        _B1_EXECUTION_OBSERVATION_FIELDS,
        "B1 execution observation",
    )
    predictions = report.get("predictions")
    if not isinstance(predictions, list):
        raise EvaluationError("B1 predictions must be an array")
    for row in predictions:
        row_object = _require_object(row, "B1 prediction row")
        _require_exact_fields(row_object, _B1_ROW_FIELDS, "B1 prediction row")
        _require_exact_fields(
            _require_object(row_object.get("gold"), "B1 prediction gold"),
            _B1_GOLD_FIELDS,
            "B1 prediction gold",
        )
        _require_exact_fields(
            _require_object(row_object.get("prediction"), "B1 prediction"),
            _B1_PREDICTION_FIELDS,
            "B1 prediction",
        )

    provider = summary.get("provider")
    model = summary.get("model")
    generation_config = summary.get("generation_config")
    raw_score_splits = summary.get("score_splits")
    if provider not in _B1_FORMAL_PROVIDER_NAMES:
        raise EvaluationError("B1 formal report provider is unsupported")
    if not isinstance(model, str) or not model.strip():
        raise EvaluationError("B1 formal report model is invalid")
    if not isinstance(generation_config, dict):
        raise EvaluationError("B1 formal report generation_config is invalid")
    if (
        not isinstance(raw_score_splits, list)
        or not raw_score_splits
        or any(not isinstance(item, str) or not item for item in raw_score_splits)
        or len(set(raw_score_splits)) != len(raw_score_splits)
    ):
        raise EvaluationError("B1 formal report score_splits are invalid")
    score_splits = tuple(raw_score_splits)
    _validate_b1_formal_contract(
        provider=provider,
        model=model,
        generation_config=generation_config,
        score_splits=score_splits,
        declared_budget_usd=summary.get("declared_budget_usd"),
    )
    _validate_b1_observations(summary, execution_observation, predictions)
    generated_at = report.get("generated_at")
    if not isinstance(generated_at, str):
        raise EvaluationError("B1 generated_at is invalid")
    try:
        timestamp = datetime.fromisoformat(generated_at)
    except ValueError as error:
        raise EvaluationError("B1 generated_at is invalid") from error
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise EvaluationError("B1 generated_at must include a timezone")

    cases_dir = _canonical_absolute_path(source.get("cases_dir"), "B1 cases_dir")
    split_path = _canonical_absolute_path(source.get("split_path"), "B1 split_path")
    cache_dir = _canonical_absolute_path(source.get("response_cache_dir"), "B1 response_cache_dir")
    dataset = load_evaluation_dataset(cases_dir, split_path)
    actual_split_sha256 = hashlib.sha256(dataset.split_raw).hexdigest()
    actual_corpus_sha256 = case_corpus_sha256(
        dataset.case_raw_by_id,
        set(dataset.split_case_ids.get("train", []))
        | {
            case_id
            for split_name in score_splits
            for case_id in dataset.split_case_ids.get(split_name, [])
        },
    )
    if not _is_b1_official_dataset(
        dataset,
        selected_splits=score_splits,
        split_sha256=actual_split_sha256,
        corpus_sha256=actual_corpus_sha256,
        provider=provider,
        model=model,
        generation_config=generation_config,
        declared_budget_usd=summary.get("declared_budget_usd"),
    ):
        raise EvaluationError("B1 formal report does not use the frozen official dataset")
    reproduced_predictions, manifest_sha256 = _replay_b1_predictions(
        dataset,
        selected_splits=score_splits,
        provider=provider,
        model=model,
        generation_config=generation_config,
        cache_dir=cache_dir,
    )
    code_revision = _require_code_revision(report.get("evaluation_contract"))
    if code_revision != _current_evaluation_code_revision():
        raise EvaluationError("B1 evaluation code revision is not current")
    run_summary = {
        "provider": provider,
        "model": model,
        "prompt_id": B1_PROMPT_ID,
        "generation_config": generation_config,
        "score_splits": list(score_splits),
        "declared_budget_usd": summary.get("declared_budget_usd"),
        "provider_response_count": sum(
            prediction.provider_request_id is not None
            for prediction in reproduced_predictions.values()
        ),
        "input_tokens": sum(
            prediction.input_tokens for prediction in reproduced_predictions.values()
        ),
        "output_tokens": sum(
            prediction.output_tokens for prediction in reproduced_predictions.values()
        ),
        "latency_ms": sum(prediction.latency_ms for prediction in reproduced_predictions.values()),
        "external_tool_calls": 0,
    }
    reproduced = _build_evaluation_report(
        dataset,
        reproduced_predictions,
        evaluation_id=B1_EVALUATION_ID,
        score_split_names=score_splits,
        run_summary=run_summary,
        limitations=_B1_LIMITATIONS,
        code_revision=code_revision,
    )
    reproduced["schema_version"] = 2
    reproduced["evaluation_qualification"] = "official_frozen_dataset"
    reproduced["execution_observation"] = execution_observation
    reproduced["generated_at"] = report.get("generated_at")
    reproduced["source"].update(
        {
            "official_split_id": _B1_OFFICIAL_SPLIT_ID,
            "official_split_sha256": _B1_OFFICIAL_SPLIT_SHA256,
            "official_case_corpus_sha256": _B1_OFFICIAL_CORPUS_SHA256_BY_SCORE_SPLIT[
                score_splits[0]
            ],
            "response_cache_dir": cache_dir.as_posix(),
            "response_manifest_sha256": manifest_sha256,
        }
    )
    for row in reproduced["predictions"]:
        row["prediction"].pop("provider_request_id", None)
        row["prediction"].pop("cache_hit", None)
        row["prediction"].pop("model_calls", None)
    if _canonical_evaluation_payload(report) != _canonical_evaluation_payload(reproduced):
        raise EvaluationError("B1 evaluation report is not reproducible")


def _validate_b1_formal_contract(
    *,
    provider: str,
    model: str,
    generation_config: dict[str, Any],
    score_splits: tuple[str, ...],
    declared_budget_usd: Any,
) -> None:
    if len(score_splits) != 1 or score_splits[0] not in {"validation", "heldout"}:
        raise EvaluationError("B1 formal report must score one frozen gate split")
    if (
        not isinstance(declared_budget_usd, (int, float))
        or isinstance(declared_budget_usd, bool)
        or not math.isfinite(declared_budget_usd)
        or declared_budget_usd <= 0
    ):
        raise EvaluationError("B1 formal report declared budget is invalid")
    if declared_budget_usd != _B1_OFFICIAL_BUDGET_USD_BY_SCORE_SPLIT[score_splits[0]]:
        raise EvaluationError("B1 formal report declared budget does not match the frozen profile")
    max_output_tokens = generation_config.get("max_output_tokens")
    if (
        not isinstance(max_output_tokens, int)
        or isinstance(max_output_tokens, bool)
        or max_output_tokens != _B1_OFFICIAL_MAX_OUTPUT_TOKENS
    ):
        raise EvaluationError("B1 formal report output-token limit is invalid")
    if (
        provider != "deepseek-responses"
        or model != "deepseek-v4-flash"
        or set(generation_config) != {"max_output_tokens", "reasoning_effort", "temperature"}
        or generation_config.get("reasoning_effort") != "none"
        or isinstance(generation_config.get("temperature"), bool)
        or generation_config.get("temperature") != 0
    ):
        raise EvaluationError("DeepSeek B1 generation contract is invalid")


def _is_b1_official_dataset(
    dataset: EvaluationDataset,
    *,
    selected_splits: tuple[str, ...],
    split_sha256: str,
    corpus_sha256: str,
    provider: str,
    model: str,
    generation_config: dict[str, Any],
    declared_budget_usd: Any,
) -> bool:
    if (
        dataset.split_id != _B1_OFFICIAL_SPLIT_ID
        or split_sha256 != _B1_OFFICIAL_SPLIT_SHA256
        or len(selected_splits) != 1
        or corpus_sha256 != _B1_OFFICIAL_CORPUS_SHA256_BY_SCORE_SPLIT.get(selected_splits[0])
    ):
        return False
    try:
        _validate_b1_formal_contract(
            provider=provider,
            model=model,
            generation_config=generation_config,
            score_splits=selected_splits,
            declared_budget_usd=declared_budget_usd,
        )
    except EvaluationError:
        return False
    return True


def _validate_b1_observations(
    summary: dict[str, Any],
    execution_observation: dict[str, Any],
    prediction_rows: list[Any],
) -> None:
    nonnegative_fields = (
        "case_count",
        "train_count",
        "validation_count",
        "heldout_count",
        "provider_response_count",
        "input_tokens",
        "output_tokens",
        "latency_ms",
        "external_tool_calls",
    )
    for field_name in nonnegative_fields:
        value = summary.get(field_name)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise EvaluationError("B1 summary counters are invalid")
    if summary.get("prompt_id") != B1_PROMPT_ID or summary.get("external_tool_calls") != 0:
        raise EvaluationError("B1 formal report execution contract is invalid")
    case_count = summary["case_count"]
    if len(prediction_rows) != case_count:
        raise EvaluationError("B1 prediction row count is invalid")
    seen_case_ids: set[str] = set()
    for row in prediction_rows:
        prediction = row["prediction"]
        case_id = row.get("case_id")
        if not isinstance(case_id, str) or not case_id or case_id in seen_case_ids:
            raise EvaluationError("B1 prediction case IDs are invalid")
        seen_case_ids.add(case_id)
        if prediction.get("case_id") != case_id:
            raise EvaluationError("B1 prediction case identity is invalid")
    if execution_observation.get("verification") != "self_reported_unverified":
        raise EvaluationError("B1 execution observation verification is invalid")
    for field_name in ("model_calls", "cache_hits"):
        value = execution_observation.get(field_name)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise EvaluationError("B1 execution observation counters are invalid")


def load_evaluation_dataset(cases_dir: Path, split_path: Path) -> EvaluationDataset:
    resolved_cases_dir = cases_dir.resolve()
    resolved_split_path = split_path.resolve()
    split_raw, split_payload = _load_object(resolved_split_path)
    split_id = split_payload.get("split_id")
    if not isinstance(split_id, str) or not split_id:
        raise EvaluationError("split manifest must contain split_id")
    raw_splits = split_payload.get("splits")
    if not isinstance(raw_splits, dict):
        raise EvaluationError("split manifest must contain splits")

    split_case_ids: dict[str, list[str]] = {}
    assigned_case_ids: set[str] = set()
    for split_name, entries in raw_splits.items():
        if not isinstance(split_name, str) or not split_name.strip():
            raise EvaluationError("split manifest contains an invalid split name")
        case_ids = _split_case_ids(entries)
        if assigned_case_ids.intersection(case_ids):
            raise EvaluationError("split manifest assigns a case_id to multiple splits")
        split_case_ids[split_name] = case_ids
        assigned_case_ids.update(case_ids)

    needed_case_ids = {case_id for case_ids in split_case_ids.values() for case_id in case_ids}
    cases, case_raw_by_id = _load_cases(resolved_cases_dir, needed_case_ids)
    return EvaluationDataset(
        split_id,
        split_case_ids,
        cases,
        split_raw,
        case_raw_by_id,
        resolved_cases_dir,
        resolved_split_path,
    )


def _b1_response_manifest_sha256(
    dataset: EvaluationDataset,
    *,
    selected_splits: tuple[str, ...],
    provider: str,
    model: str,
    generation_config: dict[str, Any],
    cache_dir: Path,
) -> str:
    train_cases = [dataset.cases[case_id] for case_id in dataset.split_case_ids.get("train", [])]
    retriever = TrainCaseRetriever(train_cases)
    evaluated_case_ids = {
        case_id for split_name in selected_splits for case_id in dataset.split_case_ids[split_name]
    }
    entries = []
    for case_id in sorted(evaluated_case_ids):
        case = dataset.cases[case_id]
        if detect_case_safety_risks(case):
            continue
        request = build_b1_request(
            case,
            retriever.retrieve(case),
            model=model,
            provider=provider,
            generation_config=generation_config,
        )
        cache_path = cache_dir / f"{request.cache_key}.json"
        try:
            raw = cache_path.read_bytes()
        except OSError as error:
            raise EvaluationError("B1 response cache is missing after evaluation") from error
        entries.append((case_id, request.cache_key, raw))
    return _response_manifest_sha256(entries)


def _replay_b1_predictions(
    dataset: EvaluationDataset,
    *,
    selected_splits: tuple[str, ...],
    provider: str,
    model: str,
    generation_config: dict[str, Any],
    cache_dir: Path,
) -> tuple[dict[str, B1Prediction], str]:
    train_cases = [dataset.cases[case_id] for case_id in dataset.split_case_ids.get("train", [])]
    retriever = TrainCaseRetriever(train_cases)
    evaluated_case_ids = {
        case_id for split_name in selected_splits for case_id in dataset.split_case_ids[split_name]
    }
    predictions: dict[str, B1Prediction] = {}
    manifest_entries: list[tuple[str, str, bytes]] = []
    for case_id in sorted(evaluated_case_ids):
        case = dataset.cases[case_id]
        safety_risks = detect_case_safety_risks(case)
        if safety_risks:
            raise EvaluationError(
                "B1 formal reports containing local safety-refusal rows are unsupported"
            )
        evidence = retriever.retrieve(case)
        request = build_b1_request(
            case,
            evidence,
            model=model,
            provider=provider,
            generation_config=generation_config,
        )
        cache_path = cache_dir / f"{request.cache_key}.json"
        try:
            raw = cache_path.read_bytes()
            payload = strict_json_loads(raw)
        except (OSError, StrictJsonError) as error:
            raise EvaluationError("B1 response cache is missing or invalid") from error
        response = _validate_b1_cached_response(payload, provider=provider, model=model)
        try:
            parsed = parse_b1_output(response.output_text, evidence)
        except B1Error as error:
            raise EvaluationError("B1 cached response output is invalid") from error
        predictions[case_id] = B1Prediction(
            case_id=case_id,
            baseline_id="b1-rag-only-v1",
            retrieved_evidence=evidence,
            secret_risk_detected=False,
            safety_risks=[],
            cache_hit=True,
            model_calls=0,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            latency_ms=response.latency_ms,
            provider_request_id=response.provider_request_id,
            **parsed,
        )
        manifest_entries.append((case_id, request.cache_key, raw))
    return predictions, _response_manifest_sha256(manifest_entries)


def _validate_b1_cached_response(
    value: Any,
    *,
    provider: str,
    model: str,
) -> B1ModelResponse:
    payload = _require_object(value, "B1 cached response")
    if not set(payload) >= _B1_RESPONSE_REQUIRED_FIELDS or not set(payload) <= (
        _B1_RESPONSE_REQUIRED_FIELDS | _B1_RESPONSE_OPTIONAL_FIELDS
    ):
        raise EvaluationError("B1 cached response fields are invalid")
    try:
        response = B1ModelResponse(**payload)
    except TypeError as error:
        raise EvaluationError("B1 cached response fields are invalid") from error
    for field_name in ("input_tokens", "output_tokens", "latency_ms"):
        field_value = getattr(response, field_name)
        if not isinstance(field_value, int) or isinstance(field_value, bool) or field_value < 0:
            raise EvaluationError("B1 cached response usage is invalid")
    if not isinstance(response.output_text, str):
        raise EvaluationError("B1 cached response output is invalid")
    if not isinstance(response.provider_request_id, str) or not response.provider_request_id:
        raise EvaluationError("B1 formal response requires a provider request ID")
    if response.provider_name not in _B1_FORMAL_PROVIDER_NAMES[provider]:
        raise EvaluationError("B1 cached response provider identity does not match")
    if response.provider_model_name != model:
        raise EvaluationError("B1 cached response model identity does not match")
    if response.cost_microusd is not None and (
        not isinstance(response.cost_microusd, int)
        or isinstance(response.cost_microusd, bool)
        or response.cost_microusd < 0
    ):
        raise EvaluationError("B1 cached response cost is invalid")
    for field_name in ("provider_name", "provider_model_name", "provider_fingerprint"):
        field_value = getattr(response, field_name)
        if field_value is not None and (
            not isinstance(field_value, str) or not field_value.strip()
        ):
            raise EvaluationError("B1 cached response identity is invalid")
    return response


def _response_manifest_sha256(entries: list[tuple[str, str, bytes]]) -> str:
    digest = hashlib.sha256(_B1_RESPONSE_MANIFEST_DOMAIN)
    for case_id, request_hash, raw in sorted(entries):
        digest.update(case_id.encode("utf-8"))
        digest.update(b"\0")
        digest.update(request_hash.encode("ascii"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(raw).hexdigest().encode("ascii"))
        digest.update(b"\0")
    return f"{_B1_RESPONSE_MANIFEST_PREFIX}{digest.hexdigest()}"


def _canonical_evaluation_payload(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise EvaluationError("B1 evaluation report contains non-canonical JSON values") from error


def _require_exact_fields(payload: dict[str, Any], fields: set[str], name: str) -> None:
    if set(payload) != fields:
        raise EvaluationError(f"{name} fields are invalid")


def _require_object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EvaluationError(f"{name} must be an object")
    return value


def _canonical_absolute_path(value: Any, name: str) -> Path:
    if not isinstance(value, str) or not value:
        raise EvaluationError(f"{name} is invalid")
    path = Path(value)
    if not path.is_absolute() or path.resolve().as_posix() != value:
        raise EvaluationError(f"{name} must be canonical and absolute")
    return path


def _require_code_revision(value: Any) -> str:
    contract = _require_object(value, "B1 evaluation contract")
    _require_exact_fields(contract, {"code_revision"}, "B1 evaluation contract")
    revision = contract.get("code_revision")
    prefix = "nbtriage-source-sha256:"
    if (
        not isinstance(revision, str)
        or not revision.startswith(prefix)
        or re.fullmatch(r"[0-9a-f]{64}", revision.removeprefix(prefix)) is None
    ):
        raise EvaluationError("B1 evaluation code revision is invalid")
    return revision


def _build_evaluation_report(
    dataset: EvaluationDataset,
    predictions: Mapping[str, EvaluationPrediction],
    *,
    evaluation_id: str,
    run_summary: dict[str, Any],
    limitations: list[str],
    score_split_names: tuple[str, ...] | None = None,
    code_revision: str,
) -> dict[str, Any]:
    selected_splits = (
        tuple(dataset.split_case_ids) if score_split_names is None else score_split_names
    )
    evaluated_case_ids = {
        case_id for split_name in selected_splits for case_id in dataset.split_case_ids[split_name]
    }
    missing_predictions = evaluated_case_ids - set(predictions)
    if missing_predictions:
        raise EvaluationError(f"predictions missing case IDs: {sorted(missing_predictions)}")

    corpus_case_ids = set(evaluated_case_ids)
    corpus_scope = "scored_splits"
    if evaluation_id in {B1_EVALUATION_ID, B1_CUSTOM_EVALUATION_ID}:
        corpus_case_ids.update(dataset.split_case_ids.get("train", []))
        corpus_scope = "train_and_scored_splits"

    split_reports = {}
    prediction_rows = []
    for split_name in selected_splits:
        case_ids = dataset.split_case_ids[split_name]
        split_reports[split_name] = _score_split(
            case_ids,
            dataset.cases,
            predictions,
            train_case_ids=set(dataset.split_case_ids.get("train", [])),
        )
        for case_id in case_ids:
            case = dataset.cases[case_id]
            prediction_rows.append(
                {
                    "split": split_name,
                    "case_id": case_id,
                    "support_level": case["curation"]["support_level"],
                    "gold": _gold_labels(case),
                    "prediction": predictions[case_id].to_dict(),
                }
            )

    return {
        "schema_version": 1,
        "evaluation_id": evaluation_id,
        "evaluation_contract": {"code_revision": code_revision},
        "split_id": dataset.split_id,
        "generated_at": datetime.now(UTC).isoformat(),
        "source": {
            "cases_dir": dataset.cases_dir.as_posix(),
            "split_path": dataset.split_path.as_posix(),
            "split_sha256": hashlib.sha256(dataset.split_raw).hexdigest(),
            "case_corpus_sha256": case_corpus_sha256(
                dataset.case_raw_by_id,
                corpus_case_ids,
            ),
            "case_corpus_scope": corpus_scope,
            "case_count": len(corpus_case_ids),
        },
        "summary": {
            "case_count": len(evaluated_case_ids),
            "train_count": (
                len(dataset.split_case_ids.get("train", [])) if "train" in selected_splits else 0
            ),
            "validation_count": (
                len(dataset.split_case_ids.get("validation", []))
                if "validation" in selected_splits
                else 0
            ),
            "heldout_count": (
                len(dataset.split_case_ids.get("heldout", []))
                if "heldout" in selected_splits
                else 0
            ),
            **run_summary,
        },
        "metrics_by_split": split_reports,
        "predictions": prediction_rows,
        "limitations": limitations,
    }


def write_evaluation_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


@contextmanager
def reserve_new_evaluation_report(path: Path) -> Iterator[EvaluationReportReservation]:
    """在昂贵评测开始前独占预留一个终态报告目标。寻址文件不是报告本身。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.path.lexists(path):
        raise FileExistsError("evaluation report target already exists")

    token = uuid4().hex
    marker_path = path.with_name(f".{path.name}.reservation")
    link_probe_path = path.with_name(f".{path.name}.{token}.link-probe")
    marker_content = f"nbtriage-evaluation-report-reservation-v1:{token}\n".encode()
    descriptor = os.open(
        marker_path,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0),
        0o600,
    )
    try:
        try:
            os.write(descriptor, marker_content)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.link(marker_path, link_probe_path)
        link_probe_path.unlink()
        if os.path.lexists(path):
            raise FileExistsError("evaluation report target already exists")

        yield EvaluationReportReservation(
            report_path=path,
            marker_path=marker_path,
            token=token,
        )
    finally:
        with suppress(OSError):
            link_probe_path.unlink(missing_ok=True)
        with suppress(OSError):
            if marker_path.read_bytes() == marker_content:
                marker_path.unlink()


def publish_reserved_evaluation_report(
    reservation: EvaluationReportReservation,
    report: dict[str, Any],
) -> None:
    """发布已预留的完整报告；冲突时保留一份不会覆盖的恢复工件。"""
    marker_content = f"nbtriage-evaluation-report-reservation-v1:{reservation.token}\n".encode()
    temporary = reservation.report_path.with_name(
        f".{reservation.report_path.name}.{reservation.token}.pending"
    )
    recovery = reservation.report_path.with_name(
        f"{reservation.report_path.name}.{reservation.token}.recovery.json"
    )
    report_bytes = (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    descriptor = os.open(
        temporary,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(report_bytes)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
        raise
    published = False
    try:
        if reservation.marker_path.read_bytes() != marker_content:
            raise FileExistsError("evaluation report reservation ownership changed")
        os.link(temporary, reservation.report_path)
        published = True
    except OSError as error:
        try:
            os.link(temporary, recovery)
            temporary.unlink()
        except OSError:
            recovery = temporary
        raise EvaluationReportPublishError(recovery) from error
    finally:
        if published:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)


def write_new_evaluation_report(path: Path, report: dict[str, Any]) -> None:
    """原子发布一份不得覆盖既有证据的新评测报告。

    Args:
        path: 尚不存在的最终报告路径。
        report: 可 JSON 序列化的评测结果。

    Raises:
        OSError: 临时文件写入、硬链接发布或清理失败；目标已存在时也会失败。

    Note:
        先完整写入同目录临时文件，再用不覆盖目标的硬链接发布。这样并发运行或运行期间新出现的目标
        不会被静默替换。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.link(temporary, path)
    finally:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)


def _score_split(
    case_ids: list[str],
    cases: dict[str, dict[str, Any]],
    predictions: Mapping[str, EvaluationPrediction],
    *,
    train_case_ids: set[str],
) -> dict[str, Any]:
    route_correct = 0
    phase_correct = 0
    symptom_counts = MultiLabelCounts()
    owner_counts = MultiLabelCounts()
    gap_counts = MultiLabelCounts()
    version_counts = MultiLabelCounts()
    route_confusion: Counter[str] = Counter()
    same_repository_eligible = 0
    same_repository_hits = 0
    support_rows: dict[str, list[tuple[dict[str, Any], EvaluationPrediction]]] = {
        "s1_verify": [],
        "s2_diagnose": [],
        "s3_abstain": [],
    }

    for case_id in case_ids:
        case = cases[case_id]
        gold = _gold_labels(case)
        prediction = predictions[case_id]
        route_correct += prediction.route == gold["route"]
        phase_correct += prediction.fault_phase == gold["fault_phase"]
        symptom_counts.add(set(gold["symptoms"]), set(prediction.symptoms))
        owner_counts.add(set(gold["candidate_owners"]), set(prediction.candidate_owners))
        gap_counts.add(set(gold["missing_evidence"]), set(prediction.missing_evidence))
        version_counts.add(set(gold["source_version_values"]), set(prediction.version_values))
        route_confusion[f"{gold['route']} -> {prediction.route}"] += 1
        support_rows.setdefault(case["curation"]["support_level"], []).append((gold, prediction))

        repository = _repository(case)
        eligible_ids = train_case_ids - {case_id}
        if any(_repository(cases[item]) == repository for item in eligible_ids):
            same_repository_eligible += 1
            if any(
                _repository(cases[retrieved_case_id]) == repository
                for retrieved_case_id in _retrieved_case_ids(prediction)
            ):
                same_repository_hits += 1

    count = len(case_ids)
    return {
        "case_count": count,
        "route_accuracy": _ratio(route_correct, count),
        "fault_phase_accuracy": _ratio(phase_correct, count),
        "symptom_micro": symptom_counts.report(),
        "candidate_owner_micro": owner_counts.report(),
        "missing_evidence_micro": gap_counts.report(),
        "version_value_micro": version_counts.report(),
        "same_repository_hit_at_5": {
            "eligible_cases": same_repository_eligible,
            "hits": same_repository_hits,
            "rate": _ratio(same_repository_hits, same_repository_eligible),
        },
        "duplicate_issue_recall_at_5": {
            "status": "not_applicable",
            "duplicate_group_count": 0,
        },
        "route_confusion": dict(sorted(route_confusion.items())),
        "by_support_level": {
            level: _score_support_rows(rows) for level, rows in support_rows.items()
        },
    }


def _score_support_rows(
    rows: list[tuple[dict[str, Any], EvaluationPrediction]],
) -> dict[str, Any]:
    if not rows:
        return {"case_count": 0, "status": "insufficient_coverage"}
    return {
        "case_count": len(rows),
        "route_accuracy": _ratio(
            sum(prediction.route == gold["route"] for gold, prediction in rows),
            len(rows),
        ),
        "fault_phase_accuracy": _ratio(
            sum(prediction.fault_phase == gold["fault_phase"] for gold, prediction in rows),
            len(rows),
        ),
    }


def _gold_labels(case: dict[str, Any]) -> dict[str, Any]:
    curation = case["curation"]
    mode = curation["execution_mode"]
    route = ROUTE_BY_MODE.get(mode)
    if route is None:
        raise EvaluationError(f"unsupported execution mode for {case['case_id']}: {mode}")
    source_text = _source_text(case)
    source_versions = set(extract_version_values(source_text))
    curated_source_versions = set()
    for value in curation.get("versions", {}).values():
        curated_source_versions.update(
            version for version in extract_version_values(str(value)) if version in source_versions
        )
    return {
        "route": route,
        "fault_phase": curation["fault_phase"],
        "symptoms": sorted(curation["symptoms"]),
        "candidate_owners": sorted(curation["candidate_owners"]),
        "missing_evidence": sorted(_gold_missing_evidence(curation)),
        "source_version_values": sorted(curated_source_versions),
    }


def _gold_missing_evidence(curation: dict[str, Any]) -> set[str]:
    gaps = set()
    versions = curation.get("versions", {})
    for key, value in versions.items():
        lowered = str(value).lower()
        if "not supplied" in lowered:
            gaps.add("python_version" if "python" in key.lower() else "component_versions")
    environment = curation.get("environment", {})
    if "not supplied" in str(environment.get("os", "")).lower():
        gaps.add("operating_system")
    evidence_text = "\n".join(
        [
            *curation.get("required_evidence_gaps", []),
            *curation.get("unknowns", []),
        ]
    ).lower()
    for slot, keywords in GAP_KEYWORDS.items():
        if any(keyword in evidence_text for keyword in keywords):
            gaps.add(slot)
    return gaps


def _load_cases(
    cases_dir: Path,
    needed_case_ids: set[str],
) -> tuple[dict[str, dict[str, Any]], dict[str, bytes]]:
    cases = {}
    raw_by_id = {}
    resolved_cases_dir = cases_dir.resolve()
    for case_id in needed_case_ids:
        path = (resolved_cases_dir / f"{case_id}.json").resolve()
        if path.parent != resolved_cases_dir:
            raise EvaluationError("split manifest contains an invalid case_id")
        if not path.is_file():
            raise EvaluationError(f"missing generated SupportCase: {path}")
        raw, payload = _load_object(path)
        if payload.get("case_id") != case_id:
            raise EvaluationError(f"case_id mismatch in {path}")
        if not isinstance(payload.get("source"), dict) or not isinstance(
            payload.get("curation"), dict
        ):
            raise EvaluationError(f"invalid SupportCase structure: {path}")
        cases[case_id] = payload
        raw_by_id[case_id] = raw
    return cases, raw_by_id


def _split_case_ids(entries: Any) -> list[str]:
    if not isinstance(entries, list):
        raise EvaluationError("split manifest contains entries that are not a list")
    case_ids = []
    seen_case_ids: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise EvaluationError("split manifest contains an invalid split entry")
        case_id = entry.get("case_id")
        if not isinstance(case_id, str) or _CASE_ID_PATTERN.fullmatch(case_id) is None:
            raise EvaluationError("split manifest contains an invalid case_id")
        if case_id in seen_case_ids:
            raise EvaluationError("split manifest contains duplicate case_id within a split")
        seen_case_ids.add(case_id)
        case_ids.append(case_id)
    return case_ids


def _load_object(path: Path) -> tuple[bytes, dict[str, Any]]:
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EvaluationError(f"failed to load {path}: {error}") from error
    if not isinstance(payload, dict):
        raise EvaluationError(f"top-level JSON value must be an object: {path}")
    return raw, payload


def _current_evaluation_code_revision() -> str:
    try:
        return evaluation_code_revision(Path(__file__).resolve().parents[2])
    except EvaluationProvenanceError as error:
        raise EvaluationError(str(error)) from error


def _ensure_evaluation_code_unchanged(expected_revision: str) -> None:
    if _current_evaluation_code_revision() != expected_revision:
        raise EvaluationError("evaluation source changed during the B0/B1 run")


def _source_text(case: dict[str, Any]) -> str:
    source = case["source"]
    return "\n".join(
        (
            str(source.get("title", "")),
            str(source.get("body", "")),
            " ".join(str(item) for item in source.get("labels", [])),
        )
    )


def _repository(case: dict[str, Any]) -> str:
    source = case["source"]
    return f"{source.get('owner', '')}/{source.get('repository', '')}"


def _retrieved_case_ids(prediction: EvaluationPrediction) -> list[str]:
    retrieved = getattr(prediction, "retrieved_cases", None)
    if retrieved is None:
        retrieved = getattr(prediction, "retrieved_evidence", [])
    return [item.case_id for item in retrieved]


def _ratio(numerator: float, denominator: float) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0
