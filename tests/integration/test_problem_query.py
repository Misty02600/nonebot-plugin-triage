from __future__ import annotations

from dataclasses import replace
from typing import Any, cast

import pytest
from nonebot_plugin_alconna.uniseg.fallback import FallbackMessage
from nonebug import App
from tests.integration._plugin_helpers import _group_text_event


async def _allow_support_requests(*_: object) -> bool:
    return True


@pytest.fixture(autouse=True)
def isolate_message_cache():
    # NoneBug 的 fallback 消息标识会在不同测试中复用；不能复用前一题的输入。
    from nonebot_plugin_alconna.extension import unimsg_cache, unimsg_origin_cache

    unimsg_cache.clear()
    unimsg_origin_cache.clear()
    yield
    unimsg_cache.clear()
    unimsg_origin_cache.clear()


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
    monkeypatch.setattr(handlers, "_support_request_allowed", _allow_support_requests)
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
    from nbtriage.bug.assessment import BugVerdict
    from nbtriage.bug.workflow import (
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
    monkeypatch.setattr(handlers, "_support_request_allowed", _allow_support_requests)
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
                "- P-23456789｜搜图没有返回结果｜未复核｜待处理｜"
                "最近观察：2026-08-16T00:00:00+00:00"
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
    monkeypatch.setattr(handlers, "_support_request_allowed", _allow_support_requests)
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
    monkeypatch.setattr(handlers, "_support_request_allowed", _allow_support_requests)

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


@pytest.mark.parametrize("user_id", [200, 201])
async def test_split_command_keeps_maintainer_authorization_and_passes_exact_occurrence(
    app, monkeypatch, user_id
):
    from nbtriage.bug.assessment import BugVerdict
    from nbtriage.bug.workflow import (
        ProblemDecisionSource,
        ProblemDetails,
        ProblemLifecycle,
        ProblemReviewStatus,
        ProblemSummary,
        format_problem_details,
    )
    from nonebot_plugin_triage import handlers

    calls = []
    details = ProblemDetails(
        summary=ProblemSummary(
            problem_id="P-3456789A",
            title="搜索失败",
            subject_id="search",
            verdict=BugVerdict.BUG,
            decision_source=ProblemDecisionSource.AGENT,
            review_status=ProblemReviewStatus.UNREVIEWED,
            lifecycle=ProblemLifecycle.OPEN,
            report_count=1,
            occurrence_count=1,
            last_observed_at="2026-09-13",
            latest_decision_at="2026-09-13",
        ),
        responsibility_candidates=(),
    )

    class Repository:
        async def split_occurrence(self, problem_id, occurrence_key, **kwargs):
            calls.append((problem_id, occurrence_key, kwargs))
            return details

    monkeypatch.setattr(
        handlers,
        "plugin_runtime",
        replace(handlers.plugin_runtime, bug_workflow_repository=cast(Any, Repository())),
    )
    monkeypatch.setattr(handlers, "_support_request_allowed", _allow_support_requests)
    async with app.test_matcher(handlers.query_matcher) as ctx:
        bot = ctx.create_bot()
        event = _group_text_event(
            "triage 报错查询 P-23456789 拆分 " + "a" * 64,
            message_id=112,
            user_id=user_id,
            to_me=True,
        )
        ctx.receive_event(bot, event)
        reply = (
            (
                "已拆分为独立问题，保留原调查并停用原指纹的自动合并；新问题等待复核。\n"
                + format_problem_details(details)
            )
            if user_id == 200
            else "该命令仅供主人使用。"
        )
        ctx.should_call_send(event, FallbackMessage(reply), result=None)
        ctx.should_finished(handlers.query_matcher)
    if user_id == 200:
        assert len(calls) == 1
        assert calls[0][:2] == ("P-23456789", "a" * 64)
        assert calls[0][2]["actor_scope_hmac"] and calls[0][2]["idempotency_key"]
    else:
        assert calls == []


async def test_split_requires_an_occurrence_before_repository_access(app, monkeypatch):
    from nonebot_plugin_triage import handlers

    class ForbiddenRepository:
        async def split_occurrence(self, *args, **kwargs):
            raise AssertionError("incomplete command must not mutate records")

    monkeypatch.setattr(
        handlers,
        "plugin_runtime",
        replace(handlers.plugin_runtime, bug_workflow_repository=cast(Any, ForbiddenRepository())),
    )
    monkeypatch.setattr(handlers, "_support_request_allowed", _allow_support_requests)
    async with app.test_matcher(handlers.query_matcher) as ctx:
        bot = ctx.create_bot()
        event = _group_text_event(
            "triage 报错查询 P-23456789 拆分", message_id=113, user_id=200, to_me=True
        )
        ctx.receive_event(bot, event)
        ctx.should_call_send(
            event,
            FallbackMessage(
                "用法：triage 报错查询 <问题编号> 拆分 <发生标识>；先用“发生记录”查看标识。"
            ),
            result=None,
        )
        ctx.should_finished(handlers.query_matcher)
