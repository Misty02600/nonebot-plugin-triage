from nonebot import on_command
from nonebot.adapters import Event

ACTIONS = (
    ("查航班", "flight"),
    ("压缩图片", "compress"),
    ("设置头衔", "title"),
)


async def query_flight(value: str) -> str:
    return f"航班：{value}"


async def compress_image(value: str) -> str:
    return f"已压缩：{value}"


async def set_special_title(value: str) -> str:
    return f"已设置头衔：{value}"


def register_action(command: str, action: str):
    matcher = on_command(command)

    @matcher.handle()
    async def handle_action(event: Event):
        argument = event.get_plaintext().removeprefix(command).strip()
        if action == "flight":
            await matcher.finish(await query_flight(argument))
        elif action == "compress":
            await matcher.finish(await compress_image(argument))
        else:
            await matcher.finish(await set_special_title(argument))


for command, action in ACTIONS:
    register_action(command, action)
