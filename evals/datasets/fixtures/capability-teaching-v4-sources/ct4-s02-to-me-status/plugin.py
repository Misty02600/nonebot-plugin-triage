from nonebot import on_command
from nonebot.rule import to_me

status = on_command("机器人状态", rule=to_me())


@status.handle()
async def handle_status():
    await status.finish(await render_status())
