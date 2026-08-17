from arclet.alconna import Alconna
from nonebot_plugin_alconna import on_alconna

FILTERS = ("复古", "锐化", "黑白")


def register_filters(prefix: str):
    for name in FILTERS:
        matcher = on_alconna(Alconna(f"{prefix}{name}"))

        @matcher.handle()
        async def handle_filter(image: bytes):
            await matcher.finish(await apply_filter(name, image))


register_filters("$")
