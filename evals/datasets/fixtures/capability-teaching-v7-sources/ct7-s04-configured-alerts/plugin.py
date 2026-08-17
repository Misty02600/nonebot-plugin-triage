from nonebot import on_command
from pydantic import BaseModel


class Config(BaseModel):
    max_alert_items: int = 6


plugin_config = Config()
alerts = on_command("最近告警")


async def list_public_alerts() -> list[str]:
    return [f"告警 {index}" for index in range(1, 20)]


@alerts.handle()
async def handle_alerts():
    items = await list_public_alerts()
    await alerts.finish(items[: plugin_config.max_alert_items])
