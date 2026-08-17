from nonebot import on_command
from nonebot_plugin_uninfo import ADMIN

cleanup = on_command("清理消息", permission=ADMIN())


@cleanup.handle()
async def handle_cleanup(count: int = 10):
    await cleanup.finish(f"已清理最近 {count} 条消息")
