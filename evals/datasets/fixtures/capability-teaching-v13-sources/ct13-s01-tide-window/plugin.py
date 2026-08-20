from nonebot import on_command
from nonebot.adapters import Message
from nonebot.params import CommandArg

from .service import build_tide_window

tide_window = on_command("潮汐窗口")


@tide_window.handle()
async def handle_tide_window(args: Message = CommandArg()):
    parts = args.extract_plain_text().split(maxsplit=1)
    if not parts:
        await tide_window.finish("请提供港口")
    date = parts[1] if len(parts) > 1 else "今天"
    await tide_window.finish(build_tide_window(parts[0], date))
