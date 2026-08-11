import asyncio
import json
from types import SimpleNamespace

from nonebot.adapters.qq.event import DirectMessageCreateEvent, GuildMessageEvent
from nonebot_plugin_uninfo.adapters.qq.main import fetcher


class FakeBot:
    self_id = "bot"

    async def get_guild(self, *, guild_id: str) -> SimpleNamespace:
        return SimpleNamespace(id=guild_id, name="guild", icon=None)

    async def get_channel(self, *, channel_id: str) -> SimpleNamespace:
        return SimpleNamespace(
            id=channel_id,
            guild_id="guild",
            name="channel",
            type=0,
        )


def make_event(event_type: type[GuildMessageEvent]) -> GuildMessageEvent:
    return event_type(
        id="message",
        channel_id="channel",
        guild_id="guild",
        author={"id": "user", "username": "user"},
    )


async def to_session(bot: FakeBot, event: GuildMessageEvent):
    assert fetcher.wildcard is not None
    data = await fetcher.wildcard(bot, event)  # type: ignore[arg-type]
    return fetcher.parse({**fetcher.supply_self(bot), **data})  # type: ignore[arg-type]


async def main() -> None:
    bot = FakeBot()
    direct = await to_session(bot, make_event(DirectMessageCreateEvent))
    public = await to_session(bot, make_event(GuildMessageEvent))
    print(
        json.dumps(
            {
                "direct_scene_id": direct.scene.id,
                "direct_scene_type": direct.scene.type.name,
                "direct_is_private": direct.scene.is_private,
                "public_scene_id": public.scene.id,
                "public_scene_type": public.scene.type.name,
                "public_is_private": public.scene.is_private,
            },
            sort_keys=True,
        )
    )


asyncio.run(main())
