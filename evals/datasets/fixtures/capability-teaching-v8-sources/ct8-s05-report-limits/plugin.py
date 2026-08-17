from nonebot import on_command
from nonebot.adapters import Message
from nonebot.params import CommandArg
from nonebot_plugin_uninfo import Uninfo
from pydantic import BaseModel


class Config(BaseModel):
    user_cooldown_seconds: int = 17
    scene_daily_quota: int = 11


plugin_config = Config()
report = on_command("生成报告")
last_generated_at: dict[str, int] = {}
scene_generated: dict[str, int] = {}


@report.handle()
async def handle_report(session: Uninfo, now: int, args: Message = CommandArg()):
    title = args.extract_plain_text().strip()
    if not title:
        await report.finish("请提供报告主题")
    if now - last_generated_at[session.user.id] < plugin_config.user_cooldown_seconds:
        await report.finish("操作过于频繁，请稍后再试")
    if scene_generated[session.scene.id] >= plugin_config.scene_daily_quota:
        await report.finish("本群今日报告次数已用完")
    await report.finish(title.encode())
