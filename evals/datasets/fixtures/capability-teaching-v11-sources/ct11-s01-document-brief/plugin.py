from nonebot import on_command
from nonebot.adapters import Message
from nonebot.params import CommandArg

brief = on_command("文档提要")


@brief.handle()
async def handle_brief(args: Message = CommandArg()):
    parts = args.extract_plain_text().split(maxsplit=1)
    if not parts:
        await brief.finish("请提供文档链接")
    length = parts[1] if len(parts) > 1 else "短"
    await brief.finish(f"已生成{length}提要：{parts[0]}")
