from arclet.alconna import Alconna, Args
from nonebot_plugin_alconna import (
    AlconnaMatch,
    Image,
    Match,
    image_fetch,
    on_alconna,
)

plant = on_alconna(
    Alconna("识别植物", Args["image", Image]),
    aliases={"植物识别"},
)


@plant.handle()
async def handle_plant(
    image: Match[bytes] = AlconnaMatch("image", image_fetch),
):
    await plant.finish(f"识别到图片共 {len(image.result)} 字节")
