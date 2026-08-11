import json
from pathlib import Path

import pytest
from tools.nbtriage_maintainer.cli import main
from tools.nbtriage_maintainer.evidence_policy import EvidencePolicyError, select_next_evidence
from tools.nbtriage_maintainer.evidence_policy_evaluation import (
    EvidencePolicyEvaluationError,
    evaluate_b3_evidence_policy,
)

ROOT = Path(__file__).resolve().parents[1]
VALIDATION_FIXTURE = (
    ROOT / "evals" / "datasets" / "fixtures" / "b3-evidence-policy-validation-v1.json"
)


def test_policy_selects_one_candidate_by_fault_phase() -> None:
    selected = select_next_evidence(
        "handle",
        ["logs", "configuration", "reproduction_steps"],
    )

    assert selected == "reproduction_steps"


def test_policy_never_invents_or_accepts_invalid_candidates() -> None:
    assert select_next_evidence("boot", []) is None

    with pytest.raises(EvidencePolicyError, match="unsupported evidence"):
        select_next_evidence("boot", ["private_token"])
    with pytest.raises(EvidencePolicyError, match="must be unique"):
        select_next_evidence("boot", ["logs", "logs"])
    with pytest.raises(EvidencePolicyError, match="unsupported fault phase"):
        select_next_evidence("unknown", ["logs"])


def test_validation_policy_report_records_precision_tradeoff_without_calls() -> None:
    report = evaluate_b3_evidence_policy(VALIDATION_FIXTURE)

    assert report["summary"] == {
        "case_count": 11,
        "policy_id": "b3-single-evidence-v1",
        "needs_evidence_action_count": 8,
        "proposed_question_count": 8,
        "model_calls": 0,
        "external_tool_calls": 0,
    }
    assert report["metrics"]["b1_missing_evidence_micro"]["precision"] == 0.30303
    assert report["metrics"]["b3_selected_evidence_micro"] == {
        "true_positive": 6,
        "false_positive": 2,
        "false_negative": 12,
        "precision": 0.75,
        "recall": 0.333333,
        "f1": 0.461538,
    }
    assert report["metrics"]["question_precision_at_1"]["rate"] == 0.75
    assert report["metrics"]["gold_gap_case_hit_at_1"]["rate"] == 0.75
    assert report["metrics"]["question_load"] == {
        "b1_candidate_questions": 33,
        "b3_selected_questions": 8,
        "b1_average_per_needs_evidence_action": 4.125,
        "b3_average_per_needs_evidence_action": 1.0,
    }


def test_policy_evaluation_rejects_consumed_heldout_report(tmp_path: Path) -> None:
    payload = json.loads(VALIDATION_FIXTURE.read_text(encoding="utf-8"))
    payload["summary"]["score_splits"] = ["heldout"]
    for row in payload["predictions"]:
        row["split"] = "heldout"
    heldout_report = tmp_path / "heldout.json"
    heldout_report.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(EvidencePolicyEvaluationError, match="validation-only"):
        evaluate_b3_evidence_policy(heldout_report)


def test_policy_fixture_excludes_provider_and_free_text_output() -> None:
    payload = json.loads(VALIDATION_FIXTURE.read_text(encoding="utf-8"))
    serialized = json.dumps(payload)

    assert payload["artifact_profile"] == "b3-evidence-policy-curated-fixture-v1"
    assert payload["contains_provider_metadata"] is False
    assert "provider_request_id" not in serialized
    assert "citations" not in serialized
    assert "answer" not in serialized


def test_evaluate_b3_policy_cli_writes_report(tmp_path: Path) -> None:
    report_path = tmp_path / "b3-policy.json"

    exit_code = main(
        [
            "evaluate-b3-evidence-policy",
            "--prediction-report",
            str(VALIDATION_FIXTURE),
            "--report",
            str(report_path),
        ]
    )

    assert exit_code == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["evaluation_id"] == "b3-single-evidence-v1"
    assert report["summary"]["model_calls"] == 0
