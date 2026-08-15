from __future__ import annotations

from dataclasses import dataclass

import pytest

from nbtriage.bug_conversation import BugConversationMessage, BugConversationPage
from nonebot_plugin_triage.uninfo_participants import (
    _UninfoConversationReader,
)


class _Reader:
    async def read_next(self) -> BugConversationPage:
        return BugConversationPage(
            page_number=1,
            request_actor_id="actor",
            messages=(
                BugConversationMessage(
                    sender_id="actor",
                    sender_name=None,
                    sender_roles=("member",),
                    is_bot=False,
                    content="当前报障者",
                ),
                BugConversationMessage(
                    sender_id="other",
                    sender_name=None,
                    is_bot=False,
                    content="其他成员",
                ),
                BugConversationMessage(
                    sender_id="native",
                    sender_roles=("owner",),
                    is_bot=False,
                    content="平台已有角色",
                ),
            ),
            has_more=False,
            partial=False,
        )


@dataclass
class _Role:
    name: str


@dataclass
class _User:
    name: str


@dataclass
class _Member:
    user: _User
    nick: str
    roles: list[_Role]


@pytest.mark.anyio
async def test_uninfo_enrichment_queries_only_missing_historical_members() -> None:
    queried: list[str] = []

    async def lookup(user_id: str):
        queried.append(user_id)
        return _Member(_User("其他用户"), "其他群名片", [_Role("ADMINISTRATOR")])

    reader = _UninfoConversationReader(
        _Reader(),
        member_lookup=lookup,
        request_actor_id="actor",
        request_actor_name="报障者",
        request_actor_roles=("MEMBER",),
    )

    page = await reader.read_next()

    assert page.request_actor_roles == ("MEMBER",)
    assert page.messages[0].sender_name == "报障者"
    assert page.messages[0].sender_current_roles == ("MEMBER",)
    assert page.messages[1].sender_name == "其他群名片"
    assert page.messages[1].sender_current_roles == ("ADMINISTRATOR",)
    assert page.messages[2].sender_current_roles == ()
    assert queried == ["other"]
