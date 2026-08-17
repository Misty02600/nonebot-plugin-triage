from nonebot import on_command

register = on_command("登记资料")


@register.got("name", prompt="请发送姓名")
@register.got("city", prompt="请发送所在城市")
async def handle_register(name: str, city: str):
    await register.finish(f"已登记：{name}，{city}")
