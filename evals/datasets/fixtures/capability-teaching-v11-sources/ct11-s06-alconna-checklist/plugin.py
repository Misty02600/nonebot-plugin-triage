from arclet.alconna import Alconna, Args, Option, Subcommand
from nonebot_plugin_alconna import on_alconna

checklist = on_alconna(
    Alconna(
        "清单",
        Subcommand(
            "新增",
            Args["事项", str],
            Option("-p|--优先级", Args["级别", str]),
        ),
        Subcommand("移除", Args["编号", int]),
    )
)


@checklist.handle()
async def handle_checklist():
    await checklist.finish("清单操作已完成")
