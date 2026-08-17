from nonebot import on_command
from nonebot_plugin_uninfo import OWNER

notice = on_command("设置群公告", permission=OWNER())


@notice.handle()
async def handle_notice(text: str):
    await notice.finish(f"群公告已更新为：{text}")
