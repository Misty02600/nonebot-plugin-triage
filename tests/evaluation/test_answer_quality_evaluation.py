import json
from pathlib import Path

import pytest
from tools.nbtriage_maintainer.answer_quality_evaluation import (
    AnswerQualityEvaluationError,
    evaluate_answer_quality,
)

ROOT = Path(__file__).resolve().parents[2]
RUBRIC = ROOT / "evals" / "rubrics" / "answer-quality-v1.json"
FIXTURES = ROOT / "evals" / "datasets" / "fixtures" / "answer-quality-calibration-v1.json"
ANNOTATIONS = ROOT / "evals" / "curation" / "answer-quality" / "calibration-v1.json"


def test_answer_quality_calibration_covers_every_rubric_anchor_without_quality_claim() -> None:
    report = evaluate_answer_quality(RUBRIC, FIXTURES, ANNOTATIONS)

    assert report["summary"]["model_calls"] == 0
    assert report["summary"]["external_tool_calls"] == 0
    assert report["metrics"]["overall_mean"] == 1.3
    assert report["calibration_gate"]["passed"] is True
    assert report["quality_claim_gate"]["eligible"] is False


def test_answer_quality_rejects_incomplete_annotation_coverage(tmp_path: Path) -> None:
    payload = json.loads(ANNOTATIONS.read_text(encoding="utf-8"))
    payload["annotations"].pop()
    annotations = tmp_path / "incomplete.json"
    annotations.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(AnswerQualityEvaluationError, match="coverage mismatch"):
        evaluate_answer_quality(RUBRIC, FIXTURES, annotations)


@pytest.mark.parametrize("field", ["answer", "context"])
def test_answer_quality_rejects_annotations_for_changed_fixture_content(
    tmp_path: Path,
    field: str,
) -> None:
    payload = json.loads(FIXTURES.read_text(encoding="utf-8"))
    if field == "answer":
        payload["fixtures"][0]["candidate"]["answer"] = "内容已被替换。"
    else:
        payload["fixtures"][0]["context"]["case_summary"] = "上下文已被替换。"
    fixtures = tmp_path / "changed-fixtures.json"
    fixtures.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(AnswerQualityEvaluationError, match="different fixture content"):
        evaluate_answer_quality(RUBRIC, fixtures, ANNOTATIONS)


def test_answer_quality_rejects_candidate_quality_without_human_review(tmp_path: Path) -> None:
    payload = json.loads(FIXTURES.read_text(encoding="utf-8"))
    payload["purpose"] = "candidate_quality"
    fixtures = tmp_path / "candidate-quality.json"
    fixtures.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(AnswerQualityEvaluationError, match="schema_version 2 source provenance"):
        evaluate_answer_quality(RUBRIC, fixtures, ANNOTATIONS)


def test_answer_quality_rejects_citation_outside_visible_citable_evidence(
    tmp_path: Path,
) -> None:
    payload = json.loads(FIXTURES.read_text(encoding="utf-8"))
    payload["fixtures"][0]["candidate"]["citations"] = ["runtime-observation-1"]
    fixtures = tmp_path / "bad-citation.json"
    fixtures.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(AnswerQualityEvaluationError, match="visible citable evidence"):
        evaluate_answer_quality(RUBRIC, fixtures, ANNOTATIONS)
