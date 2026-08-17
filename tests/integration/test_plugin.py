from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

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
    reply_content: str = "BOT_ANSWER_MUST_NOT_BE_READ",
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
            message=Message(reply_content),
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


def _inject_semantic_assessment(
    monkeypatch: pytest.MonkeyPatch,
    *,
    goals: tuple[str, ...] = (),
    reported_observation: bool = False,
) -> None:
    from nbtriage.support_semantics import (
        SUPPORT_SEMANTIC_SCHEMA_VERSION,
        SupportAssessmentExecutionStatus,
        SupportAssessmentOutcome,
        SupportAssessmentStatus,
        SupportGoal,
        SupportSemanticAssessment,
    )
    from nonebot_plugin_triage import handlers

    assessment = SupportSemanticAssessment(
        schema_version=SUPPORT_SEMANTIC_SCHEMA_VERSION,
        status=(
            SupportAssessmentStatus.ASSESSED
            if goals or reported_observation
            else SupportAssessmentStatus.NEEDS_CLARIFICATION
        ),
        goals=tuple(SupportGoal(item) for item in goals),
        reported_observation=reported_observation,
    )

    class FakeAssessor:
        async def assess(self, request: Any) -> SupportAssessmentOutcome:
            assert request.model_dump(mode="json") == {
                "schema_version": SUPPORT_SEMANTIC_SCHEMA_VERSION,
                "request_text": request.request_text,
            }
            return SupportAssessmentOutcome(
                SupportAssessmentExecutionStatus.COMPLETED,
                assessment,
            )

    monkeypatch.setattr(
        handlers,
        "plugin_runtime",
        replace(handlers.plugin_runtime, semantic_assessment_service=FakeAssessor()),
    )


def _install_isolated_support_threads(monkeypatch: pytest.MonkeyPatch) -> Any:
    from nbtriage.support_threads import (
        InMemorySupportThreadStore,
        OutboundThreadReferenceIndex,
        SupportThreadTurnCoordinator,
    )
    from nonebot_plugin_triage import handlers
    from nonebot_plugin_triage.thread_references import SupportThreadReferenceBridge

    store = InMemorySupportThreadStore(
        max_entries=32,
        idle_timeout_seconds=600,
        absolute_timeout_seconds=1_200,
    )
    index = OutboundThreadReferenceIndex(
        secret_key=b"r" * 32,
        max_entries=32,
        retention_seconds=1_200,
    )
    coordinator = SupportThreadTurnCoordinator(
        store,
        index,
        secret_key=b"t" * 32,
    )
    runtime = replace(
        handlers.plugin_runtime,
        support_threads=store,
        support_turns=coordinator,
        thread_reference_bridge=SupportThreadReferenceBridge(coordinator),
    )
    monkeypatch.setattr(handlers, "plugin_runtime", runtime)
    monkeypatch.setattr(runtime.support_rate_limiter, "allow", lambda *_: True)
    return runtime


def _clean_subprocess_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["NBTRIAGE_TRIAL_LOG_PATH"] = ""
    return environment


