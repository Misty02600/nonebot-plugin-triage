from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from nbtriage.support_threads import (
    InMemorySupportThreadStore,
    OutboundThreadReferenceIndex,
    SupportThreadError,
    SupportThreadReferenceError,
    ThreadKind,
    ThreadStatus,
)

NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)


def make_store(
    *,
    max_entries: int = 4,
    idle_timeout_seconds: int = 900,
    absolute_timeout_seconds: int = 1_800,
    ids: list[str] | None = None,
) -> InMemorySupportThreadStore:
    tokens = iter(ids or ["thread-1", "thread-2", "thread-3", "thread-4"])
    return InMemorySupportThreadStore(
        max_entries=max_entries,
        idle_timeout_seconds=idle_timeout_seconds,
        absolute_timeout_seconds=absolute_timeout_seconds,
        id_factory=lambda: next(tokens),
    )


def make_reference_index(
    *,
    max_entries: int = 4,
    retention_seconds: int = 1_800,
) -> OutboundThreadReferenceIndex:
    return OutboundThreadReferenceIndex(
        secret_key=b"test-thread-key-with-at-least-32-bytes",
        max_entries=max_entries,
        retention_seconds=retention_seconds,
    )


def test_thread_store_creates_minimal_continuable_record_without_message_text() -> None:
    store = make_store()

    record = store.create(
        ThreadKind.GUIDANCE,
        topic_refs=("capability:image-search", "source:snapshot-1"),
        now=NOW,
    )

    assert record.thread_id == "thread-1"
    assert record.kind is ThreadKind.GUIDANCE
    assert record.status is ThreadStatus.CONTINUABLE
    assert record.topic_refs == ("capability:image-search", "source:snapshot-1")
    assert record.created_at == NOW
    assert record.last_active_at == NOW
    assert set(record.__dict__) == {
        "thread_id",
        "kind",
        "status",
        "topic_refs",
        "created_at",
        "last_active_at",
    }


def test_get_is_read_only_while_touch_extends_idle_ttl_but_not_absolute_ttl() -> None:
    store = make_store(idle_timeout_seconds=10, absolute_timeout_seconds=30)
    record = store.create(ThreadKind.CLARIFICATION, now=NOW)

    assert store.get(record.thread_id, now=NOW + timedelta(seconds=9)) == record
    touched = store.touch(record.thread_id, now=NOW + timedelta(seconds=9))
    assert touched is not None
    assert touched.last_active_at == NOW + timedelta(seconds=9)
    assert store.get(record.thread_id, now=NOW + timedelta(seconds=18)) == touched
    assert store.get(record.thread_id, now=NOW + timedelta(seconds=19)) is None

    second = store.create(ThreadKind.GUIDANCE, now=NOW + timedelta(seconds=40))
    assert store.touch(second.thread_id, now=NOW + timedelta(seconds=49)) is not None
    assert store.touch(second.thread_id, now=NOW + timedelta(seconds=58)) is not None
    assert store.get(second.thread_id, now=NOW + timedelta(seconds=70)) is None


def test_close_is_idempotent_and_closed_threads_cannot_be_touched() -> None:
    store = make_store()
    record = store.create(ThreadKind.GUIDANCE, now=NOW)

    closed = store.close(record.thread_id, now=NOW + timedelta(seconds=5))

    assert closed is not None
    assert closed.status is ThreadStatus.CLOSED
    assert store.close(record.thread_id, now=NOW + timedelta(seconds=6)) == closed
    assert store.touch(record.thread_id, now=NOW + timedelta(seconds=7)) is None
    assert store.get(record.thread_id, now=NOW + timedelta(seconds=7)) == closed


def test_out_of_order_touch_does_not_move_last_activity_backwards() -> None:
    store = make_store()
    record = store.create(ThreadKind.GUIDANCE, now=NOW)
    newest = store.touch(record.thread_id, now=NOW + timedelta(seconds=10))

    older = store.touch(record.thread_id, now=NOW + timedelta(seconds=5))

    assert newest is not None
    assert older is not None
    assert older.last_active_at == newest.last_active_at


