from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message
from nonebot.adapters.onebot.v11.event import Reply as OneBotReply
from nonebot.adapters.onebot.v11.event import Sender
from nonebot.matcher import Matcher, current_matcher
from nonebot_plugin_alconna import (
    Reply,
    SupportAdapter,
    SupportScope,
    Target,
    UniMessage,
    get_target,
)

import nonebot_plugin_triage.onebot_v11_references as onebot_references
from nbtriage.message_references import PlatformMessageReferenceIndex
from nbtriage.support_threads import (
    InMemorySupportThreadStore,
    OutboundThreadReferenceIndex,
    SupportThreadTurnCoordinator,
    ThreadKind,
    ThreadStatus,
    TurnClaimStatus,
)
from nonebot_plugin_triage.nonebot_runtime import NBTRIAGE_CORRELATION_STATE_KEY
from nonebot_plugin_triage.onebot_v11_references import (
    ONEBOT_V11_ADAPTER_NAME,
    OneBotV11OutgoingReferenceProvider,
    OneBotV11OutgoingReferenceProviderError,
)
from nonebot_plugin_triage.thread_references import (
    NBTRIAGE_THREAD_BINDING_STATE_KEY,
    InitialThreadBinding,
    PendingContinuationBinding,
    PreparedContinuationBinding,
    SupportThreadReferenceBridge,
)
from nonebot_plugin_triage.universal_references import UniversalReferenceBridge, conversation_scope


def make_index() -> PlatformMessageReferenceIndex:
    return PlatformMessageReferenceIndex(
        secret_key=b"test-key-with-at-least-thirty-two-bytes",
        max_entries=16,
        retention_seconds=60,
    )


def make_thread_bridge() -> SupportThreadReferenceBridge:
    return SupportThreadReferenceBridge(
        OutboundThreadReferenceIndex(
            secret_key=b"test-thread-key-with-at-least-32-bytes",
            max_entries=16,
            retention_seconds=60,
        )
    )


def make_thread_runtime() -> tuple[
    InMemorySupportThreadStore,
    OutboundThreadReferenceIndex,
    SupportThreadTurnCoordinator,
    SupportThreadReferenceBridge,
]:
    store = InMemorySupportThreadStore(
        max_entries=16,
        idle_timeout_seconds=60,
        absolute_timeout_seconds=120,
    )
    index = OutboundThreadReferenceIndex(
        secret_key=b"test-thread-key-with-at-least-32-bytes",
        max_entries=16,
        retention_seconds=120,
    )
    coordinator = SupportThreadTurnCoordinator(
        store,
        index,
        secret_key=b"test-turn-key-with-at-least-32-bytes",
    )
    return store, index, coordinator, SupportThreadReferenceBridge(coordinator)


def make_bot(self_id: str = "4200") -> Bot:
    adapter = SimpleNamespace(get_name=lambda: ONEBOT_V11_ADAPTER_NAME)
    return Bot(adapter=adapter, self_id=self_id)  # type: ignore[arg-type]


def make_group_event(*, message_id: int, reply_message_id: int | None = None) -> GroupMessageEvent:
    sender = Sender(user_id=200, nickname="tester")
    reply = (
        OneBotReply(
            time=1,
            message_type="group",
            message_id=reply_message_id,
            real_id=reply_message_id,
            sender=sender,
            message=Message("REPLIED_CONTENT_MUST_NOT_BE_READ"),
        )
        if reply_message_id is not None
        else None
    )
    return GroupMessageEvent(
        time=1,
        self_id=4200,
        post_type="message",
        sub_type="normal",
        user_id=200,
        message_type="group",
        message_id=message_id,
        message=Message("报错"),
        original_message=Message("报错"),
        raw_message="报错",
        font=0,
        sender=sender,
        group_id=100,
        reply=reply,
        to_me=True,
    )


def group_target(group_id: str = "100", self_id: str = "4200") -> Target:
    return Target(
        group_id,
        self_id=self_id,
        scope=SupportScope.qq_client,
        adapter=SupportAdapter.onebot11,
    )


