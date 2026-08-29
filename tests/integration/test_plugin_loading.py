from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


def _clean_subprocess_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["NBTRIAGE_TRIAL_LOG_PATH"] = ""
    return environment


def _marketplace_subprocess_environment(*, configured_model: bool) -> dict[str, str]:
    environment = _clean_subprocess_environment()
    for key in (
        "NBTRIAGE_MODEL_BACKEND",
        "NBTRIAGE_MODEL_NAME",
        "NBTRIAGE_MODEL_BASE_URL",
        "NBTRIAGE_MODEL_TIMEOUT_SECONDS",
        "NBTRIAGE_MODEL_MAX_OUTPUT_TOKENS",
        "OPENAI_API_KEY",
    ):
        environment.pop(key, None)
    if configured_model:
        environment.update(
            {
                "NBTRIAGE_MODEL_NAME": "openai-chat:deepseek-v4-flash",
                "NBTRIAGE_MODEL_BASE_URL": "https://opencode.ai/zen/go/v1",
                "NBTRIAGE_MODEL_TIMEOUT_SECONDS": "60",
                "NBTRIAGE_MODEL_MAX_OUTPUT_TOKENS": "240",
            }
        )
    return environment


@pytest.mark.parametrize("configured_model", [False, True])
def test_nonebot_plugin_loads_without_private_model_configuration(
    tmp_path: Path,
    configured_model: bool,
) -> None:
    script = """
import nonebot

nonebot.init(driver="~none")
plugin = nonebot.load_plugin("nonebot_plugin_triage")
assert plugin is not None

from nonebot_plugin_triage import handlers

assert handlers.plugin_runtime.capability_shadow is not None
assert not hasattr(handlers.plugin_runtime, "model_service")
"""

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=_marketplace_subprocess_environment(configured_model=configured_model),
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr


def test_nonebot_plugin_rejects_removed_model_backend_environment(tmp_path: Path) -> None:
    environment = _marketplace_subprocess_environment(configured_model=False)
    environment["NBTRIAGE_MODEL_BACKEND"] = "pydantic-ai"
    script = """
import nonebot

nonebot.init(driver="~none")
plugin = nonebot.load_plugin("nonebot_plugin_triage")
assert plugin is not None
"""

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "nbtriage_model_backend" in output.casefold()
    assert "provider:model" in output


