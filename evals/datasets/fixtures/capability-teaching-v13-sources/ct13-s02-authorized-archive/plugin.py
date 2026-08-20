from nonebot import on_command
from nonebot.adapters import Message
from nonebot.params import CommandArg

AUTHORIZED_USERS = {"u-17", "u-42"}


async def archive_access(session) -> bool:
    return session.user.id in AUTHORIZED_USERS


archive_search = on_command("资料室检索", permission=archive_access)


@archive_search.handle()
async def handle_archive_search(args: Message = CommandArg()):
    keyword = args.extract_plain_text().strip()
    if not keyword:
        await archive_search.finish("请提供关键词")
    await archive_search.finish(f"已检索：{keyword}")
