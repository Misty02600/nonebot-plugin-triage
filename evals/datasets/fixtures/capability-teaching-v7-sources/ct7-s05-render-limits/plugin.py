from nonebot import on_command
from nonebot.adapters import Message
from nonebot.params import CommandArg
from nonebot_plugin_uninfo import Uninfo
from pydantic import BaseModel


class Config(BaseModel):
    user_cooldown_seconds: int = 22
    scene_hourly_quota: int = 8


plugin_config = Config()
render = on_command("渲染海报")
last_rendered_at: dict[str, int] = {}
scene_rendered: dict[str, int] = {}


@render.handle()
async def handle_render(
    session: Uninfo,
    now: int,
    args: Message = CommandArg(),
):
    user_id = session.user.id
    scene_id = session.scene.id
    title = args.extract_plain_text().strip()
    if not title:
        await render.finish("请提供海报标题")
    if now - last_rendered_at[user_id] < plugin_config.user_cooldown_seconds:
        await render.finish("操作过于频繁，请稍后再试")
    if scene_rendered[scene_id] >= plugin_config.scene_hourly_quota:
        await render.finish("本群本小时生成次数已用完")
    await render.finish(title.encode())
