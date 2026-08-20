from nonebot import on_command
from nonebot.adapters import Message
from nonebot.params import CommandArg

from .validators import validate_ticket

ticket = on_command("校验票据")


@ticket.handle()
async def handle_ticket(args: Message = CommandArg()):
    ticket_id = args.extract_plain_text().strip()
    if not ticket_id:
        await ticket.finish("请提供票据编号")
    await ticket.finish("票据有效" if validate_ticket(ticket_id) else "票据无效")
