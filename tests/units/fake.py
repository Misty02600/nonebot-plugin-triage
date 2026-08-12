from __future__ import annotations

from typing import Any

from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, PrivateMessageEvent
from nonebot.adapters.onebot.v11.event import Sender


def fake_group_message_event_v11(**field: Any) -> GroupMessageEvent:
    values: dict[str, Any] = {
        "time": 1_000_000,
        "self_id": 1,
        "post_type": "message",
        "sub_type": "normal",
        "user_id": 12_345_678,
        "message_type": "group",
        "group_id": 87_654_321,
        "message_id": 1,
        "message": Message("test"),
        "original_message": Message("test"),
        "raw_message": "test",
        "font": 0,
        "sender": Sender(
            user_id=12_345_678,
            card="",
            nickname="test",
            role="member",
        ),
        "to_me": False,
    }
    values.update(field)
    return GroupMessageEvent(**values)


def fake_private_message_event_v11(**field: Any) -> PrivateMessageEvent:
    values: dict[str, Any] = {
        "time": 1_000_000,
        "self_id": 1,
        "post_type": "message",
        "sub_type": "friend",
        "user_id": 12_345_678,
        "message_type": "private",
        "message_id": 1,
        "message": Message("test"),
        "original_message": Message("test"),
        "raw_message": "test",
        "font": 0,
        "sender": Sender(
            user_id=12_345_678,
            nickname="test",
        ),
        "to_me": True,
    }
    values.update(field)
    return PrivateMessageEvent(**values)
