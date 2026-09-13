from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from nbtriage.support.threads import (
    InMemorySupportThreadStore,
    OutboundThreadReferenceIndex,
    SupportThreadError,
    SupportThreadReferenceError,
    SupportThreadTurnCoordinator,
    ThreadKind,
    ThreadStatus,
    TurnClaimStatus,
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


def make_turn_coordinator(
    *,
    store: InMemorySupportThreadStore | None = None,
    index: OutboundThreadReferenceIndex | None = None,
    lease_timeout_seconds: int = 120,
) -> tuple[
    SupportThreadTurnCoordinator,
    InMemorySupportThreadStore,
    OutboundThreadReferenceIndex,
]:
    resolved_store = store if store is not None else make_store()
    resolved_index = index if index is not None else make_reference_index()
    coordinator = SupportThreadTurnCoordinator(
        resolved_store,
        resolved_index,
        secret_key=b"test-turn-lease-key-with-at-least-32-bytes",
        lease_timeout_seconds=lease_timeout_seconds,
        token_factory=lambda: "lease-1",
    )
    return coordinator, resolved_store, resolved_index


def reference_scope(message_reference: str = "message-1") -> dict[str, str]:
    return {
        "adapter_name": "onebot-v11",
        "bot_scope": "bot-SECRET-42",
        "conversation_scope": "group:SECRET-100",
        "actor_scope": "user:SECRET-7",
        "message_reference": message_reference,
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
        tuple(f"topic:{index}" for index in range(17)),
    ],
)
def test_thread_store_rejects_unbounded_or_non_opaque_topic_refs(topic_refs: object) -> None:
    store = make_store()

    with pytest.raises(SupportThreadError):
        store.create(ThreadKind.GUIDANCE, topic_refs=topic_refs, now=NOW)  # type: ignore[arg-type]


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


def test_turn_claim_is_exclusive_under_one_hundred_concurrent_attempts() -> None:
    coordinator, store, index = make_turn_coordinator()
    thread = store.create(ThreadKind.GUIDANCE, now=NOW)
    scope = reference_scope()
    index.bind(**scope, thread_id=thread.thread_id, now=NOW)

    with ThreadPoolExecutor(max_workers=16) as executor:
        results = tuple(
            executor.map(lambda _: coordinator.claim_reply(**scope, now=NOW), range(100))
        )

    assert sum(result.status is TurnClaimStatus.ACQUIRED for result in results) == 1
    assert sum(result.status is TurnClaimStatus.BUSY for result in results) == 99


def test_turn_claim_with_wrong_scope_does_not_consume_reference() -> None:
    coordinator, store, index = make_turn_coordinator()
    thread = store.create(ThreadKind.GUIDANCE, now=NOW)
    scope = reference_scope()
    index.bind(**scope, thread_id=thread.thread_id, now=NOW)

    wrong_scope = scope | {"actor_scope": "user:other"}

    assert coordinator.claim_reply(**wrong_scope, now=NOW).status is TurnClaimStatus.NOT_FOUND
    assert index.resolve(**scope, now=NOW) == thread.thread_id
    assert coordinator.claim_reply(**scope, now=NOW).status is TurnClaimStatus.ACQUIRED


def test_complete_turn_atomically_updates_context_and_publishes_next_reference() -> None:
    coordinator, store, index = make_turn_coordinator()
    thread = store.create(ThreadKind.CLARIFICATION, now=NOW)
    first_scope = reference_scope()
    index.bind(**first_scope, thread_id=thread.thread_id, now=NOW)
    claim = coordinator.claim_reply(**first_scope, now=NOW)
    assert claim.lease is not None
    next_scope = reference_scope("message-2")

    updated = coordinator.complete_turn(
        claim.lease.token,
        kind=ThreadKind.GUIDANCE,
        topic_refs=("capability:image-search",),
        **{key: value for key, value in next_scope.items() if key != "message_reference"},
        new_message_reference=next_scope["message_reference"],
        now=NOW + timedelta(seconds=1),
    )

    assert updated is not None
    assert updated.kind is ThreadKind.GUIDANCE
    assert updated.topic_refs == ("capability:image-search",)
    assert store.get(thread.thread_id, now=NOW + timedelta(seconds=1)) == updated
    assert index.resolve(**first_scope, now=NOW + timedelta(seconds=1)) is None
    assert index.resolve(**next_scope, now=NOW + timedelta(seconds=1)) == thread.thread_id
    assert coordinator.fail_turn(claim.lease.token, now=NOW + timedelta(seconds=1)) is False


