from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageSegment
from nonebot.adapters.onebot.v11.event import Reply as OneBotReply
from nonebot.adapters.onebot.v11.event import Sender
from nonebot_plugin_alconna.uniseg.fallback import FallbackMessage
from nonebug import App
from tests.units.fake import fake_group_message_event_v11, fake_private_message_event_v11


def _triage_reply_event(
    *,
    message_id: int,
    user_id: int,
    reply_id: int,
    content: str,
    self_id: int = 1,
    group_id: int = 87_654_321,
) -> GroupMessageEvent:
    sender = Sender(user_id=user_id, nickname="tester")
    text = f"triage {content}" if content else "triage"
    return fake_group_message_event_v11(
        self_id=self_id,
        group_id=group_id,
        message_id=message_id,
        user_id=user_id,
        message=Message(text),
        original_message=Message([MessageSegment.reply(reply_id), MessageSegment.text(f" {text}")]),
        raw_message=f"[CQ:reply,id={reply_id}] {text}",
        sender=sender,
        reply=OneBotReply(
            time=1,
            message_type="group",
            message_id=reply_id,
            real_id=reply_id,
            sender=sender,
            message=Message("BOT_ANSWER_MUST_NOT_BE_READ"),
        ),
        to_me=False,
    )


def _onebot_test_bot(ctx: Any, *, self_id: str = "1") -> Any:
    from nonebot import get_driver
    from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter
    from nonebot.adapters.onebot.v11 import Bot as OneBotV11Bot

    return ctx.create_bot(
        base=OneBotV11Bot,
        adapter=OneBotV11Adapter(get_driver()),
        self_id=self_id,
        auto_connect=False,
    )


def _clean_subprocess_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["NBTRIAGE_TRIAL_LOG_PATH"] = ""
    return environment


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
        env=_clean_subprocess_environment(),
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
import asyncio
from types import SimpleNamespace
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

from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message
from nonebot.adapters.onebot.v11.event import Sender

sender = Sender(user_id=201, nickname="tester")
event = GroupMessageEvent(
    time=1,
    self_id=1,
    post_type="message",
    sub_type="normal",
    user_id=201,
    message_type="group",
    message_id=1,
    message=Message("support 任意自然语言"),
    original_message=Message("support 任意自然语言"),
    raw_message="support 任意自然语言",
    font=0,
    sender=sender,
    group_id=100,
    to_me=False,
)
assert handlers._has_explicit_support_command(event)
adapter = SimpleNamespace(
    get_name=lambda: "OneBot V11",
    config=nonebot.get_driver().config,
)
bot = Bot(adapter=adapter, self_id="1")
assert asyncio.run(handlers.support_matcher.check_rule(bot, event, {}))
event.message = Message("triage 任意自然语言")
event.original_message = Message("triage 任意自然语言")
event.raw_message = "triage 任意自然语言"
assert not handlers._has_explicit_support_command(event)
assert not asyncio.run(handlers.support_matcher.check_rule(bot, event, {}))
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


