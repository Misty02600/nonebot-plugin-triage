from arclet.alconna import Alconna, Args
from nonebot_plugin_alconna import AlconnaMatch, Image, Match, image_fetch, on_alconna

THEMES = ("春日", "夏夜", "秋野", "冬雪", "晨雾")


async def render_card(theme_name: str, image: bytes) -> bytes:
    return theme_name.encode() + image


def create_theme(theme_name: str):
    matcher = on_alconna(Alconna(f"~{theme_name}款", Args["image", Image]))

    @matcher.handle()
    async def handle_theme(
        image: Match[bytes] = AlconnaMatch("image", image_fetch),
    ):
        await matcher.finish(await render_card(theme_name, image.result))

    return matcher


for theme_name in THEMES:
    create_theme(theme_name)