def test_wrong_turn_token_does_not_change_active_turn() -> None:
    coordinator, store, index = make_turn_coordinator()
    thread = store.create(ThreadKind.GUIDANCE, now=NOW)
    scope = reference_scope()
    index.bind(**scope, thread_id=thread.thread_id, now=NOW)
    claim = coordinator.claim_reply(**scope, now=NOW)
    assert claim.lease is not None

    assert coordinator.fail_turn("lease-wrong", now=NOW + timedelta(seconds=1)) is False
    assert coordinator.fail_turn("invalid token", now=NOW + timedelta(seconds=1)) is False
    assert store.get(thread.thread_id, now=NOW + timedelta(seconds=1)) == thread
    assert coordinator.close_turn(claim.lease.token, now=NOW + timedelta(seconds=1)) is True


def test_active_turn_is_protected_from_capacity_but_not_idle_ttl() -> None:
    store = make_store(
        max_entries=1,
        idle_timeout_seconds=5,
        absolute_timeout_seconds=10,
        ids=["thread-1", "discarded", "thread-2"],
    )
    coordinator, _, index = make_turn_coordinator(
        store=store,
        index=make_reference_index(retention_seconds=30),
        lease_timeout_seconds=20,
    )
    thread = store.create(ThreadKind.GUIDANCE, now=NOW)
    scope = reference_scope()
    index.bind(**scope, thread_id=thread.thread_id, now=NOW)
    claim = coordinator.claim_reply(**scope, now=NOW)
    assert claim.lease is not None

    with pytest.raises(SupportThreadError, match="reserved by active turns"):
        store.create(ThreadKind.GUIDANCE, now=NOW + timedelta(seconds=4, microseconds=999_999))
    assert (
        store.get(thread.thread_id, now=NOW + timedelta(seconds=4, microseconds=999_999)) == thread
    )
    assert (
        coordinator.complete_turn(
            claim.lease.token,
            kind=ThreadKind.GUIDANCE,
            topic_refs=("capability:image-search",),
            **{key: value for key, value in scope.items() if key != "message_reference"},
            new_message_reference="message-2",
            now=NOW + timedelta(seconds=5),
        )
        is None
    )
    replacement = store.create(ThreadKind.GUIDANCE, now=NOW + timedelta(seconds=5))
    assert replacement.thread_id == "thread-2"
    assert store.get(thread.thread_id, now=NOW + timedelta(seconds=5)) is None
    assert not coordinator._leases_by_thread
    assert not coordinator._thread_by_token
    assert not coordinator._thread_by_reply
    assert store.dropped_count == 1
    assert (
        coordinator.complete_turn(
            claim.lease.token,
            kind=ThreadKind.GUIDANCE,
            topic_refs=(),
            **{key: value for key, value in scope.items() if key != "message_reference"},
            new_message_reference="message-3",
            now=NOW + timedelta(seconds=5),
        )
        is None
    )
    assert store.dropped_count == 1
    assert (
        index.resolve(
            **{**scope, "message_reference": "message-2"},
            now=NOW + timedelta(seconds=5),
        )
        is None
    )


def test_active_turn_lease_is_bounded_by_absolute_ttl() -> None:
    store = make_store(idle_timeout_seconds=10, absolute_timeout_seconds=10)
    coordinator, _, index = make_turn_coordinator(
        store=store,
        index=make_reference_index(retention_seconds=30),
        lease_timeout_seconds=20,
    )
    thread = store.create(ThreadKind.GUIDANCE, now=NOW)
    scope = reference_scope()
    index.bind(**scope, thread_id=thread.thread_id, now=NOW)
    claim = coordinator.claim_reply(**scope, now=NOW)
    assert claim.lease is not None
    assert claim.lease.expires_at == NOW + timedelta(seconds=10)

    assert (
        coordinator.complete_turn(
            claim.lease.token,
            kind=ThreadKind.GUIDANCE,
            topic_refs=("capability:image-search",),
            **{key: value for key, value in scope.items() if key != "message_reference"},
            new_message_reference="message-2",
            now=NOW + timedelta(seconds=10),
        )
        is None
    )
    assert store.get(thread.thread_id, now=NOW + timedelta(seconds=10)) is None
    assert not coordinator._leases_by_thread
    assert not coordinator._thread_by_token
    assert not coordinator._thread_by_reply
    assert (
        index.resolve(
            **{**scope, "message_reference": "message-2"},
            now=NOW + timedelta(seconds=10),
        )
        is None
    )


