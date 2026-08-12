"""仓库维护者使用的 MLflow 评测发布器。"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import math
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tools.nbtriage_maintainer.answer_quality_evaluation import (
    ANSWER_QUALITY_EVALUATION_ID,
    evaluate_answer_quality,
)
from tools.nbtriage_maintainer.evidence_policy import B3_EVIDENCE_POLICY_ID
from tools.nbtriage_maintainer.evidence_policy_evaluation import evaluate_b3_evidence_policy
from tools.nbtriage_maintainer.evidence_receipt_evaluation import (
    B3_EVIDENCE_RECEIPT_EVALUATION_ID,
    evaluate_b3_evidence_receipts,
)
from tools.nbtriage_maintainer.safety_evaluation import S3_EVALUATION_ID, evaluate_s3

DEFAULT_MLFLOW_TRACKING_URI = "http://127.0.0.1:5000"
DEFAULT_MLFLOW_EXPERIMENT = "nbtriage/evaluations"

_B4_REAL_EVALUATION_ID = "b4-bounded-agent-real-v1"
_B4_REAL_PARTIAL_KIND = "b4-real-partial"
_B4_REAL_ABORT_KIND = "b4-real-run-abort-observation"
_B0_B1_EVALUATION_IDS = frozenset({"b0-checklist-v1", "b1-rag-only-v1"})
_TERMINAL_PARTIAL_STATUSES = frozenset({"aborted", "completed"})
_METRIC_ROOTS = (
    "summary",
    "metrics",
    "metrics_by_split",
    "promotion_gate",
    "calibration_gate",
    "quality_claim_gate",
    "budget",
    "progress",
    "ledger",
    "accounting",
)
_PARAMETER_ROOTS = ("evaluation_contract", "source_evaluation")
_SAFE_KEY_PATTERN = re.compile(r"[^A-Za-z0-9_.-]+")
_BUNDLE_TAG = "nbtriage.bundle_sha256"


class MLflowTrackingError(ValueError):
    pass


@dataclass(frozen=True)
class MLflowPublication:
    run_id: str
    experiment_id: str
    artifact_sha256: str
    bundle_sha256: str
    created: bool


@dataclass(frozen=True)
class _LoadedArtifact:
    path: Path
    raw: bytes
    payload: dict[str, Any]
    sha256: str


def publish_evaluation_to_mlflow(
    report_path: Path,
    *,
    tracking_uri: str = DEFAULT_MLFLOW_TRACKING_URI,
    experiment_name: str = DEFAULT_MLFLOW_EXPERIMENT,
    run_name: str | None = None,
    mlflow_module: Any | None = None,
) -> MLflowPublication:
    """把既有评测工件发布到 MLflow，不修改或重新执行评测。

    Args:
        report_path: 已经完整落盘的 JSON 评测报告或终态 partial audit。
        tracking_uri: 显式 MLflow Tracking URI；不会读取项目配置或改写报告。
        experiment_name: MLflow experiment 名称。
        run_name: 可选的 MLflow run 名称；缺省时由评测 ID 和工件摘要生成。
        mlflow_module: 测试时注入的 MLflow 兼容对象；正常调用不应传入。

    Returns:
        发布结果；相同 bundle 已存在时 `created` 为 `False`。

    Raises:
        MLflowTrackingError: 报告无效、partial audit 尚未终止、缺少可选依赖或发布失败。

    Note:
        MLflow 是查询索引和 UI 副本。报告原文、评测结论与 promotion gate 仍由本地 JSON 所有。
    """
    artifact = _load_artifact(report_path)
    audit = _load_related_audit(artifact)
    bundle_sha256 = _bundle_sha256(artifact, audit)
    mlflow = mlflow_module or _load_mlflow()

    if not tracking_uri.strip():
        raise MLflowTrackingError("MLflow tracking URI must not be empty")
    if not experiment_name.strip():
        raise MLflowTrackingError("MLflow experiment name must not be empty")

    try:
        mlflow.set_tracking_uri(tracking_uri)
        experiment = mlflow.set_experiment(experiment_name)
        existing = mlflow.search_runs(
            experiment_ids=[experiment.experiment_id],
            filter_string=(
                f"attributes.status = 'FINISHED' AND tags.`{_BUNDLE_TAG}` = '{bundle_sha256}'"
            ),
            output_format="list",
            max_results=1,
        )
    except Exception as error:
        raise MLflowTrackingError(f"MLflow lookup failed ({type(error).__name__})") from error

    if existing:
        return MLflowPublication(
            run_id=str(existing[0].info.run_id),
            experiment_id=str(experiment.experiment_id),
            artifact_sha256=artifact.sha256,
            bundle_sha256=bundle_sha256,
            created=False,
        )

    tags = _build_tags(artifact, audit, bundle_sha256)
    parameters = _build_parameters(artifact.payload)
    metrics = _build_metrics(artifact.payload)
    effective_run_name = run_name or (f"{artifact.payload['evaluation_id']}:{bundle_sha256[:12]}")

    url_suppression_key = "MLFLOW_SUPPRESS_PRINTING_URL_TO_STDOUT"
    previous_url_suppression = os.environ.get(url_suppression_key)
    os.environ[url_suppression_key] = "true"
    try:
        with mlflow.start_run(
            experiment_id=experiment.experiment_id,
            run_name=effective_run_name,
            tags=tags,
        ) as active_run:
            if parameters:
                mlflow.log_params(parameters)
            if metrics:
                mlflow.log_metrics(metrics)
            with tempfile.TemporaryDirectory(prefix="nbtriage-mlflow-") as directory:
                staging_root = Path(directory)
                _log_staged_artifact(mlflow, staging_root, artifact, "evaluation")
                if audit is not None:
                    _log_staged_artifact(mlflow, staging_root, audit, "audit")
            run_id = str(active_run.info.run_id)
    except Exception as error:
        raise MLflowTrackingError(f"MLflow publish failed ({type(error).__name__})") from error
    finally:
        if previous_url_suppression is None:
            os.environ.pop(url_suppression_key, None)
        else:
            os.environ[url_suppression_key] = previous_url_suppression

    return MLflowPublication(
        run_id=run_id,
        experiment_id=str(experiment.experiment_id),
        artifact_sha256=artifact.sha256,
        bundle_sha256=bundle_sha256,
        created=True,
    )


def _load_mlflow() -> Any:
    try:
        return importlib.import_module("mlflow")
    except ModuleNotFoundError as error:
        raise MLflowTrackingError(
            "MLflow support is not installed; run `uv sync --group maintainer`"
        ) from error


def _load_artifact(path: Path) -> _LoadedArtifact:
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise MLflowTrackingError(f"evaluation artifact could not be read: {path}") from error
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MLflowTrackingError("evaluation artifact must be valid UTF-8 JSON") from error
    if not isinstance(payload, dict):
        raise MLflowTrackingError("evaluation artifact must contain one JSON object")

    evaluation_id = payload.get("evaluation_id")
    if not isinstance(evaluation_id, str) or not evaluation_id:
        raise MLflowTrackingError("evaluation artifact must contain evaluation_id")
    schema_version = payload.get("schema_version")
    if not isinstance(schema_version, int) or isinstance(schema_version, bool):
        raise MLflowTrackingError("evaluation artifact must contain an integer schema_version")

    if payload.get("artifact_kind") == _B4_REAL_PARTIAL_KIND:
        status = payload.get("status")
        if status not in _TERMINAL_PARTIAL_STATUSES:
            raise MLflowTrackingError(
                "b4-real-partial must be completed or aborted before publication"
            )

    if evaluation_id in _B0_B1_EVALUATION_IDS:
        _validate_b0_b1_provenance(payload)
    if evaluation_id == ANSWER_QUALITY_EVALUATION_ID:
        _validate_answer_quality_reproducibility(payload)
    if evaluation_id == S3_EVALUATION_ID:
        _validate_s3_reproducibility(payload)
    if evaluation_id == B3_EVIDENCE_POLICY_ID:
        _validate_b3_evidence_policy_reproducibility(payload)
    if evaluation_id == B3_EVIDENCE_RECEIPT_EVALUATION_ID:
        _validate_b3_evidence_receipt_reproducibility(payload)

    return _LoadedArtifact(
        path=path,
        raw=raw,
        payload=payload,
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def _validate_answer_quality_reproducibility(payload: dict[str, Any]) -> None:
    source = payload.get("source")
    source_fields = {
        "rubric_path",
        "fixtures_path",
        "annotations_path",
        "source_report_path",
    }
    if not isinstance(source, dict) or not source_fields <= set(source):
        raise MLflowTrackingError("answer-quality report is not reproducible")

    rubric_path = source.get("rubric_path")
    fixtures_path = source.get("fixtures_path")
    annotations_path = source.get("annotations_path")
    source_report = source.get("source_report_path")
    if (
        not isinstance(payload.get("generated_at"), str)
        or not payload["generated_at"]
        or not isinstance(rubric_path, str)
        or not rubric_path
        or not isinstance(fixtures_path, str)
        or not fixtures_path
        or not isinstance(annotations_path, str)
        or not annotations_path
        or (source_report is not None and (not isinstance(source_report, str) or not source_report))
    ):
        raise MLflowTrackingError("answer-quality report is not reproducible")

    try:
        reproduced = evaluate_answer_quality(
            Path(rubric_path),
            Path(fixtures_path),
            Path(annotations_path),
            source_report_path=Path(source_report) if source_report is not None else None,
        )
    except Exception as error:
        raise MLflowTrackingError("answer-quality report is not reproducible") from error

    expected = dict(payload)
    actual = dict(reproduced)
    expected.pop("generated_at", None)
    actual.pop("generated_at", None)
    if expected != actual:
        raise MLflowTrackingError("answer-quality report is not reproducible")


def _validate_s3_reproducibility(payload: dict[str, Any]) -> None:
    fixture = payload.get("fixture")
    fixture_path = fixture.get("path") if isinstance(fixture, dict) else None
    if not isinstance(fixture_path, str) or not fixture_path:
        raise MLflowTrackingError("S3 report is not reproducible")

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise MLflowTrackingError(
            "S3 report cannot be reproduced from a synchronous publisher while an event loop is running"
        )

    try:
        reproduced = asyncio.run(evaluate_s3(Path(fixture_path)))
    except Exception as error:
        raise MLflowTrackingError("S3 report is not reproducible") from error
    _require_reproduced_report(payload, reproduced, report_name="S3")


def _validate_b3_evidence_policy_reproducibility(payload: dict[str, Any]) -> None:
    source = payload.get("source")
    prediction_report = source.get("prediction_report") if isinstance(source, dict) else None
    if not isinstance(prediction_report, str) or not prediction_report:
        raise MLflowTrackingError("B3 evidence-policy report is not reproducible")

    try:
        reproduced = evaluate_b3_evidence_policy(Path(prediction_report))
    except Exception as error:
        raise MLflowTrackingError("B3 evidence-policy report is not reproducible") from error
    _require_reproduced_report(payload, reproduced, report_name="B3 evidence-policy")


def _validate_b3_evidence_receipt_reproducibility(payload: dict[str, Any]) -> None:
    source = payload.get("source")
    fixtures_path = source.get("fixtures_path") if isinstance(source, dict) else None
    if not isinstance(fixtures_path, str) or not fixtures_path:
        raise MLflowTrackingError("B3 evidence-receipt report is not reproducible")

    try:
        reproduced = evaluate_b3_evidence_receipts(Path(fixtures_path))
    except Exception as error:
        raise MLflowTrackingError("B3 evidence-receipt report is not reproducible") from error
    _require_reproduced_report(payload, reproduced, report_name="B3 evidence-receipt")


def _require_reproduced_report(
    payload: dict[str, Any],
    reproduced: dict[str, Any],
    *,
    report_name: str,
) -> None:
    expected = dict(payload)
    actual = dict(reproduced)
    expected.pop("generated_at", None)
    actual.pop("generated_at", None)
    if expected != actual:
        raise MLflowTrackingError(f"{report_name} report is not reproducible")


def _validate_b0_b1_provenance(payload: dict[str, Any]) -> None:
    source = payload.get("source")
    if not isinstance(source, dict):
        raise MLflowTrackingError("B0/B1 evaluation artifact must contain source provenance")
    required_source_fields = {
        "split_sha256",
        "case_corpus_sha256",
        "case_corpus_scope",
        "case_count",
    }
    if set(source) != required_source_fields:
        raise MLflowTrackingError("B0/B1 evaluation source provenance is invalid")
    for field in ("split_sha256", "case_corpus_sha256"):
        value = source.get(field)
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise MLflowTrackingError("B0/B1 evaluation source hashes must be lowercase SHA-256")
    expected_scope = (
        "scored_splits"
        if payload.get("evaluation_id") == "b0-checklist-v1"
        else "train_and_scored_splits"
    )
    if source.get("case_corpus_scope") != expected_scope:
        raise MLflowTrackingError("B0/B1 evaluation case corpus scope is invalid")
    case_count = source.get("case_count")
    if not isinstance(case_count, int) or isinstance(case_count, bool) or case_count < 1:
        raise MLflowTrackingError("B0/B1 evaluation case count is invalid")

    contract = payload.get("evaluation_contract")
    if not isinstance(contract, dict) or set(contract) != {"code_revision"}:
        raise MLflowTrackingError("B0/B1 evaluation contract is invalid")
    revision = contract.get("code_revision")
    prefix = "nbtriage-source-sha256:"
    if (
        not isinstance(revision, str)
        or not revision.startswith(prefix)
        or re.fullmatch(r"[0-9a-f]{64}", revision.removeprefix(prefix)) is None
    ):
        raise MLflowTrackingError("B0/B1 evaluation code revision is invalid")


def _load_related_audit(artifact: _LoadedArtifact) -> _LoadedArtifact | None:
    payload = artifact.payload
    if payload.get("evaluation_id") != _B4_REAL_EVALUATION_ID:
        return None
    if payload.get("artifact_kind") in {_B4_REAL_PARTIAL_KIND, _B4_REAL_ABORT_KIND}:
        return None

    audit_path = artifact.path.with_suffix(".partial.json")
    if not audit_path.is_file():
        raise MLflowTrackingError("completed B4 real report requires its sibling partial audit")
    audit = _load_artifact(audit_path)
    if audit.payload.get("artifact_kind") != _B4_REAL_PARTIAL_KIND:
        raise MLflowTrackingError("B4 real sibling audit has an unexpected artifact_kind")
    if audit.payload.get("status") != "completed":
        raise MLflowTrackingError("completed B4 real report requires a completed partial audit")
    if audit.payload.get("evaluation_id") != payload.get("evaluation_id"):
        raise MLflowTrackingError("B4 real report and partial audit evaluation_id differ")
    if _source_hashes(audit.payload) != _source_hashes(payload):
        raise MLflowTrackingError("B4 real report and partial audit source hashes differ")
    return audit


def _source_hashes(payload: dict[str, Any]) -> tuple[str, str]:
    source = payload.get("source")
    if not isinstance(source, dict):
        raise MLflowTrackingError("B4 real artifact must contain source hashes")
    fixtures_sha256 = source.get("fixtures_sha256")
    split_sha256 = source.get("split_sha256")
    for value in (fixtures_sha256, split_sha256):
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise MLflowTrackingError("B4 real artifact source hashes must be lowercase SHA-256")
    return str(fixtures_sha256), str(split_sha256)


def _bundle_sha256(artifact: _LoadedArtifact, audit: _LoadedArtifact | None) -> str:
    digest = hashlib.sha256()
    digest.update(artifact.sha256.encode("ascii"))
    if audit is not None:
        digest.update(b"\0")
        digest.update(audit.sha256.encode("ascii"))
    return digest.hexdigest()


def _build_tags(
    artifact: _LoadedArtifact,
    audit: _LoadedArtifact | None,
    bundle_sha256: str,
) -> dict[str, str]:
    payload = artifact.payload
    status = payload.get("status")
    if not isinstance(status, str):
        status = "aborted" if payload.get("artifact_kind") == _B4_REAL_ABORT_KIND else "completed"

    tags = {
        "nbtriage.artifact_sha256": artifact.sha256,
        _BUNDLE_TAG: bundle_sha256,
        "nbtriage.evaluation_status": status,
    }
    if audit is not None:
        tags["nbtriage.audit_sha256"] = audit.sha256

    for key in (
        "generated_at",
        "artifact_kind",
        "artifact_profile",
        "split_id",
        "fixture_set_id",
        "evaluation_scope",
    ):
        _copy_scalar_tag(tags, f"nbtriage.{key}", payload.get(key))
    decision = _evaluation_decision(payload)
    _copy_scalar_tag(tags, "nbtriage.evaluation_decision", decision)
    if isinstance(payload.get("promotion_gate"), dict):
        _copy_scalar_tag(tags, "nbtriage.promotion_decision", decision)

    source = payload.get("source")
    if isinstance(source, dict):
        for key, value in source.items():
            if key.endswith("_sha256"):
                _copy_scalar_tag(tags, f"nbtriage.source.{key}", value)

    git_sha, git_dirty = _publisher_git_state()
    tags["nbtriage.publisher.git_sha"] = git_sha
    tags["nbtriage.publisher.git_dirty"] = str(git_dirty).lower()
    return tags


def _build_parameters(payload: dict[str, Any]) -> dict[str, str]:
    parameters: dict[str, str] = {
        "nbtriage.evaluation_id": str(payload["evaluation_id"]),
        "nbtriage.schema_version": str(payload["schema_version"]),
    }
    for key in ("split_id", "fixture_set_id", "artifact_kind", "artifact_profile"):
        _copy_scalar_parameter(parameters, f"nbtriage.{key}", payload.get(key))

    for container_name in ("summary", "authorization"):
        container = payload.get(container_name)
        if not isinstance(container, dict):
            continue
        for key in (
            "provider",
            "model",
            "prompt_id",
            "model_kind",
            "primary_score_split",
            "trials_per_fixture",
            "max_provider_requests",
            "max_agent_input_tokens_per_trial",
            "max_output_tokens_per_trial",
            "deadline_seconds",
            "whole_run_timeout_seconds",
            "declared_budget_usd",
        ):
            _copy_scalar_parameter(
                parameters,
                f"nbtriage.{container_name}.{key}",
                container.get(key),
            )

    for root in _PARAMETER_ROOTS:
        value = payload.get(root)
        if isinstance(value, dict):
            _flatten_parameters(value, f"nbtriage.{root}", parameters)
    return parameters


def _build_metrics(payload: dict[str, Any]) -> dict[str, float]:
    metrics: dict[str, float] = {}
    for root in _METRIC_ROOTS:
        value = payload.get(root)
        if isinstance(value, dict):
            _flatten_metrics(value, root, metrics)
    return metrics


def _flatten_parameters(value: dict[str, Any], prefix: str, output: dict[str, str]) -> None:
    for key, child in value.items():
        parameter_key = _safe_key(f"{prefix}.{key}")
        if isinstance(child, dict):
            _flatten_parameters(child, parameter_key, output)
        elif isinstance(child, (str, int, float, bool)) and child is not None:
            output[parameter_key] = str(child)


def _flatten_metrics(value: dict[str, Any], prefix: str, output: dict[str, float]) -> None:
    for key, child in value.items():
        metric_key = _safe_key(f"{prefix}.{key}")
        if isinstance(child, dict):
            _flatten_metrics(child, metric_key, output)
        elif isinstance(child, bool):
            output[metric_key] = float(child)
        elif isinstance(child, (int, float)):
            metric_value = float(child)
            if math.isfinite(metric_value):
                output[metric_key] = metric_value


def _evaluation_decision(payload: dict[str, Any]) -> Any:
    promotion_gate = payload.get("promotion_gate")
    if isinstance(promotion_gate, dict):
        return promotion_gate.get("decision")
    quality_claim_gate = payload.get("quality_claim_gate")
    if isinstance(quality_claim_gate, dict):
        return quality_claim_gate.get("decision")
    calibration_gate = payload.get("calibration_gate")
    if isinstance(calibration_gate, dict):
        return (
            "calibration_passed" if calibration_gate.get("passed") is True else "calibration_failed"
        )
    decision = payload.get("decision")
    if isinstance(decision, dict):
        return decision.get("outcome") or decision.get("decision")
    return decision


def _copy_scalar_tag(output: dict[str, str], key: str, value: Any) -> None:
    if isinstance(value, (str, int, float, bool)) and value is not None:
        output[_safe_key(key)] = str(value)


def _copy_scalar_parameter(output: dict[str, str], key: str, value: Any) -> None:
    if isinstance(value, (str, int, float, bool)) and value is not None:
        output[_safe_key(key)] = str(value)


def _safe_key(value: str) -> str:
    return _SAFE_KEY_PATTERN.sub("_", value)[:250]


def _publisher_git_state() -> tuple[str, bool]:
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        status = subprocess.run(
            ["git", "status", "--porcelain=v1"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return "unavailable", True

    git_sha = revision.stdout.strip() if revision.returncode == 0 else "unborn"
    git_dirty = status.returncode != 0 or bool(status.stdout)
    if git_sha == "unborn":
        git_dirty = True
    return git_sha, git_dirty


def _log_staged_artifact(
    mlflow: Any,
    staging_root: Path,
    artifact: _LoadedArtifact,
    artifact_path: str,
) -> None:
    target_dir = staging_root / artifact_path
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / artifact.path.name
    target.write_bytes(artifact.raw)
    mlflow.log_artifact(str(target), artifact_path=artifact_path)
