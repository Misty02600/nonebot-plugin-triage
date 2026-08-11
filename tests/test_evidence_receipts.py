import json
from pathlib import Path

import pytest

from nbtriage.evidence_receipts import (
    EvidenceReceiptError,
    load_evidence_receipt,
    parse_evidence_receipt,
)

VALID_FACTS = {
    "python_version": {"version": "3.12.7", "implementation": "CPython"},
    "component_versions": {"versions": ["nonebot2==2.4.0", "nonebot-adapter-onebot==2.4.6"]},
    "operating_system": {"name": "Windows", "release": "11", "runtime": "venv"},
    "logs": {
        "exception_type": "builtins.TypeError",
        "stack_modules": ["nonebot.matcher", "plugin.handlers:on_message"],
        "line_count": 48,
    },
    "reproduction_steps": {
        "steps": ["Create a clean virtual environment", "Send one group message"]
    },
    "expected_behavior": {
        "expected": "The matcher handles one message.",
        "observed": "The matcher raises TypeError before replying.",
    },
    "configuration": {
        "keys": ["driver", "host", "plugin_settings.timeout"],
        "values_redacted": True,
    },
    "deployment_topology": {
        "components": ["nonebot", "onebot"],
        "connections": ["nonebot->onebot"],
    },
    "raw_close_evidence": {
        "close_code": 1006,
        "reason_category": "abnormal closure",
        "reconnect_observed": True,
    },
}


def receipt_payload(slot: str, *, receipt_id: str = "receipt-1") -> dict:
    return {
        "schema_version": 1,
        "receipt_id": receipt_id,
        "session_id": "session-1",
        "case_id": "case-1",
        "slot": slot,
        "submitted_by": "maintainer",
        "collected_at": "2026-08-08T12:00:00+00:00",
        "redacted": True,
        "content_sha256": "a" * 64,
        "byte_count": 512,
        "facts": VALID_FACTS[slot],
    }


@pytest.mark.parametrize("slot", sorted(VALID_FACTS))
def test_all_evidence_slots_accept_bounded_redacted_facts(slot: str) -> None:
    receipt = parse_evidence_receipt(receipt_payload(slot))

    assert receipt.slot == slot
    assert receipt.redacted is True
    assert receipt.facts == VALID_FACTS[slot]


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda item: item.update(redacted=False), "redacted=true"),
        (lambda item: item.update(content_sha256="not-a-digest"), "SHA-256"),
        (lambda item: item.update(collected_at="2026-08-08T12:00:00"), "timezone"),
        (lambda item: item.update(raw_body="must not persist"), "unsupported receipt fields"),
        (lambda item: item["facts"].update(raw_log="traceback"), "unsupported fact fields"),
    ],
)
def test_receipt_rejects_unsafe_or_unbounded_shape(mutation, message: str) -> None:
    payload = receipt_payload("logs")
    payload["facts"] = dict(payload["facts"])
    mutation(payload)

    with pytest.raises(EvidenceReceiptError, match=message):
        parse_evidence_receipt(payload)


def test_receipt_rejects_suspected_secret_without_echoing_it() -> None:
    secret = "password=synthetic-secret-value"
    payload = receipt_payload("reproduction_steps")
    payload["facts"] = {"steps": ["Create a clean environment", secret]}

    with pytest.raises(EvidenceReceiptError) as raised:
        parse_evidence_receipt(payload)

    assert "suspected secret" in str(raised.value)
    assert secret not in str(raised.value)


def test_configuration_requires_values_to_be_redacted() -> None:
    payload = receipt_payload("configuration")
    payload["facts"] = {"keys": ["driver"], "values_redacted": False}

    with pytest.raises(EvidenceReceiptError, match="values_redacted must be true"):
        parse_evidence_receipt(payload)


def test_load_evidence_receipt_round_trips_json(tmp_path: Path) -> None:
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(receipt_payload("python_version")), encoding="utf-8")

    receipt = load_evidence_receipt(path)

    assert receipt.to_dict() == receipt_payload("python_version")
