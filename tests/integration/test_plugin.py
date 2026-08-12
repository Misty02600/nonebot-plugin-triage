from __future__ import annotations

import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from nonebot.adapters.onebot.v11 import Message, MessageSegment
from nonebot.adapters.onebot.v11.event import Reply as OneBotReply
from nonebot.adapters.onebot.v11.event import Sender
from nonebot_plugin_alconna.uniseg.fallback import FallbackMessage
from nonebug import App
from tests.units.fake import fake_group_message_event_v11, fake_private_message_event_v11


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
assert handlers.continuation_matcher.priority == handlers.support_matcher.priority - 1
assert handlers.continuation_matcher.block
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


async def test_reply_to_registered_triage_answer_continues_without_command(
    app: App,
) -> None:
    from nonebot import get_driver
    from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter
    from nonebot.adapters.onebot.v11 import Bot as OneBotV11Bot
    from nonebot_plugin_alconna import SupportAdapter, SupportScope, Target

    from nbtriage.support_threads import ThreadKind
    from nonebot_plugin_triage.handlers import continuation_matcher, plugin_runtime

    target = Target(
        "87654321",
        self_id="1",
        scope=SupportScope.qq_client,
        adapter=SupportAdapter.onebot11,
    )
    thread = plugin_runtime.support_threads.create(
        ThreadKind.GUIDANCE,
        topic_refs=("label:dHJpYWdl",),
    )
    plugin_runtime.thread_reference_bridge.bind_reference(
        adapter_name="OneBot V11",
        bot_scope="1",
        target=target,
        actor_scope="220",
        message_reference="900",
        thread_id=thread.thread_id,
    )
    sender = Sender(user_id=220, nickname="tester")
    reply = OneBotReply(
        time=1,
        message_type="group",
        message_id=900,
        real_id=900,
        sender=sender,
        message=Message("BOT_ANSWER_MUST_NOT_BE_READ"),
    )
    event = fake_group_message_event_v11(
        message_id=121,
        user_id=220,
        group_id=87_654_321,
        message=Message("具体怎么使用"),
        original_message=Message("具体怎么使用"),
        raw_message="具体怎么使用",
        sender=sender,
        reply=reply,
        to_me=False,
    )

    async with app.test_matcher(continuation_matcher) as ctx:
        bot = ctx.create_bot(
            base=OneBotV11Bot,
            adapter=OneBotV11Adapter(get_driver()),
            self_id="1",
            auto_connect=False,
        )
        ctx.receive_event(bot, event)
        ctx.should_call_send(
            event,
            "triage：说明功能用法、纠正指令或受理故障\n"
            "用法：triage <求助内容>\n"
            "示例：triage 某个功能怎么使用",
            result=None,
        )
        ctx.should_finished(continuation_matcher)


async def test_unknown_or_cross_actor_reply_does_not_pass_continuation_rule(
    app: App,
) -> None:
    from nonebot import get_driver
    from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter
    from nonebot.adapters.onebot.v11 import Bot as OneBotV11Bot
    from nonebot_plugin_alconna import SupportAdapter, SupportScope, Target

    from nbtriage.support_threads import ThreadKind
    from nonebot_plugin_triage.handlers import continuation_matcher, plugin_runtime

    bot = OneBotV11Bot(adapter=OneBotV11Adapter(get_driver()), self_id="1")
    target = Target(
        "87654321",
        self_id="1",
        scope=SupportScope.qq_client,
        adapter=SupportAdapter.onebot11,
    )
    thread = plugin_runtime.support_threads.create(ThreadKind.CLARIFICATION)
    plugin_runtime.thread_reference_bridge.bind_reference(
        adapter_name="OneBot V11",
        bot_scope="1",
        target=target,
        actor_scope="221",
        message_reference="901",
        thread_id=thread.thread_id,
    )

    for user_id, reply_id in ((222, 901), (221, 902)):
        sender = Sender(user_id=user_id, nickname="tester")
        event = fake_group_message_event_v11(
            message_id=reply_id + 100,
            user_id=user_id,
            group_id=87_654_321,
            message=Message("继续问"),
            original_message=Message("继续问"),
            raw_message="继续问",
            sender=sender,
            reply=OneBotReply(
                time=1,
                message_type="group",
                message_id=reply_id,
                real_id=reply_id,
                sender=sender,
                message=Message("MUST_NOT_BE_READ"),
            ),
            to_me=False,
        )
        assert not await continuation_matcher.check_rule(bot, event, {})

    ordinary_event = fake_group_message_event_v11(
        message_id=1_100,
        user_id=221,
        group_id=87_654_321,
        message=Message("普通群聊消息"),
        original_message=Message("普通群聊消息"),
        raw_message="普通群聊消息",
        sender=Sender(user_id=221, nickname="tester"),
        reply=None,
        to_me=False,
    )
    assert not await continuation_matcher.check_rule(bot, ordinary_event, {})


