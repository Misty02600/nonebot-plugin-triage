import asyncio
from pathlib import Path

from tools.nbtriage_maintainer.safety_evaluation import evaluate_s3

ROOT = Path(__file__).resolve().parents[1]
FIXTURES_PATH = ROOT / "evals" / "datasets" / "fixtures" / "s3-adversarial-v1.json"


def test_s3_evaluation_matches_the_versioned_fixture_contract() -> None:
    report = asyncio.run(evaluate_s3(FIXTURES_PATH))

    assert report["fixture_set_id"] == "s3-adversarial-v1"
    assert report["summary"]["synthetic_only"] is True
    assert report["summary"]["model_calls"] == 0
    assert report["summary"]["external_tool_calls"] == 0
    assert report["metrics"]["b1_pre_model_guard"] == {
        "route_accuracy": 1.0,
        "risk_exact_match": 1.0,
    }
    assert len(report["predictions"]) == 6
