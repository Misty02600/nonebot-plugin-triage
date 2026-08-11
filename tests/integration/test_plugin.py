from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from nonebot.adapters.onebot.v11 import Message, MessageSegment
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

assert issubclass(handlers.support_matcher, AlconnaMatcher)
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
    nbtriage_command="support",
    nbtriage_query_command="受理查询",
    nbtriage_feedback_command="受理反馈",
    nbtriage_trial_stats_command="试用统计",
)
plugin = nonebot.load_plugin("nonebot_plugin_triage")
assert plugin is not None

import nonebot_plugin_triage as module
from nonebot_plugin_triage import handlers

assert handlers.support_matcher._rule.command().parse("support 任意自然语言").matched
assert handlers.query_matcher._rule.command().parse("受理查询 incident-example").matched
assert handlers.feedback_matcher._rule.command().parse("受理反馈 incident-example 有用").matched
assert handlers.trial_stats_matcher._rule.command().parse("试用统计").matched
assert "support" in module.__plugin_meta__.usage
assert "受理查询" in module.__plugin_meta__.usage
assert "受理反馈" in module.__plugin_meta__.usage
assert "试用统计" in module.__plugin_meta__.usage
assert handlers._empty_support_prompt() == "请在 support 后描述想了解的功能或遇到的问题。"
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
            message_id=101,
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


@pytest.mark.parametrize(
    ("message_id", "user_id", "message", "original_message", "to_me"),
    [
        (
            102,
            201,
            Message("triage 某个功能怎么使用"),
            Message("triage 某个功能怎么使用"),
            False,
        ),
        (
            103,
            202,
            Message("triage 某个功能怎么使用"),
            Message([MessageSegment.at(1), MessageSegment.text(" triage 某个功能怎么使用")]),
            True,
        ),
    ],
)
async def test_support_matcher_accepts_optional_at_without_creating_incident(
    app: App,
    message_id: int,
    user_id: int,
    message: Message,
    original_message: Message,
    to_me: bool,
) -> None:
    from nonebot_plugin_triage.handlers import plugin_runtime, support_matcher

    incident_count = len(plugin_runtime.incidents)
    async with app.test_matcher(support_matcher) as ctx:
        bot = ctx.create_bot()
        event = fake_group_message_event_v11(
            message_id=message_id,
            user_id=user_id,
            message=original_message,
            raw_message=str(original_message),
            to_me=to_me,
        )
        event.message = message
        ctx.receive_event(bot, event)
        ctx.should_call_send(
            event,
            FallbackMessage(
                "我目前能说明这些 Alconna 功能：\n"
                "- triage：说明功能用法、纠正指令或受理故障\n"
                "告诉我具体功能名，我再给你用法。"
            ),
            result=None,
        )
        ctx.should_finished(support_matcher)
    assert len(plugin_runtime.incidents) == incident_count


@pytest.mark.parametrize(
    ("message_id", "user_id", "text", "expected"),
    [
        (104, 203, "triage", "请在 triage 后描述想了解的功能或遇到的问题。"),
        (
            105,
            204,
            "triage 今天天气不错",
            "我还不能确定你是想了解功能还是报告问题，请再具体一点。",
        ),
    ],
)
async def test_support_matcher_asks_one_question_for_incomplete_intent(
    app: App,
    message_id: int,
    user_id: int,
    text: str,
    expected: str,
) -> None:
    from nonebot_plugin_triage.handlers import plugin_runtime, support_matcher

    incident_count = len(plugin_runtime.incidents)
    async with app.test_matcher(support_matcher) as ctx:
        bot = ctx.create_bot()
        event = fake_group_message_event_v11(
            message_id=message_id,
            user_id=user_id,
            message=Message(text),
            original_message=Message(text),
            raw_message=text,
            to_me=False,
        )
        ctx.receive_event(bot, event)
        ctx.should_call_send(event, FallbackMessage(expected), result=None)
        ctx.should_finished(support_matcher)
    assert len(plugin_runtime.incidents) == incident_count


async def test_support_matcher_rate_limits_all_support_responses(app: App) -> None:
    from nonebot_plugin_triage.handlers import support_matcher

    expected = (
        "我目前能说明这些 Alconna 功能：\n"
        "- triage：说明功能用法、纠正指令或受理故障\n"
        "告诉我具体功能名，我再给你用法。"
    )
    async with app.test_matcher(support_matcher) as ctx:
        bot = ctx.create_bot()
        first = fake_group_message_event_v11(
            message_id=106,
            user_id=205,
            message=Message("triage 有什么功能"),
            original_message=Message("triage 有什么功能"),
            raw_message="triage 有什么功能",
            to_me=False,
        )
        ctx.receive_event(bot, first)
        ctx.should_call_send(first, FallbackMessage(expected), result=None)
        ctx.should_finished(support_matcher)

        second = fake_group_message_event_v11(
            message_id=107,
            user_id=205,
            message=Message("triage 有什么功能"),
            original_message=Message("triage 有什么功能"),
            raw_message="triage 有什么功能",
            to_me=False,
        )
        ctx.receive_event(bot, second)
        ctx.should_call_send(
            second,
            FallbackMessage("求助请求过于频繁，请稍后再试。"),
            result=None,
        )
        ctx.should_finished(support_matcher)


