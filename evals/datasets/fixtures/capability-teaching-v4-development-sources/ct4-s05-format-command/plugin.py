from nonebot import on_command

formatter = on_command("排版")


@formatter.handle()
async def handle_format(text: str, width: int | None = None):
    await formatter.finish(await format_text(text, width=width))
