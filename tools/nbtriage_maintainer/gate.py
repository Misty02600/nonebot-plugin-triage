"""仓库维护者使用的评测质量门。"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from tools.nbtriage_maintainer.models import (
    ExecutionMode,
    FaultPhase,
    ResponsibilityLayer,
    SupportLevel,
    Symptom,
)
from tools.nbtriage_maintainer.runtime_results import evaluate_runtime_results

EXECUTABLE_MODES = {
    ExecutionMode.NONEBUG_EXEC.value,
    ExecutionMode.SANDBOX_EXEC.value,
    ExecutionMode.CONTRACT_EXEC.value,
}
NON_EXECUTABLE_MODES = {
    ExecutionMode.DIAGNOSE_ONLY.value,
    ExecutionMode.ESCALATE.value,
}


@dataclass(frozen=True)
class CaseAssessment:
    case_id: str
    source_url: str | None
    decision: str
    execution_mode: str | None
    missing_fields: list[str]
    invalid_fields: list[str]
    exclusion_reason: str | None = None


@dataclass(frozen=True)
class _LoadedCase:
    path: Path
    payload: dict[str, Any]
    assessment: CaseAssessment
    normalized_case_id: str | None


def assess_case(payload: dict[str, Any]) -> CaseAssessment:
    case_id = _string_at(payload, "case_id") or "<missing-case-id>"
    source_url = _string_at(payload, "source", "issue_url")
    curation = payload.get("curation")
    if not isinstance(curation, dict):
        return CaseAssessment(
            case_id,
            source_url,
            "needs_curation",
            None,
            ["curation"],
            [],
        )

    exclusion_reason = _non_empty_string(curation.get("exclusion_reason"))
    execution_mode = _non_empty_string(curation.get("execution_mode"))
    if exclusion_reason:
        return CaseAssessment(
            case_id,
            source_url,
            "excluded",
            execution_mode,
            [],
            [],
            exclusion_reason,
        )

    missing = _common_missing(payload, curation)
    invalid = _invalid_fields(curation)
    provenance_fields = [
        "support_level",
        "execution_mode",
        "root_cause_cluster",
        "environment",
        "versions",
        "deployment_topology",
        "observed_behavior",
        "expected_behavior",
        "fault_phase",
        "symptoms",
        "candidate_owners",
    ]
    if execution_mode in EXECUTABLE_MODES:
        missing.extend(_executable_missing(curation))
        provenance_fields.extend(
            [
                "reproduction_steps",
                "oracle.buggy_ref",
                "oracle.fixed_ref",
                "oracle.failure_signature",
                "oracle.success_assertion",
            ]
        )
    elif execution_mode == ExecutionMode.DIAGNOSE_ONLY.value:
        if not _non_empty_list(curation.get("required_evidence_gaps")):
            missing.append("curation.required_evidence_gaps")
        if not _non_empty_list(curation.get("unknowns")):
            missing.append("curation.unknowns")
        provenance_fields.extend(["required_evidence_gaps", "unknowns"])
    elif execution_mode == ExecutionMode.ESCALATE.value:
        if not _non_empty_string(curation.get("escalation_target")):
            missing.append("curation.escalation_target")
        if not _non_empty_string(curation.get("safety_or_scope_reason")):
            missing.append("curation.safety_or_scope_reason")
        provenance_fields.extend(["escalation_target", "safety_or_scope_reason"])

    missing.extend(_provenance_missing(curation, provenance_fields))

    missing = sorted(set(missing))
    invalid = sorted(set(invalid))
    if missing or invalid:
        decision = "needs_curation"
    elif execution_mode in EXECUTABLE_MODES:
        decision = "ready_for_execution"
    elif execution_mode in NON_EXECUTABLE_MODES:
        decision = "ready_non_executable"
    else:
        decision = "needs_curation"
    return CaseAssessment(
        case_id,
        source_url,
        decision,
        execution_mode,
        missing,
        invalid,
    )


def evaluate_cases(
    cases_dir: Path,
    runtime_results_path: Path | None = None,
    *,
    probe_root: Path | None = None,
) -> dict[str, Any]:
    case_paths = sorted(cases_dir.glob("*.json"))
    loaded_cases: list[_LoadedCase] = []
    load_errors: list[dict[str, str]] = []
    case_id_counts: Counter[str] = Counter()
    for path in case_paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("top-level JSON value must be an object")
            normalized_case_id = _string_at(payload, "case_id")
            loaded_cases.append(
                _LoadedCase(
                    path=path,
                    payload=payload,
                    assessment=assess_case(payload),
                    normalized_case_id=normalized_case_id,
                )
            )
            if normalized_case_id is not None:
                case_id_counts[normalized_case_id] += 1
        except (OSError, ValueError, json.JSONDecodeError) as error:
            load_errors.append({"path": str(path), "error": str(error)})

    duplicate_case_ids = {case_id for case_id, count in case_id_counts.items() if count > 1}
    assessments: list[CaseAssessment] = []
    cases_by_id: dict[str, dict[str, Any]] = {}
    for loaded_case in loaded_cases:
        if loaded_case.normalized_case_id in duplicate_case_ids:
            load_errors.append({"path": str(loaded_case.path), "error": "duplicate case_id"})
            continue
        assessments.append(loaded_case.assessment)
        if loaded_case.assessment.decision == "ready_for_execution":
            cases_by_id[loaded_case.assessment.case_id] = loaded_case.payload

    load_errors.sort(key=lambda item: (item["path"], item["error"]))

    runtime_assessments, runtime_load_errors = evaluate_runtime_results(
        runtime_results_path,
        cases_by_id,
        probe_root=probe_root,
    )
    decisions = Counter(item.decision for item in assessments)
    runtime_decisions = Counter(item.decision for item in runtime_assessments)
    missing = Counter(field for item in assessments for field in item.missing_fields)
    invalid = Counter(field for item in assessments for field in item.invalid_fields)
    return {
        "schema_version": 1,
        "summary": {
            "total_case_files": len(case_paths),
            "assessed_cases": len(assessments),
            "unique_assessed_cases": sum(
                loaded_case.normalized_case_id is not None
                and loaded_case.normalized_case_id not in duplicate_case_ids
                for loaded_case in loaded_cases
            ),
            "duplicate_case_ids": len(duplicate_case_ids),
            "load_errors": len(load_errors) + len(runtime_load_errors),
            "case_load_errors": len(load_errors),
            "runtime_load_errors": len(runtime_load_errors),
            "ready_for_execution": decisions["ready_for_execution"],
            "ready_non_executable": decisions["ready_non_executable"],
            "needs_curation": decisions["needs_curation"],
            "excluded": decisions["excluded"],
            "executable_spec_target": 15,
            "executable_spec_gate_met": decisions["ready_for_execution"] >= 15,
            "runtime_validated": runtime_decisions["validated"],
            "runtime_failed": runtime_decisions["failed"],
            "runtime_blocked": runtime_decisions["blocked"],
            "runtime_invalid": runtime_decisions["invalid"],
            "runtime_target": 15,
            "runtime_gate_met": runtime_decisions["validated"] >= 15,
        },
        "readiness_definition": (
            "ready_for_execution means the frozen candidate and Oracle specification are complete; "
            "it does not mean the Oracle has been run or validated"
        ),
        "missing_field_frequency": dict(missing.most_common()),
        "invalid_field_frequency": dict(invalid.most_common()),
        "assessments": [asdict(item) for item in assessments],
        "runtime_assessments": [asdict(item) for item in runtime_assessments],
        "load_errors": load_errors,
        "runtime_load_errors": runtime_load_errors,
    }


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(path)


def _common_missing(payload: dict[str, Any], curation: dict[str, Any]) -> list[str]:
    required_strings = [
        "support_level",
        "execution_mode",
        "root_cause_cluster",
        "deployment_topology",
        "observed_behavior",
        "expected_behavior",
        "fault_phase",
    ]
    missing = [
        f"curation.{key}" for key in required_strings if not _non_empty_string(curation.get(key))
    ]
    if payload.get("schema_version") != 1:
        missing.append("schema_version")
    for key in ("case_id", "visibility_boundary"):
        if not _non_empty_string(payload.get(key)):
            missing.append(key)
    for key in ("environment", "versions"):
        if not _non_empty_mapping(curation.get(key)):
            missing.append(f"curation.{key}")
    for key in ("symptoms", "candidate_owners"):
        if not _non_empty_list(curation.get(key)):
            missing.append(f"curation.{key}")
    return missing


def _executable_missing(curation: dict[str, Any]) -> list[str]:
    missing = []
    if not _non_empty_list(curation.get("reproduction_steps")):
        missing.append("curation.reproduction_steps")
    oracle = curation.get("oracle")
    if not isinstance(oracle, dict):
        return [*missing, "curation.oracle"]
    for key in ("buggy_ref", "fixed_ref", "failure_signature", "success_assertion"):
        if not _non_empty_string(oracle.get(key)):
            missing.append(f"curation.oracle.{key}")
    return missing


def _invalid_fields(curation: dict[str, Any]) -> list[str]:
    invalid = []
    valid_values = {
        "support_level": {item.value for item in SupportLevel},
        "execution_mode": {item.value for item in ExecutionMode},
        "fault_phase": {item.value for item in FaultPhase},
    }
    for key, allowed in valid_values.items():
        value = _non_empty_string(curation.get(key))
        if value and value not in allowed:
            invalid.append(f"curation.{key}")

    for key, enum_type in (
        ("symptoms", Symptom),
        ("candidate_owners", ResponsibilityLayer),
    ):
        values = curation.get(key)
        if isinstance(values, list):
            allowed = {item.value for item in enum_type}
            if any(not isinstance(value, str) or value not in allowed for value in values):
                invalid.append(f"curation.{key}")
    return invalid


def _provenance_missing(curation: dict[str, Any], field_paths: list[str]) -> list[str]:
    provenance = curation.get("field_provenance")
    if not isinstance(provenance, dict):
        return ["curation.field_provenance"]
    return [
        f"curation.field_provenance.{field_path}"
        for field_path in field_paths
        if not _non_empty_list(provenance.get(field_path))
    ]


def _string_at(payload: dict[str, Any], *path: str) -> str | None:
    current: Any = payload
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return _non_empty_string(current)


def _non_empty_string(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _non_empty_list(value: Any) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(item, str) and item.strip() for item in value)
    )


def _non_empty_mapping(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and bool(value)
        and all(
            isinstance(key, str) and key.strip() and isinstance(item, str) and item.strip()
            for key, item in value.items()
        )
    )
