from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

BUG_CONVERSATION_SCHEMA_VERSION = 2

BugConversationAvailability = Literal[
    "complete",
    "partial",
    "unavailable",
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
    )


class BugConversationMessage(_StrictModel):
    """一次 Bug 判断可读取的可见聊天消息。"""

    schema_version: Literal[2] = BUG_CONVERSATION_SCHEMA_VERSION
    message_id: str | None = None
    reply_to_message_id: str | None = None
    sent_at: int | None = Field(default=None, ge=0)
    sender_id: str | None = None
    sender_name: str | None = None
    # 平台消息携带的发送时角色快照。
    sender_roles: tuple[str, ...] = ()
    # Uninfo 在调查时查询到的当前角色；不得覆盖发送时角色。
    sender_current_roles: tuple[str, ...] = ()
    is_bot: bool
    is_request_actor: bool | None = None
    is_current_request: bool = False
    segment_types: tuple[str, ...] = ()
    content: str = Field(repr=False)


class BugConversationPage(_StrictModel):
    """由已绑定会话读取器返回的一次有界聊天窗口。"""

    schema_version: Literal[2] = BUG_CONVERSATION_SCHEMA_VERSION
    page_number: int = Field(ge=1)
    availability: BugConversationAvailability = "complete"
    adapter: str | None = None
    platform: str | None = None
    conversation_type: str | None = None
    conversation_id: str | None = None
    bot_id: str | None = None
    request_actor_id: str | None = None
    request_actor_roles: tuple[str, ...] = ()
    messages: tuple[BugConversationMessage, ...]
    has_more: bool
    partial: bool


class BoundBugConversationReader(Protocol):
    """只读取入口预绑定会话的最新窗口，不接受 scope 或消息 ID。"""

    async def read_next(self) -> BugConversationPage: ...


__all__ = (
    "BUG_CONVERSATION_SCHEMA_VERSION",
    "BoundBugConversationReader",
    "BugConversationAvailability",
    "BugConversationMessage",
    "BugConversationPage",
)
