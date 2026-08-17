from nonebot import on_command

avatar = on_command("群头像", aliases={"本群头像"})


@avatar.handle()
async def handle_group_avatar():
    await avatar.finish(await render_group_avatar())