def test_update_context_changes_kind_and_topics_atomically() -> None:
    store = make_store()
    record = store.create(
        ThreadKind.CLARIFICATION,
        topic_refs=("route:pending",),
        now=NOW,
    )

    updated = store.update_context(
        record.thread_id,
        ThreadKind.GUIDANCE,
        ("capability:image-search", "source:snapshot-1"),
        now=NOW + timedelta(seconds=10),
    )

    assert updated is not None
    assert updated.kind is ThreadKind.GUIDANCE
    assert updated.topic_refs == ("capability:image-search", "source:snapshot-1")
    assert updated.last_active_at == NOW + timedelta(seconds=10)
    assert store.get(record.thread_id, now=NOW + timedelta(seconds=10)) == updated


def test_update_context_cannot_revive_closed_or_expired_thread() -> None:
    store = make_store(idle_timeout_seconds=10, absolute_timeout_seconds=30)
    closed = store.create(ThreadKind.CLARIFICATION, now=NOW)
    assert store.close(closed.thread_id, now=NOW + timedelta(seconds=1)) is not None

    assert (
        store.update_context(
            closed.thread_id,
            ThreadKind.GUIDANCE,
            ("capability:image-search",),
            now=NOW + timedelta(seconds=2),
        )
        is None
    )

    expired = store.create(ThreadKind.CLARIFICATION, now=NOW + timedelta(seconds=40))
    assert (
        store.update_context(
            expired.thread_id,
            ThreadKind.GUIDANCE,
            ("capability:image-search",),
            now=NOW + timedelta(seconds=50),
        )
        is None
    )


def test_update_context_is_monotonic_and_updates_capacity_order() -> None:
    store = make_store(max_entries=2, ids=["thread-1", "thread-2", "thread-3"])
    first = store.create(ThreadKind.CLARIFICATION, now=NOW)
    second = store.create(ThreadKind.GUIDANCE, now=NOW + timedelta(seconds=1))
    newest = store.update_context(
        first.thread_id,
        ThreadKind.GUIDANCE,
        ("capability:image-search",),
        now=NOW + timedelta(seconds=3),
    )
    assert newest is not None

    older = store.update_context(
        first.thread_id,
        ThreadKind.CLARIFICATION,
        ("route:pending",),
        now=NOW + timedelta(seconds=2),
    )
    assert older is not None
    assert older.last_active_at == newest.last_active_at
    third = store.create(ThreadKind.GUIDANCE, now=NOW + timedelta(seconds=4))

    assert store.get(second.thread_id, now=NOW + timedelta(seconds=4)) is None
    assert store.get(first.thread_id, now=NOW + timedelta(seconds=4)) == older
    assert store.get(third.thread_id, now=NOW + timedelta(seconds=4)) == third


def test_thread_store_evicts_least_recently_active_record_at_capacity() -> None:
    store = make_store(max_entries=2, ids=["thread-1", "thread-2", "thread-3"])
    first = store.create(ThreadKind.GUIDANCE, now=NOW)
    second = store.create(ThreadKind.GUIDANCE, now=NOW + timedelta(seconds=1))
    assert store.touch(first.thread_id, now=NOW + timedelta(seconds=2)) is not None

    third = store.create(ThreadKind.GUIDANCE, now=NOW + timedelta(seconds=3))

    assert store.get(second.thread_id, now=NOW + timedelta(seconds=3)) is None
    assert store.get(first.thread_id, now=NOW + timedelta(seconds=3)) is not None
    assert store.get(third.thread_id, now=NOW + timedelta(seconds=3)) is not None
    assert store.dropped_count == 1


