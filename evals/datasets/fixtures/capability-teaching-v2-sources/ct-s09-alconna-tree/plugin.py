from arclet.alconna import Alconna, Args, Option, Subcommand
from nonebot_plugin_alconna import on_alconna

repository = on_alconna(
    Alconna(
        "仓库",
        Subcommand(
            "搜索",
            Args["关键词", str],
            Option("--limit|-n", Args["数量", int, 5]),
        ),
        Subcommand("详情", Args["编号", int]),
    )
)


@repository.handle()
async def handle_repository():
    await repository.finish("已返回仓库信息")