@pytest.mark.anyio
async def test_matcher_send_result_binds_only_routing_fields_and_message_id() -> None:
    index = make_index()
    bridge = UniversalReferenceBridge(index)
    provider = OneBotV11OutgoingReferenceProvider(bridge)
    bot = make_bot()
    matcher = cast(
        Matcher,
        SimpleNamespace(state={NBTRIAGE_CORRELATION_STATE_KEY: "corr-outgoing"}),
    )
    token = current_matcher.set(matcher)
    try:
        await provider.bind_outgoing_group_message(
            bot,
            None,
            "send_msg",
            {
                "message_type": "group",
                "group_id": 100,
                "message": "API_DATA_MUST_NOT_BE_STORED",
            },
            {"message_id": 601, "echo": "API_RESULT_MUST_NOT_BE_STORED"},
        )
    finally:
        current_matcher.reset(token)

    assert (
        bridge.resolve_reply(
            adapter_name=ONEBOT_V11_ADAPTER_NAME,
            bot_scope="4200",
            target=group_target(),
            message_reference="601",
        )
        == "corr-outgoing"
    )
    assert len(index._entries) == 1
    digest, entry = next(iter(index._entries.items()))
    assert len(digest) == 64
    assert set(digest) <= set("0123456789abcdef")
    assert vars(entry).keys() == {"correlation_id", "stored_at"}
    assert entry.correlation_id == "corr-outgoing"


@pytest.mark.anyio
async def test_matcher_send_result_also_binds_thread_to_actor_scope() -> None:
    bridge = UniversalReferenceBridge(make_index())
    thread_bridge = make_thread_bridge()
    provider = OneBotV11OutgoingReferenceProvider(bridge, thread_bridge=thread_bridge)
    bot = make_bot()
    matcher = cast(
        Matcher,
        SimpleNamespace(
            state={NBTRIAGE_THREAD_BINDING_STATE_KEY: InitialThreadBinding("thread-1", "actor-200")}
        ),
    )
    token = current_matcher.set(matcher)
    try:
        await provider.bind_outgoing_group_message(
            bot,
            None,
            "send_group_msg",
            {"group_id": 100},
            {"message_id": 602},
        )
    finally:
        current_matcher.reset(token)

    assert (
        thread_bridge.resolve_reply(
            adapter_name=ONEBOT_V11_ADAPTER_NAME,
            bot_scope="4200",
            target=group_target(),
            actor_scope="actor-200",
            message_reference="602",
        )
        == "thread-1"
    )
    assert (
        thread_bridge.resolve_reply(
            adapter_name=ONEBOT_V11_ADAPTER_NAME,
            bot_scope="4200",
            target=group_target(),
            actor_scope="actor-201",
            message_reference="602",
        )
        is None
    )


@pytest.mark.anyio
async def test_initial_thread_send_uses_coordinator_and_binds_reference() -> None:
    store, _, _, thread_bridge = make_thread_runtime()
    provider = OneBotV11OutgoingReferenceProvider(
        UniversalReferenceBridge(make_index()),
        thread_bridge=thread_bridge,
    )
    thread = store.create(ThreadKind.GUIDANCE)
    matcher = cast(
        Matcher,
        SimpleNamespace(
            state={
                NBTRIAGE_THREAD_BINDING_STATE_KEY: InitialThreadBinding(
                    thread.thread_id,
                    "actor-200",
                )
            }
        ),
    )

    token = current_matcher.set(matcher)
    try:
        await provider.bind_outgoing_group_message(
            make_bot(),
            None,
            "send_group_msg",
            {"group_id": 100},
            {"message_id": 602},
        )
    finally:
        current_matcher.reset(token)

    current = store.get(thread.thread_id)
    assert current is not None
    assert current.status is ThreadStatus.CONTINUABLE
    assert NBTRIAGE_THREAD_BINDING_STATE_KEY not in matcher.state
    assert (
        thread_bridge.resolve_reply(
            adapter_name=ONEBOT_V11_ADAPTER_NAME,
            bot_scope="4200",
            target=group_target(),
            actor_scope="actor-200",
            message_reference="602",
        )
        == thread.thread_id
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("exception", "result"),
    [
        (RuntimeError("send failed"), {"message_id": 602}),
        (None, {"not_message_id": 602}),
    ],
)
async def test_failed_initial_thread_send_closes_thread(
    exception: Exception | None,
    result: dict[str, int],
) -> None:
    store, _, _, thread_bridge = make_thread_runtime()
    provider = OneBotV11OutgoingReferenceProvider(
        UniversalReferenceBridge(make_index()),
        thread_bridge=thread_bridge,
    )
    thread = store.create(ThreadKind.GUIDANCE)
    matcher = cast(
        Matcher,
        SimpleNamespace(
            state={
                NBTRIAGE_THREAD_BINDING_STATE_KEY: InitialThreadBinding(
                    thread.thread_id,
                    "actor-200",
                )
            }
        ),
    )

    token = current_matcher.set(matcher)
    try:
        await provider.bind_outgoing_group_message(
            make_bot(),
            exception,
            "send_group_msg",
            {"group_id": 100},
            result,
        )
    finally:
        current_matcher.reset(token)

    closed = store.get(thread.thread_id)
    assert closed is not None
    assert closed.status is ThreadStatus.CLOSED
    assert NBTRIAGE_THREAD_BINDING_STATE_KEY not in matcher.state
    assert provider.dropped_count == 1
    assert (
        thread_bridge.resolve_reply(
            adapter_name=ONEBOT_V11_ADAPTER_NAME,
            bot_scope="4200",
            target=group_target(),
            actor_scope="actor-200",
            message_reference="602",
        )
        is None
    )


