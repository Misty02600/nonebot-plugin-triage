from nonebot import on_command
from nonebot.rule import to_me

health = on_command("运行概况", rule=to_me())


@health.handle()
async def handle_health():
    await health.finish(await public_health_summary())
