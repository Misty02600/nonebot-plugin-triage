from nonebot import on_command
from pydantic import BaseModel


class Config(BaseModel):
    max_news_items: int = 7


plugin_config = Config()
news = on_command("近期新闻")


@news.handle()
async def handle_news():
    items = [f"新闻 {index}" for index in range(1, 20)]
    await news.finish(items[: plugin_config.max_news_items])
