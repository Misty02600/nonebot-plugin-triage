import ast
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

source_path = Path(sys.argv[1])
tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
scheduler_class = next(
    node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Scheduler"
)
function = next(
    node
    for node in scheduler_class.body
    if isinstance(node, ast.AsyncFunctionDef) and node.name == "run_task"
)
future = ast.ImportFrom(
    module="__future__",
    names=[ast.alias(name="annotations")],
    level=0,
)


class Target:
    def __init__(self, name: str, fails: bool) -> None:
        self.name = name
        self.fails = fails

    def __str__(self) -> str:
        return self.name


bad_target = Target("bad-group", True)
good_target = Target("good-group", False)
schedule_rows = [
    SimpleNamespace(saa_target=bad_target),
    SimpleNamespace(saa_target=good_target),
]
send_attempts: list[str] = []
logs: list[str] = []


class Message:
    async def send_to(self, target: Target) -> None:
        send_attempts.append(target.name)
        if target.fails:
            raise RuntimeError("platform rejected target")


class Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None

    async def scalars(self, statement):
        return SimpleNamespace(all=lambda: schedule_rows)


class Query:
    def where(self, expression):
        return self


class Field:
    def __eq__(self, other):
        return True


class Logger:
    @staticmethod
    def info(message: str) -> None:
        logs.append(message)

    @staticmethod
    def exception(message: str) -> None:
        logs.append(message)


async def get_messages_plain_text(**kwargs):
    return ["hello"]


async def get_wordcloud(messages, mask_key):
    return b"image"


namespace = {
    "Optional": object,
    "time": object,
    "get_session": lambda: Session(),
    "select": lambda model: Query(),
    "Schedule": SimpleNamespace(time=Field()),
    "logger": Logger(),
    "get_messages_plain_text": get_messages_plain_text,
    "get_wordcloud": get_wordcloud,
    "get_datetime_now_with_timezone": lambda: datetime(2026, 8, 8, 12, 0),
    "get_mask_key": lambda target: target.name,
    "plugin_config": SimpleNamespace(wordcloud_exclude_user_ids=[]),
    "saa": SimpleNamespace(Image=lambda image: Message(), Text=lambda text: Message()),
}
exec(
    compile(
        ast.fix_missing_locations(ast.Module(body=[future, function], type_ignores=[])),
        str(source_path),
        "exec",
    ),
    namespace,
)

raised = None
try:
    asyncio.run(namespace["run_task"](SimpleNamespace(schedules={})))
except Exception as error:
    raised = {"type": type(error).__name__, "message": str(error)}

print(
    json.dumps(
        {
            "raised": raised,
            "send_attempts": send_attempts,
            "logged_failure": any("bad-group" in message for message in logs),
        }
    )
)