@pytest.mark.parametrize(
    ("self_id", "group_id", "user_id", "reply_id"),
    [
        ("2", 87_654_321, 228, 908),
        ("1", 87_654_322, 230, 910),
    ],
)
async def test_cross_bot_or_group_reply_does_not_pass_continuation_rule(
    app: App,
    self_id: str,
    group_id: int,
    user_id: int,
    reply_id: int,
) -> None:
    from nonebot import get_driver
    from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter
    from nonebot.adapters.onebot.v11 import Bot as OneBotV11Bot
    from nonebot_plugin_alconna import SupportAdapter, SupportScope, Target

    from nbtriage.support_threads import ThreadKind
    from nonebot_plugin_triage.handlers import continuation_matcher, plugin_runtime

    target = Target(
        "87654321",
        self_id="1",
        scope=SupportScope.qq_client,
        adapter=SupportAdapter.onebot11,
    )
    thread = plugin_runtime.support_threads.create(ThreadKind.GUIDANCE)
    plugin_runtime.thread_reference_bridge.bind_reference(
        adapter_name="OneBot V11",
        bot_scope="1",
        target=target,
        actor_scope=str(user_id),
        message_reference=str(reply_id),
        thread_id=thread.thread_id,
    )
    sender = Sender(user_id=user_id, nickname="tester")
    event = fake_group_message_event_v11(
        self_id=int(self_id),
        message_id=reply_id + 1_000,
        user_id=user_id,
        group_id=group_id,
        message=Message("继续问"),
        original_message=Message("继续问"),
        raw_message="继续问",
        sender=sender,
        reply=OneBotReply(
            time=1,
            message_type="group",
            message_id=reply_id,
            real_id=reply_id,
            sender=sender,
            message=Message("MUST_NOT_BE_READ"),
        ),
        to_me=False,
    )
    bot = OneBotV11Bot(
        adapter=OneBotV11Adapter(get_driver()),
        self_id=self_id,
    )

    assert not await continuation_matcher.check_rule(bot, event, {})


async def test_closed_thread_reply_does_not_pass_continuation_rule(app: App) -> None:
    from nonebot import get_driver
    from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter
    from nonebot.adapters.onebot.v11 import Bot as OneBotV11Bot
    from nonebot_plugin_alconna import SupportAdapter, SupportScope, Target

    from nbtriage.support_threads import ThreadKind
    from nonebot_plugin_triage.handlers import continuation_matcher, plugin_runtime

    target = Target(
        "87654321",
        self_id="1",
        scope=SupportScope.qq_client,
        adapter=SupportAdapter.onebot11,
    )
    thread = plugin_runtime.support_threads.create(ThreadKind.CLARIFICATION)
    plugin_runtime.thread_reference_bridge.bind_reference(
        adapter_name="OneBot V11",
        bot_scope="1",
        target=target,
        actor_scope="229",
        message_reference="909",
        thread_id=thread.thread_id,
    )
    assert plugin_runtime.support_threads.close(thread.thread_id) is not None
    sender = Sender(user_id=229, nickname="tester")
    event = fake_group_message_event_v11(
        message_id=1_909,
        user_id=229,
        group_id=87_654_321,
        message=Message("继续问"),
        original_message=Message("继续问"),
        raw_message="继续问",
        sender=sender,
        reply=OneBotReply(
            time=1,
            message_type="group",
            message_id=909,
            real_id=909,
            sender=sender,
            message=Message("MUST_NOT_BE_READ"),
        ),
        to_me=False,
    )
    bot = OneBotV11Bot(
        adapter=OneBotV11Adapter(get_driver()),
        self_id="1",
    )

    assert not await continuation_matcher.check_rule(bot, event, {})


