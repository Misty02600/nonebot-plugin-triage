from nonebot import on_command
from nonebot.rule import to_me
from nonebot_plugin_uninfo import GROUP


def create_air_handler(region: str):
    async def handle_air():
        return f"{region}当前空气质量"

    return handle_air


north_handler = create_air_handler("北区")
south_handler = create_air_handler("南区")
harbor_handler = create_air_handler("港区")

north = on_command(
    "查北区空气",
    permission=GROUP(),
    rule=to_me(),
    handlers=[north_handler],
)
south = on_command(
    "查南区空气",
    permission=GROUP(),
    rule=to_me(),
    handlers=[south_handler],
)
harbor = on_command(
    "查港区空气",
    permission=GROUP(),
    rule=to_me(),
    handlers=[harbor_handler],
)