async def test_explicit_triage_reply_to_registered_answer_continues_thread(
    app: App,
) -> None:
    from nonebot_plugin_alconna import SupportAdapter, SupportScope, Target

    from nbtriage.support_threads import ThreadKind
    from nonebot_plugin_triage.handlers import plugin_runtime, support_matcher

    thread = plugin_runtime.support_threads.create(
        ThreadKind.GUIDANCE,
        topic_refs=("label:dHJpYWdl",),
    )
    plugin_runtime.thread_reference_bridge.bind_reference(
        adapter_name="OneBot V11",
        bot_scope="1",
        target=Target(
            "87654321",
            self_id="1",
            scope=SupportScope.qq_client,
            adapter=SupportAdapter.onebot11,
        ),
        actor_scope="220",
        message_reference="900",
        thread_id=thread.thread_id,
    )
    sender = Sender(user_id=220, nickname="tester")
    event = fake_group_message_event_v11(
        message_id=121,
        user_id=220,
        message=Message("triage 具体怎么使用"),
        original_message=Message(
            [MessageSegment.reply(900), MessageSegment.text(" triage 具体怎么使用")]
        ),
        raw_message="[CQ:reply,id=900] triage 具体怎么使用",
        sender=sender,
        reply=OneBotReply(
            time=1,
            message_type="group",
            message_id=900,
            real_id=900,
            sender=sender,
            message=Message("BOT_ANSWER_MUST_NOT_BE_READ"),
        ),
        to_me=False,
    )

    async with app.test_matcher(support_matcher) as ctx:
        bot = _onebot_test_bot(ctx)
        ctx.receive_event(bot, event)
        ctx.should_call_send(
            event,
            Message(
                "triage：说明功能用法、纠正指令或受理故障\n"
                "用法：triage <求助内容>\n"
                "示例：triage 某个功能怎么使用"
            ),
            result=None,
        )
        ctx.should_finished(support_matcher)


async def test_reply_without_triage_is_ignored_by_support_matcher(app: App) -> None:
    from nonebot_plugin_triage.handlers import support_matcher

    sender = Sender(user_id=220, nickname="tester")
    event = fake_group_message_event_v11(
        message_id=122,
        user_id=220,
        message=Message("具体怎么使用"),
        original_message=Message([MessageSegment.reply(900), MessageSegment.text(" 具体怎么使用")]),
        raw_message="[CQ:reply,id=900] 具体怎么使用",
        sender=sender,
        reply=OneBotReply(
            time=1,
            message_type="group",
            message_id=900,
            real_id=900,
            sender=sender,
            message=Message("BOT_ANSWER_MUST_NOT_BE_READ"),
        ),
        to_me=False,
    )
    async with app.test_matcher(support_matcher) as ctx:
        bot = _onebot_test_bot(ctx)
        ctx.receive_event(bot, event)
        ctx.should_not_pass_rule()


async def test_explicit_triage_with_unknown_reply_starts_new_thread(app: App) -> None:
    from nonebot_plugin_triage.handlers import plugin_runtime, support_matcher

    before = len(plugin_runtime.support_threads)
    event = fake_group_message_event_v11(
        message_id=123,
        user_id=221,
        message=Message("triage 搜图怎么用"),
        original_message=Message(
            [MessageSegment.reply(999_999), MessageSegment.text(" triage 搜图怎么用")]
        ),
        raw_message="[CQ:reply,id=999999] triage 搜图怎么用",
        to_me=False,
    )
    async with app.test_matcher(support_matcher) as ctx:
        bot = _onebot_test_bot(ctx)
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
    assert len(plugin_runtime.support_threads) == before + 1


@pytest.mark.parametrize(
    ("self_id", "group_id", "user_id"),
    [
        (1, 87_654_321, 301),
        (2, 87_654_321, 302),
        (1, 87_654_322, 303),
    ],
)
async def test_explicit_triage_reply_with_cross_scope_starts_new_thread(
    app: App,
    self_id: int,
    group_id: int,
    user_id: int,
) -> None:
    from nonebot_plugin_alconna import SupportAdapter, SupportScope, Target

    from nbtriage.support_threads import ThreadKind
    from nonebot_plugin_triage.handlers import plugin_runtime, support_matcher

    original = plugin_runtime.support_threads.create(ThreadKind.CLARIFICATION)
    plugin_runtime.thread_reference_bridge.bind_reference(
        adapter_name="OneBot V11",
        bot_scope="1",
        target=Target(
            "87654321",
            self_id="1",
            scope=SupportScope.qq_client,
            adapter=SupportAdapter.onebot11,
        ),
        actor_scope="221" if user_id == 301 else str(user_id),
        message_reference="901",
        thread_id=original.thread_id,
    )
    event = _triage_reply_event(
        message_id=1_901 + self_id + group_id % 10 + user_id % 10,
        self_id=self_id,
        group_id=group_id,
        user_id=user_id,
        reply_id=901,
        content="今天天气不错",
    )
    before = len(plugin_runtime.support_threads)
    async with app.test_matcher(support_matcher) as ctx:
        bot = _onebot_test_bot(ctx, self_id=str(self_id))
        ctx.receive_event(bot, event)
        ctx.should_call_send(
            event,
            Message("我还不能确定你是想了解功能还是报告问题，请再具体一点。"),
            result=None,
        )
        ctx.should_finished(support_matcher)
    assert len(plugin_runtime.support_threads) == before + 1
    assert plugin_runtime.support_threads.get(original.thread_id) == original


