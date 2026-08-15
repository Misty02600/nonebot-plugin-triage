from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from nonebot.adapters.onebot.v11 import Bot as OneBotV11Bot
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageSegment
from nonebot.adapters.onebot.v11.event import Reply as OneBotV11Reply

from nbtriage.bug_conversation import (
    BoundBugConversationReader,
    BugConversationAvailability,
    BugConversationMessage,
    BugConversationPage,
)

ONEBOT_HISTORY_MAX_MESSAGES = 30

_VISIBLE_SEGMENT_LABELS = {
    "at": "[艾特用户]",
    "contact": "[联系人]",
    "face": "[表情]",
    "file": "[文件]",
    "forward": "[转发消息]",
    "image": "[图片]",
    "json": "[卡片]",
    "location": "[位置]",
    "music": "[音乐]",
    "node": "[转发消息]",
    "record": "[语音]",
    "reply": "[回复消息]",
    "video": "[视频]",
    "xml": "[卡片]",
}
_VISIBLE_SEGMENT_FIELDS = {
    "at": ("qq",),
    "contact": ("type", "id"),
    "face": ("id",),
    "file": ("name", "file", "file_id", "url"),
    "forward": ("id",),
    "image": ("summary", "file", "url"),
    "location": ("lat", "lon", "title", "content"),
    "music": ("type", "id", "title", "url", "audio"),
    "record": ("file", "url"),
    "reply": ("id",),
    "video": ("file", "url"),
}


class OneBotV11BugConversationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class _GroupConversationScope:
    group_id: int
    current_message_id: int
    request_actor_id: str
    request_actor_roles: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _ParsedHistoryMessage:
    public: BugConversationMessage


@dataclass(frozen=True, slots=True)
class OneBotV11BugConversationBinding:
    """入口已绑定的 Reply 正文与同一群最新历史读取器。"""

    reply_message: BugConversationMessage | None
    history: BoundBugConversationReader


class OneBotV11GroupBugConversationReader:
    """从固定 Bot 和群读取一次 NapCat 最新群聊历史窗口。"""

    def __init__(
        self,
        bot: OneBotV11Bot,
        scope: _GroupConversationScope,
        *,
        max_messages: int = ONEBOT_HISTORY_MAX_MESSAGES,
    ) -> None:
        if max_messages < 1:
            raise ValueError("max_messages must be positive")
        self._bot = bot
        self._scope = scope
        self._max_messages = max_messages
        self._exhausted = False

    async def read_next(self) -> BugConversationPage:
        if self._exhausted:
            return self._page(availability="complete", messages=(), partial=False)
        self._exhausted = True
        try:
            response = await self._bot.get_group_msg_history(
                group_id=self._scope.group_id,
                count=self._max_messages,
                reverse_order=False,
                disable_get_url=False,
                parse_mult_msg=True,
                quick_reply=False,
                reverseOrder=False,
            )
            raw_messages = _history_messages(response)
        except Exception:
            return self._page(availability="unavailable", messages=(), partial=True)

        parsed: list[_ParsedHistoryMessage] = []
        partial = len(raw_messages) >= self._max_messages
        for raw in raw_messages:
            try:
                item = _parse_history_message(
                    raw,
                    bot_self_id=self._bot.self_id,
                    request_actor_id=self._scope.request_actor_id,
                    current_message_id=self._scope.current_message_id,
                )
            except (TypeError, ValueError):
                partial = True
                continue
            parsed.append(item)

        if parsed and all(item.public.sent_at is not None for item in parsed):
            parsed.sort(key=lambda item: item.public.sent_at or 0)
        if len(parsed) > self._max_messages:
            parsed = parsed[-self._max_messages :]
            partial = True
        return self._page(
            availability="partial" if partial else "complete",
            messages=tuple(item.public for item in parsed),
            partial=partial,
        )

    def _page(
        self,
        *,
        availability: BugConversationAvailability,
        messages: tuple[BugConversationMessage, ...],
        partial: bool,
    ) -> BugConversationPage:
        return BugConversationPage(
            page_number=1,
            availability=availability,
            adapter="OneBot V11",
            platform="qq",
            conversation_type="group",
            conversation_id=str(self._scope.group_id),
            bot_id=str(self._bot.self_id),
            request_actor_id=self._scope.request_actor_id,
            request_actor_roles=self._scope.request_actor_roles,
            messages=messages,
            has_more=False,
            partial=partial,
        )


def bind_onebot_v11_bug_conversation(
    bot: OneBotV11Bot,
    event: GroupMessageEvent,
    *,
    max_messages: int = ONEBOT_HISTORY_MAX_MESSAGES,
) -> OneBotV11BugConversationBinding:
    """把读取范围固定到当前群，并预装当前事件的精确 Reply。"""
    if not isinstance(bot, OneBotV11Bot):
        raise TypeError("bot must be a OneBot V11 Bot")
    if not isinstance(event, GroupMessageEvent):
        raise TypeError("event must be a OneBot V11 group message event")

    reply = event.reply
    request_actor_id = str(event.user_id)
    scope = _GroupConversationScope(
        group_id=event.group_id,
        current_message_id=event.message_id,
        request_actor_id=request_actor_id,
        request_actor_roles=_sender_roles(event.sender.role),
    )
    reply_message = (
        None
        if reply is None
        else _reply_message(
            reply,
            bot_self_id=bot.self_id,
            request_actor_id=request_actor_id,
        )
    )

    return OneBotV11BugConversationBinding(
        reply_message=reply_message,
        history=OneBotV11GroupBugConversationReader(
            bot,
            scope,
            max_messages=max_messages,
        ),
    )


