from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from nonebot.adapters import Event

from nbtriage.support_threads import SupportThreadRecord, ThreadKind, ThreadStatus
from nonebot_plugin_triage import handlers
from nonebot_plugin_triage.support_intake import normalize_support_request

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "evals" / "datasets" / "fixtures" / "support-language-contract-v1.json"
NOW = datetime(2026, 8, 12, tzinfo=UTC)


class _PlainTextEvent:
    def __init__(self, text: str) -> None:
        self._text = text

    def get_plaintext(self) -> str:
        return self._text


def _load_contract() -> dict[str, Any]:
    payload = json.loads(FIXTURES.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return cast(dict[str, Any], payload)


def _thread(case: dict[str, Any]) -> SupportThreadRecord:
    labels = cast(list[str], case.get("topic_labels", []))
    return SupportThreadRecord(
        thread_id=f"thread-{case['case_id']}",
        kind=ThreadKind(cast(str, case["turn_type"])),
        status=ThreadStatus.CONTINUABLE,
        topic_refs=handlers._encode_topic_labels(labels),
        created_at=NOW,
        last_active_at=NOW,
    )


def _deterministic_outcome(case: dict[str, Any]) -> tuple[str, str]:
    text = cast(str, case["text"])
    turn_type = cast(str, case["turn_type"])
    if turn_type == "initial":
        return "open_clarification", normalize_support_request(text).content

    thread = _thread(case)
    query = handlers._continuation_query(thread, text)
    return "close_unresolved", query


def test_support_language_fixture_is_public_deterministic_contract() -> None:
    payload = _load_contract()

    assert payload["schema_version"] == 1
    assert payload["fixture_set_id"] == "support-language-contract-v1"
    assert payload["evaluation_kind"] == "deterministic_framing_contract"
    assert payload["sample_origin"] == "maintainer_authored_production_like"
    assert payload["synthetic_only"] is True
    assert payload["contains_real_user_data"] is False
    assert payload["semantic_quality_eval"] is False
    assert payload["expected_external_tool_calls"] == 0
    assert payload["contract"] == {
        "command": "triage",
        "command_required_for_all_turns": True,
        "reply_alone_is_not_an_entry": True,
    }

    all_cases = [*payload["trigger_cases"], *payload["utterance_cases"]]
    case_ids = [case["case_id"] for case in all_cases]
    assert len(case_ids) == len(set(case_ids))


@pytest.mark.parametrize(
    "case", _load_contract()["trigger_cases"], ids=lambda case: case["case_id"]
)
def test_explicit_triage_trigger_contract(case: dict[str, Any]) -> None:
    event = cast(Event, _PlainTextEvent(cast(str, case["message"])))

    assert handlers._has_explicit_support_command(event) is case["expected_match"]


@pytest.mark.parametrize(
    "case",
    _load_contract()["utterance_cases"],
    ids=lambda case: case["case_id"],
)
def test_support_utterance_contract(case: dict[str, Any]) -> None:
    outcome, query = _deterministic_outcome(case)

    assert outcome == case["expected_outcome"]
    if "expected_context_query" in case:
        assert query == case["expected_context_query"]