@pytest.mark.parametrize("closed", [False, True])
async def test_expired_or_closed_thread_reply_starts_new_thread(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
    closed: bool,
) -> None:
    from nonebot_plugin_alconna import SupportAdapter, SupportScope, Target

    from nbtriage.support_threads import InMemorySupportThreadStore, ThreadKind
    from nonebot_plugin_triage import handlers

    now = datetime(2026, 8, 12, tzinfo=UTC)
    store = InMemorySupportThreadStore(
        max_entries=16,
        idle_timeout_seconds=10,
        absolute_timeout_seconds=20,
        clock=lambda: now,
    )
    thread = store.create(ThreadKind.CLARIFICATION)
    if closed:
        store.close(thread.thread_id)
    else:
        monkeypatch.setattr(store, "_clock", lambda: now.replace(second=20))
    monkeypatch.setattr(handlers.plugin_runtime.support_turns, "store", store)
    monkeypatch.setattr(handlers.plugin_runtime.support_turns, "_clock", store._clock)
    monkeypatch.setattr(
        handlers,
        "plugin_runtime",
        replace(handlers.plugin_runtime, support_threads=store),
    )
    handlers.plugin_runtime.thread_reference_bridge.bind_reference(
        adapter_name="OneBot V11",
        bot_scope="1",
        target=Target(
            "87654321",
            self_id="1",
            scope=SupportScope.qq_client,
            adapter=SupportAdapter.onebot11,
        ),
        actor_scope=str(310 + int(closed)),
        message_reference=str(903 + int(closed)),
        thread_id=thread.thread_id,
    )
    event = _triage_reply_event(
        message_id=1_903 + int(closed),
        user_id=310 + int(closed),
        reply_id=903 + int(closed),
        content="今天天气不错",
    )
    async with app.test_matcher(handlers.support_matcher) as ctx:
        bot = _onebot_test_bot(ctx)
        ctx.receive_event(bot, event)
        ctx.should_call_send(
            event,
            Message("我还不能确定你是想了解功能还是报告问题，请再具体一点。"),
            result=None,
        )
        ctx.should_finished(handlers.support_matcher)
    assert len(store) == (2 if closed else 1)
    assert any(item.thread_id != thread.thread_id for item in store._entries.values())


