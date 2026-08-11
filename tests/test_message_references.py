from datetime import UTC, datetime, timedelta

import pytest

from nbtriage.message_references import (
    MessageReferenceError,
    PlatformMessageReferenceIndex,
)


def make_index(*, max_entries: int = 4, retention_seconds: int = 60):
    return PlatformMessageReferenceIndex(
        secret_key=b"test-key-with-at-least-thirty-two-bytes",
        max_entries=max_entries,
        retention_seconds=retention_seconds,
    )


def test_reference_index_resolves_exact_scope_without_storing_raw_reference() -> None:
    index = make_index()
    now = datetime(2026, 8, 9, 2, 0, tzinfo=UTC)

    index.bind(
        adapter_name="onebot-v11",
        bot_scope="bot-SECRET-42",
        conversation_scope="group:SECRET-100",
        message_reference="MESSAGE-SECRET-900",
        correlation_id="corr-1",
        now=now,
    )

    assert (
        index.resolve(
            adapter_name="onebot-v11",
            bot_scope="bot-SECRET-42",
            conversation_scope="group:SECRET-100",
            message_reference="MESSAGE-SECRET-900",
            now=now,
        )
        == "corr-1"
    )
    stored_state = repr(index._entries)
    assert "bot-SECRET-42" not in stored_state
    assert "group:SECRET-100" not in stored_state
    assert "MESSAGE-SECRET-900" not in stored_state


@pytest.mark.parametrize(
    "overrides",
    [
        {"bot_scope": "other-bot"},
        {"conversation_scope": "group:other"},
        {"message_reference": "other-message"},
        {"adapter_name": "other-adapter"},
    ],
)
def test_reference_index_rejects_cross_scope_lookup(overrides: dict[str, str]) -> None:
    index = make_index()
    now = datetime(2026, 8, 9, 2, 0, tzinfo=UTC)
    values = {
        "adapter_name": "onebot-v11",
        "bot_scope": "bot-1",
        "conversation_scope": "group:1",
        "message_reference": "message-1",
    }
    index.bind(**values, correlation_id="corr-1", now=now)

    assert index.resolve(**(values | overrides), now=now) is None


def test_reference_index_prunes_ttl_and_capacity_with_visible_loss() -> None:
    index = make_index(max_entries=1, retention_seconds=5)
    now = datetime(2026, 8, 9, 2, 0, tzinfo=UTC)
    common = {
        "adapter_name": "onebot-v11",
        "bot_scope": "bot-1",
        "conversation_scope": "group:1",
    }
    index.bind(**common, message_reference="message-1", correlation_id="corr-1", now=now)
    index.bind(**common, message_reference="message-2", correlation_id="corr-2", now=now)

    assert index.resolve(**common, message_reference="message-1", now=now) is None
    assert index.dropped_count == 1
    assert (
        index.resolve(
            **common,
            message_reference="message-2",
            now=now + timedelta(seconds=6),
        )
        is None
    )
    assert index.dropped_count == 2


def test_reference_index_rejects_conflicting_rebind() -> None:
    index = make_index()
    now = datetime(2026, 8, 9, 2, 0, tzinfo=UTC)
    values = {
        "adapter_name": "onebot-v11",
        "bot_scope": "bot-1",
        "conversation_scope": "group:1",
        "message_reference": "message-1",
    }
    index.bind(**values, correlation_id="corr-1", now=now)

    with pytest.raises(MessageReferenceError, match="already bound"):
        index.bind(**values, correlation_id="corr-2", now=now)

    assert index.resolve(**values, now=now) == "corr-1"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"secret_key": b"short", "max_entries": 1, "retention_seconds": 1},
        {
            "secret_key": b"x" * 32,
            "max_entries": 0,
            "retention_seconds": 1,
        },
        {
            "secret_key": b"x" * 32,
            "max_entries": 1,
            "retention_seconds": 0,
        },
    ],
)
def test_reference_index_requires_explicit_bounded_policy(kwargs: dict) -> None:
    with pytest.raises(MessageReferenceError):
        PlatformMessageReferenceIndex(**kwargs)
