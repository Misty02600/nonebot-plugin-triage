from __future__ import annotations

import os

import nonebot
import pytest
from nonebot.adapters.onebot.v11 import Adapter

os.environ["ENVIRONMENT"] = "test"


def pytest_configure() -> None:
    try:
        nonebot.get_driver()
    except ValueError:
        nonebot.init(driver="~none", superusers={"200"})
    nonebot.get_driver().register_adapter(Adapter)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="session", autouse=True)
async def load_nonebot_plugin(after_nonebot_init: None) -> None:
    nonebot.get_driver().register_adapter(Adapter)
    if nonebot.get_plugin_by_module_name("nonebot_plugin_triage") is None:
        nonebot.load_from_toml("pyproject.toml")
