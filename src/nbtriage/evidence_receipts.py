from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from nbtriage.baselines import SECRET_PATTERNS
from nbtriage.rag import ALLOWED_EVIDENCE_SLOTS

EVIDENCE_RECEIPT_SCHEMA_VERSION = 2
LEGACY_EVIDENCE_RECEIPT_SCHEMA_VERSION = 1
EVIDENCE_RECEIPT_REVISION_PREFIX = "nbtriage-evidence-receipt-sha256:"
EVIDENCE_RECEIPT_REVISION_DOMAIN = b"nbtriage-evidence-receipt-v1\0"
OPAQUE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
VERSION_PATTERN = re.compile(r"^\d+\.\d+(?:\.\d+)?(?:[abrc]\d+)?$", re.IGNORECASE)
PACKAGE_VERSION_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}==[A-Za-z0-9][A-Za-z0-9.+_-]{0,63}$"
)
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/ -]{0,127}$")
MODULE_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:]{0,127}$")
CONNECTION_PATTERN = re.compile(
    r"^(?P<source>[A-Za-z0-9][A-Za-z0-9_.-]{0,63})->"
    r"(?P<target>[A-Za-z0-9][A-Za-z0-9_.-]{0,63})$"
)
ADDITIONAL_SECRET_PATTERNS = (
    re.compile(r"(?i)(?:password|passwd|cookie)\s*[:=]\s*['\"]?\S{8,}"),
    re.compile(r"(?i)authorization\s*[:=]\s*['\"]?bearer\s+\S+"),
)

RECEIPT_CONTENT_FIELDS = {
    "schema_version",
    "receipt_id",
    "session_id",
    "case_id",
    "slot",
    "submitted_by",
    "collected_at",
    "redacted",
    "content_sha256",
    "byte_count",
    "facts",
}
RECEIPT_REVISION_FIELDS = {
    "schema_version",
    "receipt_id",
    "session_id",
    "case_id",
    "slot",
    "content_sha256",
    "byte_count",
    "facts",
}
TOP_LEVEL_FIELDS = {*RECEIPT_CONTENT_FIELDS, "receipt_revision"}


class EvidenceReceiptError(ValueError):
    pass


@dataclass(frozen=True)
class EvidenceReceipt:
    schema_version: int
    receipt_id: str
    session_id: str
    case_id: str
    slot: str
    submitted_by: str
    collected_at: str
    redacted: bool
    content_sha256: str
    byte_count: int
    facts: dict[str, Any]
    receipt_revision: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_evidence_receipt(path: Path) -> EvidenceReceipt:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise EvidenceReceiptError(f"failed to load evidence receipt {path}: {error}") from error
    return parse_evidence_receipt(payload)


def create_evidence_receipt(payload: Any) -> EvidenceReceipt:
    """从受限内容创建带规范化版本的证据回执。

    Args:
        payload: 不含 `receipt_revision` 的回执内容；字段和事实摘要必须通过完整校验。

    Returns:
        带域分隔规范化内容版本的不可变回执。

    Raises:
        EvidenceReceiptError: schema、脱敏声明、摘要字段或敏感值检查失败。

    Note:
        `receipt_revision` 是内容地址，不是签名；它不能证明摘要真实来自
        `content_sha256` 指向的原始材料。
    """
    normalized = _normalize_receipt_content(
        payload,
        expected_fields=RECEIPT_CONTENT_FIELDS,
        accepted_schema_version=EVIDENCE_RECEIPT_SCHEMA_VERSION,
    )
    return _receipt_from_content(normalized)


