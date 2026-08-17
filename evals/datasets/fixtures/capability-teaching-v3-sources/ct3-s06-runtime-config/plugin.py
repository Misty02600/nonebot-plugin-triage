from nonebot import on_command
from pydantic import BaseModel


class Config(BaseModel):
    translate_enabled: bool = True
    max_characters: int = 500


plugin_config = Config()
translate = on_command("翻译")


@translate.handle()
async def handle_translate(text: str):
    if not plugin_config.translate_enabled:
        await translate.finish("翻译功能当前未开放")
    if len(text) > plugin_config.max_characters:
        await translate.finish("文本过长")
    await translate.finish(await translate_text(text))