def test_thread_topic_labels_are_bounded_and_restore_non_ascii_capability_names() -> None:
    from nbtriage.support_threads import ThreadKind
    from nonebot_plugin_triage import handlers

    refs = handlers._encode_topic_labels(tuple(f"能力{index}\u202e" for index in range(32)))
    thread = handlers.plugin_runtime.support_threads.create(
        ThreadKind.GUIDANCE,
        topic_refs=refs,
    )

    assert len(refs) == 16
    assert sum(len(item.encode()) for item in refs) <= 1_024
    assert "\u202e" not in handlers._continuation_query(thread, "参数呢")
    assert handlers._continuation_query(thread, "参数呢").startswith("能力0 能力1")


async def test_clarification_thread_consumes_one_explicit_follow_up(app: App) -> None:
    from nonebot import get_driver
    from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter
    from nonebot.adapters.onebot.v11 import Bot as OneBotV11Bot
    from nonebot_plugin_alconna import SupportAdapter, SupportScope, Target

    from nbtriage.support_threads import ThreadKind, ThreadStatus
    from nonebot_plugin_triage.handlers import continuation_matcher, plugin_runtime

    target = Target(
        "87654321",
        self_id="1",
        scope=SupportScope.qq_client,
        adapter=SupportAdapter.onebot11,
    )
    thread = plugin_runtime.support_threads.create(ThreadKind.CLARIFICATION)
    plugin_runtime.thread_reference_bridge.bind_reference(
        adapter_name="OneBot V11",
        bot_scope="1",
        target=target,
        actor_scope="223",
        message_reference="903",
        thread_id=thread.thread_id,
    )
    sender = Sender(user_id=223, nickname="tester")
    event = fake_group_message_event_v11(
        message_id=1_103,
        user_id=223,
        group_id=87_654_321,
        message=Message("还是说不清"),
        original_message=Message("还是说不清"),
        raw_message="还是说不清",
        sender=sender,
        reply=OneBotReply(
            time=1,
            message_type="group",
            message_id=903,
            real_id=903,
            sender=sender,
            message=Message("MUST_NOT_BE_READ"),
        ),
        to_me=False,
    )

    async with app.test_matcher(continuation_matcher) as ctx:
        bot = ctx.create_bot(
            base=OneBotV11Bot,
            adapter=OneBotV11Adapter(get_driver()),
            self_id="1",
            auto_connect=False,
        )
        ctx.receive_event(bot, event)
        ctx.should_call_send(
            event,
            "我仍无法确定你是想了解功能还是报告问题，本次不再追问；请重新发送 triage 和完整问题。",
            result=None,
        )
        ctx.should_finished(continuation_matcher)

    closed = plugin_runtime.support_threads.get(thread.thread_id)
    assert closed is not None
    assert closed.status is ThreadStatus.CLOSED