def _reply_message(
    reply: OneBotV11Reply,
    *,
    bot_self_id: str,
    request_actor_id: str,
) -> BugConversationMessage:
    sender_id = _optional_string(reply.sender.user_id)
    sender_name = reply.sender.card or reply.sender.nickname
    return BugConversationMessage(
        message_id=str(reply.message_id),
        reply_to_message_id=_reply_to_message_id(reply.message),
        sent_at=reply.time,
        sender_id=sender_id,
        sender_name=sender_name,
        sender_roles=_sender_roles(reply.sender.role),
        is_bot=sender_id == bot_self_id,
        is_request_actor=sender_id == request_actor_id,
        segment_types=_segment_types(reply.message),
        content=_visible_message_content(reply.message),
    )


def _parse_history_message(
    raw: object,
    *,
    bot_self_id: str,
    request_actor_id: str,
    current_message_id: int,
) -> _ParsedHistoryMessage:
    if not isinstance(raw, Mapping):
        raise TypeError("history message must be a mapping")
    content_source = raw.get("message")
    if content_source is None:
        content_source = raw.get("raw_message")
    if content_source is None:
        raise ValueError("history message body is unavailable")

    sender = raw.get("sender")
    sender_mapping = sender if isinstance(sender, Mapping) else {}
    sender_id = _optional_string(raw.get("user_id")) or _optional_string(
        sender_mapping.get("user_id")
    )
    sender_name = _optional_string(sender_mapping.get("card")) or _optional_string(
        sender_mapping.get("nickname")
    )
    message_id = _optional_string(raw.get("message_id"))
    return _ParsedHistoryMessage(
        public=BugConversationMessage(
            message_id=message_id,
            reply_to_message_id=_reply_to_message_id(content_source),
            sent_at=_optional_nonnegative_int(raw.get("time")),
            sender_id=sender_id,
            sender_name=sender_name,
            sender_roles=_sender_roles(sender_mapping.get("role")),
            is_bot=sender_id == bot_self_id,
            is_request_actor=sender_id == request_actor_id,
            is_current_request=message_id == str(current_message_id),
            segment_types=_segment_types(content_source),
            content=_visible_message_content(content_source),
        ),
    )


def _history_messages(response: object) -> list[object]:
    data = _response_data(response)
    messages = data.get("messages")
    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes, bytearray)):
        raise OneBotV11BugConversationError("group history response has no message list")
    return list(messages)


def _response_data(response: object) -> Mapping[str, Any]:
    if not isinstance(response, Mapping):
        raise OneBotV11BugConversationError("OneBot response must be a mapping")
    data = response.get("data")
    if isinstance(data, Mapping):
        return data
    return response


def _visible_message_content(value: object) -> str:
    if isinstance(value, Message):
        return _visible_segments(value)
    if isinstance(value, str):
        return value
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        segments: list[MessageSegment] = []
        for raw_segment in value:
            if not isinstance(raw_segment, Mapping):
                raise TypeError("OneBot message segment must be a mapping")
            segment_type = raw_segment.get("type")
            data = raw_segment.get("data")
            if not isinstance(segment_type, str) or not isinstance(data, Mapping):
                raise TypeError("OneBot message segment is malformed")
            segments.append(MessageSegment(segment_type, dict(data)))
        return _visible_segments(segments)
    raise TypeError("OneBot message body is unsupported")


def _visible_segments(segments: Sequence[MessageSegment]) -> str:
    return "".join(_visible_segment(segment) for segment in segments)


def _visible_segment(segment: MessageSegment) -> str:
    if segment.is_text():
        return str(segment.data.get("text", ""))
    label = _VISIBLE_SEGMENT_LABELS.get(segment.type, "[非文本消息]")
    fields = _VISIBLE_SEGMENT_FIELDS.get(segment.type, ())
    details = [
        f"{field}={value}"
        for field in fields
        if (value := _optional_string(segment.data.get(field))) is not None
    ]
    if not details:
        return label
    return f"{label[:-1]} {' '.join(details)}]"


def _segment_types(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return ("text",) if value else ()
    if isinstance(value, Message):
        return tuple(segment.type for segment in value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(
            segment_type
            for item in value
            if isinstance(item, Mapping) and isinstance((segment_type := item.get("type")), str)
        )
    return ()


def _reply_to_message_id(value: object) -> str | None:
    if isinstance(value, Message):
        segments: Sequence[object] = value
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        segments = value
    else:
        return None
    for segment in segments:
        if isinstance(segment, MessageSegment):
            segment_type = segment.type
            data: object = segment.data
        elif isinstance(segment, Mapping):
            segment_type = segment.get("type")
            data = segment.get("data")
        else:
            continue
        if segment_type != "reply" or not isinstance(data, Mapping):
            continue
        return _optional_string(data.get("id"))
    return None


def _sender_roles(value: object) -> tuple[str, ...]:
    role = _optional_string(value)
    return (role,) if role is not None else ()


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _optional_nonnegative_int(value: object) -> int | None:
    result = _optional_int(value)
    return result if result is not None and result >= 0 else None


def _optional_string(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    result = str(value)
    return result or None


__all__ = (
    "ONEBOT_HISTORY_MAX_MESSAGES",
    "OneBotV11BugConversationBinding",
    "OneBotV11BugConversationError",
    "OneBotV11GroupBugConversationReader",
    "bind_onebot_v11_bug_conversation",
)
