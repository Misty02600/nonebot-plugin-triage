from nonebot import on_command
from nonebot_plugin_uninfo import ADMIN

mute = on_command("禁言", permission=ADMIN())


@mute.handle()
async def handle_mute(user: str, duration: int = 600):
    await mute.finish(await mute_user(user, duration))
