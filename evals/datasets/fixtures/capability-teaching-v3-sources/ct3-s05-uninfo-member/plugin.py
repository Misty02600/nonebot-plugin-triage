from nonebot import on_command
from nonebot_plugin_uninfo import MEMBER

profile = on_command("我的群资料", permission=MEMBER())


@profile.handle()
async def handle_profile():
    await profile.finish("已生成群资料卡片")
