from nonebot import on_command

index_refresh = on_command(
    "清理索引",
    aliases={"刷新索引", "重建索引", "同步索引"},
)


@index_refresh.handle()
async def handle_index_refresh():
    await index_refresh.finish("索引已更新")
