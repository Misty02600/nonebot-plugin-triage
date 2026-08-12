"""仓库维护者使用的离线模型评测编排。"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast
from uuid import uuid4

from nbtriage.baselines import B0SearchIndex, extract_version_values, predict_b0
from nbtriage.rag import (
    B1_PROMPT_ID,
    B1ModelClient,
    B1ResponseCache,
    B1Runner,
    TrainCaseRetriever,
)
from tools.nbtriage_maintainer.evaluation_provenance import (
    EvaluationProvenanceError,
    case_corpus_sha256,
    evaluation_code_revision,
)

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


class EvaluationError(ValueError):
    pass


class EvaluationPrediction(Protocol):
    case_id: str
    version_values: list[str]
    missing_evidence: list[str]
    symptoms: list[str]
    fault_phase: str
    candidate_owners: list[str]
    route: str

    def to_dict(self) -> dict[str, Any]: ...


@dataclass(frozen=True)
class EvaluationDataset:
    split_id: str
    split_case_ids: dict[str, list[str]]
    cases: dict[str, dict[str, Any]]
    split_raw: bytes
    case_raw_by_id: dict[str, bytes]


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
    runner = B1Runner(
        client,
        model,
        TrainCaseRetriever(train_cases),
        B1ResponseCache(cache_dir),
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
    report = _build_evaluation_report(
        dataset,
        predictions,
        evaluation_id="b1-rag-only-v1",
        score_split_names=selected_splits,
        run_summary={
            "provider": provider,
            "model": model,
            "prompt_id": B1_PROMPT_ID,
            "generation_config": generation_config or {},
            "score_splits": list(selected_splits),
            "declared_budget_usd": declared_budget_usd,
            "model_calls": model_calls,
            "cache_hits": cache_hits,
            "provider_response_count": provider_response_count,
            "input_tokens": sum(prediction.input_tokens for prediction in predictions.values()),
            "output_tokens": sum(prediction.output_tokens for prediction in predictions.values()),
            "latency_ms": sum(prediction.latency_ms for prediction in predictions.values()),
            "external_tool_calls": 0,
        },
        limitations=[
            "B1 uses source-only target input and train-only retrieved cases; "
            "curation and Gold are passed only to the shared scorer.",
            "Model output can reflect pretraining exposure to historical public Issues; "
            "a forward hidden set or counterfactual fixtures are still required.",
            "The v1 corpus has no duplicate root-cause groups or qualified S3 cases, so "
            "duplicate Recall@5 is not applicable and unsafe-refusal coverage is insufficient.",
            "Version metrics compare normalized values, not package-to-version association.",
        ],
        code_revision=code_revision,
    )
    _ensure_evaluation_code_unchanged(code_revision)
    return report


def load_evaluation_dataset(cases_dir: Path, split_path: Path) -> EvaluationDataset:
    split_raw, split_payload = _load_object(split_path)
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
    cases, case_raw_by_id = _load_cases(cases_dir, needed_case_ids)
    return EvaluationDataset(split_id, split_case_ids, cases, split_raw, case_raw_by_id)


def _build_evaluation_report(
    dataset: EvaluationDataset,
    predictions: dict[str, EvaluationPrediction],
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
    if evaluation_id == "b1-rag-only-v1":
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
    predictions: dict[str, EvaluationPrediction],
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
