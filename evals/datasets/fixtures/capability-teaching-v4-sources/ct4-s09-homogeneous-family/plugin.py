from arclet.alconna import Alconna
from nonebot_plugin_alconna import on_alconna

TONES = ("暖色", "冷色", "高对比")


def register_tones(prefix: str):
    for name in TONES:
        matcher = on_alconna(Alconna(f"{prefix}{name}"))

        @matcher.handle()
        async def handle_tone(image: bytes):
            await matcher.finish(await apply_tone(name, image))


register_tones("&")
