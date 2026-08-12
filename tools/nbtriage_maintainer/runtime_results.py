"""仓库维护者使用的运行结果评估。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

RUNTIME_STATUSES = {"validated", "failed", "blocked"}
RUNTIME_RESULT_FIELDS = frozenset(
    {
        "decision",
        "probe_id",
        "buggy_ref",
        "fixed_ref",
        "blocking_reason",
        "failure_reason",
        "required_runner",
    }
)


@dataclass(frozen=True)
class RuntimeAssessment:
    case_id: str
    decision: str
    buggy_ref: str | None
    fixed_ref: str | None
    probe_id: str | None
    errors: list[str]
    blocking_reason: str | None = None
    failure_reason: str | None = None
    required_runner: str | None = None


def runtime_result_validation_error(result: Mapping[str, Any]) -> str | None:
    """验证 RuntimeAssessment 持久化稳定字段的结构和组合语义。

    Args:
        result: 只包含会话持久化所需七个稳定字段的映射。

    Returns:
        首个稳定验证错误；结构和字段组合合法时返回 ``None``。
    """
    if set(result) != RUNTIME_RESULT_FIELDS:
        return "fields do not match the runtime result schema"

    decision = result.get("decision")
    if not isinstance(decision, str) or decision not in RUNTIME_STATUSES:
        return "decision must be validated, failed, or blocked"
    for field in ("probe_id", "buggy_ref", "fixed_ref"):
        value = result.get(field)
        normalized = _non_empty_string(value)
        if normalized is None:
            return f"{field} is required"
        if value != normalized:
            return f"{field} must be a normalized string"

    blocking_reason = _non_empty_string(result.get("blocking_reason"))
    failure_reason = _non_empty_string(result.get("failure_reason"))
    required_runner = _non_empty_string(result.get("required_runner"))
    optional_values = {
        "blocking_reason": blocking_reason,
        "failure_reason": failure_reason,
        "required_runner": required_runner,
    }
    for field, normalized in optional_values.items():
        value = result.get(field)
        if value is not None and (not isinstance(value, str) or value != normalized):
            return f"{field} must be null or a non-empty normalized string"

    if decision == "validated":
        if any(value is not None for value in optional_values.values()):
            return "validated result cannot contain failure or blocking reasons"
    elif decision == "failed":
        if failure_reason is None:
            return "failure_reason is required for failed result"
        if blocking_reason is not None or required_runner is not None:
            return "failed result cannot contain blocking fields"
    else:
        if blocking_reason is None or required_runner is None:
            return "blocking_reason and required_runner are required for blocked result"
        if failure_reason is not None:
            return "blocked result cannot contain failure_reason"
    return None


def evaluate_runtime_results(
    results_path: Path | None,
    cases_by_id: dict[str, dict[str, Any]],
) -> tuple[list[RuntimeAssessment], list[dict[str, str]]]:
    """读取并核对版本化 Oracle 运行结果。

    运行结果只能证明已经策展的 Case，且其中的故障与修复引用必须和
    `SupportCase` 完全一致，避免用另一个提交或环境结果冒充目标 Oracle。

    Args:
        results_path: 单个结果文件或包含结果文件的目录；不存在时视为尚未运行。
        cases_by_id: 已达到可执行规格门槛的 Case，以 `case_id` 为键。

    Returns:
        逐 Case 的运行核对结果，以及文件级加载错误。
    """
    if results_path is None or not results_path.exists():
        return [], []

    paths = [results_path] if results_path.is_file() else sorted(results_path.glob("*.json"))
    assessments: list[RuntimeAssessment] = []
    load_errors: list[dict[str, str]] = []
    seen_case_ids: set[str] = set()

    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            results = _runtime_entries(payload)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            load_errors.append({"path": str(path), "error": str(error)})
            continue

        for result in results:
            case_id = _non_empty_string(result.get("case_id")) or "<missing-case-id>"
            if case_id in seen_case_ids:
                assessments.append(
                    RuntimeAssessment(
                        case_id=case_id,
                        decision="invalid",
                        buggy_ref=_non_empty_string(result.get("buggy_ref")),
                        fixed_ref=_non_empty_string(result.get("fixed_ref")),
                        probe_id=_non_empty_string(result.get("probe_id")),
                        errors=["duplicate runtime result for case_id"],
                    )
                )
                continue
            seen_case_ids.add(case_id)
            assessments.append(assess_runtime_result(result, cases_by_id.get(case_id)))

    return assessments, load_errors


def assess_runtime_result(
    result: dict[str, Any],
    case_payload: dict[str, Any] | None,
) -> RuntimeAssessment:
    case_id = _non_empty_string(result.get("case_id")) or "<missing-case-id>"
    status = _non_empty_string(result.get("status"))
    buggy_ref = _non_empty_string(result.get("buggy_ref"))
    fixed_ref = _non_empty_string(result.get("fixed_ref"))
    probe_id = _non_empty_string(result.get("probe_id"))
    blocking_reason = _non_empty_string(result.get("blocking_reason"))
    failure_reason = _non_empty_string(result.get("failure_reason"))
    required_runner = _non_empty_string(result.get("required_runner"))
    errors: list[str] = []

    if case_payload is None:
        errors.append("case_id does not match an executable-ready SupportCase")
    else:
        curation = case_payload.get("curation")
        oracle = curation.get("oracle") if isinstance(curation, dict) else None
        if not isinstance(oracle, dict):
            errors.append("loaded SupportCase has no Oracle")
        else:
            if buggy_ref != _non_empty_string(oracle.get("buggy_ref")):
                errors.append("buggy_ref does not match SupportCase Oracle")
            if fixed_ref != _non_empty_string(oracle.get("fixed_ref")):
                errors.append("fixed_ref does not match SupportCase Oracle")

    stable_result = {
        "decision": status,
        "probe_id": probe_id,
        "buggy_ref": buggy_ref,
        "fixed_ref": fixed_ref,
        "blocking_reason": blocking_reason,
        "failure_reason": failure_reason,
        "required_runner": required_runner,
    }
    if validation_error := runtime_result_validation_error(stable_result):
        errors.append(validation_error)

    if status == "validated":
        if result.get("buggy_oracle_matched") is not True:
            errors.append("validated result must match the buggy Oracle")
        if result.get("fixed_oracle_matched") is not True:
            errors.append("validated result must match the fixed Oracle")
        if not _non_empty_string(result.get("buggy_observation")):
            errors.append("buggy_observation is required for validated result")
        if not _non_empty_string(result.get("fixed_observation")):
            errors.append("fixed_observation is required for validated result")
    return RuntimeAssessment(
        case_id=case_id,
        decision="invalid" if errors else status or "invalid",
        buggy_ref=buggy_ref,
        fixed_ref=fixed_ref,
        probe_id=probe_id,
        errors=errors,
        blocking_reason=blocking_reason,
        failure_reason=failure_reason,
        required_runner=required_runner,
    )


def _runtime_entries(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        raise ValueError("top-level runtime result must be an object")
    if payload.get("schema_version") != 1:
        raise ValueError("unsupported runtime result schema_version")
    results = payload.get("results")
    if not isinstance(results, list):
        raise ValueError("runtime result must contain a results list")
    if any(not isinstance(item, dict) for item in results):
        raise ValueError("each runtime result entry must be an object")
    return results


def _non_empty_string(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None
