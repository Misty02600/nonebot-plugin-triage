import json
from pathlib import Path

from tools.nbtriage_maintainer.cli import main
from tools.nbtriage_maintainer.evidence_receipt_evaluation import (
    B3_EVIDENCE_RECEIPT_CUSTOM_EVALUATION_ID,
    B3_EVIDENCE_RECEIPT_EVALUATION_ID,
    B3_EVIDENCE_RECEIPT_OFFICIAL_CASE_COUNT,
    B3_EVIDENCE_RECEIPT_OFFICIAL_FIXTURE_SHA256,
    evaluate_b3_evidence_receipts,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "evals" / "datasets" / "fixtures" / "b3-evidence-receipts-v1.json"


def test_evidence_receipt_evaluation_matches_the_versioned_fixture_contract() -> None:
    report = evaluate_b3_evidence_receipts(FIXTURES)

    assert report["evaluation_id"] == B3_EVIDENCE_RECEIPT_EVALUATION_ID
    assert report["evaluation_qualification"] == "official_frozen_fixture"
    assert report["source"] == {
        "fixtures_path": FIXTURES.as_posix(),
        "fixtures_sha256": B3_EVIDENCE_RECEIPT_OFFICIAL_FIXTURE_SHA256,
        "official_fixtures_sha256": B3_EVIDENCE_RECEIPT_OFFICIAL_FIXTURE_SHA256,
        "official_fixture_set_id": B3_EVIDENCE_RECEIPT_EVALUATION_ID,
        "official_case_count": B3_EVIDENCE_RECEIPT_OFFICIAL_CASE_COUNT,
    }
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
    assert report["quality_gate"]["status"] == "passed"
    assert all(report["quality_gate"]["checks"].values())
    assert len(report["predictions"]) == 16


def test_evidence_receipt_evaluation_downgrades_semantically_identical_custom_bytes(
    tmp_path: Path,
) -> None:
    custom_path = tmp_path / "custom.json"
    payload = json.loads(FIXTURES.read_text(encoding="utf-8"))
    custom_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    report = evaluate_b3_evidence_receipts(custom_path)

    assert report["fixture_set_id"] == B3_EVIDENCE_RECEIPT_EVALUATION_ID
    assert report["summary"]["case_count"] == B3_EVIDENCE_RECEIPT_OFFICIAL_CASE_COUNT
    assert report["metrics"]["decision_accuracy"] == 1.0
    assert report["source"]["fixtures_sha256"] != B3_EVIDENCE_RECEIPT_OFFICIAL_FIXTURE_SHA256
    assert report["evaluation_id"] == B3_EVIDENCE_RECEIPT_CUSTOM_EVALUATION_ID
    assert report["evaluation_qualification"] == "custom_unqualified"
    assert report["quality_gate"]["status"] == "unqualified"
    assert report["quality_gate"]["checks"]["official_fixture_contract"] is False


def test_evidence_receipt_evaluation_downgrades_tampered_expectation(
    tmp_path: Path,
) -> None:
    tampered_path = tmp_path / "tampered.json"
    payload = json.loads(FIXTURES.read_text(encoding="utf-8"))
    payload["fixtures"][0]["expected_decision"] = "rejected"
    tampered_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    report = evaluate_b3_evidence_receipts(tampered_path)

    assert report["fixture_set_id"] == B3_EVIDENCE_RECEIPT_EVALUATION_ID
    assert report["summary"]["case_count"] == B3_EVIDENCE_RECEIPT_OFFICIAL_CASE_COUNT
    assert report["metrics"]["decision_accuracy"] < 1.0
    assert report["evaluation_id"] == B3_EVIDENCE_RECEIPT_CUSTOM_EVALUATION_ID
    assert report["evaluation_qualification"] == "custom_unqualified"
    assert report["quality_gate"]["status"] == "unqualified"
    assert report["quality_gate"]["checks"]["official_fixture_contract"] is False


def test_evidence_receipt_cli_returns_nonzero_for_custom_fixture_bytes(
    tmp_path: Path,
) -> None:
    custom_path = tmp_path / "custom.json"
    report_path = tmp_path / "report.json"
    payload = json.loads(FIXTURES.read_text(encoding="utf-8"))
    custom_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    exit_code = main(
        [
            "evaluate-b3-evidence-receipts",
            "--fixtures",
            str(custom_path),
            "--report",
            str(report_path),
        ]
    )

    assert exit_code == 1
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["evaluation_id"] == B3_EVIDENCE_RECEIPT_CUSTOM_EVALUATION_ID
    assert report["evaluation_qualification"] == "custom_unqualified"
    assert report["quality_gate"]["status"] == "unqualified"
