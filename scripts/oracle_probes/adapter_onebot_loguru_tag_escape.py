import json

from nonebot.adapters.onebot.v11 import MessageSegment, PrivateMessageEvent
from nonebot.adapters.onebot.v11.event import Sender
from nonebot.log import logger

message = MessageSegment.text("[text]") + MessageSegment.at(123) + MessageSegment.text("<t\nag>")
event = PrivateMessageEvent(
    time=0,
    self_id=0,
    post_type="message",
    sub_type="friend",
    user_id=1,
    message_type="private",
    message_id=1,
    message=message,
    original_message=message,
    raw_message=str(message),
    font=0,
    sender=Sender(),
    to_me=True,
)

try:
    logger.opt(colors=True).success(f"{event.get_event_name()}: {event.get_event_description()}")
except Exception as error:
    print(json.dumps({"raised": type(error).__name__, "message": str(error)}))
else:
    print(json.dumps({"raised": None}))
