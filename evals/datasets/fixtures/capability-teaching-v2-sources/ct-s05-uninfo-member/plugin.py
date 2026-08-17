from nonebot import on_command
from nonebot_plugin_uninfo import MEMBER

member_card = on_command("成员卡片", permission=MEMBER())


@member_card.handle()
async def handle_member_card():
    await member_card.finish("已生成普通成员卡片")
