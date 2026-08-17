from nonebot import on_command
from nonebot.rule import to_me

status = on_command("运行状态", rule=to_me())


@status.handle()
async def handle_status():
    await status.finish("Bot 当前运行正常")
