from nonebot import on_command
from pydantic import BaseModel


class Config(BaseModel):
    max_news_items: int = 7


plugin_config = Config()
news = on_command("近期动态")


async def list_recent_news() -> list[str]:
    return [f"动态 {index}" for index in range(1, 21)]


@news.handle()
async def handle_news():
    items = await list_recent_news()
    await news.finish(items[: plugin_config.max_news_items])
