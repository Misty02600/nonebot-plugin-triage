import asyncio
import json
from contextlib import suppress

from nonebot.adapters.qq.bot import Bot
from nonebot.adapters.qq.models import MessageReference
from yarl import URL


class RequestCaptured(Exception):
    pass


class AdapterStub:
    @staticmethod
    def get_api_base() -> URL:
        return URL("https://example.invalid/api/")


class BotStub:
    def __init__(self) -> None:
        self.adapter = AdapterStub()
        self.request = None

    async def _request(self, request):
        self.request = request
        raise RequestCaptured


async def capture(method_name: str, target_name: str) -> dict:
    bot = BotStub()
    method = getattr(Bot, method_name).func
    kwargs = {
        target_name: "target-id",
        "msg_type": 0,
        "content": "hello",
        "message_reference": MessageReference(message_id="quoted-id"),
        "timestamp": 1_700_000_000,
    }
    with suppress(RequestCaptured):
        await method(bot, **kwargs)

    body = bot.request.json
    reference = body["message_reference"]
    try:
        json.dumps(body)
    except TypeError as error:
        serializable = False
        serialization_error = str(error)
    else:
        serializable = True
        serialization_error = None
    return {
        "reference_type": type(reference).__name__,
        "reference_value": reference if isinstance(reference, dict) else repr(reference),
        "serializable": serializable,
        "serialization_error": serialization_error,
    }


async def main() -> None:
    result = {
        "c2c": await capture("post_c2c_messages", "openid"),
        "group": await capture("post_group_messages", "group_openid"),
    }
    print(json.dumps(result))


asyncio.run(main())