async def test_only_latest_registered_answer_can_continue_thread(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nonebot_plugin_alconna import SupportAdapter, SupportScope, Target

    from nbtriage.support_threads import ThreadKind, ThreadStatus
    from nonebot_plugin_triage.handlers import plugin_runtime, support_matcher

    target = Target(
        "87654321",
        self_id="1",
        scope=SupportScope.qq_client,
        adapter=SupportAdapter.onebot11,
    )
    thread = plugin_runtime.support_threads.create(ThreadKind.CLARIFICATION)
    for reply_id in (904, 905):
        plugin_runtime.thread_reference_bridge.bind_reference(
            adapter_name="OneBot V11",
            bot_scope="1",
            target=target,
            actor_scope="320",
            message_reference=str(reply_id),
            thread_id=thread.thread_id,
        )

    old_reply = _triage_reply_event(
        message_id=1_904,
        user_id=320,
        reply_id=904,
        content="取消",
    )
    async with app.test_matcher(support_matcher) as ctx:
        monkeypatch.setattr(plugin_runtime.support_rate_limiter, "allow", lambda *_: True)
        bot = _onebot_test_bot(ctx)
        ctx.receive_event(bot, old_reply)
        ctx.should_call_send(
            old_reply,
            Message("我还不能确定你是想了解功能还是报告问题，请再具体一点。"),
            result=None,
        )
        ctx.should_finished(support_matcher)
    assert plugin_runtime.support_threads.get(thread.thread_id).status is ThreadStatus.CONTINUABLE
    latest_reply = _triage_reply_event(
        message_id=1_905,
        user_id=320,
        reply_id=905,
        content="取消",
    )
    async with app.test_matcher(support_matcher) as ctx:
        bot = _onebot_test_bot(ctx)
        ctx.receive_event(bot, latest_reply)
        ctx.should_call_send(latest_reply, Message("已结束这次求助。"), result=None)
        ctx.should_finished(support_matcher)
    assert plugin_runtime.support_threads.get(thread.thread_id).status is ThreadStatus.CLOSED


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


@pytest.mark.parametrize(
    ("user_id", "content", "expected"),
    [
        (
            330,
            "还是说不清",
            "我仍无法确定你是想了解功能还是报告问题，本次不再追问；请重新发送 triage 和完整问题。",
        ),
        (331, "取消", "已结束这次求助。"),
        (332, "", "本次澄清已结束，请重新发送 triage 和完整问题。"),
        (
            333,
            "x" * 2_001,
            "本次澄清已结束；请重新发送 triage 和缩短后的完整问题，内容需在 2000 字以内。",
        ),
    ],
)
async def test_clarification_thread_terminal_follow_ups(
    app: App,
    user_id: int,
    content: str,
    expected: str,
) -> None:
    from nonebot_plugin_alconna import SupportAdapter, SupportScope, Target

    from nbtriage.support_threads import ThreadKind, ThreadStatus
    from nonebot_plugin_triage.handlers import plugin_runtime, support_matcher

    reply_id = 910 + len(content) % 20
    thread = plugin_runtime.support_threads.create(ThreadKind.CLARIFICATION)
    plugin_runtime.thread_reference_bridge.bind_reference(
        adapter_name="OneBot V11",
        bot_scope="1",
        target=Target(
            "87654321",
            self_id="1",
            scope=SupportScope.qq_client,
            adapter=SupportAdapter.onebot11,
        ),
        actor_scope=str(user_id),
        message_reference=str(reply_id),
        thread_id=thread.thread_id,
    )
    event = _triage_reply_event(
        message_id=reply_id + 1_000,
        user_id=user_id,
        reply_id=reply_id,
        content=content,
    )
    async with app.test_matcher(support_matcher) as ctx:
        bot = _onebot_test_bot(ctx)
        ctx.receive_event(bot, event)
        ctx.should_call_send(event, Message(expected), result=None)
        ctx.should_finished(support_matcher)
    assert plugin_runtime.support_threads.get(thread.thread_id).status is ThreadStatus.CLOSED


async def test_clarification_follow_up_can_become_incident_without_bot_reply_evidence(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nonebot_plugin_alconna import SupportAdapter, SupportScope, Target

    from nbtriage.support_threads import ThreadKind, ThreadStatus
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
            "incident-continuation",
        )

    monkeypatch.setattr(plugin_runtime.report_service, "handle", accept)
    thread = plugin_runtime.support_threads.create(ThreadKind.CLARIFICATION)
    plugin_runtime.thread_reference_bridge.bind_reference(
        adapter_name="OneBot V11",
        bot_scope="1",
        target=Target(
            "87654321",
            self_id="1",
            scope=SupportScope.qq_client,
            adapter=SupportAdapter.onebot11,
        ),
        actor_scope="340",
        message_reference="926",
        thread_id=thread.thread_id,
    )
    event = _triage_reply_event(
        message_id=1_926,
        user_id=340,
        reply_id=926,
        content="请受理这个故障",
    )
    async with app.test_matcher(support_matcher) as ctx:
        bot = _onebot_test_bot(ctx)
        ctx.receive_event(bot, event)
        ctx.should_call_send(event, Message("固定受理回执"), result=None)
        ctx.should_finished(support_matcher)
    assert len(captured) == 1
    assert captured[0].reply_reference is None
    assert plugin_runtime.support_threads.get(thread.thread_id).status is ThreadStatus.CLOSED


async def test_continuation_turn_uses_same_entry_rate_limit(app: App) -> None:
    from nonebot_plugin_alconna import SupportAdapter, SupportScope, Target

    from nbtriage.support_threads import ThreadKind
    from nonebot_plugin_triage.handlers import plugin_runtime, support_matcher

    thread = plugin_runtime.support_threads.create(ThreadKind.GUIDANCE)
    plugin_runtime.thread_reference_bridge.bind_reference(
        adapter_name="OneBot V11",
        bot_scope="1",
        target=Target(
            "87654321",
            self_id="1",
            scope=SupportScope.qq_client,
            adapter=SupportAdapter.onebot11,
        ),
        actor_scope="350",
        message_reference="927",
        thread_id=thread.thread_id,
    )
    first = _triage_reply_event(
        message_id=1_927,
        user_id=350,
        reply_id=927,
        content="参数怎么用",
    )
    second = _triage_reply_event(
        message_id=1_928,
        user_id=350,
        reply_id=927,
        content="还有示例吗",
    )
    async with app.test_matcher(support_matcher) as ctx:
        bot = _onebot_test_bot(ctx)
        ctx.receive_event(bot, first)
        ctx.should_call_send(
            first,
            Message(
                "我目前能说明这些 Alconna 功能：\n"
                "- triage：说明功能用法、纠正指令或受理故障\n"
                "告诉我具体功能名，我再给你用法。"
            ),
            result=None,
        )
        ctx.should_finished(support_matcher)
        ctx.receive_event(bot, second)
        ctx.should_call_send(
            second,
            Message("求助请求过于频繁，请稍后再试。"),
            result=None,
        )
        ctx.should_finished(support_matcher)


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
        PlatformScope,
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
        platform_scope=PlatformScope.all(),
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
        EvidenceRef,
        PlatformScope,
        RecordState,
        SourceRevision,
    )
    from nbtriage.module_source_revisions import scan_python_module_source
    from nonebot_plugin_triage import handlers
    from nonebot_plugin_triage.capability_shadow import CapabilityShadowService

    module_name = "nonebot_plugin_triage"
    module_path = Path(__file__).parents[2] / "src" / module_name
    scan = scan_python_module_source(module_name, module_path)
    assert scan.manifest is not None
    manifest = scan.manifest
    source = SourceRevision(
        source_id="integration-plugin-source",
        kind="plugin_source",
        revision=manifest.revision,
        locator=f"{module_name}/__init__.py",
        payload={
            "module_name": module_name,
            "line": None,
            "module_source_manifest": manifest.to_dict(),
        },
    )
    evidence = EvidenceRef(
        evidence_id="integration-plugin-evidence",
        source_id=source.source_id,
        kind="plugin_source",
        locator=f"{module_name}/__init__.py",
        content_hash=manifest.revision,
        payload={"module_name": module_name, "line": None},
    )

    def build_deployment(
        pyproject_path: Path,
        *,
        runtime_modules,
    ):
        from nbtriage.artifact_revisions import (
            ArtifactRevision,
            ArtifactRevisionStatus,
            ArtifactSourceKind,
        )
        from nbtriage.capability_deployment import build_capability_deployment

        assert pyproject_path == Path("pyproject.toml")

        def revision_builder(module_name: str, **_: object) -> ArtifactRevision:
            assert module_name == "nonebot_plugin_triage"
            return ArtifactRevision(
                module_name=module_name,
                status=ArtifactRevisionStatus.LOCATED,
                source_kind=ArtifactSourceKind.LOCAL,
                revision=manifest.revision,
                evidence=(),
                distribution_name="nonebot-plugin-triage",
                module_source_manifest=manifest,
            )

        return build_capability_deployment(
            pyproject_path,
            runtime_modules=runtime_modules,
            revision_builder=revision_builder,
        )

    record = CapabilityRecord(
        capability_id="command:image",
        owner="YetAnotherPicSearch",
        kind="command",
        disclosure=Disclosure.PUBLIC,
        state=RecordState.VERIFIED,
        platform_scope=PlatformScope.explicit(("~onebot.v11",)),
        claims=(
            Claim("command.header", "搜图", ClaimBasis.OBSERVED),
            Claim(
                "plugin.module_name",
                module_name,
                ClaimBasis.OBSERVED,
                (evidence.evidence_id,),
            ),
            Claim("description", "搜索图片出处", ClaimBasis.DECLARED),
            Claim("usage", "回复图片后发送搜图", ClaimBasis.DECLARED),
            Claim(
                "plugin.metadata",
                {"supported_adapters": ["~onebot.v11"]},
                ClaimBasis.DECLARED,
            ),
        ),
        evidence_refs=(evidence,),
    )
    shadow = CapabilityShadowService(
        tmp_path / "capabilities.sqlite3",
        snapshot_builder=lambda **_: CapabilitySnapshot.create((record,), (source,)),
        deployment_builder=build_deployment,
        runtime_modules=lambda: (module_name,),
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


@pytest.mark.parametrize("occupied_state", ["pending", "active"])
@pytest.mark.parametrize(
    "text",
    ["triage", "triage 有什么功能", "triage 今天天气不错"],
    ids=["empty", "guidance", "unknown"],
)
async def test_support_matcher_fails_closed_when_thread_capacity_is_reserved(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
    occupied_state: str,
    text: str,
) -> None:
    from nbtriage.support_threads import (
        InMemorySupportThreadStore,
        OutboundThreadReferenceIndex,
        SupportThreadTurnCoordinator,
        ThreadKind,
        TurnClaimStatus,
    )
    from nonebot_plugin_triage import handlers
    from nonebot_plugin_triage.thread_references import SupportThreadReferenceBridge

    thread_ids = iter(("thread-occupied", "thread-attempt"))
    store = InMemorySupportThreadStore(
        max_entries=1,
        idle_timeout_seconds=600,
        absolute_timeout_seconds=1_200,
        id_factory=lambda: next(thread_ids),
    )
    index = OutboundThreadReferenceIndex(
        secret_key=b"r" * 32,
        max_entries=1,
        retention_seconds=1_200,
    )
    coordinator = SupportThreadTurnCoordinator(
        store,
        index,
        secret_key=b"l" * 32,
        lease_timeout_seconds=300,
        token_factory=lambda: "lease-occupied",
    )
    thread = coordinator.create_initial_thread(ThreadKind.CLARIFICATION)
    lease_token: str | None = None
    if occupied_state == "active":
        assert coordinator.bind_initial_reference(
            adapter_name="OneBot V11",
            bot_scope="1",
            conversation_scope="group-87654321",
            actor_scope="500",
            message_reference="occupied-reference",
            thread_id=thread.thread_id,
        )
        claim = coordinator.claim_reply(
            adapter_name="OneBot V11",
            bot_scope="1",
            conversation_scope="group-87654321",
            actor_scope="500",
            message_reference="occupied-reference",
        )
        assert claim.status is TurnClaimStatus.ACQUIRED
        assert claim.lease is not None
        lease_token = claim.lease.token

    bridge = SupportThreadReferenceBridge(coordinator)
    monkeypatch.setattr(
        handlers,
        "plugin_runtime",
        replace(
            handlers.plugin_runtime,
            support_threads=store,
            support_turns=coordinator,
            thread_reference_bridge=bridge,
        ),
    )
    monkeypatch.setattr(
        handlers.plugin_runtime.support_rate_limiter,
        "allow",
        lambda *_: True,
    )

    async def fixed_guidance(*_: object, **__: object) -> object:
        return handlers._GuidanceResult("unused", ())

    def forbidden_binding(*_: object, **__: object) -> None:
        raise AssertionError("failed thread creation must not create an outgoing binding")

    monkeypatch.setattr(handlers, "_capability_guidance_result", fixed_guidance)
    monkeypatch.setattr(handlers, "_set_outgoing_thread", forbidden_binding)
    thread_before = store.get(thread.thread_id)
    assert thread_before is not None
    pending_before = dict(coordinator._pending_initials)
    leases_before = dict(coordinator._leases_by_thread)
    references_before = dict(index._entries)
    dropped_before = store.dropped_count

    event = fake_group_message_event_v11(
        message_id=600,
        user_id=501,
        message=Message(text),
        original_message=Message(text),
        raw_message=text,
        to_me=False,
    )
    async with app.test_matcher(handlers.support_matcher) as ctx:
        bot = _onebot_test_bot(ctx)
        ctx.receive_event(bot, event)
        ctx.should_call_send(
            event,
            Message("求助上下文繁忙，请稍后重试。"),
            result=None,
        )
        ctx.should_finished(handlers.support_matcher)

    assert len(store) == 1
    assert store.get(thread.thread_id) == thread_before
    assert store.dropped_count == dropped_before
    assert coordinator._pending_initials == pending_before
    assert coordinator._leases_by_thread == leases_before
    assert index._entries == references_before
    if lease_token is None:
        assert coordinator.bind_initial_reference(
            adapter_name="OneBot V11",
            bot_scope="1",
            conversation_scope="group-87654321",
            actor_scope="500",
            message_reference="occupied-reference",
            thread_id=thread.thread_id,
        )
    else:
        assert coordinator.close_turn(lease_token)


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


async def test_busy_thread_turn_is_rejected_before_guidance_or_incident(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nonebot_plugin_alconna import SupportAdapter, SupportScope, Target

    from nbtriage.support_threads import ThreadKind, TurnClaimStatus
    from nonebot_plugin_triage import handlers

    target = Target(
        "87654321",
        self_id="1",
        scope=SupportScope.qq_client,
        adapter=SupportAdapter.onebot11,
    )
    thread = handlers.plugin_runtime.support_threads.create(ThreadKind.GUIDANCE)
    handlers.plugin_runtime.thread_reference_bridge.bind_reference(
        adapter_name="OneBot V11",
        bot_scope="1",
        target=target,
        actor_scope="351",
        message_reference="929",
        thread_id=thread.thread_id,
    )
    first_claim = handlers.plugin_runtime.thread_reference_bridge.claim_reply(
        adapter_name="OneBot V11",
        bot_scope="1",
        target=target,
        actor_scope="351",
        message_reference="929",
    )
    assert first_claim.status is TurnClaimStatus.ACQUIRED
    assert first_claim.lease is not None
    thread_count = len(handlers.plugin_runtime.support_threads)

    async def forbidden_guidance(*_: object, **__: object) -> object:
        raise AssertionError("BUSY turn must not enter capability guidance")

    def forbidden_incident(*_: object, **__: object) -> object:
        raise AssertionError("BUSY turn must not enter incident intake")

    monkeypatch.setattr(handlers, "_capability_guidance_result", forbidden_guidance)
    monkeypatch.setattr(handlers.plugin_runtime.report_service, "handle", forbidden_incident)
    monkeypatch.setattr(
        handlers.plugin_runtime.support_rate_limiter,
        "allow",
        lambda *_: True,
    )
    event = _triage_reply_event(
        message_id=1_929,
        user_id=351,
        reply_id=929,
        content="参数怎么用",
    )
    async with app.test_matcher(handlers.support_matcher) as ctx:
        bot = _onebot_test_bot(ctx)
        ctx.receive_event(bot, event)
        ctx.should_call_send(
            event,
            Message("上一轮仍在处理，请稍后重新发送 triage。"),
            result=None,
        )
        ctx.should_finished(handlers.support_matcher)

    current = handlers.plugin_runtime.support_threads.get(thread.thread_id)
    assert current is not None
    assert current.status.value == "continuable"
    assert len(handlers.plugin_runtime.support_threads) == thread_count
    assert handlers.plugin_runtime.thread_reference_bridge.close_turn(first_claim.lease.token)


async def test_thread_claim_error_fails_closed_before_new_request(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nbtriage.support_threads import TurnClaimResult, TurnClaimStatus
    from nonebot_plugin_triage import handlers

    async def forbidden_guidance(*_: object, **__: object) -> object:
        raise AssertionError("claim error must not enter capability guidance")

    def forbidden_incident(*_: object, **__: object) -> object:
        raise AssertionError("claim error must not enter incident intake")

    monkeypatch.setattr(handlers, "_capability_guidance_result", forbidden_guidance)
    monkeypatch.setattr(handlers.plugin_runtime.report_service, "handle", forbidden_incident)
    monkeypatch.setattr(
        handlers.plugin_runtime.support_rate_limiter,
        "allow",
        lambda *_: True,
    )
    monkeypatch.setattr(
        handlers.plugin_runtime.thread_reference_bridge,
        "claim_reply",
        lambda **_: TurnClaimResult(TurnClaimStatus.ERROR),
    )
    thread_count = len(handlers.plugin_runtime.support_threads)
    event = _triage_reply_event(
        message_id=1_930,
        user_id=351,
        reply_id=930,
        content="参数怎么用",
    )

    async with app.test_matcher(handlers.support_matcher) as ctx:
        bot = _onebot_test_bot(ctx)
        ctx.receive_event(bot, event)
        ctx.should_call_send(
            event,
            Message("求助上下文暂时不可用，请重新发送完整 triage。"),
            result=None,
        )
        ctx.should_finished(handlers.support_matcher)

    assert len(handlers.plugin_runtime.support_threads) == thread_count


async def test_superuser_is_rechecked_on_every_guidance_turn(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nonebot_plugin_alconna import SupportAdapter, SupportScope, Target

    from nbtriage.support_threads import ThreadKind
    from nonebot_plugin_triage import handlers

    checks: list[int] = []

    async def changing_permission(*_: object, **__: object) -> bool:
        checks.append(len(checks))
        return len(checks) == 1

    class Shadow:
        async def search_public(self, *_: object, **__: object) -> None:
            return None

        async def search_for_maintainer(self, *_: object, **__: object) -> object:
            return SimpleNamespace(hits=(), partial=False, stale=False)

    monkeypatch.setattr(handlers, "SUPERUSER", changing_permission)
    monkeypatch.setattr(
        handlers,
        "plugin_runtime",
        replace(handlers.plugin_runtime, capability_shadow=Shadow()),
    )
    thread = handlers.plugin_runtime.support_threads.create(ThreadKind.GUIDANCE)
    handlers.plugin_runtime.thread_reference_bridge.bind_reference(
        adapter_name="OneBot V11",
        bot_scope="1",
        target=Target(
            "87654321",
            self_id="1",
            scope=SupportScope.qq_client,
            adapter=SupportAdapter.onebot11,
        ),
        actor_scope="360",
        message_reference="928",
        thread_id=thread.thread_id,
    )
    first = _triage_reply_event(
        message_id=1_929,
        user_id=360,
        reply_id=928,
        content="参数怎么用",
    )
    second = _triage_reply_event(
        message_id=1_930,
        user_id=360,
        reply_id=928,
        content="示例怎么用",
    )
    monkeypatch.setattr(
        handlers.plugin_runtime.support_rate_limiter,
        "allow",
        lambda *_: True,
    )
    async with app.test_matcher(handlers.support_matcher) as ctx:
        bot = _onebot_test_bot(ctx)
        for event in (first, second):
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
            ctx.should_finished(handlers.support_matcher)
    assert checks == [0, 1]


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
