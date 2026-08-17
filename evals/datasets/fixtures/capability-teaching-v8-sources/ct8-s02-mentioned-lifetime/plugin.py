from nonebot import on_command
from nonebot.rule import to_me

lifetime = on_command("存活时长", rule=to_me())


@lifetime.handle()
async def handle_lifetime():
    await lifetime.finish("已连续运行 36 小时")
