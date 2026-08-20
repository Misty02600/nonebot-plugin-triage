from nonebot import on_command
from nonebot.rule import to_me
from nonebot_plugin_uninfo import GROUP

rollcall = on_command("发起点名", permission=GROUP(), rule=to_me())


@rollcall.handle()
async def handle_rollcall():
    await rollcall.finish("点名已开始")
