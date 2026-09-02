from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from nonebot.adapters.onebot.v11 import Message
from nonebug import App
from tests.integration._plugin_helpers import (
    _BehaviorServiceProbe,
    _group_text_event,
    _inject_semantic_assessment,
    _install_behavior_probe,
    _onebot_test_bot,
)


async def test_explicit_behavior_route_commits_delivery_state_after_send(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nonebot_plugin_triage import handlers

    probe = _BehaviorServiceProbe()
    _install_behavior_probe(monkeypatch, probe)
    _inject_semantic_assessment(monkeypatch, goals=("behavior_exploration",))
    authorization_events: list[Any] = []
    resolved_receipts: list[object] = []

    async def authorized(_bot: object, event: object) -> bool:
        authorization_events.append(event)
        return True

    def resolve_receipt(
        value: object,
        *,
        bot: object,
        expected_target: object,
    ) -> str:
        del bot, expected_target
        resolved_receipts.append(value)
        return "outgoing-message-2601"

    monkeypatch.setattr(handlers, "SUPERUSER", authorized)
    monkeypatch.setattr(handlers, "resolve_outgoing_receipt", resolve_receipt)
    event = _group_text_event(
        "triage 为什么当前插件没有触发",
        message_id=2_601,
        user_id=200,
        to_me=False,
    )

    async with app.test_matcher(handlers.support_matcher) as ctx:
        bot = _onebot_test_bot(ctx)
        ctx.receive_event(bot, event)
        ctx.should_call_send(event, Message("行为解释 1"), result=None)
        ctx.should_finished(handlers.support_matcher)

    assert len(probe.explorations) == 1
    request = probe.explorations[0]
    assert request.question == "为什么当前插件没有触发"
    assert request.event_reference == "message_id:2601"
    assert request.scope.bot_scope == "1"
    assert request.scope.actor_scope == "200"
    assert probe.begin_calls == [
        {
            "scope": request.scope,
            "turn_id": "turn-1",
            "delivery_token": "delivery-1",
            "authorized": True,
        }
    ]
    assert probe.finish_calls == [
        {
            "scope": request.scope,
            "turn_id": "turn-1",
            "delivery_token": "delivery-1",
            "receipt_reference": "outgoing-message-2601",
        }
    ]
    assert probe.abandon_calls == []
    assert len(resolved_receipts) == 1
    assert len(authorization_events) >= 5


@pytest.mark.parametrize("semantic_status", ["needs_clarification", "unsupported"])
async def test_active_behavior_inquiry_continues_unresolved_or_out_of_scope_text(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
    semantic_status: str,
) -> None:
    from nonebot_plugin_triage import handlers

    probe = _BehaviorServiceProbe(active=True)
    _install_behavior_probe(monkeypatch, probe)
    _inject_semantic_assessment(monkeypatch, status=semantic_status)

    async def authorized(*_: object, **__: object) -> bool:
        return True

    monkeypatch.setattr(handlers, "SUPERUSER", authorized)
    monkeypatch.setattr(
        handlers,
        "resolve_outgoing_receipt",
        lambda *_args, **_kwargs: "outgoing-message-2602",
    )
    event = _group_text_event(
        "triage 那配置覆盖之后呢",
        message_id=2_602,
        user_id=200,
        to_me=False,
    )

    async with app.test_matcher(handlers.support_matcher) as ctx:
        bot = _onebot_test_bot(ctx)
        ctx.receive_event(bot, event)
        ctx.should_call_send(event, Message("行为解释 1"), result=None)
        ctx.should_finished(handlers.support_matcher)

    assert len(probe.active_checks) == 1
    assert len(probe.explorations) == 1
    assert probe.active_checks[0] == probe.explorations[0].scope
    assert probe.explorations[0].question == "那配置覆盖之后呢"
    assert len(probe.finish_calls) == 1
    assert probe.abandon_calls == []


@pytest.mark.parametrize("explicit_route", ["bug", "feature", "guidance"])
async def test_explicit_short_routes_are_not_hijacked_by_active_behavior_inquiry(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
    explicit_route: str,
) -> None:
    from nonebot_plugin_triage import handlers

    probe = _BehaviorServiceProbe(active=True)
    _install_behavior_probe(monkeypatch, probe)

    if explicit_route == "feature":
        _inject_semantic_assessment(monkeypatch, goals=("feature_feedback",))
        expected = (
            "我识别到这是一项功能建议；反馈生命周期还未接通，本轮不会建立故障记录或外部工单。"
        )
    elif explicit_route == "guidance":
        _inject_semantic_assessment(monkeypatch, goals=("guidance",))

        async def fixed_guidance(*_: object, **__: object) -> object:
            return handlers._GuidanceResult("公开教学", ())

        monkeypatch.setattr(handlers, "_capability_guidance_result", fixed_guidance)
        expected = "公开教学"
    else:
        from nbtriage.bug.assessment import (
            BugAssessmentDecision,
            BugDecisionSource,
            BugOccurrence,
            BugReason,
            BugResponsibility,
            BugVerdict,
            format_bug_assessment_reply,
        )
        from nonebot_plugin_triage.bug.assessment import (
            BugAssessmentRuntimeOutcome,
        )

        _inject_semantic_assessment(
            monkeypatch,
            goals=("bug_assessment",),
            reported_observation=True,
        )
        decision = BugAssessmentDecision(
            verdict=BugVerdict.NOT_BUG,
            occurrence=BugOccurrence.SINGLE_OBSERVED,
            responsibility_candidates=(BugResponsibility.INTENTIONAL_CONFIGURATION,),
            reason=BugReason.INTENTIONAL_CONFIGURATION,
            evidence_ids=("test-evidence",),
            missing_evidence=(),
            source=BugDecisionSource.AGENT,
        )

        async def assessed(*_: object, **__: object) -> BugAssessmentRuntimeOutcome:
            return BugAssessmentRuntimeOutcome(decision)

        monkeypatch.setattr(handlers, "_bug_assessment_decision", assessed)
        expected = format_bug_assessment_reply(decision)

    async def forbidden_behavior_permission(*_: object, **__: object) -> bool:
        raise AssertionError("explicit short routes must not inspect Behavior authorization")

    monkeypatch.setattr(handlers, "SUPERUSER", forbidden_behavior_permission)
    text = f"triage explicit {explicit_route} request"
    event = _group_text_event(
        text,
        message_id=2_610 + len(explicit_route),
        user_id=301,
        to_me=False,
    )

    async with app.test_matcher(handlers.support_matcher) as ctx:
        bot = _onebot_test_bot(ctx)
        ctx.receive_event(bot, event)
        ctx.should_call_send(event, Message(expected), result=None)
        ctx.should_finished(handlers.support_matcher)

    assert probe.active_checks == []
    assert probe.explorations == []
    assert probe.begin_calls == []


async def test_behavior_reset_permission_precedes_scoped_workspace_delete(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nonebot_plugin_triage import handlers

    probe = _BehaviorServiceProbe(active=True)
    _install_behavior_probe(monkeypatch, probe)
    denied = _group_text_event(
        "triage 行为重置",
        message_id=2_620,
        user_id=201,
        to_me=False,
    )
    allowed = _group_text_event(
        "triage 行为重置",
        message_id=2_621,
        user_id=200,
        to_me=False,
    )

    async with app.test_matcher(handlers.behavior_reset_matcher) as ctx:
        bot = _onebot_test_bot(ctx)
        ctx.receive_event(bot, denied)
        ctx.should_not_pass_permission(handlers.behavior_reset_matcher)
        assert probe.delete_calls == []
        ctx.receive_event(bot, allowed)
        ctx.should_pass_permission(handlers.behavior_reset_matcher)
        ctx.should_call_send(
            allowed,
            Message("已删除当前维护者在当前会话的长期行为工作区。"),
            result=None,
        )
        ctx.should_finished(handlers.behavior_reset_matcher)

    assert len(probe.delete_calls) == 1
    deleted_scope = probe.delete_calls[0]
    assert deleted_scope.bot_scope == "1"
    assert deleted_scope.actor_scope == "200"


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
    from nonebot_plugin_triage.behavior_exploration_runtime import (
        UnavailableBehaviorExplorationService,
    )

    _inject_semantic_assessment(monkeypatch, goals=goals)
    runtime = replace(
        handlers.plugin_runtime,
        behavior_exploration_service=UnavailableBehaviorExplorationService(),
    )
    monkeypatch.setattr(handlers, "plugin_runtime", runtime)
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
    event = _group_text_event(
        "triage 合成候选请求",
        message_id=2_400 + len(goals),
        user_id=2_400 + int(bool(authorized)),
        to_me=False,
    )

    async with app.test_matcher(handlers.support_matcher) as ctx:
        bot = _onebot_test_bot(ctx)
        ctx.receive_event(bot, event)
        ctx.should_call_send(event, Message(expected), result=None)
        ctx.should_finished(handlers.support_matcher)

    assert len(handlers.plugin_runtime.incidents) == incident_count