def parse_evidence_receipt(payload: Any) -> EvidenceReceipt:
    """校验并规范化一份可安全持久化的结构化证据回执。

    Args:
        payload: 已解析的 JSON 值；顶层必须是字段严格受限的对象。

    Returns:
        只包含白名单化摘要和原始材料指纹的不可变回执。

    Raises:
        EvidenceReceiptError: schema、会话绑定、脱敏声明、摘要字段或敏感值检查失败。
    """
    if not isinstance(payload, dict):
        raise EvidenceReceiptError("evidence receipt must be an object")
    source_schema_version = payload.get("schema_version")
    if source_schema_version == LEGACY_EVIDENCE_RECEIPT_SCHEMA_VERSION:
        normalized = _normalize_receipt_content(
            payload,
            expected_fields=RECEIPT_CONTENT_FIELDS,
            accepted_schema_version=LEGACY_EVIDENCE_RECEIPT_SCHEMA_VERSION,
        )
        return _receipt_from_content(normalized)
    if source_schema_version != EVIDENCE_RECEIPT_SCHEMA_VERSION:
        raise EvidenceReceiptError("unsupported evidence receipt schema_version")
    normalized = _normalize_receipt_content(
        payload,
        expected_fields=TOP_LEVEL_FIELDS,
        accepted_schema_version=EVIDENCE_RECEIPT_SCHEMA_VERSION,
    )
    declared_revision = _receipt_revision(payload.get("receipt_revision"))
    expected_revision = receipt_revision_for_observation(
        {key: value for key, value in normalized.items() if key in RECEIPT_REVISION_FIELDS}
    )
    if not hmac.compare_digest(declared_revision, expected_revision):
        raise EvidenceReceiptError(
            "receipt_revision does not match normalized evidence receipt content"
        )
    return _receipt_from_content(normalized, receipt_revision=declared_revision)


def evidence_receipt_revision(receipt: EvidenceReceipt) -> str:
    """重新计算回执规范化内容的域分隔版本。"""
    return receipt_revision_for_observation(
        {
            "schema_version": receipt.schema_version,
            "receipt_id": receipt.receipt_id,
            "session_id": receipt.session_id,
            "case_id": receipt.case_id,
            "slot": receipt.slot,
            "content_sha256": receipt.content_sha256,
            "byte_count": receipt.byte_count,
            "facts": receipt.facts,
        }
    )


def receipt_revision_for_content(payload: Any) -> str:
    """为受限回执内容计算域分隔版本，不接受已声明的版本字段。"""
    normalized = _normalize_receipt_content(
        payload,
        expected_fields=RECEIPT_CONTENT_FIELDS,
        accepted_schema_version=EVIDENCE_RECEIPT_SCHEMA_VERSION,
    )
    return receipt_revision_for_observation(
        {key: value for key, value in normalized.items() if key in RECEIPT_REVISION_FIELDS}
    )


def receipt_revision_for_observation(payload: Any) -> str:
    """根据 Agent observation 保存的回执绑定与规范化事实重算版本。"""
    if not isinstance(payload, dict) or set(payload) != RECEIPT_REVISION_FIELDS:
        raise EvidenceReceiptError("evidence receipt observation fields are invalid")
    if payload.get("schema_version") != EVIDENCE_RECEIPT_SCHEMA_VERSION:
        raise EvidenceReceiptError("unsupported evidence receipt schema_version")
    slot = _enum_string(payload.get("slot"), "slot", ALLOWED_EVIDENCE_SLOTS)
    facts = payload.get("facts")
    if not isinstance(facts, dict):
        raise EvidenceReceiptError("facts must be an object")
    normalized_facts = FACT_VALIDATORS[slot](facts)
    _reject_suspected_secret(normalized_facts)
    return _content_revision(
        {
            "schema_version": EVIDENCE_RECEIPT_SCHEMA_VERSION,
            "receipt_id": _opaque_id(payload.get("receipt_id"), "receipt_id"),
            "session_id": _opaque_id(payload.get("session_id"), "session_id"),
            "case_id": _opaque_id(payload.get("case_id"), "case_id"),
            "slot": slot,
            "content_sha256": _sha256(payload.get("content_sha256")),
            "byte_count": _byte_count(payload.get("byte_count")),
            "facts": normalized_facts,
        }
    )


