from nonebot import on_command
from nonebot.adapters import Event

OPERATIONS = (
    ("查天气", "weather"),
    ("翻译", "translate"),
    ("踢出", "kick"),
    ("随机语录", "quote"),
)


def create_operation(command: str, operation: str):
    matcher = on_command(command)

    @matcher.handle()
    async def handle_operation(event: Event):
        argument = event.get_plaintext().removeprefix(command).strip()
        if operation == "weather":
            await matcher.finish(await query_weather(argument))
        elif operation == "translate":
            await matcher.finish(await translate_text(argument))
        elif operation == "kick":
            await kick_member(argument)
        else:
            await matcher.finish(await random_quote())

    return matcher


for command, operation in OPERATIONS:
    create_operation(command, operation)
