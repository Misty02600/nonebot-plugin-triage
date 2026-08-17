from arclet.alconna import Alconna, Args
from nonebot_plugin_alconna import AlconnaMatch, Image, Match, image_fetch, on_alconna

color = on_alconna(Alconna("提取色彩", Args["image", Image]))


@color.handle()
async def handle_color(image: Match[bytes] = AlconnaMatch("image", image_fetch)):
    await color.finish(f"已分析 {len(image.result)} 字节")
