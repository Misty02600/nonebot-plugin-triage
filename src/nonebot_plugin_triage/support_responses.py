from __future__ import annotations

from collections.abc import Mapping
from importlib.util import find_spec
from typing import NoReturn

from nonebot.adapters import Bot
from nonebot.matcher import Matcher
from nonebot_plugin_alconna import Reply, SupportAdapter, Target, UniMessage, get_target
from nonebot_plugin_alconna.matcher import AlconnaMatcher
from nonebot_plugin_alconna.uniseg import Receipt

from nonebot_plugin_triage.thread_references import (
    OutgoingThreadBinding,
    SupportThreadReferenceBridge,
    pop_outgoing_thread_binding,
)
from nonebot_plugin_triage.universal_references import adapter_name, conversation_scope


def _bounded_message_reference(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    normalized = str(value)
    if not normalized or len(normalized.encode("utf-8")) > 512:
        return None
    return normalized


def _receipt_target(receipt: Receipt, bot: Bot) -> Target | None:
    if isinstance(receipt.context, Target):
        return receipt.context
    try:
        return get_target(receipt.context, bot)
    except Exception:
        return None


def _onebot_message_reference(raw_result: object, reply: Reply) -> str | None:
    if not isinstance(raw_result, Mapping):
        return None
    reference = _bounded_message_reference(raw_result.get("message_id"))
    return reference if reference is not None and reply.id == reference else None


def _discord_message_reference(
    raw_result: object,
    reply: Reply,
    expected_target: Target,
) -> str | None:
    try:
        discord_available = find_spec("nonebot.adapters.discord.api") is not None
    except ModuleNotFoundError:
        discord_available = False
    if not discord_available:
        return None

    from nonebot.adapters.discord.api import MessageGet

    if not isinstance(raw_result, MessageGet):
        return None
    if (
        isinstance(raw_result.id, bool)
        or not isinstance(raw_result.id, int)
        or raw_result.id <= 0
        or isinstance(raw_result.channel_id, bool)
        or not isinstance(raw_result.channel_id, int)
        or raw_result.channel_id <= 0
    ):
        return None
    reference = _bounded_message_reference(raw_result.id)
    channel_id = _bounded_message_reference(raw_result.channel_id)
    if reference is None or channel_id != expected_target.id:
        return None
    return reference if reply.id == reference else None


def resolve_outgoing_receipt(
    value: object,
    *,
    bot: Bot,
    expected_target: Target,
) -> str | None:
    """把 UniSeg Receipt 收窄为当前 Bot 与场景下唯一可信的消息引用。"""
    if (
        not isinstance(value, Receipt)
        or value.bot is not bot
        or not isinstance(value.msg_ids, list)
        or len(value.msg_ids) != 1
    ):
        return None
    try:
        receipt_adapter = value.exporter.get_adapter()
        current_adapter = adapter_name(bot)
    except Exception:
        return None
    if receipt_adapter.value != current_adapter:
        return None
    actual_target = _receipt_target(value, bot)
    if actual_target is None or conversation_scope(actual_target) != conversation_scope(
        expected_target
    ):
        return None

    raw_result = value.msg_ids[0]
    if raw_result is None:
        return None
    try:
        reply = value.get_reply(0)
    except Exception:
        return None
    if reply is None or not isinstance(reply, Reply):
        return None

    if receipt_adapter is SupportAdapter.onebot11:
        return _onebot_message_reference(raw_result, reply)
    if receipt_adapter is SupportAdapter.discord:
        return _discord_message_reference(raw_result, reply, expected_target)
    return None


async def finish_support_response(
    matcher: type[AlconnaMatcher],
    current_matcher: Matcher,
    *,
    message: UniMessage,
    bot: Bot,
    target: Target,
    thread_bridge: SupportThreadReferenceBridge,
) -> NoReturn:
    """发送一次支持回复，并用发送回执结算可续接 Thread 后结束 Matcher。"""
    binding: OutgoingThreadBinding | None = pop_outgoing_thread_binding(current_matcher.state)
    binding_finalized = binding is None
    try:
        receipt = await matcher.send(message)
        if binding is not None:
            try:
                reference = resolve_outgoing_receipt(
                    receipt,
                    bot=bot,
                    expected_target=target,
                )
            except Exception:
                reference = None
            if reference is not None:
                thread_bridge.settle_outgoing_binding(
                    binding,
                    adapter_name=adapter_name(bot),
                    bot_scope=str(bot.self_id),
                    target=target,
                    message_reference=reference,
                )
                binding_finalized = True
    finally:
        if binding is not None and not binding_finalized:
            thread_bridge.fail_outgoing_binding(binding)
    await matcher.finish()


__all__ = ("finish_support_response", "resolve_outgoing_receipt")
