from nonebot import on_command
from pydantic import BaseModel


class Config(BaseModel):
    user_cooldown_seconds: int = 25
    scene_daily_quota: int = 60


plugin_config = Config()
cover = on_command("生成封面")
last_created_at: dict[str, int] = {}
daily_created: dict[str, int] = {}


async def render_cover(description: str) -> bytes:
    return description.encode()


@cover.handle()
async def handle_cover(user_id: str, scene_id: str, description: str, now: int):
    if now - last_created_at[user_id] < plugin_config.user_cooldown_seconds:
        await cover.finish("操作过于频繁，请稍后再试")
    if daily_created[scene_id] >= plugin_config.scene_daily_quota:
        await cover.finish("本群今日生成次数已用完")
    await cover.finish(await render_cover(description))
