from arclet.alconna import Alconna, Args
from nonebot_plugin_alconna import AlconnaMatch, Image, Match, image_fetch, on_alconna

FILTERS = ("水彩", "素描", "油画", "像素", "胶片")


def create_filter(filter_name: str):
    matcher = on_alconna(Alconna(f"画{filter_name}图", Args["image", Image]))

    @matcher.handle()
    async def handle_filter(image: Match[bytes] = AlconnaMatch("image", image_fetch)):
        await matcher.finish(filter_name.encode() + image.result)

    return matcher


for filter_name in FILTERS:
    create_filter(filter_name)
