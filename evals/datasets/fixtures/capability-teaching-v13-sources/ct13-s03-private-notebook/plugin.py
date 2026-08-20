from nonebot import on_command
from nonebot_plugin_uninfo import PRIVATE

private_notebook = on_command("私密便笺", permission=PRIVATE())


@private_notebook.handle()
async def handle_private_notebook():
    await private_notebook.finish("这是你的私密便笺")
