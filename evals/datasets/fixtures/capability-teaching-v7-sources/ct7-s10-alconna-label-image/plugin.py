from arclet.alconna import Alconna, Args
from nonebot_plugin_alconna import AlconnaMatch, Image, Match, image_fetch, on_alconna

label = on_alconna(Alconna("识别标签", Args["image", Image]))


@label.handle()
async def handle_label(
    image: Match[bytes] = AlconnaMatch("image", image_fetch),
):
    await label.finish(f"图片大小：{len(image.result)}")
