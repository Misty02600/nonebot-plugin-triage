from nonebot import on_command


async def public_query_allowed() -> bool:
    return True


async def read_public_version() -> str:
    return "当前公开版本为 2.0"


version = on_command("公开版本", permission=public_query_allowed)


@version.handle()
async def handle_version():
    await version.finish(await read_public_version())
