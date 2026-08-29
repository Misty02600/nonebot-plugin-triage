from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from nonebot.adapters.onebot.v11 import Bot
from nonebot_plugin_alconna import SupportAdapter, SupportScope, Target

import nonebot_plugin_triage.universal_references as universal_references
from nbtriage.message_references import PlatformMessageReferenceIndex
from nonebot_plugin_triage.nonebot_runtime import NBTRIAGE_CORRELATION_STATE_KEY
from nonebot_plugin_triage.universal_references import (
    UniversalReferenceBridge,
    UniversalReferenceBridgeError,
    conversation_scope,
)

NOW = datetime(2026, 8, 9, 2, 0, tzinfo=UTC)


class FakeAdapter:
    @classmethod
    def get_name(cls) -> str:
        return "Example Adapter"


def make_bot(self_id: str = "bot-1") -> Bot:
    return Bot(adapter=FakeAdapter(), self_id=self_id)  # type: ignore[arg-type]


def make_target(target_id: str = "room-1", *, private: bool = False) -> Target:
    return Target(
        target_id,
        private=private,
        self_id="bot-1",
        scope=SupportScope.qq_client,
        adapter=SupportAdapter.onebot11,
    )


def make_index() -> PlatformMessageReferenceIndex:
    return PlatformMessageReferenceIndex(
        secret_key=b"test-key-with-at-least-thirty-two-bytes",
        max_entries=16,
        retention_seconds=60,
    )


@pytest.mark.anyio
async def test_incoming_message_uses_uniseg_target_and_message_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = make_target()
    monkeypatch.setattr(universal_references, "get_target", lambda **_: target)
    monkeypatch.setattr(universal_references, "get_message_id", lambda **_: "message-1")
    index = make_index()
    bridge = UniversalReferenceBridge(index, clock=lambda: NOW)

    await bridge.bind_incoming_message(
        make_bot(),
        object(),  # type: ignore[arg-type]
        {NBTRIAGE_CORRELATION_STATE_KEY: "corr-incoming"},
    )

    assert (
        bridge.resolve_reply(
            adapter_name="Example Adapter",
            bot_scope="bot-1",
            target=target,
            message_reference="message-1",
        )
        == "corr-incoming"
    )
    assert "room-1" not in repr(index._entries)
    assert "message-1" not in repr(index._entries)


def test_reference_does_not_cross_adapter_bot_or_target_scope() -> None:
    bridge = UniversalReferenceBridge(make_index(), clock=lambda: NOW)
    bridge.bind_reference(
        adapter_name="Adapter A",
        bot_scope="bot-1",
        target=make_target(),
        message_reference="message-1",
        correlation_id="corr-scope",
    )

    for adapter, bot_scope, target in (
        ("Adapter B", "bot-1", make_target()),
        ("Adapter A", "bot-2", make_target()),
        ("Adapter A", "bot-1", make_target("room-2")),
    ):
        assert (
            bridge.resolve_reply(
                adapter_name=adapter,
                bot_scope=bot_scope,
                target=target,
                message_reference="message-1",
            )
            is None
        )


def test_conversation_scope_ignores_event_source_but_preserves_scene_shape() -> None:
    first = make_target()
    first.source = "event-secret-a"
    second = make_target()
    second.source = "event-secret-b"

    assert conversation_scope(first) == conversation_scope(second)
    assert conversation_scope(first) != conversation_scope(make_target(private=True))
    assert "event-secret" not in conversation_scope(first)


def test_registration_is_explicit_and_rejects_repeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    callbacks: list[Any] = []
    monkeypatch.setattr(
        universal_references,
        "register_event_postprocessor",
        lambda callback: callbacks.append(callback),
    )
    bridge = UniversalReferenceBridge(make_index())

    bridge.register()

    assert callbacks == [bridge.bind_incoming_message]
    with pytest.raises(UniversalReferenceBridgeError, match="already registered"):
        bridge.register()
