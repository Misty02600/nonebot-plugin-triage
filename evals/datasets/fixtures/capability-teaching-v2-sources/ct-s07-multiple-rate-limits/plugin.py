from nonebot import on_command
from pydantic import BaseModel


class Config(BaseModel):
    user_cooldown_seconds: int = 30
    scene_daily_quota: int = 100


plugin_config = Config()
draw = on_command("抽签")


@draw.handle()
async def handle_draw(user_id: str, scene_id: str, now: int):
    if now - last_used[user_id] < plugin_config.user_cooldown_seconds:
        await draw.finish("使用太快了，请稍后再试")
    if daily_count[scene_id] >= plugin_config.scene_daily_quota:
        await draw.finish("本群今天的抽签次数已经用完")
    await draw.finish(await draw_one())