@pytest.mark.parametrize(
    "topic_refs",
    [
        "raw-text-is-not-a-sequence",
        ("not allowed whitespace",),
        ("duplicate", "duplicate"),
        tuple(f"topic:{index}" for index in range(17)),
        tuple(f"topic-{index}-" + "x" * 110 for index in range(9)),
    ],
)
def test_thread_store_rejects_unbounded_or_non_opaque_topic_refs(topic_refs: object) -> None:
    store = make_store()

    with pytest.raises(SupportThreadError):
        store.create(ThreadKind.GUIDANCE, topic_refs=topic_refs, now=NOW)  # type: ignore[arg-type]


def test_thread_store_rejects_invalid_policy_and_naive_time() -> None:
    with pytest.raises(SupportThreadError, match="must not be shorter"):
        make_store(idle_timeout_seconds=11, absolute_timeout_seconds=10)
    store = make_store()
    with pytest.raises(SupportThreadError, match="timezone"):
        store.create(ThreadKind.GUIDANCE, now=NOW.replace(tzinfo=None))


def test_outbound_reference_requires_exact_actor_bot_scene_and_adapter_scope() -> None:
    index = make_reference_index()
    values = {
        "adapter_name": "onebot-v11",
        "bot_scope": "bot-SECRET-42",
        "conversation_scope": "group:SECRET-100",
        "actor_scope": "user:SECRET-7",
        "message_reference": "MESSAGE-SECRET-900",
    }
    index.bind(**values, thread_id="thread-1", now=NOW)

    assert index.resolve(**values, now=NOW) == "thread-1"
    for field in (
        "adapter_name",
        "bot_scope",
        "conversation_scope",
        "actor_scope",
        "message_reference",
    ):
        mismatched = values | {field: f"other-{field}"}
        assert index.resolve(**mismatched, now=NOW) is None

    stored_state = repr(index._entries)
    assert "SECRET" not in stored_state


def test_outbound_reference_prunes_ttl_and_capacity_and_rejects_conflicts() -> None:
    index = make_reference_index(max_entries=1, retention_seconds=5)
    common = {
        "adapter_name": "onebot-v11",
        "bot_scope": "bot-1",
        "conversation_scope": "group:1",
        "actor_scope": "user:1",
    }
    first = common | {"message_reference": "message-1"}
    second = common | {"message_reference": "message-2"}
    index.bind(**first, thread_id="thread-1", now=NOW)
    with pytest.raises(SupportThreadReferenceError, match="another thread"):
        index.bind(**first, thread_id="thread-2", now=NOW)

    index.bind(**second, thread_id="thread-2", now=NOW)
    assert index.resolve(**first, now=NOW) is None
    assert index.dropped_count == 1
    assert index.resolve(**second, now=NOW + timedelta(seconds=5)) is None
    assert index.dropped_count == 2


def test_outbound_reference_keeps_only_latest_answer_for_each_thread() -> None:
    index = make_reference_index(max_entries=4)
    common = {
        "adapter_name": "onebot-v11",
        "bot_scope": "bot-1",
        "conversation_scope": "group:1",
        "actor_scope": "user:1",
    }
    first = common | {"message_reference": "message-1"}
    latest = common | {"message_reference": "message-2"}

    index.bind(**first, thread_id="thread-1", now=NOW)
    index.bind(**latest, thread_id="thread-1", now=NOW + timedelta(seconds=1))

    assert index.resolve(**first, now=NOW + timedelta(seconds=1)) is None
    assert index.resolve(**latest, now=NOW + timedelta(seconds=1)) == "thread-1"
    assert len(index) == 1


def test_thread_store_serializes_concurrent_creates() -> None:
    sequence = iter(f"thread-{index}" for index in range(100))
    store = InMemorySupportThreadStore(
        max_entries=100,
        idle_timeout_seconds=900,
        absolute_timeout_seconds=1_800,
        id_factory=lambda: next(sequence),
    )

    with ThreadPoolExecutor(max_workers=8) as executor:
        records = tuple(
            executor.map(
                lambda _: store.create(ThreadKind.GUIDANCE, now=NOW),
                range(100),
            )
        )

    assert len(records) == 100
    assert len({record.thread_id for record in records}) == 100
    assert len(store) == 100
