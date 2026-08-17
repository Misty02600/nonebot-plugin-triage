from arclet.alconna import Alconna, Args, Option, Subcommand
from nonebot_plugin_alconna import on_alconna

bookmark = on_alconna(
    Alconna(
        "收藏",
        Subcommand(
            "保存",
            Args["链接", str],
            Option("-t|--标签", Args["标签", str]),
        ),
        Subcommand("删除", Args["编号", int]),
    )
)


@bookmark.handle()
async def handle_bookmark():
    await bookmark.finish("收藏操作已完成")
