from nonebot import on_command
from nonebot.rule import to_me

latency = on_command("节点延迟", rule=to_me())


async def read_public_latency() -> str:
    return "当前节点延迟 42 ms"


@latency.handle()
async def handle_latency():
    await latency.finish(await read_public_latency())
