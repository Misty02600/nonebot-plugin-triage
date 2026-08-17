from nonebot import on_command
from nonebot.adapters import Message
from nonebot.params import CommandArg

OPERATIONS = (
    ("查航班", "flight"),
    ("压缩图片", "compress"),
    ("清空昵称", "nickname"),
)


def register_operation(command: str, operation: str):
    matcher = on_command(command)

    @matcher.handle()
    async def handle_operation(args: Message = CommandArg()):
        value = args.extract_plain_text().strip()
        if operation == "flight":
            await matcher.finish(f"航班信息：{value}")
        if operation == "compress":
            await matcher.finish(f"已压缩：{value}")
        await matcher.finish("已清空昵称")


for command, operation in OPERATIONS:
    register_operation(command, operation)
