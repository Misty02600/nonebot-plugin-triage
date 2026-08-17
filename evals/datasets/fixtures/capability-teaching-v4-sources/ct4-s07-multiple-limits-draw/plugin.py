from nonebot import on_command
from pydantic import BaseModel


class Config(BaseModel):
    user_cooldown_seconds: int = 30
    scene_daily_quota: int = 100


plugin_config = Config()
draw = on_command("抽签")


@draw.handle()
async def handle_draw(user_id: str, scene_id: str, now: int):
    if now - last_draw[user_id] < plugin_config.user_cooldown_seconds:
        await draw.finish("请稍后再抽")
    if daily_draws[scene_id] >= plugin_config.scene_daily_quota:
        await draw.finish("本群今日次数已用完")
    await draw.finish(await draw_lot())
