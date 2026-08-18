from __future__ import annotations

import os
from dataclasses import replace

os.environ["ENVIRONMENT"] = "test"
# 隔离维护者本地 `.env` 中可能残留的已删除 trial 路径；迁移错误由配置单测直接覆盖。
os.environ["NBTRIAGE_TRIAL_LOG_PATH"] = ""
# 普通测试不访问公开知识包 catalog；默认自动更新由专用运行时测试覆盖。
os.environ["NBTRIAGE_KNOWLEDGE_PACK_AUTO_UPDATE"] = "false"
# 测试使用假的合格配置覆盖通用模型入口，但不启动真实 Provider 请求。
# 已删除的 backend 环境变量必须隔离，商城式无模型配置导入由独立用例覆盖。
os.environ.pop("NBTRIAGE_MODEL_BACKEND", None)
os.environ["NBTRIAGE_MODEL_NAME"] = "openai-chat:deepseek-v4-flash"
os.environ["NBTRIAGE_MODEL_BASE_URL"] = "https://opencode.ai/zen/go/v1"
os.environ["NBTRIAGE_MODEL_TIMEOUT_SECONDS"] = "60"
os.environ["NBTRIAGE_MODEL_MAX_OUTPUT_TOKENS"] = "240"
os.environ["NBTRIAGE_AGENT_TRACE_ENABLED"] = "false"
os.environ["OPENAI_API_KEY"] = "test-only-not-a-secret"
# 普通测试使用独立内存数据库；迁移 upgrade/check 由专用子进程用例验证。
os.environ["SQLALCHEMY_DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ["ALEMBIC_STARTUP_CHECK"] = "false"

import nonebot
import pytest
from nonebot.adapters.onebot.v11 import Adapter


def pytest_configure() -> None:
    try:
        nonebot.get_driver()
    except ValueError:
        nonebot.init(
            _env_file=(".nonebot-triage-pytest.env",),
            driver="~none",
            superusers={"200"},
        )
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
    from nonebot_plugin_triage.bug_assessment_runtime import (
        UnavailableBugAssessmentService,
    )
    from nonebot_plugin_triage.public_guidance import PublicGuidanceService
    from nonebot_plugin_triage.semantic_assessment import SemanticAssessmentService

    monkeypatch.setattr(
        handlers,
        "plugin_runtime",
        replace(
            handlers.plugin_runtime,
            semantic_assessment_service=SemanticAssessmentService(None, timeout_seconds=1),
            public_guidance_service=PublicGuidanceService(None, timeout_seconds=1),
            bug_assessment_service=UnavailableBugAssessmentService(),
        ),
    )
