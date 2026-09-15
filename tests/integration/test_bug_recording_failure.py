from __future__ import annotations

from dataclasses import replace
from typing import Any, cast


async def test_failed_bug_recording_closes_without_a_success_receipt(app, monkeypatch):
    from nonebot.adapters.onebot.v11 import Message
    from tests.bug.test_bug_workflow import _command
    from tests.integration._plugin_helpers import (
        _group_text_event,
        _inject_semantic_assessment,
        _install_isolated_support_threads,
        _onebot_test_bot,
    )

    from nbtriage.bug.assessment import BugAssessmentDecision
    from nbtriage.support.threads import ThreadStatus
    from nonebot_plugin_triage import handlers
    from nonebot_plugin_triage.bug.assessment import BugAssessmentRuntimeOutcome

    runtime = _install_isolated_support_threads(monkeypatch)
    _inject_semantic_assessment(monkeypatch, goals=("bug_assessment",), reported_observation=True)
    command = _command(report_key="failed-save-report", occurrence_key="failed-save-occurrence")
    decision = BugAssessmentDecision.model_validate(
        {
            "verdict": "bug",
            "occurrence": "single_observed",
            "responsibility_candidates": ["target_plugin"],
            "reason": "implementation_contradicts_contract",
            "evidence_ids": ["log:fixture"],
            "missing_evidence": [],
            "source": "agent",
        }
    )
    attempts = []

    class FailingRepository:
        async def record_bug(self, received):
            attempts.append(received)
            raise OSError("isolated storage failure")

    async def investigated(*_args, **_kwargs):
        return BugAssessmentRuntimeOutcome(decision, command)

    monkeypatch.setattr(handlers, "_bug_assessment_decision", investigated)
    monkeypatch.setattr(
        handlers,
        "plugin_runtime",
        replace(handlers.plugin_runtime, bug_workflow_repository=cast(Any, FailingRepository())),
    )
    event = _group_text_event(
        "triage 搜图一直停在正在搜索",
        message_id=291451,
        user_id=291452,
        to_me=False,
    )
    async with app.test_matcher(handlers.support_matcher) as ctx:
        bot = _onebot_test_bot(ctx)
        ctx.receive_event(bot, event)
        ctx.should_call_send(
            event,
            Message("已经完成判断，但问题记录暂时失败，请等待主人处理。"),
            result=None,
        )
        ctx.should_finished(handlers.support_matcher)
    assert attempts == [command]
    entries = tuple(runtime.support_threads._entries.values())
    assert len(entries) == 1 and entries[0].status is ThreadStatus.CLOSED
    assert not runtime.support_turns._thread_by_scope