def test_nonebot_plugin_loads_with_alconna_cross_platform_metadata() -> None:
    project_root = Path(__file__).parents[2]
    script = """
import nonebot
nonebot.init(
    _env_file=(".nonebot-triage-pytest.env",),
    driver="~none",
    superusers={"200"},
)
plugin = nonebot.load_plugin("nonebot_plugin_triage")
assert plugin is not None

import asyncio
from types import SimpleNamespace

from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message
from nonebot.adapters.onebot.v11.event import Sender
from nonebot_plugin_alconna import AlconnaMatcher
import nonebot_plugin_triage as module
from nonebot_plugin_triage import handlers, runtime

assert issubclass(handlers.support_matcher, AlconnaMatcher)
assert handlers.support_matcher.priority == 10
assert handlers.support_matcher.block
assert not hasattr(handlers, "continuation_matcher")
command = handlers.support_matcher._rule.command()
assert command.parse("triage").matched
assert command.parse("triage 某个功能怎么使用").matched
assert command.parse("triage hello world").query("request_text") == ("hello", "world")
assert command.parse("triage 为什么 --foo 不工作").matched
assert command.parse("triage 为什么 --help 不工作").query("request_text") == (
    "为什么",
    "--help",
    "不工作",
)
assert command.parse("triage 为什么 -h 不工作").matched
assert command.parse("triage 为什么 --comp 不工作").matched
assert command.parse("triage ?").query("request_text") == ("?",)
assert not command.parse("报错").matched
assert not command.parse("triage-other hello").matched
query_command = handlers.query_matcher._rule.command()
assert query_command.parse("triage 报错查询").matched
assert query_command.parse("triage 报错查询 P-23456789").matched
assert query_command.parse("triage 报错查询 P-23456789 确认Bug").matched
assert not query_command.parse("报错查询 P-23456789").matched
refresh_help_command = handlers.refresh_help_matcher._rule.command()
assert handlers.refresh_help_matcher.priority == 9
assert handlers.refresh_help_matcher.block
assert refresh_help_command.parse("triage 刷新帮助").matched
assert refresh_help_command.parse("triage 刷新帮助 nonebot_plugin_memes").matched
assert not refresh_help_command.parse("triage 刷新帮助 plugin extra").matched
assert not hasattr(handlers, "feedback_matcher")
assert not hasattr(handlers, "trial_stats_matcher")
assert handlers.plugin_runtime.observer.registered
assert handlers.plugin_runtime.reference_bridge.registered
assert handlers.plugin_runtime.trials.mode.value == "off"
assert "nonebot.adapters.onebot.v11" in module.__plugin_meta__.supported_adapters
assert "nonebot.adapters.qq" in module.__plugin_meta__.supported_adapters
assert module.__plugin_meta__.config is module.NBTriageConfig
assert module.__plugin_meta__.name == "NoneBot Triage Agent"
assert module.__plugin_meta__.homepage.endswith("/nonebot-plugin-triage")
assert "triage <求助内容>" in module.__plugin_meta__.usage
assert "triage 报错查询" in module.__plugin_meta__.usage
assert "报错反馈" not in module.__plugin_meta__.usage
assert "报错统计" not in module.__plugin_meta__.usage
assert handlers._empty_support_prompt() == "请在 triage 后描述想了解的功能或遇到的问题。"
assert len(handlers.plugin_runtime.outgoing_reference_providers) == 1
runtime.find_spec = lambda _: None
assert runtime._create_outgoing_reference_providers(
    handlers.plugin_runtime.reference_bridge
) == ()

adapter = SimpleNamespace(
    get_name=lambda: "OneBot V11",
    config=nonebot.get_driver().config,
)
bot = Bot(adapter=adapter, self_id="4200")

def event_for(user_id: int) -> GroupMessageEvent:
    sender = Sender(user_id=user_id, nickname="tester")
    return GroupMessageEvent(
        time=1,
        self_id=4200,
        post_type="message",
        sub_type="normal",
        user_id=user_id,
        message_type="group",
        message_id=1,
        message=Message("triage 报错查询 P-23456789"),
        original_message=Message("triage 报错查询 P-23456789"),
        raw_message="triage 报错查询 P-23456789",
        font=0,
        sender=sender,
        group_id=100,
        to_me=True,
    )

assert asyncio.run(handlers.query_matcher.check_perm(bot, event_for(200)))
assert asyncio.run(handlers.query_matcher.check_perm(bot, event_for(201)))
assert asyncio.run(handlers.refresh_help_matcher.check_perm(bot, event_for(200)))
assert not asyncio.run(handlers.refresh_help_matcher.check_perm(bot, event_for(201)))
"""

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=project_root,
        env=_clean_subprocess_environment(),
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr


def test_plugin_rejects_removed_custom_command_configuration() -> None:
    project_root = Path(__file__).parents[2]
    script = """
import nonebot
import asyncio
from types import SimpleNamespace
nonebot.init(
    _env_file=(".nonebot-triage-pytest.env",),
    driver="~none",
    nbtriage_command="support",
)
plugin = nonebot.load_plugin("nonebot_plugin_triage")
assert plugin is None
"""

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=project_root,
        env=_clean_subprocess_environment(),
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr


def test_domain_core_does_not_import_nonebot_transport_types() -> None:
    core_root = Path(__file__).parents[2] / "src" / "nbtriage"

    offenders = []
    for path in core_root.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        if "from nonebot" in source or "import nonebot" in source:
            offenders.append(path.name)

    assert offenders == []
