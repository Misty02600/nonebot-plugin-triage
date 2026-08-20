from nonebot import on_command
from pydantic import BaseModel


class Config(BaseModel):
    max_exhibition_items: int = 7


plugin_config = Config()
exhibitions = on_command("近期展览")


@exhibitions.handle()
async def handle_exhibitions():
    items = [f"展览 {index}" for index in range(1, 30)]
    await exhibitions.finish(items[: plugin_config.max_exhibition_items])
