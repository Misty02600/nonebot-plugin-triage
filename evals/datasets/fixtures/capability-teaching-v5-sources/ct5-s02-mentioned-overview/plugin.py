from nonebot import on_command
from nonebot.rule import to_me

overview = on_command("服务概况", rule=to_me())


async def build_public_overview() -> str:
    return "服务运行正常"


@overview.handle()
async def handle_overview():
    await overview.finish(await build_public_overview())
