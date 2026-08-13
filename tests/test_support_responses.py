from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from nonebot.adapters import Bot, Event
from nonebot.adapters.discord import Bot as DiscordBot
from nonebot.adapters.discord.api import (
    UNSET,
    MessageGet,
    MessageReference,
    MessageReferenceType,
    MessageType,
    Snowflake,
    User,
)
from nonebot.adapters.discord.config import BotInfo
from nonebot.adapters.discord.event import (
    DirectMessageCreateEvent,
    GuildMessageCreateEvent,
)
from nonebot.adapters.onebot.v11 import Bot as OneBotV11Bot
from nonebot.exception import FinishedException
from nonebot.matcher import Matcher
from nonebot_plugin_alconna import SupportAdapter, SupportScope, Target, get_target
from nonebot_plugin_alconna.matcher import AlconnaMatcher
from nonebot_plugin_alconna.uniseg import Receipt, UniMessage
from nonebot_plugin_alconna.uniseg.adapters.discord.exporter import DiscordMessageExporter
from nonebot_plugin_alconna.uniseg.adapters.onebot11.exporter import Onebot11MessageExporter

from nbtriage.support_threads import (
    InMemorySupportThreadStore,
    OutboundThreadReferenceIndex,
    SupportThreadTurnCoordinator,
    ThreadKind,
    ThreadStatus,
    TurnClaimStatus,
)
from nonebot_plugin_triage.handlers import _is_supported_thread_reply
from nonebot_plugin_triage.support_responses import (
    finish_support_response,
    resolve_outgoing_receipt,
)
from nonebot_plugin_triage.thread_references import (
    InitialThreadBinding,
    SupportThreadReferenceBridge,
)


def _onebot_bot(self_id: str = "1") -> OneBotV11Bot:
    adapter = SimpleNamespace(get_name=lambda: SupportAdapter.onebot11.value)
    return OneBotV11Bot(adapter=adapter, self_id=self_id)  # type: ignore[arg-type]


def _discord_bot(self_id: str = "1") -> DiscordBot:
    adapter = SimpleNamespace(get_name=lambda: SupportAdapter.discord.value)
    return DiscordBot(
        adapter=adapter,  # type: ignore[arg-type]
        self_id=self_id,
        bot_info=BotInfo(token="test-token"),
    )


def _onebot_target(group_id: str = "100") -> Target:
    return Target(
        group_id,
        self_id="1",
        scope=SupportScope.qq_client,
        adapter=SupportAdapter.onebot11,
    )


def _onebot_private_target(user_id: str = "200") -> Target:
    return Target(
        user_id,
        private=True,
        self_id="1",
        scope=SupportScope.qq_client,
        adapter=SupportAdapter.onebot11,
    )


def _discord_target(channel_id: str = "900", guild_id: str = "800") -> Target:
    return Target(
        channel_id,
        guild_id,
        channel=True,
        self_id="1",
        scope=SupportScope.discord,
        adapter=SupportAdapter.discord,
    )


def _discord_message(*, message_id: int = 700, channel_id: int = 900) -> MessageGet:
    return MessageGet(
        id=Snowflake(message_id),
        channel_id=Snowflake(channel_id),
        author=User(
            id=Snowflake(123),
            username="bot",
            discriminator="0001",
            avatar=None,
        ),
        content="answer",
        timestamp=datetime(2026, 8, 13, tzinfo=UTC),
        edited_timestamp=None,
        tts=False,
        mention_everyone=False,
        mentions=[],
        mention_roles=[],
        attachments=[],
        embeds=[],
        pinned=False,
        type=MessageType.DEFAULT,
    )


