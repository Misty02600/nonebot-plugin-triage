from nonebot import on_command
from nonebot.adapters import Message
from nonebot.params import CommandArg

exchange = on_command("汇率换算", aliases={"换汇"})


async def query_rate(currency_pair: str) -> str:
    return f"{currency_pair} 的当前汇率为 1.23"


@exchange.handle()
async def handle_exchange(args: Message = CommandArg()):
    currency_pair = args.extract_plain_text().strip()
    if not currency_pair:
        await exchange.finish("请提供货币对")
    await exchange.finish(await query_rate(currency_pair))
