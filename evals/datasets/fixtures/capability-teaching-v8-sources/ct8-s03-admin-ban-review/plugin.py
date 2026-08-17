from nonebot import on_command
from nonebot.adapters import Message
from nonebot.params import CommandArg
from nonebot_plugin_uninfo import ADMIN

review = on_command("审查封禁", permission=ADMIN())


@review.handle()
async def handle_review(args: Message = CommandArg()):
    target = args.extract_plain_text().strip()
    if not target:
        await review.finish("请提供用户")
    await review.finish(f"已生成 {target} 的公开封禁摘要")
