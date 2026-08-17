from nonebot import on_command
from pydantic import BaseModel


class Config(BaseModel):
    search_enabled: bool = True
    result_count: int = 6


plugin_config = Config()
search = on_command("资料搜索")


@search.handle()
async def handle_search(keyword: str):
    if not plugin_config.search_enabled:
        await search.finish("资料搜索当前未开放")
    results = await query_documents(keyword, limit=plugin_config.result_count)
    await search.finish(results)
