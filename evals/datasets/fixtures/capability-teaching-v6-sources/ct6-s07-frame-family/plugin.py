from arclet.alconna import Alconna, Args
from nonebot_plugin_alconna import (
    AlconnaMatch,
    Image,
    Match,
    image_fetch,
    on_alconna,
)

FRAMES = ("拍立得", "邮票", "胶片", "木纹", "星空", "花边")


async def add_frame(frame_name: str, image: bytes) -> bytes:
    return frame_name.encode() + image


def create_frame(frame_name: str):
    matcher = on_alconna(Alconna(f"^{frame_name}", Args["image", Image]))

    @matcher.handle()
    async def handle_frame(
        image: Match[bytes] = AlconnaMatch("image", image_fetch),
    ):
        await matcher.finish(await add_frame(frame_name, image.result))

    return matcher


for frame_name in FRAMES:
    create_frame(frame_name)
