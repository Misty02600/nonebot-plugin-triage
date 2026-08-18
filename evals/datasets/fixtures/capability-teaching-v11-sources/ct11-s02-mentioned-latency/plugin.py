from nonebot import on_command
from nonebot.rule import to_me

latency = on_command("延迟概览", rule=to_me())


@latency.handle()
async def handle_latency():
    await latency.finish("当前延迟 42 毫秒")
