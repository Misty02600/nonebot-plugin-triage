from nonebot import on_command
from pydantic import BaseModel


class Config(BaseModel):
    max_topics: int = 8


plugin_config = Config()
topics = on_command("热门话题")


async def list_hot_topics() -> list[str]:
    return [f"话题 {index}" for index in range(1, 21)]


@topics.handle()
async def handle_topics():
    result = await list_hot_topics()
    await topics.finish(result[: plugin_config.max_topics])
