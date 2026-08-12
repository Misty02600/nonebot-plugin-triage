import json
from pathlib import Path

import pytest
from tools.nbtriage_maintainer.runtime_results import (
    assess_runtime_result,
    case_oracle_revision,
    probe_file_sha256,
)

ROOT = Path(__file__).resolve().parents[1]


def _executable_case() -> dict:
    return {
        "schema_version": 1,
        "case_id": "case-1",
        "curation": {
            "oracle": {
                "buggy_ref": "buggy",
                "fixed_ref": "fixed",
                "failure_signature": "target failure",
                "success_assertion": "successful exit",
            }
        },
    }


def _runtime_result(status: str, tmp_path: Path) -> dict:
    probe_source = "probe.py"
    (tmp_path / probe_source).write_text("assert True\n", encoding="utf-8")
    case = _executable_case()
    return {
        "case_id": "case-1",
        "status": status,
        "probe_id": "probe-1",
        "probe_source": probe_source,
        "probe_source_sha256": probe_file_sha256(tmp_path, probe_source),
        "case_oracle_revision": case_oracle_revision(case),
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
    tmp_path: Path,
    status: str,
    extra_fields: dict[str, str],
    error: str,
) -> None:
    result = {**_runtime_result(status, tmp_path), **extra_fields}

    assessment = assess_runtime_result(result, _executable_case(), probe_root=tmp_path)

    assert assessment.decision == "invalid"
    assert error in assessment.errors


def test_versioned_runtime_results_match_annotation_oracles() -> None:
    annotation_oracles = {}
    annotation_cases = {}
    for path in sorted((ROOT / "evals" / "curation" / "annotations").glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        for item in payload["annotations"]:
            oracle = item["curation"].get("oracle", {})
            if oracle.get("buggy_ref") and oracle.get("fixed_ref"):
                annotation_oracles[item["case_id"]] = oracle
                annotation_cases[item["case_id"]] = {
                    "schema_version": 1,
                    "case_id": item["case_id"],
                    "curation": item["curation"],
                }

    seen_case_ids = set()
    for path in sorted((ROOT / "evals" / "oracles").glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["schema_version"] == 2
        for result in payload["results"]:
            case_id = result["case_id"]
            assert case_id not in seen_case_ids
            seen_case_ids.add(case_id)
            assert case_id in annotation_oracles
            assert result["buggy_ref"] == annotation_oracles[case_id]["buggy_ref"]
            assert result["fixed_ref"] == annotation_oracles[case_id]["fixed_ref"]
            assert result["case_oracle_revision"] == case_oracle_revision(annotation_cases[case_id])
            if result["status"] == "validated":
                assert result["buggy_oracle_matched"] is True
                assert result["fixed_oracle_matched"] is True
                assert result["buggy_observation"]
                assert result["fixed_observation"]
                probe_path = ROOT / result["probe_source"]
                assert probe_path.is_file()
                assert result["probe_source_sha256"] == probe_file_sha256(
                    ROOT, result["probe_source"]
                )


def test_runtime_result_is_bound_to_failure_signature(tmp_path: Path) -> None:
    case = _executable_case()
    result = _runtime_result("validated", tmp_path)
    case["curation"]["oracle"]["failure_signature"] = "different failure"

    assessment = assess_runtime_result(result, case, probe_root=tmp_path)

    assert assessment.decision == "invalid"
    assert "case_oracle_revision does not match SupportCase content" in assessment.errors


def test_runtime_result_is_bound_to_probe_bytes(tmp_path: Path) -> None:
    result = _runtime_result("validated", tmp_path)
    (tmp_path / result["probe_source"]).write_text("assert False\n", encoding="utf-8")

    assessment = assess_runtime_result(result, _executable_case(), probe_root=tmp_path)

    assert assessment.decision == "invalid"
    assert "probe_source_sha256 does not match probe_source bytes" in assessment.errors


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        (
            "case_oracle_revision",
            None,
            "case_oracle_revision must be a lowercase SHA-256 digest",
        ),
        (
            "case_oracle_revision",
            "0" * 64,
            "case_oracle_revision does not match SupportCase content",
        ),
        (
            "probe_source_sha256",
            None,
            "probe_source_sha256 is required for validated result",
        ),
        (
            "probe_source_sha256",
            "0" * 64,
            "probe_source_sha256 does not match probe_source bytes",
        ),
    ],
)
def test_runtime_result_rejects_missing_or_forged_revisions(
    tmp_path: Path,
    field: str,
    value: str | None,
    error: str,
) -> None:
    result = _runtime_result("validated", tmp_path)
    result[field] = value

    assessment = assess_runtime_result(result, _executable_case(), probe_root=tmp_path)

    assert assessment.decision == "invalid"
    assert error in assessment.errors
