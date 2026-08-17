from arclet.alconna import Alconna, Args, Option, Subcommand
from nonebot_plugin_alconna import on_alconna

task = on_alconna(
    Alconna(
        "任务",
        Subcommand("添加", Args["内容", str], Option("--urgent|-u")),
        Subcommand("完成", Args["编号", int]),
    )
)


@task.handle()
async def handle_task():
    await task.finish("任务操作已完成")
