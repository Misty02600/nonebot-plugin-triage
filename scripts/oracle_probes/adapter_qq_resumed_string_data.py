import json

from nonebot.adapters.qq.adapter import Adapter
from nonebot.adapters.qq.models import Dispatch, Opcode

payload = Dispatch(
    opcode=Opcode.DISPATCH,
    data="",
    sequence=42,
    type="RESUMED",
    id="resume-event",
)

try:
    event = Adapter.payload_to_event(payload)
except Exception as error:
    result = {
        "raised": type(error).__name__,
        "message": str(error),
        "event_type": None,
    }
else:
    result = {
        "raised": None,
        "message": None,
        "event_type": type(event).__name__,
    }

print(json.dumps(result))