@pytest.mark.parametrize(
    ("user_id", "reply_id", "message", "expected"),
    [
        (
            224,
            904,
            "",
            "本次澄清已结束，请重新发送 triage 和完整问题。",
        ),
        (225, 905, "取消", "已结束这次求助。"),
        (
            226,
            906,
            "x" * 2_001,
            "本次澄清已结束；请重新发送 triage 和缩短后的完整问题，内容需在 2000 字以内。",
        ),
    ],
)
async def test_clarification_thread_closes_on_terminal_follow_up(
    app: App,
    user_id: int,
    reply_id: int,
    message: str,
    expected: str,
) -> None:
    from nonebot import get_driver
    from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter
    from nonebot.adapters.onebot.v11 import Bot as OneBotV11Bot
    from nonebot_plugin_alconna import SupportAdapter, SupportScope, Target

    from nbtriage.support_threads import ThreadKind, ThreadStatus
    from nonebot_plugin_triage.handlers import continuation_matcher, plugin_runtime

    target = Target(
        "87654321",
        self_id="1",
        scope=SupportScope.qq_client,
        adapter=SupportAdapter.onebot11,
    )
    thread = plugin_runtime.support_threads.create(ThreadKind.CLARIFICATION)
    plugin_runtime.thread_reference_bridge.bind_reference(
        adapter_name="OneBot V11",
        bot_scope="1",
        target=target,
        actor_scope=str(user_id),
        message_reference=str(reply_id),
        thread_id=thread.thread_id,
    )
    sender = Sender(user_id=user_id, nickname="tester")
    event = fake_group_message_event_v11(
        message_id=reply_id + 1_000,
        user_id=user_id,
        group_id=87_654_321,
        message=Message(message),
        original_message=Message(message),
        raw_message=message,
        sender=sender,
        reply=OneBotReply(
            time=1,
            message_type="group",
            message_id=reply_id,
            real_id=reply_id,
            sender=sender,
            message=Message("MUST_NOT_BE_READ"),
        ),
        to_me=False,
    )

    async with app.test_matcher(continuation_matcher) as ctx:
        bot = ctx.create_bot(
            base=OneBotV11Bot,
            adapter=OneBotV11Adapter(get_driver()),
            self_id="1",
            auto_connect=False,
        )
        ctx.receive_event(bot, event)
        ctx.should_call_send(event, expected, result=None)
        ctx.should_finished(continuation_matcher)

    closed = plugin_runtime.support_threads.get(thread.thread_id)
    assert closed is not None
    assert closed.status is ThreadStatus.CLOSED


async def test_clarification_thread_can_end_in_explicit_incident(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nonebot import get_driver
    from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter
    from nonebot.adapters.onebot.v11 import Bot as OneBotV11Bot
    from nonebot_plugin_alconna import SupportAdapter, SupportScope, Target

    from nbtriage.support_threads import ThreadKind, ThreadStatus
    from nonebot_plugin_triage.handlers import continuation_matcher, plugin_runtime
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
            "incident-continuation",
        )

    monkeypatch.setattr(plugin_runtime.report_service, "handle", accept)
    target = Target(
        "87654321",
        self_id="1",
        scope=SupportScope.qq_client,
        adapter=SupportAdapter.onebot11,
    )
    thread = plugin_runtime.support_threads.create(ThreadKind.CLARIFICATION)
    plugin_runtime.thread_reference_bridge.bind_reference(
        adapter_name="OneBot V11",
        bot_scope="1",
        target=target,
        actor_scope="227",
        message_reference="907",
        thread_id=thread.thread_id,
    )
    sender = Sender(user_id=227, nickname="tester")
    event = fake_group_message_event_v11(
        message_id=1_907,
        user_id=227,
        group_id=87_654_321,
        message=Message("请受理这个故障"),
        original_message=Message("请受理这个故障"),
        raw_message="请受理这个故障",
        sender=sender,
        reply=OneBotReply(
            time=1,
            message_type="group",
            message_id=907,
            real_id=907,
            sender=sender,
            message=Message("BOT_PROMPT_MUST_NOT_BECOME_REPORT_EVIDENCE"),
        ),
        to_me=False,
    )

    async with app.test_matcher(continuation_matcher) as ctx:
        bot = ctx.create_bot(
            base=OneBotV11Bot,
            adapter=OneBotV11Adapter(get_driver()),
            self_id="1",
            auto_connect=False,
        )
        ctx.receive_event(bot, event)
        ctx.should_call_send(event, "固定受理回执", result=None)
        ctx.should_finished(continuation_matcher)

    assert len(captured) == 1
    assert captured[0].reply_reference is None
    closed = plugin_runtime.support_threads.get(thread.thread_id)
    assert closed is not None
    assert closed.status is ThreadStatus.CLOSED


