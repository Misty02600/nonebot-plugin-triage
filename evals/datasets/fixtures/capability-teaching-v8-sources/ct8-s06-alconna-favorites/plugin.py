from arclet.alconna import Alconna, Args, Option, Subcommand
from nonebot_plugin_alconna import on_alconna

favorite = on_alconna(
    Alconna(
        "收藏",
        Subcommand(
            "添加",
            Args["链接", str],
            Option("-t|--标签", Args["标签", str]),
        ),
        Subcommand("删除", Args["编号", int]),
    )
)


@favorite.handle()
async def handle_favorite():
    await favorite.finish("收藏操作已完成")
