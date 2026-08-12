import json
from pathlib import Path
from typing import Any

from tools.nbtriage_maintainer.gate import assess_case, evaluate_cases


def complete_case(mode: str = "sandbox_exec") -> dict[str, Any]:
    return {
        "schema_version": 1,
        "case_id": "gh-owner-repo-1",
        "visibility_boundary": "2025-01-01T00:00:00Z",
        "source": {"issue_url": "https://github.com/owner/repo/issues/1"},
        "curation": {
            "field_provenance": {
                "support_level": ["curator.inference"],
                "execution_mode": ["curator.inference"],
                "root_cause_cluster": ["gold.comment.1"],
                "environment": ["source.body"],
                "versions": ["source.body"],
                "deployment_topology": ["source.body"],
                "observed_behavior": ["source.body"],
                "expected_behavior": ["source.body"],
                "reproduction_steps": ["source.body"],
                "fault_phase": ["source.body"],
                "symptoms": ["source.body"],
                "candidate_owners": ["curator.inference"],
                "required_evidence_gaps": ["curator.inference"],
                "unknowns": ["curator.inference"],
                "oracle.buggy_ref": ["gold.commit.buggy"],
                "oracle.fixed_ref": ["gold.commit.fixed"],
                "oracle.failure_signature": ["source.body"],
                "oracle.success_assertion": ["curator.inference"],
            },
            "support_level": "s1_verify",
            "execution_mode": mode,
            "root_cause_cluster": "cluster-1",
            "environment": {"os": "linux"},
            "versions": {"python": "3.12"},
            "deployment_topology": "local uv virtual environment",
            "observed_behavior": "import raises a target exception",
            "expected_behavior": "plugin imports successfully",
            "reproduction_steps": ["install the frozen dependency set"],
            "fault_phase": "boot",
            "symptoms": ["exception"],
            "candidate_owners": ["plugin"],
            "required_evidence_gaps": [],
            "ruled_out": [],
            "unknowns": [],
            "escalation_target": None,
            "safety_or_scope_reason": None,
            "exclusion_reason": None,
            "oracle": {
                "buggy_ref": "v1.0.0",
                "fixed_ref": "v1.0.1",
                "failure_signature": "TargetException",
                "success_assertion": "process exits with code 0",
            },
        },
    }


def test_executable_case_is_ready_only_with_full_oracle() -> None:
    assessment = assess_case(complete_case())

    assert assessment.decision == "ready_for_execution"
    assert assessment.missing_fields == []
    assert assessment.invalid_fields == []


def test_missing_executable_oracle_is_reported() -> None:
    case = complete_case()
    case["curation"]["oracle"]["fixed_ref"] = None
    case["curation"]["versions"] = {}

    assessment = assess_case(case)

    assert assessment.decision == "needs_curation"
    assert assessment.missing_fields == [
        "curation.oracle.fixed_ref",
        "curation.versions",
    ]


def test_diagnose_only_uses_evidence_gap_oracle() -> None:
    case = complete_case("diagnose_only")
    case["curation"]["support_level"] = "s2_diagnose"
    case["curation"]["required_evidence_gaps"] = ["protocol implementation logs"]
    case["curation"]["unknowns"] = ["which side closed the websocket"]
    case["curation"]["reproduction_steps"] = []
    case["curation"]["oracle"] = {
        "buggy_ref": None,
        "fixed_ref": None,
        "failure_signature": None,
        "success_assertion": None,
    }

    assessment = assess_case(case)

    assert assessment.decision == "ready_non_executable"
    assert assessment.missing_fields == []


def test_invalid_taxonomy_value_is_not_ready() -> None:
    case = complete_case()
    case["curation"]["candidate_owners"] = ["unknown-layer"]

    assessment = assess_case(case)

    assert assessment.decision == "needs_curation"
    assert assessment.invalid_fields == ["curation.candidate_owners"]


def test_evaluate_cases_summarizes_decisions(tmp_path: Path) -> None:
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    (cases_dir / "ready.json").write_text(json.dumps(complete_case()), encoding="utf-8")
    incomplete = complete_case()
    incomplete["case_id"] = "gh-owner-repo-2"
    incomplete["curation"]["execution_mode"] = None
    (cases_dir / "incomplete.json").write_text(json.dumps(incomplete), encoding="utf-8")

    report = evaluate_cases(cases_dir)

    assert report["summary"]["assessed_cases"] == 2
    assert report["summary"]["unique_assessed_cases"] == 2
    assert report["summary"]["duplicate_case_ids"] == 0
    assert report["summary"]["ready_for_execution"] == 1
    assert report["summary"]["needs_curation"] == 1
    assert report["summary"]["executable_spec_gate_met"] is False
    assert report["summary"]["runtime_validated"] == 0
    assert report["missing_field_frequency"]["curation.execution_mode"] == 1


