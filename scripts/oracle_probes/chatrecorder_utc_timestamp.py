import ast
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

source_path = Path(sys.argv[1])
tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
function = next(
    node
    for node in tree.body
    if isinstance(node, ast.AsyncFunctionDef) and node.name == "record_recv_msg"
)
function.decorator_list = []


class DateTimeProbe:
    calls: ClassVar[list[tuple[str, int]]] = []

    @classmethod
    def fromtimestamp(cls, value: int) -> str:
        cls.calls.append(("fromtimestamp", value))
        return "host-local-time"

    @classmethod
    def utcfromtimestamp(cls, value: int) -> str:
        cls.calls.append(("utcfromtimestamp", value))
        return "utc-time"


class GroupMessageEvent:
    pass


class Message:
    @staticmethod
    def extract_plain_text() -> str:
        return "hello"


class Session:
    def __init__(self) -> None:
        self.added = None
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None

    def add(self, record) -> None:
        self.added = record

    async def commit(self) -> None:
        self.committed = True


session = Session()
namespace = {
    "datetime": DateTimeProbe,
    "MessageEvent": object,
    "GroupMessageEvent": GroupMessageEvent,
    "MessageRecord": lambda **kwargs: SimpleNamespace(**kwargs),
    "serialize_message": lambda message: [{"type": "text", "data": "hello"}],
    "create_session": lambda: session,
}
exec(
    compile(ast.Module(body=[function], type_ignores=[]), str(source_path), "exec"),
    namespace,
)

event = SimpleNamespace(
    time=1_000_000,
    message_id=1,
    message=Message(),
    user_id=2,
)
asyncio.run(namespace["record_recv_msg"](event))

print(
    json.dumps(
        {
            "datetime_calls": DateTimeProbe.calls,
            "stored_time": session.added.time,
            "committed": session.committed,
        }
    )
)
