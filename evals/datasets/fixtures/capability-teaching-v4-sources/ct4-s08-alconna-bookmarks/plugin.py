from arclet.alconna import Alconna, Args, Option, Subcommand
from nonebot_plugin_alconna import on_alconna

bookmark = on_alconna(
    Alconna(
        "收藏",
        Subcommand("添加", Args["链接", str], Option("--tag|-t", Args["标签", str])),
        Subcommand("移除", Args["编号", int]),
    )
)


@bookmark.handle()
async def handle_bookmark():
    await bookmark.finish("收藏操作已完成")
