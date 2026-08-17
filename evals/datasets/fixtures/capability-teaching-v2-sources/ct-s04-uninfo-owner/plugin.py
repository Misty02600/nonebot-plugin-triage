from nonebot import on_command
from nonebot_plugin_uninfo import OWNER

transfer = on_command("移交群设置", permission=OWNER())


@transfer.handle()
async def handle_transfer(user: str):
    await transfer.finish(f"已把群设置管理权移交给 {user}")
