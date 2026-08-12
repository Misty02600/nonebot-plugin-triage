from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageSegment
from nonebot.adapters.onebot.v11.event import Reply as OneBotReply
from nonebot.adapters.onebot.v11.event import Sender
from nonebot.matcher import current_matcher
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
from nbtriage.support_threads import OutboundThreadReferenceIndex
from nonebot_plugin_triage.nonebot_runtime import NBTRIAGE_CORRELATION_STATE_KEY
from nonebot_plugin_triage.onebot_v11_references import (
    ONEBOT_V11_ADAPTER_NAME,
    OneBotV11IncomingReplyReferenceProvider,
    OneBotV11OutgoingReferenceProvider,
    OneBotV11OutgoingReferenceProviderError,
)
from nonebot_plugin_triage.thread_references import (
    NBTRIAGE_THREAD_BINDING_STATE_KEY,
    OutgoingThreadBinding,
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
    matcher = SimpleNamespace(state={NBTRIAGE_CORRELATION_STATE_KEY: "corr-outgoing"})
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

    serialized_state = repr(index._entries)
    assert (
        bridge.resolve_reply(
            adapter_name=ONEBOT_V11_ADAPTER_NAME,
            bot_scope="4200",
            target=group_target(),
            message_reference="601",
        )
        == "corr-outgoing"
    )
    assert "API_DATA_MUST_NOT_BE_STORED" not in serialized_state
    assert "API_RESULT_MUST_NOT_BE_STORED" not in serialized_state
    assert "4200" not in serialized_state
    assert "601" not in serialized_state


@pytest.mark.anyio
async def test_matcher_send_result_also_binds_thread_to_actor_scope() -> None:
    bridge = UniversalReferenceBridge(make_index())
    thread_bridge = make_thread_bridge()
    provider = OneBotV11OutgoingReferenceProvider(bridge, thread_bridge=thread_bridge)
    bot = make_bot()
    matcher = SimpleNamespace(
        state={NBTRIAGE_THREAD_BINDING_STATE_KEY: OutgoingThreadBinding("thread-1", "actor-200")}
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
    matcher = SimpleNamespace(state={NBTRIAGE_CORRELATION_STATE_KEY: "corr-malformed"})
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


def test_incoming_provider_uses_structured_original_reply_without_fetching_content() -> None:
    event = make_group_event(message_id=603)
    event.reply = None
    event.original_message = Message(
        [
            MessageSegment.reply(601),
            MessageSegment.text("FOLLOW_UP_ONLY"),
        ]
    )

    reference = OneBotV11IncomingReplyReferenceProvider().extract(make_bot(), event)

    assert reference is not None
    assert reference.message_reference == "601"
    assert reference.actor_scope == "200"
    assert reference.target == group_target()


def test_incoming_provider_prefers_adapter_reply_and_rejects_non_group_event() -> None:
    event = make_group_event(message_id=604, reply_message_id=602)
    event.original_message = Message(
        [
            MessageSegment.reply(999),
            MessageSegment.text("FOLLOW_UP_ONLY"),
        ]
    )

    reference = OneBotV11IncomingReplyReferenceProvider().extract(make_bot(), event)

    assert reference is not None
    assert reference.message_reference == "602"
    assert OneBotV11IncomingReplyReferenceProvider().extract(make_bot(), object()) is None
