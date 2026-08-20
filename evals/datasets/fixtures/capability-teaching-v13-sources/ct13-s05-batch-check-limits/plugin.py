from nonebot import on_command
from nonebot.adapters import Message
from nonebot.params import CommandArg
from nonebot_plugin_uninfo import Uninfo
from pydantic import BaseModel


class Config(BaseModel):
    user_cooldown_seconds: int = 23
    bot_parallel_jobs: int = 4


plugin_config = Config()
batch_check = on_command("批量校验")
last_checked_at: dict[str, int] = {}
active_jobs = 0


@batch_check.handle()
async def handle_batch_check(session: Uninfo, now: int, args: Message = CommandArg()):
    filename = args.extract_plain_text().strip()
    if not filename:
        await batch_check.finish("请提供文件")
    if now - last_checked_at.get(session.user.id, 0) < plugin_config.user_cooldown_seconds:
        await batch_check.finish("操作过于频繁，请稍后再试")
    if active_jobs >= plugin_config.bot_parallel_jobs:
        await batch_check.finish("当前校验任务已满")
    await batch_check.finish(f"已提交 {filename}")
