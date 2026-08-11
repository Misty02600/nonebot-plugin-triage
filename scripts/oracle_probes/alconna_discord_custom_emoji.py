import json

from nonebot.adapters.discord.message import CustomEmojiSegment
from nonebot_plugin_alconna.uniseg.adapters.discord.builder import (
    DiscordMessageBuilder,
)

segment = CustomEmojiSegment(
    "custom_emoji",
    {"id": "123456", "name": "wave", "animated": False},
)
converted = DiscordMessageBuilder().convert(segment)

print(
    json.dumps(
        {
            "type": type(converted).__name__,
            "id": getattr(converted, "id", None),
            "url": getattr(converted, "url", None),
            "name": getattr(converted, "name", None),
        },
        sort_keys=True,
    )
)
