from nonebot import on_command

avatar = on_command("头像", aliases={"头像查看"})


@avatar.handle()
async def handle_avatar(user: str | None):
    if user is None:
        user = current_user()
    await avatar.finish(await render_avatar(user))
