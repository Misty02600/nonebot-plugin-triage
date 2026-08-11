from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from nonebot.adapters.onebot.v11 import Message
from nonebot_plugin_alconna.uniseg.fallback import FallbackMessage
from nonebug import App
from tests.units.fake import fake_group_message_event_v11


def test_nonebot_plugin_loads_with_alconna_cross_platform_metadata() -> None:
    project_root = Path(__file__).parents[2]
    script = """
import nonebot
nonebot.init(driver="~none", superusers={"200"})
plugin = nonebot.load_plugin("nonebot_plugin_triage")
assert plugin is not None

import asyncio
from types import SimpleNamespace

from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message
from nonebot.adapters.onebot.v11.event import Sender
from nonebot_plugin_alconna import AlconnaMatcher
import nonebot_plugin_triage as module
from nonebot_plugin_triage import handlers, runtime

assert issubclass(handlers.report_matcher, AlconnaMatcher)
command = handlers.report_matcher._rule.command()
assert command.parse("报错").matched
assert not command.parse("报错 extra").matched
query_command = handlers.query_matcher._rule.command()
assert query_command.parse("报错查询 incident-example").matched
assert not query_command.parse("报错查询").matched
assert not query_command.parse("报错查询 incident-example extra").matched
feedback_command = handlers.feedback_matcher._rule.command()
assert feedback_command.parse("报错反馈 incident-example 有用").matched
assert not feedback_command.parse("报错反馈 incident-example").matched
stats_command = handlers.trial_stats_matcher._rule.command()
assert stats_command.parse("报错统计").matched
assert not stats_command.parse("报错统计 extra").matched
assert handlers.plugin_runtime.observer.registered
assert handlers.plugin_runtime.reference_bridge.registered
assert handlers.plugin_runtime.trials.mode.value == "off"
assert "nonebot.adapters.onebot.v11" in module.__plugin_meta__.supported_adapters
assert "nonebot.adapters.qq" in module.__plugin_meta__.supported_adapters
assert module.__plugin_meta__.config is module.NBTriageConfig
assert module.__plugin_meta__.name == "NoneBot Triage Agent"
assert module.__plugin_meta__.homepage.endswith("/nonebot-plugin-triage")
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
        message=Message("报错查询 incident-example"),
        original_message=Message("报错查询 incident-example"),
        raw_message="报错查询 incident-example",
        font=0,
        sender=sender,
        group_id=100,
        to_me=True,
    )

assert asyncio.run(handlers.query_matcher.check_perm(bot, event_for(200)))
assert not asyncio.run(handlers.query_matcher.check_perm(bot, event_for(201)))
assert asyncio.run(handlers.feedback_matcher.check_perm(bot, event_for(200)))
assert not asyncio.run(handlers.feedback_matcher.check_perm(bot, event_for(201)))
assert asyncio.run(handlers.trial_stats_matcher.check_perm(bot, event_for(200)))
assert not asyncio.run(handlers.trial_stats_matcher.check_perm(bot, event_for(201)))
"""

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=project_root,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr


def test_plugin_metadata_and_prompts_follow_custom_commands() -> None:
    project_root = Path(__file__).parents[2]
    script = """
import nonebot
nonebot.init(
    driver="~none",
    nbtriage_report_command="求助",
    nbtriage_query_command="受理查询",
    nbtriage_feedback_command="受理反馈",
    nbtriage_trial_stats_command="试用统计",
)
plugin = nonebot.load_plugin("nonebot_plugin_triage")
assert plugin is not None

import nonebot_plugin_triage as module
from nonebot_plugin_triage import handlers

assert handlers.report_matcher._rule.command().parse("求助").matched
assert handlers.query_matcher._rule.command().parse("受理查询 incident-example").matched
assert handlers.feedback_matcher._rule.command().parse("受理反馈 incident-example 有用").matched
assert handlers.trial_stats_matcher._rule.command().parse("试用统计").matched
assert "求助" in module.__plugin_meta__.usage
assert "受理查询" in module.__plugin_meta__.usage
assert "受理反馈" in module.__plugin_meta__.usage
assert "试用统计" in module.__plugin_meta__.usage
assert handlers.plugin_runtime.report_service.report_command == "求助"
"""

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=project_root,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr


async def test_query_matcher_sends_narrow_not_found_reply(app: App) -> None:
    from nonebot_plugin_triage.handlers import query_matcher

    async with app.test_matcher(query_matcher) as ctx:
        bot = ctx.create_bot()
        event = fake_group_message_event_v11(
            user_id=200,
            message=Message("报错查询 incident-example"),
            original_message=Message("报错查询 incident-example"),
            raw_message="报错查询 incident-example",
            to_me=True,
        )
        ctx.receive_event(bot, event)
        ctx.should_call_send(
            event,
            FallbackMessage("未找到该受理编号；记录可能已过期或被容量策略淘汰。"),
            result=None,
        )
        ctx.should_finished(query_matcher)


def test_domain_core_does_not_import_nonebot_transport_types() -> None:
    core_root = Path(__file__).parents[2] / "src" / "nbtriage"

    offenders = []
    for path in core_root.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        if "from nonebot" in source or "import nonebot" in source:
            offenders.append(path.name)

    assert offenders == []
