from nonebot import on_command
from nonebot.adapters import Message
from nonebot.params import CommandArg

from .window import enforce_bookmark_window

bookmark = on_command("同步书签")


@bookmark.handle()
async def handle_bookmark(user_id: str, args: Message = CommandArg()):
    target = args.extract_plain_text().strip()
    if not target:
        await bookmark.finish("请提供同步目标")
    await enforce_bookmark_window(user_id)
    await bookmark.finish(f"已同步 {target}")
