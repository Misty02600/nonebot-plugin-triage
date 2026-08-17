from arclet.alconna import Alconna
from nonebot_plugin_alconna import on_alconna

TEMPLATES = {
    "拥抱": {"inputs": ["图片"]},
    "字幕": {"inputs": ["图片", "文字"]},
    "贴纸": {"inputs": ["图片"]},
}


def register_templates(prefix: str):
    for name, metadata in TEMPLATES.items():
        matcher = on_alconna(Alconna(f"{prefix}{name}"))

        @matcher.handle()
        async def handle_template():
            material = await collect_material(metadata["inputs"])
            await matcher.finish(await render_template(name, material))


register_templates("!")