@pytest.mark.anyio
async def test_run_postprocessor_closes_unsettled_initial_thread() -> None:
    store, _, _, thread_bridge = make_thread_runtime()
    thread = store.create(ThreadKind.CLARIFICATION)
    state: dict[str, Any] = {
        NBTRIAGE_THREAD_BINDING_STATE_KEY: InitialThreadBinding(
            thread.thread_id,
            "actor-200",
        )
    }

    await thread_bridge.cleanup_unsettled_binding(
        cast(Matcher, SimpleNamespace(state=state)),
        state,
        RuntimeError("matcher ended before send"),
    )

    closed = store.get(thread.thread_id)
    assert closed is not None
    assert closed.status is ThreadStatus.CLOSED
    assert NBTRIAGE_THREAD_BINDING_STATE_KEY not in state
    assert thread_bridge.dropped_count == 1


@pytest.mark.anyio
async def test_uniseg_reply_and_target_match_onebot_outgoing_scope() -> None:
    bot = make_bot()
    event = make_group_event(message_id=602, reply_message_id=601)

    message = UniMessage.of(event.original_message, bot=bot)
    message = await message.attach_reply(event=event, bot=bot)
    incoming_target = get_target(event=event, bot=bot)

    assert message.get(Reply, 1)[0].id == "601"
    assert conversation_scope(incoming_target) == conversation_scope(group_target())


@pytest.mark.anyio
async def test_provider_ignores_failed_non_group_and_contextless_sends() -> None:
    bridge = UniversalReferenceBridge(make_index())
    provider = OneBotV11OutgoingReferenceProvider(bridge)
    bot = make_bot()

    await provider.bind_outgoing_group_message(
        bot, RuntimeError("failed"), "send_group_msg", {"group_id": 100}, {"message_id": 1}
    )
    await provider.bind_outgoing_group_message(
        bot, None, "send_private_msg", {"user_id": 200}, {"message_id": 2}
    )
    await provider.bind_outgoing_group_message(
        bot, None, "send_group_msg", {"group_id": 100}, {"message_id": 3}
    )

    assert len(bridge.index) == 0
    assert provider.dropped_count == 0


@pytest.mark.anyio
async def test_malformed_result_is_fail_open() -> None:
    provider = OneBotV11OutgoingReferenceProvider(UniversalReferenceBridge(make_index()))
    matcher = cast(
        Matcher,
        SimpleNamespace(state={NBTRIAGE_CORRELATION_STATE_KEY: "corr-malformed"}),
    )
    token = current_matcher.set(matcher)
    try:
        await provider.bind_outgoing_group_message(
            make_bot(),
            None,
            "send_group_msg",
            {"group_id": 100},
            {"not_message_id": 1},
        )
    finally:
        current_matcher.reset(token)

    assert provider.dropped_count == 1


