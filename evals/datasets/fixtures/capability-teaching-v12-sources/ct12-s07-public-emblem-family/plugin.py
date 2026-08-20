from nonebot import on_command
from nonebot.adapters import Message
from nonebot.params import CommandArg


async def public_card_allowed() -> bool:
    return True


def create_emblem_handler(material: str):
    async def handle_emblem_card(args: Message = CommandArg()):
        text = args.extract_plain_text().strip()
        if not text:
            return "请提供卡片文字"
        return material.encode() + text.encode()

    return handle_emblem_card


bronze_handler = create_emblem_handler("青铜")
enamel_handler = create_emblem_handler("珐琅")
woodcut_handler = create_emblem_handler("木刻")


bronze = on_command(
    "纹章-青铜-卡",
    permission=public_card_allowed,
    handlers=[bronze_handler],
)
enamel = on_command(
    "纹章-珐琅-卡",
    permission=public_card_allowed,
    handlers=[enamel_handler],
)
woodcut = on_command(
    "纹章-木刻-卡",
    permission=public_card_allowed,
    handlers=[woodcut_handler],
)
