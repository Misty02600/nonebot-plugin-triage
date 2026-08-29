from __future__ import annotations

from dataclasses import replace
from typing import Any, cast

import pytest
from nonebot_plugin_alconna.uniseg.fallback import FallbackMessage
from nonebug import App
from tests.integration._plugin_helpers import _group_text_event


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
        event = _group_text_event(
            "triage 报错查询 P-23456789",
            message_id=101,
            user_id=200,
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
        event = _group_text_event(
            "triage 报错查询",
            group_id=76_543_210,
            message_id=102,
            user_id=200,
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
        event = _group_text_event(
            "triage 报错查询",
            group_id=76_543_211,
            message_id=103,
            user_id=201,
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
        event = _group_text_event(
            "triage 报错查询 123",
            group_id=76_543_212,
            message_id=104,
            user_id=200,
            to_me=True,
        )
        ctx.receive_event(bot, event)
        ctx.should_call_send(
            event,
            FallbackMessage("问题编号格式不正确；请使用以 P- 开头的完整编号。"),
            result=None,
        )
        ctx.should_finished(handlers.query_matcher)
