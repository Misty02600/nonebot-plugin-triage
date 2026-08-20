from nonebot import on_command


async def private_draft_only(event) -> bool:
    return event.message_type == "private"


draft = on_command("草稿预览", rule=private_draft_only)


@draft.handle()
async def handle_draft():
    await draft.finish("当前草稿预览")
