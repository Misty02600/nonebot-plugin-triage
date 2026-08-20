from nonebot import on_command
from nonebot.adapters import Message
from nonebot.params import CommandArg
from nonebot_plugin_uninfo import Uninfo
from pydantic import BaseModel


class Config(BaseModel):
    user_cooldown_seconds: int = 31
    scene_daily_quota: int = 17


plugin_config = Config()
collage = on_command("生成拼图")
last_generated_at: dict[str, int] = {}
scene_generated: dict[str, int] = {}


@collage.handle()
async def handle_collage(session: Uninfo, now: int, args: Message = CommandArg()):
    title = args.extract_plain_text().strip()
    if not title:
        await collage.finish("请提供拼图标题")
    if now - last_generated_at.get(session.user.id, 0) < plugin_config.user_cooldown_seconds:
        await collage.finish("操作过于频繁，请稍后再试")
    if scene_generated.get(session.scene.id, 0) >= plugin_config.scene_daily_quota:
        await collage.finish("本群今日拼图次数已用完")
    await collage.finish(title.encode())
