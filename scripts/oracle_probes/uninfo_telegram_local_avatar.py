import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import nonebot

nonebot.init(driver="~none")

from nonebot_plugin_uninfo.adapters.telegram.main import _supply_userdata  # noqa: E402


class FakeBot:
    self_id = "bot"
    bot_config = SimpleNamespace(token="redacted-token")

    def __init__(self) -> None:
        self.file_path = "remote/avatar.jpg"

    async def get_chat(self, *, chat_id: int) -> SimpleNamespace:
        return SimpleNamespace(
            id=chat_id,
            username="alice",
            first_name="Alice",
            last_name=None,
        )

    async def get_user_profile_photos(self, *, user_id: int, limit: int) -> SimpleNamespace:
        return SimpleNamespace(
            total_count=1,
            photos=[[SimpleNamespace(file_id="avatar-file")]],
        )

    async def get_file(self, *, file_id: str) -> SimpleNamespace:
        return SimpleNamespace(file_path=self.file_path)


async def main() -> None:
    bot = FakeBot()
    with TemporaryDirectory() as directory:
        local_path = Path(directory, "avatar.jpg")
        local_path.write_bytes(b"avatar")

        bot.file_path = str(local_path.resolve())
        local = await _supply_userdata(bot, "123")  # type: ignore[arg-type]

        bot.file_path = "remote/avatar.jpg"
        remote = await _supply_userdata(bot, "123")  # type: ignore[arg-type]

        print(
            json.dumps(
                {
                    "local_avatar": local["avatar"],
                    "local_is_file_uri": str(local["avatar"]).startswith("file:"),
                    "remote_avatar": remote["avatar"],
                    "remote_is_cloud_url": remote["avatar"] == "https://api.telegram.org/file/"
                    "botredacted-token/remote/avatar.jpg",
                },
                sort_keys=True,
            )
        )


asyncio.run(main())
