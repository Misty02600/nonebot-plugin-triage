from nonebot import on_command
from nonebot.adapters import Message
from nonebot.params import CommandArg
from nonebot_plugin_uninfo import ADMIN

audit = on_command("审计成员", permission=ADMIN())


@audit.handle()
async def handle_audit(args: Message = CommandArg()):
    target = args.extract_plain_text().strip()
    if not target:
        await audit.finish("请提供用户")
    await audit.finish(f"已生成 {target} 的公开审计摘要")
