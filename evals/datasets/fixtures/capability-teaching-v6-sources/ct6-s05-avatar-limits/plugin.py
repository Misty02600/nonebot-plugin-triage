from nonebot import on_command
from nonebot.adapters import Message
from nonebot.params import CommandArg
from nonebot_plugin_uninfo import Uninfo
from pydantic import BaseModel


class Config(BaseModel):
    user_cooldown_seconds: int = 18
    scene_daily_quota: int = 30


plugin_config = Config()
avatar = on_command("生成头像")
last_generated_at: dict[str, int] = {}
scene_generated: dict[str, int] = {}


@avatar.handle()
async def handle_avatar(
    session: Uninfo,
    now: int,
    args: Message = CommandArg(),
):
    user_id = session.user.id
    scene_id = session.scene.id
    description = args.extract_plain_text().strip()
    if not description:
        await avatar.finish("请提供头像描述")
    if now - last_generated_at[user_id] < plugin_config.user_cooldown_seconds:
        await avatar.finish("操作过于频繁，请稍后再试")
    if scene_generated[scene_id] >= plugin_config.scene_daily_quota:
        await avatar.finish("本群今日生成次数已用完")
    await avatar.finish(description.encode())
