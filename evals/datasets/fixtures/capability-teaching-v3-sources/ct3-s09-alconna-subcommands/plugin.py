from arclet.alconna import Alconna, Args, Option, Subcommand
from nonebot_plugin_alconna import on_alconna

subscription = on_alconna(
    Alconna(
        "订阅",
        Subcommand("添加", Args["主题", str], Option("--quiet|-q")),
        Subcommand("删除", Args["编号", int]),
    )
)


@subscription.handle()
async def handle_subscription():
    await subscription.finish("订阅操作已完成")
