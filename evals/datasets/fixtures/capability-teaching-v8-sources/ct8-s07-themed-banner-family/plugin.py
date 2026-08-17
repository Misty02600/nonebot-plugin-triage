from arclet.alconna import Alconna, Args
from nonebot_plugin_alconna import AlconnaMatch, Image, Match, image_fetch, on_alconna

THEMES = ("蓝海", "星夜", "晚霞", "森林", "雪原")


def create_banner(theme_name: str):
    matcher = on_alconna(Alconna(f"&{theme_name}卡", Args["image", Image]))

    @matcher.handle()
    async def handle_banner(image: Match[bytes] = AlconnaMatch("image", image_fetch)):
        await matcher.finish(theme_name.encode() + image.result)

    return matcher


for theme_name in THEMES:
    create_banner(theme_name)
