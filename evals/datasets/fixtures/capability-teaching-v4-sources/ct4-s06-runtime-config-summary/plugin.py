from nonebot import on_command
from pydantic import BaseModel


class Config(BaseModel):
    summary_enabled: bool = True
    max_characters: int = 300


plugin_config = Config()
summary = on_command("摘要")


@summary.handle()
async def handle_summary(text: str):
    if not plugin_config.summary_enabled:
        await summary.finish("摘要功能当前未开放")
    if len(text) > plugin_config.max_characters:
        await summary.finish("文本过长")
    await summary.finish(await summarize_text(text))
