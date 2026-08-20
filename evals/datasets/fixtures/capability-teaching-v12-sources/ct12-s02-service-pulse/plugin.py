from nonebot import on_command
from nonebot.rule import to_me

pulse = on_command("服务脉搏", rule=to_me())


@pulse.handle()
async def handle_pulse():
    await pulse.finish("当前公开服务状态正常")