@pytest.mark.anyio
async def test_successful_continuation_send_completes_and_rebinds_once() -> None:
    store, _, coordinator, thread_bridge = make_thread_runtime()
    provider = OneBotV11OutgoingReferenceProvider(
        UniversalReferenceBridge(make_index()),
        thread_bridge=thread_bridge,
    )
    thread = store.create(ThreadKind.GUIDANCE, topic_refs=("topic-old",))
    assert coordinator.bind_initial_reference(
        adapter_name=ONEBOT_V11_ADAPTER_NAME,
        bot_scope="4200",
        conversation_scope=conversation_scope(group_target()),
        actor_scope="actor-200",
        message_reference="601",
        thread_id=thread.thread_id,
    )
    claim = thread_bridge.claim_reply(
        adapter_name=ONEBOT_V11_ADAPTER_NAME,
        bot_scope="4200",
        target=group_target(),
        actor_scope="actor-200",
        message_reference="601",
    )
    assert claim.status is TurnClaimStatus.ACQUIRED
    assert claim.lease is not None
    matcher = cast(
        Matcher,
        SimpleNamespace(
            state={
                NBTRIAGE_THREAD_BINDING_STATE_KEY: PreparedContinuationBinding(
                    claim.lease.token,
                    "actor-200",
                    ThreadKind.GUIDANCE,
                    ("topic-next",),
                )
            }
        ),
    )

    token = current_matcher.set(matcher)
    try:
        await provider.bind_outgoing_group_message(
            make_bot(),
            None,
            "send_group_msg",
            {"group_id": 100},
            {"message_id": 602},
        )
        await provider.bind_outgoing_group_message(
            make_bot(),
            None,
            "send_group_msg",
            {"group_id": 100},
            {"message_id": 603},
        )
    finally:
        current_matcher.reset(token)

    updated = store.get(thread.thread_id)
    assert updated is not None
    assert updated.topic_refs == ("topic-next",)
    assert NBTRIAGE_THREAD_BINDING_STATE_KEY not in matcher.state
    assert (
        thread_bridge.resolve_reply(
            adapter_name=ONEBOT_V11_ADAPTER_NAME,
            bot_scope="4200",
            target=group_target(),
            actor_scope="actor-200",
            message_reference="602",
        )
        == thread.thread_id
    )
    assert (
        thread_bridge.resolve_reply(
            adapter_name=ONEBOT_V11_ADAPTER_NAME,
            bot_scope="4200",
            target=group_target(),
            actor_scope="actor-200",
            message_reference="603",
        )
        is None
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("exception", "result"),
    [
        (RuntimeError("send failed"), {"message_id": 602}),
        (None, {"not_message_id": 602}),
    ],
)
async def test_failed_continuation_send_closes_claimed_thread(
    exception: Exception | None,
    result: dict[str, int],
) -> None:
    store, _, coordinator, thread_bridge = make_thread_runtime()
    provider = OneBotV11OutgoingReferenceProvider(
        UniversalReferenceBridge(make_index()),
        thread_bridge=thread_bridge,
    )
    thread = store.create(ThreadKind.GUIDANCE)
    assert coordinator.bind_initial_reference(
        adapter_name=ONEBOT_V11_ADAPTER_NAME,
        bot_scope="4200",
        conversation_scope=conversation_scope(group_target()),
        actor_scope="actor-200",
        message_reference="601",
        thread_id=thread.thread_id,
    )
    claim = thread_bridge.claim_reply(
        adapter_name=ONEBOT_V11_ADAPTER_NAME,
        bot_scope="4200",
        target=group_target(),
        actor_scope="actor-200",
        message_reference="601",
    )
    assert claim.lease is not None
    matcher = cast(
        Matcher,
        SimpleNamespace(
            state={
                NBTRIAGE_THREAD_BINDING_STATE_KEY: PreparedContinuationBinding(
                    claim.lease.token,
                    "actor-200",
                    ThreadKind.GUIDANCE,
                    ("topic-next",),
                )
            }
        ),
    )

    token = current_matcher.set(matcher)
    try:
        await provider.bind_outgoing_group_message(
            make_bot(),
            exception,
            "send_group_msg",
            {"group_id": 100},
            result,
        )
    finally:
        current_matcher.reset(token)

    closed = store.get(thread.thread_id)
    assert closed is not None
    assert closed.status is ThreadStatus.CLOSED
    assert NBTRIAGE_THREAD_BINDING_STATE_KEY not in matcher.state
    assert provider.dropped_count == 1
    assert (
        thread_bridge.resolve_reply(
            adapter_name=ONEBOT_V11_ADAPTER_NAME,
            bot_scope="4200",
            target=group_target(),
            actor_scope="actor-200",
            message_reference="601",
        )
        is None
    )
    assert (
        thread_bridge.resolve_reply(
            adapter_name=ONEBOT_V11_ADAPTER_NAME,
            bot_scope="4200",
            target=group_target(),
            actor_scope="actor-200",
            message_reference="602",
        )
        is None
    )


