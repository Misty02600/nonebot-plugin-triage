from nonebot import on_command


async def service_is_available() -> bool:
    return True


async def read_public_service_time() -> str:
    return "每日 09:00 至 21:00"


service_time = on_command("服务时间", permission=service_is_available)


@service_time.handle()
async def handle_service_time():
    await service_time.finish(await read_public_service_time())