def test_duplicate_case_ids_cannot_meet_either_gate(tmp_path: Path) -> None:
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    for index in range(15):
        (cases_dir / f"case-{index:02d}.json").write_text(
            json.dumps(complete_case()),
            encoding="utf-8",
        )
    runtime_file = tmp_path / "runtime.json"
    runtime_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "results": [
                    {
                        "case_id": "gh-owner-repo-1",
                        "status": "validated",
                        "probe_id": "probe-1",
                        "buggy_ref": "v1.0.0",
                        "fixed_ref": "v1.0.1",
                        "buggy_oracle_matched": True,
                        "fixed_oracle_matched": True,
                        "buggy_observation": "TargetException was raised",
                        "fixed_observation": "process exited with code 0",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    report = evaluate_cases(cases_dir, runtime_file)

    assert report["summary"]["total_case_files"] == 15
    assert report["summary"]["assessed_cases"] == 0
    assert report["summary"]["unique_assessed_cases"] == 0
    assert report["summary"]["duplicate_case_ids"] == 1
    assert report["summary"]["case_load_errors"] == 15
    assert report["summary"]["ready_for_execution"] == 0
    assert report["summary"]["executable_spec_gate_met"] is False
    assert report["summary"]["runtime_validated"] == 0
    assert report["summary"]["runtime_invalid"] == 1
    assert report["summary"]["runtime_gate_met"] is False
    assert {item["error"] for item in report["load_errors"]} == {"duplicate case_id"}


def test_mixed_duplicate_group_is_fully_rejected(tmp_path: Path) -> None:
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    ready = complete_case()
    incomplete = complete_case()
    incomplete["case_id"] = "  gh-owner-repo-1  "
    incomplete["curation"]["execution_mode"] = None
    excluded = complete_case()
    excluded["curation"]["exclusion_reason"] = "not reproducible"
    for filename, payload in (
        ("ready.json", ready),
        ("incomplete.json", incomplete),
        ("excluded.json", excluded),
    ):
        (cases_dir / filename).write_text(json.dumps(payload), encoding="utf-8")

    report = evaluate_cases(cases_dir)

    assert report["summary"]["assessed_cases"] == 0
    assert report["summary"]["duplicate_case_ids"] == 1
    assert report["summary"]["case_load_errors"] == 3
    assert report["summary"]["ready_for_execution"] == 0
    assert report["summary"]["needs_curation"] == 0
    assert report["summary"]["excluded"] == 0
    assert report["assessments"] == []


def test_missing_case_ids_are_assessed_independently(tmp_path: Path) -> None:
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    for index in range(2):
        payload = complete_case()
        payload.pop("case_id")
        (cases_dir / f"missing-{index}.json").write_text(
            json.dumps(payload),
            encoding="utf-8",
        )

    report = evaluate_cases(cases_dir)

    assert report["summary"]["assessed_cases"] == 2
    assert report["summary"]["unique_assessed_cases"] == 0
    assert report["summary"]["duplicate_case_ids"] == 0
    assert report["summary"]["case_load_errors"] == 0
    assert report["summary"]["needs_curation"] == 2


def test_evaluate_cases_counts_matching_runtime_oracle(tmp_path: Path) -> None:
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    (cases_dir / "ready.json").write_text(json.dumps(complete_case()), encoding="utf-8")
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    (runtime_dir / "batch.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "results": [
                    {
                        "case_id": "gh-owner-repo-1",
                        "status": "validated",
                        "probe_id": "probe-1",
                        "buggy_ref": "v1.0.0",
                        "fixed_ref": "v1.0.1",
                        "buggy_oracle_matched": True,
                        "fixed_oracle_matched": True,
                        "buggy_observation": "TargetException was raised",
                        "fixed_observation": "process exited with code 0",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    report = evaluate_cases(cases_dir, runtime_dir)

    assert report["summary"]["runtime_validated"] == 1
    assert report["summary"]["runtime_invalid"] == 0
    assert report["runtime_assessments"][0]["decision"] == "validated"


def test_runtime_oracle_ref_must_match_case(tmp_path: Path) -> None:
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    (cases_dir / "ready.json").write_text(json.dumps(complete_case()), encoding="utf-8")
    runtime_file = tmp_path / "runtime.json"
    runtime_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "results": [
                    {
                        "case_id": "gh-owner-repo-1",
                        "status": "blocked",
                        "probe_id": "probe-1",
                        "buggy_ref": "wrong-ref",
                        "fixed_ref": "v1.0.1",
                        "blocking_reason": "Linux runner is unavailable",
                        "required_runner": "Linux container",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    report = evaluate_cases(cases_dir, runtime_file)

    assert report["summary"]["runtime_invalid"] == 1
    assert report["summary"]["runtime_blocked"] == 0
    assert report["runtime_assessments"][0]["errors"] == [
        "buggy_ref does not match SupportCase Oracle"
    ]
