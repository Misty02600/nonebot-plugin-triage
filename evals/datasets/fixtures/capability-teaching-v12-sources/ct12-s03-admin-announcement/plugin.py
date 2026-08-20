from nonebot import on_command
from nonebot_plugin_uninfo import ADMIN

clear_announcement = on_command("清理群公告", permission=ADMIN())


@clear_announcement.handle()
async def handle_clear_announcement():
    await clear_announcement.finish("已清理过期群公告")