def _normalize_receipt_content(
    payload: Any,
    *,
    expected_fields: set[str],
    accepted_schema_version: int,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise EvidenceReceiptError("evidence receipt must be an object")
    unknown_fields = set(payload) - expected_fields
    missing_fields = expected_fields - set(payload)
    if unknown_fields:
        raise EvidenceReceiptError(f"unsupported receipt fields: {sorted(unknown_fields)}")
    if missing_fields:
        raise EvidenceReceiptError(f"missing receipt fields: {sorted(missing_fields)}")
    if payload.get("schema_version") != accepted_schema_version:
        raise EvidenceReceiptError("unsupported evidence receipt schema_version")

    slot = _enum_string(payload.get("slot"), "slot", ALLOWED_EVIDENCE_SLOTS)
    redacted = payload.get("redacted")
    if redacted is not True:
        raise EvidenceReceiptError("evidence receipt must declare redacted=true")
    byte_count = _byte_count(payload.get("byte_count"))

    collected_at = _short_string(payload.get("collected_at"), "collected_at", max_length=64)
    try:
        parsed_time = datetime.fromisoformat(collected_at)
    except ValueError as error:
        raise EvidenceReceiptError("collected_at must be an ISO-8601 timestamp") from error
    if parsed_time.tzinfo is None:
        raise EvidenceReceiptError("collected_at must include a timezone")

    facts = payload.get("facts")
    if not isinstance(facts, dict):
        raise EvidenceReceiptError("facts must be an object")
    normalized_facts = FACT_VALIDATORS[slot](facts)
    _reject_suspected_secret(normalized_facts)

    return {
        "schema_version": accepted_schema_version,
        "receipt_id": _opaque_id(payload.get("receipt_id"), "receipt_id"),
        "session_id": _opaque_id(payload.get("session_id"), "session_id"),
        "case_id": _opaque_id(payload.get("case_id"), "case_id"),
        "slot": slot,
        "submitted_by": _short_string(payload.get("submitted_by"), "submitted_by"),
        "collected_at": collected_at,
        "redacted": True,
        "content_sha256": _sha256(payload.get("content_sha256")),
        "byte_count": byte_count,
        "facts": normalized_facts,
    }


def _receipt_from_content(
    content: dict[str, Any],
    *,
    receipt_revision: str | None = None,
) -> EvidenceReceipt:
    revision_content = {
        key: value for key, value in content.items() if key in RECEIPT_REVISION_FIELDS
    }
    revision_content["schema_version"] = EVIDENCE_RECEIPT_SCHEMA_VERSION
    return EvidenceReceipt(
        schema_version=EVIDENCE_RECEIPT_SCHEMA_VERSION,
        receipt_id=content["receipt_id"],
        session_id=content["session_id"],
        case_id=content["case_id"],
        slot=content["slot"],
        submitted_by=content["submitted_by"],
        collected_at=content["collected_at"],
        redacted=content["redacted"],
        content_sha256=content["content_sha256"],
        byte_count=content["byte_count"],
        facts=content["facts"],
        receipt_revision=receipt_revision or _content_revision(revision_content),
    )


def _content_revision(content: dict[str, Any]) -> str:
    canonical = json.dumps(
        content,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    digest = hashlib.sha256(EVIDENCE_RECEIPT_REVISION_DOMAIN)
    digest.update(canonical)
    return f"{EVIDENCE_RECEIPT_REVISION_PREFIX}{digest.hexdigest()}"


def _receipt_revision(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith(EVIDENCE_RECEIPT_REVISION_PREFIX)
        or not SHA256_PATTERN.fullmatch(value.removeprefix(EVIDENCE_RECEIPT_REVISION_PREFIX))
    ):
        raise EvidenceReceiptError(
            "receipt_revision must be a nbtriage evidence receipt SHA-256 revision"
        )
    return value


def _reject_suspected_secret(facts: dict[str, Any]) -> None:
    serialized_facts = json.dumps(facts, ensure_ascii=False, sort_keys=True)
    secret_patterns = (*SECRET_PATTERNS, *ADDITIONAL_SECRET_PATTERNS)
    if any(pattern.search(serialized_facts) for pattern in secret_patterns):
        raise EvidenceReceiptError("facts contain a suspected secret value")


def _validate_python_version(facts: dict[str, Any]) -> dict[str, Any]:
    _exact_fields(facts, {"version"}, {"implementation"})
    version = _short_string(facts.get("version"), "facts.version", max_length=32)
    if not VERSION_PATTERN.fullmatch(version):
        raise EvidenceReceiptError("facts.version must be a normalized Python version")
    normalized = {"version": version}
    if "implementation" in facts:
        normalized["implementation"] = _identifier(
            facts.get("implementation"), "facts.implementation"
        )
    return normalized


def _validate_component_versions(facts: dict[str, Any]) -> dict[str, Any]:
    _exact_fields(facts, {"versions"})
    versions = _string_list(facts.get("versions"), "facts.versions", max_items=32)
    if any(not PACKAGE_VERSION_PATTERN.fullmatch(item) for item in versions):
        raise EvidenceReceiptError("facts.versions entries must use package==version")
    return {"versions": versions}


def _validate_operating_system(facts: dict[str, Any]) -> dict[str, Any]:
    _exact_fields(facts, {"name", "runtime"}, {"release"})
    normalized = {
        "name": _identifier(facts.get("name"), "facts.name"),
        "runtime": _identifier(facts.get("runtime"), "facts.runtime"),
    }
    if "release" in facts:
        normalized["release"] = _identifier(facts.get("release"), "facts.release")
    return normalized


def _validate_logs(facts: dict[str, Any]) -> dict[str, Any]:
    _exact_fields(facts, {"exception_type", "stack_modules", "line_count"})
    exception_type = _short_string(
        facts.get("exception_type"), "facts.exception_type", max_length=128
    )
    if not MODULE_PATTERN.fullmatch(exception_type):
        raise EvidenceReceiptError("facts.exception_type must be a qualified identifier")
    modules = _string_list(facts.get("stack_modules"), "facts.stack_modules", max_items=32)
    if any(not MODULE_PATTERN.fullmatch(item) for item in modules):
        raise EvidenceReceiptError("facts.stack_modules entries must be qualified identifiers")
    return {
        "exception_type": exception_type,
        "stack_modules": modules,
        "line_count": _positive_int(facts.get("line_count"), "facts.line_count"),
    }


def _validate_reproduction_steps(facts: dict[str, Any]) -> dict[str, Any]:
    _exact_fields(facts, {"steps"})
    return {"steps": _string_list(facts.get("steps"), "facts.steps", max_items=12, max_length=240)}


def _validate_expected_behavior(facts: dict[str, Any]) -> dict[str, Any]:
    _exact_fields(facts, {"expected", "observed"})
    return {
        "expected": _short_string(facts.get("expected"), "facts.expected", max_length=500),
        "observed": _short_string(facts.get("observed"), "facts.observed", max_length=500),
    }


def _validate_configuration(facts: dict[str, Any]) -> dict[str, Any]:
    _exact_fields(facts, {"keys", "values_redacted"})
    if facts.get("values_redacted") is not True:
        raise EvidenceReceiptError("facts.values_redacted must be true")
    keys = _string_list(facts.get("keys"), "facts.keys", max_items=64)
    if any(not MODULE_PATTERN.fullmatch(item) for item in keys):
        raise EvidenceReceiptError("facts.keys entries must be configuration key names")
    return {"keys": keys, "values_redacted": True}


def _validate_deployment_topology(facts: dict[str, Any]) -> dict[str, Any]:
    _exact_fields(facts, {"components", "connections"})
    components = _string_list(facts.get("components"), "facts.components", max_items=32)
    if len(components) < 2 or any(not MODULE_PATTERN.fullmatch(item) for item in components):
        raise EvidenceReceiptError("facts.components must contain at least two identifiers")
    connections = _string_list(facts.get("connections"), "facts.connections", max_items=64)
    component_set = set(components)
    for connection in connections:
        match = CONNECTION_PATTERN.fullmatch(connection)
        if match is None or {match.group("source"), match.group("target")} - component_set:
            raise EvidenceReceiptError("facts.connections must connect declared components")
    return {"components": components, "connections": connections}


def _validate_raw_close_evidence(facts: dict[str, Any]) -> dict[str, Any]:
    _exact_fields(facts, {"close_code", "reason_category", "reconnect_observed"})
    close_code = facts.get("close_code")
    if (
        not isinstance(close_code, int)
        or isinstance(close_code, bool)
        or not 1000 <= close_code <= 4999
    ):
        raise EvidenceReceiptError("facts.close_code must be between 1000 and 4999")
    reconnect_observed = facts.get("reconnect_observed")
    if not isinstance(reconnect_observed, bool):
        raise EvidenceReceiptError("facts.reconnect_observed must be a boolean")
    return {
        "close_code": close_code,
        "reason_category": _identifier(facts.get("reason_category"), "facts.reason_category"),
        "reconnect_observed": reconnect_observed,
    }


FACT_VALIDATORS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "python_version": _validate_python_version,
    "component_versions": _validate_component_versions,
    "operating_system": _validate_operating_system,
    "logs": _validate_logs,
    "reproduction_steps": _validate_reproduction_steps,
    "expected_behavior": _validate_expected_behavior,
    "configuration": _validate_configuration,
    "deployment_topology": _validate_deployment_topology,
    "raw_close_evidence": _validate_raw_close_evidence,
}


def _exact_fields(
    payload: dict[str, Any], required: set[str], optional: set[str] | None = None
) -> None:
    allowed = required | (optional or set())
    missing = required - set(payload)
    unknown = set(payload) - allowed
    if missing:
        raise EvidenceReceiptError(f"missing fact fields: {sorted(missing)}")
    if unknown:
        raise EvidenceReceiptError(f"unsupported fact fields: {sorted(unknown)}")


def _opaque_id(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not OPAQUE_ID_PATTERN.fullmatch(value):
        raise EvidenceReceiptError(f"{field_name} contains unsupported characters")
    return value


def _sha256(value: Any) -> str:
    if not isinstance(value, str) or not SHA256_PATTERN.fullmatch(value):
        raise EvidenceReceiptError("content_sha256 must be a lowercase SHA-256 digest")
    return value


def _short_string(value: Any, field_name: str, *, max_length: int = 128) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvidenceReceiptError(f"{field_name} must be a non-empty string")
    normalized = value.strip()
    if len(normalized) > max_length or "\x00" in normalized:
        raise EvidenceReceiptError(f"{field_name} exceeds its safe string boundary")
    return normalized


def _identifier(value: Any, field_name: str) -> str:
    normalized = _short_string(value, field_name)
    if not IDENTIFIER_PATTERN.fullmatch(normalized):
        raise EvidenceReceiptError(f"{field_name} must be a bounded identifier")
    return normalized


def _enum_string(value: Any, field_name: str, allowed: set[str]) -> str:
    normalized = _short_string(value, field_name)
    if normalized not in allowed:
        raise EvidenceReceiptError(f"{field_name} is unsupported")
    return normalized


def _string_list(
    value: Any,
    field_name: str,
    *,
    max_items: int,
    max_length: int = 128,
) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= max_items:
        raise EvidenceReceiptError(f"{field_name} must be a non-empty bounded list")
    normalized = [_short_string(item, f"{field_name}[]", max_length=max_length) for item in value]
    if len(set(normalized)) != len(normalized):
        raise EvidenceReceiptError(f"{field_name} entries must be unique")
    return normalized


def _positive_int(value: Any, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise EvidenceReceiptError(f"{field_name} must be a positive integer")
    return value


def _byte_count(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 1_000_000_000_000:
        raise EvidenceReceiptError("byte_count must be an integer between 1 and 1000000000000")
    return value
