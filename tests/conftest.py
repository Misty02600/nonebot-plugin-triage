from __future__ import annotations

import os
from dataclasses import replace

import nonebot
import pytest
from nonebot.adapters.onebot.v11 import Adapter

os.environ["ENVIRONMENT"] = "test"
# 隔离维护者本地 `.env` 中可能残留的已删除 trial 路径；迁移错误由配置单测直接覆盖。
os.environ["NBTRIAGE_TRIAL_LOG_PATH"] = ""


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


@pytest.fixture(autouse=True)
def isolate_live_semantic_transport(
    monkeypatch: pytest.MonkeyPatch,
    load_nonebot_plugin: None,
) -> None:
    """普通 pytest 不得因维护者本机配置而调用真实语义 Provider。"""
    from nonebot_plugin_triage import handlers
    from nonebot_plugin_triage.semantic_assessment import SemanticAssessmentService

    monkeypatch.setattr(
        handlers,
        "plugin_runtime",
        replace(
            handlers.plugin_runtime,
            semantic_assessment_service=SemanticAssessmentService(None, timeout_seconds=1),
        ),
    )
