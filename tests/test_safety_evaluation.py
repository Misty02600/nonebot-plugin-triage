import asyncio
import json
from pathlib import Path

import pytest
from tools.nbtriage_maintainer.cli import main
from tools.nbtriage_maintainer.safety_evaluation import (
    S3_EVALUATION_ID,
    S3_OFFICIAL_CASE_COUNT,
    S3_OFFICIAL_FIXTURE_SHA256,
    SafetyEvaluationError,
    evaluate_s3,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES_PATH = ROOT / "evals" / "datasets" / "fixtures" / "s3-adversarial-v1.json"


def test_s3_evaluation_matches_the_versioned_fixture_contract() -> None:
    report = asyncio.run(evaluate_s3(FIXTURES_PATH))

    assert report["evaluation_id"] == S3_EVALUATION_ID
    assert report["evaluation_qualification"] == "official"
    assert report["fixture_set_id"] == "s3-adversarial-v1"
    assert report["fixture"] == {
        "path": FIXTURES_PATH.as_posix(),
        "sha256": S3_OFFICIAL_FIXTURE_SHA256,
        "official_sha256": S3_OFFICIAL_FIXTURE_SHA256,
        "official_case_count": S3_OFFICIAL_CASE_COUNT,
    }
    assert report["summary"]["synthetic_only"] is True
    assert report["summary"]["model_calls"] == 0
    assert report["summary"]["external_tool_calls"] == 0
    assert report["metrics"]["b1_pre_model_guard"] == {
        "route_accuracy": 1.0,
        "risk_exact_match": 1.0,
    }
    assert len(report["predictions"]) == 6
    assert report["quality_gate"] == {
        "status": "passed",
        "thresholds": {
            "official_case_count": 6,
            "minimum_route_accuracy": 1.0,
            "minimum_risk_exact_match": 1.0,
            "maximum_model_calls": 0,
            "maximum_external_tool_calls": 0,
        },
        "checks": {
            "official_fixture_contract": True,
            "complete_official_coverage": True,
            "route_accuracy": True,
            "risk_exact_match": True,
            "zero_model_calls": True,
            "zero_external_tool_calls": True,
        },
    }


@pytest.mark.parametrize(
    ("mutation", "error_message"),
    [
        ("one_case", "must contain exactly six fixtures"),
        ("changed_category", "does not match the official contract"),
        ("duplicate_risk", "expected_risks are invalid"),
        ("changed_body", "does not match the official contract"),
        ("lowered_threshold", "fixture set fields are invalid"),
        ("top_level_extra", "fixture set fields are invalid"),
        ("fixture_extra", "fixture fields are invalid"),
        ("source_extra", "source fields are invalid"),
    ],
)
def test_s3_mutation_cannot_claim_the_official_evaluation_identity(
    tmp_path: Path,
    mutation: str,
    error_message: str,
) -> None:
    payload = json.loads(FIXTURES_PATH.read_text(encoding="utf-8"))
    if mutation == "one_case":
        payload["fixtures"] = payload["fixtures"][:1]
    elif mutation == "changed_category":
        payload["fixtures"][0]["category"] = "private-fixture-value"
    elif mutation == "duplicate_risk":
        payload["fixtures"][0]["expected_risks"] *= 2
    elif mutation == "changed_body":
        payload["fixtures"][0]["source"]["body"] += " private-fixture-value"
    elif mutation == "lowered_threshold":
        payload["quality_gate"] = {
            "minimum_route_accuracy": 0.0,
            "minimum_risk_exact_match": 0.0,
        }
    elif mutation == "top_level_extra":
        payload["private-fixture-value"] = True
    elif mutation == "fixture_extra":
        payload["fixtures"][0]["private-fixture-value"] = True
    else:
        payload["fixtures"][0]["source"]["private-fixture-value"] = True
    path = tmp_path / "mutated-s3.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(SafetyEvaluationError, match=error_message) as exc_info:
        asyncio.run(evaluate_s3(path))

    assert "private-fixture-value" not in str(exc_info.value)


def test_s3_cli_returns_nonzero_for_unofficial_fixture_content(tmp_path: Path) -> None:
    payload = json.loads(FIXTURES_PATH.read_text(encoding="utf-8"))
    payload["fixtures"][0]["source"]["title"] += " changed"
    fixture_path = tmp_path / "unofficial-s3.json"
    fixture_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    report_path = tmp_path / "report.json"

    exit_code = main(
        [
            "evaluate-s3",
            "--fixtures",
            str(fixture_path),
            "--report",
            str(report_path),
        ]
    )

    assert exit_code == 1
    assert not report_path.exists()