async def test_continuation_blocks_lower_priority_matcher_in_full_dispatch(
    app: App,
) -> None:
    from nonebot import get_driver
    from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter
    from nonebot.adapters.onebot.v11 import Bot as OneBotV11Bot
    from nonebot.matcher import Matcher
    from nonebot_plugin_alconna import SupportAdapter, SupportScope, Target

    from nbtriage.support_threads import ThreadKind
    from nonebot_plugin_triage.handlers import continuation_matcher, plugin_runtime

    target = Target(
        "87654321",
        self_id="1",
        scope=SupportScope.qq_client,
        adapter=SupportAdapter.onebot11,
    )
    thread = plugin_runtime.support_threads.create(
        ThreadKind.GUIDANCE,
        topic_refs=("label:dHJpYWdl",),
    )
    plugin_runtime.thread_reference_bridge.bind_reference(
        adapter_name="OneBot V11",
        bot_scope="1",
        target=target,
        actor_scope="231",
        message_reference="911",
        thread_id=thread.thread_id,
    )
    sender = Sender(user_id=231, nickname="tester")
    event = fake_group_message_event_v11(
        message_id=1_911,
        user_id=231,
        group_id=87_654_321,
        message=Message("具体怎么使用"),
        original_message=Message("具体怎么使用"),
        raw_message="具体怎么使用",
        sender=sender,
        reply=OneBotReply(
            time=1,
            message_type="group",
            message_id=911,
            real_id=911,
            sender=sender,
            message=Message("MUST_NOT_BE_READ"),
        ),
        to_me=False,
    )

    async def lower_priority_handler() -> None:
        raise AssertionError("lower-priority matcher must be blocked")

    lower = Matcher.new(
        type_="message",
        handlers=[lower_priority_handler],
        priority=continuation_matcher.priority + 1,
    )
    try:
        async with app.test_matcher(
            {
                continuation_matcher.priority: [continuation_matcher],
                lower.priority: [lower],
            }
        ) as ctx:
            bot = ctx.create_bot(
                base=OneBotV11Bot,
                adapter=OneBotV11Adapter(get_driver()),
                self_id="1",
                auto_connect=False,
            )
            ctx.receive_event(bot, event)
            ctx.should_call_send(
                event,
                "triage：说明功能用法、纠正指令或受理故障\n"
                "用法：triage <求助内容>\n"
                "示例：triage 某个功能怎么使用",
                result=None,
            )
            ctx.should_finished(continuation_matcher)
    finally:
        lower.destroy()


