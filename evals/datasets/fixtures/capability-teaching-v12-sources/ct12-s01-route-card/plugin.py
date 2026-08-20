from nonebot import on_command
from nonebot.adapters import Message
from nonebot.params import CommandArg

from .service import build_route

route_card = on_command("路线卡")


@route_card.handle()
async def handle_route_card(args: Message = CommandArg()):
    parts = args.extract_plain_text().split(maxsplit=1)
    if not parts:
        await route_card.finish("请提供目的地")
    days = parts[1] if len(parts) > 1 else "1"
    await route_card.finish(build_route(parts[0], days))
