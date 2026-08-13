from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from nonebot.adapters import Bot as BaseBot
from nonebot.adapters.onebot.v11 import Bot as OneBotV11Bot
from nonebot.matcher import current_matcher
from nonebot_plugin_alconna import SupportAdapter, SupportScope, Target

from nonebot_plugin_triage.nonebot_runtime import NBTRIAGE_CORRELATION_STATE_KEY
from nonebot_plugin_triage.universal_references import UniversalReferenceBridge

ONEBOT_V11_ADAPTER_NAME = SupportAdapter.onebot11.value
_GROUP_SEND_APIS = {"send_group_msg", "send_msg"}


class OneBotV11OutgoingReferenceProviderError(RuntimeError):
    pass


class OneBotV11OutgoingReferenceProvider:
    """补齐 OneBot V11 群消息发送结果中的平台消息引用。"""

    def __init__(self, bridge: UniversalReferenceBridge) -> None:
        self.bridge = bridge
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
            raise OneBotV11OutgoingReferenceProviderError(
                "outgoing reference provider is already registered"
            )
        BaseBot.on_called_api(self.bind_outgoing_group_message)
        self._registered = True

    async def bind_outgoing_group_message(
        self,
        bot: BaseBot,
        exception: Exception | None,
        api: str,
        data: dict[str, Any],
        result: Any,
    ) -> None:
        if not isinstance(bot, OneBotV11Bot) or api not in _GROUP_SEND_APIS:
            return
        matcher = current_matcher.get(None)
        if matcher is None:
            return
        if exception is not None:
            return
        try:
            group_id = _outgoing_group_id(api, data)
            if group_id is None:
                return
            message_id = _result_message_id(result)
            target = _group_target(group_id, str(bot.self_id))
        except Exception:
            self._record_drop()
            return

        correlation_id = _optional_correlation_from_state(matcher.state)
        if correlation_id is not None:
            try:
                self.bridge.bind_reference(
                    adapter_name=ONEBOT_V11_ADAPTER_NAME,
                    bot_scope=str(bot.self_id),
                    target=target,
                    message_reference=str(message_id),
                    correlation_id=correlation_id,
                )
            except Exception:
                self._record_drop()

    def _record_drop(self) -> None:
        self._dropped_count += 1


def _outgoing_group_id(api: str, data: Mapping[str, Any]) -> int | str | None:
    group_id = data.get("group_id")
    if isinstance(group_id, bool) or not isinstance(group_id, (int, str)):
        return None
    if api == "send_msg" and data.get("message_type") not in {None, "group"}:
        return None
    return group_id


def _result_message_id(result: Any) -> int | str:
    if not isinstance(result, Mapping):
        raise OneBotV11OutgoingReferenceProviderError("send result has no structured message id")
    message_id = result.get("message_id")
    if isinstance(message_id, bool) or not isinstance(message_id, (int, str)):
        raise OneBotV11OutgoingReferenceProviderError("send result has no structured message id")
    return message_id


def _optional_correlation_from_state(state: dict[str, Any]) -> str | None:
    value = state.get(NBTRIAGE_CORRELATION_STATE_KEY)
    return value if isinstance(value, str) else None


def _group_target(group_id: int | str, self_id: str) -> Target:
    return Target(
        str(group_id),
        self_id=self_id,
        scope=SupportScope.qq_client,
        adapter=SupportAdapter.onebot11,
    )
