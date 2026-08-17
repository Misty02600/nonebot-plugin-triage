from __future__ import annotations

from collections.abc import Awaitable, Callable
from importlib import import_module
from typing import Any

from nonebot.adapters import Bot, Event

from nbtriage.bug_conversation import (
    BoundBugConversationReader,
    BugConversationMessage,
    BugConversationPage,
)

MemberLookup = Callable[[str], Awaitable[object | None]]


async def enrich_conversation_with_uninfo(
    bot: Bot,
    event: Event,
    reader: BoundBugConversationReader | None,
) -> BoundBugConversationReader | None:
    """返回惰性 Uninfo 投影，只有 Agent 真正读取聊天时才查询成员。"""
    if reader is None:
        return None
    return _LazyUninfoConversationReader(bot, event, reader)


class _LazyUninfoConversationReader:
    def __init__(
        self,
        bot: Bot,
        event: Event,
        delegate: BoundBugConversationReader,
    ) -> None:
        self._bot = bot
        self._event = event
        self._delegate = delegate
        self._resolved: BoundBugConversationReader | None = None

    async def read_next(self) -> BugConversationPage:
        if self._resolved is None:
            self._resolved = await _resolve_uninfo_reader(
                self._bot,
                self._event,
                self._delegate,
            )
        return await self._resolved.read_next()


async def _resolve_uninfo_reader(
    bot: Bot,
    event: Event,
    reader: BoundBugConversationReader,
) -> BoundBugConversationReader:
    try:
        uninfo = import_module("nonebot_plugin_uninfo")
        get_interface = uninfo.get_interface
        get_session = uninfo.get_session
    except (AttributeError, ImportError):
        return reader
    try:
        session = await get_session(bot, event)
        interface = get_interface(bot)
    except Exception:
        return reader
    if session is None:
        return reader

    actor_roles = _member_roles(getattr(session, "member", None))
    actor_name = _member_name(getattr(session, "member", None))
    scene = getattr(session, "scene", None)
    parent = getattr(scene, "parent", None)
    member_scene = parent or scene
    scene_type = getattr(member_scene, "type", None)
    scene_id = getattr(member_scene, "id", None)

    async def lookup(user_id: str) -> object | None:
        if interface is None or scene_type is None or scene_id is None:
            return None
        try:
            return await interface.get_member(scene_type, str(scene_id), user_id)
        except Exception:
            return None

    return _UninfoConversationReader(
        reader,
        member_lookup=lookup,
        request_actor_id=event.get_user_id(),
        request_actor_name=actor_name,
        request_actor_roles=actor_roles,
    )


class _UninfoConversationReader:
    def __init__(
        self,
        delegate: BoundBugConversationReader,
        *,
        member_lookup: MemberLookup,
        request_actor_id: str,
        request_actor_name: str | None,
        request_actor_roles: tuple[str, ...],
    ) -> None:
        self._delegate = delegate
        self._member_lookup = member_lookup
        self._request_actor_id = request_actor_id
        self._request_actor_name = request_actor_name
        self._request_actor_roles = request_actor_roles
        self._members: dict[str, object | None] = {}

    async def read_next(self) -> BugConversationPage:
        page = await self._delegate.read_next()
        messages: list[BugConversationMessage] = []
        for message in page.messages:
            messages.append(await self._enrich_message(message))
        return page.model_copy(
            update={
                "request_actor_roles": (self._request_actor_roles or page.request_actor_roles),
                "messages": tuple(messages),
            }
        )

    async def _enrich_message(
        self,
        message: BugConversationMessage,
    ) -> BugConversationMessage:
        if message.sender_id is None or message.is_bot:
            return message
        if message.sender_id == self._request_actor_id:
            return message.model_copy(
                update={
                    "sender_name": message.sender_name or self._request_actor_name,
                    "sender_current_roles": self._request_actor_roles,
                }
            )
        if message.sender_roles or message.sender_current_roles:
            return message
        if message.sender_id not in self._members:
            self._members[message.sender_id] = await self._member_lookup(message.sender_id)
        member = self._members[message.sender_id]
        if member is None:
            return message
        return message.model_copy(
            update={
                "sender_name": message.sender_name or _member_name(member),
                "sender_current_roles": _member_roles(member),
            }
        )


def _member_name(member: object | None) -> str | None:
    if member is None:
        return None
    nick = _optional_string(getattr(member, "nick", None))
    if nick is not None:
        return nick
    user = getattr(member, "user", None)
    return _optional_string(getattr(user, "name", None)) or _optional_string(
        getattr(user, "nick", None)
    )


def _member_roles(member: object | None) -> tuple[str, ...]:
    if member is None:
        return ()
    values = getattr(member, "roles", None)
    if values is None:
        role = getattr(member, "role", None)
        values = () if role is None else (role,)
    roles: list[str] = []
    for value in values:
        role = _optional_string(getattr(value, "name", None)) or _optional_string(
            getattr(value, "id", None)
        )
        if role is not None and role not in roles:
            roles.append(role)
    return tuple(roles)


def _optional_string(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (str, int)):
        result = str(value)
        return result or None
    return None


__all__ = ("enrich_conversation_with_uninfo",)
