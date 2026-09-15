from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TypedDict

import pytest

from nbtriage.support.threads import (
    InMemorySupportThreadStore,
    OutboundThreadReferenceIndex,
    SupportThreadError,
    SupportThreadInitialContext,
    SupportThreadTurnCoordinator,
    ThreadKind,
    ThreadStatus,
    TurnClaimStatus,
)

_NOW = datetime(2026, 8, 14, 12, tzinfo=UTC)


class _SupportScope(TypedDict):
    adapter_name: str
    bot_scope: str
    conversation_scope: str
    actor_scope: str


def _support_scope() -> _SupportScope:
    return {
        "adapter_name": "adapter-raw",
        "bot_scope": "bot-raw",
        "conversation_scope": "conversation-raw",
        "actor_scope": "actor-raw",
    }


def _scope_turn_coordinator(
    *,
    ids: tuple[str, ...] = ("thread-1", "thread-2", "thread-3"),
    tokens: tuple[str, ...] = ("lease-1", "lease-2", "lease-3", "lease-4"),
) -> tuple[InMemorySupportThreadStore, SupportThreadTurnCoordinator]:
    thread_ids = iter(ids)
    lease_tokens = iter(tokens)
    store = InMemorySupportThreadStore(
        max_entries=8,
        idle_timeout_seconds=10,
        absolute_timeout_seconds=30,
        clock=lambda: _NOW,
        id_factory=lambda: next(thread_ids),
    )
    index = OutboundThreadReferenceIndex(
        secret_key=b"scope-index-secret-with-at-least-32-bytes",
        max_entries=8,
        retention_seconds=30,
    )
    return store, SupportThreadTurnCoordinator(
        store,
        index,
        secret_key=b"scope-lease-secret-with-at-least-32-bytes",
        lease_timeout_seconds=10,
        clock=lambda: _NOW,
        token_factory=lambda: next(lease_tokens),
    )


def test_scope_thread_keeps_bounded_initial_context_behind_hmac_scope() -> None:
    store, coordinator = _scope_turn_coordinator()
    context = SupportThreadInitialContext(
        request_text="搜图",
        reply_text="此前消息中的图片与说明",
        correlation_id="corr-first-operation",
    )

    claim = coordinator.claim_scope(
        **_support_scope(),
        create_kind=ThreadKind.CLARIFICATION,
        initial_context=context,
        now=_NOW,
    )

    assert claim.status is TurnClaimStatus.ACQUIRED
    assert claim.lease is not None
    assert claim.lease.is_supplement is False
    assert claim.lease.initial_context == context
    assert store.get(claim.lease.thread.thread_id, now=_NOW) == claim.lease.thread
    stored_scope_state = repr(
        (
            coordinator._thread_by_scope,
            coordinator._scope_by_thread,
            coordinator._leases_by_thread,
        )
    )
    for raw_component in (
        "adapter-raw",
        "bot-raw",
        "conversation-raw",
        "actor-raw",
    ):
        assert raw_component not in stored_scope_state

    with pytest.raises(SupportThreadError, match="at most 8000"):
        SupportThreadInitialContext(request_text="x" * 8_001)


