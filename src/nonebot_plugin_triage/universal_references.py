from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime

from nonebot.adapters import Bot, Event
from nonebot.message import event_postprocessor as register_event_postprocessor
from nonebot.typing import T_State
from nonebot_plugin_alconna import Target, get_message_id, get_target

from nbtriage.message_references import PlatformMessageReferenceIndex
from nonebot_plugin_triage.nonebot_runtime import NBTRIAGE_CORRELATION_STATE_KEY


class UniversalReferenceBridgeError(RuntimeError):
    pass


class UniversalReferenceBridge:
    """通过 UniSeg 目标模型关联跨适配器入站消息与运行观察。"""

    def __init__(
        self,
        index: PlatformMessageReferenceIndex,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.index = index
        self._clock = clock
        self._dropped_count = 0
        self._registered = False

    @property
    def dropped_count(self) -> int:
        return self._dropped_count

    @property
    def registered(self) -> bool:
        return self._registered

    def register(self) -> None:
        if self._registered:
            raise UniversalReferenceBridgeError("reference bridge is already registered")
        register_event_postprocessor(self.bind_incoming_message)
        self._registered = True

    async def bind_incoming_message(
        self,
        bot: Bot,
        event: Event,
        state: T_State,
    ) -> None:
        try:
            self.bind_reference(
                adapter_name=adapter_name(bot),
                bot_scope=str(bot.self_id),
                target=get_target(event=event, bot=bot),
                message_reference=get_message_id(event=event, bot=bot),
                correlation_id=_correlation_from_state(state),
            )
        except Exception:
            self._record_drop()

    def bind_reference(
        self,
        *,
        adapter_name: str,
        bot_scope: str,
        target: Target,
        message_reference: str,
        correlation_id: str,
    ) -> None:
        self.index.bind(
            adapter_name=adapter_name,
            bot_scope=bot_scope,
            conversation_scope=conversation_scope(target),
            message_reference=message_reference,
            correlation_id=correlation_id,
            now=self._now(),
        )

    def resolve_reply(
        self,
        *,
        adapter_name: str,
        bot_scope: str,
        target: Target,
        message_reference: str,
    ) -> str | None:
        try:
            return self.index.resolve(
                adapter_name=adapter_name,
                bot_scope=bot_scope,
                conversation_scope=conversation_scope(target),
                message_reference=message_reference,
                now=self._now(),
            )
        except Exception:
            self._record_drop()
            return None

    def _now(self) -> datetime | None:
        return self._clock() if self._clock is not None else None

    def _record_drop(self) -> None:
        self._dropped_count += 1


def adapter_name(bot: Bot) -> str:
    name = bot.adapter.get_name()
    if not isinstance(name, str) or not name:
        raise UniversalReferenceBridgeError("adapter name is unavailable")
    return name


def conversation_scope(target: Target) -> str:
    """生成不包含事件 source 的稳定会话 scope；结果只应瞬时进入 HMAC。"""
    return json.dumps(
        [
            target.id,
            target.parent_id,
            target.channel,
            target.private,
            target.scope,
            target.adapter,
            target.platform,
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _correlation_from_state(state: T_State) -> str:
    value = state.get(NBTRIAGE_CORRELATION_STATE_KEY)
    if not isinstance(value, str):
        raise UniversalReferenceBridgeError("runtime correlation id is missing")
    return value
