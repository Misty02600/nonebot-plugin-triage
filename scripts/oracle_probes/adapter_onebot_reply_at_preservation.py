import asyncio
import json

from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageSegment
from nonebot.adapters.onebot.v11.bot import _check_at_me, _check_reply
from nonebot.adapters.onebot.v11.event import Sender


class FakeBot:
    async def get_msg(self, *, message_id: int) -> dict[str, object]:
        return {
            "time": 0,
            "message_type": "group",
            "message_id": message_id,
            "real_id": message_id,
            "sender": {"user_id": 7},
            "message": Message("original"),
        }


def make_event(at_user: int) -> GroupMessageEvent:
    message = Message([MessageSegment.reply(10), MessageSegment.at(at_user)])
    return GroupMessageEvent(
        time=0,
        self_id=42,
        post_type="message",
        sub_type="normal",
        user_id=1,
        message_type="group",
        message_id=100,
        message=message,
        original_message=message.copy(),
        raw_message=str(message),
        font=0,
        sender=Sender(user_id=1),
        group_id=99,
        to_me=False,
    )


def serialize_message(message: Message) -> list[dict[str, object]]:
    return [{"type": segment.type, "data": segment.data} for segment in message]


async def main() -> None:
    bot = FakeBot()
    explicit_bot_at = make_event(42)
    await _check_reply(bot, explicit_bot_at)  # type: ignore[arg-type]
    after_reply = serialize_message(explicit_bot_at.message)
    _check_at_me(bot, explicit_bot_at)  # type: ignore[arg-type]

    automatic_reply_at = make_event(7)
    await _check_reply(bot, automatic_reply_at)  # type: ignore[arg-type]

    print(
        json.dumps(
            {
                "explicit_bot_at_after_reply": after_reply,
                "explicit_bot_at_to_me": explicit_bot_at.to_me,
                "automatic_reply_at_after_reply": serialize_message(automatic_reply_at.message),
            },
            sort_keys=True,
        )
    )


asyncio.run(main())
