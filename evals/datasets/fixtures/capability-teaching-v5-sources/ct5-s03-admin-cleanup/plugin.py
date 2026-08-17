from nonebot import on_command
from nonebot_plugin_uninfo import ADMIN

cleanup = on_command("清理缓存", permission=ADMIN())


async def clear_public_cache(scope: str | None) -> str:
    return f"已清理 {scope or '默认'} 缓存"


@cleanup.handle()
async def handle_cleanup(scope: str | None = None):
    await cleanup.finish(await clear_public_cache(scope))
