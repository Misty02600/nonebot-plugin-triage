from nonebot import on_command
from nonebot.adapters import Message
from nonebot.params import CommandArg

OPERATIONS = (("查地铁", "metro"), ("裁剪图片", "crop"), ("清除签名", "signature"))


def register_operation(command: str, operation: str):
    matcher = on_command(command)

    @matcher.handle()
    async def handle_operation(args: Message = CommandArg()):
        value = args.extract_plain_text().strip()
        await matcher.finish(f"{operation}:{value}")

    return matcher


for command, operation in OPERATIONS:
    register_operation(command, operation)
