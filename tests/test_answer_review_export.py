import asyncio
import json
from pathlib import Path

import pytest
from tools.nbtriage_maintainer.agent_evaluation import (
    B4_REAL_EVALUATION_ID,
    evaluate_b4_scripted_fixtures,
)
from tools.nbtriage_maintainer.answer_quality_evaluation import (
    ANSWER_QUALITY_AXES,
    AnswerQualityEvaluationError,
    evaluate_answer_quality,
)
from tools.nbtriage_maintainer.answer_review_export import (
    AnswerReviewExportError,
    build_b4_answer_quality_review,
)
from tools.nbtriage_maintainer.cli import main

ROOT = Path(__file__).resolve().parents[1]
B4_FIXTURES = ROOT / "evals" / "datasets" / "fixtures" / "b4-bounded-agent-v1.json"
B4_SPLIT = ROOT / "evals" / "datasets" / "splits" / "b4-gate-v1.json"
RUBRIC = ROOT / "evals" / "rubrics" / "answer-quality-v1.json"


def _real_report(path: Path, *, promotion_passed: bool = True) -> Path:
    report = asyncio.run(evaluate_b4_scripted_fixtures(B4_FIXTURES, B4_SPLIT))
    report["evaluation_id"] = B4_REAL_EVALUATION_ID
    report["summary"].update(
        {
            "model_kind": "real",
            "provider": "fixture-provider",
            "model": "fixture-model",
            "trials_per_fixture": 2,
        }
    )
    report["promotion_gate"]["checks"]["real_model_multi_trial"] = True
    report["promotion_gate"]["passed"] = promotion_passed
    report["promotion_gate"]["decision"] = (
        "eligible_for_fixture" if promotion_passed else "not_eligible_real_model_gate_failed"
    )
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _write_review_package(
    tmp_path: Path,
    *,
    promotion_passed: bool = True,
) -> tuple[Path, Path, Path]:
    report_path = _real_report(tmp_path / "b4-real.json", promotion_passed=promotion_passed)
    samples, annotations = build_b4_answer_quality_review(
        report_path,
        B4_FIXTURES,
        B4_SPLIT,
        RUBRIC,
    )
    samples_path = tmp_path / "samples.json"
    annotations_path = tmp_path / "annotations.json"
    samples_path.write_text(
        json.dumps(samples, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    annotations_path.write_text(
        json.dumps(annotations, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return report_path, samples_path, annotations_path


def _complete_annotations(path: Path) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["review"] = {
        "kind": "human_review",
        "reviewer_id": "fixture-reviewer",
        "completed_at": "2026-08-10T12:00:00+08:00",
    }
    for annotation in payload["annotations"]:
        annotation["scores"] = dict.fromkeys(ANSWER_QUALITY_AXES, 2)
        annotation["rationales"] = {
            axis: f"人工复核确认 {axis} 达到完整锚点。" for axis in ANSWER_QUALITY_AXES
        }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def test_export_builds_forward_hidden_offline_review_package(tmp_path: Path) -> None:
    report_path = _real_report(tmp_path / "b4-real.json")

    samples, annotations = build_b4_answer_quality_review(
        report_path,
        B4_FIXTURES,
        B4_SPLIT,
        RUBRIC,
    )

    assert samples["schema_version"] == 2
    assert samples["purpose"] == "candidate_quality"
    assert samples["evaluation_scope"] == "offline_fixed_fixture"
    assert samples["source_evaluation"]["model_kind"] == "real"
    assert samples["source_evaluation"]["score_split"] == "forward_hidden"
    assert samples["source_evaluation"]["real_model_multi_trial"] is True
    assert samples["source_evaluation"]["promotion_gate_passed"] is True
    assert [item["sample_id"] for item in samples["fixtures"]] == [
        "b4-evidence-interruption--b4-trial-1"
    ]
    sample = samples["fixtures"][0]
    assert sample["candidate"]["answer"] == "补充回执确认连接关闭异常来自适配器路径。"
    assert sample["context"]["evidence"][0]["evidence_id"] == "receipt:logs"
    assert annotations["review"]["kind"] == "pending_human_review"
    assert annotations["schema_version"] == 2
    assert annotations["fixture_revision"].startswith("nbtriage-answer-quality-fixtures-sha256:")
    assert annotations["annotations"][0]["scores"] == dict.fromkeys(ANSWER_QUALITY_AXES)

    serialized = json.dumps((samples, annotations), ensure_ascii=False)
    assert "GOLD-" not in serialized
    assert "leakage_marker" not in serialized
    assert "content_sha256" not in serialized
    assert "correlation_id" not in serialized
    assert "prompt_payload" not in serialized
    assert "chain_of_thought" not in serialized


def test_export_ignores_terminal_step_failures_and_keeps_completed_only(
    tmp_path: Path,
) -> None:
    report_path = _real_report(tmp_path / "b4-real.json")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    completed = next(
        trial
        for trial in report["trials"]
        if trial["split"] == "forward_hidden" and trial["status"] == "completed"
    )
    failed = json.loads(json.dumps(completed))
    failed["trial"] = 999
    failed["status"] = "stopped"
    failed["stop_reason"] = "model_error"
    failed["structured_output_valid"] = False
    failed["terminal_step_failure"] = {
        "category": "local_step_error",
        "rejection_reason": None,
        "provider_failure_reason": None,
        "provider_http_status": None,
        "usage": None,
        "provider_request_id": None,
        "provider_name": None,
        "provider_model_name": None,
        "provider_fingerprint": None,
        "latency_ms": 1,
    }
    report["trials"].append(failed)
    report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")

    samples, _ = build_b4_answer_quality_review(
        report_path,
        B4_FIXTURES,
        B4_SPLIT,
        RUBRIC,
    )

    assert [item["sample_id"] for item in samples["fixtures"]] == [
        "b4-evidence-interruption--b4-trial-1"
    ]


def test_pending_review_template_cannot_be_scored(tmp_path: Path) -> None:
    _, samples_path, annotations_path = _write_review_package(tmp_path)

    with pytest.raises(AnswerQualityEvaluationError, match="review kind is not supported"):
        evaluate_answer_quality(RUBRIC, samples_path, annotations_path)


def test_completed_review_is_only_offline_fixed_fixture_evidence(tmp_path: Path) -> None:
    report_path, samples_path, annotations_path = _write_review_package(tmp_path)
    _complete_annotations(annotations_path)

    report = evaluate_answer_quality(
        RUBRIC,
        samples_path,
        annotations_path,
        source_report_path=report_path,
    )

    assert report["evaluation_scope"] == "offline_fixed_fixture"
    assert report["summary"]["human_reviewed"] is True
    assert report["quality_claim_gate"]["eligible"] is True
    assert (
        report["quality_claim_gate"]["decision"]
        == "eligible_as_offline_fixed_fixture_human_evidence"
    )
    assert any("deployed Bot" in item for item in report["limitations"])


def test_completed_review_requires_the_original_source_report(tmp_path: Path) -> None:
    source_report_path, samples_path, annotations_path = _write_review_package(tmp_path)
    _complete_annotations(annotations_path)

    with pytest.raises(AnswerQualityEvaluationError, match="requires its source B4 report"):
        evaluate_answer_quality(RUBRIC, samples_path, annotations_path)

    source_report = json.loads(source_report_path.read_text(encoding="utf-8"))
    source_report["trials"][0]["fixture_id"] = "changed-after-review-export"
    source_report_path.write_text(json.dumps(source_report), encoding="utf-8")
    with pytest.raises(AnswerQualityEvaluationError, match="does not match its digest"):
        evaluate_answer_quality(
            RUBRIC,
            samples_path,
            annotations_path,
            source_report_path=source_report_path,
        )


def test_human_scores_cannot_override_failed_source_b4_gate(tmp_path: Path) -> None:
    report_path, samples_path, annotations_path = _write_review_package(
        tmp_path,
        promotion_passed=False,
    )
    _complete_annotations(annotations_path)

    report = evaluate_answer_quality(
        RUBRIC,
        samples_path,
        annotations_path,
        source_report_path=report_path,
    )

    assert report["quality_claim_gate"]["eligible"] is False
    assert report["quality_claim_gate"]["decision"] == "not_eligible_source_b4_gate_failed"


def test_export_rejects_scripted_or_mismatched_source(tmp_path: Path) -> None:
    scripted = asyncio.run(evaluate_b4_scripted_fixtures(B4_FIXTURES, B4_SPLIT))
    scripted_path = tmp_path / "scripted.json"
    scripted_path.write_text(json.dumps(scripted), encoding="utf-8")
    with pytest.raises(AnswerReviewExportError, match="real B4 report"):
        build_b4_answer_quality_review(scripted_path, B4_FIXTURES, B4_SPLIT, RUBRIC)

    real_path = _real_report(tmp_path / "real.json")
    tampered = json.loads(B4_FIXTURES.read_text(encoding="utf-8"))
    tampered["fixtures"][0]["case"]["source"]["title"] = "tampered"
    tampered_path = tmp_path / "tampered-fixtures.json"
    tampered_path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(AnswerReviewExportError, match="different fixtures"):
        build_b4_answer_quality_review(real_path, tampered_path, B4_SPLIT, RUBRIC)


def test_export_cli_writes_new_local_package_without_overwrite(tmp_path: Path) -> None:
    report_path = _real_report(tmp_path / "b4-real.json")
    output_dir = tmp_path / "review"
    arguments = [
        "export-answer-quality-review",
        "--evaluation-report",
        str(report_path),
        "--fixtures",
        str(B4_FIXTURES),
        "--split",
        str(B4_SPLIT),
        "--rubric",
        str(RUBRIC),
        "--output-dir",
        str(output_dir),
    ]

    assert main(arguments) == 0
    samples_path = output_dir / "samples.json"
    annotations_path = output_dir / "annotations.draft.json"
    assert samples_path.exists()
    assert annotations_path.exists()
    preserved = samples_path.read_bytes()

    assert main(arguments) == 1
    assert samples_path.read_bytes() == preserved


def test_candidate_quality_cli_never_overwrites_human_result(tmp_path: Path) -> None:
    source_report_path, samples_path, annotations_path = _write_review_package(tmp_path)
    _complete_annotations(annotations_path)
    quality_report_path = tmp_path / "quality-report.json"
    arguments = [
        "evaluate-answer-quality",
        "--rubric",
        str(RUBRIC),
        "--fixtures",
        str(samples_path),
        "--annotations",
        str(annotations_path),
        "--source-report",
        str(source_report_path),
        "--report",
        str(quality_report_path),
    ]

    assert main(arguments) == 0
    preserved = quality_report_path.read_bytes()
    assert main(arguments) == 1
    assert quality_report_path.read_bytes() == preserved
