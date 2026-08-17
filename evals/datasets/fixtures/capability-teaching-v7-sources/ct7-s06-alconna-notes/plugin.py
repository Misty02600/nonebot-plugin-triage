from arclet.alconna import Alconna, Args, Option, Subcommand
from nonebot_plugin_alconna import on_alconna

note = on_alconna(
    Alconna(
        "便签",
        Subcommand(
            "新建",
            Args["内容", str],
            Option("-c|--颜色", Args["颜色", str]),
        ),
        Subcommand("完成", Args["编号", int]),
    )
)


@note.handle()
async def handle_note():
    await note.finish("便签操作已完成")