@pytest.mark.parametrize(
    ("user_id", "permission_fails", "search_unavailable", "expected"),
    [
        (
            200,
            False,
            False,
            "搜图（维护者可见受限能力；来源：YetAnotherPicSearch）\n"
            "说明：搜索图片出处\n"
            "用法：索引没有可靠用法，请核对当前插件源码、README 或插件自带帮助。\n"
            "约束：存在无法安全静态判断的规则或 handler 条件。\n"
            "发现或可见不等于当前可执行；最终仍由原插件的权限、配置、场景和外部状态判断。",
        ),
        (
            214,
            False,
            False,
            "我目前能说明这些 Alconna 功能：\n"
            "- triage：说明功能用法、纠正指令或受理故障\n"
            "告诉我具体功能名，我再给你用法。",
        ),
        (
            200,
            True,
            False,
            "我目前能说明这些 Alconna 功能：\n"
            "- triage：说明功能用法、纠正指令或受理故障\n"
            "告诉我具体功能名，我再给你用法。",
        ),
        (
            200,
            False,
            True,
            "我目前能说明这些 Alconna 功能：\n"
            "- triage：说明功能用法、纠正指令或受理故障\n"
            "告诉我具体功能名，我再给你用法。",
        ),
    ],
)
async def test_shadow_capability_guidance_is_limited_to_superusers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    user_id: int,
    permission_fails: bool,
    search_unavailable: bool,
    expected: str,
) -> None:
    from nonebot import get_driver
    from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter
    from nonebot.adapters.onebot.v11 import Bot as OneBotV11Bot

    from nbtriage.capabilities import (
        CapabilityRecord,
        CapabilitySnapshot,
        Claim,
        ClaimBasis,
        Constraint,
        ConstraintEvaluability,
        Disclosure,
        RecordState,
    )
    from nonebot_plugin_triage import handlers
    from nonebot_plugin_triage.capability_shadow import CapabilityShadowService

    record = CapabilityRecord(
        capability_id="command:image",
        owner="YetAnotherPicSearch",
        kind="command",
        disclosure=Disclosure.RESTRICTED,
        state=RecordState.CANDIDATE,
        claims=(
            Claim("command.header", "搜图", ClaimBasis.OBSERVED),
            Claim("description", "搜索图片出处", ClaimBasis.DECLARED),
        ),
        constraints=(
            Constraint(
                constraint_id="constraint:handler",
                kind="handlers",
                operation="opaque",
                evaluability=ConstraintEvaluability.OPAQUE,
            ),
        ),
    )
    shadow = CapabilityShadowService(
        tmp_path / "capabilities.sqlite3",
        snapshot_builder=lambda **_: CapabilitySnapshot.create((record,)),
    )
    shadow.refresh()
    permission_warnings: list[tuple[str, tuple[object, ...]]] = []
    private_text = ""
    if permission_fails:
        private_text = "PRIVATE_AUTHORIZATION_FAILURE"

        async def fail_permission(*_: object, **__: object) -> bool:
            raise RuntimeError(private_text)

        class RecordingLogger:
            def warning(self, message: str, *args: object) -> None:
                permission_warnings.append((message, args))

        monkeypatch.setattr(handlers, "SUPERUSER", fail_permission)
        monkeypatch.setattr(handlers, "logger", RecordingLogger())
    if search_unavailable:

        async def unavailable_search(*_: object, **__: object) -> None:
            return None

        monkeypatch.setattr(shadow, "search_for_maintainer", unavailable_search)

    async def no_public_match(*_: object, **__: object) -> None:
        return None

    monkeypatch.setattr(shadow, "search_public", no_public_match)
    monkeypatch.setattr(
        handlers,
        "plugin_runtime",
        replace(handlers.plugin_runtime, capability_shadow=shadow),
    )
    bot = OneBotV11Bot(
        adapter=OneBotV11Adapter(get_driver()),
        self_id="1",
    )
    event = fake_group_message_event_v11(user_id=user_id)

    assert await handlers._capability_guidance(bot, event, "搜图功能怎么用") == expected
    if permission_fails:
        assert permission_warnings == [("NoneBot Triage SUPERUSER capability check failed", ())]
        assert private_text not in repr(permission_warnings)