def test_scope_thread_is_busy_then_allows_exactly_two_supplements() -> None:
    store, coordinator = _scope_turn_coordinator()
    context = SupportThreadInitialContext(request_text="搜图")
    first = coordinator.claim_scope(
        **_support_scope(),
        create_kind=ThreadKind.CLARIFICATION,
        initial_context=context,
        now=_NOW,
    )
    assert first.lease is not None

    busy = coordinator.claim_scope(
        **_support_scope(),
        create_kind=ThreadKind.CLARIFICATION,
        initial_context=SupportThreadInitialContext(request_text="不应建立第二个 Thread"),
        now=_NOW,
    )
    assert busy.status is TurnClaimStatus.BUSY
    assert len(store) == 1

    waiting = coordinator.await_supplement(
        first.lease.token,
        kind=ThreadKind.CLARIFICATION,
        topic_refs=("capability:image-search",),
        question="你使用的具体功能是什么？",
        now=_NOW + timedelta(seconds=1),
    )
    assert waiting is not None
    assert waiting.topic_refs == ("capability:image-search",)

    supplement = coordinator.claim_scope(
        **_support_scope(),
        create_kind=ThreadKind.CLARIFICATION,
        initial_context=SupportThreadInitialContext(request_text="不会覆盖首轮"),
        now=_NOW + timedelta(seconds=2),
    )
    assert supplement.status is TurnClaimStatus.ACQUIRED
    assert supplement.lease is not None
    assert supplement.lease.is_supplement is True
    assert supplement.lease.initial_context == replace(
        context, supplement_question="你使用的具体功能是什么？"
    )

    assert supplement.lease.can_ask is True
    assert (
        coordinator.await_supplement(
            supplement.lease.token,
            kind=ThreadKind.CLARIFICATION,
            question="实际出现了什么现象？",
            now=_NOW + timedelta(seconds=3),
        )
        is not None
    )
    last = coordinator.claim_scope(
        **_support_scope(),
        initial_context=SupportThreadInitialContext(request_text="没有响应"),
        now=_NOW + timedelta(seconds=3),
    )
    assert last.lease is not None
    assert last.lease.can_ask is False
    assert last.lease.thread.supplements_used == 2
    assert last.lease.initial_context is not None
    assert last.lease.initial_context.request_text == "搜图"
    assert last.lease.initial_context.supplements[0].request_text == "不会覆盖首轮"
    assert last.lease.initial_context.supplements[0].question == "你使用的具体功能是什么？"
    assert last.lease.initial_context.supplement_question == "实际出现了什么现象？"
    assert (
        coordinator.await_supplement(
            last.lease.token,
            kind=ThreadKind.CLARIFICATION,
            now=_NOW + timedelta(seconds=3),
        )
        is None
    )
    closed = store.get(first.lease.thread.thread_id, now=_NOW + timedelta(seconds=3))
    assert closed is not None and closed.status is ThreadStatus.CLOSED

    next_thread = coordinator.claim_scope(
        **_support_scope(),
        create_kind=ThreadKind.CLARIFICATION,
        initial_context=SupportThreadInitialContext(request_text="新的首轮"),
        now=_NOW + timedelta(seconds=4),
    )
    assert next_thread.lease is not None
    assert next_thread.lease.is_supplement is False
    assert next_thread.lease.thread.thread_id != first.lease.thread.thread_id


def test_scope_thread_requires_exact_scope_and_expires_before_supplement() -> None:
    store, coordinator = _scope_turn_coordinator()
    first = coordinator.claim_scope(
        **_support_scope(),
        create_kind=ThreadKind.CLARIFICATION,
        initial_context=SupportThreadInitialContext(request_text="首轮"),
        now=_NOW,
    )
    assert first.lease is not None
    assert (
        coordinator.await_supplement(
            first.lease.token,
            kind=ThreadKind.CLARIFICATION,
            now=_NOW + timedelta(seconds=1),
        )
        is not None
    )

    wrong_scope: _SupportScope = {
        **_support_scope(),
        "actor_scope": "another-actor",
    }
    assert (
        coordinator.claim_scope(
            **wrong_scope,
            now=_NOW + timedelta(seconds=2),
        ).status
        is TurnClaimStatus.NOT_FOUND
    )

    expired_replacement = coordinator.claim_scope(
        **_support_scope(),
        create_kind=ThreadKind.CLARIFICATION,
        initial_context=SupportThreadInitialContext(request_text="超时后的新首轮"),
        now=_NOW + timedelta(seconds=11),
    )
    assert expired_replacement.lease is not None
    assert expired_replacement.lease.is_supplement is False
    assert expired_replacement.lease.thread.thread_id != first.lease.thread.thread_id
    assert store.get(first.lease.thread.thread_id, now=_NOW + timedelta(seconds=11)) is None


def test_failed_second_question_does_not_reserve_another_supplement() -> None:
    store, coordinator = _scope_turn_coordinator()
    first = coordinator.claim_scope(
        **_support_scope(),
        create_kind=ThreadKind.CLARIFICATION,
        initial_context=SupportThreadInitialContext(request_text="下一页没反应"),
        now=_NOW,
    )
    assert first.lease is not None
    coordinator.await_supplement(
        first.lease.token, kind=ThreadKind.CLARIFICATION, question="过了多久？", now=_NOW
    )
    second = coordinator.claim_scope(
        **_support_scope(),
        initial_context=SupportThreadInitialContext(request_text="十秒"),
        now=_NOW,
    )
    assert second.lease is not None
    coordinator.fail_turn(second.lease.token, now=_NOW)
    record = store.get(first.lease.thread.thread_id, now=_NOW)
    assert record is not None and record.supplements_used == 1
    assert record.status is ThreadStatus.CLOSED
