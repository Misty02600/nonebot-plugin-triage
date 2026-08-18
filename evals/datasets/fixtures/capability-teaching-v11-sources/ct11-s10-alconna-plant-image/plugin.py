from arclet.alconna import Alconna, Args
from nonebot_plugin_alconna import AlconnaMatch, Image, Match, image_fetch, on_alconna

plant = on_alconna(Alconna("识别植物", Args["image", Image]))


@plant.handle()
async def handle_plant(image: Match[bytes] = AlconnaMatch("image", image_fetch)):
    await plant.finish(f"已识别 {len(image.result)} 字节")
