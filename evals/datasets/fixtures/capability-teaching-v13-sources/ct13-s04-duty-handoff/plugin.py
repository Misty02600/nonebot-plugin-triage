from nonebot import on_command
from nonebot.adapters import Message
from nonebot.params import CommandArg


async def duty_operator_only(session) -> bool:
    return session.user.role == "duty_operator"


handoff = on_command("轮值交接", permission=duty_operator_only)


@handoff.handle()
async def handle_handoff(args: Message = CommandArg()):
    shift = args.extract_plain_text().strip()
    if not shift:
        await handoff.finish("请提供班次")
    await handoff.finish(f"已生成 {shift} 交接清单")
