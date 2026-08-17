from nonebot import on_command
from nonebot_plugin_uninfo import OWNER

rename = on_command("修改群名", permission=OWNER())


@rename.handle()
async def handle_rename(name: str):
    await rename.finish(await rename_group(name))
