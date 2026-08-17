from nonebot import on_command
from nonebot_plugin_uninfo import MEMBER

checkin = on_command("成员签到", permission=MEMBER())


@checkin.handle()
async def handle_checkin():
    await checkin.finish(await check_in_member())
