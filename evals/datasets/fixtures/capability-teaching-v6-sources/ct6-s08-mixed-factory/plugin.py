from nonebot import on_command
from nonebot.adapters import Message
from nonebot.params import CommandArg

ACTIONS = (
    ("查询列车", "train"),
    ("裁剪图片", "crop"),
    ("修改昵称", "nickname"),
)


def register_action(command: str, action: str):
    matcher = on_command(command)

    @matcher.handle()
    async def handle_action(args: Message = CommandArg()):
        value = args.extract_plain_text().strip()
        if action == "train":
            await matcher.finish(f"列车信息：{value}")
        if action == "crop":
            await matcher.finish(f"已裁剪：{value}")
        await matcher.finish(f"已修改昵称：{value}")


for command, action in ACTIONS:
    register_action(command, action)
