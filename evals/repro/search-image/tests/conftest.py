import os
from collections.abc import Iterator

import nonebot
import pytest
from nonebot.adapters.onebot.v11 import Adapter
from nonebug import NONEBOT_INIT_KWARGS, NONEBOT_START_LIFESPAN


def pytest_configure(config: pytest.Config) -> None:
    config.stash[NONEBOT_INIT_KWARGS] = {
        "_env_file": (".nonebug-unused-env",),
        "driver": "~none",
        "nickname": {"搜图测试Bot"},
        "command_start": {"/"},
        "saucenao_api_key": "nonebug-only-not-a-real-key",
    }
    config.stash[NONEBOT_START_LIFESPAN] = False


@pytest.fixture(scope="session")
def after_nonebot_init(
    _nonebot_init: None, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[None]:
    original_cwd = os.getcwd()
    os.chdir(tmp_path_factory.mktemp("search-image-runtime"))
    try:
        nonebot.get_driver().register_adapter(Adapter)
        assert nonebot.load_plugin("YetAnotherPicSearch") is not None
        yield
    finally:
        os.chdir(original_cwd)
