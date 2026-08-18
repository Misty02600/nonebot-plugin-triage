from nonebot import on_command


async def public_status_allowed() -> bool:
    return True


status = on_command("公开状态", permission=public_status_allowed)


@status.handle()
async def handle_status():
    await status.finish("当前公开状态正常")