def test_initial_reference_binding_and_failure_are_fail_closed() -> None:
    coordinator, store, index = make_turn_coordinator()
    first = store.create(ThreadKind.GUIDANCE, now=NOW)
    second = store.create(ThreadKind.GUIDANCE, now=NOW)
    first_scope = reference_scope()
    index.bind(**first_scope, thread_id=first.thread_id, now=NOW)

    assert (
        coordinator.bind_initial_reference(
            **first_scope,
            thread_id=second.thread_id,
            now=NOW + timedelta(seconds=1),
        )
        is False
    )
    failed = store.get(second.thread_id, now=NOW + timedelta(seconds=1))
    assert failed is not None
    assert failed.status is ThreadStatus.CLOSED
    assert index.resolve(**first_scope, now=NOW + timedelta(seconds=1)) == first.thread_id

    third = store.create(ThreadKind.GUIDANCE, now=NOW + timedelta(seconds=2))
    assert coordinator.fail_initial(third.thread_id, now=NOW + timedelta(seconds=3)) is True
    assert coordinator.fail_initial(third.thread_id, now=NOW + timedelta(seconds=3)) is False


def test_pending_initial_is_protected_from_idle_but_not_absolute_ttl() -> None:
    store = make_store(
        max_entries=1,
        idle_timeout_seconds=5,
        absolute_timeout_seconds=10,
        ids=["thread-1", "discarded", "thread-2"],
    )
    coordinator, _, index = make_turn_coordinator(
        store=store,
        index=make_reference_index(retention_seconds=30),
        lease_timeout_seconds=20,
    )
    thread = coordinator.create_initial_thread(ThreadKind.GUIDANCE, now=NOW)
    assert coordinator._pending_initials[thread.thread_id] == NOW + timedelta(seconds=10)

    assert store.get(thread.thread_id, now=NOW + timedelta(seconds=9)) == thread
    with pytest.raises(SupportThreadError, match="reserved by active turns"):
        store.create(ThreadKind.GUIDANCE, now=NOW + timedelta(seconds=9))
    replacement = store.create(ThreadKind.GUIDANCE, now=NOW + timedelta(seconds=10))
    assert replacement.thread_id == "thread-2"
    assert not coordinator.bind_initial_reference(
        **reference_scope(),
        thread_id=thread.thread_id,
        now=NOW + timedelta(seconds=10),
    )
    assert store.get(thread.thread_id, now=NOW + timedelta(seconds=10)) is None
    assert thread.thread_id not in coordinator._pending_initials
    assert store.dropped_count == 1
    assert index.resolve(**reference_scope(), now=NOW + timedelta(seconds=10)) is None


def test_expired_turn_lease_closes_thread_and_rejects_late_completion() -> None:
    coordinator, store, index = make_turn_coordinator(lease_timeout_seconds=5)
    thread = store.create(ThreadKind.GUIDANCE, now=NOW)
    scope = reference_scope()
    index.bind(**scope, thread_id=thread.thread_id, now=NOW)
    claim = coordinator.claim_reply(**scope, now=NOW)
    assert claim.lease is not None

    assert (
        coordinator.claim_reply(**scope, now=NOW + timedelta(seconds=5)).status
        is TurnClaimStatus.NOT_FOUND
    )
    assert (
        coordinator.complete_turn(
            claim.lease.token,
            kind=ThreadKind.GUIDANCE,
            topic_refs=("capability:image-search",),
            **{key: value for key, value in scope.items() if key != "message_reference"},
            new_message_reference="message-2",
            now=NOW + timedelta(seconds=5),
        )
        is None
    )
    closed = store.get(thread.thread_id, now=NOW + timedelta(seconds=5))
    assert closed is not None
    assert closed.status is ThreadStatus.CLOSED


