from nonebot import on_command
from pydantic import BaseModel


class Config(BaseModel):
    max_topic_items: int = 9


plugin_config = Config()
topics = on_command("热门话题")


@topics.handle()
async def handle_topics():
    items = [f"话题 {index}" for index in range(1, 30)]
    await topics.finish(items[: plugin_config.max_topic_items])
