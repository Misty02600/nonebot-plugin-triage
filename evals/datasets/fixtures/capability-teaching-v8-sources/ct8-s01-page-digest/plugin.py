from nonebot import on_command
from nonebot.adapters import Message
from nonebot.params import CommandArg

digest = on_command("页面速览")


@digest.handle()
async def handle_digest(args: Message = CommandArg()):
    parts = args.extract_plain_text().split(maxsplit=1)
    if not parts:
        await digest.finish("请提供网页链接")
    style = parts[1] if len(parts) > 1 else "简洁"
    await digest.finish(f"已生成{style}速览：{parts[0]}")
