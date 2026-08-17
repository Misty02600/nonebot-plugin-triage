from nonebot import on_command
from nonebot.adapters import Event

TOOLS = (
    ("查汇率", "exchange"),
    ("缩短链接", "shorten"),
    ("移出成员", "remove"),
    ("随机诗句", "poem"),
)


def create_tool(command: str, operation: str):
    matcher = on_command(command)

    @matcher.handle()
    async def handle_tool(event: Event):
        argument = event.get_plaintext().removeprefix(command).strip()
        if operation == "exchange":
            await matcher.finish(await query_exchange_rate(argument))
        elif operation == "shorten":
            await matcher.finish(await shorten_url(argument))
        elif operation == "remove":
            await remove_member(argument)
        else:
            await matcher.finish(await random_poem())

    return matcher


for command, operation in TOOLS:
    create_tool(command, operation)
