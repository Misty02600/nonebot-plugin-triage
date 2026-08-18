from nonebot import on_command
from nonebot.adapters import Message
from nonebot.params import CommandArg
from nonebot_plugin_uninfo import Uninfo
from pydantic import BaseModel


class Config(BaseModel):
    user_cooldown_seconds: int = 23
    scene_daily_quota: int = 13


plugin_config = Config()
poster = on_command("生成海报")
last_generated_at: dict[str, int] = {}
scene_generated: dict[str, int] = {}


@poster.handle()
async def handle_poster(session: Uninfo, now: int, args: Message = CommandArg()):
    topic = args.extract_plain_text().strip()
    if not topic:
        await poster.finish("请提供海报主题")
    if now - last_generated_at[session.user.id] < plugin_config.user_cooldown_seconds:
        await poster.finish("操作过于频繁，请稍后再试")
    if scene_generated[session.scene.id] >= plugin_config.scene_daily_quota:
        await poster.finish("本群今日海报次数已用完")
    await poster.finish(topic.encode())