def _discord_event(
    *,
    direct: bool = False,
    reference_type: MessageReferenceType | type[UNSET] = MessageReferenceType.DEFAULT,
    message_type: MessageType = MessageType.REPLY,
) -> GuildMessageCreateEvent | DirectMessageCreateEvent:
    common = {
        "id": Snowflake(701),
        "channel_id": Snowflake(900),
        "author": User(
            id=Snowflake(123),
            username="user",
            discriminator="0001",
            avatar=None,
        ),
        "content": "triage 参数呢",
        "timestamp": datetime(2026, 8, 13, tzinfo=UTC),
        "edited_timestamp": None,
        "tts": False,
        "mention_everyone": False,
        "mentions": [],
        "mention_roles": [],
        "attachments": [],
        "embeds": [],
        "pinned": False,
        "type": message_type,
        "message_reference": MessageReference(
            type=reference_type,
            message_id=Snowflake(700),
            channel_id=Snowflake(900),
            guild_id=Snowflake(800),
        ),
    }
    if direct:
        return DirectMessageCreateEvent.model_validate(common)
    return GuildMessageCreateEvent.model_validate({**common, "guild_id": Snowflake(800)})


def _receipt(
    bot: Bot,
    target: Event | Target,
    exporter: Onebot11MessageExporter | DiscordMessageExporter,
    raw_result: object,
) -> Receipt:
    return Receipt(bot, target, exporter, [raw_result], UniMessage)


