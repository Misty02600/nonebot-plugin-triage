"""仓库维护者使用的证据策略评测。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tools.nbtriage_maintainer.evidence_policy import (
    B3_EVIDENCE_POLICY_ID,
    select_next_evidence,
)


class EvidencePolicyEvaluationError(ValueError):
    pass


@dataclass
class _MultiLabelCounts:
    true_positive: int = 0
    false_positive: int = 0
    false_negative: int = 0

    def add(self, gold: set[str], predicted: set[str]) -> None:
        self.true_positive += len(gold & predicted)
        self.false_positive += len(predicted - gold)
        self.false_negative += len(gold - predicted)

    def report(self) -> dict[str, int | float]:
        precision = _ratio(self.true_positive, self.true_positive + self.false_positive)
        recall = _ratio(self.true_positive, self.true_positive + self.false_negative)
        return {
            "true_positive": self.true_positive,
            "false_positive": self.false_positive,
            "false_negative": self.false_negative,
            "precision": precision,
            "recall": recall,
            "f1": _ratio(2 * precision * recall, precision + recall),
        }


def evaluate_b3_evidence_policy(prediction_report: Path) -> dict[str, Any]:
    """在冻结 B1 validation 报告上评估单步补证策略。

    该入口拒绝 held-out，用于在已有 validation 上冻结确定性策略。它只读取报告中已经存在的预测与离线
    Gold，不调用模型或工具。

    Args:
        prediction_report: 只包含 validation 预测的 B1 机器报告。

    Returns:
        原始多槽位与 B3 单槽位策略的对照报告。

    Raises:
        EvidencePolicyEvaluationError: 报告不是可用的 B1 validation 工件。
    """
    raw, report = _load_report(prediction_report)
    summary = _object(report.get("summary"), "summary")
    if summary.get("score_splits") != ["validation"]:
        raise EvidencePolicyEvaluationError(
            "B3 evidence policy v1 can only be tuned on a validation-only B1 report"
        )
    rows = report.get("predictions")
    if not isinstance(rows, list) or not rows:
        raise EvidencePolicyEvaluationError("B1 report must contain predictions")

    b1_counts = _MultiLabelCounts()
    b3_counts = _MultiLabelCounts()
    predictions = []
    proposed_questions = 0
    valid_questions = 0
    gold_gap_cases = 0
    correct_case_hits = 0
    needs_evidence_actions = 0
    b1_candidate_questions = 0

    for row in rows:
        item = _object(row, "prediction row")
        if item.get("split") != "validation":
            raise EvidencePolicyEvaluationError("B3 policy report contains a non-validation row")
        case_id = _required_string(item.get("case_id"), "case_id")
        gold = _object(item.get("gold"), "gold")
        prediction = _object(item.get("prediction"), "prediction")
        gold_slots = set(_string_list(gold.get("missing_evidence"), "gold.missing_evidence"))
        b1_slots = _string_list(prediction.get("missing_evidence"), "prediction.missing_evidence")
        route = _required_string(prediction.get("route"), "prediction.route")
        phase = _required_string(prediction.get("fault_phase"), "prediction.fault_phase")
        selected = None
        if route == "needs_evidence":
            needs_evidence_actions += 1
            b1_candidate_questions += len(b1_slots)
            selected = select_next_evidence(phase, b1_slots)
            if selected is None:
                raise EvidencePolicyEvaluationError(
                    f"needs_evidence prediction has no candidate slots: {case_id}"
                )

        selected_set = {selected} if selected else set()
        b1_counts.add(gold_slots, set(b1_slots))
        b3_counts.add(gold_slots, selected_set)
        proposed_questions += selected is not None
        valid_questions += selected in gold_slots if selected else 0
        gold_gap_cases += bool(gold_slots)
        correct_case_hits += bool(selected_set & gold_slots)
        predictions.append(
            {
                "case_id": case_id,
                "gold_route": gold.get("route"),
                "predicted_route": route,
                "fault_phase": phase,
                "gold_missing_evidence": sorted(gold_slots),
                "b1_missing_evidence": b1_slots,
                "selected_evidence": selected,
                "selected_is_gold": selected in gold_slots if selected else None,
            }
        )

    return {
        "schema_version": 1,
        "evaluation_id": B3_EVIDENCE_POLICY_ID,
        "generated_at": datetime.now(UTC).isoformat(),
        "source": {
            "prediction_report": prediction_report.as_posix(),
            "prediction_report_sha256": hashlib.sha256(raw).hexdigest(),
            "split": "validation",
        },
        "summary": {
            "case_count": len(rows),
            "policy_id": B3_EVIDENCE_POLICY_ID,
            "needs_evidence_action_count": needs_evidence_actions,
            "proposed_question_count": proposed_questions,
            "model_calls": 0,
            "external_tool_calls": 0,
        },
        "metrics": {
            "b1_missing_evidence_micro": b1_counts.report(),
            "b3_selected_evidence_micro": b3_counts.report(),
            "question_precision_at_1": {
                "valid_questions": valid_questions,
                "unnecessary_questions": proposed_questions - valid_questions,
                "rate": _ratio(valid_questions, proposed_questions),
            },
            "gold_gap_case_hit_at_1": {
                "eligible_cases": gold_gap_cases,
                "hits": correct_case_hits,
                "rate": _ratio(correct_case_hits, gold_gap_cases),
            },
            "question_load": {
                "b1_candidate_questions": b1_candidate_questions,
                "b3_selected_questions": proposed_questions,
                "b1_average_per_needs_evidence_action": _ratio(
                    b1_candidate_questions, needs_evidence_actions
                ),
                "b3_average_per_needs_evidence_action": _ratio(
                    proposed_questions, needs_evidence_actions
                ),
            },
        },
        "predictions": predictions,
        "limitations": [
            "The policy was selected on validation and must not be reported as held-out evidence.",
            "Selecting one slot improves question precision but intentionally lowers "
            "per-turn recall.",
            "A forward hidden set is required before claiming that the phase priorities "
            "generalize.",
            "The report evaluates slot choice only; it does not ingest evidence or rerun B1.",
        ],
    }


def _load_report(path: Path) -> tuple[bytes, dict[str, Any]]:
    try:
        raw = path.read_bytes()
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError) as error:
        raise EvidencePolicyEvaluationError(f"failed to load B1 report {path}: {error}") from error
    if not isinstance(payload, dict) or payload.get("evaluation_id") != "b1-rag-only-v1":
        raise EvidencePolicyEvaluationError("prediction report must be a B1 evaluation report")
    return raw, payload


def _object(value: Any, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EvidencePolicyEvaluationError(f"{field_name} must be an object")
    return value


def _required_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise EvidencePolicyEvaluationError(f"{field_name} must be a non-empty string")
    return value


def _string_list(value: Any, field_name: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise EvidencePolicyEvaluationError(f"{field_name} must be a string list")
    return list(value)


def _ratio(numerator: float, denominator: float) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0