def test_complete_turn_with_wrong_scope_fails_closed_without_partial_context() -> None:
    coordinator, store, index = make_turn_coordinator()
    thread = store.create(ThreadKind.CLARIFICATION, topic_refs=("route:pending",), now=NOW)
    scope = reference_scope()
    index.bind(**scope, thread_id=thread.thread_id, now=NOW)
    claim = coordinator.claim_reply(**scope, now=NOW)
    assert claim.lease is not None

    assert (
        coordinator.complete_turn(
            claim.lease.token,
            kind=ThreadKind.GUIDANCE,
            topic_refs=("capability:image-search",),
            adapter_name=scope["adapter_name"],
            bot_scope=scope["bot_scope"],
            conversation_scope="group:other",
            actor_scope=scope["actor_scope"],
            new_message_reference="message-2",
            now=NOW + timedelta(seconds=1),
        )
        is None
    )
    closed = store.get(thread.thread_id, now=NOW + timedelta(seconds=1))
    assert closed is not None
    assert closed.status is ThreadStatus.CLOSED
    assert closed.kind is ThreadKind.CLARIFICATION
    assert closed.topic_refs == ("route:pending",)
    assert index.resolve(**reference_scope("message-2"), now=NOW + timedelta(seconds=1)) is None


def test_complete_turn_cannot_republish_consumed_reply_reference() -> None:
    coordinator, store, index = make_turn_coordinator()
    thread = store.create(ThreadKind.GUIDANCE, now=NOW)
    scope = reference_scope()
    index.bind(**scope, thread_id=thread.thread_id, now=NOW)
    claim = coordinator.claim_reply(**scope, now=NOW)
    assert claim.lease is not None

    assert (
        coordinator.complete_turn(
            claim.lease.token,
            kind=ThreadKind.GUIDANCE,
            topic_refs=(),
            **{key: value for key, value in scope.items() if key != "message_reference"},
            new_message_reference=scope["message_reference"],
            now=NOW + timedelta(seconds=1),
        )
        is None
    )
    closed = store.get(thread.thread_id, now=NOW + timedelta(seconds=1))
    assert closed is not None
    assert closed.status is ThreadStatus.CLOSED
    assert index.resolve(**scope, now=NOW + timedelta(seconds=1)) is None


def test_turn_coordinator_state_does_not_retain_raw_scope_or_message_reference() -> None:
    coordinator, store, index = make_turn_coordinator()
    thread = store.create(ThreadKind.GUIDANCE, now=NOW)
    scope = reference_scope("MESSAGE-SECRET-900")
    index.bind(**scope, thread_id=thread.thread_id, now=NOW)
    claim = coordinator.claim_reply(**scope, now=NOW)

    assert claim.status is TurnClaimStatus.ACQUIRED
    serialized_state = repr(
        (
            coordinator._leases_by_thread,
            coordinator._thread_by_token,
            coordinator._thread_by_reply,
        )
    )
    assert "SECRET" not in serialized_state


def test_claim_internal_failure_after_resolution_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, store, index = make_turn_coordinator()
    thread = store.create(ThreadKind.GUIDANCE, now=NOW)
    scope = reference_scope()
    index.bind(**scope, thread_id=thread.thread_id, now=NOW)

    def fail_claim_stage(*_: object, **__: object) -> None:
        raise RuntimeError("simulated index_consume failure")

    with monkeypatch.context() as patch:
        patch.setattr(index, "consume_digest", fail_claim_stage)
        with pytest.raises(RuntimeError, match="index_consume"):
            coordinator.claim_reply(**scope, now=NOW)

    closed = store.get(thread.thread_id, now=NOW)
    assert closed is not None
    assert closed.status is ThreadStatus.CLOSED
    assert index.resolve(**scope, now=NOW) is None
    assert thread.thread_id not in coordinator._pending_initials
    assert thread.thread_id not in coordinator._leases_by_thread
    assert thread.thread_id not in store._protected_until
    assert thread.thread_id not in store._idle_protected_until
    assert thread.thread_id not in index._protected_until
    assert not coordinator._thread_by_token
    assert not coordinator._thread_by_reply