def _thread_runtime() -> tuple[
    InMemorySupportThreadStore,
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
    return store, coordinator, SupportThreadReferenceBridge(coordinator)


@pytest.mark.parametrize(
    ("raw_result", "expected"),
    [
        ({"message_id": 601}, "601"),
        ({"message_id": "602"}, "602"),
        (None, None),
        ({"message_id": ""}, None),
        ({"message_id": False}, None),
        ({"missing": 601}, None),
        (601, None),
    ],
)
def test_onebot_receipt_requires_one_structured_bounded_message_id(
    raw_result: object,
    expected: str | None,
) -> None:
    bot = _onebot_bot()
    target = _onebot_target()

    assert (
        resolve_outgoing_receipt(
            _receipt(bot, target, Onebot11MessageExporter(), raw_result),
            bot=bot,
            expected_target=target,
        )
        == expected
    )


def test_onebot_private_receipt_uses_the_same_exact_scope_contract() -> None:
    _, coordinator, bridge = _thread_runtime()
    bot = _onebot_bot()
    target = _onebot_private_target()
    receipt = _receipt(bot, target, Onebot11MessageExporter(), {"message_id": 603})
    thread = coordinator.create_initial_thread(ThreadKind.GUIDANCE)

    assert resolve_outgoing_receipt(receipt, bot=bot, expected_target=target) == "603"
    assert bridge.settle_outgoing_binding(
        InitialThreadBinding(thread.thread_id, "actor-200"),
        adapter_name=SupportAdapter.onebot11.value,
        bot_scope=str(bot.self_id),
        target=target,
        message_reference="603",
    )
    wrong_scope = bridge.claim_reply(
        adapter_name=SupportAdapter.onebot11.value,
        bot_scope=str(bot.self_id),
        target=_onebot_target("200"),
        actor_scope="actor-200",
        message_reference="603",
    )
    assert wrong_scope.status is TurnClaimStatus.NOT_FOUND
    claim = bridge.claim_reply(
        adapter_name=SupportAdapter.onebot11.value,
        bot_scope=str(bot.self_id),
        target=target,
        actor_scope="actor-200",
        message_reference="603",
    )
    assert claim.status is TurnClaimStatus.ACQUIRED
    assert claim.lease is not None and claim.lease.thread.thread_id == thread.thread_id


def test_receipt_rejects_wrong_bot_target_and_multiple_results() -> None:
    bot = _onebot_bot()
    target = _onebot_target()
    receipt = _receipt(bot, target, Onebot11MessageExporter(), {"message_id": 601})

    assert resolve_outgoing_receipt(receipt, bot=_onebot_bot("2"), expected_target=target) is None
    assert resolve_outgoing_receipt(receipt, bot=bot, expected_target=_onebot_target("101")) is None
    receipt.msg_ids.append({"message_id": 602})
    assert resolve_outgoing_receipt(receipt, bot=bot, expected_target=target) is None


def test_discord_receipt_requires_message_get_from_expected_channel() -> None:
    bot = _discord_bot()
    target = _discord_target()
    valid = _receipt(bot, target, DiscordMessageExporter(), _discord_message())
    wrong_channel = _receipt(
        bot,
        target,
        DiscordMessageExporter(),
        _discord_message(channel_id=901),
    )
    duck = _receipt(
        bot,
        target,
        DiscordMessageExporter(),
        SimpleNamespace(id=Snowflake(700), channel_id=Snowflake(900)),
    )
    zero_id = _receipt(bot, target, DiscordMessageExporter(), _discord_message(message_id=0))
    negative_id = _receipt(
        bot,
        target,
        DiscordMessageExporter(),
        _discord_message(message_id=-1),
    )

    assert resolve_outgoing_receipt(valid, bot=bot, expected_target=target) == "700"
    assert resolve_outgoing_receipt(wrong_channel, bot=bot, expected_target=target) is None
    assert resolve_outgoing_receipt(duck, bot=bot, expected_target=target) is None
    assert resolve_outgoing_receipt(zero_id, bot=bot, expected_target=target) is None
    assert resolve_outgoing_receipt(negative_id, bot=bot, expected_target=target) is None


@pytest.mark.parametrize("direct", [False, True])
async def test_discord_reply_round_trip_claims_guild_and_dm_thread(
    direct: bool,
) -> None:
    _, coordinator, bridge = _thread_runtime()
    bot = _discord_bot()
    event = _discord_event(direct=direct)
    target = get_target(event, bot)
    receipt = _receipt(bot, event, DiscordMessageExporter(), _discord_message())
    message = await UniMessage.of(event.get_message(), bot=bot).attach_reply(event, bot)
    reply = message.get(type(message[0]), 1)[0]
    thread = coordinator.create_initial_thread(ThreadKind.GUIDANCE)

    assert resolve_outgoing_receipt(receipt, bot=bot, expected_target=target) == "700"
    assert str(reply.id) == "700"
    assert _is_supported_thread_reply(bot, event, reply)
    assert bridge.settle_outgoing_binding(
        InitialThreadBinding(thread.thread_id, event.get_user_id()),
        adapter_name=SupportAdapter.discord.value,
        bot_scope=str(bot.self_id),
        target=target,
        message_reference="700",
    )
    wrong_target = _discord_target(
        channel_id="901" if direct else "900",
        guild_id="" if direct else "801",
    )
    wrong_target.private = direct
    wrong_claim = bridge.claim_reply(
        adapter_name=SupportAdapter.discord.value,
        bot_scope=str(bot.self_id),
        target=wrong_target,
        actor_scope=event.get_user_id(),
        message_reference=str(reply.id),
    )
    assert wrong_claim.status is TurnClaimStatus.NOT_FOUND
    claim = bridge.claim_reply(
        adapter_name=SupportAdapter.discord.value,
        bot_scope=str(bot.self_id),
        target=target,
        actor_scope=event.get_user_id(),
        message_reference=str(reply.id),
    )
    assert claim.status is TurnClaimStatus.ACQUIRED
    assert claim.lease is not None and claim.lease.thread.thread_id == thread.thread_id


async def test_discord_forward_reference_is_not_a_thread_reply() -> None:
    bot = _discord_bot()
    event = _discord_event(reference_type=MessageReferenceType.FORWARD)
    message = await UniMessage.of(event.get_message(), bot=bot).attach_reply(event, bot)
    reply = message.get(type(message[0]), 1)[0]

    assert str(reply.id) == "700"
    assert not _is_supported_thread_reply(bot, event, reply)


async def test_discord_non_reply_message_reference_is_not_a_thread_reply() -> None:
    bot = _discord_bot()
    event = _discord_event(message_type=MessageType.DEFAULT)
    message = await UniMessage.of(event.get_message(), bot=bot).attach_reply(event, bot)
    reply = message.get(type(message[0]), 1)[0]

    assert str(reply.id) == "700"
    assert not _is_supported_thread_reply(bot, event, reply)


async def test_discord_reply_id_must_match_origin_message_id() -> None:
    bot = _discord_bot()
    event = _discord_event()
    message = await UniMessage.of(event.get_message(), bot=bot).attach_reply(event, bot)
    reply = message.get(type(message[0]), 1)[0]
    reply.origin.message_id = Snowflake(701)

    assert str(reply.id) == "700"
    assert not _is_supported_thread_reply(bot, event, reply)


async def test_discord_unset_reference_type_is_treated_as_direct_reply() -> None:
    bot = _discord_bot()
    event = _discord_event(reference_type=UNSET)
    message = await UniMessage.of(event.get_message(), bot=bot).attach_reply(event, bot)
    reply = message.get(type(message[0]), 1)[0]

    assert str(reply.id) == "700"
    assert _is_supported_thread_reply(bot, event, reply)


async def test_discord_reply_id_survives_failed_replied_message_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nonebot.adapters.discord.bot import _check_reply

    _, coordinator, bridge = _thread_runtime()
    bot = _discord_bot()
    event = _discord_event()

    async def fail_fetch(**_: object) -> None:
        raise RuntimeError("simulated Discord history lookup failure")

    monkeypatch.setattr(bot, "get_channel_message", fail_fetch, raising=False)
    await _check_reply(bot, event)
    message = await UniMessage.of(event.get_message(), bot=bot).attach_reply(event, bot)
    reply = message.get(type(message[0]), 1)[0]
    target = get_target(event, bot)
    thread = coordinator.create_initial_thread(ThreadKind.GUIDANCE)

    assert event.reply is None
    assert str(reply.id) == "700"
    assert _is_supported_thread_reply(bot, event, reply)
    assert bridge.settle_outgoing_binding(
        InitialThreadBinding(thread.thread_id, event.get_user_id()),
        adapter_name=SupportAdapter.discord.value,
        bot_scope=str(bot.self_id),
        target=target,
        message_reference="700",
    )
    claim = bridge.claim_reply(
        adapter_name=SupportAdapter.discord.value,
        bot_scope=str(bot.self_id),
        target=target,
        actor_scope=event.get_user_id(),
        message_reference=str(reply.id),
    )
    assert claim.status is TurnClaimStatus.ACQUIRED
    assert claim.lease is not None and claim.lease.thread.thread_id == thread.thread_id


def test_receipt_settlement_binds_initial_thread_to_actor_and_scope() -> None:
    store, coordinator, bridge = _thread_runtime()
    thread = coordinator.create_initial_thread(ThreadKind.GUIDANCE)
    binding = InitialThreadBinding(thread.thread_id, "actor-200")
    target = _discord_target()

    assert bridge.settle_outgoing_binding(
        binding,
        adapter_name=SupportAdapter.discord.value,
        bot_scope="1",
        target=target,
        message_reference="700",
    )
    assert (
        bridge.resolve_reply(
            adapter_name=SupportAdapter.discord.value,
            bot_scope="1",
            target=target,
            actor_scope="actor-200",
            message_reference="700",
        )
        == thread.thread_id
    )
    assert (
        bridge.resolve_reply(
            adapter_name=SupportAdapter.discord.value,
            bot_scope="1",
            target=target,
            actor_scope="actor-201",
            message_reference="700",
        )
        is None
    )
    current = store.get(thread.thread_id)
    assert current is not None and current.status is ThreadStatus.CONTINUABLE


async def test_unsettled_binding_cleanup_remains_fail_closed() -> None:
    store, coordinator, bridge = _thread_runtime()
    thread = coordinator.create_initial_thread(ThreadKind.GUIDANCE)
    state: dict[str, Any] = {
        "_nbtriage_thread_binding": InitialThreadBinding(thread.thread_id, "actor-200")
    }

    await bridge.cleanup_unsettled_binding(
        cast(Matcher, SimpleNamespace(state=state)),
        state,
        RuntimeError("send failed"),
    )

    current = store.get(thread.thread_id)
    assert current is not None and current.status is ThreadStatus.CLOSED
    assert bridge.dropped_count == 1


@pytest.mark.parametrize("failure", ["malformed", "error", "cancelled"])
async def test_finish_support_response_closes_unsettled_thread(
    failure: str,
) -> None:
    store, coordinator, bridge = _thread_runtime()
    thread = coordinator.create_initial_thread(ThreadKind.GUIDANCE)
    current_matcher = cast(
        Matcher,
        SimpleNamespace(
            state={
                "_nbtriage_thread_binding": InitialThreadBinding(
                    thread.thread_id,
                    "actor-200",
                )
            }
        ),
    )
    bot = _onebot_bot()
    target = _onebot_target()

    class FakeMatcher:
        finish_calls = 0

        @classmethod
        async def send(cls, _: object) -> object:
            if failure == "error":
                raise RuntimeError("send failed")
            if failure == "cancelled":
                raise asyncio.CancelledError
            return _receipt(bot, target, Onebot11MessageExporter(), None)

        @classmethod
        async def finish(cls) -> None:
            cls.finish_calls += 1
            raise FinishedException

    matcher = cast(type[AlconnaMatcher], FakeMatcher)
    expected = (
        asyncio.CancelledError
        if failure == "cancelled"
        else RuntimeError
        if failure == "error"
        else FinishedException
    )
    with pytest.raises(expected):
        await finish_support_response(
            matcher,
            current_matcher,
            message=UniMessage.text("answer"),
            bot=bot,
            target=target,
            thread_bridge=bridge,
        )

    current = store.get(thread.thread_id)
    assert current is not None and current.status is ThreadStatus.CLOSED
    assert bridge.dropped_count == 1
    assert FakeMatcher.finish_calls == (1 if failure == "malformed" else 0)
    assert "_nbtriage_thread_binding" not in current_matcher.state


async def test_finish_support_response_settles_before_finishing() -> None:
    store, coordinator, bridge = _thread_runtime()
    thread = coordinator.create_initial_thread(ThreadKind.GUIDANCE)
    current_matcher = cast(
        Matcher,
        SimpleNamespace(
            state={
                "_nbtriage_thread_binding": InitialThreadBinding(
                    thread.thread_id,
                    "actor-200",
                )
            }
        ),
    )
    bot = _onebot_bot()
    target = _onebot_target()

    class FakeMatcher:
        @classmethod
        async def send(cls, _: object) -> object:
            return _receipt(
                bot,
                target,
                Onebot11MessageExporter(),
                {"message_id": 601},
            )

        @classmethod
        async def finish(cls) -> None:
            raise FinishedException

    with pytest.raises(FinishedException):
        await finish_support_response(
            cast(type[AlconnaMatcher], FakeMatcher),
            current_matcher,
            message=UniMessage.text("answer"),
            bot=bot,
            target=target,
            thread_bridge=bridge,
        )

    assert (
        bridge.resolve_reply(
            adapter_name=SupportAdapter.onebot11.value,
            bot_scope="1",
            target=target,
            actor_scope="actor-200",
            message_reference="601",
        )
        == thread.thread_id
    )
    current = store.get(thread.thread_id)
    assert current is not None and current.status is ThreadStatus.CONTINUABLE
    assert bridge.dropped_count == 0


async def test_finish_support_response_fails_closed_when_settlement_is_rejected() -> None:
    store, coordinator, bridge = _thread_runtime()
    thread = coordinator.create_initial_thread(ThreadKind.GUIDANCE)
    assert coordinator.fail_initial(thread.thread_id)
    current_matcher = cast(
        Matcher,
        SimpleNamespace(
            state={
                "_nbtriage_thread_binding": InitialThreadBinding(
                    thread.thread_id,
                    "actor-200",
                )
            }
        ),
    )
    bot = _onebot_bot()
    target = _onebot_target()

    class FakeMatcher:
        @classmethod
        async def send(cls, _: object) -> object:
            return _receipt(
                bot,
                target,
                Onebot11MessageExporter(),
                {"message_id": 601},
            )

        @classmethod
        async def finish(cls) -> None:
            raise FinishedException

    with pytest.raises(FinishedException):
        await finish_support_response(
            cast(type[AlconnaMatcher], FakeMatcher),
            current_matcher,
            message=UniMessage.text("answer"),
            bot=bot,
            target=target,
            thread_bridge=bridge,
        )

    current = store.get(thread.thread_id)
    assert current is not None and current.status is ThreadStatus.CLOSED
    assert bridge.dropped_count == 1
    assert (
        bridge.resolve_reply(
            adapter_name=SupportAdapter.onebot11.value,
            bot_scope="1",
            target=target,
            actor_scope="actor-200",
            message_reference="601",
        )
        is None
    )
