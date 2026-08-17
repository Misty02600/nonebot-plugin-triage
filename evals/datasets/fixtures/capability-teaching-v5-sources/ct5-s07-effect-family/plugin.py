from arclet.alconna import Alconna
from nonebot_plugin_alconna import on_alconna

EFFECTS = ("油画", "素描", "浮雕", "霓虹", "像素")


async def apply_effect(effect_name: str, image: bytes) -> bytes:
    return effect_name.encode() + image


def create_effect(prefix: str, effect_name: str):
    matcher = on_alconna(Alconna(f"{prefix}{effect_name}"))

    @matcher.handle()
    async def handle_effect(image: bytes):
        await matcher.finish(await apply_effect(effect_name, image))

    return matcher


for effect_name in EFFECTS:
    create_effect("~", effect_name)
