from nonebot import on_command
from nonebot.adapters import Message
from nonebot.params import CommandArg
from nonebot_plugin_uninfo import OWNER

transfer = on_command("移交群配置", permission=OWNER())


@transfer.handle()
async def handle_transfer(args: Message = CommandArg()):
    target = args.extract_plain_text().strip()
    if not target:
        await transfer.finish("请提供接收用户")
    await transfer.finish(f"已将群配置移交给 {target}")
