import json
from pathlib import Path

import pytest

from nbtriage.evidence_receipts import (
    EvidenceReceiptError,
    create_evidence_receipt,
    evidence_receipt_revision,
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
        "schema_version": 2,
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
    receipt = create_evidence_receipt(receipt_payload(slot))

    assert receipt.slot == slot
    assert receipt.redacted is True
    assert receipt.facts == VALID_FACTS[slot]
    assert receipt.receipt_revision == evidence_receipt_revision(receipt)


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
        create_evidence_receipt(payload)


def test_receipt_rejects_suspected_secret_without_echoing_it() -> None:
    secret = "password=synthetic-secret-value"
    payload = receipt_payload("reproduction_steps")
    payload["facts"] = {"steps": ["Create a clean environment", secret]}

    with pytest.raises(EvidenceReceiptError) as raised:
        create_evidence_receipt(payload)

    assert "suspected secret" in str(raised.value)
    assert secret not in str(raised.value)


def test_configuration_requires_values_to_be_redacted() -> None:
    payload = receipt_payload("configuration")
    payload["facts"] = {"keys": ["driver"], "values_redacted": False}

    with pytest.raises(EvidenceReceiptError, match="values_redacted must be true"):
        create_evidence_receipt(payload)


def test_receipt_revision_changes_with_facts_under_the_same_raw_digest() -> None:
    first_payload = receipt_payload("logs")
    changed_payload = receipt_payload("logs")
    changed_payload["facts"] = {**changed_payload["facts"], "line_count": 49}

    first = create_evidence_receipt(first_payload)
    changed = create_evidence_receipt(changed_payload)

    assert first.content_sha256 == changed.content_sha256
    assert first.receipt_revision != changed.receipt_revision


def test_receipt_revision_is_stable_across_json_key_order() -> None:
    payload = receipt_payload("logs")
    reordered = dict(reversed(list(payload.items())))
    reordered["facts"] = dict(reversed(list(payload["facts"].items())))

    assert (
        create_evidence_receipt(payload).receipt_revision
        == create_evidence_receipt(reordered).receipt_revision
    )


def test_declared_receipt_revision_rejects_synchronized_shape_tampering() -> None:
    receipt = create_evidence_receipt(receipt_payload("logs"))
    payload = receipt.to_dict()
    payload["facts"]["line_count"] = 49

    with pytest.raises(EvidenceReceiptError, match="does not match"):
        parse_evidence_receipt(payload)


def test_legacy_receipt_is_upgraded_but_current_schema_requires_revision() -> None:
    legacy = receipt_payload("logs")
    legacy["schema_version"] = 1

    upgraded = parse_evidence_receipt(legacy)

    assert upgraded.schema_version == 2
    assert upgraded.receipt_revision == evidence_receipt_revision(upgraded)
    with pytest.raises(EvidenceReceiptError, match="missing receipt fields"):
        parse_evidence_receipt(receipt_payload("logs"))


def test_load_evidence_receipt_round_trips_json(tmp_path: Path) -> None:
    path = tmp_path / "receipt.json"
    expected = create_evidence_receipt(receipt_payload("python_version"))
    path.write_text(json.dumps(expected.to_dict()), encoding="utf-8")

    receipt = load_evidence_receipt(path)

    assert receipt == expected
