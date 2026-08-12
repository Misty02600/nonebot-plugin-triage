"""仓库维护者使用的证据回执评测。"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nbtriage.evidence_receipts import (
    EvidenceReceiptError,
    create_evidence_receipt,
    parse_evidence_receipt,
)
from nbtriage.rag import ALLOWED_EVIDENCE_SLOTS

B3_EVIDENCE_RECEIPT_EVALUATION_ID = "b3-evidence-receipts-v1"
B3_EVIDENCE_RECEIPT_CUSTOM_EVALUATION_ID = "b3-evidence-receipts-custom-unqualified-v1"
B3_EVIDENCE_RECEIPT_OFFICIAL_FIXTURE_SHA256 = (
    "b788c4590eb61fa9d9ca0a34e281f038baeef9053e781ed9697137a51b3ebcef"
)
B3_EVIDENCE_RECEIPT_OFFICIAL_CASE_COUNT = 16


class EvidenceReceiptEvaluationError(ValueError):
    pass


def evaluate_b3_evidence_receipts(fixtures_path: Path) -> dict[str, Any]:
    """离线评估结构化回执校验与请求绑定，不接触原始私人材料。

    只有与仓库冻结 Fixture 原始字节、集合 ID 和 Case 数量都一致的输入才具备正式 Gate
    资格。其他结构合法的合成集合仍可用于本地诊断，但报告会降级为
    ``custom_unqualified``。
    """
    raw, payload = _load_fixtures(fixtures_path)
    rows = []
    correct = 0
    valid_accepted = 0
    invalid_rejected = 0
    expected_valid = 0
    expected_invalid = 0
    for fixture in payload["fixtures"]:
        request = fixture["request"]
        error_category = None
        try:
            receipt_payload = fixture["receipt"]
            receipt = (
                create_evidence_receipt(receipt_payload)
                if receipt_payload.get("schema_version") == 2
                and "receipt_revision" not in receipt_payload
                else parse_evidence_receipt(receipt_payload)
            )
            if (
                receipt.session_id != request["session_id"]
                or receipt.case_id != request["case_id"]
                or receipt.slot != request["slot"]
            ):
                raise EvidenceReceiptError("receipt binding does not match request")
            decision = "accepted"
        except EvidenceReceiptError as error:
            decision = "rejected"
            error_category = _error_category(error)

        expected = fixture["expected_decision"]
        is_correct = decision == expected
        correct += is_correct
        if expected == "accepted":
            expected_valid += 1
            valid_accepted += decision == "accepted"
        else:
            expected_invalid += 1
            invalid_rejected += decision == "rejected"
        rows.append(
            {
                "fixture_id": fixture["fixture_id"],
                "category": fixture["category"],
                "expected_decision": expected,
                "decision": decision,
                "correct": is_correct,
                "error_category": error_category,
            }
        )

    count = len(rows)
    fixtures_sha256 = hashlib.sha256(raw).hexdigest()
    official_contract = (
        fixtures_sha256 == B3_EVIDENCE_RECEIPT_OFFICIAL_FIXTURE_SHA256
        and payload["fixture_set_id"] == B3_EVIDENCE_RECEIPT_EVALUATION_ID
        and count == B3_EVIDENCE_RECEIPT_OFFICIAL_CASE_COUNT
    )
    summary = {
        "case_count": count,
        "synthetic_only": True,
        "expected_valid": expected_valid,
        "expected_invalid": expected_invalid,
        "model_calls": 0,
        "external_tool_calls": 0,
    }
    metrics = {
        "decision_accuracy": _ratio(correct, count),
        "valid_accept_rate": _ratio(valid_accepted, expected_valid),
        "invalid_reject_rate": _ratio(invalid_rejected, expected_invalid),
    }
    quality_gate = _quality_gate(
        summary,
        metrics,
        official_contract=official_contract,
    )
    return {
        "schema_version": 1,
        "evaluation_id": (
            B3_EVIDENCE_RECEIPT_EVALUATION_ID
            if official_contract
            else B3_EVIDENCE_RECEIPT_CUSTOM_EVALUATION_ID
        ),
        "evaluation_qualification": (
            "official_frozen_fixture" if official_contract else "custom_unqualified"
        ),
        "fixture_set_id": payload["fixture_set_id"],
        "generated_at": datetime.now(UTC).isoformat(),
        "source": {
            "fixtures_path": fixtures_path.as_posix(),
            "fixtures_sha256": fixtures_sha256,
            "official_fixtures_sha256": B3_EVIDENCE_RECEIPT_OFFICIAL_FIXTURE_SHA256,
            "official_fixture_set_id": B3_EVIDENCE_RECEIPT_EVALUATION_ID,
            "official_case_count": B3_EVIDENCE_RECEIPT_OFFICIAL_CASE_COUNT,
        },
        "summary": summary,
        "metrics": metrics,
        "quality_gate": quality_gate,
        "predictions": rows,
        "limitations": [
            "All receipts are synthetic and do not establish truth or diagnostic sufficiency.",
            "The evaluator checks schema, redaction boundaries, suspected secrets, and request "
            "binding; it does not inspect the original evidence referenced by the digest.",
            "A later reassessment layer must decide whether accepted facts change the diagnosis.",
        ],
    }


def _load_fixtures(path: Path) -> tuple[bytes, dict[str, Any]]:
    try:
        raw = path.read_bytes()
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError) as error:
        raise EvidenceReceiptEvaluationError(
            f"failed to load B3 evidence receipt fixtures {path}: {error}"
        ) from error
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise EvidenceReceiptEvaluationError("fixture set must be a schema_version 1 object")
    if payload.get("synthetic_only") is not True:
        raise EvidenceReceiptEvaluationError("fixture set must declare synthetic_only=true")
    fixture_set_id = payload.get("fixture_set_id")
    if not isinstance(fixture_set_id, str) or not fixture_set_id:
        raise EvidenceReceiptEvaluationError("fixture set must contain fixture_set_id")
    fixtures = payload.get("fixtures")
    if not isinstance(fixtures, list) or not fixtures:
        raise EvidenceReceiptEvaluationError("fixture set must contain fixtures")
    seen: set[str] = set()
    for fixture in fixtures:
        if not isinstance(fixture, dict):
            raise EvidenceReceiptEvaluationError("each fixture must be an object")
        fixture_id = fixture.get("fixture_id")
        if not isinstance(fixture_id, str) or not fixture_id or fixture_id in seen:
            raise EvidenceReceiptEvaluationError("fixture IDs must be non-empty and unique")
        seen.add(fixture_id)
        if not isinstance(fixture.get("category"), str) or not fixture["category"]:
            raise EvidenceReceiptEvaluationError(f"fixture {fixture_id} must contain category")
        if fixture.get("expected_decision") not in {"accepted", "rejected"}:
            raise EvidenceReceiptEvaluationError(
                f"fixture {fixture_id} has invalid expected_decision"
            )
        if not isinstance(fixture.get("receipt"), dict):
            raise EvidenceReceiptEvaluationError(f"fixture {fixture_id} must contain receipt")
        request = fixture.get("request")
        if not isinstance(request, dict) or set(request) != {"session_id", "case_id", "slot"}:
            raise EvidenceReceiptEvaluationError(f"fixture {fixture_id} has invalid request")
        if request["slot"] not in ALLOWED_EVIDENCE_SLOTS:
            raise EvidenceReceiptEvaluationError(f"fixture {fixture_id} has invalid request slot")
        if any(
            not isinstance(request[field], str) or not request[field]
            for field in ("session_id", "case_id")
        ):
            raise EvidenceReceiptEvaluationError(f"fixture {fixture_id} has invalid binding")
    return raw, payload


def _error_category(error: EvidenceReceiptError) -> str:
    message = str(error)
    if "secret" in message:
        return "suspected_secret"
    if "redacted" in message:
        return "redaction"
    if "binding" in message:
        return "binding"
    if "field" in message or "facts" in message:
        return "schema"
    return "value"


def _quality_gate(
    summary: dict[str, Any],
    metrics: dict[str, float],
    *,
    official_contract: bool,
) -> dict[str, Any]:
    checks = {
        "official_fixture_contract": official_contract,
        "complete_official_coverage": (
            summary["case_count"] == B3_EVIDENCE_RECEIPT_OFFICIAL_CASE_COUNT
        ),
        "decision_accuracy": metrics["decision_accuracy"] == 1.0,
        "valid_accept_rate": metrics["valid_accept_rate"] == 1.0,
        "invalid_reject_rate": metrics["invalid_reject_rate"] == 1.0,
        "zero_model_calls": summary["model_calls"] == 0,
        "zero_external_tool_calls": summary["external_tool_calls"] == 0,
    }
    if not official_contract:
        status = "unqualified"
    else:
        status = "passed" if all(checks.values()) else "failed"
    return {
        "status": status,
        "thresholds": {
            "official_fixture_set_id": B3_EVIDENCE_RECEIPT_EVALUATION_ID,
            "official_case_count": B3_EVIDENCE_RECEIPT_OFFICIAL_CASE_COUNT,
            "minimum_decision_accuracy": 1.0,
            "minimum_valid_accept_rate": 1.0,
            "minimum_invalid_reject_rate": 1.0,
            "maximum_model_calls": 0,
            "maximum_external_tool_calls": 0,
        },
        "checks": checks,
    }


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0
