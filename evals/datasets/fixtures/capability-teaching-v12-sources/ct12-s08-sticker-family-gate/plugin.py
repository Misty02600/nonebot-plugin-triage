from nonebot import on_command
from nonebot.adapters import Message
from nonebot.params import CommandArg

last_used_at: dict[str, int] = {}


async def sticker_window(user_id: str, now: int) -> bool:
    return now - last_used_at.get(user_id, 0) >= 37


def create_sticker_handler(style: str):
    async def handle_sticker(args: Message = CommandArg()):
        material = args.extract_plain_text().strip()
        if not material:
            return "请提供素材"
        return style.encode() + material.encode()

    return handle_sticker


mist_handler = create_sticker_handler("晨雾")
sand_handler = create_sticker_handler("星砂")
night_handler = create_sticker_handler("夜航")


mist = on_command(
    "贴纸::晨雾",
    permission=sticker_window,
    handlers=[mist_handler],
)
sand = on_command(
    "贴纸::星砂",
    permission=sticker_window,
    handlers=[sand_handler],
)
night = on_command(
    "贴纸::夜航",
    permission=sticker_window,
    handlers=[night_handler],
)
