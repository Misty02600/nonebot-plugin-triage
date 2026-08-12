import json
from pathlib import Path

import pytest
from tools.nbtriage_maintainer.answer_quality_evaluation import (
    ANSWER_QUALITY_AXES,
    AnswerQualityEvaluationError,
    answer_quality_rubric_revision,
    evaluate_answer_quality,
)
from tools.nbtriage_maintainer.cli import main

ROOT = Path(__file__).resolve().parents[1]
RUBRIC = ROOT / "evals" / "rubrics" / "answer-quality-v1.json"
FIXTURES = ROOT / "evals" / "datasets" / "fixtures" / "answer-quality-calibration-v1.json"
ANNOTATIONS = ROOT / "evals" / "curation" / "answer-quality" / "calibration-v1.json"


def test_answer_quality_calibration_covers_every_rubric_anchor_without_quality_claim() -> None:
    report = evaluate_answer_quality(RUBRIC, FIXTURES, ANNOTATIONS)

    assert report["summary"] == {
        "sample_count": 5,
        "purpose": "rubric_calibration",
        "synthetic_only": True,
        "evaluation_scope": "rubric_calibration",
        "review_kind": "synthetic_calibration_oracle",
        "human_reviewed": False,
        "model_calls": 0,
        "external_tool_calls": 0,
    }
    assert report["metrics"]["axis_means"] == {
        "groundedness": 1.4,
        "completeness": 1.2,
        "limitation_awareness": 1.2,
        "overclaim_control": 1.4,
    }
    assert report["metrics"]["overall_mean"] == 1.3
    assert report["metrics"]["passing_sample_rate"] == 0.6
    assert report["metrics"]["score_coverage"] == {axis: [0, 1, 2] for axis in ANSWER_QUALITY_AXES}
    assert report["calibration_gate"]["passed"] is True
    assert report["quality_claim_gate"]["eligible"] is False
    assert report["quality_claim_gate"]["decision"] == "not_eligible_calibration_only"


def test_answer_quality_cli_writes_local_calibration_report(tmp_path: Path) -> None:
    report_path = tmp_path / "answer-quality.json"

    exit_code = main(
        [
            "evaluate-answer-quality",
            "--rubric",
            str(RUBRIC),
            "--fixtures",
            str(FIXTURES),
            "--annotations",
            str(ANNOTATIONS),
            "--report",
            str(report_path),
        ]
    )

    assert exit_code == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["schema_version"] == 2
    assert report["evaluation_id"] == "answer-quality-human-rubric-v2"
    assert report["summary"]["model_calls"] == 0
    assert report["quality_claim_gate"]["eligible"] is False


def test_answer_quality_rejects_incomplete_annotation_coverage(tmp_path: Path) -> None:
    payload = json.loads(ANNOTATIONS.read_text(encoding="utf-8"))
    payload["annotations"].pop()
    annotations = tmp_path / "incomplete.json"
    annotations.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(AnswerQualityEvaluationError, match="coverage mismatch"):
        evaluate_answer_quality(RUBRIC, FIXTURES, annotations)


def test_answer_quality_rejects_duplicate_annotation_keys(tmp_path: Path) -> None:
    annotations = tmp_path / "duplicate-annotations.json"
    raw = ANNOTATIONS.read_text(encoding="utf-8")
    annotations.write_text(
        raw.replace(
            '"schema_version": 3,',
            '"schema_version": 3,\n  "schema_version": 3,',
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(AnswerQualityEvaluationError, match="failed to load"):
        evaluate_answer_quality(RUBRIC, FIXTURES, annotations)


@pytest.mark.parametrize(
    ("section", "field"),
    [("thresholds", "min_axis_mean"), ("axes", "groundedness")],
)
def test_answer_quality_rejects_annotations_for_changed_rubric_content(
    tmp_path: Path,
    section: str,
    field: str,
) -> None:
    payload = json.loads(RUBRIC.read_text(encoding="utf-8"))
    if section == "thresholds":
        payload[section][field] = 1.75
    else:
        payload[section][field]["anchors"]["2"] += " 已篡改。"
    rubric = tmp_path / "changed-rubric.json"
    rubric.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(AnswerQualityEvaluationError, match="unsupported answer quality rubric"):
        evaluate_answer_quality(rubric, FIXTURES, ANNOTATIONS)


@pytest.mark.parametrize(
    ("section", "field"),
    [("thresholds", "min_axis_mean"), ("axes", "groundedness")],
)
def test_answer_quality_rejects_synchronously_resigned_rubric(
    tmp_path: Path,
    section: str,
    field: str,
) -> None:
    rubric_payload = json.loads(RUBRIC.read_text(encoding="utf-8"))
    if section == "thresholds":
        rubric_payload[section][field] = 0
    else:
        rubric_payload[section][field]["anchors"]["2"] = "被替换的宽松满分锚点。"
    rubric = tmp_path / "resigned-rubric.json"
    rubric.write_text(json.dumps(rubric_payload, ensure_ascii=False), encoding="utf-8")

    annotations_payload = json.loads(ANNOTATIONS.read_text(encoding="utf-8"))
    annotations_payload["rubric_revision"] = answer_quality_rubric_revision(rubric_payload)
    annotations = tmp_path / "resigned-annotations.json"
    annotations.write_text(json.dumps(annotations_payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(AnswerQualityEvaluationError, match="unsupported answer quality rubric"):
        evaluate_answer_quality(rubric, FIXTURES, annotations)


def test_answer_quality_canonical_revisions_ignore_json_layout(tmp_path: Path) -> None:
    rubric = tmp_path / "rubric.json"
    fixtures = tmp_path / "fixtures.json"
    rubric.write_text(
        json.dumps(json.loads(RUBRIC.read_text(encoding="utf-8")), ensure_ascii=False),
        encoding="utf-8",
    )
    fixtures.write_text(
        json.dumps(json.loads(FIXTURES.read_text(encoding="utf-8")), ensure_ascii=False),
        encoding="utf-8",
    )

    report = evaluate_answer_quality(rubric, fixtures, ANNOTATIONS)

    assert report["calibration_gate"]["passed"] is True


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
