from nonebot import on_command
from nonebot.adapters import Message
from nonebot.params import CommandArg

from .validators import validate_flight_number

flight = on_command("校验航班")


@flight.handle()
async def handle_flight(args: Message = CommandArg()):
    flight_number = args.extract_plain_text().strip()
    if not flight_number:
        await flight.finish("请提供航班号")
    result = "格式有效" if validate_flight_number(flight_number) else "格式无效"
    await flight.finish(result)