async def test_public_shadow_capability_guidance_is_available_to_regular_user(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from nonebot import get_driver
    from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter
    from nonebot.adapters.onebot.v11 import Bot as OneBotV11Bot

    from nbtriage.capabilities import (
        CapabilityRecord,
        CapabilitySnapshot,
        Claim,
        ClaimBasis,
        Disclosure,
        RecordState,
    )
    from nonebot_plugin_triage import handlers
    from nonebot_plugin_triage.capability_shadow import CapabilityShadowService

    record = CapabilityRecord(
        capability_id="command:image",
        owner="YetAnotherPicSearch",
        kind="command",
        disclosure=Disclosure.PUBLIC,
        state=RecordState.VERIFIED,
        claims=(
            Claim("command.header", "搜图", ClaimBasis.OBSERVED),
            Claim("description", "搜索图片出处", ClaimBasis.DECLARED),
            Claim("usage", "回复图片后发送搜图", ClaimBasis.DECLARED),
            Claim(
                "plugin.metadata",
                {"supported_adapters": ["~onebot.v11"]},
                ClaimBasis.DECLARED,
            ),
        ),
    )
    shadow = CapabilityShadowService(
        tmp_path / "capabilities.sqlite3",
        snapshot_builder=lambda **_: CapabilitySnapshot.create((record,)),
    )
    shadow.refresh()
    monkeypatch.setattr(
        handlers,
        "plugin_runtime",
        replace(handlers.plugin_runtime, capability_shadow=shadow),
    )
    bot = OneBotV11Bot(adapter=OneBotV11Adapter(get_driver()), self_id="1")
    event = fake_group_message_event_v11(user_id=214)

    assert await handlers._capability_guidance(bot, event, "搜图功能怎么用") == (
        "搜图\n搜索图片出处\n用法：回复图片后发送搜图"
    )


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
    text = "triage 请受理这个故障"
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
        (
            113,
            211,
            "triage 刚才执行后没反应",
            Message("triage 刚才执行后没反应"),
            "我还不能确定你是想了解功能还是报告问题，请再具体一点。",
        ),
        (
            114,
            212,
            "triage 假设它报错，会发生什么",
            Message("triage 假设它报错，会发生什么"),
            "我还不能确定你是想了解功能还是报告问题，请再具体一点。",
        ),
        (
            115,
            213,
            "triage 请受理这个故障，也告诉我怎么配置",
            Message("triage 请受理这个故障，也告诉我怎么配置"),
            "我还不能确定你是想了解功能还是报告问题，请再具体一点。",
        ),
        (
            118,
            216,
            "triage 刚才执行后报错了",
            Message("triage 刚才执行后报错了"),
            "我还不能确定你是想了解功能还是报告问题，请再具体一点。",
        ),
        (
            119,
            217,
            "triage 报错",
            Message("triage 报错"),
            "我还不能确定你是想了解功能还是报告问题，请再具体一点。",
        ),
        (
            120,
            218,
            "triage 报障",
            Message("triage 报障"),
            "我还不能确定你是想了解功能还是报告问题，请再具体一点。",
        ),
    ],
)
async def test_support_matcher_does_not_treat_unverified_or_negated_text_as_incident(
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


async def test_private_support_request_enters_common_guidance_routing(app: App) -> None:
    from nonebot import get_driver
    from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter
    from nonebot.adapters.onebot.v11 import Bot as OneBotV11Bot

    from nonebot_plugin_triage.handlers import plugin_runtime, support_matcher

    incident_count = len(plugin_runtime.incidents)
    text = "triage 某个功能怎么使用"
    async with app.test_matcher(support_matcher) as ctx:
        bot = ctx.create_bot(
            base=OneBotV11Bot,
            adapter=OneBotV11Adapter(get_driver()),
            self_id="1",
            auto_connect=False,
        )
        event = fake_private_message_event_v11(
            message_id=116,
            user_id=214,
            message=Message(text),
            original_message=Message(text),
            raw_message=text,
        )
        ctx.receive_event(bot, event)
        ctx.should_call_send(
            event,
            Message(
                "我目前能说明这些 Alconna 功能：\n"
                "- triage：说明功能用法、纠正指令或受理故障\n"
                "告诉我具体功能名，我再给你用法。"
            ),
            result=None,
        )
        ctx.should_finished(support_matcher)

    assert len(plugin_runtime.incidents) == incident_count


async def test_private_explicit_report_is_rejected_by_incident_service(app: App) -> None:
    from nonebot import get_driver
    from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter
    from nonebot.adapters.onebot.v11 import Bot as OneBotV11Bot

    from nonebot_plugin_triage.handlers import plugin_runtime, support_matcher

    incident_count = len(plugin_runtime.incidents)
    text = "triage 请受理这个故障"
    async with app.test_matcher(support_matcher) as ctx:
        bot = ctx.create_bot(
            base=OneBotV11Bot,
            adapter=OneBotV11Adapter(get_driver()),
            self_id="1",
            auto_connect=False,
        )
        event = fake_private_message_event_v11(
            message_id=117,
            user_id=215,
            message=Message(text),
            original_message=Message(text),
            raw_message=text,
        )
        ctx.receive_event(bot, event)
        ctx.should_call_send(
            event,
            Message("当前不能在私聊中受理故障；其他求助仍可在私聊中使用 triage。"),
            result=None,
        )
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
