import asyncio
import json
from types import SimpleNamespace

import nonebot

nonebot.init(driver="~none")

from nonebot.adapters.satori.models import ChannelType  # noqa: E402
from nonebot_plugin_uninfo.adapters.satori.main import fetcher  # noqa: E402


class FakeBot:
    self_id = "bot"
    platform = "satori-test"
    _self_info = SimpleNamespace(features=set())


def make_event(channel_type: ChannelType) -> SimpleNamespace:
    return SimpleNamespace(
        self_id="bot",
        user=None,
        guild=SimpleNamespace(id="guild", name="guild", avatar=None),
        channel=SimpleNamespace(id="peer-channel", name="channel", type=channel_type),
        member=SimpleNamespace(nick="member", joined_at=None, avatar=None),
        role=None,
        operator=None,
    )


async def to_session(bot: FakeBot, channel_type: ChannelType):
    assert fetcher.wildcard is not None
    data = await fetcher.wildcard(bot, make_event(channel_type))  # type: ignore[arg-type]
    return fetcher.parse({**fetcher.supply_self(bot), **data})  # type: ignore[arg-type]


async def main() -> None:
    bot = FakeBot()
    direct = await to_session(bot, ChannelType.DIRECT)
    public = await to_session(bot, ChannelType.TEXT)
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
