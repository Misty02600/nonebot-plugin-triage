from nonebot import on_command
from nonebot.adapters import Message
from nonebot.params import CommandArg

summary = on_command("网页摘要")


@summary.handle()
async def handle_summary(args: Message = CommandArg()):
    parts = args.extract_plain_text().split(maxsplit=1)
    if not parts:
        await summary.finish("请提供网页链接")
    language = parts[1] if len(parts) > 1 else "中文"
    await summary.finish(f"已使用{language}生成摘要：{parts[0]}")