def _marketplace_subprocess_environment(*, configured_model: bool) -> dict[str, str]:
    environment = _clean_subprocess_environment()
    for key in (
        "NBTRIAGE_MODEL_BACKEND",
        "NBTRIAGE_MODEL_NAME",
        "NBTRIAGE_MODEL_TIMEOUT_SECONDS",
        "NBTRIAGE_MODEL_MAX_OUTPUT_TOKENS",
        "OPENCODE_API_KEY",
    ):
        environment.pop(key, None)
    if configured_model:
        environment.update(
            {
                "NBTRIAGE_MODEL_BACKEND": "opencode-go-chat",
                "NBTRIAGE_MODEL_NAME": "deepseek-v4-flash",
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
assert handlers.plugin_runtime.model_service is None
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


async def test_query_subcommand_sends_narrow_not_found_reply(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nonebot_plugin_triage import handlers

    class EmptyRepository:
        async def get_problem(self, _problem_id: str) -> None:
            return None

    monkeypatch.setattr(
        handlers,
        "plugin_runtime",
        replace(
            handlers.plugin_runtime,
            bug_workflow_repository=cast(Any, EmptyRepository()),
        ),
    )
    monkeypatch.setattr(handlers, "_support_request_allowed", lambda *_: True)
    query_matcher = handlers.query_matcher

    async with app.test_matcher(query_matcher) as ctx:
        bot = ctx.create_bot()
        event = fake_group_message_event_v11(
            message_id=101,
            user_id=200,
            message=Message("triage 报错查询 P-23456789"),
            original_message=Message("triage 报错查询 P-23456789"),
            raw_message="triage 报错查询 P-23456789",
            to_me=True,
        )
        ctx.receive_event(bot, event)
        ctx.should_call_send(
            event,
            FallbackMessage("没有找到这个问题编号。"),
            result=None,
        )
        ctx.should_finished(query_matcher)


async def test_query_subcommand_lists_pending_problems_without_semantic(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nbtriage.bug_assessment import BugVerdict
    from nbtriage.bug_workflow import (
        ProblemDecisionSource,
        ProblemLifecycle,
        ProblemReviewStatus,
        ProblemSummary,
    )
    from nonebot_plugin_triage import handlers

    class Repository:
        async def list_pending(self) -> tuple[ProblemSummary, ...]:
            return (
                ProblemSummary(
                    problem_id="P-23456789",
                    title="搜图没有返回结果",
                    subject_id="YetAnotherPicSearch.search",
                    verdict=BugVerdict.BUG,
                    decision_source=ProblemDecisionSource.AGENT,
                    review_status=ProblemReviewStatus.UNREVIEWED,
                    lifecycle=ProblemLifecycle.OPEN,
                    report_count=2,
                    occurrence_count=1,
                    last_observed_at="2026-08-16T00:00:00+00:00",
                    latest_decision_at="2026-08-16T00:00:00+00:00",
                ),
            )

    class ForbiddenSemantic:
        async def assess(self, _request: object) -> None:
            raise AssertionError("problem maintenance must not call Semantic")

    monkeypatch.setattr(
        handlers,
        "plugin_runtime",
        replace(
            handlers.plugin_runtime,
            bug_workflow_repository=cast(Any, Repository()),
            semantic_assessment_service=cast(Any, ForbiddenSemantic()),
        ),
    )
    monkeypatch.setattr(handlers, "_support_request_allowed", lambda *_: True)
    query_matcher = handlers.query_matcher

    async with app.test_matcher(query_matcher) as ctx:
        bot = ctx.create_bot()
        event = fake_group_message_event_v11(
            group_id=76_543_210,
            message_id=102,
            user_id=200,
            message=Message("triage 报错查询"),
            original_message=Message("triage 报错查询"),
            raw_message="triage 报错查询",
            to_me=True,
        )
        ctx.receive_event(bot, event)
        ctx.should_call_send(
            event,
            FallbackMessage(
                "当前待处理的 Bug 问题：\n"
                "- P-23456789｜搜图没有返回结果｜报告 2 次｜发生 1 次｜未复核｜待处理"
            ),
            result=None,
        )
        ctx.should_finished(query_matcher)


async def test_query_subcommand_denies_non_superuser_before_repository_access(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nonebot_plugin_triage import handlers

    class ForbiddenRepository:
        async def list_pending(self) -> None:
            raise AssertionError("non-superuser must not read problem data")

    monkeypatch.setattr(
        handlers,
        "plugin_runtime",
        replace(
            handlers.plugin_runtime,
            bug_workflow_repository=cast(Any, ForbiddenRepository()),
        ),
    )
    monkeypatch.setattr(handlers, "_support_request_allowed", lambda *_: True)
    query_matcher = handlers.query_matcher

    async with app.test_matcher(query_matcher) as ctx:
        bot = ctx.create_bot()
        event = fake_group_message_event_v11(
            group_id=76_543_211,
            message_id=103,
            user_id=201,
            message=Message("triage 报错查询"),
            original_message=Message("triage 报错查询"),
            raw_message="triage 报错查询",
            to_me=True,
        )
        ctx.receive_event(bot, event)
        ctx.should_call_send(
            event,
            FallbackMessage("该命令仅供主人使用。"),
            result=None,
        )
        ctx.should_finished(query_matcher)


async def test_query_subcommand_rejects_invalid_problem_id_before_repository_access(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nonebot_plugin_triage import handlers

    class ForbiddenRepository:
        async def get_problem(self, _problem_id: str) -> None:
            raise AssertionError("invalid public ID must not query the repository")

    monkeypatch.setattr(
        handlers,
        "plugin_runtime",
        replace(
            handlers.plugin_runtime,
            bug_workflow_repository=cast(Any, ForbiddenRepository()),
        ),
    )
    monkeypatch.setattr(handlers, "_support_request_allowed", lambda *_: True)

    async with app.test_matcher(handlers.query_matcher) as ctx:
        bot = ctx.create_bot()
        event = fake_group_message_event_v11(
            group_id=76_543_212,
            message_id=104,
            user_id=200,
            message=Message("triage 报错查询 123"),
            original_message=Message("triage 报错查询 123"),
            raw_message="triage 报错查询 123",
            to_me=True,
        )
        ctx.receive_event(bot, event)
        ctx.should_call_send(
            event,
            FallbackMessage("问题编号格式不正确；请使用以 P- 开头的完整编号。"),
            result=None,
        )
        ctx.should_finished(handlers.query_matcher)


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
    monkeypatch: pytest.MonkeyPatch,
    message_id: int,
    user_id: int,
    message: Message,
    original_message: Message,
    to_me: bool,
) -> None:
    from nonebot_plugin_triage import handlers

    _inject_semantic_assessment(monkeypatch, goals=("guidance",))
    incident_count = len(handlers.plugin_runtime.incidents)
    async with app.test_matcher(handlers.support_matcher) as ctx:
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
        ctx.should_finished(handlers.support_matcher)
    assert len(handlers.plugin_runtime.incidents) == incident_count


async def test_support_matcher_rejects_fixed_2000_character_overflow_before_assessment(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nonebot_plugin_triage import handlers

    async def unexpected_assessment(_: object) -> object:
        raise AssertionError("overlong support text must not reach semantic assessment")

    monkeypatch.setattr(
        handlers.plugin_runtime.semantic_assessment_service,
        "assess",
        unexpected_assessment,
    )
    event = fake_group_message_event_v11(
        message_id=104,
        user_id=20_003,
        message=Message(f"triage {'x' * 2_001}"),
        original_message=Message(f"triage {'x' * 2_001}"),
        raw_message=f"triage {'x' * 2_001}",
        to_me=False,
    )

    async with app.test_matcher(handlers.support_matcher) as ctx:
        bot = ctx.create_bot()
        ctx.receive_event(bot, event)
        ctx.should_call_send(
            event,
            FallbackMessage("求助内容过长，请缩短到 2000 字以内。"),
            result=None,
        )
        ctx.should_finished(handlers.support_matcher)


async def test_scope_thread_is_consumed_by_next_explicit_triage_without_reply(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nbtriage.support_threads import ThreadStatus
    from nonebot_plugin_triage import handlers

    runtime = _install_isolated_support_threads(monkeypatch)
    _inject_semantic_assessment(monkeypatch)
    first = fake_group_message_event_v11(
        message_id=1_201,
        user_id=601,
        message=Message("triage 继续"),
        original_message=Message("triage 继续"),
        raw_message="triage 继续",
        to_me=False,
    )
    async with app.test_matcher(handlers.support_matcher) as ctx:
        bot = _onebot_test_bot(ctx)
        ctx.receive_event(bot, first)
        ctx.should_call_send(
            first,
            Message(
                "我还不能确定你想获得什么结果，请再明确一次：了解用法、判断 Bug，还是提出功能建议。"
            ),
            result=None,
        )
        ctx.should_finished(handlers.support_matcher)

    waiting = tuple(runtime.support_threads._entries.values())
    assert len(waiting) == 1
    assert waiting[0].status is ThreadStatus.CONTINUABLE

    observed: list[tuple[str, str | None]] = []

    async def fixed_guidance(
        _bot: object,
        _event: object,
        content: str,
        *,
        conversation_context: str | None = None,
    ) -> object:
        observed.append((content, conversation_context))
        return handlers._GuidanceResult("固定教学回答", ())

    monkeypatch.setattr(handlers, "_capability_guidance_result", fixed_guidance)
    _inject_semantic_assessment(monkeypatch, goals=("guidance",))
    second = fake_group_message_event_v11(
        message_id=1_202,
        user_id=601,
        message=Message("triage 搜图怎么用"),
        original_message=Message("triage 搜图怎么用"),
        raw_message="triage 搜图怎么用",
        to_me=False,
    )
    async with app.test_matcher(handlers.support_matcher) as ctx:
        bot = _onebot_test_bot(ctx)
        ctx.receive_event(bot, second)
        ctx.should_call_send(second, Message("固定教学回答"), result=None)
        ctx.should_finished(handlers.support_matcher)

    assert observed == [("搜图怎么用", "首轮 triage：\n继续")]
    assert runtime.support_threads.get(waiting[0].thread_id).status is ThreadStatus.CLOSED


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


async def test_reply_cannot_select_cross_scope_thread_but_reaches_guidance(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nbtriage.support_threads import ThreadStatus
    from nonebot_plugin_triage import handlers

    runtime = _install_isolated_support_threads(monkeypatch)
    _inject_semantic_assessment(monkeypatch)
    first = fake_group_message_event_v11(
        message_id=1_211,
        user_id=611,
        group_id=87_654_321,
        message=Message("triage 原作用域首轮"),
        original_message=Message("triage 原作用域首轮"),
        raw_message="triage 原作用域首轮",
        to_me=False,
    )
    async with app.test_matcher(handlers.support_matcher) as ctx:
        bot = _onebot_test_bot(ctx)
        ctx.receive_event(bot, first)
        ctx.should_call_send(
            first,
            Message(
                "我还不能确定你想获得什么结果，请再明确一次：了解用法、判断 Bug，还是提出功能建议。"
            ),
            result=None,
        )
        ctx.should_finished(handlers.support_matcher)

    original = next(iter(runtime.support_threads._entries.values()))
    observed: list[str | None] = []

    async def fixed_guidance(
        _bot: object,
        _event: object,
        _content: str,
        *,
        conversation_context: str | None = None,
    ) -> object:
        observed.append(conversation_context)
        return handlers._GuidanceResult("跨作用域教学", ())

    monkeypatch.setattr(handlers, "_capability_guidance_result", fixed_guidance)
    _inject_semantic_assessment(monkeypatch, goals=("guidance",))
    second = _triage_reply_event(
        message_id=1_212,
        user_id=611,
        group_id=87_654_322,
        reply_id=9_999,
        content="这个怎么用",
        reply_content="被回复的高相关原文 api_key=visible-in-chat",
    )
    async with app.test_matcher(handlers.support_matcher) as ctx:
        bot = _onebot_test_bot(ctx)
        ctx.receive_event(bot, second)
        ctx.should_call_send(second, Message("跨作用域教学"), result=None)
        ctx.should_finished(handlers.support_matcher)

    assert observed == ["本轮 Reply：\n被回复的高相关原文 api_key=visible-in-chat"]
    assert observed[0] is not None
    assert "原作用域首轮" not in observed[0]
    assert runtime.support_threads.get(original.thread_id).status is ThreadStatus.CONTINUABLE
    assert [item.status for item in runtime.support_threads._entries.values()] == [
        ThreadStatus.CONTINUABLE,
        ThreadStatus.CLOSED,
    ]


async def test_successful_guidance_closes_scope_so_next_triage_is_new(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nbtriage.support_threads import ThreadStatus
    from nonebot_plugin_triage import handlers

    runtime = _install_isolated_support_threads(monkeypatch)
    _inject_semantic_assessment(monkeypatch, goals=("guidance",))

    async def fixed_guidance(*_: object, **__: object) -> object:
        return handlers._GuidanceResult("教学完成", ())

    monkeypatch.setattr(handlers, "_capability_guidance_result", fixed_guidance)
    events = tuple(
        fake_group_message_event_v11(
            message_id=1_220 + index,
            user_id=612,
            message=Message(f"triage 用法 {index}"),
            original_message=Message(f"triage 用法 {index}"),
            raw_message=f"triage 用法 {index}",
            to_me=False,
        )
        for index in (1, 2)
    )
    async with app.test_matcher(handlers.support_matcher) as ctx:
        bot = _onebot_test_bot(ctx)
        for event in events:
            ctx.receive_event(bot, event)
            ctx.should_call_send(event, Message("教学完成"), result=None)
            ctx.should_finished(handlers.support_matcher)

    records = tuple(runtime.support_threads._entries.values())
    assert len(records) == 2
    assert records[0].thread_id != records[1].thread_id
    assert all(item.status is ThreadStatus.CLOSED for item in records)


async def test_unmatched_guidance_waits_once_then_closes_after_second_miss(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nbtriage.support_threads import ThreadStatus
    from nonebot_plugin_triage import handlers

    runtime = _install_isolated_support_threads(monkeypatch)
    _inject_semantic_assessment(monkeypatch, goals=("guidance",))
    observed_contexts: list[str | None] = []

    async def unmatched_guidance(
        _bot: object,
        _event: object,
        _content: str,
        *,
        conversation_context: str | None = None,
    ) -> object:
        observed_contexts.append(conversation_context)
        return handlers._GuidanceResult(
            "告诉我具体功能名，我再给你用法。",
            (),
            handlers._GuidanceStatus.NEEDS_SUBJECT,
        )

    monkeypatch.setattr(handlers, "_capability_guidance_result", unmatched_guidance)
    first = fake_group_message_event_v11(
        message_id=1_225,
        user_id=613,
        message=Message("triage 这个怎么用"),
        original_message=Message("triage 这个怎么用"),
        raw_message="triage 这个怎么用",
        to_me=False,
    )
    second = fake_group_message_event_v11(
        message_id=1_226,
        user_id=613,
        message=Message("triage 还是这个"),
        original_message=Message("triage 还是这个"),
        raw_message="triage 还是这个",
        to_me=False,
    )
    async with app.test_matcher(handlers.support_matcher) as ctx:
        bot = _onebot_test_bot(ctx)
        ctx.receive_event(bot, first)
        ctx.should_call_send(
            first,
            Message("告诉我具体功能名，我再给你用法。"),
            result=None,
        )
        ctx.should_finished(handlers.support_matcher)
        ctx.receive_event(bot, second)
        ctx.should_call_send(
            second,
            Message(
                "告诉我具体功能名，我再给你用法。\n本次补充已结束；请重新发送 triage 和完整问题。"
            ),
            result=None,
        )
        ctx.should_finished(handlers.support_matcher)

    assert observed_contexts == [None, "首轮 triage：\n这个怎么用"]
    records = tuple(runtime.support_threads._entries.values())
    assert len(records) == 1
    assert records[0].status is ThreadStatus.CLOSED


async def test_unavailable_guidance_evidence_closes_without_supplement(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nbtriage.support_threads import ThreadStatus
    from nonebot_plugin_triage import handlers

    runtime = _install_isolated_support_threads(monkeypatch)
    _inject_semantic_assessment(monkeypatch, goals=("guidance",))

    async def unavailable_guidance(*_: object, **__: object) -> object:
        return handlers._GuidanceResult(
            "当前没有可说明的公开能力。",
            (),
            handlers._GuidanceStatus.UNAVAILABLE,
        )

    monkeypatch.setattr(handlers, "_capability_guidance_result", unavailable_guidance)
    event = fake_group_message_event_v11(
        message_id=1_227,
        user_id=614,
        message=Message("triage 这个怎么用"),
        original_message=Message("triage 这个怎么用"),
        raw_message="triage 这个怎么用",
        to_me=False,
    )
    async with app.test_matcher(handlers.support_matcher) as ctx:
        bot = _onebot_test_bot(ctx)
        ctx.receive_event(bot, event)
        ctx.should_call_send(event, Message("当前没有可说明的公开能力。"), result=None)
        ctx.should_finished(handlers.support_matcher)

    records = tuple(runtime.support_threads._entries.values())
    assert len(records) == 1
    assert records[0].status is ThreadStatus.CLOSED
    assert runtime.support_turns._thread_by_scope == {}


async def test_first_clarify_waits_once_and_second_unresolved_closes(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nbtriage.support_threads import ThreadStatus
    from nonebot_plugin_triage import handlers

    runtime = _install_isolated_support_threads(monkeypatch)
    _inject_semantic_assessment(monkeypatch)
    first = fake_group_message_event_v11(
        message_id=1_231,
        user_id=621,
        message=Message("triage 看看这个"),
        original_message=Message("triage 看看这个"),
        raw_message="triage 看看这个",
        to_me=False,
    )
    second = fake_group_message_event_v11(
        message_id=1_232,
        user_id=621,
        message=Message("triage 还是看看这个"),
        original_message=Message("triage 还是看看这个"),
        raw_message="triage 还是看看这个",
        to_me=False,
    )
    async with app.test_matcher(handlers.support_matcher) as ctx:
        bot = _onebot_test_bot(ctx)
        ctx.receive_event(bot, first)
        ctx.should_call_send(
            first,
            Message(
                "我还不能确定你想获得什么结果，请再明确一次：了解用法、判断 Bug，还是提出功能建议。"
            ),
            result=None,
        )
        ctx.should_finished(handlers.support_matcher)
        ctx.receive_event(bot, second)
        ctx.should_call_send(
            second,
            Message("我仍无法确定你想获得什么结果，本次补充已结束；请重新发送 triage 和完整问题。"),
            result=None,
        )
        ctx.should_finished(handlers.support_matcher)

    records = tuple(runtime.support_threads._entries.values())
    assert len(records) == 1
    assert records[0].status is ThreadStatus.CLOSED


async def test_first_bug_unknown_waits_once_and_second_unknown_closes(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nbtriage.bug_assessment import (
        BugAssessmentDecision,
        BugDecisionSource,
        BugEvidenceKind,
        BugOccurrence,
        BugReason,
        BugResponsibility,
        BugVerdict,
    )
    from nbtriage.support_threads import ThreadStatus
    from nonebot_plugin_triage import handlers

    runtime = _install_isolated_support_threads(monkeypatch)
    _inject_semantic_assessment(
        monkeypatch,
        goals=("bug_assessment",),
        reported_observation=True,
    )
    observed: list[Any] = []

    class UnknownBugService:
        async def assess(self, request: Any) -> BugAssessmentDecision:
            observed.append(request)
            return BugAssessmentDecision(
                verdict=BugVerdict.UNKNOWN,
                occurrence=BugOccurrence.UNKNOWN,
                responsibility_candidates=(BugResponsibility.UNKNOWN,),
                reason=BugReason.INSUFFICIENT_EVIDENCE,
                evidence_ids=(),
                missing_evidence=(BugEvidenceKind.RUNTIME_OBSERVATION,),
                source=BugDecisionSource.AGENT,
            )

    monkeypatch.setattr(
        handlers,
        "plugin_runtime",
        replace(handlers.plugin_runtime, bug_assessment_service=UnknownBugService()),
    )
    first = fake_group_message_event_v11(
        message_id=1_241,
        user_id=631,
        message=Message("triage 刚才没反应，判断是不是 Bug"),
        original_message=Message("triage 刚才没反应，判断是不是 Bug"),
        raw_message="triage 刚才没反应，判断是不是 Bug",
        to_me=False,
    )
    second = fake_group_message_event_v11(
        message_id=1_242,
        user_id=631,
        message=Message("triage 点了按钮仍然没响应"),
        original_message=Message("triage 点了按钮仍然没响应"),
        raw_message="triage 点了按钮仍然没响应",
        to_me=False,
    )
    async with app.test_matcher(handlers.support_matcher) as ctx:
        bot = _onebot_test_bot(ctx)
        ctx.receive_event(bot, first)
        ctx.should_call_send(
            first,
            Message(
                "判断结果：暂时无法判断。请回复实际执行的命令或机器人返回，"
                "并在下一条 triage 中补充操作对象、输入与可见结果。"
            ),
            result=None,
        )
        ctx.should_finished(handlers.support_matcher)
        ctx.receive_event(bot, second)
        ctx.should_call_send(
            second,
            Message("暂时无法判断是不是 Bug。"),
            result=None,
        )
        ctx.should_finished(handlers.support_matcher)

    assert len(observed) == 2
    assert observed[0].reported_observation is True
    assert observed[1].reported_observation is True
    assert observed[0].conversation_context is None
    assert observed[1].conversation_context == "首轮 triage：\n刚才没反应，判断是不是 Bug"
    records = tuple(runtime.support_threads._entries.values())
    assert len(records) == 1
    assert records[0].status is ThreadStatus.CLOSED


async def test_conclusive_bug_assessment_closes_scope(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nbtriage.bug_assessment import (
        BugAssessmentDecision,
        BugDecisionSource,
        BugOccurrence,
        BugReason,
        BugResponsibility,
        BugVerdict,
    )
    from nbtriage.bug_workflow import BugRecordReceipt, RecordBugCommand
    from nbtriage.support_threads import ThreadStatus
    from nonebot_plugin_triage import handlers
    from nonebot_plugin_triage.bug_assessment_runtime import BugAssessmentRuntimeOutcome

    runtime = _install_isolated_support_threads(monkeypatch)
    _inject_semantic_assessment(
        monkeypatch,
        goals=("bug_assessment",),
        reported_observation=True,
    )

    decision = BugAssessmentDecision(
        verdict=BugVerdict.BUG,
        occurrence=BugOccurrence.SINGLE_OBSERVED,
        responsibility_candidates=(BugResponsibility.TARGET_PLUGIN,),
        reason=BugReason.RUNTIME_CONTRADICTS_CONTRACT,
        evidence_ids=("runtime-evidence",),
        missing_evidence=(),
        source=BugDecisionSource.AGENT,
    )

    async def assessed(*_: Any, **__: Any) -> BugAssessmentRuntimeOutcome:
        return BugAssessmentRuntimeOutcome(decision, cast(RecordBugCommand, object()))

    class WorkflowRepository:
        async def record_bug(self, command: RecordBugCommand) -> BugRecordReceipt:
            assert command is not None
            return BugRecordReceipt("P-23456789", False, 1, 1)

    monkeypatch.setattr(
        handlers,
        "plugin_runtime",
        replace(
            handlers.plugin_runtime,
            bug_workflow_repository=cast(Any, WorkflowRepository()),
        ),
    )
    monkeypatch.setattr(handlers, "_bug_assessment_decision", assessed)
    event = fake_group_message_event_v11(
        message_id=1_251,
        user_id=641,
        message=Message("triage 判断刚才的问题是不是 Bug"),
        original_message=Message("triage 判断刚才的问题是不是 Bug"),
        raw_message="triage 判断刚才的问题是不是 Bug",
        to_me=False,
    )
    async with app.test_matcher(handlers.support_matcher) as ctx:
        bot = _onebot_test_bot(ctx)
        ctx.receive_event(bot, event)
        ctx.should_call_send(
            event,
            Message("确认这是一个 Bug，已记录（编号 P-23456789），请等待主人解决。"),
            result=None,
        )
        ctx.should_finished(handlers.support_matcher)

    records = tuple(runtime.support_threads._entries.values())
    assert len(records) == 1
    assert records[0].status is ThreadStatus.CLOSED


async def test_public_precheck_misuse_reuses_guidance_and_closes_scope(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nbtriage.bug_assessment import (
        BugAssessmentDecision,
        BugDecisionSource,
        BugOccurrence,
        BugReason,
        BugResponsibility,
        BugVerdict,
    )
    from nbtriage.support_threads import ThreadStatus
    from nonebot_plugin_triage import handlers

    runtime = _install_isolated_support_threads(monkeypatch)
    _inject_semantic_assessment(
        monkeypatch,
        goals=("bug_assessment",),
        reported_observation=True,
    )

    class PublicPrecheckService:
        async def assess(self, _request: Any) -> BugAssessmentDecision:
            return BugAssessmentDecision(
                verdict=BugVerdict.NOT_BUG,
                occurrence=BugOccurrence.SINGLE_OBSERVED,
                responsibility_candidates=(BugResponsibility.USER_INPUT,),
                reason=BugReason.PUBLIC_PRECONDITION_NOT_MET,
                evidence_ids=("public-contract:test",),
                missing_evidence=(),
                source=BugDecisionSource.PUBLIC_PRECHECK,
            )

    async def correction_guidance(*_: Any, **__: Any) -> Any:
        return handlers._GuidanceResult("正确用法：回复图片后发送“搜图”。", ("搜图",))

    monkeypatch.setattr(
        handlers,
        "plugin_runtime",
        replace(handlers.plugin_runtime, bug_assessment_service=PublicPrecheckService()),
    )
    monkeypatch.setattr(handlers, "_capability_guidance_result", correction_guidance)
    event = fake_group_message_event_v11(
        message_id=1_261,
        user_id=651,
        message=Message("triage 搜图没有响应，请判断是不是 Bug"),
        original_message=Message("triage 搜图没有响应，请判断是不是 Bug"),
        raw_message="triage 搜图没有响应，请判断是不是 Bug",
        to_me=False,
    )

    async with app.test_matcher(handlers.support_matcher) as ctx:
        bot = _onebot_test_bot(ctx)
        ctx.receive_event(bot, event)
        ctx.should_call_send(
            event,
            Message(
                "这次操作不符合当前公开用法，因此不作为 Bot 软件 Bug。\n"
                "正确用法：回复图片后发送“搜图”。"
            ),
            result=None,
        )
        ctx.should_finished(handlers.support_matcher)

    records = tuple(runtime.support_threads._entries.values())
    assert len(records) == 1
    assert records[0].status is ThreadStatus.CLOSED


async def test_failed_clarification_send_closes_scope(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nbtriage.support_threads import ThreadStatus
    from nonebot_plugin_triage import handlers

    runtime = _install_isolated_support_threads(monkeypatch)
    _inject_semantic_assessment(monkeypatch)
    event = fake_group_message_event_v11(
        message_id=1_271,
        user_id=661,
        message=Message("triage 看看这个"),
        original_message=Message("triage 看看这个"),
        raw_message="triage 看看这个",
        to_me=False,
    )

    with pytest.raises(pytest.fail.Exception):
        async with app.test_matcher(handlers.support_matcher) as ctx:
            bot = _onebot_test_bot(ctx)
            ctx.receive_event(bot, event)
            ctx.should_call_send(
                event,
                Message(
                    "我还不能确定你想获得什么结果，请再明确一次：了解用法、判断 Bug，"
                    "还是提出功能建议。"
                ),
                exception=RuntimeError("send failed"),
            )

    records = tuple(runtime.support_threads._entries.values())
    assert len(records) == 1
    assert records[0].status is ThreadStatus.CLOSED


async def test_guidance_never_reads_restricted_shadow_or_checks_superuser(
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

    async def forbidden_permission(*_: object, **__: object) -> bool:
        raise AssertionError("guidance must not check SUPERUSER")

    async def forbidden_restricted_search(*_: object, **__: object) -> None:
        raise AssertionError("guidance must not read restricted capability evidence")

    monkeypatch.setattr(handlers, "SUPERUSER", forbidden_permission)
    monkeypatch.setattr(shadow, "search_for_maintainer", forbidden_restricted_search)

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
    event = fake_group_message_event_v11(user_id=200)

    assert await handlers._capability_guidance(bot, event, "搜图功能怎么用") == (
        "我目前能说明这些 Alconna 功能：\n"
        "- triage：说明功能用法、纠正指令或受理故障\n"
        "告诉我具体功能名，我再给你用法。"
    )


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
    from nonebot_plugin_triage import handlers
    from nonebot_plugin_triage.capability_shadow import CapabilityShadowService

    module_name = "nonebot_plugin_triage"
    revision = "0" * 64
    source = SourceRevision(
        source_id="integration-plugin-source",
        kind="plugin_source",
        revision=revision,
        locator=f"{module_name}/__init__.py",
        payload={
            "module_name": module_name,
            "line": None,
        },
    )
    evidence = EvidenceRef(
        evidence_id="integration-plugin-evidence",
        source_id=source.source_id,
        kind="plugin_source",
        locator=f"{module_name}/__init__.py",
        content_hash=revision,
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
                revision=revision,
                evidence=(),
                distribution_name="nonebot-plugin-triage",
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
            ("我还不能确定你想获得什么结果，请再明确一次：了解用法、判断 Bug，还是提出功能建议。"),
        ),
    ],
)
async def test_support_matcher_asks_one_question_for_incomplete_intent(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
    message_id: int,
    user_id: int,
    text: str,
    expected: str,
) -> None:
    from nonebot_plugin_triage import handlers

    _install_isolated_support_threads(monkeypatch)
    async with app.test_matcher(handlers.support_matcher) as ctx:
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
        ctx.should_finished(handlers.support_matcher)


async def test_support_matcher_fails_closed_when_thread_capacity_is_reserved(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nbtriage.support_threads import (
        InMemorySupportThreadStore,
        OutboundThreadReferenceIndex,
        SupportThreadInitialContext,
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
    claim = coordinator.claim_scope(
        adapter_name="OneBot V11",
        bot_scope="1",
        conversation_scope="group-87654321",
        actor_scope="500",
        create_kind=ThreadKind.CLARIFICATION,
        initial_context=SupportThreadInitialContext(request_text="occupied"),
    )
    assert claim.status is TurnClaimStatus.ACQUIRED
    assert claim.lease is not None
    thread = claim.lease.thread

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

    monkeypatch.setattr(handlers, "_capability_guidance_result", fixed_guidance)
    thread_before = store.get(thread.thread_id)
    assert thread_before is not None
    leases_before = dict(coordinator._leases_by_thread)
    dropped_before = store.dropped_count

    text = "triage 今天天气不错"
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
            Message("求助上下文暂时不可用，请重新发送完整 triage。"),
            result=None,
        )
        ctx.should_finished(handlers.support_matcher)

    assert len(store) == 1
    assert store.get(thread.thread_id) == thread_before
    assert store.dropped_count == dropped_before
    assert coordinator._leases_by_thread == leases_before
    assert coordinator.close_turn(claim.lease.token)


async def test_support_matcher_rate_limits_all_support_responses(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nonebot_plugin_triage import handlers

    async def unexpected_guidance(*_: object, **__: object) -> object:
        raise AssertionError("unclassified text must not read capability sources")

    def unexpected_report(*_: object, **__: object) -> object:
        raise AssertionError("unclassified text must not enter incident intake")

    monkeypatch.setattr(handlers, "_capability_guidance_result", unexpected_guidance)
    monkeypatch.setattr(handlers.plugin_runtime.report_service, "handle", unexpected_report)
    async with app.test_matcher(handlers.support_matcher) as ctx:
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
        ctx.should_call_send(
            first,
            FallbackMessage(
                "我还不能确定你想获得什么结果，请再明确一次：了解用法、判断 Bug，还是提出功能建议。"
            ),
            result=None,
        )
        ctx.should_finished(handlers.support_matcher)

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
        ctx.should_finished(handlers.support_matcher)


async def test_same_scope_busy_is_rejected_while_another_actor_is_isolated(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nonebot_plugin_alconna import SupportAdapter, SupportScope, Target

    from nbtriage.support_threads import (
        SupportThreadInitialContext,
        ThreadKind,
        ThreadStatus,
        TurnClaimStatus,
    )
    from nonebot_plugin_triage import handlers
    from nonebot_plugin_triage.universal_references import conversation_scope

    runtime = _install_isolated_support_threads(monkeypatch)
    target = Target(
        "87654321",
        self_id="1",
        scope=SupportScope.qq_client,
        adapter=SupportAdapter.onebot11,
    )
    first_claim = runtime.support_turns.claim_scope(
        adapter_name="OneBot V11",
        bot_scope="1",
        conversation_scope=conversation_scope(target),
        actor_scope="651",
        create_kind=ThreadKind.CLARIFICATION,
        initial_context=SupportThreadInitialContext(request_text="正在处理"),
    )
    assert first_claim.status is TurnClaimStatus.ACQUIRED
    assert first_claim.lease is not None

    calls: list[str] = []

    async def fixed_guidance(
        _bot: object,
        _event: object,
        content: str,
        **_: object,
    ) -> object:
        calls.append(content)
        return handlers._GuidanceResult("另一用户的教学", ())

    monkeypatch.setattr(handlers, "_capability_guidance_result", fixed_guidance)
    _inject_semantic_assessment(monkeypatch, goals=("guidance",))
    busy = fake_group_message_event_v11(
        message_id=1_261,
        user_id=651,
        message=Message("triage 参数怎么用"),
        original_message=Message("triage 参数怎么用"),
        raw_message="triage 参数怎么用",
        to_me=False,
    )
    other_actor = fake_group_message_event_v11(
        message_id=1_262,
        user_id=652,
        message=Message("triage 搜图怎么用"),
        original_message=Message("triage 搜图怎么用"),
        raw_message="triage 搜图怎么用",
        to_me=False,
    )
    async with app.test_matcher(handlers.support_matcher) as ctx:
        bot = _onebot_test_bot(ctx)
        ctx.receive_event(bot, busy)
        ctx.should_call_send(
            busy,
            Message("上一轮仍在处理，请稍后重新发送 triage。"),
            result=None,
        )
        ctx.should_finished(handlers.support_matcher)
        ctx.receive_event(bot, other_actor)
        ctx.should_call_send(other_actor, Message("另一用户的教学"), result=None)
        ctx.should_finished(handlers.support_matcher)

    current = runtime.support_threads.get(first_claim.lease.thread.thread_id)
    assert current is not None
    assert current.status is ThreadStatus.CONTINUABLE
    assert calls == ["搜图怎么用"]
    assert len(runtime.support_threads) == 2
    assert runtime.support_turns.close_turn(first_claim.lease.token)


async def test_thread_claim_error_fails_closed_before_new_request(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nbtriage.support_threads import TurnClaimResult, TurnClaimStatus
    from nonebot_plugin_triage import handlers

    runtime = _install_isolated_support_threads(monkeypatch)

    async def forbidden_guidance(*_: object, **__: object) -> object:
        raise AssertionError("claim error must not enter capability guidance")

    monkeypatch.setattr(handlers, "_capability_guidance_result", forbidden_guidance)
    monkeypatch.setattr(
        runtime.support_turns,
        "claim_scope",
        lambda **_: TurnClaimResult(TurnClaimStatus.ERROR),
    )
    event = fake_group_message_event_v11(
        message_id=1_930,
        user_id=351,
        message=Message("triage 参数怎么用"),
        original_message=Message("triage 参数怎么用"),
        raw_message="triage 参数怎么用",
        to_me=False,
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

    assert len(runtime.support_threads) == 0


async def test_guidance_turns_do_not_check_superuser(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nonebot_plugin_triage import handlers

    _install_isolated_support_threads(monkeypatch)
    checks: list[int] = []

    async def forbidden_permission(*_: object, **__: object) -> bool:
        checks.append(len(checks))
        raise AssertionError("guidance must not check SUPERUSER")

    monkeypatch.setattr(handlers, "SUPERUSER", forbidden_permission)
    _inject_semantic_assessment(monkeypatch, goals=("guidance",))

    async def fixed_guidance(*_: object, **__: object) -> object:
        return handlers._GuidanceResult("公开教学", ())

    monkeypatch.setattr(handlers, "_capability_guidance_result", fixed_guidance)
    first = fake_group_message_event_v11(
        message_id=1_929,
        user_id=360,
        message=Message("triage 参数怎么用"),
        original_message=Message("triage 参数怎么用"),
        raw_message="triage 参数怎么用",
        to_me=False,
    )
    second = fake_group_message_event_v11(
        message_id=1_930,
        user_id=360,
        message=Message("triage 示例怎么用"),
        original_message=Message("triage 示例怎么用"),
        raw_message="triage 示例怎么用",
        to_me=False,
    )
    async with app.test_matcher(handlers.support_matcher) as ctx:
        bot = _onebot_test_bot(ctx)
        for event in (first, second):
            ctx.receive_event(bot, event)
            ctx.should_call_send(
                event,
                Message("公开教学"),
                result=None,
            )
            ctx.should_finished(handlers.support_matcher)
    assert checks == []


async def test_sensitive_support_text_is_refused_before_capability_or_incident(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nbtriage.support_threads import ThreadStatus
    from nonebot_plugin_triage import handlers

    runtime = _install_isolated_support_threads(monkeypatch)

    async def unexpected_guidance(*_: object, **__: object) -> object:
        raise AssertionError("policy-blocked text must not read capability sources")

    def unexpected_report(*_: object, **__: object) -> object:
        raise AssertionError("policy-blocked text must not enter incident intake")

    monkeypatch.setattr(handlers, "_capability_guidance_result", unexpected_guidance)
    monkeypatch.setattr(handlers.plugin_runtime.report_service, "handle", unexpected_report)
    incident_count = len(handlers.plugin_runtime.incidents)
    event = fake_group_message_event_v11(
        message_id=123,
        user_id=229,
        message=Message("triage api_key=abcdefghijklmnopqrstuvwxyz123456"),
        original_message=Message("triage api_key=abcdefghijklmnopqrstuvwxyz123456"),
        raw_message="triage api_key=abcdefghijklmnopqrstuvwxyz123456",
        to_me=False,
    )

    async with app.test_matcher(handlers.support_matcher) as ctx:
        bot = _onebot_test_bot(ctx)
        ctx.receive_event(bot, event)
        ctx.should_call_send(
            event,
            Message("求助内容可能包含密钥或其他敏感信息，请移除后重新发送。"),
            result=None,
        )
        ctx.should_finished(handlers.support_matcher)

    records = tuple(runtime.support_threads._entries.values())
    assert len(records) == 1
    assert records[0].status is ThreadStatus.CLOSED
    assert len(handlers.plugin_runtime.incidents) == incident_count


async def test_private_semantic_guidance_uses_common_routing(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nonebot import get_driver
    from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter
    from nonebot.adapters.onebot.v11 import Bot as OneBotV11Bot

    from nonebot_plugin_triage import handlers

    _inject_semantic_assessment(monkeypatch, goals=("guidance",))
    incident_count = len(handlers.plugin_runtime.incidents)
    text = "triage 某个功能怎么使用"
    async with app.test_matcher(handlers.support_matcher) as ctx:
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
        ctx.should_finished(handlers.support_matcher)

    assert len(handlers.plugin_runtime.incidents) == incident_count


@pytest.mark.parametrize(
    ("goals", "authorized", "expected"),
    [
        (
            ("behavior_exploration",),
            False,
            "该请求需要部署维护者权限；本轮不会读取内部配置、源码、环境或运行证据。",
        ),
        (
            ("behavior_exploration",),
            True,
            "已识别为行为探索并通过维护者鉴权；证据探索还未接通，本轮不会读取内部配置、源码、环境或运行证据。",
        ),
        (
            ("feature_feedback",),
            None,
            "我识别到这是一项功能建议；反馈生命周期还未接通，本轮不会建立故障记录或外部工单。",
        ),
    ],
)
async def test_semantic_candidate_routes_have_specific_zero_side_effect_responses(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
    goals: tuple[str, ...],
    authorized: bool | None,
    expected: str,
) -> None:
    from nonebot_plugin_triage import handlers

    _inject_semantic_assessment(monkeypatch, goals=goals)
    if authorized is None:

        async def unexpected_permission(*_: object, **__: object) -> bool:
            raise AssertionError("non-behavior routes must not check SUPERUSER")

        monkeypatch.setattr(handlers, "SUPERUSER", unexpected_permission)
    else:

        async def behavior_permission(*_: object, **__: object) -> bool:
            return authorized

        monkeypatch.setattr(handlers, "SUPERUSER", behavior_permission)
    monkeypatch.setattr(handlers.plugin_runtime.support_rate_limiter, "allow", lambda *_: True)
    incident_count = len(handlers.plugin_runtime.incidents)
    event = fake_group_message_event_v11(
        message_id=2_400 + len(goals),
        user_id=2_400 + int(bool(authorized)),
        message=Message("triage 合成候选请求"),
        original_message=Message("triage 合成候选请求"),
        raw_message="triage 合成候选请求",
        to_me=False,
    )

    async with app.test_matcher(handlers.support_matcher) as ctx:
        bot = _onebot_test_bot(ctx)
        ctx.receive_event(bot, event)
        ctx.should_call_send(event, Message(expected), result=None)
        ctx.should_finished(handlers.support_matcher)

    assert len(handlers.plugin_runtime.incidents) == incident_count


def test_domain_core_does_not_import_nonebot_transport_types() -> None:
    core_root = Path(__file__).parents[2] / "src" / "nbtriage"

    offenders = []
    for path in core_root.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        if "from nonebot" in source or "import nonebot" in source:
            offenders.append(path.name)

    assert offenders == []
