from __future__ import annotations

from inspect import signature
from typing import Any

import pytest
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageSegment
from nonebot.adapters.onebot.v11.event import Reply, Sender

from nonebot_plugin_triage.onebot_bug_conversation import (
    ONEBOT_HISTORY_MAX_MESSAGES,
    bind_onebot_v11_bug_conversation,
)


class _FakeAdapter:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, object]]] = []

    def get_name(self) -> str:
        return "OneBot V11"

    async def _call_api(self, _bot: Bot, api: str, **data: Any) -> object:
        self.calls.append((api, data))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _bot(responses: list[object]) -> tuple[Bot, _FakeAdapter]:
    adapter = _FakeAdapter(responses)
    return Bot(adapter=adapter, self_id="4200"), adapter  # type: ignore[arg-type]


def _event(*, message_id: int, reply: Reply | None = None) -> GroupMessageEvent:
    sender = Sender(user_id=200, nickname="提问者", role="admin")
    return GroupMessageEvent(
        time=1,
        self_id=4200,
        post_type="message",
        sub_type="normal",
        user_id=200,
        message_type="group",
        message_id=message_id,
        message=Message("triage 按这个做没反应"),
        original_message=Message("triage 按这个做没反应"),
        raw_message="triage 按这个做没反应",
        font=0,
        sender=sender,
        group_id=100,
        reply=reply,
        to_me=True,
    )


def _history_message(
    *,
    sequence: int,
    message_id: int,
    user_id: int,
    name: str,
    content: str,
    role: str = "member",
    reply_to: int | None = None,
) -> dict[str, object]:
    message: list[dict[str, object]] = []
    if reply_to is not None:
        message.append({"type": "reply", "data": {"id": str(reply_to)}})
    message.append({"type": "text", "data": {"text": content}})
    return {
        "time": sequence,
        "message_id": message_id,
        "message_seq": sequence,
        "user_id": user_id,
        "sender": {"user_id": user_id, "nickname": name, "role": role},
        "message": message,
    }


@pytest.mark.anyio
async def test_binding_preloads_reply_and_reads_one_latest_group_window() -> None:
    latest = {
        "messages": [
            _history_message(
                sequence=899,
                message_id=89,
                user_id=200,
                name="提问者",
                content="cookie=visible-cookie",
            ),
            _history_message(
                sequence=900,
                message_id=90,
                user_id=4200,
                name="机器人",
                content="请回复图片后发送搜图",
                role="owner",
            ),
            _history_message(
                sequence=901,
                message_id=100,
                user_id=200,
                name="提问者",
                content="triage 按这个做没反应",
                role="admin",
                reply_to=90,
            ),
        ]
    }
    bot, adapter = _bot([latest])
    reply = Reply.model_validate(
        {
            "time": 900,
            "message_type": "group",
            "message_id": 90,
            "real_id": 90,
            "sender": {"user_id": 4200, "nickname": "机器人", "role": "owner"},
            "message": Message(
                [
                    MessageSegment.text("authorization=Bearer visible-token "),
                    MessageSegment.at(200),
                    MessageSegment.image("https://example.invalid/private-image?token=visible"),
                ]
            ),
            "message_seq": 900,
        }
    )
    binding = bind_onebot_v11_bug_conversation(
        bot,
        _event(message_id=100, reply=reply),
        max_messages=4,
    )

    assert binding.reply_message is not None
    assert binding.reply_message.content == (
        "authorization=Bearer visible-token [艾特用户 qq=200]"
        "[图片 file=https://example.invalid/private-image?token=visible]"
    )
    assert binding.reply_message.is_bot is True
    assert binding.reply_message.sender_id == "4200"
    assert binding.reply_message.sender_roles == ("owner",)

    window = await binding.history.read_next()

    assert [message.content for message in window.messages] == [
        "cookie=visible-cookie",
        "请回复图片后发送搜图",
        "[回复消息 id=90]triage 按这个做没反应",
    ]
    assert window.has_more is False
    assert window.availability == "complete"
    assert window.request_actor_id == "200"
    assert window.request_actor_roles == ("admin",)
    assert window.messages[1].sender_roles == ("owner",)
    assert window.messages[2].is_request_actor is True
    assert window.messages[2].is_current_request is True
    assert window.messages[2].reply_to_message_id == "90"
    assert [api for api, _data in adapter.calls] == ["get_group_msg_history"]
    assert adapter.calls[0][1]["group_id"] == 100
    assert "message_seq" not in adapter.calls[0][1]
    assert adapter.calls[0][1]["count"] == 4


@pytest.mark.anyio
async def test_exact_reply_remains_available_outside_latest_history_window() -> None:
    latest = {
        "messages": [
            _history_message(
                sequence=1000,
                message_id=100,
                user_id=200,
                name="提问者",
                content="triage 按这个做没反应",
                role="admin",
                reply_to=10,
            )
        ]
    }
    bot, adapter = _bot([latest])
    reply = Reply.model_validate(
        {
            "time": 10,
            "message_type": "group",
            "message_id": 10,
            "real_id": 10,
            "sender": {"user_id": 4200, "nickname": "机器人", "role": "owner"},
            "message": Message("这条教学已经不在最新窗口中"),
            "message_seq": 10,
        }
    )
    binding = bind_onebot_v11_bug_conversation(
        bot,
        _event(message_id=100, reply=reply),
        max_messages=1,
    )

    window = await binding.history.read_next()

    assert binding.reply_message is not None
    assert binding.reply_message.message_id == "10"
    assert binding.reply_message.content == "这条教学已经不在最新窗口中"
    assert [message.message_id for message in window.messages] == ["100"]
    assert adapter.calls[0][1]["count"] == 1


def test_default_latest_history_window_is_thirty_messages() -> None:
    assert ONEBOT_HISTORY_MAX_MESSAGES == 30


@pytest.mark.anyio
async def test_reader_reports_unavailable_when_latest_history_call_fails() -> None:
    bot, adapter = _bot([RuntimeError("history unavailable")])
    binding = bind_onebot_v11_bug_conversation(bot, _event(message_id=700))

    page = await binding.history.read_next()

    assert page.messages == ()
    assert page.availability == "unavailable"
    assert page.partial is True
    assert adapter.calls[0][0] == "get_group_msg_history"


def test_history_reader_exposes_no_scope_or_message_identifier_parameters() -> None:
    bot, _adapter = _bot([])
    binding = bind_onebot_v11_bug_conversation(bot, _event(message_id=700))

    assert not signature(binding.history.read_next).parameters
