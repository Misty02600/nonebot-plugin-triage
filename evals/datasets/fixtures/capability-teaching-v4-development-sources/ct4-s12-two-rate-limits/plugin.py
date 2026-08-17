from pydantic import BaseModel


class Config(BaseModel):
    user_cooldown: int = 30
    scene_quota: int = 100


plugin_config = Config()
draw = on_command("绘图")


@draw.handle()
async def handle_draw(prompt: str):
    await enforce_user_cooldown(plugin_config.user_cooldown)
    await enforce_scene_quota(plugin_config.scene_quota)
    await draw.finish(await render_image(prompt))
