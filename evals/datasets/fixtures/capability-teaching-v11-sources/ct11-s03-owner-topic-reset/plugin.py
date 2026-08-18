from nonebot import on_command
from nonebot_plugin_uninfo import OWNER

reset = on_command("重置群主题", permission=OWNER())


@reset.handle()
async def handle_reset():
    await reset.finish("已重置群主题")
