from nonebot import on_command
from nonebot.rule import to_me

ping = on_command("延迟", rule=to_me())


@ping.handle()
async def handle_ping():
    await ping.finish("当前延迟为 20ms")
