from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest
from nonebot.adapters import Bot, Event
from nonebot.adapters.onebot.v11 import Bot as OneBotV11Bot
from nonebot.exception import FinishedException
from nonebot.matcher import Matcher
from nonebot_plugin_alconna import SupportAdapter, SupportScope, Target
from nonebot_plugin_alconna.matcher import AlconnaMatcher
from nonebot_plugin_alconna.uniseg import Receipt, UniMessage
from nonebot_plugin_alconna.uniseg.adapters.onebot11.exporter import Onebot11MessageExporter

from nbtriage.support.threads import (
    InMemorySupportThreadStore,
    OutboundThreadReferenceIndex,
    SupportThreadTurnCoordinator,
    ThreadKind,
    ThreadStatus,
    TurnClaimStatus,
)
from nonebot_plugin_triage.support.responses import (
    finish_support_response,
    resolve_outgoing_receipt,
)
from nonebot_plugin_triage.support.threads import (
    InitialThreadBinding,
    PreparedScopeSupplementBinding,
    SupportThreadReferenceBridge,
)
from nonebot_plugin_triage.universal_references import conversation_scope


def _onebot_bot(self_id: str = "1") -> OneBotV11Bot:
    adapter = SimpleNamespace(get_name=lambda: SupportAdapter.onebot11.value)
    return OneBotV11Bot(adapter=adapter, self_id=self_id)  # type: ignore[arg-type]


def _onebot_target(group_id: str = "100") -> Target:
    return Target(
        group_id,
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


def _receipt(
    bot: Bot,
    target: Event | Target,
    exporter: Onebot11MessageExporter,
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


def test_receipt_rejects_wrong_bot_target_and_multiple_results() -> None:
    bot = _onebot_bot()
    target = _onebot_target()
    receipt = _receipt(bot, target, Onebot11MessageExporter(), {"message_id": 601})

    assert resolve_outgoing_receipt(receipt, bot=_onebot_bot("2"), expected_target=target) is None
    assert resolve_outgoing_receipt(receipt, bot=bot, expected_target=_onebot_target("101")) is None
    receipt.msg_ids.append({"message_id": 602})
    assert resolve_outgoing_receipt(receipt, bot=bot, expected_target=target) is None


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


async def test_finish_support_response_send_failure_closes_unsettled_thread() -> None:
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
            raise RuntimeError("send failed")

        @classmethod
        async def finish(cls) -> None:
            cls.finish_calls += 1
            raise FinishedException

    with pytest.raises(RuntimeError, match="send failed"):
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
    assert FakeMatcher.finish_calls == 0
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


async def test_scope_supplement_settles_after_send_without_receipt() -> None:
    store, coordinator, bridge = _thread_runtime()
    target = _onebot_target()
    claim = coordinator.claim_scope(
        adapter_name=SupportAdapter.onebot11.value,
        bot_scope="1",
        conversation_scope=conversation_scope(target),
        actor_scope="actor-200",
        create_kind=ThreadKind.CLARIFICATION,
    )
    assert claim.status is TurnClaimStatus.ACQUIRED
    assert claim.lease is not None and not claim.lease.is_supplement
    current_matcher = cast(
        Matcher,
        SimpleNamespace(
            state={
                "_nbtriage_thread_binding": PreparedScopeSupplementBinding(
                    lease_token=claim.lease.token,
                    kind=ThreadKind.CLARIFICATION,
                    topic_refs=("capability:search-image",),
                )
            }
        ),
    )

    class FakeMatcher:
        @classmethod
        async def send(cls, _: object) -> object:
            return None

        @classmethod
        async def finish(cls) -> None:
            raise FinishedException

    with pytest.raises(FinishedException):
        await finish_support_response(
            cast(type[AlconnaMatcher], FakeMatcher),
            current_matcher,
            message=UniMessage.text("请再补充一次"),
            bot=_onebot_bot(),
            target=target,
            thread_bridge=bridge,
        )

    supplement = coordinator.claim_scope(
        adapter_name=SupportAdapter.onebot11.value,
        bot_scope="1",
        conversation_scope=conversation_scope(target),
        actor_scope="actor-200",
    )
    assert supplement.status is TurnClaimStatus.ACQUIRED
    assert supplement.lease is not None and supplement.lease.is_supplement
    assert supplement.lease.thread.topic_refs == ("capability:search-image",)
    assert supplement.lease.thread.thread_id == claim.lease.thread.thread_id
    assert bridge.dropped_count == 0
    assert "_nbtriage_thread_binding" not in current_matcher.state
    assert coordinator.close_turn(supplement.lease.token)
    current = store.get(claim.lease.thread.thread_id)
    assert current is not None and current.status is ThreadStatus.CLOSED


async def test_scope_supplement_send_failure_closes_lease() -> None:
    store, coordinator, bridge = _thread_runtime()
    target = _onebot_target()
    claim = coordinator.claim_scope(
        adapter_name=SupportAdapter.onebot11.value,
        bot_scope="1",
        conversation_scope=conversation_scope(target),
        actor_scope="actor-200",
        create_kind=ThreadKind.CLARIFICATION,
    )
    assert claim.lease is not None
    current_matcher = cast(
        Matcher,
        SimpleNamespace(
            state={
                "_nbtriage_thread_binding": PreparedScopeSupplementBinding(
                    lease_token=claim.lease.token,
                    kind=ThreadKind.CLARIFICATION,
                    topic_refs=(),
                )
            }
        ),
    )

    class FakeMatcher:
        @classmethod
        async def send(cls, _: object) -> object:
            raise RuntimeError("send failed")

        @classmethod
        async def finish(cls) -> None:
            raise AssertionError("finish must not run")

    with pytest.raises(RuntimeError, match="send failed"):
        await finish_support_response(
            cast(type[AlconnaMatcher], FakeMatcher),
            current_matcher,
            message=UniMessage.text("请再补充一次"),
            bot=_onebot_bot(),
            target=target,
            thread_bridge=bridge,
        )

    current = store.get(claim.lease.thread.thread_id)
    assert current is not None and current.status is ThreadStatus.CLOSED
    assert bridge.dropped_count == 1
    assert "_nbtriage_thread_binding" not in current_matcher.state


async def test_scope_supplement_rejected_settlement_fails_once() -> None:
    store, coordinator, bridge = _thread_runtime()
    target = _onebot_target()
    claim = coordinator.claim_scope(
        adapter_name=SupportAdapter.onebot11.value,
        bot_scope="1",
        conversation_scope=conversation_scope(target),
        actor_scope="actor-200",
        create_kind=ThreadKind.CLARIFICATION,
    )
    assert claim.lease is not None
    assert coordinator.close_turn(claim.lease.token)
    current_matcher = cast(
        Matcher,
        SimpleNamespace(
            state={
                "_nbtriage_thread_binding": PreparedScopeSupplementBinding(
                    lease_token=claim.lease.token,
                    kind=ThreadKind.CLARIFICATION,
                    topic_refs=(),
                )
            }
        ),
    )

    class FakeMatcher:
        @classmethod
        async def send(cls, _: object) -> object:
            return object()

        @classmethod
        async def finish(cls) -> None:
            raise FinishedException

    with pytest.raises(FinishedException):
        await finish_support_response(
            cast(type[AlconnaMatcher], FakeMatcher),
            current_matcher,
            message=UniMessage.text("请再补充一次"),
            bot=_onebot_bot(),
            target=target,
            thread_bridge=bridge,
        )

    current = store.get(claim.lease.thread.thread_id)
    assert current is not None and current.status is ThreadStatus.CLOSED
    assert bridge.dropped_count == 1
