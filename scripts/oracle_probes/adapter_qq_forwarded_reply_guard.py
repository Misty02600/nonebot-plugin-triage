import asyncio
import json
from types import SimpleNamespace

from nonebot.adapters.qq.bot import _check_reply


class EventStub:
    def __init__(self, first_element) -> None:
        self.msg_elements = [first_element]
        self.reply = None
        self.to_me = False


bot = SimpleNamespace(self_info=SimpleNamespace(username="nbtriage-bot"))


async def run_content_only() -> dict:
    event = EventStub(SimpleNamespace(content="forwarded content"))
    try:
        await _check_reply(bot, event)
    except Exception as error:
        return {
            "raised": type(error).__name__,
            "message": str(error),
            "reply_assigned": event.reply is not None,
        }
    return {"raised": None, "message": None, "reply_assigned": event.reply is not None}


async def run_explicit_reply() -> dict:
    element = SimpleNamespace(
        type="reply",
        author=SimpleNamespace(bot=True, username="nbtriage-bot"),
    )
    event = EventStub(element)
    await _check_reply(bot, event)
    return {"reply_assigned": event.reply is element, "to_me": event.to_me}


async def main() -> None:
    result = {
        "content_only": await run_content_only(),
        "explicit_reply": await run_explicit_reply(),
    }
    print(json.dumps(result))


asyncio.run(main())