@pytest.mark.anyio
async def test_run_postprocessor_closes_unsettled_continuation() -> None:
    store, _, coordinator, thread_bridge = make_thread_runtime()
    thread = store.create(ThreadKind.GUIDANCE)
    assert coordinator.bind_initial_reference(
        adapter_name=ONEBOT_V11_ADAPTER_NAME,
        bot_scope="4200",
        conversation_scope=conversation_scope(group_target()),
        actor_scope="actor-200",
        message_reference="601",
        thread_id=thread.thread_id,
    )
    claim = thread_bridge.claim_reply(
        adapter_name=ONEBOT_V11_ADAPTER_NAME,
        bot_scope="4200",
        target=group_target(),
        actor_scope="actor-200",
        message_reference="601",
    )
    assert claim.lease is not None
    state: dict[str, Any] = {
        NBTRIAGE_THREAD_BINDING_STATE_KEY: PendingContinuationBinding(
            claim.lease.token,
            "actor-200",
        )
    }

    await thread_bridge.cleanup_unsettled_binding(
        cast(Matcher, SimpleNamespace(state=state)),
        state,
        RuntimeError("matcher failed before send"),
    )

    closed = store.get(thread.thread_id)
    assert closed is not None
    assert closed.status is ThreadStatus.CLOSED
    assert NBTRIAGE_THREAD_BINDING_STATE_KEY not in state
    assert thread_bridge.dropped_count == 1


def test_registration_is_explicit_and_rejects_repeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    callbacks: list[Any] = []

    def capture(callback: Any) -> Any:
        callbacks.append(callback)
        return callback

    monkeypatch.setattr(onebot_references.BaseBot, "on_called_api", staticmethod(capture))
    provider = OneBotV11OutgoingReferenceProvider(UniversalReferenceBridge(make_index()))

    assert callbacks == []
    provider.register()
    assert len(callbacks) == 1
    with pytest.raises(
        OneBotV11OutgoingReferenceProviderError,
        match="already registered",
    ):
        provider.register()


def test_claim_internal_failure_is_reported_as_error_and_does_not_revive_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _, coordinator, thread_bridge = make_thread_runtime()
    thread = store.create(ThreadKind.GUIDANCE)
    thread_bridge.bind_reference(
        adapter_name=ONEBOT_V11_ADAPTER_NAME,
        bot_scope="4200",
        target=group_target(),
        actor_scope="actor-200",
        message_reference="601",
        thread_id=thread.thread_id,
    )

    def fail_get(*_: object, **__: object) -> None:
        raise RuntimeError("simulated thread store failure")

    with monkeypatch.context() as patch:
        patch.setattr(store, "get", fail_get)
        result = thread_bridge.claim_reply(
            adapter_name=ONEBOT_V11_ADAPTER_NAME,
            bot_scope="4200",
            target=group_target(),
            actor_scope="actor-200",
            message_reference="601",
        )

    assert result.status is TurnClaimStatus.ERROR
    closed = store.get(thread.thread_id)
    assert closed is not None
    assert closed.status is ThreadStatus.CLOSED
    assert (
        thread_bridge.resolve_reply(
            adapter_name=ONEBOT_V11_ADAPTER_NAME,
            bot_scope="4200",
            target=group_target(),
            actor_scope="actor-200",
            message_reference="601",
        )
        is None
    )
    assert not coordinator._leases_by_thread
