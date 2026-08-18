from nonebot import on_command
from nonebot.adapters import Message
from nonebot.params import CommandArg

from .throttle import enforce_share_window

share = on_command("分享摘要")


@share.handle()
async def handle_share(user_id: str, args: Message = CommandArg()):
    target = args.extract_plain_text().strip()
    if not target:
        await share.finish("请提供分享目标")
    await enforce_share_window(user_id)
    await share.finish(f"已分享 {target}")
