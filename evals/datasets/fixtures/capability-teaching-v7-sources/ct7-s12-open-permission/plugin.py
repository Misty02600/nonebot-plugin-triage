from nonebot import on_command


async def public_status_allowed() -> bool:
    return True


async def read_public_status() -> str:
    return "当前服务状态正常"


status = on_command("公开状态", permission=public_status_allowed)


@status.handle()
async def handle_status():
    await status.finish(await read_public_status())
