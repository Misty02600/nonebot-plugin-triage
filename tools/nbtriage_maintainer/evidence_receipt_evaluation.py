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


class EvidenceReceiptEvaluationError(ValueError):
    pass


def evaluate_b3_evidence_receipts(fixtures_path: Path) -> dict[str, Any]:
    """离线评估结构化回执校验与请求绑定，不接触原始私人材料。"""
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
    return {
        "schema_version": 1,
        "evaluation_id": "b3-evidence-receipts-v1",
        "fixture_set_id": payload["fixture_set_id"],
        "generated_at": datetime.now(UTC).isoformat(),
        "source": {
            "fixtures_path": str(fixtures_path),
            "fixtures_sha256": hashlib.sha256(raw).hexdigest(),
        },
        "summary": {
            "case_count": count,
            "synthetic_only": True,
            "expected_valid": expected_valid,
            "expected_invalid": expected_invalid,
            "model_calls": 0,
            "external_tool_calls": 0,
        },
        "metrics": {
            "decision_accuracy": _ratio(correct, count),
            "valid_accept_rate": _ratio(valid_accepted, expected_valid),
            "invalid_reject_rate": _ratio(invalid_rejected, expected_invalid),
        },
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


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0
