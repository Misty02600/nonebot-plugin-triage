from nonebot import on_command
from nonebot.adapters import Message
from nonebot.params import CommandArg

from .throttle import enforce_export_window

export = on_command("导出清单")


@export.handle()
async def handle_export(user_id: str, args: Message = CommandArg()):
    target = args.extract_plain_text().strip()
    if not target:
        await export.finish("请提供导出目标")
    await enforce_export_window(user_id)
    await export.finish(f"已导出 {target}")
