import asyncio
import json
from pathlib import Path

from tools.nbtriage_maintainer.cli import main
from tools.nbtriage_maintainer.safety_evaluation import (
    S3_EVALUATION_ID,
    S3_OFFICIAL_CASE_COUNT,
    S3_OFFICIAL_FIXTURE_SHA256,
    evaluate_s3,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURES_PATH = ROOT / "evals" / "datasets" / "fixtures" / "s3-adversarial-v1.json"


def test_s3_evaluation_matches_the_versioned_fixture_contract() -> None:
    report = asyncio.run(evaluate_s3(FIXTURES_PATH))

    assert report["evaluation_id"] == S3_EVALUATION_ID
    assert report["evaluation_qualification"] == "official"
    assert report["fixture"]["sha256"] == S3_OFFICIAL_FIXTURE_SHA256
    assert report["fixture"]["official_case_count"] == S3_OFFICIAL_CASE_COUNT
    assert report["summary"]["model_calls"] == 0
    assert report["summary"]["external_tool_calls"] == 0
    assert report["quality_gate"]["status"] == "passed"


def test_s3_fixture_set_is_intercepted_before_model_calls() -> None:
    report = asyncio.run(evaluate_s3(FIXTURES_PATH))

    assert report["summary"] == {
        "case_count": 6,
        "synthetic_only": True,
        "model_calls": 0,
        "external_tool_calls": 0,
    }
    assert report["metrics"]["b0_frozen"]["route_accuracy"] == 0.166667
    assert report["metrics"]["b1_pre_model_guard"] == {
        "route_accuracy": 1.0,
        "risk_exact_match": 1.0,
    }
    assert all(
        row["b1_pre_model_guard"]["retrieved_evidence_count"] == 0 for row in report["predictions"]
    )


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
