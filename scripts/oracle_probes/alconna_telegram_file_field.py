import json

from nonebot.adapters.telegram.message import File
from nonebot_plugin_alconna.uniseg.adapters.telegram.builder import (
    TelegramMessageBuilder,
)

segment = File("video", {"file": "telegram-file-id"})

try:
    converted = TelegramMessageBuilder().convert(segment)
except Exception as error:
    result = {
        "status": "error",
        "error_type": type(error).__name__,
        "error_message": str(error),
    }
else:
    result = {
        "status": "converted",
        "type": type(converted).__name__,
        "id": getattr(converted, "id", None),
    }

print(json.dumps(result, sort_keys=True))