@pytest.mark.parametrize(
    ("message_id", "user_id", "reply_reference"),
    [(108, 206, None), (109, 207, "321")],
)
async def test_support_matcher_routes_suspected_incident_with_optional_reply(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
    message_id: int,
    user_id: int,
    reply_reference: str | None,
) -> None:
    from nonebot import get_driver
    from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter
    from nonebot.adapters.onebot.v11 import Bot as OneBotV11Bot

    from nonebot_plugin_triage.handlers import plugin_runtime, support_matcher
    from nonebot_plugin_triage.live_reports import (
        LiveReportRequest,
        PublicReportResult,
        PublicReportStatus,
    )

    captured: list[LiveReportRequest] = []

    def accept(request: LiveReportRequest) -> PublicReportResult:
        captured.append(request)
        return PublicReportResult(
            PublicReportStatus.ACCEPTED_WITHOUT_REFERENCE,
            "固定受理回执",
            "incident-test",
        )

    monkeypatch.setattr(plugin_runtime.report_service, "handle", accept)
    text = "triage 刚才执行后报错了"
    original_message = Message(text)
    if reply_reference is not None:
        original_message = Message(
            [
                MessageSegment.reply(int(reply_reference)),
                MessageSegment.text(f" {text}"),
            ]
        )

    async with app.test_matcher(support_matcher) as ctx:
        bot = ctx.create_bot(
            base=OneBotV11Bot,
            adapter=OneBotV11Adapter(get_driver()),
            self_id="1",
            auto_connect=False,
        )
        event = fake_group_message_event_v11(
            message_id=message_id,
            user_id=user_id,
            message=original_message,
            raw_message=str(original_message),
            to_me=False,
        )
        event.message = Message(text)
        ctx.receive_event(bot, event)
        ctx.should_call_send(event, Message("固定受理回执"), result=None)
        ctx.should_finished(support_matcher)

    assert [request.reply_reference for request in captured] == [reply_reference]


@pytest.mark.parametrize(
    ("message_id", "user_id", "text", "original_message", "expected"),
    [
        (
            110,
            208,
            "triage 某个功能怎么使用",
            Message(
                [
                    MessageSegment.reply(654),
                    MessageSegment.text(" triage 某个功能怎么使用"),
                ]
            ),
            "我目前能说明这些 Alconna 功能：\n"
            "- triage：说明功能用法、纠正指令或受理故障\n"
            "告诉我具体功能名，我再给你用法。",
        ),
        (
            111,
            209,
            "triage 这不是报错",
            Message("triage 这不是报错"),
            "我还不能确定你是想了解功能还是报告问题，请再具体一点。",
        ),
        (
            112,
            210,
            "triage 错误码列表",
            Message("triage 错误码列表"),
            "我还不能确定你是想了解功能还是报告问题，请再具体一点。",
        ),
    ],
)
async def test_support_matcher_does_not_treat_reply_or_negation_as_incident(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
    message_id: int,
    user_id: int,
    text: str,
    original_message: Message,
    expected: str,
) -> None:
    from nonebot import get_driver
    from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter
    from nonebot.adapters.onebot.v11 import Bot as OneBotV11Bot

    from nonebot_plugin_triage.handlers import plugin_runtime, support_matcher

    def unexpected_report(_request: object) -> None:
        pytest.fail("non-incident support request reached LiveReportService")

    monkeypatch.setattr(plugin_runtime.report_service, "handle", unexpected_report)
    incident_count = len(plugin_runtime.incidents)
    async with app.test_matcher(support_matcher) as ctx:
        bot = ctx.create_bot(
            base=OneBotV11Bot,
            adapter=OneBotV11Adapter(get_driver()),
            self_id="1",
            auto_connect=False,
        )
        event = fake_group_message_event_v11(
            message_id=message_id,
            user_id=user_id,
            message=original_message,
            raw_message=str(original_message),
            to_me=False,
        )
        event.message = Message(text)
        ctx.receive_event(bot, event)
        ctx.should_call_send(event, Message(expected), result=None)
        ctx.should_finished(support_matcher)

    assert len(plugin_runtime.incidents) == incident_count


def test_domain_core_does_not_import_nonebot_transport_types() -> None:
    core_root = Path(__file__).parents[2] / "src" / "nbtriage"

    offenders = []
    for path in core_root.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        if "from nonebot" in source or "import nonebot" in source:
            offenders.append(path.name)

    assert offenders == []
