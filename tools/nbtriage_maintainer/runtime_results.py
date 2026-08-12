"""仓库维护者使用的运行结果评估。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

RUNTIME_STATUSES = {"validated", "failed", "blocked"}
DEFAULT_PROBE_ROOT = Path(__file__).resolve().parents[2]
MAX_PROBE_BYTES = 2 * 1024 * 1024
CASE_ORACLE_FIELDS = (
    "field_provenance",
    "support_level",
    "execution_mode",
    "root_cause_cluster",
    "environment",
    "versions",
    "deployment_topology",
    "observed_behavior",
    "expected_behavior",
    "reproduction_steps",
    "fault_phase",
    "symptoms",
    "candidate_owners",
    "oracle",
)
RUNTIME_RESULT_FIELDS = frozenset(
    {
        "decision",
        "probe_id",
        "probe_source",
        "probe_source_sha256",
        "case_oracle_revision",
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
    case_oracle_revision: str = ""
    probe_source: str | None = None
    probe_source_sha256: str | None = None
    blocking_reason: str | None = None
    failure_reason: str | None = None
    required_runner: str | None = None


def runtime_result_validation_error(result: Mapping[str, Any]) -> str | None:
    """验证 RuntimeAssessment 持久化稳定字段的结构和组合语义。

    Args:
        result: 只包含会话持久化所需稳定字段的映射。

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

    case_revision = _sha256(result.get("case_oracle_revision"))
    if case_revision is None:
        return "case_oracle_revision must be a lowercase SHA-256 digest"

    probe_source = _non_empty_string(result.get("probe_source"))
    probe_sha256 = _sha256(result.get("probe_source_sha256"))
    if decision in {"validated", "failed"}:
        if probe_source is None:
            return f"probe_source is required for {decision} result"
        if result.get("probe_source") != probe_source:
            return "probe_source must be a normalized string"
        if probe_sha256 is None:
            return f"probe_source_sha256 is required for {decision} result"
    elif (probe_source is None) != (probe_sha256 is None):
        return "probe_source and probe_source_sha256 must be provided together"
    elif probe_source is not None and result.get("probe_source") != probe_source:
        return "probe_source must be a normalized string"
    elif probe_sha256 is None and result.get("probe_source_sha256") is not None:
        return "probe_source_sha256 must be a lowercase SHA-256 digest"

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
    *,
    probe_root: Path | None = None,
) -> tuple[list[RuntimeAssessment], list[dict[str, str]]]:
    """读取并核对版本化 Oracle 运行结果。

    运行结果只能证明已经策展的 Case，且其中的故障与修复引用必须和
    `SupportCase` 完全一致，避免用另一个提交或环境结果冒充目标 Oracle。

    Args:
        results_path: 单个结果文件或包含结果文件的目录；不存在时视为尚未运行。
        cases_by_id: 已达到可执行规格门槛的 Case，以 `case_id` 为键。
        probe_root: `probe_source` 相对路径的受控根目录；默认为代码仓根目录。

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
            assessments.append(
                assess_runtime_result(
                    result,
                    cases_by_id.get(case_id),
                    probe_root=probe_root,
                )
            )

    return assessments, load_errors


def assess_runtime_result(
    result: dict[str, Any],
    case_payload: dict[str, Any] | None,
    *,
    probe_root: Path | None = None,
) -> RuntimeAssessment:
    case_id = _non_empty_string(result.get("case_id")) or "<missing-case-id>"
    status = _non_empty_string(result.get("status"))
    buggy_ref = _non_empty_string(result.get("buggy_ref"))
    fixed_ref = _non_empty_string(result.get("fixed_ref"))
    probe_id = _non_empty_string(result.get("probe_id"))
    declared_case_revision = _sha256(result.get("case_oracle_revision"))
    probe_source = _non_empty_string(result.get("probe_source"))
    declared_probe_sha256 = _sha256(result.get("probe_source_sha256"))
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
        try:
            expected_case_revision = case_oracle_revision(case_payload)
        except (TypeError, ValueError) as error:
            errors.append(f"loaded SupportCase cannot produce a canonical revision: {error}")
        else:
            if declared_case_revision != expected_case_revision:
                errors.append("case_oracle_revision does not match SupportCase content")

    stable_result = {
        "decision": result.get("status"),
        "probe_id": result.get("probe_id"),
        "probe_source": result.get("probe_source"),
        "probe_source_sha256": result.get("probe_source_sha256"),
        "case_oracle_revision": result.get("case_oracle_revision"),
        "buggy_ref": result.get("buggy_ref"),
        "fixed_ref": result.get("fixed_ref"),
        "blocking_reason": result.get("blocking_reason"),
        "failure_reason": result.get("failure_reason"),
        "required_runner": result.get("required_runner"),
    }
    if validation_error := runtime_result_validation_error(stable_result):
        errors.append(validation_error)

    if probe_source is not None and declared_probe_sha256 is not None:
        try:
            actual_probe_sha256 = probe_file_sha256(
                probe_root or DEFAULT_PROBE_ROOT,
                probe_source,
            )
        except (OSError, ValueError) as error:
            errors.append(f"probe_source cannot be verified: {error}")
        else:
            if declared_probe_sha256 != actual_probe_sha256:
                errors.append("probe_source_sha256 does not match probe_source bytes")

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
        case_oracle_revision=declared_case_revision or "",
        probe_source=probe_source,
        probe_source_sha256=declared_probe_sha256,
        blocking_reason=blocking_reason,
        failure_reason=failure_reason,
        required_runner=required_runner,
    )


def _runtime_entries(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        raise ValueError("top-level runtime result must be an object")
    if payload.get("schema_version") != 2:
        raise ValueError("unsupported runtime result schema_version")
    results = payload.get("results")
    if not isinstance(results, list):
        raise ValueError("runtime result must contain a results list")
    if any(not isinstance(item, dict) for item in results):
        raise ValueError("each runtime result entry must be an object")
    return results


def _non_empty_string(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def case_oracle_revision(case_payload: Mapping[str, Any]) -> str:
    """计算 Case 与完整策展合同的规范化版本摘要。

    源文本和 JSON 排版不影响摘要，但 Case ID、schema 或任何策展字段（包括
    Oracle 的故障签名和成功断言）改变都会产生新版本。

    Args:
        case_payload: 已解析的 SupportCase。

    Returns:
        小写十六进制 SHA-256 摘要。

    Raises:
        ValueError: Case ID 或策展对象缺失，或内容不能规范化为 JSON。
    """
    case_id = _non_empty_string(case_payload.get("case_id"))
    if case_id is None:
        raise ValueError("case_id is required")
    curation = case_payload.get("curation")
    if not isinstance(curation, Mapping):
        raise ValueError("curation must be an object")
    contract = {
        "contract": "nbtriage.runtime-case-oracle.v1",
        "schema_version": case_payload.get("schema_version"),
        "case_id": case_id,
        "curation": {field: curation.get(field) for field in CASE_ORACLE_FIELDS},
    }
    try:
        encoded = json.dumps(
            contract,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError("case contract must contain canonical JSON values") from error
    return hashlib.sha256(encoded).hexdigest()


def probe_file_sha256(probe_root: Path, probe_source: str) -> str:
    """在受控根目录内解析 Probe 并计算原始字节摘要。

    Args:
        probe_root: Probe 路径允许解析到的唯一根目录。
        probe_source: 结果合同中的相对源码路径。

    Returns:
        Probe 文件原始字节的小写 SHA-256 摘要。

    Raises:
        ValueError: 路径非相对路径、越界、不是普通文件或文件过大。
        OSError: 根目录或 Probe 无法读取。
    """
    normalized = _non_empty_string(probe_source)
    if normalized is None or normalized != probe_source:
        raise ValueError("probe_source must be a normalized string")
    relative = Path(normalized)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("probe_source must be a repository-relative path")
    root = probe_root.resolve(strict=True)
    target = (root / relative).resolve(strict=True)
    if not target.is_relative_to(root):
        raise ValueError("probe_source resolves outside probe_root")
    if not target.is_file():
        raise ValueError("probe_source must resolve to a file")
    if target.stat().st_size > MAX_PROBE_BYTES:
        raise ValueError("probe_source exceeds the 2 MiB verification limit")
    with target.open("rb") as stream:
        raw = stream.read(MAX_PROBE_BYTES + 1)
    if len(raw) > MAX_PROBE_BYTES:
        raise ValueError("probe_source exceeds the 2 MiB verification limit")
    return hashlib.sha256(raw).hexdigest()


def _sha256(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) != 64:
        return None
    return value if all(character in "0123456789abcdef" for character in value) else None
