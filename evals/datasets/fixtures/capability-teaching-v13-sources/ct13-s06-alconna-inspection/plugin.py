from arclet.alconna import Alconna, Args, Option, Subcommand
from nonebot_plugin_alconna import on_alconna

inspection = on_alconna(
    Alconna(
        "巡检",
        Subcommand(
            "启动",
            Args["区域", str],
            Option("-q|--安静"),
        ),
        Subcommand(
            "状态",
            Args["编号", int],
        ),
    )
)


@inspection.handle()
async def handle_inspection():
    await inspection.finish("巡检操作已受理")
