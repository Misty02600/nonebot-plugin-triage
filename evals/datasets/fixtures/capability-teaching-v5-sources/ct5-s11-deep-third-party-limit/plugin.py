from nonebot import on_command

from .throttle import enforce_request_window

report = on_command("生成周报")


async def generate_weekly_report(topic: str) -> str:
    return f"{topic} 周报"


@report.handle()
async def handle_report(user_id: str, topic: str):
    await enforce_request_window(user_id)
    await report.finish(await generate_weekly_report(topic))
