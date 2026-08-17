from nonebot import on_command
from nonebot.rule import to_me

uptime = on_command("服务时间", rule=to_me())


@uptime.handle()
async def handle_uptime():
    await uptime.finish("服务已连续运行 12 小时")
