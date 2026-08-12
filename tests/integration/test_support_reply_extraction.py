from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from nonebot import get_driver
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter
from nonebot.adapters.onebot.v11 import Bot as OneBotV11Bot
from nonebot.adapters.onebot.v11 import Message, MessageSegment
from nonebot.adapters.onebot.v11.bot import _check_reply
from nonebot.adapters.onebot.v11.event import Reply as OneBotReply
from nonebot.adapters.onebot.v11.event import Sender
from nonebot.compat import model_dump
from nonebot_plugin_alconna import Reply
from nonebot_plugin_alconna.uniseg import get_builder
from nonebot_plugin_alconna.uniseg.params import _orig_uni_msg
from nonebug import App
from pytest import MonkeyPatch
from tests.units.fake import fake_group_message_event_v11


def _reply_event(*, message_id: int) -> Any:
    sender = Sender(user_id=9_100_001, nickname="tester")
    return fake_group_message_event_v11(
        message_id=message_id,
        user_id=sender.user_id,
        message=Message("triage"),
        original_message=Message([MessageSegment.reply(8_100_001), MessageSegment.text(" triage")]),
        raw_message="[CQ:reply,id=8100001] triage",
        sender=sender,
        reply=OneBotReply(
            time=1,
            message_type="group",
            message_id=8_100_001,
            real_id=8_100_001,
            sender=sender,
            message=Message("BOT_ANSWER_MUST_NOT_BE_READ"),
        ),
        to_me=False,
    )


def _unresolved_reply_event(*, message_id: int) -> Any:
    original_message = Message([MessageSegment.reply(8_100_001), MessageSegment.text(" triage")])
    return fake_group_message_event_v11(
        message_id=message_id,
        user_id=9_100_001,
        message=Message(original_message),
        original_message=Message(original_message),
        raw_message="[CQ:reply,id=8100001] triage",
        sender=Sender(user_id=9_100_001, nickname="tester"),
        reply=None,
        to_me=False,
    )


async def test_support_reply_is_extracted_only_for_handler_original_message(
    app: App,
    monkeypatch: MonkeyPatch,
) -> None:
    from nonebot_plugin_triage.handlers import support_matcher

    async with app.test_matcher(support_matcher) as ctx:
        bot = ctx.create_bot(
            base=OneBotV11Bot,
            adapter=OneBotV11Adapter(get_driver()),
            self_id="1",
            auto_connect=False,
        )
        builder = get_builder(bot)
        assert builder is not None
        builder_type = type(builder)
        original_extract_reply = builder_type.extract_reply
        extract_reply_calls = 0

        async def recording_extract_reply(self: Any, event: Any, current_bot: Any) -> Any:
            nonlocal extract_reply_calls
            extract_reply_calls += 1
            return await original_extract_reply(self, event, current_bot)

        monkeypatch.setattr(builder_type, "extract_reply", recording_extract_reply)

        assert await support_matcher._rule(bot, _reply_event(message_id=9_100_001), {})
        assert extract_reply_calls == 0

        event = _reply_event(message_id=9_100_002)
        ctx.receive_event(bot, event)
        ctx.should_call_send(
            event,
            Message("请在 triage 后描述想了解的功能或遇到的问题。"),
            result=None,
        )
        ctx.should_finished(support_matcher)

    assert extract_reply_calls == 1


@pytest.mark.parametrize("reply_lookup", ["failed", "missing_sender"])
async def test_support_command_survives_unresolved_onebot_reply_preprocessing(
    app: App,
    reply_lookup: str,
    monkeypatch: MonkeyPatch,
) -> None:
    from nonebot_plugin_triage import handlers

    support_matcher = handlers.support_matcher
    monkeypatch.setattr(
        handlers.plugin_runtime.support_rate_limiter,
        "allow",
        lambda *_: True,
    )

    event = _unresolved_reply_event(message_id=9_100_003)

    async def get_msg(**_: Any) -> dict[str, Any]:
        if reply_lookup == "failed":
            raise RuntimeError("simulated get_msg failure")
        return model_dump(
            OneBotReply(
                time=1,
                message_type="group",
                message_id=8_100_001,
                real_id=8_100_001,
                sender=Sender(user_id=None, nickname="bot"),
                message=Message("BOT_ANSWER_MUST_NOT_BE_READ"),
            )
        )

    preprocess_bot = SimpleNamespace(get_msg=get_msg, self_id="1")
    await _check_reply(preprocess_bot, event)  # type: ignore[arg-type]
    assert event.message[0].type == "reply"

    async with app.test_matcher(support_matcher) as ctx:
        bot = ctx.create_bot(
            base=OneBotV11Bot,
            adapter=OneBotV11Adapter(get_driver()),
            self_id="1",
            auto_connect=False,
        )
        assert await support_matcher._rule(bot, event, {})

        original = await _orig_uni_msg(bot, event, {})
        assert original.get(Reply, 1)[0].id == "8100001"

        ctx.receive_event(bot, event)
        ctx.should_call_send(
            event,
            Message("请在 triage 后描述想了解的功能或遇到的问题。"),
            result=None,
        )
        ctx.should_finished(support_matcher)
