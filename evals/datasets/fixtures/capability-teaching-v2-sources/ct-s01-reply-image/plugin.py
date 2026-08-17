from nonebot import on_command

search = on_command("识图", aliases={"找图"})


async def find_source(image: bytes) -> str:
    return "图片来源"


@search.handle()
async def handle_search(image: bytes | None):
    if image is None:
        await search.send("请发送图片，或回复一张图片")
        image = await search.receive()
    result = await find_source(image)
    await search.finish(result)
