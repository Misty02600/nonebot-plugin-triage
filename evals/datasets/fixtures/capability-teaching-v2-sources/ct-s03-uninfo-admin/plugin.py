from nonebot import on_command
from nonebot_plugin_uninfo import ADMIN

mute = on_command("禁言", permission=ADMIN())


@mute.handle()
async def handle_mute(user: str, minutes: int = 10):
    await mute.finish(f"已将 {user} 禁言 {minutes} 分钟")
