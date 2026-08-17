from nonebot import on_command
from pydantic import BaseModel


class Config(BaseModel):
    user_cooldown_seconds: int = 45
    scene_hourly_quota: int = 20


plugin_config = Config()
oracle = on_command("占卜")


@oracle.handle()
async def handle_oracle(user_id: str, scene_id: str, now: int):
    if now - last_used[user_id] < plugin_config.user_cooldown_seconds:
        await oracle.finish("请稍后再试")
    if hourly_count[scene_id] >= plugin_config.scene_hourly_quota:
        await oracle.finish("本群本小时次数已用完")
    await oracle.finish(await draw_oracle())
