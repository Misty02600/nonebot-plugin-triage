"""仓库维护者使用的回答质量评测。"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ANSWER_QUALITY_EVALUATION_ID = "answer-quality-human-rubric-v2"
ANSWER_QUALITY_RUBRIC_ID = "answer-quality-v1"
ANSWER_QUALITY_OFFLINE_SCOPE = "offline_fixed_fixture"
ANSWER_QUALITY_FIXTURE_REVISION_PREFIX = "nbtriage-answer-quality-fixtures-sha256:"
ANSWER_QUALITY_RUBRIC_REVISION_PREFIX = "nbtriage-answer-quality-rubric-sha256:"
ANSWER_QUALITY_RUBRIC_REVISION = (
    "nbtriage-answer-quality-rubric-sha256:"
    "a238938a03893faddc4f9e699b2ba519a565dc5b59d3a3ca46637a90fe992664"
)
ANSWER_QUALITY_AXES = (
    "groundedness",
    "completeness",
    "limitation_awareness",
    "overclaim_control",
)
_FIXTURE_PURPOSES = frozenset({"rubric_calibration", "candidate_quality"})
_REVIEW_KINDS = frozenset({"synthetic_calibration_oracle", "human_review"})


class AnswerQualityEvaluationError(ValueError):
    pass


def _canonical_revision(payload: dict[str, Any], *, domain: bytes, prefix: str) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    digest = hashlib.sha256(domain)
    digest.update(canonical)
    return prefix + digest.hexdigest()


def answer_quality_fixture_revision(payload: dict[str, Any]) -> str:
    return _canonical_revision(
        payload,
        domain=b"nbtriage-answer-quality-fixtures-v1\0",
        prefix=ANSWER_QUALITY_FIXTURE_REVISION_PREFIX,
    )


def answer_quality_rubric_revision(payload: dict[str, Any]) -> str:
    return _canonical_revision(
        payload,
        domain=b"nbtriage-answer-quality-rubric-v1\0",
        prefix=ANSWER_QUALITY_RUBRIC_REVISION_PREFIX,
    )


def evaluate_answer_quality(
    rubric_path: Path,
    fixtures_path: Path,
    annotations_path: Path,
    *,
    source_report_path: Path | None = None,
) -> dict[str, Any]:
    """校验并汇总 answer + citations 的人工评分合同。

    校准集只验证评分轴、锚点和报告机械是否可复现，不产生真实模型质量结论。只有非校准样本、完整人工
    复核和全部质量门同时满足时，报告才允许把结果标记为候选质量证据。

    Args:
        rubric_path: 版本化评分轴、锚点和门槛定义。
        fixtures_path: 固定的可见 Context、候选回答和引用集合。
        annotations_path: 与 Fixture 完整对应的人工评分或合成校准 Oracle。
        source_report_path: 候选质量 Fixture 对应的原始真实 B4 报告；校准集必须省略。

    Returns:
        包含来源哈希、逐样本评分、聚合指标、校准门和质量声明门的报告。

    Raises:
        AnswerQualityEvaluationError: 任一合同无效、相互错绑或标注不完整。
    """
    rubric_raw, rubric = _load_object(rubric_path, "answer quality rubric")
    fixtures_raw, fixtures = _load_object(fixtures_path, "answer quality fixtures")
    annotations_raw, annotations = _load_object(annotations_path, "answer quality annotations")
    scale_minimum, scale_maximum = _validate_rubric(rubric)
    samples = _validate_fixtures(fixtures)
    try:
        fixture_revision = answer_quality_fixture_revision(fixtures)
        rubric_revision = answer_quality_rubric_revision(rubric)
    except (TypeError, ValueError) as error:
        raise AnswerQualityEvaluationError("fixture content cannot be canonicalized") from error
    if rubric_revision != ANSWER_QUALITY_RUBRIC_REVISION:
        raise AnswerQualityEvaluationError("unsupported answer quality rubric revision")
    judgments = _validate_annotations(
        annotations,
        fixture_set_id=fixtures["fixture_set_id"],
        fixture_revision=fixture_revision,
        rubric_revision=rubric_revision,
        sample_ids=set(samples),
        scale_minimum=scale_minimum,
        scale_maximum=scale_maximum,
    )
    _validate_contract_binding(rubric, fixtures, annotations)
    source_report_reference = _validate_source_report_binding(
        rubric,
        fixtures,
        source_report_path,
    )

    rows = []
    axis_totals = dict.fromkeys(ANSWER_QUALITY_AXES, 0)
    score_coverage = {axis: set() for axis in ANSWER_QUALITY_AXES}
    passing_samples = 0
    minimum_score = rubric["thresholds"]["min_sample_axis_score"]
    for sample_id, sample in samples.items():
        judgment = judgments[sample_id]
        scores = judgment["scores"]
        for axis in ANSWER_QUALITY_AXES:
            axis_totals[axis] += scores[axis]
            score_coverage[axis].add(scores[axis])
        passed = all(score >= minimum_score for score in scores.values())
        passing_samples += passed
        rows.append(
            {
                "sample_id": sample_id,
                "category": sample["category"],
                "answer": sample["candidate"]["answer"],
                "citations": sample["candidate"]["citations"],
                "scores": scores,
                "rationales": judgment["rationales"],
                "passed": passed,
            }
        )

    sample_count = len(rows)
    axis_means = {axis: _ratio(total, sample_count) for axis, total in axis_totals.items()}
    critical_axes = rubric["thresholds"]["critical_axes"]
    critical_zero_count = sum(
        row["scores"][axis] == scale_minimum for row in rows for axis in critical_axes
    )
    required_score_values = set(range(scale_minimum, scale_maximum + 1))
    coverage_rows = {axis: sorted(values) for axis, values in score_coverage.items()}
    calibration_checks = {
        "rubric_calibration_dataset": fixtures["purpose"] == "rubric_calibration",
        "synthetic_calibration_oracle": (
            annotations["review"]["kind"] == "synthetic_calibration_oracle"
        ),
        "all_score_anchors_covered": all(
            values == required_score_values for values in score_coverage.values()
        ),
    }
    thresholds = rubric["thresholds"]
    source_evaluation = fixtures.get("source_evaluation") or {}
    quality_checks = {
        "non_calibration_dataset": fixtures["purpose"] == "candidate_quality",
        "human_reviewed": annotations["review"]["kind"] == "human_review",
        "offline_fixed_fixture_scope": (
            fixtures.get("evaluation_scope") == ANSWER_QUALITY_OFFLINE_SCOPE
        ),
        "real_model_multi_trial": source_evaluation.get("real_model_multi_trial") is True,
        "forward_hidden_score_split": source_evaluation.get("score_split") == "forward_hidden",
        "source_b4_promotion_gate_passed": (source_evaluation.get("promotion_gate_passed") is True),
        "all_axis_means_meet_threshold": all(
            value >= thresholds["min_axis_mean"] for value in axis_means.values()
        ),
        "all_samples_meet_minimum": passing_samples == sample_count,
        "critical_zero_count_within_limit": (
            critical_zero_count <= thresholds["max_critical_zero_count"]
        ),
    }
    quality_eligible = all(quality_checks.values())
    if fixtures["purpose"] == "rubric_calibration":
        quality_decision = "not_eligible_calibration_only"
    elif annotations["review"]["kind"] != "human_review":
        quality_decision = "not_eligible_human_review_required"
    elif source_evaluation.get("promotion_gate_passed") is not True:
        quality_decision = "not_eligible_source_b4_gate_failed"
    elif quality_eligible:
        quality_decision = "eligible_as_offline_fixed_fixture_human_evidence"
    else:
        quality_decision = "not_eligible_answer_quality_gate_failed"

    return {
        "schema_version": 2,
        "evaluation_id": ANSWER_QUALITY_EVALUATION_ID,
        "rubric_id": rubric["rubric_id"],
        "fixture_set_id": fixtures["fixture_set_id"],
        "annotation_set_id": annotations["annotation_set_id"],
        "evaluation_scope": fixtures.get("evaluation_scope", "rubric_calibration"),
        "source_evaluation": fixtures.get("source_evaluation"),
        "generated_at": datetime.now(UTC).isoformat(),
        "source": {
            "rubric_path": str(rubric_path),
            "rubric_sha256": hashlib.sha256(rubric_raw).hexdigest(),
            "rubric_revision": rubric_revision,
            "fixtures_path": str(fixtures_path),
            "fixtures_sha256": hashlib.sha256(fixtures_raw).hexdigest(),
            "fixture_revision": fixture_revision,
            "annotations_path": str(annotations_path),
            "annotations_sha256": hashlib.sha256(annotations_raw).hexdigest(),
            "source_report_path": source_report_reference[0],
            "source_report_sha256": source_report_reference[1],
        },
        "summary": {
            "sample_count": sample_count,
            "purpose": fixtures["purpose"],
            "synthetic_only": fixtures["synthetic_only"],
            "evaluation_scope": fixtures.get("evaluation_scope", "rubric_calibration"),
            "review_kind": annotations["review"]["kind"],
            "human_reviewed": annotations["review"]["kind"] == "human_review",
            "model_calls": 0,
            "external_tool_calls": 0,
        },
        "rubric": rubric,
        "metrics": {
            "axis_means": axis_means,
            "overall_mean": _ratio(sum(axis_totals.values()), sample_count * len(axis_totals)),
            "passing_sample_rate": _ratio(passing_samples, sample_count),
            "critical_zero_count": critical_zero_count,
            "score_coverage": coverage_rows,
        },
        "calibration_gate": {
            "passed": all(calibration_checks.values()),
            "checks": calibration_checks,
        },
        "quality_claim_gate": {
            "eligible": quality_eligible,
            "checks": quality_checks,
            "decision": quality_decision,
        },
        "judgments": rows,
        "limitations": _report_limitations(fixtures),
    }


def _load_object(path: Path, label: str) -> tuple[bytes, dict[str, Any]]:
    try:
        raw = path.read_bytes()
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError) as error:
        raise AnswerQualityEvaluationError(f"failed to load {label} {path}: {error}") from error
    if not isinstance(payload, dict):
        raise AnswerQualityEvaluationError(f"{label} must be a JSON object")
    return raw, payload


def _validate_rubric(payload: dict[str, Any]) -> tuple[int, int]:
    required = {"schema_version", "rubric_id", "scale", "axes", "thresholds"}
    if set(payload) != required or payload.get("schema_version") != 1:
        raise AnswerQualityEvaluationError("rubric must be a schema_version 1 contract")
    if payload.get("rubric_id") != ANSWER_QUALITY_RUBRIC_ID:
        raise AnswerQualityEvaluationError("rubric_id is not supported")
    scale = payload.get("scale")
    if not isinstance(scale, dict) or scale != {"minimum": 0, "maximum": 2}:
        raise AnswerQualityEvaluationError("rubric scale must be the inclusive integer range 0..2")
    axes = payload.get("axes")
    if not isinstance(axes, dict) or set(axes) != set(ANSWER_QUALITY_AXES):
        raise AnswerQualityEvaluationError("rubric axes must match the frozen answer-quality axes")
    for axis, definition in axes.items():
        if not isinstance(definition, dict) or set(definition) != {"description", "anchors"}:
            raise AnswerQualityEvaluationError(f"rubric axis {axis} has invalid fields")
        if not isinstance(definition["description"], str) or not definition["description"].strip():
            raise AnswerQualityEvaluationError(f"rubric axis {axis} needs a description")
        anchors = definition["anchors"]
        if not isinstance(anchors, dict) or set(anchors) != {"0", "1", "2"}:
            raise AnswerQualityEvaluationError(f"rubric axis {axis} must define anchors 0, 1, 2")
        if any(not isinstance(value, str) or not value.strip() for value in anchors.values()):
            raise AnswerQualityEvaluationError(f"rubric axis {axis} anchors must be non-empty")
    thresholds = payload.get("thresholds")
    threshold_fields = {
        "min_axis_mean",
        "min_sample_axis_score",
        "critical_axes",
        "max_critical_zero_count",
        "require_human_review",
        "require_non_calibration_dataset",
    }
    if not isinstance(thresholds, dict) or set(thresholds) != threshold_fields:
        raise AnswerQualityEvaluationError("rubric thresholds have invalid fields")
    if (
        not isinstance(thresholds["min_axis_mean"], int | float)
        or isinstance(thresholds["min_axis_mean"], bool)
        or not 0 <= thresholds["min_axis_mean"] <= 2
    ):
        raise AnswerQualityEvaluationError("min_axis_mean must be between 0 and 2")
    if (
        not isinstance(thresholds["min_sample_axis_score"], int)
        or isinstance(thresholds["min_sample_axis_score"], bool)
        or not 0 <= thresholds["min_sample_axis_score"] <= 2
    ):
        raise AnswerQualityEvaluationError("min_sample_axis_score must be an integer from 0 to 2")
    critical_axes = thresholds["critical_axes"]
    if (
        not isinstance(critical_axes, list)
        or not critical_axes
        or any(not isinstance(axis, str) for axis in critical_axes)
        or len(critical_axes) != len(set(critical_axes))
        or set(critical_axes) - set(ANSWER_QUALITY_AXES)
    ):
        raise AnswerQualityEvaluationError("critical_axes must be unique frozen axis IDs")
    if (
        not isinstance(thresholds["max_critical_zero_count"], int)
        or isinstance(thresholds["max_critical_zero_count"], bool)
        or thresholds["max_critical_zero_count"] < 0
    ):
        raise AnswerQualityEvaluationError("max_critical_zero_count must be non-negative")
    if thresholds["require_human_review"] is not True:
        raise AnswerQualityEvaluationError("quality claims must require human review")
    if thresholds["require_non_calibration_dataset"] is not True:
        raise AnswerQualityEvaluationError("quality claims must require a non-calibration dataset")
    return 0, 2


def _validate_fixtures(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    common = {
        "schema_version",
        "fixture_set_id",
        "rubric_id",
        "purpose",
        "synthetic_only",
        "fixtures",
    }
    schema_version = payload.get("schema_version")
    if schema_version == 1:
        if set(payload) != common:
            raise AnswerQualityEvaluationError("schema_version 1 fixture set has invalid fields")
        if payload.get("purpose") != "rubric_calibration":
            raise AnswerQualityEvaluationError(
                "candidate quality fixtures require schema_version 2 source provenance"
            )
    elif schema_version == 2:
        if set(payload) != {*common, "evaluation_scope", "source_evaluation"}:
            raise AnswerQualityEvaluationError("schema_version 2 fixture set has invalid fields")
        if payload.get("purpose") != "candidate_quality":
            raise AnswerQualityEvaluationError(
                "schema_version 2 fixtures are reserved for candidate quality"
            )
        _validate_source_evaluation(payload)
    else:
        raise AnswerQualityEvaluationError("fixture set schema_version is not supported")
    if not isinstance(payload.get("fixture_set_id"), str) or not payload["fixture_set_id"]:
        raise AnswerQualityEvaluationError("fixture_set_id must be non-empty")
    if payload.get("rubric_id") != ANSWER_QUALITY_RUBRIC_ID:
        raise AnswerQualityEvaluationError("fixture rubric_id is not supported")
    if payload.get("purpose") not in _FIXTURE_PURPOSES:
        raise AnswerQualityEvaluationError("fixture purpose is not supported")
    if not isinstance(payload.get("synthetic_only"), bool):
        raise AnswerQualityEvaluationError("fixture set must declare synthetic_only")
    fixtures = payload.get("fixtures")
    if not isinstance(fixtures, list) or not fixtures:
        raise AnswerQualityEvaluationError("fixture set must contain fixtures")
    samples = {}
    for sample in fixtures:
        if not isinstance(sample, dict) or set(sample) != {
            "sample_id",
            "category",
            "context",
            "candidate",
        }:
            raise AnswerQualityEvaluationError("each answer-quality fixture has invalid fields")
        sample_id = sample.get("sample_id")
        if not isinstance(sample_id, str) or not sample_id or sample_id in samples:
            raise AnswerQualityEvaluationError("sample IDs must be non-empty and unique")
        if not isinstance(sample.get("category"), str) or not sample["category"]:
            raise AnswerQualityEvaluationError(f"sample {sample_id} must contain category")
        context = sample.get("context")
        if not isinstance(context, dict) or set(context) != {
            "case_summary",
            "evidence",
            "known_limitations",
            "required_answer_points",
        }:
            raise AnswerQualityEvaluationError(f"sample {sample_id} has invalid context")
        if not isinstance(context["case_summary"], str) or not context["case_summary"].strip():
            raise AnswerQualityEvaluationError(f"sample {sample_id} needs a case summary")
        for field in ("known_limitations", "required_answer_points"):
            if not _non_empty_string_list(context[field], allow_empty=False):
                raise AnswerQualityEvaluationError(f"sample {sample_id} has invalid {field}")
        evidence = context["evidence"]
        if not isinstance(evidence, list):
            raise AnswerQualityEvaluationError(f"sample {sample_id} evidence must be an array")
        evidence_ids = set()
        citable_ids = set()
        for item in evidence:
            if not isinstance(item, dict) or set(item) != {"evidence_id", "citable", "facts"}:
                raise AnswerQualityEvaluationError(f"sample {sample_id} has invalid evidence")
            evidence_id = item["evidence_id"]
            if (
                not isinstance(evidence_id, str)
                or not evidence_id
                or evidence_id in evidence_ids
                or not isinstance(item["citable"], bool)
                or not _non_empty_string_list(item["facts"], allow_empty=False)
            ):
                raise AnswerQualityEvaluationError(f"sample {sample_id} has invalid evidence")
            evidence_ids.add(evidence_id)
            if item["citable"]:
                citable_ids.add(evidence_id)
        candidate = sample.get("candidate")
        if not isinstance(candidate, dict) or set(candidate) != {"answer", "citations"}:
            raise AnswerQualityEvaluationError(f"sample {sample_id} has invalid candidate")
        if not isinstance(candidate["answer"], str) or not candidate["answer"].strip():
            raise AnswerQualityEvaluationError(f"sample {sample_id} answer must be non-empty")
        if not _non_empty_string_list(candidate["citations"], allow_empty=True):
            raise AnswerQualityEvaluationError(f"sample {sample_id} citations are invalid")
        if set(candidate["citations"]) - citable_ids:
            raise AnswerQualityEvaluationError(
                f"sample {sample_id} citations must reference visible citable evidence"
            )
        samples[sample_id] = sample
    return samples


def _validate_source_evaluation(payload: dict[str, Any]) -> None:
    if payload.get("evaluation_scope") != ANSWER_QUALITY_OFFLINE_SCOPE:
        raise AnswerQualityEvaluationError(
            "candidate quality evaluation_scope must be offline_fixed_fixture"
        )
    if payload.get("synthetic_only") is not True:
        raise AnswerQualityEvaluationError(
            "offline fixed-fixture candidate quality must use synthetic inputs"
        )
    source = payload.get("source_evaluation")
    required = {
        "evaluation_id",
        "report_schema_version",
        "report_sha256",
        "generated_at",
        "evaluation_contract",
        "fixture_set_id",
        "split_id",
        "score_split",
        "model_kind",
        "provider",
        "model",
        "trials_per_fixture",
        "real_model_multi_trial",
        "promotion_gate_passed",
        "fixtures_path",
        "fixtures_sha256",
        "split_path",
        "split_sha256",
    }
    if not isinstance(source, dict) or set(source) != required:
        raise AnswerQualityEvaluationError("candidate quality source_evaluation is invalid")
    if source["evaluation_id"] != "b4-bounded-agent-real-v1":
        raise AnswerQualityEvaluationError("candidate quality must originate from a real B4 report")
    if source["report_schema_version"] != 3:
        raise AnswerQualityEvaluationError("candidate quality requires a schema v3 B4 report")
    report_sha256 = source["report_sha256"]
    if (
        not isinstance(report_sha256, str)
        or len(report_sha256) != 64
        or any(character not in "0123456789abcdef" for character in report_sha256)
    ):
        raise AnswerQualityEvaluationError("source report_sha256 is invalid")
    for field in (
        "generated_at",
        "fixture_set_id",
        "split_id",
        "provider",
        "model",
        "fixtures_path",
        "split_path",
    ):
        if not isinstance(source[field], str) or not source[field].strip():
            raise AnswerQualityEvaluationError(f"source {field} must be non-empty")
    try:
        generated_at = datetime.fromisoformat(source["generated_at"])
    except ValueError as error:
        raise AnswerQualityEvaluationError(
            "source generated_at must be an ISO timestamp"
        ) from error
    if generated_at.tzinfo is None:
        raise AnswerQualityEvaluationError("source generated_at must include a timezone")
    contract = source["evaluation_contract"]
    if (
        not isinstance(contract, dict)
        or not isinstance(contract.get("code_revision"), str)
        or not _is_revision(contract["code_revision"], prefix="nbtriage-source-sha256:")
    ):
        raise AnswerQualityEvaluationError("source evaluation_contract is invalid")
    if source["score_split"] != "forward_hidden" or source["model_kind"] != "real":
        raise AnswerQualityEvaluationError(
            "candidate quality requires a real forward_hidden source evaluation"
        )
    if (
        not isinstance(source["trials_per_fixture"], int)
        or isinstance(source["trials_per_fixture"], bool)
        or source["trials_per_fixture"] < 2
        or source["real_model_multi_trial"] is not True
    ):
        raise AnswerQualityEvaluationError(
            "candidate quality requires a real multi-trial source evaluation"
        )
    if not isinstance(source["promotion_gate_passed"], bool):
        raise AnswerQualityEvaluationError("source promotion_gate_passed must be boolean")
    for field in ("fixtures_sha256", "split_sha256"):
        if not _is_sha256(source[field]):
            raise AnswerQualityEvaluationError(f"source {field} is invalid")


def _validate_source_report_binding(
    rubric: dict[str, Any], fixtures: dict[str, Any], source_report_path: Path | None
) -> tuple[str | None, str | None]:
    if fixtures["purpose"] == "rubric_calibration":
        if source_report_path is not None:
            raise AnswerQualityEvaluationError("calibration fixtures must not bind a source report")
        return None, None
    if source_report_path is None:
        raise AnswerQualityEvaluationError("candidate quality requires its source B4 report")
    report_raw, report = _load_object(source_report_path, "source B4 report")
    source = fixtures["source_evaluation"]
    if hashlib.sha256(report_raw).hexdigest() != source["report_sha256"]:
        raise AnswerQualityEvaluationError("source B4 report content does not match its digest")
    summary = report.get("summary")
    promotion_gate = report.get("promotion_gate")
    expected = {
        "evaluation_id": report.get("evaluation_id"),
        "report_schema_version": report.get("schema_version"),
        "generated_at": report.get("generated_at"),
        "evaluation_contract": report.get("evaluation_contract"),
        "fixture_set_id": report.get("fixture_set_id"),
        "split_id": report.get("split_id"),
        "score_split": (
            promotion_gate.get("score_split") if isinstance(promotion_gate, dict) else None
        ),
        "model_kind": summary.get("model_kind") if isinstance(summary, dict) else None,
        "provider": summary.get("provider") if isinstance(summary, dict) else None,
        "model": summary.get("model") if isinstance(summary, dict) else None,
        "trials_per_fixture": (
            summary.get("trials_per_fixture") if isinstance(summary, dict) else None
        ),
        "real_model_multi_trial": (
            promotion_gate.get("checks", {}).get("real_model_multi_trial")
            if isinstance(promotion_gate, dict) and isinstance(promotion_gate.get("checks"), dict)
            else None
        ),
        "promotion_gate_passed": (
            promotion_gate.get("passed") if isinstance(promotion_gate, dict) else None
        ),
    }
    if any(source[field] != value for field, value in expected.items()):
        raise AnswerQualityEvaluationError("source B4 report projection does not match fixtures")
    from tools.nbtriage_maintainer.answer_review_export import (
        AnswerReviewExportError,
        build_b4_answer_quality_review_payloads,
    )

    fixtures_source_path = Path(source["fixtures_path"])
    split_source_path = Path(source["split_path"])
    try:
        replayed, _ = build_b4_answer_quality_review_payloads(
            report_raw=report_raw,
            report=report,
            fixtures_path=fixtures_source_path,
            split_path=split_source_path,
            rubric=rubric,
        )
    except AnswerReviewExportError as error:
        raise AnswerQualityEvaluationError(
            f"candidate source projection cannot be replayed: {error}"
        ) from error
    if source["fixtures_sha256"] != _file_sha256(fixtures_source_path, "source B4 fixtures"):
        raise AnswerQualityEvaluationError("source B4 fixtures do not match their digest")
    if source["split_sha256"] != _file_sha256(split_source_path, "source B4 split"):
        raise AnswerQualityEvaluationError("source B4 split does not match its digest")
    if replayed != fixtures:
        raise AnswerQualityEvaluationError("candidate fixtures do not match replayed B4 projection")
    return str(source_report_path), hashlib.sha256(report_raw).hexdigest()


def _validate_annotations(
    payload: dict[str, Any],
    *,
    fixture_set_id: str,
    fixture_revision: str,
    rubric_revision: str,
    sample_ids: set[str],
    scale_minimum: int,
    scale_maximum: int,
) -> dict[str, dict[str, Any]]:
    required = {
        "schema_version",
        "annotation_set_id",
        "fixture_set_id",
        "fixture_revision",
        "rubric_revision",
        "rubric_id",
        "review",
        "annotations",
    }
    if set(payload) != required or payload.get("schema_version") != 3:
        raise AnswerQualityEvaluationError("annotation set must be a schema_version 3 contract")
    if not isinstance(payload.get("annotation_set_id"), str) or not payload["annotation_set_id"]:
        raise AnswerQualityEvaluationError("annotation_set_id must be non-empty")
    if payload.get("fixture_set_id") != fixture_set_id:
        raise AnswerQualityEvaluationError("annotations are bound to a different fixture set")
    if payload.get("fixture_revision") != fixture_revision:
        raise AnswerQualityEvaluationError("annotations are bound to different fixture content")
    if payload.get("rubric_revision") != rubric_revision:
        raise AnswerQualityEvaluationError("annotations are bound to different rubric content")
    if payload.get("rubric_id") != ANSWER_QUALITY_RUBRIC_ID:
        raise AnswerQualityEvaluationError("annotation rubric_id is not supported")
    review = payload.get("review")
    if not isinstance(review, dict) or set(review) != {"kind", "reviewer_id", "completed_at"}:
        raise AnswerQualityEvaluationError("annotations must contain review provenance")
    if review["kind"] not in _REVIEW_KINDS:
        raise AnswerQualityEvaluationError("review kind is not supported")
    if not isinstance(review["reviewer_id"], str) or not review["reviewer_id"].strip():
        raise AnswerQualityEvaluationError("reviewer_id must be non-empty")
    if not isinstance(review["completed_at"], str):
        raise AnswerQualityEvaluationError("completed_at must be an ISO timestamp")
    try:
        completed_at = datetime.fromisoformat(review["completed_at"])
    except ValueError as error:
        raise AnswerQualityEvaluationError("completed_at must be an ISO timestamp") from error
    if completed_at.tzinfo is None:
        raise AnswerQualityEvaluationError("completed_at must include a timezone")
    annotations = payload.get("annotations")
    if not isinstance(annotations, list) or not annotations:
        raise AnswerQualityEvaluationError("annotation set must contain annotations")
    judgments = {}
    for annotation in annotations:
        if not isinstance(annotation, dict) or set(annotation) != {
            "sample_id",
            "scores",
            "rationales",
        }:
            raise AnswerQualityEvaluationError("each annotation has invalid fields")
        sample_id = annotation.get("sample_id")
        if not isinstance(sample_id, str) or not sample_id or sample_id in judgments:
            raise AnswerQualityEvaluationError("annotation sample IDs must be unique")
        scores = annotation.get("scores")
        rationales = annotation.get("rationales")
        if not isinstance(scores, dict) or set(scores) != set(ANSWER_QUALITY_AXES):
            raise AnswerQualityEvaluationError(f"annotation {sample_id} has invalid score axes")
        if any(
            not isinstance(score, int)
            or isinstance(score, bool)
            or not scale_minimum <= score <= scale_maximum
            for score in scores.values()
        ):
            raise AnswerQualityEvaluationError(f"annotation {sample_id} has an invalid score")
        if (
            not isinstance(rationales, dict)
            or set(rationales) != set(ANSWER_QUALITY_AXES)
            or any(not isinstance(text, str) or not text.strip() for text in rationales.values())
        ):
            raise AnswerQualityEvaluationError(
                f"annotation {sample_id} needs a rationale for every axis"
            )
        judgments[sample_id] = annotation
    if set(judgments) != sample_ids:
        missing = sorted(sample_ids - set(judgments))
        extra = sorted(set(judgments) - sample_ids)
        raise AnswerQualityEvaluationError(
            f"annotation coverage mismatch; missing={missing}, extra={extra}"
        )
    return judgments


def _validate_contract_binding(
    rubric: dict[str, Any], fixtures: dict[str, Any], annotations: dict[str, Any]
) -> None:
    if fixtures["rubric_id"] != rubric["rubric_id"]:
        raise AnswerQualityEvaluationError("fixtures are bound to a different rubric")
    if annotations["rubric_id"] != rubric["rubric_id"]:
        raise AnswerQualityEvaluationError("annotations are bound to a different rubric")
    purpose = fixtures["purpose"]
    review_kind = annotations["review"]["kind"]
    if purpose == "rubric_calibration" and review_kind != "synthetic_calibration_oracle":
        raise AnswerQualityEvaluationError(
            "rubric calibration fixtures require a synthetic calibration oracle"
        )
    if purpose == "candidate_quality" and review_kind != "human_review":
        raise AnswerQualityEvaluationError(
            "candidate quality fixtures require an explicit human review"
        )


def _report_limitations(fixtures: dict[str, Any]) -> list[str]:
    common = [
        "Citation IDs are checked against the visible citable evidence set; the rubric still "
        "requires a reviewer to judge whether cited facts support each material claim.",
    ]
    if fixtures["purpose"] == "rubric_calibration":
        return [
            "The bundled samples are synthetic calibration examples, not model-quality or "
            "production-prevalence evidence.",
            "A calibration oracle exercises rubric anchors but does not count as independent "
            "human review.",
            *common,
            "This report makes no model, Provider, B4 promotion, or user-visible quality claim.",
        ]
    return [
        "This report evaluates real-model outputs on fixed synthetic offline fixtures; it does "
        "not establish behavior, prevalence, latency, cost, or usefulness in a deployed Bot.",
        "The human answer-quality gate complements but does not replace the structural B4 "
        "promotion gate, safety checks, or a later deployment shadow/canary evaluation.",
        *common,
    ]


def _non_empty_string_list(value: Any, *, allow_empty: bool) -> bool:
    if not isinstance(value, list) or (not allow_empty and not value):
        return False
    if not all(isinstance(item, str) and bool(item.strip()) for item in value):
        return False
    return len(value) == len(set(value))


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_revision(value: Any, *, prefix: str) -> bool:
    return isinstance(value, str) and value.startswith(prefix) and _is_sha256(value[len(prefix) :])


def _file_sha256(path: Path, label: str) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as error:
        raise AnswerQualityEvaluationError(f"failed to load {label} {path}: {error}") from error


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0
