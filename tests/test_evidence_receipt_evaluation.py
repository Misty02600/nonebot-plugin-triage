from pathlib import Path

from tools.nbtriage_maintainer.evidence_receipt_evaluation import evaluate_b3_evidence_receipts

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "evals" / "datasets" / "fixtures" / "b3-evidence-receipts-v1.json"


def test_evidence_receipt_evaluation_matches_the_versioned_fixture_contract() -> None:
    report = evaluate_b3_evidence_receipts(FIXTURES)

    assert report["summary"]["synthetic_only"] is True
    assert report["summary"]["expected_valid"] == 9
    assert report["summary"]["expected_invalid"] == 7
    assert report["summary"]["model_calls"] == 0
    assert report["summary"]["external_tool_calls"] == 0
    assert report["metrics"] == {
        "decision_accuracy": 1.0,
        "valid_accept_rate": 1.0,
        "invalid_reject_rate": 1.0,
    }
    assert len(report["predictions"]) == 16
