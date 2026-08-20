from arclet.alconna import Alconna, Args, Option, Subcommand
from nonebot_plugin_alconna import on_alconna

archive = on_alconna(
    Alconna(
        "档案",
        Subcommand(
            "导出",
            Args["格式", str],
            Option("-z|--压缩"),
        ),
        Subcommand(
            "删除",
            Args["编号", int],
            Option("--确认"),
        ),
    )
)


@archive.handle()
async def handle_archive():
    await archive.finish("档案操作已完成")
