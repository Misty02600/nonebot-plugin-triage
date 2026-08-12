import json
from pathlib import Path

import pytest
from tools.nbtriage_maintainer.runtime_results import assess_runtime_result

ROOT = Path(__file__).resolve().parents[1]


def _executable_case() -> dict:
    return {
        "case_id": "case-1",
        "curation": {"oracle": {"buggy_ref": "buggy", "fixed_ref": "fixed"}},
    }


def _runtime_result(status: str) -> dict:
    return {
        "case_id": "case-1",
        "status": status,
        "probe_id": "probe-1",
        "buggy_ref": "buggy",
        "fixed_ref": "fixed",
        "buggy_oracle_matched": True,
        "fixed_oracle_matched": True,
        "buggy_observation": "target failure",
        "fixed_observation": "successful exit",
    }


@pytest.mark.parametrize(
    ("status", "extra_fields", "error"),
    [
        (
            "validated",
            {"failure_reason": "unexpected failure"},
            "validated result cannot contain failure or blocking reasons",
        ),
        (
            "validated",
            {"blocking_reason": "unexpected block", "required_runner": "runner"},
            "validated result cannot contain failure or blocking reasons",
        ),
        (
            "failed",
            {
                "failure_reason": "probe failed",
                "blocking_reason": "unexpected block",
                "required_runner": "runner",
            },
            "failed result cannot contain blocking fields",
        ),
        (
            "blocked",
            {
                "blocking_reason": "runner unavailable",
                "required_runner": "runner",
                "failure_reason": "unexpected failure",
            },
            "blocked result cannot contain failure_reason",
        ),
    ],
)
def test_runtime_result_rejects_mutually_exclusive_reason_fields(
    status: str,
    extra_fields: dict[str, str],
    error: str,
) -> None:
    result = {**_runtime_result(status), **extra_fields}

    assessment = assess_runtime_result(result, _executable_case())

    assert assessment.decision == "invalid"
    assert error in assessment.errors


def test_versioned_runtime_results_match_annotation_oracles() -> None:
    annotation_oracles = {}
    for path in sorted((ROOT / "evals" / "curation" / "annotations").glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        for item in payload["annotations"]:
            oracle = item["curation"].get("oracle", {})
            if oracle.get("buggy_ref") and oracle.get("fixed_ref"):
                annotation_oracles[item["case_id"]] = oracle

    seen_case_ids = set()
    for path in sorted((ROOT / "evals" / "oracles").glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["schema_version"] == 1
        for result in payload["results"]:
            case_id = result["case_id"]
            assert case_id not in seen_case_ids
            seen_case_ids.add(case_id)
            assert case_id in annotation_oracles
            assert result["buggy_ref"] == annotation_oracles[case_id]["buggy_ref"]
            assert result["fixed_ref"] == annotation_oracles[case_id]["fixed_ref"]
            if result["status"] == "validated":
                assert result["buggy_oracle_matched"] is True
                assert result["fixed_oracle_matched"] is True
                assert result["buggy_observation"]
                assert result["fixed_observation"]
                probe_path = ROOT / result["probe_source"]
                assert probe_path.is_file()
